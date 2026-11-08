"""Raw input events captured during recording.

Pure data: the recorder turns these into strategy steps and is tested without
any Win32 involvement.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum

from .coords import NormPoint


class RawKind(Enum):
    KEY_DOWN = "key_down"
    KEY_UP = "key_up"
    MOUSE_DOWN = "mouse_down"
    MOUSE_UP = "mouse_up"


@dataclass(frozen=True)
class RawEvent:
    """One observed user action.

    ``pos`` is normalized to the Roblox client area and is ``None`` when the
    event happened outside it (or the window could not be measured at the
    time).  ``in_client`` is kept separately so the recorder can distinguish
    "outside the game window" from "position unknown".
    """

    t_ms: int
    kind: RawKind
    key: str | None = None
    button: str | None = None
    screen_pos: tuple[int, int] | None = None
    pos: NormPoint | None = None
    in_client: bool = False

    def describe(self) -> str:
        if self.kind in (RawKind.KEY_DOWN, RawKind.KEY_UP):
            return f"{self.kind.value} {self.key}"
        where = f" at ({self.pos.x:.3f}, {self.pos.y:.3f})" if self.pos else ""
        return f"{self.kind.value} {self.button}{where}"
