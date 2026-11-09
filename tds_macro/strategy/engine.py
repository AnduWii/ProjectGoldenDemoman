"""Playback engine: turns a :class:`Strategy` into timed input.

Design notes that matter:

* **Nothing blocks.**  Steps are expressed as generators that *yield* commands
  (``Sleep``, ``Do``, ``AwaitState``); a driver advances them on each
  :meth:`StrategyRunner.tick`.  The clock is injected, so every timing path --
  retries, timeouts, focus pauses -- is unit-testable without real delays, and
  the UI thread is never held up.
* **Focus is checked every tick**, not once per step.  Alt-tabbing mid-run
  pauses playback and releases held keys; deadlines are shifted by the pause
  duration so a 10-second wait does not silently expire while the user is in
  another window.
* **Failed placements are detected by cash**, the only signal available from
  outside the process.  If cash cannot be read the engine says so rather than
  pretending the placement worked.
"""

from __future__ import annotations

import logging
import random
from collections.abc import Callable, Generator
from dataclasses import dataclass, field
from enum import Enum
from typing import Protocol

from ..config import ActionBinding, Keybinds, Timing
from ..input.coords import NormPoint, remap_world_point
from ..input.sendinput import InputBackend, MouseButton
from .steps import (
    Ability,
    Click,
    Drag,
    KeyPress,
    Place,
    Scroll,
    Sell,
    SkipWave,
    Step,
    Strategy,
    Upgrade,
    Wait,
    WaitForCash,
    WaitForWave,
)

log = logging.getLogger(__name__)


# --- observed game state ------------------------------------------------------
@dataclass(frozen=True)
class GameState:
    """What the vision layer currently believes about the match.

    ``None`` means "could not read", which is deliberately distinct from 0 --
    an unreadable cash value must never satisfy a ``WaitForCash(0)``.
    """

    wave: int | None = None
    cash: int | None = None
    confidence: float = 0.0
    end_screen: bool = False
    timestamp_ms: int = 0

    def describe(self) -> str:
        wave = self.wave if self.wave is not None else "?"
        cash = f"${self.cash:,}" if self.cash is not None else "$?"
        return f"wave {wave}, {cash}"


class GameStateProvider(Protocol):
    def snapshot(self) -> GameState: ...


class CashDropWatcher:
    """Decides whether a purchase happened, from cash readings alone.

    Comparing against the pre-click figure fails whenever kill or wave income
    lands in the same window: $1000, buy for $300, earn $400, and cash never
    reads below $1000 -- so the engine retried and bought a second tower.

    Cash only ever *decreases* because of a purchase, so the watcher tracks the
    highest reading seen since the baseline and confirms on any reading below
    it. Income that arrives before the purchase shows up raises the peak, and
    the purchase then registers as a drop from that peak.

    Residual risk, stated rather than hidden: if the purchase and a larger burst
    of income both land between two consecutive readings, no drop is ever
    visible and a retry can still double-place. Readings every ~150 ms make
    that rare, not impossible.
    """

    def __init__(self, baseline: int) -> None:
        self.peak = baseline
        self.low = baseline

    def __call__(self, state: GameState) -> bool:
        if state.cash is None:
            return False
        if state.cash < self.peak:
            self.low = state.cash
            return True
        self.peak = state.cash
        return False


class FocusProvider(Protocol):
    def is_foreground(self) -> bool: ...


# --- commands the step generators yield ---------------------------------------
@dataclass(frozen=True)
class Sleep:
    ms: int


@dataclass(frozen=True)
class Do:
    """Perform one input primitive.  ``fn`` returns False if the send failed."""

    fn: Callable[[], bool]
    description: str


@dataclass(frozen=True)
class AwaitState:
    """Poll game state until ``predicate`` holds or ``timeout_ms`` elapses.

    Resumes the generator with ``True`` (satisfied) or ``False`` (timed out).
    """

    predicate: Callable[[GameState], bool]
    description: str
    timeout_ms: int = 0


Command = Sleep | Do | AwaitState
# Values are sent *into* these generators (an AwaitState resumes with
# satisfied/timed-out), so Iterator is not a strong enough type: .send() and
# .close() are both part of the contract.
Program = Generator[Command, object, None]


def _move_to(backend: InputBackend, point: NormPoint) -> Callable[[], bool]:
    """A move bound to one point -- a plain lambda in a loop would late-bind
    and send every drag sample to the last point."""
    return lambda: backend.move(point)


