#!/usr/bin/env python3
"""Verify the macOS integration without needing Roblox.

Run this on your Mac if something is not working:

    python3 tools/mac_check.py

It checks what the Linux test suite cannot: that every macOS framework call
the app makes actually exists, that events can be built, the display and
window lists, and -- reported but not failed, since only you can switch them
on -- the three Privacy & Security permissions. It sends no input.

Exits non-zero on a structural failure, so CI can use it as a gate.
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from tds_macro.input import mac_quartz  # noqa: E402
from tds_macro.input.coords import Rect  # noqa: E402
from tds_macro.input.mac_keys import MAC_KEYCODES, mac_keycode  # noqa: E402
from tds_macro.input.mac_quartz import APPKIT, APPSERVICES, QUARTZ  # noqa: E402

# Every Quartz/CoreFoundation symbol the app uses. A misspelt constant would
# otherwise only surface as an AttributeError the first time a user pressed a
# hotkey or started a run.
QUARTZ_SYMBOLS = [
    "CFMachPortCreateRunLoopSource",
    "CFMachPortInvalidate",
    "CFRunLoopAddSource",
    "CFRunLoopGetCurrent",
    "CFRunLoopRemoveSource",
    "CFRunLoopRunInMode",
    "CFRunLoopStop",
    "CGDisplayBounds",
    "CGEventCreate",
    "CGEventCreateKeyboardEvent",
    "CGEventCreateMouseEvent",
    "CGEventGetFlags",
    "CGEventGetIntegerValueField",
    "CGEventGetLocation",
    "CGEventPost",
    "CGEventSetFlags",
    "CGEventSetIntegerValueField",
    "CGEventTapCreate",
    "CGEventTapEnable",
    "CGGetActiveDisplayList",
    "CGMainDisplayID",
    "CGWindowListCopyWindowInfo",
    "kCFRunLoopDefaultMode",
    "kCGEventKeyDown",
    "kCGEventKeyUp",
    "kCGEventLeftMouseDown",
    "kCGEventLeftMouseUp",
    "kCGEventMouseMoved",
    "kCGEventOtherMouseDown",
    "kCGEventOtherMouseUp",
    "kCGEventRightMouseDown",
    "kCGEventRightMouseUp",
    "kCGEventSourceUserData",
    "kCGEventTapDisabledByTimeout",
    "kCGEventTapDisabledByUserInput",
    "kCGEventTapOptionDefault",
    "kCGEventTapOptionListenOnly",
    "kCGHIDEventTap",
    "kCGHeadInsertEventTap",
    "kCGKeyboardEventAutorepeat",
    "kCGKeyboardEventKeycode",
    "kCGMouseButtonCenter",
    "kCGMouseButtonLeft",
    "kCGMouseButtonRight",
    "kCGMouseEventButtonNumber",
    "kCGMouseEventClickState",
    "kCGSessionEventTap",
    "kCGWindowListExcludeDesktopElements",
    "kCGWindowListOptionIncludingWindow",
    "kCGWindowListOptionOnScreenOnly",
]
# Present on macOS 10.15+ only; the app degrades without them.
OPTIONAL_QUARTZ = ["CGPreflightScreenCaptureAccess", "CGRequestScreenCaptureAccess"]
APPSERVICES_SYMBOLS = [
    "AXIsProcessTrusted",
    "AXIsProcessTrustedWithOptions",
    "kAXTrustedCheckOptionPrompt",
]

# The window-list keys mac_window.py uses as plain strings must equal the
# framework's constants, or every window lookup silently finds nothing.
WINDOW_KEYS = [
    "kCGWindowNumber",
    "kCGWindowOwnerName",
    "kCGWindowOwnerPID",
    "kCGWindowBounds",
    "kCGWindowLayer",
    "kCGWindowName",
    "kCGWindowIsOnscreen",
]


class Report:
    def __init__(self) -> None:
        self.failures: list[str] = []
        self.skips: list[str] = []

    def ok(self, label: str, detail: str = "") -> None:
        print(f"  [ ok ] {label}" + (f": {detail}" if detail else ""))

    def fail(self, label: str, detail: str) -> None:
        print(f"  [FAIL] {label}: {detail}")
        self.failures.append(f"{label}: {detail}")

    def skip(self, label: str, detail: str) -> None:
        print(f"  [skip] {label}: {detail}")
        self.skips.append(label)


def check_frameworks(report: Report) -> bool:
    print("\nmacOS frameworks")
    missing = mac_quartz.missing_apis()
    if missing:
        report.fail("pyobjc frameworks", "missing " + ", ".join(missing))
        return False
    report.ok("pyobjc frameworks", "Quartz, AppKit, ApplicationServices")
    absent = [name for name in QUARTZ_SYMBOLS if not hasattr(QUARTZ, name)]
    if absent:
        report.fail("Quartz symbols", "missing " + ", ".join(absent))
    else:
        report.ok("Quartz symbols", f"all {len(QUARTZ_SYMBOLS)} present")
    for name in OPTIONAL_QUARTZ:
        if hasattr(QUARTZ, name):
            report.ok(name)
        else:
            report.skip(name, "not available on this macOS version")
    absent = [name for name in APPSERVICES_SYMBOLS if not hasattr(APPSERVICES, name)]
    if absent:
        report.fail("ApplicationServices symbols", "missing " + ", ".join(absent))
    else:
        report.ok("ApplicationServices symbols", "Accessibility checks present")
    if not hasattr(APPKIT, "NSWorkspace"):
        report.fail("AppKit", "NSWorkspace missing")
    for key in WINDOW_KEYS:
        value = getattr(QUARTZ, key, None)
        if value != key:
            report.fail("window-list key", f"{key} is {value!r} in this pyobjc")
    if not report.failures:
        report.ok("window-list keys", "match the framework's constants")
    return not report.failures


def check_keys(report: Report) -> None:
    print("\nKeyboard table")
    codes = list(MAC_KEYCODES.values())
    duplicates = len(codes) - len(set(codes))
    # enter/return and esc/escape intentionally share a code.
    if duplicates != 2:
        report.fail("keycodes", f"{duplicates} unexpected duplicate codes")
    else:
        report.ok("keycodes", f"{len(MAC_KEYCODES)} names")
    for name in ("1", "5", "e", "space", "f6"):
        report.ok(f"key {name!r}", hex(mac_keycode(name)))


def check_events(report: Report) -> None:
    print("\nEvent construction (nothing is posted)")
    q = QUARTZ
    try:
        move = q.CGEventCreateMouseEvent(
            None, q.kCGEventMouseMoved, (10.0, 10.0), q.kCGMouseButtonLeft
        )
        q.CGEventSetFlags(move, 0)
        q.CGEventSetIntegerValueField(
            move, q.kCGEventSourceUserData, mac_quartz.TDS_MACRO_SIGNATURE
        )
        tagged = mac_quartz.is_our_event(move)
        key = q.CGEventCreateKeyboardEvent(None, mac_keycode("e"), True)
        keycode = int(q.CGEventGetIntegerValueField(key, q.kCGKeyboardEventKeycode))
        here = q.CGEventGetLocation(q.CGEventCreate(None))
    except Exception as exc:
        report.fail("build events", repr(exc))
        return
    if not tagged:
        report.fail("event tagging", "our signature did not round-trip")
    else:
        report.ok("event tagging", "own events are recognisable")
    if keycode != mac_keycode("e"):
        report.fail("keyboard event", f"keycode came back as {keycode}")
    else:
        report.ok("keyboard event", "keycode round-trips")
    report.ok("cursor position", f"({here.x:.0f}, {here.y:.0f}) pt")


def check_geometry(report: Report) -> None:
    print("\nDisplays and windows")
    from tds_macro.input.mac_window import (
        MacRobloxWindow,
        display_rects,
        enumerate_mac_windows,
        frontmost_pid,
        title_bar_points,
        union_screen,
    )

    displays = display_rects()
    if not displays:
        report.fail("displays", "none reported")
    else:
        for display in displays:
            report.ok(
                "display",
                f"{display.width}x{display.height} pt at {display.left},{display.top}",
            )
        screen = union_screen(displays)
        report.ok("desktop", f"{screen.width}x{screen.height} pt at {screen.left},{screen.top}")
    report.ok("title bar", f"{title_bar_points()} pt on this macOS version")
    windows = enumerate_mac_windows()
    report.ok("on-screen windows", str(len(windows)))
    pid = frontmost_pid()
    if pid:
        report.ok("frontmost app", f"pid {pid}")
    else:
        report.skip("frontmost app", "could not be determined")
    roblox = MacRobloxWindow()
    if roblox.refresh():
        rect = roblox.client_rect()
        if rect is None:
            report.fail("Roblox content area", roblox.last_error or "unavailable")
        else:
            report.ok("Roblox window", roblox.describe())
    else:
        report.skip("Roblox window", "not found (fine if Roblox is not open right now)")


def check_permissions(report: Report) -> None:
    print(f"\nPrivacy & Security (for {mac_quartz.responsible_app_name()})")
    for permission in mac_quartz.permissions():
        if permission.granted:
            report.ok(permission.title, "allowed")
        else:
            report.skip(
                permission.title,
                f"{'not allowed' if permission.granted is False else 'not decided'} -- "
                f"this {permission.why}",
            )


def check_taps(report: Report) -> None:
    print("\nHotkeys (event tap)")
    from tds_macro.input.mac_hotkeys import MacHotkeyManager

    manager = MacHotkeyManager()
    manager.set_binding("probe", "Ctrl+Option+Shift+F19", lambda: None)
    problems = manager.start()
    if manager.active_bindings():
        report.ok("hotkey tap", "running")
    else:
        report.skip("hotkey tap", "; ".join(problems) or "not started")
    manager.stop()
    if manager.active_bindings():
        report.fail("hotkey tap", "still active after stop()")


def check_capture(report: Report) -> None:
    print("\nScreen capture")
    from tds_macro.vision.capture import ScreenCapture

    capture = ScreenCapture()
    try:
        frame = capture.grab(Rect(0, 0, 64, 64))
        if frame is None:
            report.skip("grab 64x64 pt", capture.error or "no image (Screen Recording off?)")
        else:
            report.ok("grab 64x64 pt", f"{frame.shape[1]}x{frame.shape[0]} px (Retina doubles it)")
    finally:
        capture.close()


def main() -> int:
    print("TDS Macro -- macOS integration check")
    print(f"Python {sys.version.split()[0]} on {sys.platform}")
    report = Report()
    if sys.platform != "darwin":
        print("\nNot a Mac; nothing to check.")
        return 0
    if check_frameworks(report):
        check_keys(report)
        check_events(report)
        check_geometry(report)
        check_permissions(report)
        check_taps(report)
        check_capture(report)

    print("\n" + "-" * 60)
    if report.failures:
        print(f"{len(report.failures)} check(s) FAILED:")
        for failure in report.failures:
            print(f"  - {failure}")
        return 1
    print(
        "All checks passed"
        + (f" ({len(report.skips)} skipped -- usually permissions)." if report.skips else ".")
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
