"""macOS system frameworks: loading, permissions, and the event-tap thread.

Everything Apple-specific funnels through here. The frameworks come from
pyobjc, which only installs on macOS, so each is loaded defensively and is
``None`` elsewhere -- the module still imports (and type-checks) on Windows and
Linux, which is what keeps the test suite cross-platform.

**Permissions.** macOS will not let any app post synthetic input, read other
apps' windows, or observe the keyboard until the user switches it on in
System Settings -> Privacy & Security. There is no way around that and no
setting this app changes on the user's behalf: it can only *check* the three
switches and open the right pane so the user can flip them.

* Accessibility -- needed to send clicks and key presses (CGEventPost
  silently drops them otherwise).
* Screen Recording -- needed to see the Roblox window; without it a capture
  shows only the desktop wallpaper.
* Input Monitoring -- needed for the global hotkeys and for recording.

Permission is granted to the *app the user launched*: TDS Macro.app for the
packaged build, or Terminal when running from source.
"""

from __future__ import annotations

import ctypes
import logging
import sys
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

log = logging.getLogger(__name__)

IS_MAC = sys.platform == "darwin"

# Static imports so PyInstaller's analysis finds and bundles them; each is Any
# so the rest of the module type-checks on platforms without pyobjc.
try:
    import Quartz as _Quartz

    QUARTZ: Any = _Quartz
except ImportError:
    QUARTZ = None
try:
    import AppKit as _AppKit

    APPKIT: Any = _AppKit
except ImportError:
    APPKIT = None
try:
    import ApplicationServices as _ApplicationServices

    APPSERVICES: Any = _ApplicationServices
except ImportError:
    APPSERVICES = None

# Tags every event this app posts, so our own event taps can tell our
# synthetic input apart from the user's -- otherwise recording a replay would
# record the replay. Same value as the Windows dwExtraInfo signature.
TDS_MACRO_SIGNATURE = 0x7D5AC20

IOKIT_PATH = "/System/Library/Frameworks/IOKit.framework/IOKit"
IOHID_REQUEST_LISTEN_EVENT = 1  # kIOHIDRequestTypeListenEvent
IOHID_ACCESS_GRANTED = 0  # kIOHIDAccessTypeGranted
IOHID_ACCESS_DENIED = 1  # kIOHIDAccessTypeDenied

SETTINGS_URLS = {
    "accessibility": (
        "x-apple.systempreferences:com.apple.preference.security?Privacy_Accessibility"
    ),
    "screen_recording": (
        "x-apple.systempreferences:com.apple.preference.security?Privacy_ScreenCapture"
    ),
    "input_monitoring": (
        "x-apple.systempreferences:com.apple.preference.security?Privacy_ListenEvent"
    ),
}


def apis_available() -> bool:
    """True when the macOS frameworks loaded -- i.e. pyobjc is installed."""
    return QUARTZ is not None


def missing_apis() -> list[str]:
    missing = []
    if QUARTZ is None:
        missing.append("Quartz")
    if APPKIT is None:
        missing.append("AppKit")
    if APPSERVICES is None:
        missing.append("ApplicationServices")
    return missing


# --- permissions ---------------------------------------------------------------
@dataclass(frozen=True)
class Permission:
    """One Privacy & Security switch and whether it is on."""

    key: str
    title: str
    why: str
    granted: bool | None  # None = could not be determined
    settings_url: str
    needs_restart: bool

    def describe(self) -> str:
        state = {True: "allowed", False: "NOT allowed", None: "unknown"}[self.granted]
        return f"{self.title}: {state}"


def accessibility_granted(prompt: bool = False) -> bool | None:
    """Whether this app may post synthetic input.

    ``prompt=True`` asks macOS to show its own dialog, which offers to open
    the Accessibility pane. It changes nothing by itself; the user flips the
    switch.
    """
    if APPSERVICES is None:
        return None
    try:
        if prompt and hasattr(APPSERVICES, "AXIsProcessTrustedWithOptions"):
            options = {APPSERVICES.kAXTrustedCheckOptionPrompt: True}
            return bool(APPSERVICES.AXIsProcessTrustedWithOptions(options))
        return bool(APPSERVICES.AXIsProcessTrusted())
    except Exception as exc:  # pragma: no cover - macOS only
        log.warning("could not read the Accessibility permission: %s", exc)
        return None


