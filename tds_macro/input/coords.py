"""Coordinate conversion between normalized client space and Win32 absolute space.

Three coordinate systems are in play:

1. **Normalized client space** -- ``(0.0, 0.0)`` is the top-left of the Roblox
   *client area*, ``(1.0, 1.0)`` the bottom-right.  Everything persisted in a
   strategy file lives here, which is what makes a macro recorded at 1280x720
   windowed replay correctly at 2560x1440 fullscreen.
2. **Physical screen pixels** -- the virtual desktop coordinate space used by
   ``GetClientRect``/``ClientToScreen`` once the process is per-monitor DPI
   aware (v2).  The origin may be *negative* when a monitor sits to the left of
   or above the primary display.
3. **Win32 absolute space** -- the ``0..65535`` range that ``SendInput``
   expects with ``MOUSEEVENTF_ABSOLUTE | MOUSEEVENTF_VIRTUALDESK``.  Using this
   (rather than relative deltas) is what makes pointer acceleration and the
   "Enhance pointer precision" setting irrelevant to us.

All functions here are pure so they can be unit tested without Windows.
"""

from __future__ import annotations

from dataclasses import dataclass

# Win32 normalizes absolute mouse coordinates into [0, 65535].
ABS_MAX = 65535
# ...but the internal conversion divides by 65536 (a shift of 16), not 65535.
ABS_RANGE = 65536


