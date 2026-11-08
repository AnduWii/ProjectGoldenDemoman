"""Strategy engine behaviour, driven by a fake clock and a dry-run backend."""

from __future__ import annotations

import random

import pytest
from conftest import FakeClock, FakeFocus, FakeState, LogCapture, pump

from tds_macro.config import ActionBinding, Keybinds, Timing
from tds_macro.input.coords import NormPoint
from tds_macro.input.sendinput import MouseButton, NullBackend
from tds_macro.strategy.engine import RunnerDeps, RunStatus, StrategyRunner
from tds_macro.strategy.steps import (
    Ability,
    Place,
    Sell,
    SkipWave,
    Strategy,
    Upgrade,
    Wait,
    WaitForCash,
    WaitForWave,
)


def build(
    steps: list,
    *,
    state: FakeState | None = None,
    focus: FakeFocus | None = None,
    timing: Timing | None = None,
    keybinds: Keybinds | None = None,
    abort_on_timeout: bool = True,
) -> tuple[StrategyRunner, NullBackend, FakeClock, FakeState, LogCapture, FakeFocus]:
    clock = FakeClock()
    backend = NullBackend()
    game = state or FakeState(clock=clock)
    game.clock = clock
    watcher = focus or FakeFocus()
    logs = LogCapture()
    deps = RunnerDeps(
        backend=backend,
        state=game,
        focus=watcher,
        keybinds=keybinds or Keybinds(),
        # Jitter off by default so assertions are exact; a dedicated test covers it.
        timing=timing or Timing(jitter_ms=0),
        clock=clock,
        log=logs,
        rng=random.Random(1234),
        abort_on_timeout=abort_on_timeout,
    )
    runner = StrategyRunner(Strategy(name="test", steps=steps), deps)
    return runner, backend, clock, game, logs, watcher


# --- basics -------------------------------------------------------------------
def test_wait_step_consumes_exactly_its_duration() -> None:
    runner, _, clock, _, _, _ = build([Wait(ms=2500)])
    runner.start()
    elapsed = pump(runner, clock)
    assert runner.status is RunStatus.COMPLETED
    # The trailing inter-step delay is included; the wait itself must be honoured.
    assert 2500 <= elapsed <= 2500 + Timing().action_delay_ms + 10


def test_place_presses_slot_key_then_clicks_position() -> None:
    state = FakeState(cash=1000)
    # Cash drops once, which is how a successful placement is recognised.
    runner, backend, clock, game, _, _ = build(
        [Place(slot=3, position=NormPoint(0.25, 0.75), tower_id="T1")], state=state
    )

    placed = {"done": False}

    def spend(intent) -> None:
        if intent.kind == "mouse_up" and not placed["done"]:
            placed["done"] = True
            game.cash = 700

    backend.on_intent = spend
    runner.start()
    pump(runner, clock)

    assert runner.status is RunStatus.COMPLETED
    assert backend.trace() == [
        "key down 3",
        "key up 3",
        "move to (0.2500, 0.7500)",
        "mouse down left",
        "mouse up left",
    ]


def test_disabled_steps_are_skipped() -> None:
    runner, backend, clock, _, _, _ = build(
        [Wait(ms=10, enabled=False), SkipWave(times=1)]
    )
    runner.start()
    pump(runner, clock)
    assert runner.total_steps == 1
    assert backend.trace() == ["key down g", "key up g"]


# --- gating -------------------------------------------------------------------
def test_wait_for_wave_blocks_until_hud_reports_the_wave() -> None:
    state = FakeState(wave=9)
    runner, _, clock, game, logs, _ = build([WaitForWave(wave=12), SkipWave()], state=state)
    runner.start()
    for _ in range(5):
        runner.tick()
        clock.advance(200)
    assert runner.status is RunStatus.WAITING
    assert "Waiting for wave 12 - detected wave 9" in runner.describe_status()

    game.wave = 12
    pump(runner, clock)
    assert runner.status is RunStatus.COMPLETED


