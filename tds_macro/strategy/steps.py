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
from ..input.keys import KeyParseError, normalize_key_name

# 2: raw click/key/drag/scroll steps and the recording's aspect ratio.
SCHEMA_VERSION = 2
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
    CLICK = "click"
    KEY = "key"
    DRAG = "drag"
    SCROLL = "scroll"


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


# --- raw input steps ------------------------------------------------------------
# What the recorder emits for anything it cannot name as a tower action, so that
# nothing the user did is dropped -- the TinyTask behaviour. They replay exactly
# what was done, without the retry and tower tracking the named steps get.
MOUSE_BUTTONS = ("left", "right", "middle")


@dataclass
class Click(Step):
    """A mouse click at a position in the game window."""

    kind: ClassVar[StepKind] = StepKind.CLICK
    position: NormPoint = field(default_factory=lambda: NormPoint(0.5, 0.5))
    button: str = "left"

    def describe(self) -> str:
        which = "" if self.button == "left" else f"{self.button} "
        return f"Click {which}at ({self.position.x:.3f}, {self.position.y:.3f})"

    def payload(self) -> dict[str, Any]:
        data: dict[str, Any] = {"position": self.position.to_dict()}
        if self.button != "left":
            data["button"] = self.button
        return data

    def estimated_ms(self) -> int:
        return 150


@dataclass
class KeyPress(Step):
    """Press and release a key, holding it for ``hold_ms``."""

    kind: ClassVar[StepKind] = StepKind.KEY
    key: str = "space"
    hold_ms: int = 35

    def describe(self) -> str:
        held = f" (held {self.hold_ms / 1000:.1f}s)" if self.hold_ms >= 300 else ""
        return f"Press {self.key.upper()}{held}"

    def payload(self) -> dict[str, Any]:
        return {"key": self.key, "hold_ms": self.hold_ms}

    def estimated_ms(self) -> int:
        return self.hold_ms


@dataclass(frozen=True)
class DragPoint:
    """One sample of a drag: where the pointer was, ``t_ms`` after it started."""

    t_ms: int
    position: NormPoint

    def to_dict(self) -> dict[str, Any]:
        return {"t": self.t_ms, **self.position.to_dict()}

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> DragPoint:
        return cls(int(data["t"]), NormPoint(float(data["x"]), float(data["y"])))


@dataclass
class Drag(Step):
    """Hold a mouse button and move along a recorded path -- e.g. turning the
    camera with the right button. Replayed with the original timing."""

    kind: ClassVar[StepKind] = StepKind.DRAG
    button: str = "right"
    path: list[DragPoint] = field(default_factory=list)

    @property
    def duration_ms(self) -> int:
        return self.path[-1].t_ms if self.path else 0

    def describe(self) -> str:
        return f"Drag with {self.button} button ({self.duration_ms / 1000:.1f}s)"

    def payload(self) -> dict[str, Any]:
        return {"button": self.button, "path": [p.to_dict() for p in self.path]}

    def estimated_ms(self) -> int:
        return self.duration_ms + 100


@dataclass
class Scroll(Step):
    """Turn the mouse wheel at a position. Positive scrolls up (zooms in)."""

    kind: ClassVar[StepKind] = StepKind.SCROLL
    position: NormPoint = field(default_factory=lambda: NormPoint(0.5, 0.5))
    clicks: int = 1

    def describe(self) -> str:
        direction = "up" if self.clicks > 0 else "down"
        return f"Scroll {direction} x{abs(self.clicks)}"

    def payload(self) -> dict[str, Any]:
        return {"position": self.position.to_dict(), "clicks": self.clicks}

    def estimated_ms(self) -> int:
        return 100


STEP_CLASSES: dict[StepKind, type[Step]] = {
    StepKind.PLACE: Place,
    StepKind.UPGRADE: Upgrade,
    StepKind.SELL: Sell,
    StepKind.ABILITY: Ability,
    StepKind.SKIP_WAVE: SkipWave,
    StepKind.WAIT: Wait,
    StepKind.WAIT_FOR_WAVE: WaitForWave,
    StepKind.WAIT_FOR_CASH: WaitForCash,
    StepKind.CLICK: Click,
    StepKind.KEY: KeyPress,
    StepKind.DRAG: Drag,
    StepKind.SCROLL: Scroll,
}


