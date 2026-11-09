"""TinyTask-style recording and replay: everything the user does, kept.

The recorder keeps plain clicks, held keys, right-button camera drags and
scroll-wheel zoom as steps of their own, while still turning hotbar presses
into placements and upgrade keys into upgrades. The engine replays them, and
moves tower spots when the game window is a different shape from the one the
recording was made on.
"""

from __future__ import annotations

import random

import pytest
from conftest import FakeClock, FakeFocus, FakeState, LogCapture, pump

from tds_macro.config import Keybinds, Timing
from tds_macro.input.coords import NormPoint, remap_world_point
from tds_macro.input.events import RawEvent, RawKind
from tds_macro.input.sendinput import NullBackend
from tds_macro.strategy.engine import RunnerDeps, RunStatus, StrategyRunner
from tds_macro.strategy.recorder import Recorder, RecorderOptions
from tds_macro.strategy.steps import (
    SCHEMA_VERSION,
    Click,
    Drag,
    DragPoint,
    KeyPress,
    Place,
    Scroll,
    Strategy,
    StrategyError,
    Upgrade,
    Wait,
    WaitForWave,
)


# --- recording ------------------------------------------------------------------
def ev(t: int, kind: RawKind, **kwargs) -> RawEvent:
    return RawEvent(t_ms=t, kind=kind, **kwargs)


def down(t: int, name: str) -> RawEvent:
    return ev(t, RawKind.KEY_DOWN, key=name)


def up(t: int, name: str) -> RawEvent:
    return ev(t, RawKind.KEY_UP, key=name)


def press(t: int, x: float, y: float, button: str = "left") -> RawEvent:
    return ev(t, RawKind.MOUSE_DOWN, button=button, pos=NormPoint(x, y), in_client=True)


def release(t: int, x: float, y: float, button: str = "left") -> RawEvent:
    return ev(t, RawKind.MOUSE_UP, button=button, pos=NormPoint(x, y), in_client=True)


def move(t: int, x: float, y: float) -> RawEvent:
    return ev(t, RawKind.MOUSE_MOVE, pos=NormPoint(x, y), in_client=True)


def wheel(t: int, x: float, y: float, delta: int) -> RawEvent:
    return ev(t, RawKind.SCROLL, pos=NormPoint(x, y), in_client=True, delta=delta)


def keep(events: list[RawEvent], **options) -> Recorder:
    options.setdefault("keep_everything", True)
    extra = _recorder_kwargs(options)  # pops the Recorder's own arguments first
    recorder = Recorder(Keybinds(), RecorderOptions(**options), **extra)
    recorder.feed_all(events)
    return recorder


def _recorder_kwargs(options: dict) -> dict:
    extra = {}
    for name in ("wave_provider", "excluded_keys", "aspect_ratio"):
        if name in options:
            extra[name] = options.pop(name)
    return extra


def kinds(steps: list) -> list[str]:
    return [type(s).__name__ for s in steps]


def test_a_click_on_nothing_is_kept_as_a_click() -> None:
    rec = keep([press(0, 0.9, 0.1), release(40, 0.9, 0.1)], insert_waits=False)
    steps = rec.finish().steps
    assert kinds(steps) == ["Click"]
    assert steps[0].position == NormPoint(0.9, 0.1) and steps[0].button == "left"


def test_without_keep_mode_plain_clicks_are_still_dropped() -> None:
    rec = keep([press(0, 0.9, 0.1)], keep_everything=False)
    assert rec.finish().steps == []


def test_a_held_key_is_replayed_for_as_long_as_it_was_held() -> None:
    repeats = [down(100 + 33 * i, "w") for i in range(1, 30)]
    rec = keep([down(100, "w"), *repeats, up(1_300, "w")], insert_waits=False)
    steps = rec.finish().steps
    assert kinds(steps) == ["KeyPress"]
    assert steps[0].key == "w" and steps[0].hold_ms == 1_200


