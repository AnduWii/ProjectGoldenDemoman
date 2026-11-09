"""Finding the Roblox window on macOS and measuring it.

Read-only, like the Windows side: the window server's public window list
(owner app, bounds, on-screen state) and the frontmost application. No access
to Roblox's process.

Coordinates are global *points* with the origin at the top-left of the main
display, the same space ``CGEventPost`` takes and ``mss`` captures in. On a
Retina screen a capture comes back at twice that resolution, which the vision
layer absorbs because everything it stores is normalized.

**The title bar.** The window list reports the whole window, title bar
included, and macOS offers no way to ask another app for its content area. Left
uncorrected, normalized positions would shift with window height, so a point
picked in a small window would land a little off in a large one. The title bar
is a fixed height in points on a given macOS version (28 since Big Sur, 22
before), and absent entirely in native full screen, so the content area is the
window minus that strip unless the window exactly fills a display.
"""

from __future__ import annotations

import logging
import platform
import time
from collections.abc import Mapping
from typing import Any

from .coords import Rect, VirtualScreen
from .mac_quartz import APPKIT, QUARTZ
from .window import WindowInfo, choose_best_candidate

log = logging.getLogger(__name__)

TITLE_BAR_POINTS_MODERN = 28  # macOS 11 Big Sur and later
TITLE_BAR_POINTS_LEGACY = 22  # macOS 10.x
FOREGROUND_CACHE_S = 0.05

# The window-list dictionary keys. pyobjc exposes them as constants whose
# values are exactly these strings, so pure code can use the literals.
KEY_NUMBER = "kCGWindowNumber"
KEY_OWNER = "kCGWindowOwnerName"
KEY_PID = "kCGWindowOwnerPID"
KEY_BOUNDS = "kCGWindowBounds"
KEY_LAYER = "kCGWindowLayer"
KEY_NAME = "kCGWindowName"
KEY_ONSCREEN = "kCGWindowIsOnscreen"


def title_bar_points(macos_release: str | None = None) -> int:
    """Title bar height in points for a macOS version string like '14.5'."""
    release = macos_release if macos_release is not None else platform.mac_ver()[0]
    try:
        major = int(release.split(".")[0])
    except (ValueError, IndexError):
        return TITLE_BAR_POINTS_MODERN
    return TITLE_BAR_POINTS_MODERN if major >= 11 else TITLE_BAR_POINTS_LEGACY


def rect_from_bounds(bounds: Mapping[str, Any]) -> Rect:
    """A window-list bounds dictionary ({X, Y, Width, Height}) as a Rect."""
    left = int(round(float(bounds["X"])))
    top = int(round(float(bounds["Y"])))
    return Rect(
        left,
        top,
        left + int(round(float(bounds["Width"]))),
        top + int(round(float(bounds["Height"]))),
    )


def content_rect(window: Rect, displays: list[Rect], title_bar: int) -> Rect:
    """The game's drawable area inside a window's frame.

    A window that exactly covers a display is in native full screen and has no
    title bar. Anything else has one along the top.
    """
    if any(window == display for display in displays):
        return window
    if window.height <= title_bar:
        return window
    return Rect(window.left, window.top + title_bar, window.right, window.bottom)


def union_screen(displays: list[Rect]) -> VirtualScreen:
    """The bounding box of every display, in global points."""
    if not displays:
        return VirtualScreen(0, 0, 0, 0)
    left = min(d.left for d in displays)
    top = min(d.top for d in displays)
    right = max(d.right for d in displays)
    bottom = max(d.bottom for d in displays)
    return VirtualScreen(left, top, right - left, bottom - top)


def window_info_from_entry(entry: Mapping[str, Any]) -> WindowInfo | None:
    """Turn one window-list dictionary into a WindowInfo, or None to skip it.

    Only ordinary windows (layer 0) qualify: the menu bar, the Dock and
    floating panels live on other layers and are never the game.
    """
    if int(entry.get(KEY_LAYER, 0) or 0) != 0:
        return None
    bounds = entry.get(KEY_BOUNDS)
    if not bounds:
        return None
    try:
        rect = rect_from_bounds(bounds)
    except (KeyError, TypeError, ValueError):
        return None
    return WindowInfo(
        hwnd=int(entry.get(KEY_NUMBER, 0) or 0),
        # The title is only readable with Screen Recording permission; the
        # owner name is always available and is what identifies Roblox.
        title=str(entry.get(KEY_NAME, "") or ""),
        class_name="",
        pid=int(entry.get(KEY_PID, 0) or 0),
        exe_name=str(entry.get(KEY_OWNER, "") or ""),
        rect=rect,
        visible=bool(entry.get(KEY_ONSCREEN, True)),
    )


# --- live queries (macOS only) ----------------------------------------------------
def _window_list(option: int, window_id: int = 0) -> list[Any]:  # pragma: no cover
    if QUARTZ is None:
        return []
    entries = QUARTZ.CGWindowListCopyWindowInfo(option, window_id)
    return list(entries or [])


def enumerate_mac_windows() -> list[WindowInfo]:  # pragma: no cover - macOS only
    """Every ordinary on-screen window, front to back."""
    if QUARTZ is None:
        return []
    option = QUARTZ.kCGWindowListOptionOnScreenOnly | QUARTZ.kCGWindowListExcludeDesktopElements
    infos = [window_info_from_entry(entry) for entry in _window_list(option)]
    return [info for info in infos if info is not None]


