"""Strategy step types and the on-disk JSON format.

Pure data plus validation.  A step never knows how to execute itself -- that
lives in :mod:`tds_macro.strategy.engine` -- which keeps this module trivially
testable and lets the editor, the recorder and the importer all share one
definition of what a strategy *is*.

Positions are stored as normalized client-area coordinates, so a strategy file
is portable between resolutions, window sizes and monitors.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import Enum
from typing import Any, ClassVar

from ..input.coords import NormPoint

SCHEMA_VERSION = 1
MIN_SLOT = 1
MAX_SLOT = 5


class StepKind(Enum):
    PLACE = "place"
    UPGRADE = "upgrade"
    SELL = "sell"
    ABILITY = "ability"
    SKIP_WAVE = "skip_wave"
    WAIT = "wait"
    WAIT_FOR_WAVE = "wait_for_wave"
    WAIT_FOR_CASH = "wait_for_cash"


class StrategyError(ValueError):
    """Raised when a strategy file cannot be loaded or is structurally invalid."""


@dataclass
class Step:
    """Base class for every step.

    ``enabled`` lets the user mute a step without deleting it, which is how you
    bisect a strategy that places one tower in the wrong spot.
    """

    kind: ClassVar[StepKind]
    enabled: bool = True
    note: str = ""

    def describe(self) -> str:  # pragma: no cover - overridden everywhere
        return self.kind.value

    def payload(self) -> dict[str, Any]:
        return {}

    def to_dict(self) -> dict[str, Any]:
        data: dict[str, Any] = {"kind": self.kind.value, **self.payload()}
        if not self.enabled:
            data["enabled"] = False
        if self.note:
            data["note"] = self.note
        return data

    def estimated_ms(self) -> int:
        """Rough duration, used only for the editor's total-time estimate."""
        return 0


@dataclass
class Place(Step):
    """Select a hotbar slot, then click a position to place that tower."""

    kind: ClassVar[StepKind] = StepKind.PLACE
    slot: int = 1
    position: NormPoint = field(default_factory=lambda: NormPoint(0.5, 0.5))
    tower_id: str = "T1"
    tower_name: str = ""

    def describe(self) -> str:
        name = self.tower_name or f"slot {self.slot}"
        return (
            f"Place {name} as {self.tower_id} at "
            f"({self.position.x:.3f}, {self.position.y:.3f})"
        )

    def payload(self) -> dict[str, Any]:
        data: dict[str, Any] = {
            "slot": self.slot,
            "position": self.position.to_dict(),
            "tower_id": self.tower_id,
        }
        if self.tower_name:
            data["tower_name"] = self.tower_name
        return data

    def estimated_ms(self) -> int:
        return 400


@dataclass
class TowerRefStep(Step):
    """Shared base for steps that act on a previously placed tower."""

    tower_id: str = "T1"


@dataclass
class Upgrade(TowerRefStep):
    """Select a placed tower and press the upgrade key ``times`` times."""

    kind: ClassVar[StepKind] = StepKind.UPGRADE
    times: int = 1

    def describe(self) -> str:
        suffix = f" x{self.times}" if self.times != 1 else ""
        return f"Upgrade {self.tower_id}{suffix}"

    def payload(self) -> dict[str, Any]:
        return {"tower_id": self.tower_id, "times": self.times}

    def estimated_ms(self) -> int:
        return 300 * max(1, self.times)


@dataclass
class Sell(TowerRefStep):
    kind: ClassVar[StepKind] = StepKind.SELL

    def describe(self) -> str:
        return f"Sell {self.tower_id}"

    def payload(self) -> dict[str, Any]:
        return {"tower_id": self.tower_id}

    def estimated_ms(self) -> int:
        return 300


@dataclass
class Ability(TowerRefStep):
    kind: ClassVar[StepKind] = StepKind.ABILITY

    def describe(self) -> str:
        return f"Use ability of {self.tower_id}"

    def payload(self) -> dict[str, Any]:
        return {"tower_id": self.tower_id}

    def estimated_ms(self) -> int:
        return 300