def test_wait_for_cash_ignores_unreadable_values() -> None:
    """An unreadable HUD must not satisfy a cash gate -- None is not zero."""
    state = FakeState(cash=None)
    runner, _, clock, game, _, _ = build([WaitForCash(amount=0)], state=state)
    runner.start()
    for _ in range(4):
        runner.tick()
        clock.advance(200)
    assert runner.status is RunStatus.WAITING
    game.cash = 0
    pump(runner, clock)
    assert runner.status is RunStatus.COMPLETED


def test_wait_for_wave_times_out_and_aborts_with_a_readable_reason() -> None:
    state = FakeState(wave=3)
    runner, _, clock, _, logs, _ = build(
        [WaitForWave(wave=40, timeout_ms=5_000), SkipWave()], state=state
    )
    runner.start()
    pump(runner, clock)
    assert runner.status is RunStatus.FAILED
    assert "Timed out" in runner.failure_reason
    assert "calibration" in runner.failure_reason
    assert "key down g" not in NullBackend().trace()


def test_timeout_can_continue_instead_of_aborting() -> None:
    state = FakeState(wave=3)
    runner, backend, clock, _, logs, _ = build(
        [WaitForWave(wave=40, timeout_ms=1_000), SkipWave()],
        state=state,
        abort_on_timeout=False,
    )
    runner.start()
    pump(runner, clock)
    assert runner.status is RunStatus.COMPLETED
    assert "key down g" in backend.trace()
    assert any("continuing anyway" in m for m in logs.messages("warn"))


# --- failed placement retry ----------------------------------------------------
def test_placement_retries_when_cash_does_not_drop() -> None:
    state = FakeState(cash=500)
    runner, backend, clock, game, logs, _ = build(
        [Place(slot=1, position=NormPoint(0.5, 0.5), tower_id="T1")],
        state=state,
        timing=Timing(jitter_ms=0, place_retries=2),
    )
    clicks = {"n": 0}

    def maybe_spend(intent) -> None:
        if intent.kind == "mouse_up":
            clicks["n"] += 1
            if clicks["n"] == 3:  # succeed only on the final attempt
                game.cash = 300

    backend.on_intent = maybe_spend
    runner.start()
    pump(runner, clock)

    assert clicks["n"] == 3
    assert runner.placements_retried == 2
    assert runner.placements_failed == 0
    assert runner.status is RunStatus.COMPLETED


def test_placement_gives_up_after_configured_retries() -> None:
    runner, _, clock, _, logs, _ = build(
        [Place(slot=1, position=NormPoint(0.5, 0.5), tower_id="T1")],
        state=FakeState(cash=500),
        timing=Timing(jitter_ms=0, place_retries=1),
    )
    runner.start()
    pump(runner, clock)
    assert runner.placements_failed == 1
    assert runner.status is RunStatus.COMPLETED  # a failed place is not a failed run
    assert any("Gave up placing T1" in m for m in logs.messages("error"))


def test_placement_with_unreadable_cash_warns_rather_than_retrying() -> None:
    runner, _, clock, _, logs, _ = build(
        [Place(slot=1, position=NormPoint(0.5, 0.5), tower_id="T1")],
        state=FakeState(cash=None),
    )
    runner.start()
    pump(runner, clock)
    assert runner.placements_retried == 0
    assert any("could not verify" in m for m in logs.messages("warn"))


# --- tower references ----------------------------------------------------------
def test_upgrade_clicks_the_recorded_placement_then_applies_the_binding() -> None:
    state = FakeState(cash=1000)
    runner, backend, clock, game, _, _ = build(
        [
            Place(slot=2, position=NormPoint(0.4, 0.8), tower_id="T1"),
            Upgrade(tower_id="T1", times=2),
        ],
        state=state,
    )
    backend.on_intent = lambda i: setattr(game, "cash", 400) if i.kind == "mouse_up" else None
    runner.start()
    pump(runner, clock)
    trace = backend.trace()
    assert trace.count("move to (0.4000, 0.8000)") == 2  # place, then select to upgrade
    assert trace.count("key down e") == 2  # upgraded twice
    assert runner.status is RunStatus.COMPLETED