class RunStatus(Enum):
    IDLE = "idle"
    RUNNING = "running"
    WAITING = "waiting"
    PAUSED_FOCUS = "paused (Roblox not in focus)"
    PAUSED_USER = "paused"
    STOPPED = "stopped"
    COMPLETED = "completed"
    FAILED = "failed"

    @property
    def is_active(self) -> bool:
        return self in {
            RunStatus.RUNNING,
            RunStatus.WAITING,
            RunStatus.PAUSED_FOCUS,
            RunStatus.PAUSED_USER,
        }

    @property
    def is_finished(self) -> bool:
        return self in {RunStatus.STOPPED, RunStatus.COMPLETED, RunStatus.FAILED}


LogFn = Callable[[str, str], None]


def null_log(level: str, message: str) -> None:
    log.log(logging.ERROR if level == "error" else logging.INFO, "%s", message)


@dataclass
class RunnerDeps:
    """Everything the runner talks to.  All swappable for tests."""

    backend: InputBackend
    state: GameStateProvider
    focus: FocusProvider
    keybinds: Keybinds = field(default_factory=Keybinds)
    timing: Timing = field(default_factory=Timing)
    clock: Callable[[], int] | None = None
    log: LogFn = null_log
    rng: random.Random = field(default_factory=random.Random)
    require_foreground: bool = True
    abort_on_timeout: bool = True
    # Without HUD calibration there is no cash to watch, so placements are sent
    # without the confirm-and-retry check instead of warning on every one.
    verify_placements: bool = True
    # Width / height of the game view right now, for moving tower spots
    # recorded on a screen of a different shape. None or 0 = leave them as-is.
    target_aspect: Callable[[], float] | None = None


