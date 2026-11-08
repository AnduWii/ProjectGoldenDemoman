"""Recorder: raw input stream -> semantic steps."""

from __future__ import annotations

from tds_macro.config import ActionBinding, Keybinds
from tds_macro.input.coords import NormPoint
from tds_macro.input.events import RawEvent, RawKind
from tds_macro.strategy.recorder import Recorder, RecorderOptions
from tds_macro.strategy.steps import Ability, Place, Sell, SkipWave, Upgrade, Wait


def key(t: int, name: str) -> RawEvent:
    return RawEvent(t_ms=t, kind=RawKind.KEY_DOWN, key=name)


def click(t: int, x: float, y: float, inside: bool = True) -> RawEvent:
    return RawEvent(
        t_ms=t,
        kind=RawKind.MOUSE_DOWN,
        button="left",
        pos=NormPoint(x, y) if inside else None,
        in_client=inside,
    )


def record(events: list[RawEvent], **kwargs) -> Recorder:
    recorder = Recorder(Keybinds(), RecorderOptions(**kwargs))
    recorder.feed_all(events)
    return recorder


def test_hotbar_key_then_click_becomes_a_place_step() -> None:
    rec = record([key(0, "2"), click(100, 0.4, 0.6)])
    strategy = rec.finish()
    assert len(strategy.steps) == 1
    step = strategy.steps[0]
    assert isinstance(step, Place)
    assert (step.slot, step.tower_id) == (2, "T1")
    assert step.position.as_tuple() == (0.4, 0.6)


def test_raw_clicks_are_not_recorded_as_coordinate_spam() -> None:
    """A click that matches nothing produces no step, and is counted."""
    rec = record([click(0, 0.1, 0.1), click(50, 0.2, 0.2), click(90, 0.3, 0.3)])
    assert rec.finish().steps == []
    assert rec.stats.unmatched_clicks == 3
    assert "did not match a placed tower" in " ".join(rec.stats.warnings())


def test_click_near_a_placed_tower_then_upgrade_key_references_that_tower() -> None:
    rec = record(
        [
            key(0, "1"),
            click(100, 0.30, 0.50),
            key(200, "3"),
            click(300, 0.70, 0.50),
            click(2_000, 0.31, 0.51),  # back to the first tower
            key(2_100, "e"),
        ],
        insert_waits=False,
    )
    steps = rec.finish().steps
    assert [type(s) for s in steps] == [Place, Place, Upgrade]
    assert steps[2].tower_id == "T1"
    assert rec.stats.inferred_tower_refs == 1


def test_click_too_far_from_any_tower_does_not_select_one() -> None:
    rec = record(
        [key(0, "1"), click(100, 0.30, 0.50), click(500, 0.80, 0.80), key(600, "e")],
        insert_waits=False,
    )
    steps = rec.finish().steps
    assert [type(s) for s in steps] == [Place]  # the upgrade had nothing selected
    assert rec.stats.unmatched_clicks == 1
    assert rec.stats.unmatched_actions == 1


def test_repeated_upgrades_coalesce_into_one_step() -> None:
    rec = record(
        [key(0, "1"), click(50, 0.3, 0.3), key(100, "e"), key(200, "e"), key(300, "e")],
        insert_waits=False,
    )
    steps = rec.finish().steps
    assert [type(s) for s in steps] == [Place, Upgrade]
    assert steps[1].times == 3


def test_repeated_skips_coalesce() -> None:
    rec = record([key(0, "g"), key(100, "g")], insert_waits=False)
    steps = rec.finish().steps
    assert len(steps) == 1 and isinstance(steps[0], SkipWave) and steps[0].times == 2


def test_sell_removes_the_tower_from_later_matching() -> None:
    rec = record(
        [
            key(0, "1"),
            click(50, 0.3, 0.3),
            key(100, "x"),
            click(1_000, 0.3, 0.3),
            key(1_100, "e"),
        ],
        insert_waits=False,
    )
    steps = rec.finish().steps
    assert [type(s) for s in steps] == [Place, Sell]
    assert rec.stats.unmatched_clicks == 1  # the click on the sold tower matched nothing
    assert rec.stats.unmatched_actions == 1  # and the upgrade that followed had no target


def test_ability_key_emits_an_ability_step() -> None:
    rec = record([key(0, "1"), click(50, 0.3, 0.3), key(100, "f")], insert_waits=False)
    assert isinstance(rec.finish().steps[-1], Ability)


def test_idle_gaps_become_quantized_wait_steps() -> None:
    rec = record(
        [key(0, "1"), click(50, 0.2, 0.2), key(3_040, "2"), click(3_100, 0.6, 0.6)],
        min_wait_ms=400,
        quantize_wait_ms=100,
    )
    steps = rec.finish().steps
    assert [type(s) for s in steps] == [Place, Wait, Place]
    assert steps[1].ms == 3000