def step_from_dict(data: dict[str, Any]) -> Step:
    """Rebuild a step from its JSON form, rejecting anything unrecognised."""
    try:
        kind = StepKind(data["kind"])
    except (KeyError, ValueError) as exc:
        raise StrategyError(f"unknown step kind: {data.get('kind')!r}") from exc

    enabled = bool(data.get("enabled", True))
    note = str(data.get("note", ""))
    try:
        if kind is StepKind.PLACE:
            return Place(
                enabled=enabled,
                note=note,
                slot=int(data["slot"]),
                position=NormPoint.from_dict(data["position"]),
                tower_id=str(data.get("tower_id", "T1")),
                tower_name=str(data.get("tower_name", "")),
            )
        if kind is StepKind.UPGRADE:
            return Upgrade(
                enabled=enabled,
                note=note,
                tower_id=str(data["tower_id"]),
                times=int(data.get("times", 1)),
            )
        if kind is StepKind.SELL:
            return Sell(enabled=enabled, note=note, tower_id=str(data["tower_id"]))
        if kind is StepKind.ABILITY:
            return Ability(enabled=enabled, note=note, tower_id=str(data["tower_id"]))
        if kind is StepKind.SKIP_WAVE:
            return SkipWave(enabled=enabled, note=note, times=int(data.get("times", 1)))
        if kind is StepKind.WAIT:
            return Wait(enabled=enabled, note=note, ms=int(data["ms"]))
        if kind is StepKind.WAIT_FOR_WAVE:
            return WaitForWave(
                enabled=enabled,
                note=note,
                wave=int(data["wave"]),
                timeout_ms=int(data.get("timeout_ms", 0)),
            )
        if kind is StepKind.WAIT_FOR_CASH:
            return WaitForCash(
                enabled=enabled,
                note=note,
                amount=int(data["amount"]),
                timeout_ms=int(data.get("timeout_ms", 0)),
            )
        if kind is StepKind.CLICK:
            return Click(
                enabled=enabled,
                note=note,
                position=NormPoint.from_dict(data["position"]),
                button=str(data.get("button", "left")),
            )
        if kind is StepKind.KEY:
            return KeyPress(
                enabled=enabled,
                note=note,
                key=str(data["key"]),
                hold_ms=int(data.get("hold_ms", 35)),
            )
        if kind is StepKind.DRAG:
            return Drag(
                enabled=enabled,
                note=note,
                button=str(data.get("button", "right")),
                path=[DragPoint.from_dict(point) for point in data["path"]],
            )
        return Scroll(
            enabled=enabled,
            note=note,
            position=NormPoint.from_dict(data["position"]),
            clicks=int(data["clicks"]),
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
    # Width / height of the game view this was recorded at; 0 if unknown.
    # Roblox keeps the vertical field of view fixed, so on a wider or narrower
    # screen the map shows more or less at the sides, and tower positions are
    # adjusted for that at replay (see coords.remap_world_point).
    aspect_ratio: float = 0.0

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
            elif isinstance(step, Click | Scroll) and not step.position.is_inside():
                problems.append((index, "position is outside the game window"))
            elif isinstance(step, Click | Drag) and step.button not in MOUSE_BUTTONS:
                problems.append((index, f"unknown mouse button {step.button!r}"))
            elif isinstance(step, KeyPress):
                try:
                    normalize_key_name(step.key)
                except KeyParseError as exc:
                    problems.append((index, str(exc)))
            elif isinstance(step, Drag) and not step.path:
                problems.append((index, "drag has no recorded movement"))
            elif isinstance(step, Scroll) and step.clicks == 0:
                problems.append((index, "scroll of zero clicks"))
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
            "aspect_ratio": round(self.aspect_ratio, 4),
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
            aspect_ratio=float(data.get("aspect_ratio", 0.0) or 0.0),
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