@dataclass
class SkipWave(Step):
    kind: ClassVar[StepKind] = StepKind.SKIP_WAVE
    times: int = 1

    def describe(self) -> str:
        return f"Skip wave x{self.times}" if self.times != 1 else "Skip wave"

    def payload(self) -> dict[str, Any]:
        return {"times": self.times}

    def estimated_ms(self) -> int:
        return 200 * max(1, self.times)


@dataclass
class Wait(Step):
    kind: ClassVar[StepKind] = StepKind.WAIT
    ms: int = 1000

    def describe(self) -> str:
        return f"Wait {self.ms / 1000:.2f}s"

    def payload(self) -> dict[str, Any]:
        return {"ms": self.ms}

    def estimated_ms(self) -> int:
        return self.ms


@dataclass
class WaitForWave(Step):
    """Block until the HUD reports wave >= ``wave``.

    ``timeout_ms`` of 0 means wait forever; any other value is a safety valve
    so a mis-calibrated HUD stalls with a visible error instead of hanging.
    """

    kind: ClassVar[StepKind] = StepKind.WAIT_FOR_WAVE
    wave: int = 2
    timeout_ms: int = 0

    def describe(self) -> str:
        return f"Wait for wave {self.wave}"

    def payload(self) -> dict[str, Any]:
        return {"wave": self.wave, "timeout_ms": self.timeout_ms}

    def estimated_ms(self) -> int:
        return 0


@dataclass
class WaitForCash(Step):
    kind: ClassVar[StepKind] = StepKind.WAIT_FOR_CASH
    amount: int = 1000
    timeout_ms: int = 0

    def describe(self) -> str:
        return f"Wait for ${self.amount:,}"

    def payload(self) -> dict[str, Any]:
        return {"amount": self.amount, "timeout_ms": self.timeout_ms}

    def estimated_ms(self) -> int:
        return 0


STEP_CLASSES: dict[StepKind, type[Step]] = {
    StepKind.PLACE: Place,
    StepKind.UPGRADE: Upgrade,
    StepKind.SELL: Sell,
    StepKind.ABILITY: Ability,
    StepKind.SKIP_WAVE: SkipWave,
    StepKind.WAIT: Wait,
    StepKind.WAIT_FOR_WAVE: WaitForWave,
    StepKind.WAIT_FOR_CASH: WaitForCash,
}


def step_from_dict(data: dict[str, Any]) -> Step:
    """Rebuild a step from its JSON form, rejecting anything unrecognised."""
    try:
        kind = StepKind(data["kind"])
    except (KeyError, ValueError) as exc:
        raise StrategyError(f"unknown step kind: {data.get('kind')!r}") from exc

    common = {"enabled": bool(data.get("enabled", True)), "note": str(data.get("note", ""))}
    try:
        if kind is StepKind.PLACE:
            return Place(
                slot=int(data["slot"]),
                position=NormPoint.from_dict(data["position"]),
                tower_id=str(data.get("tower_id", "T1")),
                tower_name=str(data.get("tower_name", "")),
                **common,
            )
        if kind is StepKind.UPGRADE:
            return Upgrade(
                tower_id=str(data["tower_id"]), times=int(data.get("times", 1)), **common
            )
        if kind is StepKind.SELL:
            return Sell(tower_id=str(data["tower_id"]), **common)
        if kind is StepKind.ABILITY:
            return Ability(tower_id=str(data["tower_id"]), **common)
        if kind is StepKind.SKIP_WAVE:
            return SkipWave(times=int(data.get("times", 1)), **common)
        if kind is StepKind.WAIT:
            return Wait(ms=int(data["ms"]), **common)
        if kind is StepKind.WAIT_FOR_WAVE:
            return WaitForWave(
                wave=int(data["wave"]), timeout_ms=int(data.get("timeout_ms", 0)), **common
            )
        return WaitForCash(
            amount=int(data["amount"]), timeout_ms=int(data.get("timeout_ms", 0)), **common
        )
    except (KeyError, TypeError, ValueError) as exc:
        raise StrategyError(f"malformed {kind.value} step: {exc}") from exc


