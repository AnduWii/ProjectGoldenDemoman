"""Raw input events captured during recording.

Pure data: the recorder turns these into strategy steps and is tested without
any Win32 involvement.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum

from .coords import NormPoint, Rect, screen_to_norm


class RawKind(Enum):
    KEY_DOWN = "key_down"
    KEY_UP = "key_up"
    MOUSE_DOWN = "mouse_down"
    MOUSE_UP = "mouse_up"
    # Pointer movement, captured only while a button is held (a drag).
    MOUSE_MOVE = "mouse_move"
    SCROLL = "scroll"


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
    # Wheel notches for SCROLL; positive is up / away from the user.
    delta: int = 0

    def describe(self) -> str:
        if self.kind in (RawKind.KEY_DOWN, RawKind.KEY_UP):
            return f"{self.kind.value} {self.key}"
        if self.kind is RawKind.SCROLL:
            return f"scroll {self.delta:+d}"
        where = f" at ({self.pos.x:.3f}, {self.pos.y:.3f})" if self.pos else ""
        return f"{self.kind.value} {self.button}{where}"


def build_raw_event(
    t_ms: int,
    kind: RawKind,
    key: str | None,
    button: str | None,
    screen_pos: tuple[int, int] | None,
    client: Rect | None,
    delta: int = 0,
) -> RawEvent:
    """Make a RawEvent, normalizing the position against the game's client area.

    Shared by the Windows hook and the macOS event tap so both platforms decide
    "inside the game window" identically.
    """
    pos: NormPoint | None = None
    in_client = False
    if screen_pos is not None and client is not None and client.is_valid():
        in_client = client.contains(*screen_pos)
        # Drag movement keeps its position even off the game view: a camera
        # turn is tracked as a virtual pointer that can travel well past the
        # window edge while the real cursor stays locked in place.
        if in_client or kind is RawKind.MOUSE_MOVE:
            pos = screen_to_norm(screen_pos[0], screen_pos[1], client)
    return RawEvent(
        t_ms=t_ms,
        kind=kind,
        key=key,
        button=button,
        screen_pos=screen_pos,
        pos=pos,
        in_client=in_client,
        delta=delta,
    )


class WheelAccumulator:
    """Turns wheel movement into whole notches without losing fractions.

    Precision touchpads and free-spinning or high-resolution wheels report a
    notch as several small deltas (on Windows, 30 + 30 + 30 + 30 instead of
    one 120). Rounding each to a whole notch would record four notches -- and
    replay a zoom four times too far. The remainder is carried instead, and
    it is dropped when the direction reverses.
    """

    def __init__(self, unit: float) -> None:
        self.unit = float(unit)
        self._rest = 0.0

    def reset(self) -> None:
        self._rest = 0.0

    def add(self, amount: float) -> int:
        if amount == 0:
            return 0
        if self._rest and (self._rest > 0) != (amount > 0):
            self._rest = 0.0
        self._rest += amount
        notches = int(self._rest / self.unit)  # truncates toward zero
        self._rest -= notches * self.unit
        return notches


class DragTracker:
    """A virtual pointer for drags, built from movement rather than position.

    While the right button turns the camera, Roblox locks the cursor in place:
    on macOS the reported location never changes (only the event's delta
    fields do), and on Windows the cursor is put back after every move. A path
    of reported positions therefore shows no movement, and the turn would be
    recorded as a plain click. Summing the movement instead gives the path the
    hand actually made, whether or not the cursor was locked.
    """

    def __init__(self) -> None:
        self.position: tuple[float, float] | None = None

    def press(self, where: tuple[float, float]) -> None:
        self.position = (float(where[0]), float(where[1]))

    def move_by(self, dx: float, dy: float, fallback: tuple[float, float]) -> tuple[int, int]:
        base = self.position if self.position is not None else fallback
        self.position = (base[0] + dx, base[1] + dy)
        return (int(round(self.position[0])), int(round(self.position[1])))

    def release(self) -> None:
        self.position = None
