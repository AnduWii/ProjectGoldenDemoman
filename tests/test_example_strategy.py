"""The bundled example must stay loadable and valid.

A shipped example that fails validation is worse than none: it teaches the
format wrongly and makes the app look broken on first open.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from tds_macro.strategy.steps import SCHEMA_VERSION, StepKind, Strategy

EXAMPLES = sorted((Path(__file__).resolve().parent.parent / "strategies").glob("*.json"))


def test_there_is_at_least_one_example() -> None:
    assert EXAMPLES, "expected a bundled example strategy"


@pytest.mark.parametrize("path", EXAMPLES, ids=lambda p: p.name)
def test_example_loads_and_validates(path: Path) -> None:
    strategy = Strategy.from_json(path.read_text(encoding="utf-8"))
    assert strategy.validate() == []
    assert strategy.schema_version == SCHEMA_VERSION
    assert strategy.name
    assert strategy.description, "an example should explain itself"


@pytest.mark.parametrize("path", EXAMPLES, ids=lambda p: p.name)
def test_example_survives_a_round_trip(path: Path) -> None:
    original = Strategy.from_json(path.read_text(encoding="utf-8"))
    assert Strategy.from_json(original.to_json()).to_dict() == original.to_dict()


def test_the_example_demonstrates_every_step_type() -> None:
    """It is documentation as much as data; a missing type is a gap in both."""
    kinds: set[str] = set()
    for path in EXAMPLES:
        for step in Strategy.from_json(path.read_text(encoding="utf-8")).steps:
            kinds.add(step.kind.value)
    assert kinds == {kind.value for kind in StepKind}