def test_short_gaps_do_not_create_wait_steps() -> None:
    rec = record(
        [key(0, "1"), click(50, 0.2, 0.2), key(200, "2"), click(250, 0.6, 0.6)],
        min_wait_ms=400,
    )
    assert [type(s) for s in rec.finish().steps] == [Place, Place]


def test_trailing_wait_is_dropped() -> None:
    rec = record([key(0, "1"), click(50, 0.2, 0.2), key(9_000, "q")])
    steps = rec.finish().steps
    assert not any(isinstance(s, Wait) for s in steps)
    assert "q" in rec.stats.ignored_keys


def test_clicks_outside_the_game_window_are_ignored() -> None:
    rec = record([key(0, "1"), click(50, 0, 0, inside=False)])
    assert rec.finish().steps == []
    assert rec.stats.clicks_outside_window == 1
    assert rec.stats.dropped_slot_presses == 1


def test_hotbar_press_without_a_following_click_is_dropped() -> None:
    rec = record([key(0, "1"), key(9_000, "2"), click(9_050, 0.5, 0.5)], insert_waits=False)
    steps = rec.finish().steps
    assert len(steps) == 1 and steps[0].slot == 2
    assert rec.stats.dropped_slot_presses == 1


def test_stale_hotbar_press_times_out_and_the_click_selects_instead() -> None:
    rec = record(
        [
            key(0, "1"),
            click(50, 0.3, 0.3),
            key(1_000, "2"),
            click(30_000, 0.3, 0.3),
            key(30_100, "e"),
        ],
        insert_waits=False,
        pending_slot_timeout_ms=5_000,
    )
    steps = rec.finish().steps
    assert [type(s) for s in steps] == [Place, Upgrade]
    assert steps[1].tower_id == "T1"
    assert rec.stats.dropped_slot_presses == 1


def test_menu_button_clicks_are_recognised_as_actions() -> None:
    button = NormPoint(0.92, 0.40)
    binds = Keybinds(upgrade=ActionBinding("click", point=button))
    rec = Recorder(binds, RecorderOptions(insert_waits=False))
    rec.feed_all(
        [key(0, "1"), click(50, 0.3, 0.3), click(400, 0.3, 0.3), click(500, button.x, button.y)]
    )
    steps = rec.finish().steps
    assert [type(s) for s in steps] == [Place, Upgrade]


def test_recorded_strategy_passes_validation() -> None:
    rec = record(
        [key(0, "1"), click(50, 0.3, 0.3), key(100, "e"), key(200, "3"), click(260, 0.7, 0.2)],
        insert_waits=False,
    )
    assert rec.finish().validate() == []


def test_wave_gates_can_be_inserted_from_live_hud_readings() -> None:
    waves = iter([5, 7])
    current = {"wave": 5}

    def provider() -> int:
        current["wave"] = next(waves, current["wave"])
        return current["wave"]

    options = RecorderOptions(insert_waits=False, insert_wave_gates=True)
    rec = Recorder(Keybinds(), options, provider)
    rec.feed_all([key(0, "1"), click(50, 0.2, 0.2), key(60, "2"), click(100, 0.6, 0.6)])
    kinds = [type(s).__name__ for s in rec.finish().steps]
    assert kinds == ["Place", "WaitForWave", "Place"]


def test_a_pending_hotbar_press_wins_over_a_nearby_menu_button() -> None:
    """Placing a tower near a calibrated Upgrade button is still a placement:
    the user selected a slot and then clicked, which is unambiguous intent."""
    button = NormPoint(0.50, 0.50)
    binds = Keybinds(upgrade=ActionBinding("click", point=button))
    rec = Recorder(binds, RecorderOptions(insert_waits=False))
    rec.feed_all([key(0, "1"), click(50, button.x, button.y)])
    steps = rec.finish().steps
    assert [type(s) for s in steps] == [Place]
    assert steps[0].position.as_tuple() == button.as_tuple()


def test_a_menu_button_click_with_no_pending_press_is_still_the_action() -> None:
    button = NormPoint(0.90, 0.30)
    binds = Keybinds(upgrade=ActionBinding("click", point=button))
    rec = Recorder(binds, RecorderOptions(insert_waits=False))
    rec.feed_all(
        [key(0, "1"), click(50, 0.2, 0.2), click(400, 0.2, 0.2), click(500, button.x, button.y)]
    )
    assert [type(s) for s in rec.finish().steps] == [Place, Upgrade]