@dataclass
class Strategy:
    """An ordered list of steps plus the metadata needed to share it."""

    name: str = "Untitled strategy"
    steps: list[Step] = field(default_factory=list)
    description: str = ""
    map_name: str = ""
    difficulty: str = ""
    created_utc: str = field(
        default_factory=lambda: datetime.now(UTC).isoformat(timespec="seconds")
    )
    schema_version: int = SCHEMA_VERSION

    # -- tower references -----------------------------------------------------
    def next_tower_id(self) -> str:
        """Allocate an unused tower id (``T1``, ``T2``, ...)."""
        used = {s.tower_id for s in self.steps if isinstance(s, Place)}
        index = 1
        while f"T{index}" in used:
            index += 1
        return f"T{index}"

    def placements(self) -> dict[str, Place]:
        return {s.tower_id: s for s in self.steps if isinstance(s, Place)}

    def validate(self) -> list[str]:
        """Return human-readable problems, in step order.

        Catches the failure modes that otherwise look like "the macro just
        skipped that bit": referencing a tower before it is placed, acting on a
        tower that was already sold, and out-of-range hotbar slots.
        """
        return [
            f"Step {index + 1} ({self.steps[index].describe()}): {message}"
            for index, message in self.problems_by_step()
        ]

    def problems_by_step(self) -> list[tuple[int, str]]:
        """Problems as ``(zero-based step index, message)``.

        The editor needs them per row so it can flag the offending step
        directly rather than making the user map a sentence back to a row.
        """
        problems: list[tuple[int, str]] = []
        placed: set[str] = set()
        sold: set[str] = set()

        for position, step in enumerate(self.steps):
            index = position
            if isinstance(step, Place):
                if not MIN_SLOT <= step.slot <= MAX_SLOT:
                    problems.append(
                        (index, f"hotbar slot {step.slot} is outside {MIN_SLOT}-{MAX_SLOT}")
                    )
                if step.tower_id in placed:
                    problems.append((index, f"tower id {step.tower_id} is already in use"))
                if not step.position.is_inside():
                    problems.append((index, "position is outside the game window"))
                placed.add(step.tower_id)
                sold.discard(step.tower_id)
            elif isinstance(step, TowerRefStep):
                if step.tower_id not in placed:
                    problems.append(
                        (index, f"{step.tower_id} is never placed before this point")
                    )
                elif step.tower_id in sold:
                    problems.append((index, f"{step.tower_id} was already sold"))
                if isinstance(step, Sell):
                    sold.add(step.tower_id)
            elif isinstance(step, Wait) and step.ms < 0:
                problems.append((index, "negative wait"))
            elif isinstance(step, WaitForWave) and step.wave < 1:
                problems.append((index, "wave numbers start at 1"))
            elif isinstance(step, WaitForCash) and step.amount < 0:
                problems.append((index, "negative cash target"))
        return problems

    def estimated_ms(self) -> int:
        return sum(s.estimated_ms() for s in self.steps if s.enabled)

    # -- serialization --------------------------------------------------------
    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": SCHEMA_VERSION,
            "name": self.name,
            "description": self.description,
            "map_name": self.map_name,
            "difficulty": self.difficulty,
            "created_utc": self.created_utc,
            "steps": [s.to_dict() for s in self.steps],
        }

    def to_json(self, indent: int = 2) -> str:
        return json.dumps(self.to_dict(), indent=indent)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> Strategy:
        version = int(data.get("schema_version", SCHEMA_VERSION))
        if version > SCHEMA_VERSION:
            raise StrategyError(
                f"this strategy was saved by a newer version of TDS Macro "
                f"(format {version}, this build understands {SCHEMA_VERSION}). Update the app."
            )
        raw_steps = data.get("steps", [])
        if not isinstance(raw_steps, list):
            raise StrategyError("'steps' must be a list")
        return cls(
            name=str(data.get("name", "Untitled strategy")),
            description=str(data.get("description", "")),
            map_name=str(data.get("map_name", "")),
            difficulty=str(data.get("difficulty", "")),
            created_utc=str(data.get("created_utc", "")),
            steps=[step_from_dict(s) for s in raw_steps],
            schema_version=version,
        )

    @classmethod
    def from_json(cls, text: str) -> Strategy:
        try:
            data = json.loads(text)
        except json.JSONDecodeError as exc:
            raise StrategyError(f"not valid JSON: {exc}") from exc
        if not isinstance(data, dict):
            raise StrategyError("a strategy file must contain a JSON object")
        return cls.from_dict(data)