def test_a_right_button_drag_keeps_its_path_and_timing() -> None:
    rec = keep(
        [
            press(0, 0.50, 0.50, "right"),
            move(50, 0.55, 0.50),
            move(100, 0.60, 0.52),
            move(150, 0.65, 0.55),
            release(200, 0.65, 0.55, "right"),
        ],
        insert_waits=False,
    )
    steps = rec.finish().steps
    assert kinds(steps) == ["Drag"]
    drag = steps[0]
    assert drag.button == "right"
    assert drag.path[0].position == NormPoint(0.50, 0.50)
    assert drag.path[-1].position == NormPoint(0.65, 0.55)
    assert drag.duration_ms == 200


def test_a_right_click_that_barely_moves_is_a_click_not_a_drag() -> None:
    rec = keep(
        [press(0, 0.5, 0.5, "right"), move(30, 0.501, 0.5), release(60, 0.501, 0.5, "right")],
        insert_waits=False,
    )
    steps = rec.finish().steps
    assert kinds(steps) == ["Click"] and steps[0].button == "right"


def test_wheel_notches_in_one_burst_merge_into_one_scroll() -> None:
    rec = keep(
        [wheel(0, 0.5, 0.5, 1), wheel(80, 0.5, 0.5, 1), wheel(160, 0.5, 0.5, 1),
         wheel(2_000, 0.5, 0.5, -1)],
        insert_waits=False,
    )
    steps = rec.finish().steps
    assert kinds(steps) == ["Scroll", "Scroll"]
    assert [s.clicks for s in steps] == [3, -1]


def test_shortcuts_and_the_apps_own_hotkeys_are_never_recorded() -> None:
    rec = keep(
        [
            down(0, "f6"), up(30, "f6"),             # the record hotkey itself
            down(100, "ctrl"), down(120, "p"), up(150, "p"), up(160, "ctrl"),
            down(300, "cmd"), down(320, "q"), up(340, "q"), up(350, "cmd"),
            down(500, "t"), up(530, "t"),           # an ordinary key: kept
        ],
        insert_waits=False,
        excluded_keys={"f6"},
    )
    steps = rec.finish().steps
    assert kinds(steps) == ["KeyPress"] and steps[0].key == "t"
    assert rec.stats.skipped_shortcuts == 3


def test_selecting_a_tower_then_upgrading_does_not_leave_a_stray_click() -> None:
    rec = keep(
        [
            down(0, "1"), up(20, "1"), press(100, 0.3, 0.3),   # place T1
            press(1_000, 0.31, 0.31),                          # select it
            down(1_100, "e"), up(1_130, "e"),                  # upgrade it
        ],
        insert_waits=False,
    )
    assert kinds(rec.finish().steps) == ["Place", "Upgrade"]


def test_selecting_a_tower_and_doing_nothing_is_kept_as_a_click() -> None:
    rec = keep(
        [down(0, "1"), up(20, "1"), press(100, 0.3, 0.3), press(1_000, 0.31, 0.31)],
        insert_waits=False,
    )
    steps = rec.finish().steps
    assert kinds(steps) == ["Place", "Click"]


def test_a_new_wave_replaces_the_fixed_wait_with_a_wave_gate() -> None:
    wave = {"now": 1}
    events = [down(0, "1"), up(20, "1"), press(100, 0.3, 0.3)]
    rec = Recorder(
        Keybinds(),
        RecorderOptions(keep_everything=True, insert_wave_gates=True),
        wave_provider=lambda: wave["now"],
    )
    rec.feed_all(events)
    wave["now"] = 2
    rec.feed_all([press(40_000, 0.8, 0.2), release(40_050, 0.8, 0.2)])
    steps = rec.finish().steps
    assert kinds(steps) == ["Place", "WaitForWave", "Click"]
    assert isinstance(steps[1], WaitForWave) and steps[1].wave == 2


