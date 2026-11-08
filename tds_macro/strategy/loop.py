"""Loop mode: replay a strategy, get back to the lobby, re-queue, repeat.

Written as an explicit state machine rather than as more generator plumbing,
because the lobby flow genuinely is "wait for this screen, click it, wait for
the next one" and every one of those waits needs its own timeout and its own
message when it expires.  A loop that silently stalls on an unexpected screen
is the worst possible failure here: it would leave the macro clicking into a
menu for hours.

Each click is verified by waiting for the *next* expected screen rather than
assuming it landed, so a missed click retries instead of desynchronising the
whole sequence.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from dataclasses import dataclass, field
from enum import Enum

from ..config import LoopSettings, Timing
from ..input.coords import NormPoint
from ..input.sendinput import InputBackend, MouseButton
from ..vision.calibration import (
    TEMPLATE_END_SCREEN,
    TEMPLATE_MAP_TILE,
    TEMPLATE_PLAY_AGAIN,
    TEMPLATE_PLAY_BUTTON,
)
from ..vision.templates import MatchResult
from .engine import FocusProvider, GameStateProvider, LogFn, RunStatus, StrategyRunner, null_log

log = logging.getLogger(__name__)


class LoopPhase(Enum):
    IDLE = "idle"
    PLAYING = "playing the strategy"
    WAITING_END = "waiting for the match to end"
    REQUEUEING = "returning to the lobby"
    WAITING_MATCH = "waiting for the next match to start"
    COMPLETED = "all runs completed"
    STOPPED = "stopped"
    FAILED = "failed"

    @property
    def is_active(self) -> bool:
        return self in {
            LoopPhase.PLAYING,
            LoopPhase.WAITING_END,
            LoopPhase.REQUEUEING,
            LoopPhase.WAITING_MATCH,
        }


@dataclass
class LobbyStep:
    """One screen to wait for, and optionally click, between matches."""

    template: str
    description: str
    click: bool = True
    timeout_ms: int = 20_000
    # Optional steps are skipped when they never appear.  "Play Again" is
    # optional precisely because TDS does not always offer it -- when it is
    # missing we fall through to picking the map by hand.
    optional: bool = False
    settle_ms: int = 700
    skip_rest_on_match: bool = False


def default_lobby_sequence(settings: LoopSettings) -> list[LobbyStep]:
    """Solo re-queue: try Play Again, otherwise walk the map picker."""
    steps: list[LobbyStep] = []
    if settings.use_play_again:
        steps.append(
            LobbyStep(
                template=TEMPLATE_PLAY_AGAIN,
                description="Play Again",
                timeout_ms=8_000,
                optional=True,
                # If Play Again works we are already queued; the map picker
                # steps would then click into whatever screen follows.
                skip_rest_on_match=True,
            )
        )
    steps.append(
        LobbyStep(template=TEMPLATE_MAP_TILE, description="the map tile", timeout_ms=25_000)
    )
    steps.append(
        LobbyStep(template=TEMPLATE_PLAY_BUTTON, description="the Play button", timeout_ms=15_000)
    )
    return steps


@dataclass
class LoopDeps:
    """Collaborators for :class:`LoopController`."""

    make_runner: Callable[[], StrategyRunner]
    backend: InputBackend
    state: GameStateProvider
    focus: FocusProvider
    find_template: Callable[[str], MatchResult]
    # Called when the match ends, before any re-queue clicking. Wired to the
    # HUD reader's reset: its smoothing keeps the last good wave for a few
    # frames after the readout disappears, and a stale "wave 40" left sitting
    # there makes the next phase believe a new match has already started.
    on_match_ended: Callable[[], None] | None = None
    on_match_started: Callable[[], None] | None = None
    settings: LoopSettings = field(default_factory=LoopSettings)
    timing: Timing = field(default_factory=Timing)
    clock: Callable[[], int] | None = None
    log: LogFn = null_log
    require_foreground: bool = True


class LoopController:
    """Repeats a strategy for N runs, or until stopped."""

    def __init__(self, deps: LoopDeps, sequence: list[LobbyStep] | None = None) -> None:
        self.deps = deps
        self.timing = deps.timing.clamped()
        self.sequence = sequence if sequence is not None else default_lobby_sequence(deps.settings)
        self.phase = LoopPhase.IDLE
        self.detail = ""
        self.runs_completed = 0
        self.runs_failed = 0
        self.failure_reason = ""
        self.runner: StrategyRunner | None = None

        self._step_index = 0
        self._phase_deadline = 0
        self._wake_at = 0
        self._ops: list[tuple[int, Callable[[], bool], str]] = []

    # -- clock ---------------------------------------------------------------
    def now(self) -> int:
        if self.deps.clock is not None:
            return self.deps.clock()
        import time

        return int(time.monotonic() * 1000)

    @property
    def target(self) -> int:
        return max(0, self.deps.settings.runs_target)

    def describe(self) -> str:
        if self.detail:
            suffix = f" (run {self.runs_completed + 1}" + (
                f" of {self.target})" if self.target else ")"
            )
            return self.detail + suffix
        return self.phase.value

    # -- lifecycle -----------------------------------------------------------
    def start(self) -> None:
        self.runs_completed = 0
        self.runs_failed = 0
        self.failure_reason = ""
        self._begin_run()
        if self.phase is LoopPhase.PLAYING:
            goal = f"{self.target} run(s)" if self.target else "until stopped"
            self.deps.log("info", f"Loop mode started: {goal}")

    def stop(self, reason: str = "stopped by user") -> None:
        if self.runner is not None:
            self.runner.stop(reason)
        self._ops.clear()
        self.deps.backend.release_all()
        self.phase = LoopPhase.STOPPED
        self.detail = reason
        self.deps.log("warn", f"Loop stopped after {self.runs_completed} run(s): {reason}")

    def _fail(self, reason: str) -> None:
        self.failure_reason = reason
        self.phase = LoopPhase.FAILED
        self.detail = reason
        self._ops.clear()
        self.deps.backend.release_all()
        self.deps.log("error", reason)

    def _begin_run(self) -> None:
        self.runner = self.deps.make_runner()
        self.runner.start()
        if self.runner.status is RunStatus.FAILED:
            self._fail(self.runner.failure_reason or "strategy could not start")
            return
        self.phase = LoopPhase.PLAYING
        self.detail = ""

    # -- driver ---------------------------------------------------------------
    def tick(self) -> None:
        if not self.phase.is_active:
            return
        if self.deps.require_foreground and not self.deps.focus.is_foreground():
            # The runner handles its own pause; the lobby phases simply wait,
            # so a user who alt-tabs between matches does not get stray clicks.
            if self.phase is LoopPhase.PLAYING and self.runner is not None:
                self.runner.tick()
            else:
                self.detail = "Roblox lost focus -- waiting"
            return

        now = self.now()
        if self._wake_at and now < self._wake_at:
            return
        self._wake_at = 0

        if self._ops:
            self._run_next_op(now)
            return

        if self.phase is LoopPhase.PLAYING:
            self._tick_playing()
        elif self.phase is LoopPhase.WAITING_END:
            self._tick_waiting_end(now)
        elif self.phase is LoopPhase.REQUEUEING:
            self._tick_requeueing(now)
        elif self.phase is LoopPhase.WAITING_MATCH:
            self._tick_waiting_match(now)

    def _run_next_op(self, now: int) -> None:
        delay, action, description = self._ops.pop(0)
        if not action():
            self.deps.log("error", f"Could not {description}: {self.deps.backend.last_error()}")
        if delay > 0:
            self._wake_at = now + delay

    def _queue_click(self, point: NormPoint) -> None:
        """Schedule a move/press/release with the configured dwell times."""
        backend = self.deps.backend
        self._ops.extend(
            [
                (self.timing.move_settle_ms, lambda: backend.move(point), "move the mouse"),
                (
                    self.timing.click_hold_ms,
                    lambda: backend.mouse_down(MouseButton.LEFT),
                    "press the mouse button",
                ),
                (
                    self.timing.action_delay_ms,
                    lambda: backend.mouse_up(MouseButton.LEFT),
                    "release the mouse button",
                ),
            ]
        )

    # -- phases ----------------------------------------------------------------
    def _tick_playing(self) -> None:
        runner = self.runner
        if runner is None:
            self._fail("internal error: no runner")
            return
        runner.tick()
        if runner.status.is_active:
            self.detail = runner.describe_status()
            return
        if runner.status is RunStatus.FAILED:
            self.runs_failed += 1
            self._fail(f"Run stopped: {runner.failure_reason}")
            return
        if runner.status is RunStatus.STOPPED:
            self.phase = LoopPhase.STOPPED
            return
        self.deps.log("info", "Strategy finished; waiting for the match to end.")
        self._enter(LoopPhase.WAITING_END, self.deps.settings.requeue_timeout_ms)

    def _enter(self, phase: LoopPhase, timeout_ms: int) -> None:
        self.phase = phase
        self.detail = phase.value
        self._phase_deadline = self.now() + timeout_ms if timeout_ms > 0 else 0
        self._step_index = 0

    def _expired(self, now: int) -> bool:
        return bool(self._phase_deadline) and now >= self._phase_deadline

    def _tick_waiting_end(self, now: int) -> None:
        result = self.deps.find_template(TEMPLATE_END_SCREEN)
        if result.found:
            self.deps.log("info", "End-of-match screen detected.")
            # Drop the finished match's readings now, so the wait for the next
            # match cannot be satisfied by a stale wave number.
            if self.deps.on_match_ended is not None:
                self.deps.on_match_ended()
            self._enter(LoopPhase.REQUEUEING, 0)
            self._wake_at = now + self.deps.settings.between_match_delay_ms
            return
        if self._expired(now):
            self._fail(
                "The end-of-match screen never appeared. Re-capture it in the calibration "
                "wizard, or raise the re-queue timeout in Settings."
            )
            return
        self.detail = "Waiting for the match to end"
        self._wake_at = now + max(250, self.timing.poll_interval_ms)

    def _tick_requeueing(self, now: int) -> None:
        if self._step_index >= len(self.sequence):
            self._enter(LoopPhase.WAITING_MATCH, self.deps.settings.requeue_timeout_ms)
            return

        step = self.sequence[self._step_index]
        if not self._phase_deadline:
            self._phase_deadline = now + step.timeout_ms

        result = self.deps.find_template(step.template)
        if result.found and result.centre is not None:
            self.deps.log("info", f"Clicking {step.description}.")
            if step.click:
                self._queue_click(result.centre)
            self._step_index += 1
            self._phase_deadline = 0
            self._wake_at = now + step.settle_ms
            if step.skip_rest_on_match:
                # Play Again already re-queued us; the map picker never appears.
                self._enter(LoopPhase.WAITING_MATCH, self.deps.settings.requeue_timeout_ms)
                self._wake_at = now + step.settle_ms
            return

        if self._expired(now):
            self._phase_deadline = 0
            if step.optional:
                self.deps.log("info", f"{step.description} was not offered; continuing.")
                self._step_index += 1
                return
            self._fail(
                f"Could not find {step.description} to click. Re-capture it in the "
                "calibration wizard -- the lobby layout may have changed."
            )
            return
        self.detail = f"Looking for {step.description}"
        self._wake_at = now + max(250, self.timing.poll_interval_ms)

    def _tick_waiting_match(self, now: int) -> None:
        state = self.deps.state.snapshot()
        in_match = state.wave is not None and not state.end_screen
        if in_match:
            self.runs_completed += 1
            self.deps.log(
                "info",
                f"Run {self.runs_completed}"
                + (f" of {self.target}" if self.target else "")
                + " complete; next match has started.",
            )
            if self.deps.on_match_started is not None:
                self.deps.on_match_started()
            if self.target and self.runs_completed >= self.target:
                self.phase = LoopPhase.COMPLETED
                self.detail = f"Finished {self.runs_completed} run(s)"
                self.deps.log("info", self.detail)
                return
            self._begin_run()
            return
        if self._expired(now):
            self._fail(
                "The next match never started. Check that the HUD calibration still reads "
                "the wave number, and that the queue actually began."
            )
            return
        self.detail = "Waiting for the next match to start"
        self._wake_at = now + max(250, self.timing.poll_interval_ms)