def screen_recording_granted(prompt: bool = False) -> bool | None:
    """Whether this app may capture other apps' windows (macOS 10.15+)."""
    if QUARTZ is None:
        return None
    try:
        if prompt and hasattr(QUARTZ, "CGRequestScreenCaptureAccess"):
            return bool(QUARTZ.CGRequestScreenCaptureAccess())
        if hasattr(QUARTZ, "CGPreflightScreenCaptureAccess"):
            return bool(QUARTZ.CGPreflightScreenCaptureAccess())
    except Exception as exc:  # pragma: no cover - macOS only
        log.warning("could not read the Screen Recording permission: %s", exc)
    return None


def _iokit() -> Any:
    if not IS_MAC:
        return None
    try:
        lib = ctypes.cdll.LoadLibrary(IOKIT_PATH)
        lib.IOHIDCheckAccess.argtypes = (ctypes.c_uint32,)
        lib.IOHIDCheckAccess.restype = ctypes.c_uint32
        lib.IOHIDRequestAccess.argtypes = (ctypes.c_uint32,)
        lib.IOHIDRequestAccess.restype = ctypes.c_bool
        return lib
    except (OSError, AttributeError):  # pragma: no cover - pre-10.15 macOS
        return None


def input_monitoring_granted(prompt: bool = False) -> bool | None:
    """Whether this app may observe keyboard and mouse events (10.15+)."""
    lib = _iokit()
    if lib is None:
        return None
    try:
        if prompt:
            return bool(lib.IOHIDRequestAccess(IOHID_REQUEST_LISTEN_EVENT))
        state = int(lib.IOHIDCheckAccess(IOHID_REQUEST_LISTEN_EVENT))
    except Exception as exc:  # pragma: no cover - macOS only
        log.warning("could not read the Input Monitoring permission: %s", exc)
        return None
    if state == IOHID_ACCESS_GRANTED:
        return True
    if state == IOHID_ACCESS_DENIED:
        return False
    return None  # not decided yet


def permissions() -> list[Permission]:
    """The three switches, in the order the wizard asks for them."""
    return [
        Permission(
            "accessibility",
            "Accessibility",
            "lets TDS Macro click and press keys in Roblox",
            accessibility_granted(),
            SETTINGS_URLS["accessibility"],
            needs_restart=False,
        ),
        Permission(
            "screen_recording",
            "Screen Recording",
            "lets TDS Macro see the Roblox window to read your wave and cash",
            screen_recording_granted(),
            SETTINGS_URLS["screen_recording"],
            needs_restart=True,
        ),
        Permission(
            "input_monitoring",
            "Input Monitoring",
            "lets the hotkeys work and lets TDS Macro record your moves",
            input_monitoring_granted(),
            SETTINGS_URLS["input_monitoring"],
            needs_restart=True,
        ),
    ]


def request_permission(key: str) -> None:
    """Trigger macOS's own permission prompt for one switch."""
    if key == "accessibility":
        accessibility_granted(prompt=True)
    elif key == "screen_recording":
        screen_recording_granted(prompt=True)
    elif key == "input_monitoring":
        input_monitoring_granted(prompt=True)


def responsible_app_name() -> str:
    """Which entry the user must switch on in Privacy & Security."""
    if getattr(sys, "frozen", False):
        return "TDS Macro"
    return "the app you started TDS Macro from (usually Terminal)"


# --- event taps ------------------------------------------------------------------
EventHandler = Callable[[int, Any], bool]


