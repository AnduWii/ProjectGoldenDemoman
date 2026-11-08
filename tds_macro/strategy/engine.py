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
from ..input.coords import NormPoint
from ..input.sendinput import InputBackend, MouseButton
from .steps import (
    Ability,
    Place,
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

        if self._await is not None:
            self._poll_await(now)
            return

        self._advance()

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
        else:  # pragma: no cover - every kind is handled above
            self.deps.log("error", f"Unsupported step: {step!r}")

    # -- primitives -----------------------------------------------------------
    def _tap_key(self, key: str) -> Program:
        backend = self.deps.backend
        yield Do(lambda: backend.key_down(key), f"press {key.upper()}")
        yield Sleep(self.timing.key_hold_ms)
        yield Do(lambda: backend.key_up(key), f"release {key.upper()}")

    def _click_at(self, point: NormPoint) -> Program:
        backend = self.deps.backend
        yield Do(lambda: backend.move(point), f"move to ({point.x:.3f}, {point.y:.3f})")
        yield Sleep(self.timing.move_settle_ms)
        yield Do(lambda: backend.mouse_down(MouseButton.LEFT), "press the mouse button")
        yield Sleep(self.timing.click_hold_ms)
        yield Do(lambda: backend.mouse_up(MouseButton.LEFT), "release the mouse button")

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

        attempts = self.timing.place_retries + 1
        for attempt in range(1, attempts + 1):
            before = self._cash()
            yield from self._tap_key(key)
            yield Sleep(self._jitter(self.timing.action_delay_ms))
            yield from self._click_at(step.position)
            yield Sleep(self.timing.place_confirm_ms)

            after = self._cash()
            if before is None or after is None:
                self.deps.log(
                    "warn",
                    f"Placed {step.tower_id} but could not verify it: cash is unreadable. "
                    "Re-run HUD calibration if this keeps happening.",
                )
                self._tower_positions[step.tower_id] = step.position
                return
            if after < before:
                self._tower_positions[step.tower_id] = step.position
                self.deps.log(
                    "info", f"Placed {step.tower_id} (cash ${before:,} -> ${after:,})"
                )
                return
            if attempt < attempts:
                self.placements_retried += 1
                self.deps.log(
                    "warn",
                    f"Placement of {step.tower_id} did not go through "
                    f"(cash unchanged at ${after:,}); retry {attempt}/{attempts - 1}",
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
        self._tower_positions.setdefault(step.tower_id, step.position)

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

    def _do_wait_for_wave(self, step: WaitForWave) -> Program:
        target = step.wave
        timeout = step.timeout_ms or self.timing.default_timeout_ms
        satisfied = yield AwaitState(
            predicate=lambda s: s.wave is not None and s.wave >= target,
            description=f"wave {target}",
            timeout_ms=timeout,
        )
        if not satisfied:
            self._handle_timeout(f"wave {target} was not reached within {timeout / 1000:.0f}s")

    def _do_wait_for_cash(self, step: WaitForCash) -> Program:
        target = step.amount
        timeout = step.timeout_ms or self.timing.default_timeout_ms
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