def test_gaps_between_actions_become_waits() -> None:
    rec = keep([press(0, 0.9, 0.1), press(2_000, 0.8, 0.1)])
    steps = rec.finish().steps
    assert kinds(steps) == ["Click", "Wait", "Click"]
    assert isinstance(steps[1], Wait) and abs(steps[1].ms - 2_000) <= 50


def test_the_recording_remembers_the_shape_of_the_window() -> None:
    rec = keep([press(0, 0.9, 0.1)], aspect_ratio=1.6)
    assert rec.finish().aspect_ratio == 1.6


# --- replay ---------------------------------------------------------------------
def runner_for(
    steps: list, *, aspect: float = 0.0, target: float | None = None, verify: bool = True,
    state: FakeState | None = None,
) -> tuple[StrategyRunner, NullBackend, FakeClock]:
    clock = FakeClock()
    backend = NullBackend()
    game = state or FakeState(clock=clock)
    game.clock = clock
    deps = RunnerDeps(
        backend=backend,
        state=game,
        focus=FakeFocus(),
        keybinds=Keybinds(),
        timing=Timing(jitter_ms=0),
        clock=clock,
        log=LogCapture(),
        rng=random.Random(7),
        verify_placements=verify,
        target_aspect=(lambda: target) if target is not None else None,
    )
    strategy = Strategy(name="t", steps=steps, aspect_ratio=aspect)
    return StrategyRunner(strategy, deps), backend, clock


def run(steps: list, **kwargs) -> tuple[list[str], FakeClock, StrategyRunner]:
    runner, backend, clock = runner_for(steps, **kwargs)
    runner.start()
    pump(runner, clock)
    assert runner.status is RunStatus.COMPLETED, runner.status
    return backend.trace(), clock, runner


def test_replaying_a_click_moves_then_clicks_that_button() -> None:
    trace, _, _ = run([Click(position=NormPoint(0.2, 0.4), button="right")])
    assert trace == ["move to (0.2000, 0.4000)", "mouse down right", "mouse up right"]


def test_replaying_a_held_key_holds_it_for_the_recorded_time() -> None:
    runner, backend, clock = runner_for([KeyPress(key="w", hold_ms=1_200)])
    downs: list[int] = []
    ups: list[int] = []
    backend.on_intent = lambda intent: (
        downs.append(clock.value) if intent.kind == "key_down" else
        ups.append(clock.value) if intent.kind == "key_up" else None
    )
    runner.start()
    pump(runner, clock)
    assert backend.trace() == ["key down w", "key up w"]
    assert ups[0] - downs[0] >= 1_200


def test_replaying_a_drag_holds_the_button_along_the_whole_path() -> None:
    path = [
        DragPoint(0, NormPoint(0.5, 0.5)),
        DragPoint(100, NormPoint(0.6, 0.5)),
        DragPoint(200, NormPoint(0.7, 0.5)),
    ]
    trace, _, _ = run([Drag(button="right", path=path)])
    assert trace == [
        "move to (0.5000, 0.5000)",
        "mouse down right",
        "move to (0.6000, 0.5000)",
        "move to (0.7000, 0.5000)",
        "mouse up right",
    ]


def test_replaying_a_scroll_turns_the_wheel_where_it_was_turned() -> None:
    trace, _, _ = run([Scroll(position=NormPoint(0.4, 0.6), clicks=-3)])
    assert trace == ["move to (0.4000, 0.6000)", "scroll -3"]


def test_without_cash_reading_a_placement_is_sent_once_without_checking() -> None:
    state = FakeState(cash=None)
    trace, _, runner = run(
        [Place(slot=2, position=NormPoint(0.3, 0.3), tower_id="T1"),
         Upgrade(tower_id="T1")],
        verify=False, state=state,
    )
    assert trace.count("key down 2") == 1
    assert "key down e" in trace