class StrategyRunner:
    """Executes one pass of a strategy."""

    def __init__(self, strategy: Strategy, deps: RunnerDeps) -> None:
        self.strategy = strategy
        self.deps = deps
        self.timing = deps.timing.clamped()
        self.status = RunStatus.IDLE
        self.status_detail = ""
        self.failure_reason = ""
        self.current_index = -1
        self.steps_completed = 0
        self.placements_retried = 0
        self.placements_failed = 0
        self.match_ended_early = False

        self._program: Program | None = None
        self._wake_at = 0
        self._await: AwaitState | None = None
        self._await_deadline = 0
        self._send_value: object = None
        self._paused_at = 0
        self._in_program = False
        self._tower_positions: dict[str, NormPoint] = {}
        self._started_at = 0

    # -- clock ---------------------------------------------------------------
    def now(self) -> int:
        if self.deps.clock is not None:
            return self.deps.clock()
        import time

        return int(time.monotonic() * 1000)

    def _jitter(self, ms: int) -> int:
        """Add +/- jitter so repeated runs do not land on identical frame timings."""
        spread = self.timing.jitter_ms
        if spread <= 0 or ms <= 0:
            return max(0, ms)
        return max(0, ms + self.deps.rng.randint(-spread, spread))

    # -- lifecycle -----------------------------------------------------------
    def start(self) -> None:
        problems = self.strategy.validate()
        if problems:
            self.status = RunStatus.FAILED
            self.failure_reason = problems[0]
            self.deps.log("error", f"Strategy has problems; not starting: {problems[0]}")
            return
        enabled = [s for s in self.strategy.steps if s.enabled]
        if not enabled:
            self.status = RunStatus.COMPLETED
            self.deps.log("warn", "Strategy has no enabled steps; nothing to do.")
            return
        self._tower_positions = {}
        self._program = self._build_program()
        self._wake_at = 0
        self._await = None
        self._send_value = None
        self.current_index = -1
        self.steps_completed = 0
        self.placements_retried = 0
        self.placements_failed = 0
        self.match_ended_early = False
        self.failure_reason = ""
        self._started_at = self.now()
        self.status = RunStatus.RUNNING
        mode = "DRY RUN -- no input will be sent" if self.deps.backend.dry_run else "live"
        self.deps.log("info", f"Starting '{self.strategy.name}' ({len(enabled)} steps, {mode})")

    def stop(self, reason: str = "stopped by user") -> None:
        """Emergency stop.  Always releases held input, even on an error path."""
        if self.status.is_finished and self._program is None:
            return
        try:
            if self._program is not None:
                self._program.close()
        finally:
            self._program = None
            try:
                self.deps.backend.release_all()
            except Exception:  # pragma: no cover - defensive
                log.exception("release_all failed during stop")
            self.status = RunStatus.STOPPED
            self.status_detail = reason
            self.deps.log("warn", f"Playback stopped: {reason}")

    def pause(self) -> None:
        if self.status in {RunStatus.RUNNING, RunStatus.WAITING}:
            self._enter_pause(RunStatus.PAUSED_USER, "paused by user")

    def resume(self) -> None:
        if self.status is RunStatus.PAUSED_USER:
            self._leave_pause()

    def _enter_pause(self, status: RunStatus, message: str) -> None:
        self._paused_at = self.now()
        self.status = status
        self.status_detail = message
        # Drop anything held so a paused macro cannot leave a key stuck down.
        self.deps.backend.release_all()
        self.deps.log("warn", message.capitalize())

    def _leave_pause(self) -> None:
        elapsed = self.now() - self._paused_at
        # Shift deadlines by the pause so a wait does not expire while paused.
        if self._wake_at:
            self._wake_at += elapsed
        if self._await_deadline:
            self._await_deadline += elapsed
        self._paused_at = 0
        self.status = RunStatus.WAITING if self._await is not None else RunStatus.RUNNING
        self.deps.log("info", "Resumed.")

    # -- progress ------------------------------------------------------------
    @property
    def total_steps(self) -> int:
        return sum(1 for s in self.strategy.steps if s.enabled)

    def current_step(self) -> Step | None:
        if 0 <= self.current_index < len(self.strategy.steps):
            return self.strategy.steps[self.current_index]
        return None

    def describe_status(self) -> str:
        """One line for the status bar, e.g. 'Waiting for wave 12 - detected 11'."""
        if self.status_detail:
            return self.status_detail
        step = self.current_step()
        if step is not None and self.status.is_active:
            return f"Step {self.steps_completed + 1}/{self.total_steps}: {step.describe()}"
        return self.status.value.capitalize()

    @property
    def next_wake_at(self) -> int:
        """Clock value at which the next tick can do useful work (0 = now).

        Lets the UI idle instead of spinning, and lets tests jump the clock
        forward rather than simulating every millisecond.
        """
        return self._wake_at

    def elapsed_ms(self) -> int:
        return max(0, self.now() - self._started_at) if self._started_at else 0

    # -- the driver ----------------------------------------------------------
    def tick(self) -> None:
        """Advance playback.  Call frequently (every ~10-20 ms); never blocks."""
        if not self.status.is_active or self._program is None:
            return

        if self.status is RunStatus.PAUSED_USER:
            return

        if self.deps.require_foreground and not self.deps.focus.is_foreground():
            if self.status is not RunStatus.PAUSED_FOCUS:
                self._enter_pause(
                    RunStatus.PAUSED_FOCUS, "Roblox lost focus -- playback paused"
                )
            return
        if self.status is RunStatus.PAUSED_FOCUS:
            self._leave_pause()

        now = self.now()
        if self._wake_at and now < self._wake_at:
            return
        self._wake_at = 0

        # A match can end before the strategy does -- the base dies, or the
        # strategy simply outlasts the map. Without this the runner sat in a
        # WaitForWave that could never be satisfied until it timed out, and in
        # loop mode that timeout failed the whole loop instead of re-queueing.
        if self.deps.state.snapshot().end_screen:
            self._finish_match_ended()
            return

        if self._await is not None:
            self._poll_await(now)
            return

        self._advance()

    def _finish_match_ended(self) -> None:
        step = self.current_step()
        where = f" at step {self.current_index + 1} ({step.describe()})" if step else ""
        if self._program is not None:
            self._program.close()
            self._program = None
        self._await = None
        self._await_deadline = 0
        self.deps.backend.release_all()
        self.status = RunStatus.COMPLETED
        self.status_detail = ""
        self.match_ended_early = True
        self.deps.log(
            "warn",
            f"The match ended{where}; stopping the strategy. "
            f"{self.steps_completed} of {self.total_steps} steps had run.",
        )

    def _poll_await(self, now: int) -> None:
        pending = self._await
        assert pending is not None
        snapshot = self.deps.state.snapshot()
        if pending.predicate(snapshot):
            self._await = None
            self._await_deadline = 0
            self.status = RunStatus.RUNNING
            self.status_detail = ""
            self._send_value = True
            self._advance()
            return
        if self._await_deadline and now >= self._await_deadline:
            self._await = None
            self._await_deadline = 0
            self.status = RunStatus.RUNNING
            self._send_value = False
            self._advance()
            return
        # Still waiting: show what we want versus what we see.
        self.status = RunStatus.WAITING
        self.status_detail = f"Waiting for {pending.description} - detected {snapshot.describe()}"
        self._wake_at = now + self.timing.poll_interval_ms

    def _advance(self) -> None:
        assert self._program is not None
        while True:
            try:
                self._in_program = True
                command = self._program.send(self._send_value)
            except StopIteration:
                self._program = None
                # A step that called fail() unwinds the generator normally; the
                # failure status must survive that, not be overwritten here.
                if self.status is RunStatus.FAILED:
                    return
                self.status = RunStatus.COMPLETED
                self.status_detail = ""
                self.deps.log(
                    "info",
                    f"Finished '{self.strategy.name}' in {self.elapsed_ms() / 1000:.1f}s "
                    f"({self.steps_completed} steps, {self.placements_retried} retries, "
                    f"{self.placements_failed} failed placements)",
                )
                return
            except Exception as exc:  # pragma: no cover - defensive
                log.exception("strategy program raised")
                self._program = None
                self.failure_reason = str(exc)
                self.status = RunStatus.FAILED
                self.deps.backend.release_all()
                self.deps.log("error", f"Playback failed: {exc}")
                return
            finally:
                self._in_program = False
            self._send_value = None

            if isinstance(command, Sleep):
                if command.ms > 0:
                    self._wake_at = self.now() + command.ms
                    return
                continue
            if isinstance(command, Do):
                if not command.fn():
                    error = self.deps.backend.last_error() or "input was rejected"
                    self.deps.log("error", f"Could not {command.description}: {error}")
                continue
            if isinstance(command, AwaitState):
                self._await = command
                self._await_deadline = (
                    self.now() + command.timeout_ms if command.timeout_ms > 0 else 0
                )
                self.status = RunStatus.WAITING
                self._wake_at = 0
                self._poll_await(self.now())
                return

    def fail(self, reason: str) -> None:
        self.failure_reason = reason
        self.status = RunStatus.FAILED
        self.status_detail = reason
        # When a step generator calls this, it is still on the stack and cannot
        # close itself; it returns on its own because _build_program checks the
        # status after each step.
        if self._program is not None and not self._in_program:
            self._program.close()
            self._program = None
        self.deps.backend.release_all()
        self.deps.log("error", reason)

    # -- program construction -------------------------------------------------
    def _build_program(self) -> Program:
        for index, step in enumerate(self.strategy.steps):
            if not step.enabled:
                continue
            self.current_index = index
            self.status_detail = ""
            self.deps.log("step", f"[{index + 1}] {step.describe()}")
            yield from self._execute(step)
            if self.status is RunStatus.FAILED:
                return
            self.steps_completed += 1
            yield Sleep(self._jitter(self.timing.action_delay_ms))

    def _execute(self, step: Step) -> Program:
        if isinstance(step, Place):
            yield from self._do_place(step)
        elif isinstance(step, Upgrade):
            yield from self._do_tower_action(step.tower_id, self.deps.keybinds.upgrade,
                                             "upgrade", step.times)
        elif isinstance(step, Sell):
            yield from self._do_tower_action(step.tower_id, self.deps.keybinds.sell, "sell", 1)
        elif isinstance(step, Ability):
            yield from self._do_tower_action(step.tower_id, self.deps.keybinds.ability,
                                             "ability", 1)
        elif isinstance(step, SkipWave):
            yield from self._do_skip(step)
        elif isinstance(step, Wait):
            yield Sleep(max(0, step.ms))
        elif isinstance(step, WaitForWave):
            yield from self._do_wait_for_wave(step)
        elif isinstance(step, WaitForCash):
            yield from self._do_wait_for_cash(step)
        elif isinstance(step, Click):
            yield from self._click_at(step.position, MouseButton(step.button))
        elif isinstance(step, KeyPress):
            yield from self._tap_key(step.key, max(self.timing.key_hold_ms, step.hold_ms))
        elif isinstance(step, Drag):
            yield from self._do_drag(step)
        elif isinstance(step, Scroll):
            yield from self._do_scroll(step)
        else:  # pragma: no cover - every kind is handled above
            self.deps.log("error", f"Unsupported step: {step!r}")

    # -- primitives -----------------------------------------------------------
    def _tap_key(self, key: str, hold_ms: int | None = None) -> Program:
        backend = self.deps.backend
        yield Do(lambda: backend.key_down(key), f"press {key.upper()}")
        yield Sleep(self.timing.key_hold_ms if hold_ms is None else hold_ms)
        yield Do(lambda: backend.key_up(key), f"release {key.upper()}")

    def _click_at(self, point: NormPoint, button: MouseButton = MouseButton.LEFT) -> Program:
        backend = self.deps.backend
        yield Do(lambda: backend.move(point), f"move to ({point.x:.3f}, {point.y:.3f})")
        yield Sleep(self.timing.move_settle_ms)
        yield Do(lambda: backend.mouse_down(button), f"press the {button.value} mouse button")
        yield Sleep(self.timing.click_hold_ms)
        yield Do(lambda: backend.mouse_up(button), f"release the {button.value} mouse button")

    def _world(self, point: NormPoint) -> NormPoint:
        """A spot on the map, adjusted for the shape of the current game view."""
        provider = self.deps.target_aspect
        target = provider() if provider is not None else 0.0
        return remap_world_point(point, self.strategy.aspect_ratio, target or 0.0)

    def _do_drag(self, step: Drag) -> Program:
        """Replay a drag along its recorded path with its original timing."""
        if not step.path:
            return
        backend = self.deps.backend
        button = MouseButton(step.button)
        start = step.path[0].position
        yield Do(lambda: backend.move(start), "move to the start of the drag")
        yield Sleep(self.timing.move_settle_ms)
        yield Do(lambda: backend.mouse_down(button), f"press the {button.value} mouse button")
        elapsed = 0
        for sample in step.path[1:]:
            if sample.t_ms > elapsed:
                yield Sleep(sample.t_ms - elapsed)
                elapsed = sample.t_ms
            yield Do(_move_to(backend, sample.position), "move during the drag")
        yield Sleep(self.timing.click_hold_ms)
        yield Do(lambda: backend.mouse_up(button), f"release the {button.value} mouse button")

    def _do_scroll(self, step: Scroll) -> Program:
        backend = self.deps.backend
        point = step.position
        # Roblox zooms toward the pointer, so the wheel turns where it did.
        yield Do(lambda: backend.move(point), f"move to ({point.x:.3f}, {point.y:.3f})")
        yield Sleep(self.timing.move_settle_ms)
        clicks = step.clicks
        yield Do(lambda: backend.scroll(clicks), f"scroll {clicks:+d}")

    def _apply_binding(self, binding: ActionBinding, label: str) -> Program:
        """Trigger a configured action, by key or by clicking a menu button."""
        if not binding.is_configured():
            self.deps.log(
                "error",
                f"No keybind configured for {label}. Set it in Settings > Keybinds.",
            )
            return
        if binding.mode == "key":
            yield from self._tap_key(binding.key)
        elif binding.point is not None:
            yield from self._click_at(binding.point)

    def _cash(self) -> int | None:
        return self.deps.state.snapshot().cash

    # -- steps ----------------------------------------------------------------
    def _do_place(self, step: Place) -> Program:
        key = self.deps.keybinds.slot_key(step.slot)
        if key is None:
            self.deps.log(
                "error",
                f"Hotbar slot {step.slot} has no key bound; set it in Settings > Keybinds.",
            )
            return

        position = self._world(step.position)
        if position != step.position and not position.is_inside():
            self.deps.log(
                "warn",
                f"{step.tower_id}'s spot falls outside this screen's view (it was recorded "
                "on a wider screen); placing it at the nearest edge.",
            )

        if not self.deps.verify_placements:
            # No HUD calibration, so nothing to verify against: place it the
            # way TinyTask would, and track the spot for later tower actions.
            yield from self._tap_key(key)
            yield Sleep(self._jitter(self.timing.action_delay_ms))
            yield from self._click_at(position)
            self._tower_positions[step.tower_id] = position
            return

        attempts = self.timing.place_retries + 1
        for attempt in range(1, attempts + 1):
            yield from self._tap_key(key)
            yield Sleep(self._jitter(self.timing.action_delay_ms))
            # Read the baseline as late as possible -- after selecting the slot,
            # immediately before the click -- so as little income as possible
            # can land between it and the purchase.
            before = self._cash()
            yield from self._click_at(position)
            yield Sleep(self.timing.place_confirm_ms)

            if before is None or self._cash() is None:
                self.deps.log(
                    "warn",
                    f"Placed {step.tower_id} but could not verify it: cash is unreadable. "
                    "Re-run HUD calibration if this keeps happening.",
                )
                self._tower_positions[step.tower_id] = position
                return

            watcher = CashDropWatcher(before)
            confirmed = yield AwaitState(
                predicate=watcher,
                description=f"the cash drop from placing {step.tower_id}",
                timeout_ms=self.timing.place_verify_ms,
            )
            if confirmed:
                self._tower_positions[step.tower_id] = position
                self.deps.log(
                    "info",
                    f"Placed {step.tower_id} (cash ${watcher.peak:,} -> ${watcher.low:,})",
                )
                return
            if attempt < attempts:
                self.placements_retried += 1
                self.deps.log(
                    "warn",
                    f"Placement of {step.tower_id} did not go through "
                    f"(cash never dropped from ${watcher.peak:,} within "
                    f"{(self.timing.place_confirm_ms + self.timing.place_verify_ms) / 1000:.1f}s); "
                    f"retry {attempt}/{attempts - 1}",
                )
                yield Sleep(self._jitter(self.timing.action_delay_ms * 2))

        self.placements_failed += 1
        self.deps.log(
            "error",
            f"Gave up placing {step.tower_id} after {attempts} attempts -- not enough cash, "
            "an invalid spot, or the hotbar slot holds a different tower.",
        )
        # Record the position anyway so later Upgrade/Sell steps still aim
        # somewhere sensible instead of silently doing nothing.
        self._tower_positions.setdefault(step.tower_id, position)

    def _do_tower_action(
        self, tower_id: str, binding: ActionBinding, label: str, times: int
    ) -> Program:
        point = self._tower_positions.get(tower_id)
        if point is None:
            self.deps.log(
                "error", f"Cannot {label} {tower_id}: it has not been placed in this run."
            )
            return
        yield from self._click_at(point)
        yield Sleep(self.timing.menu_open_ms)
        for index in range(max(1, times)):
            if index:
                yield Sleep(self._jitter(self.timing.action_delay_ms))
            yield from self._apply_binding(binding, label)

    def _do_skip(self, step: SkipWave) -> Program:
        for index in range(max(1, step.times)):
            if index:
                yield Sleep(self._jitter(self.timing.action_delay_ms))
            yield from self._apply_binding(self.deps.keybinds.skip_wave, "skip wave")

    def _step_timeout(self, step_timeout_ms: int) -> int:
        """Resolve a step's timeout: its own if set, else the app default.

        0 at the step level means "use the default" (Settings > Timing), and
        a default of 0 means wait forever -- which is now the shipped default.
        The old fixed 180 s default aborted any WaitForWave more than three
        minutes away while the editor labelled 0 as "wait forever", which in
        TDS is nearly every wave gate past the opening.
        """
        return step_timeout_ms if step_timeout_ms > 0 else self.timing.default_timeout_ms

    def _do_wait_for_wave(self, step: WaitForWave) -> Program:
        target = step.wave
        timeout = self._step_timeout(step.timeout_ms)
        satisfied = yield AwaitState(
            predicate=lambda s: s.wave is not None and s.wave >= target,
            description=f"wave {target}",
            timeout_ms=timeout,
        )
        if not satisfied:
            self._handle_timeout(f"wave {target} was not reached within {timeout / 1000:.0f}s")

    def _do_wait_for_cash(self, step: WaitForCash) -> Program:
        target = step.amount
        timeout = self._step_timeout(step.timeout_ms)
        satisfied = yield AwaitState(
            predicate=lambda s: s.cash is not None and s.cash >= target,
            description=f"${target:,}",
            timeout_ms=timeout,
        )
        if not satisfied:
            self._handle_timeout(f"${target:,} was not reached within {timeout / 1000:.0f}s")

    def _handle_timeout(self, message: str) -> None:
        if self.deps.abort_on_timeout:
            self.fail(
                f"Timed out: {message}. Check HUD calibration -- the app may not be "
                "reading the wave/cash numbers correctly."
            )
        else:
            self.deps.log("warn", f"Timed out: {message}; continuing anyway.")