def test_click_mode_binding_clicks_a_menu_button_instead_of_pressing_a_key() -> None:
    binds = Keybinds(sell=ActionBinding("click", point=NormPoint(0.9, 0.1)))
    state = FakeState(cash=1000)
    runner, backend, clock, game, _, _ = build(
        [Place(slot=1, position=NormPoint(0.2, 0.2), tower_id="T1"), Sell(tower_id="T1")],
        state=state,
        keybinds=binds,
    )
    backend.on_intent = lambda i: setattr(game, "cash", 500) if i.kind == "mouse_up" else None
    runner.start()
    pump(runner, clock)
    assert "move to (0.9000, 0.1000)" in backend.trace()


def test_acting_on_an_unplaced_tower_logs_instead_of_crashing() -> None:
    runner, backend, clock, _, logs, _ = build([Ability(tower_id="T4")])
    # Bypass validation to simulate a mid-run reference problem.
    runner.strategy.validate = lambda: []  # type: ignore[method-assign]
    runner.start()
    pump(runner, clock)
    assert backend.intents == []
    assert any("has not been placed" in m for m in logs.messages("error"))


def test_validation_blocks_a_broken_strategy_before_any_input() -> None:
    runner, backend, clock, _, logs, _ = build([Upgrade(tower_id="T7")])
    runner.start()
    assert runner.status is RunStatus.FAILED
    assert backend.intents == []
    assert "never placed" in runner.failure_reason


# --- focus, pause and stop -----------------------------------------------------
def test_losing_focus_pauses_playback_and_releases_held_keys() -> None:
    watcher = FakeFocus()
    runner, backend, clock, _, logs, _ = build([Wait(ms=5_000), SkipWave()], focus=watcher)
    runner.start()
    runner.tick()

    watcher.foreground = False
    runner.tick()
    assert runner.status is RunStatus.PAUSED_FOCUS
    assert "lost focus" in runner.describe_status()

    before = len(backend.intents)
    for _ in range(50):
        clock.advance(1_000)
        runner.tick()
    assert len(backend.intents) == before  # nothing sent while unfocused

    watcher.foreground = True
    pump(runner, clock)
    assert runner.status is RunStatus.COMPLETED
    assert "key down g" in backend.trace()


def test_pause_shifts_deadlines_so_a_wait_does_not_expire_while_paused() -> None:
    watcher = FakeFocus()
    runner, _, clock, _, _, _ = build([Wait(ms=3_000)], focus=watcher)
    runner.start()
    runner.tick()
    clock.advance(500)

    watcher.foreground = False
    runner.tick()
    clock.advance(60_000)  # a whole minute in another window
    watcher.foreground = True
    runner.tick()
    assert runner.status is RunStatus.RUNNING

    clock.advance(2_400)
    runner.tick()
    assert runner.status is RunStatus.RUNNING  # 100 ms of the wait still owed
    clock.advance(200)
    pump(runner, clock)
    assert runner.status is RunStatus.COMPLETED


def test_emergency_stop_releases_everything_and_halts() -> None:
    runner, backend, clock, _, logs, _ = build([Wait(ms=60_000), SkipWave()])
    runner.start()
    runner.tick()
    backend.key_down("shift")  # simulate a key held when the user panics

    runner.stop("emergency stop")
    assert runner.status is RunStatus.STOPPED
    assert backend.held_keys == set()
    assert backend.held_buttons == set()
    assert backend.trace()[-1] == "key up shift"

    before = len(backend.intents)
    clock.advance(120_000)
    runner.tick()
    assert len(backend.intents) == before  # stopped stays stopped


def test_stop_is_idempotent() -> None:
    runner, _, _, _, _, _ = build([Wait(ms=1_000)])
    runner.start()
    runner.stop()
    runner.stop()
    assert runner.status is RunStatus.STOPPED


# --- dry run and jitter ---------------------------------------------------------
def test_dry_run_sends_nothing_but_records_the_full_trace() -> None:
    state = FakeState(cash=1000)
    runner, backend, clock, _, logs, _ = build(
        [Place(slot=1, position=NormPoint(0.1, 0.2), tower_id="T1")], state=state
    )
    runner.start()
    pump(runner, clock)
    assert backend.dry_run is True
    assert "DRY RUN" in logs.text()
    assert backend.trace()[0] == "key down 1"