def test_a_16_9_recording_puts_towers_in_the_same_map_spot_on_a_16_10_screen() -> None:
    trace, _, _ = run(
        [Place(slot=1, position=NormPoint(0.2, 0.5), tower_id="T1")],
        aspect=16 / 9, target=16 / 10, verify=False,
    )
    expected = remap_world_point(NormPoint(0.2, 0.5), 16 / 9, 16 / 10)
    assert f"move to ({expected.x:.4f}, 0.5000)" in trace
    assert expected.x == pytest.approx(0.5 - 0.3 * (16 / 9) / (16 / 10))


def test_a_recording_of_unknown_shape_is_replayed_as_is() -> None:
    trace, _, _ = run(
        [Place(slot=1, position=NormPoint(0.2, 0.5), tower_id="T1")],
        aspect=0.0, target=16 / 10, verify=False,
    )
    assert "move to (0.2000, 0.5000)" in trace


@pytest.mark.parametrize(
    ("x", "source", "target", "expected"),
    [
        (0.5, 16 / 9, 16 / 10, 0.5),              # the centre never moves
        (0.2, 16 / 9, 16 / 9, 0.2),               # same shape: unchanged
        (0.2, 0.0, 16 / 10, 0.2),                 # unknown: unchanged
        (0.0, 16 / 10, 16 / 9, 0.5 - 0.5 * 1.6 / (16 / 9)),  # narrower source
    ],
)
def test_remap_world_point(x: float, source: float, target: float, expected: float) -> None:
    point = remap_world_point(NormPoint(x, 0.7), source, target)
    assert point.x == pytest.approx(expected) and point.y == 0.7


def test_null_backend_records_scrolls() -> None:
    backend = NullBackend()
    backend.scroll(2)
    backend.scroll(-1)
    assert backend.trace() == ["scroll +2", "scroll -1"]


# --- files ----------------------------------------------------------------------
def test_every_new_step_survives_a_save_and_load() -> None:
    original = Strategy(
        name="all",
        aspect_ratio=1.6,
        steps=[
            Click(position=NormPoint(0.1, 0.2), button="middle"),
            KeyPress(key="space", hold_ms=400),
            Drag(button="right", path=[DragPoint(0, NormPoint(0.5, 0.5)),
                                       DragPoint(120, NormPoint(0.6, 0.4))]),
            Scroll(position=NormPoint(0.3, 0.3), clicks=-2),
        ],
    )
    loaded = Strategy.from_json(original.to_json())
    assert loaded.to_json() == original.to_json()
    assert loaded.aspect_ratio == 1.6
    assert kinds(loaded.steps) == ["Click", "KeyPress", "Drag", "Scroll"]
    assert loaded.validate() == []


def test_files_are_written_with_the_current_schema_version() -> None:
    import json

    assert json.loads(Strategy(name="x").to_json())["schema_version"] == SCHEMA_VERSION == 2


@pytest.mark.parametrize(
    "step",
    [
        Click(position=NormPoint(1.5, 0.2)),
        Click(position=NormPoint(0.5, 0.5), button="side"),
        KeyPress(key="not-a-key"),
        Drag(button="right", path=[]),
        Scroll(position=NormPoint(0.5, 0.5), clicks=0),
    ],
)
def test_broken_new_steps_are_caught_before_playing(step: object) -> None:
    assert Strategy(name="bad", steps=[step]).validate() != []  # type: ignore[list-item]


def test_a_file_from_the_first_version_still_loads() -> None:
    old = (
        '{"schema_version": 1, "name": "old", "steps": ['
        '{"kind": "wait", "ms": 500}]}'
    )
    try:
        loaded = Strategy.from_json(old)
    except StrategyError as exc:  # pragma: no cover - would be a regression
        pytest.fail(f"version 1 file rejected: {exc}")
    assert kinds(loaded.steps) == ["Wait"] and loaded.aspect_ratio == 0.0


# --- review fixes ----------------------------------------------------------------------
from tds_macro.input.coords import Rect  # noqa: E402
from tds_macro.input.events import DragTracker, WheelAccumulator, build_raw_event  # noqa: E402


