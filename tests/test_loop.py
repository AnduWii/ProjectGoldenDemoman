"""Loop mode: end-of-match detection, re-queue, run counting."""

from __future__ import annotations

import random

from conftest import FakeClock, FakeFocus, FakeState, LogCapture

from tds_macro.config import LoopSettings, Timing
from tds_macro.input.coords import NormPoint
from tds_macro.input.sendinput import NullBackend
from tds_macro.strategy.engine import RunnerDeps, StrategyRunner
from tds_macro.strategy.loop import (
    LobbyStep,
    LoopController,
    LoopDeps,
    LoopPhase,
    default_lobby_sequence,
)
from tds_macro.strategy.steps import SkipWave, Strategy
from tds_macro.vision.calibration import (
    TEMPLATE_END_SCREEN,
    TEMPLATE_MAP_TILE,
    TEMPLATE_PLAY_AGAIN,
    TEMPLATE_PLAY_BUTTON,
)
from tds_macro.vision.templates import MatchResult


class FakeWorld:
    """Scripted lobby: which templates are on screen, and the HUD state."""

    def __init__(self) -> None:
        self.visible: set[str] = set()
        self.lookups: list[str] = []

    def find(self, name: str) -> MatchResult:
        self.lookups.append(name)
        if name in self.visible:
            return MatchResult(True, 0.95, NormPoint(0.5, 0.6))
        return MatchResult(False, 0.1)


def build(
    runs_target: int = 2,
    use_play_again: bool = True,
    sequence: list[LobbyStep] | None = None,
) -> tuple[LoopController, FakeWorld, FakeClock, FakeState, NullBackend, LogCapture]:
    clock = FakeClock()
    backend = NullBackend()
    state = FakeState(wave=1, cash=1000, clock=clock)
    focus = FakeFocus()
    logs = LogCapture()
    world = FakeWorld()
    settings = LoopSettings(
        enabled=True,
        runs_target=runs_target,
        use_play_again=use_play_again,
        requeue_timeout_ms=30_000,
        between_match_delay_ms=500,
    )
    timing = Timing(jitter_ms=0)

    def make_runner() -> StrategyRunner:
        return StrategyRunner(
            Strategy(name="loop test", steps=[SkipWave()]),
            RunnerDeps(
                backend=backend,
                state=state,
                focus=focus,
                timing=timing,
                clock=clock,
                log=logs,
                rng=random.Random(7),
            ),
        )

    controller = LoopController(
        LoopDeps(
            make_runner=make_runner,
            backend=backend,
            state=state,
            focus=focus,
            find_template=world.find,
            settings=settings,
            timing=timing,
            clock=clock,
            log=logs,
        ),
        sequence=sequence,
    )
    return controller, world, clock, state, backend, logs


