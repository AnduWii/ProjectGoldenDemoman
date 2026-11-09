"""Finding the Roblox window and reading its geometry.

Everything in here is read-only observation of *window* state through
documented Win32 APIs: enumerate top-level windows, read their title, class and
owning executable name, read their rectangle, ask which one is in the
foreground.  No process memory is read, nothing is injected, no code runs
inside Roblox.

The selection heuristics are separated from the Win32 calls so they can be
tested on any platform, and so the first-run wizard can fall back to "pick the
window yourself from this list" when a Roblox update changes the class name.
"""

from __future__ import annotations

import ctypes
import logging
from dataclasses import dataclass, field
from typing import Protocol

from .coords import Rect, VirtualScreen, client_rect_from_window
from .win32_defs import (
    DPI_AWARENESS_CONTEXT_PER_MONITOR_AWARE_V2,
    IS_WINDOWS,
    POINT,
    PROCESS_PER_MONITOR_DPI_AWARE,
    RECT,
    SM_CXVIRTUALSCREEN,
    SM_CYVIRTUALSCREEN,
    SM_XVIRTUALSCREEN,
    SM_YVIRTUALSCREEN,
    kernel32,
    shcore,
    user32,
)

log = logging.getLogger(__name__)

# Known-good identifiers for the Roblox desktop client, most specific first.
ROBLOX_EXE_NAMES: tuple[str, ...] = (
    "robloxplayerbeta.exe",
    "windows10universal.exe",  # Microsoft Store build
)
ROBLOX_CLASS_NAMES: tuple[str, ...] = ("windowsclient", "roblox")
ROBLOX_TITLE_HINT = "roblox"

PROCESS_QUERY_LIMITED_INFORMATION = 0x1000

# DPI awareness can only be set once per process; later calls fail. The result
# is cached so that a second caller is told what the process actually is, rather
# than that setting it "failed" -- which is what the About dialog and the
# troubleshooting steps read.
_dpi_mode: str | None = None

DPI_AWARENESS_UNAWARE = 0
DPI_AWARENESS_SYSTEM_AWARE = 1
DPI_AWARENESS_PER_MONITOR_AWARE = 2

_AWARENESS_NAMES = {
    DPI_AWARENESS_UNAWARE: "DPI unaware",
    DPI_AWARENESS_SYSTEM_AWARE: "system DPI aware",
    DPI_AWARENESS_PER_MONITOR_AWARE: "per-monitor DPI aware",
}


@dataclass(frozen=True)
class WindowInfo:
    """A top-level window as seen from outside."""

    hwnd: int
    title: str
    class_name: str
    pid: int
    exe_name: str
    rect: Rect
    visible: bool = True

    def describe(self) -> str:
        size = f"{self.rect.width}x{self.rect.height}"
        return f"{self.title or '(untitled)'} [{self.exe_name or self.class_name}] {size}"


@dataclass
class WindowMatch:
    """A scored candidate from :func:`score_candidates`."""

    window: WindowInfo
    score: int
    reasons: list[str] = field(default_factory=list)


def score_candidate(window: WindowInfo) -> WindowMatch:
    """Score how likely a window is to be the Roblox game client.

    Pure function -- the scoring rules are the part most likely to need
    adjusting after a Roblox update, so they are kept testable and visible.
    """
    match = WindowMatch(window=window, score=0)
    exe = window.exe_name.lower()
    cls = window.class_name.lower()
    title = window.title.lower()

    if exe in ROBLOX_EXE_NAMES:
        match.score += 100
        match.reasons.append(f"executable {window.exe_name}")
    if cls in ROBLOX_CLASS_NAMES:
        match.score += 50
        match.reasons.append(f"window class {window.class_name}")
    if ROBLOX_TITLE_HINT in title:
        match.score += 20
        match.reasons.append("title mentions Roblox")
    if not window.visible:
        match.score -= 200
        match.reasons.append("window is not visible")
    # The launcher and small utility windows are never the game viewport.
    if window.rect.width < 320 or window.rect.height < 240:
        match.score -= 60
        match.reasons.append("window too small to be the game view")
    return match


def choose_best_candidate(windows: list[WindowInfo], minimum_score: int = 40) -> WindowMatch | None:
    """Pick the most likely Roblox window, or ``None`` if nothing is convincing.

    Ties break toward the larger window, which is the game view rather than a
    stray dialog owned by the same process.
    """
    scored = [score_candidate(w) for w in windows]
    viable = [m for m in scored if m.score >= minimum_score]
    if not viable:
        return None
    viable.sort(key=lambda m: (m.score, m.window.rect.width * m.window.rect.height), reverse=True)
    return viable[0]