def test_a_held_key_is_not_waited_out_twice() -> None:
    rec = keep([down(0, "w"), up(2_000, "w"), press(2_500, 0.9, 0.1)])
    steps = rec.finish().steps
    assert kinds(steps) == ["KeyPress", "Wait", "Click"]
    assert steps[0].hold_ms == 2_000
    assert abs(steps[1].ms - 500) <= 50  # from the release, not from the press


def test_a_dropped_hotbar_press_never_moves_the_clock_backwards() -> None:
    rec = keep(
        [
            down(0, "1"), up(30, "1"),                       # selected, never placed
            press(1_000, 0.5, 0.5, "right"), move(2_000, 0.7, 0.5),
            release(3_000, 0.7, 0.5, "right"),               # a camera turn
            down(4_000, "2"), up(4_030, "2"), press(4_100, 0.4, 0.4),
        ]
    )
    steps = rec.finish().steps
    assert kinds(steps)[-2:] == ["KeyPress", "Place"] or kinds(steps)[-3:-1] == [
        "KeyPress", "Wait"
    ]
    waited = sum(s.ms for s in steps if isinstance(s, Wait))
    assert waited <= 4_100, f"{waited} ms of waits in a 4.1 s recording"


def test_a_camera_turn_with_the_cursor_locked_keeps_its_movement() -> None:
    """Roblox puts the cursor back while the camera turns: the release is
    reported where the drag began, and must not turn the camera back."""
    rec = keep(
        [
            press(0, 0.5, 0.5, "right"),
            move(50, 0.55, 0.5),
            move(100, 0.65, 0.5),
            release(150, 0.5, 0.5, "right"),
        ],
        insert_waits=False,
    )
    steps = rec.finish().steps
    assert kinds(steps) == ["Drag"]
    assert steps[0].path[-1].position == NormPoint(0.65, 0.5)
    assert steps[0].duration_ms == 150


def test_small_wheel_steps_add_up_to_whole_notches() -> None:
    wheel = WheelAccumulator(120)
    assert [wheel.add(30) for _ in range(4)] == [0, 0, 0, 1]
    assert wheel.add(-30) == 0 and wheel.add(-90) == -1  # reversal starts afresh
    assert WheelAccumulator(120).add(-240) == -2
    lines = WheelAccumulator(1.0)
    assert sum(lines.add(0.4) for _ in range(5)) == 2


def test_a_drag_is_followed_by_its_movement_not_the_locked_cursor() -> None:
    tracker = DragTracker()
    tracker.press((100, 100))
    for _ in range(3):
        where = tracker.move_by(5, -2, fallback=(100, 100))
    assert where == (115, 94)
    tracker.release()
    assert tracker.position is None


def test_drag_movement_keeps_a_position_past_the_window_edge() -> None:
    client = Rect(0, 0, 200, 100)
    moved = build_raw_event(0, RawKind.MOUSE_MOVE, None, "right", (260, 50), client)
    assert moved.pos is not None and moved.pos.x == pytest.approx(1.3)
    assert not moved.in_client
    clicked = build_raw_event(0, RawKind.MOUSE_DOWN, None, "left", (260, 50), client)
    assert clicked.pos is None


def test_a_drag_past_the_edge_reaches_the_backend_unclamped() -> None:
    """The Mac backend turns the camera by the full movement, so the engine
    must hand it the real point; each backend clamps the cursor itself."""
    path = [DragPoint(0, NormPoint(0.5, 0.5)), DragPoint(100, NormPoint(1.4, 0.5))]
    runner, backend, clock = runner_for([Drag(button="right", path=path)])
    sent: list[NormPoint] = []
    real_move = backend.move

    def spy(point: NormPoint) -> bool:
        sent.append(point)
        return real_move(point)

    backend.move = spy  # type: ignore[method-assign]
    runner.start()
    pump(runner, clock)
    assert sent == [NormPoint(0.5, 0.5), NormPoint(1.4, 0.5)]