def drive(controller: LoopController, clock: FakeClock, ms: int, tick_ms: int = 50) -> None:
    for _ in range(ms // tick_ms):
        if not controller.phase.is_active:
            return
        controller.tick()
        clock.advance(tick_ms)


def test_full_loop_completes_the_requested_number_of_runs() -> None:
    controller, world, clock, state, _, logs = build(runs_target=2)
    controller.start()

    for _ in range(2):
        drive(controller, clock, 3_000)
        assert controller.phase is LoopPhase.WAITING_END, controller.describe()

        # Match ends.
        world.visible = {TEMPLATE_END_SCREEN}
        state.wave = None
        drive(controller, clock, 3_000)
        assert controller.phase is LoopPhase.REQUEUEING

        # Play Again appears and is clicked.
        world.visible = {TEMPLATE_PLAY_AGAIN}
        drive(controller, clock, 3_000)
        assert controller.phase is LoopPhase.WAITING_MATCH

        # New match begins.
        world.visible = set()
        state.wave = 1
        drive(controller, clock, 3_000)

    assert controller.runs_completed == 2
    assert controller.phase is LoopPhase.COMPLETED
    assert "Finished 2 run(s)" in controller.detail


def test_falls_back_to_the_map_picker_when_play_again_is_not_offered() -> None:
    controller, world, clock, state, _, logs = build(runs_target=1)
    controller.start()
    drive(controller, clock, 3_000)

    world.visible = {TEMPLATE_END_SCREEN}
    state.wave = None
    drive(controller, clock, 3_000)
    assert controller.phase is LoopPhase.REQUEUEING

    # Play Again never shows; it is optional, so after its timeout we move on.
    world.visible = set()
    drive(controller, clock, 10_000)
    assert "not offered" in logs.text()

    world.visible = {TEMPLATE_MAP_TILE}
    drive(controller, clock, 3_000)
    world.visible = {TEMPLATE_PLAY_BUTTON}
    drive(controller, clock, 3_000)
    assert controller.phase is LoopPhase.WAITING_MATCH

    state.wave = 1
    drive(controller, clock, 3_000)
    assert controller.runs_completed == 1
    assert controller.phase is LoopPhase.COMPLETED


def test_missing_end_screen_template_fails_with_an_actionable_message() -> None:
    controller, world, clock, state, _, _ = build(runs_target=1)
    controller.start()
    drive(controller, clock, 3_000)
    assert controller.phase is LoopPhase.WAITING_END

    drive(controller, clock, 60_000)  # end screen never appears
    assert controller.phase is LoopPhase.FAILED
    assert "calibration wizard" in controller.failure_reason


def test_a_required_lobby_button_that_never_appears_fails_loudly() -> None:
    controller, world, clock, state, _, _ = build(runs_target=1, use_play_again=False)
    controller.start()
    drive(controller, clock, 3_000)
    world.visible = {TEMPLATE_END_SCREEN}
    state.wave = None
    drive(controller, clock, 3_000)

    world.visible = set()
    drive(controller, clock, 60_000)
    assert controller.phase is LoopPhase.FAILED
    assert "map tile" in controller.failure_reason


def test_next_match_never_starting_fails_instead_of_hanging() -> None:
    controller, world, clock, state, _, _ = build(runs_target=2)
    controller.start()
    drive(controller, clock, 3_000)
    world.visible = {TEMPLATE_END_SCREEN}
    state.wave = None
    drive(controller, clock, 3_000)
    world.visible = {TEMPLATE_PLAY_AGAIN}
    drive(controller, clock, 3_000)
    assert controller.phase is LoopPhase.WAITING_MATCH

    state.wave = None  # HUD never reports a wave again
    drive(controller, clock, 60_000)
    assert controller.phase is LoopPhase.FAILED
    assert "never started" in controller.failure_reason


def test_runs_forever_when_no_target_is_set() -> None:
    controller, world, clock, state, _, _ = build(runs_target=0)
    controller.start()
    for _ in range(3):
        drive(controller, clock, 3_000)
        world.visible = {TEMPLATE_END_SCREEN}
        state.wave = None
        drive(controller, clock, 3_000)
        world.visible = {TEMPLATE_PLAY_AGAIN}
        drive(controller, clock, 3_000)
        world.visible = set()
        state.wave = 1
        drive(controller, clock, 3_000)
    assert controller.runs_completed == 3
    # No target means it never reports COMPLETED; it is still cycling.
    assert controller.phase.is_active
    assert controller.phase is not LoopPhase.COMPLETED


def test_stopping_mid_loop_releases_input_and_halts() -> None:
    controller, world, clock, state, backend, _ = build(runs_target=5)
    controller.start()
    drive(controller, clock, 1_000)
    backend.key_down("shift")

    controller.stop("emergency stop")
    assert controller.phase is LoopPhase.STOPPED
    assert backend.held_keys == set()

    before = len(backend.intents)
    drive(controller, clock, 10_000)
    assert len(backend.intents) == before


def test_losing_focus_between_matches_does_not_produce_stray_clicks() -> None:
    controller, world, clock, state, backend, _ = build(runs_target=1)
    controller.start()
    drive(controller, clock, 3_000)
    world.visible = {TEMPLATE_END_SCREEN}
    state.wave = None
    drive(controller, clock, 2_000)

    controller.deps.focus.foreground = False
    world.visible = {TEMPLATE_PLAY_AGAIN}
    before = len(backend.intents)
    drive(controller, clock, 10_000)
    assert len(backend.intents) == before
    assert "lost focus" in controller.detail

    controller.deps.focus.foreground = True
    drive(controller, clock, 3_000)
    assert controller.phase is LoopPhase.WAITING_MATCH


def test_a_failed_strategy_stops_the_loop_rather_than_queueing_again() -> None:
    controller, world, clock, state, _, _ = build(runs_target=3)
    controller.start()
    assert controller.runner is not None
    controller.runner.fail("HUD timeout")
    drive(controller, clock, 1_000)
    assert controller.phase is LoopPhase.FAILED
    assert controller.runs_failed == 1


def test_default_sequence_order_is_play_again_then_map_then_play() -> None:
    steps = default_lobby_sequence(LoopSettings(use_play_again=True))
    assert [s.template for s in steps] == [
        TEMPLATE_PLAY_AGAIN,
        TEMPLATE_MAP_TILE,
        TEMPLATE_PLAY_BUTTON,
    ]
    assert steps[0].optional is True
    assert default_lobby_sequence(LoopSettings(use_play_again=False))[0].template == (
        TEMPLATE_MAP_TILE
    )


def test_progress_description_includes_the_run_counter() -> None:
    controller, world, clock, state, _, _ = build(runs_target=4)
    controller.start()
    drive(controller, clock, 500)
    assert "run 1 of 4" in controller.describe()


def test_stale_wave_from_the_finished_match_cannot_satisfy_the_next_one() -> None:
    """Without the end-of-match reset, the smoothed wave number from the match
    that just ended made WAITING_MATCH believe the next one had already begun,
    so the loop counted a run and started replaying into the lobby."""
    controller, world, clock, state, _, _ = build(runs_target=2)
    resets: list[int] = []

    def forget_readings() -> None:
        resets.append(clock.value)
        state.wave = None

    controller.deps.on_match_ended = forget_readings
    controller.start()
    drive(controller, clock, 3_000)

    # The match ends but the HUD still reports the final wave, as smoothing does.
    state.wave = 40
    world.visible = {TEMPLATE_END_SCREEN}
    drive(controller, clock, 3_000)
    assert resets, "the end of the match must clear the previous readings"
    assert controller.phase is LoopPhase.REQUEUEING

    world.visible = {TEMPLATE_PLAY_AGAIN}
    drive(controller, clock, 3_000)
    assert controller.phase is LoopPhase.WAITING_MATCH
    assert controller.runs_completed == 0, "no run may be counted until a match starts"

    world.visible = set()
    state.wave = 1
    drive(controller, clock, 3_000)
    assert controller.runs_completed == 1