def test_jitter_varies_delays_without_changing_the_sequence() -> None:
    sequences = []
    totals = []
    for seed in (1, 2, 3):
        clock = FakeClock()
        backend = NullBackend()
        deps = RunnerDeps(
            backend=backend,
            state=FakeState(clock=clock),
            focus=FakeFocus(),
            timing=Timing(jitter_ms=40),
            clock=clock,
            log=LogCapture(),
            rng=random.Random(seed),
        )
        runner = StrategyRunner(Strategy(steps=[SkipWave(times=4)]), deps)
        runner.start()
        totals.append(pump(runner, clock))
        sequences.append(backend.trace())
    assert len({tuple(s) for s in sequences}) == 1  # identical actions
    assert len(set(totals)) > 1  # different timing


@pytest.mark.parametrize("times", [1, 3])
def test_skip_wave_repeats_the_configured_number_of_times(times: int) -> None:
    runner, backend, clock, _, _, _ = build([SkipWave(times=times)])
    runner.start()
    pump(runner, clock)
    assert backend.trace().count("key down g") == times


def test_missing_keybind_reports_where_to_fix_it() -> None:
    binds = Keybinds(skip_wave=ActionBinding("key", ""))
    runner, backend, clock, _, logs, _ = build([SkipWave()], keybinds=binds)
    runner.start()
    pump(runner, clock)
    assert backend.intents == []
    assert any("Settings > Keybinds" in m for m in logs.messages("error"))


# --- panic release --------------------------------------------------------------
def test_release_keys_sends_key_up_for_untracked_keys() -> None:
    """After a crash the held-key set can be lost while the key is still down,
    so the panic path sends key-up regardless of what it thinks is held."""
    backend = NullBackend()
    backend.key_down("3")
    backend.held_keys.clear()  # bookkeeping lost
    backend.release_keys(["3", "e"], [MouseButton.LEFT, MouseButton.RIGHT])
    trace = backend.trace()
    assert trace.count("key up 3") == 1
    assert "key up e" in trace
    assert "mouse up left" in trace and "mouse up right" in trace
    assert backend.held_keys == set() and backend.held_buttons == set()


def test_a_late_cash_reading_still_counts_as_a_successful_placement() -> None:
    """The HUD publishes a value only once a majority of recent frames agree, so
    the drop can land a few frames after the confirm delay. Treating that lag as
    a failure would retry and place a second tower."""
    state = FakeState(cash=1000)
    runner, backend, clock, game, logs, _ = build(
        [Place(slot=1, position=NormPoint(0.5, 0.5), tower_id="T1")],
        state=state,
        timing=Timing(jitter_ms=0, place_confirm_ms=100, place_verify_ms=2_000),
    )
    clicked_at: dict[str, int] = {}

    def on_intent(intent) -> None:
        if intent.kind == "mouse_up":
            clicked_at["t"] = clock.value

    def maybe_update(fake: FakeState) -> None:
        # Cash only reflects the purchase a second after the click.
        if "t" in clicked_at and clock.value - clicked_at["t"] >= 1_000:
            fake.cash = 700

    backend.on_intent = on_intent
    game.on_snapshot = maybe_update
    runner.start()
    pump(runner, clock)

    assert runner.placements_retried == 0, logs.text()
    assert runner.placements_failed == 0
    assert backend.trace().count("mouse up left") == 1, "the tower was placed twice"
    assert runner.status is RunStatus.COMPLETED


def test_placement_still_gives_up_when_the_drop_never_arrives() -> None:
    runner, backend, clock, _, logs, _ = build(
        [Place(slot=1, position=NormPoint(0.5, 0.5), tower_id="T1")],
        state=FakeState(cash=500),
        timing=Timing(jitter_ms=0, place_confirm_ms=100, place_verify_ms=500, place_retries=1),
    )
    runner.start()
    pump(runner, clock)
    assert runner.placements_retried == 1
    assert runner.placements_failed == 1
    assert backend.trace().count("mouse up left") == 2