class EventTapThread:
    """A Quartz event tap on its own thread with its own run loop.

    Used for the global hotkeys and for recording. ``handler(type, event)``
    returns True to swallow the event; that only takes effect for an *active*
    tap, which macOS grants only with the Accessibility permission. Without it
    the tap falls back to listen-only (Input Monitoring), where events always
    pass through to Roblox.
    """

    def __init__(
        self,
        name: str,
        event_types: list[int],
        handler: EventHandler,
        want_filter: bool = False,
    ) -> None:
        self.name = name
        self.event_types = event_types
        self.handler = handler
        self.want_filter = want_filter
        self.error = ""
        self.mode = ""
        self._thread: threading.Thread | None = None
        self._ready = threading.Event()
        self._running = False
        self._loop: Any = None
        self._tap: Any = None
        # Strong reference: pyobjc must keep the callback alive while the tap
        # can still call it.
        self._callback = self._on_event

    @property
    def running(self) -> bool:
        return self._running and self._thread is not None and self._thread.is_alive()

    def start(self, timeout: float = 3.0) -> str:
        if QUARTZ is None:
            self.error = "macOS event APIs are unavailable (pyobjc is not installed)"
            return self.error
        if self.running:
            return ""
        self.error = ""
        self._ready.clear()
        self._running = True
        self._thread = threading.Thread(target=self._run, name=self.name, daemon=True)
        self._thread.start()
        if not self._ready.wait(timeout):
            self.error = f"{self.name} did not start in time"
        if self.error:
            self._running = False
        return self.error

    def stop(self) -> None:
        if not self._running and self._thread is None:
            return
        self._running = False
        loop = self._loop
        if QUARTZ is not None and loop is not None:
            try:
                QUARTZ.CFRunLoopStop(loop)
            except Exception:  # pragma: no cover - macOS only
                log.exception("could not stop the %s run loop", self.name)
        if self._thread is not None:
            self._thread.join(timeout=2.0)
        self._thread = None
        self._loop = None

    def _mask(self) -> int:
        mask = 0
        for event_type in self.event_types:
            mask |= 1 << int(event_type)
        return mask

    def _run(self) -> None:  # pragma: no cover - macOS only
        q = QUARTZ
        tap = None
        if self.want_filter:
            tap = q.CGEventTapCreate(
                q.kCGSessionEventTap,
                q.kCGHeadInsertEventTap,
                q.kCGEventTapOptionDefault,
                self._mask(),
                self._callback,
                None,
            )
            self.mode = "active" if tap is not None else ""
        if tap is None:
            tap = q.CGEventTapCreate(
                q.kCGSessionEventTap,
                q.kCGHeadInsertEventTap,
                q.kCGEventTapOptionListenOnly,
                self._mask(),
                self._callback,
                None,
            )
            self.mode = "listen" if tap is not None else ""
        if tap is None:
            self.error = (
                "macOS did not allow TDS Macro to watch the keyboard. Switch on "
                f"{responsible_app_name()} under System Settings > Privacy & Security > "
                "Input Monitoring, then quit and reopen TDS Macro."
            )
            self._ready.set()
            return

        self._tap = tap
        source = q.CFMachPortCreateRunLoopSource(None, tap, 0)
        self._loop = q.CFRunLoopGetCurrent()
        q.CFRunLoopAddSource(self._loop, source, q.kCFRunLoopDefaultMode)
        q.CGEventTapEnable(tap, True)
        self._ready.set()
        log.info("%s running (%s tap)", self.name, self.mode)
        try:
            while self._running:
                q.CFRunLoopRunInMode(q.kCFRunLoopDefaultMode, 0.5, False)
        finally:
            q.CGEventTapEnable(tap, False)
            q.CFRunLoopRemoveSource(self._loop, source, q.kCFRunLoopDefaultMode)
            q.CFMachPortInvalidate(tap)
            self._tap = None
            log.info("%s stopped", self.name)

    def _on_event(  # pragma: no cover - macOS only
        self, proxy: Any, event_type: int, event: Any, refcon: Any
    ) -> Any:
        del proxy, refcon
        q = QUARTZ
        # macOS disables a tap whose callback is ever too slow; turn it back on
        # rather than silently losing hotkeys for the rest of the session.
        if event_type in (q.kCGEventTapDisabledByTimeout, q.kCGEventTapDisabledByUserInput):
            if self._tap is not None:
                q.CGEventTapEnable(self._tap, True)
            return event
        try:
            swallow = self.handler(int(event_type), event)
        except Exception:
            log.exception("%s handler raised", self.name)
            swallow = False
        if swallow and self.mode == "active":
            return None
        return event


def is_our_event(event: Any) -> bool:  # pragma: no cover - macOS only
    """True for events this app posted itself."""
    value = QUARTZ.CGEventGetIntegerValueField(event, QUARTZ.kCGEventSourceUserData)
    return int(value) == TDS_MACRO_SIGNATURE


def is_autorepeat(event: Any) -> bool:  # pragma: no cover - macOS only
    value = QUARTZ.CGEventGetIntegerValueField(event, QUARTZ.kCGKeyboardEventAutorepeat)
    return bool(value)


def monotonic_ms() -> int:
    return int(time.monotonic() * 1000)