def window_entry(window_id: int) -> Any:  # pragma: no cover - macOS only
    """The window-list dictionary for one window, or None if it is gone."""
    if QUARTZ is None or not window_id:
        return None
    entries = _window_list(QUARTZ.kCGWindowListOptionIncludingWindow, window_id)
    for entry in entries:
        if int(entry.get(KEY_NUMBER, 0) or 0) == window_id:
            return entry
    return None


def display_rects() -> list[Rect]:  # pragma: no cover - macOS only
    """Every active display's bounds in global points."""
    if QUARTZ is None:
        return []
    try:
        error, ids, count = QUARTZ.CGGetActiveDisplayList(16, None, None)
        displays = list(ids or [])[: int(count)] if not error else []
    except Exception:
        displays = []
    if not displays:
        displays = [QUARTZ.CGMainDisplayID()]
    rects = []
    for display in displays:
        bounds = QUARTZ.CGDisplayBounds(display)
        left, top = int(bounds.origin.x), int(bounds.origin.y)
        rects.append(
            Rect(left, top, left + int(bounds.size.width), top + int(bounds.size.height))
        )
    return rects


def frontmost_pid() -> int:  # pragma: no cover - macOS only
    """Process id of the active application, or 0 if unknown."""
    if APPKIT is not None:
        try:
            app = APPKIT.NSWorkspace.sharedWorkspace().frontmostApplication()
            if app is not None:
                return int(app.processIdentifier())
        except Exception:
            log.debug("NSWorkspace lookup failed", exc_info=True)
    # Fallback: the window list is ordered front to back.
    for info in enumerate_mac_windows():
        return info.pid
    return 0


class MacRobloxWindow:
    """Live handle on the Roblox window on macOS. Same interface as RobloxWindow."""

    def __init__(self) -> None:
        self._window_id: int | None = None
        self._pid = 0
        self._pinned = False
        self._last_error = ""
        self._front_checked = 0.0
        self._front = False
        self._title_bar = title_bar_points()

    @property
    def hwnd(self) -> int | None:
        return self._window_id

    @property
    def last_error(self) -> str:
        return self._last_error

    def pin(self, hwnd: int) -> None:  # pragma: no cover - macOS only
        entry = window_entry(hwnd)
        self._window_id = hwnd
        self._pid = int(entry.get(KEY_PID, 0) or 0) if entry is not None else 0
        self._pinned = True

    def unpin(self) -> None:
        self._pinned = False

    def refresh(self) -> bool:  # pragma: no cover - macOS only
        if QUARTZ is None:
            self._last_error = "macOS window APIs are unavailable (pyobjc is not installed)"
            return False
        if self._pinned and self._window_id:
            entry = window_entry(self._window_id)
            if entry is not None and int(entry.get(KEY_PID, 0) or 0) == self._pid:
                return True
            log.info("pinned window %s is gone; searching for Roblox again", self._window_id)
            self.unpin()
        match = choose_best_candidate(enumerate_mac_windows())
        if match is None:
            self._window_id = None
            self._pid = 0
            self._last_error = (
                "Roblox window not found. Is Roblox open, visible on this desktop, and not "
                "minimized?"
            )
            return False
        self._window_id = match.window.hwnd
        self._pid = match.window.pid
        self._last_error = ""
        log.info("using window %s (%s)", match.window.describe(), ", ".join(match.reasons))
        return True

    def client_rect(self) -> Rect | None:  # pragma: no cover - macOS only
        if QUARTZ is None or not self._window_id:
            return None
        entry = window_entry(self._window_id)
        if entry is None:
            self._last_error = "the Roblox window has closed"
            return None
        if not bool(entry.get(KEY_ONSCREEN, False)):
            self._last_error = (
                "the Roblox window is not visible -- minimized, or full screen on another desktop"
            )
            return None
        try:
            window = rect_from_bounds(entry[KEY_BOUNDS])
        except (KeyError, TypeError, ValueError):
            self._last_error = "could not read the Roblox window's position"
            return None
        rect = content_rect(window, display_rects(), self._title_bar)
        return rect if rect.is_valid() else None

    def virtual_screen(self) -> VirtualScreen:  # pragma: no cover - macOS only
        return union_screen(display_rects())

    def is_foreground(self) -> bool:  # pragma: no cover - macOS only
        """True when Roblox is the active app. Cached briefly: the engine asks
        every tick, and the answer cannot change faster than a person can
        switch windows."""
        if not self._pid:
            return False
        now = time.monotonic()
        if now - self._front_checked > FOREGROUND_CACHE_S:
            self._front = frontmost_pid() == self._pid
            self._front_checked = now
        return self._front

    def describe(self) -> str:  # pragma: no cover - macOS only
        if not self._window_id:
            return self._last_error or "no window"
        rect = self.client_rect()
        if rect is None:
            return f"window {self._window_id} ({self._last_error or 'geometry unavailable'})"
        return (
            f"window {self._window_id} content {rect.width}x{rect.height} pt "
            f"at ({rect.left},{rect.top})"
        )
