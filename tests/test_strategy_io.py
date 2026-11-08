"""Strategy validation and the shareable JSON format."""

from __future__ import annotations

import json

import pytest

from tds_macro.input.coords import NormPoint
from tds_macro.strategy.steps import (
    SCHEMA_VERSION,
    Ability,
    Place,
    Sell,
    SkipWave,
    Strategy,
    StrategyError,
    Upgrade,
    Wait,
    WaitForCash,
    WaitForWave,
    step_from_dict,
)


def sample() -> Strategy:
    return Strategy(
        name="Molten solo",
        map_name="Crossroads",
        difficulty="Molten",
        steps=[
            Place(slot=1, position=NormPoint(0.31, 0.62), tower_id="T1", tower_name="Scout"),
            WaitForCash(amount=500),
            Upgrade(tower_id="T1", times=2),
            WaitForWave(wave=5, timeout_ms=90_000),
            Place(slot=3, position=NormPoint(0.68, 0.44), tower_id="T2"),
            Ability(tower_id="T2"),
            SkipWave(times=2),
            Wait(ms=1_500),
            Sell(tower_id="T1", note="make room for the farm"),
        ],
    )


def test_round_trip_preserves_every_field() -> None:
    original = sample()
    restored = Strategy.from_json(original.to_json())
    assert restored.to_dict() == original.to_dict()
    assert restored.steps[0].tower_name == "Scout"
    assert restored.steps[3].timeout_ms == 90_000
    assert restored.steps[8].note == "make room for the farm"


def test_a_valid_strategy_reports_no_problems() -> None:
    assert sample().validate() == []


def test_disabled_flag_survives_a_round_trip() -> None:
    strategy = Strategy(steps=[Wait(ms=10, enabled=False)])
    assert Strategy.from_json(strategy.to_json()).steps[0].enabled is False


def test_defaults_are_omitted_to_keep_shared_files_readable() -> None:
    data = json.loads(Strategy(steps=[Wait(ms=10)]).to_json())
    assert data["steps"][0] == {"kind": "wait", "ms": 10}


@pytest.mark.parametrize(
    ("steps", "expected"),
    [
        ([Upgrade(tower_id="T9")], "never placed"),
        ([Place(slot=9, position=NormPoint(0.5, 0.5))], "outside 1-5"),
        ([Place(slot=1, position=NormPoint(1.4, 0.2))], "outside the game window"),
        (
            [Place(slot=1, position=NormPoint(0.5, 0.5), tower_id="T1"),
             Sell(tower_id="T1"), Upgrade(tower_id="T1")],
            "already sold",
        ),
        (
            [Place(slot=1, position=NormPoint(0.2, 0.2), tower_id="T1"),
             Place(slot=2, position=NormPoint(0.4, 0.4), tower_id="T1")],
            "already in use",
        ),
        ([WaitForWave(wave=0)], "wave numbers start at 1"),
        ([Wait(ms=-5)], "negative wait"),
    ],
)
def test_validation_catches_broken_references(steps: list, expected: str) -> None:
    problems = Strategy(steps=steps).validate()
    assert problems, "expected a validation problem"
    assert any(expected in p for p in problems), problems


def test_replacing_a_sold_tower_clears_the_sold_state() -> None:
    strategy = Strategy(
        steps=[
            Place(slot=1, position=NormPoint(0.2, 0.2), tower_id="T1"),
            Sell(tower_id="T1"),
            Place(slot=2, position=NormPoint(0.3, 0.3), tower_id="T1"),
            Upgrade(tower_id="T1"),
        ]
    )
    assert [p for p in strategy.validate() if "already sold" in p] == []


def test_newer_schema_versions_are_refused_with_an_actionable_message() -> None:
    with pytest.raises(StrategyError, match="newer version"):
        Strategy.from_dict({"schema_version": SCHEMA_VERSION + 1, "steps": []})


def test_malformed_files_fail_loudly() -> None:
    with pytest.raises(StrategyError, match="not valid JSON"):
        Strategy.from_json("{nope")
    with pytest.raises(StrategyError, match="must contain a JSON object"):
        Strategy.from_json("[]")
    with pytest.raises(StrategyError, match="'steps' must be a list"):
        Strategy.from_dict({"steps": "nope"})
    with pytest.raises(StrategyError, match="unknown step kind"):
        step_from_dict({"kind": "teleport"})
    with pytest.raises(StrategyError, match="malformed place step"):
        step_from_dict({"kind": "place", "slot": "x", "position": {"x": 0, "y": 0}})


def test_missing_optional_metadata_loads_with_defaults() -> None:
    strategy = Strategy.from_dict({"steps": [{"kind": "wait", "ms": 5}]})
    assert strategy.name == "Untitled strategy"
    assert strategy.schema_version == SCHEMA_VERSION


def test_next_tower_id_fills_gaps() -> None:
    strategy = Strategy(
        steps=[
            Place(slot=1, position=NormPoint(0.1, 0.1), tower_id="T1"),
            Place(slot=2, position=NormPoint(0.2, 0.2), tower_id="T3"),
        ]
    )
    assert strategy.next_tower_id() == "T2"


def test_estimated_time_ignores_disabled_steps() -> None:
    strategy = Strategy(steps=[Wait(ms=1_000), Wait(ms=5_000, enabled=False)])
    assert strategy.estimated_ms() == 1_000


def test_descriptions_are_human_readable() -> None:
    assert "Place Scout as T1" in sample().steps[0].describe()
    assert sample().steps[1].describe() == "Wait for $500"
    assert sample().steps[2].describe() == "Upgrade T1 x2"
    assert sample().steps[6].describe() == "Skip wave x2"
    assert sample().steps[7].describe() == "Wait 1.50s"