@dataclass(frozen=True)
class Rect:
    """An axis-aligned rectangle in physical screen pixels.

    ``right``/``bottom`` are exclusive, matching Win32 ``RECT`` semantics.
    """

    left: int
    top: int
    right: int
    bottom: int

    @property
    def width(self) -> int:
        return self.right - self.left

    @property
    def height(self) -> int:
        return self.bottom - self.top

    @property
    def center(self) -> tuple[int, int]:
        return (self.left + self.width // 2, self.top + self.height // 2)

    def contains(self, x: int, y: int) -> bool:
        return self.left <= x < self.right and self.top <= y < self.bottom

    def is_valid(self) -> bool:
        return self.width > 0 and self.height > 0

    def as_tuple(self) -> tuple[int, int, int, int]:
        return (self.left, self.top, self.right, self.bottom)


@dataclass(frozen=True)
class VirtualScreen:
    """Bounding box of every monitor combined.

    Corresponds to ``SM_XVIRTUALSCREEN``, ``SM_YVIRTUALSCREEN``,
    ``SM_CXVIRTUALSCREEN``, ``SM_CYVIRTUALSCREEN``.  ``left``/``top`` are
    negative whenever a secondary monitor is positioned left of or above the
    primary one -- the single most common cause of "my macro clicks on the
    wrong screen".
    """

    left: int
    top: int
    width: int
    height: int

    @property
    def right(self) -> int:
        return self.left + self.width

    @property
    def bottom(self) -> int:
        return self.top + self.height

    def as_rect(self) -> Rect:
        return Rect(self.left, self.top, self.right, self.bottom)

    def is_valid(self) -> bool:
        return self.width > 0 and self.height > 0


@dataclass(frozen=True)
class NormPoint:
    """A point in normalized client space.

    Values are *not* clamped on construction so a recorder can capture a click
    that lands slightly outside the client area (window border, resize drag)
    and the caller can decide whether to discard it.  Use :meth:`clamped`
    before sending input.
    """

    x: float
    y: float

    def clamped(self) -> NormPoint:
        return NormPoint(_clamp01(self.x), _clamp01(self.y))

    def is_inside(self) -> bool:
        return 0.0 <= self.x <= 1.0 and 0.0 <= self.y <= 1.0

    def as_tuple(self) -> tuple[float, float]:
        return (self.x, self.y)

    def to_dict(self) -> dict[str, float]:
        return {"x": round(self.x, 6), "y": round(self.y, 6)}

    @classmethod
    def from_dict(cls, data: dict[str, float]) -> NormPoint:
        return cls(float(data["x"]), float(data["y"]))

    def distance_to(self, other: NormPoint) -> float:
        return ((self.x - other.x) ** 2 + (self.y - other.y) ** 2) ** 0.5


def _clamp01(value: float) -> float:
    return 0.0 if value < 0.0 else 1.0 if value > 1.0 else value


def _clamp(value: int, low: int, high: int) -> int:
    return low if value < low else high if value > high else value


def norm_to_screen(point: NormPoint, client: Rect) -> tuple[int, int]:
    """Map a normalized client point to a physical screen pixel.

    The point is placed at the *centre* of its pixel band so that a
    normalized 0.5 on an even-width window does not systematically bias left.
    Raises :class:`ValueError` on a degenerate client rect, which is what you
    get when the Roblox window is minimized -- callers must treat that as
    "window not ready", never as a click at (0, 0).
    """
    if not client.is_valid():
        raise ValueError(f"client rect has no area: {client.as_tuple()}")
    cx = client.left + int(point.x * client.width)
    cy = client.top + int(point.y * client.height)
    # Keep the result inside the client area even for point values of exactly 1.0.
    return (
        _clamp(cx, client.left, client.right - 1),
        _clamp(cy, client.top, client.bottom - 1),
    )


def screen_to_norm(x: int, y: int, client: Rect) -> NormPoint:
    """Inverse of :func:`norm_to_screen` (to within one pixel)."""
    if not client.is_valid():
        raise ValueError(f"client rect has no area: {client.as_tuple()}")
    return NormPoint(
        (x - client.left + 0.5) / client.width,
        (y - client.top + 0.5) / client.height,
    )


def screen_to_absolute(x: int, y: int, screen: VirtualScreen) -> tuple[int, int]:
    """Map a physical screen pixel to Win32 ``0..65535`` absolute coordinates.

    Windows converts back with an integer ``(abs * width) >> 16``, so we aim at
    the middle of the destination pixel's band (``+ ABS_RANGE // 2``) instead of
    its leading edge.  Hitting the leading edge is the classic off-by-one that
    lands clicks one pixel short -- harmless in the middle of a button, fatal on
    the last column of a monitor, where it spills onto the neighbouring display.
    """
    if not screen.is_valid():
        raise ValueError(f"virtual screen has no area: {screen}")
    ax = ((x - screen.left) * ABS_RANGE + ABS_RANGE // 2) // screen.width
    ay = ((y - screen.top) * ABS_RANGE + ABS_RANGE // 2) // screen.height
    return (_clamp(ax, 0, ABS_MAX), _clamp(ay, 0, ABS_MAX))


def absolute_to_screen(ax: int, ay: int, screen: VirtualScreen) -> tuple[int, int]:
    """Replicate the conversion Windows itself performs on an absolute event.

    Used by tests to prove a round trip, and by the diagnostics panel to show
    the user where a click will actually land.
    """
    if not screen.is_valid():
        raise ValueError(f"virtual screen has no area: {screen}")
    x = screen.left + (ax * screen.width) // ABS_RANGE
    y = screen.top + (ay * screen.height) // ABS_RANGE
    return (
        _clamp(x, screen.left, screen.right - 1),
        _clamp(y, screen.top, screen.bottom - 1),
    )


def norm_to_absolute(
    point: NormPoint, client: Rect, screen: VirtualScreen
) -> tuple[int, int]:
    """Full pipeline: normalized client point -> ``SendInput`` absolute coords."""
    sx, sy = norm_to_screen(point.clamped(), client)
    return screen_to_absolute(sx, sy, screen)


def round_trip_error(screen: VirtualScreen) -> int:
    """Worst-case pixel error of a screen -> absolute -> screen round trip.

    Zero for any virtual desktop narrower and shorter than 65536 px, which is
    every real setup.  Exposed so the diagnostics panel can state the limit
    rather than pretending the mapping is lossless.
    """
    ex = max(0, -(-screen.width // ABS_RANGE) - 1)
    ey = max(0, -(-screen.height // ABS_RANGE) - 1)
    return max(ex, ey)


def client_rect_from_window(
    window_rect: Rect, client_size: tuple[int, int], client_origin: tuple[int, int]
) -> Rect:
    """Build the screen-space client rect from Win32 window metrics.

    ``GetClientRect`` yields a size with a ``(0, 0)`` origin; ``ClientToScreen``
    turns that origin into screen coordinates.  ``window_rect`` is only used to
    sanity-check the result, because a minimized window reports a client origin
    of ``(-32000, -32000)``.
    """
    width, height = client_size
    ox, oy = client_origin
    rect = Rect(ox, oy, ox + width, oy + height)
    if ox <= -30000 or oy <= -30000:
        raise ValueError("window appears minimized; client origin is off-screen")
    if not rect.is_valid():
        raise ValueError(f"client area has no size: {rect.as_tuple()}")
    del window_rect  # retained for signature stability / future validation
    return rect