# --- Win32-backed implementation ---------------------------------------------
def query_dpi_awareness() -> str:
    """Describe the awareness the process actually has, as Windows sees it.

    Used to report the truth rather than the return value of a setter that
    legitimately fails the second time it is called.
    """
    if not IS_WINDOWS or user32 is None:
        return "not Windows; DPI awareness not applicable"
    try:
        if hasattr(user32, "GetThreadDpiAwarenessContext") and hasattr(
            user32, "GetAwarenessFromDpiAwarenessContext"
        ):
            user32.GetThreadDpiAwarenessContext.restype = ctypes.c_void_p
            user32.GetAwarenessFromDpiAwarenessContext.argtypes = (ctypes.c_void_p,)
            user32.GetAwarenessFromDpiAwarenessContext.restype = ctypes.c_int
            context = user32.GetThreadDpiAwarenessContext()
            awareness = int(user32.GetAwarenessFromDpiAwarenessContext(context))
            return _AWARENESS_NAMES.get(awareness, f"unknown DPI awareness ({awareness})")
    except OSError as exc:  # pragma: no cover - Windows only
        log.warning("could not read DPI awareness: %s", exc)
    return "DPI awareness unknown"


def set_dpi_awareness() -> str:
    """Opt into per-monitor DPI awareness v2 as early as possible.

    Must run **before** Qt is imported/instantiated, otherwise Qt sets its own
    (lower) awareness first and every later ``GetClientRect`` comes back in
    scaled logical pixels while ``mss`` captures physical ones -- a mismatch
    that silently offsets every click on any display not at 100% scaling.

    Returns a human-readable description of what the process ended up with.
    Awareness can only be set once, so the answer is cached: without that, a
    second caller sees the setter fail and reports "could not be set" for a
    process that is in fact correctly per-monitor aware.
    """
    global _dpi_mode
    if _dpi_mode is not None:
        return _dpi_mode
    if not IS_WINDOWS:
        _dpi_mode = "not Windows; DPI awareness not applicable"
        return _dpi_mode
    try:
        assert user32 is not None
        if hasattr(user32, "SetProcessDpiAwarenessContext"):
            user32.SetProcessDpiAwarenessContext.argtypes = (ctypes.c_void_p,)
            if user32.SetProcessDpiAwarenessContext(
                DPI_AWARENESS_CONTEXT_PER_MONITOR_AWARE_V2
            ):
                _dpi_mode = "per-monitor DPI aware v2"
                return _dpi_mode
        if (
            shcore is not None
            and hasattr(shcore, "SetProcessDpiAwareness")
            and shcore.SetProcessDpiAwareness(PROCESS_PER_MONITOR_DPI_AWARE) == 0
        ):
            _dpi_mode = "per-monitor DPI aware v1 (v2 unavailable)"
            return _dpi_mode
        if hasattr(user32, "SetProcessDPIAware") and user32.SetProcessDPIAware():
            _dpi_mode = "system DPI aware only (per-monitor unavailable)"
            return _dpi_mode
    except OSError as exc:  # pragma: no cover - Windows only
        log.warning("could not set DPI awareness: %s", exc)
        _dpi_mode = f"DPI awareness failed: {exc}"
        return _dpi_mode
    # Setting failed, which usually means it was already set (by an app manifest
    # or an earlier call), so report what the process actually has.
    _dpi_mode = f"already set: {query_dpi_awareness()}"
    return _dpi_mode


def _process_exe_name(pid: int) -> str:  # pragma: no cover - Windows only
    if not IS_WINDOWS or kernel32 is None:
        return ""
    handle = kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
    if not handle:
        return ""
    try:
        buffer = ctypes.create_unicode_buffer(1024)
        size = ctypes.c_uint32(len(buffer))
        if kernel32.QueryFullProcessImageNameW(handle, 0, buffer, ctypes.byref(size)):
            return buffer.value.rsplit("\\", 1)[-1]
    finally:
        kernel32.CloseHandle(handle)
    return ""


def window_pid(hwnd: int) -> int:  # pragma: no cover - Windows only
    """Process id owning ``hwnd``, or 0 if it cannot be read."""
    if not IS_WINDOWS or user32 is None:
        return 0
    pid = ctypes.c_uint32()
    user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
    return int(pid.value)


def enumerate_windows(visible_only: bool = True) -> list[WindowInfo]:  # pragma: no cover
    """List top-level windows, for the wizard's manual picker."""
    if not IS_WINDOWS or user32 is None:
        return []
    results: list[WindowInfo] = []

    @ctypes.WINFUNCTYPE(ctypes.c_bool, ctypes.c_void_p, ctypes.c_void_p)
    def callback(hwnd: int, _lparam: int) -> bool:
        visible = bool(user32.IsWindowVisible(hwnd))
        if visible_only and not visible:
            return True
        length = user32.GetWindowTextLengthW(hwnd)
        title_buf = ctypes.create_unicode_buffer(length + 2)
        user32.GetWindowTextW(hwnd, title_buf, length + 2)
        class_buf = ctypes.create_unicode_buffer(256)
        user32.GetClassNameW(hwnd, class_buf, 256)
        rect = RECT()
        user32.GetWindowRect(hwnd, ctypes.byref(rect))
        pid = ctypes.c_uint32()
        user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
        results.append(
            WindowInfo(
                hwnd=int(hwnd),
                title=title_buf.value,
                class_name=class_buf.value,
                pid=int(pid.value),
                exe_name=_process_exe_name(int(pid.value)),
                rect=Rect(rect.left, rect.top, rect.right, rect.bottom),
                visible=visible,
            )
        )
        return True

    user32.EnumWindows(callback, 0)
    return results


class GeometryProvider(Protocol):
    """What the input backend and the vision layer need from the target window."""

    def client_rect(self) -> Rect | None: ...

    def virtual_screen(self) -> VirtualScreen: ...

    def is_foreground(self) -> bool: ...

    def refresh(self) -> bool: ...

    def describe(self) -> str: ...


class RobloxWindow:
    """Live handle on the Roblox window, re-resolved on demand.

    ``refresh()`` is called before every run (and whenever a lookup fails) so
    that closing and reopening Roblox, or moving it to another monitor, does not
    require restarting the app.
    """

    def __init__(self) -> None:
        self._hwnd: int | None = None
        self._pinned = False
        self._pinned_pid = 0
        self._last_error: str = ""

    @property
    def hwnd(self) -> int | None:
        return self._hwnd

    @property
    def last_error(self) -> str:
        return self._last_error

    def pin(self, hwnd: int) -> None:
        """Lock onto a window the user chose manually in the wizard.

        The owning process id is recorded with it. Windows recycles HWND
        values, so a handle alone can come to name a different window once the
        original closes; the pid check in :meth:`refresh` catches that instead
        of sending input to whatever window inherited the number.
        """
        self._hwnd = hwnd
        self._pinned = True
        self._pinned_pid = window_pid(hwnd)

    def unpin(self) -> None:
        self._pinned = False
        self._pinned_pid = 0

    def refresh(self) -> bool:  # pragma: no cover - Windows only
        """Re-find the window.  Returns ``True`` when a usable handle is held."""
        if not IS_WINDOWS or user32 is None:
            self._last_error = "not running on Windows"
            return False
        if self._pinned and self._hwnd:
            if user32.IsWindow(self._hwnd) and window_pid(self._hwnd) == self._pinned_pid:
                return True
            log.info("pinned window %s is gone; searching for Roblox again", self._hwnd)
            self.unpin()
        match = choose_best_candidate(enumerate_windows())
        if match is None:
            self._hwnd = None
            self._last_error = (
                "Roblox window not found. Is Roblox running and out of fullscreen-exclusive mode?"
            )
            return False
        self._hwnd = match.window.hwnd
        self._last_error = ""
        log.info("using window %s (%s)", match.window.describe(), ", ".join(match.reasons))
        return True

    def is_minimized(self) -> bool:  # pragma: no cover - Windows only
        if not IS_WINDOWS or user32 is None or not self._hwnd:
            return True
        return bool(user32.IsIconic(self._hwnd))

    def client_rect(self) -> Rect | None:  # pragma: no cover - Windows only
        """Client area in physical screen pixels, or ``None`` if unavailable."""
        if not IS_WINDOWS or user32 is None or not self._hwnd:
            return None
        if self.is_minimized():
            self._last_error = "Roblox window is minimized"
            return None
        rect = RECT()
        if not user32.GetClientRect(self._hwnd, ctypes.byref(rect)):
            self._last_error = "GetClientRect failed; window may have closed"
            return None
        origin = POINT(0, 0)
        if not user32.ClientToScreen(self._hwnd, ctypes.byref(origin)):
            self._last_error = "ClientToScreen failed"
            return None
        window = RECT()
        user32.GetWindowRect(self._hwnd, ctypes.byref(window))
        try:
            return client_rect_from_window(
                Rect(window.left, window.top, window.right, window.bottom),
                (rect.right - rect.left, rect.bottom - rect.top),
                (origin.x, origin.y),
            )
        except ValueError as exc:
            self._last_error = str(exc)
            return None

    def virtual_screen(self) -> VirtualScreen:  # pragma: no cover - Windows only
        if not IS_WINDOWS or user32 is None:
            return VirtualScreen(0, 0, 1920, 1080)
        return VirtualScreen(
            user32.GetSystemMetrics(SM_XVIRTUALSCREEN),
            user32.GetSystemMetrics(SM_YVIRTUALSCREEN),
            user32.GetSystemMetrics(SM_CXVIRTUALSCREEN),
            user32.GetSystemMetrics(SM_CYVIRTUALSCREEN),
        )

    def is_foreground(self) -> bool:  # pragma: no cover - Windows only
        """True only when Roblox is the active window.

        Every input batch is gated on this, so that a macro can never type into
        the user's browser or Discord if they alt-tab mid-run.
        """
        if not IS_WINDOWS or user32 is None or not self._hwnd:
            return False
        return int(user32.GetForegroundWindow() or 0) == self._hwnd

    def describe(self) -> str:  # pragma: no cover - Windows only
        if not self._hwnd:
            return self._last_error or "no window"
        rect = self.client_rect()
        if rect is None:
            return f"hwnd {self._hwnd} ({self._last_error or 'geometry unavailable'})"
        return f"hwnd {self._hwnd} client {rect.width}x{rect.height} at ({rect.left},{rect.top})"
