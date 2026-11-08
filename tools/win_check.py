#!/usr/bin/env python3
"""Verify the Windows integration without needing Roblox.

Run this on your own machine if something is not working:

    python tools/win_check.py

It checks the parts that cannot be covered by unit tests on Linux CI -- the
ctypes structure sizes against the real Win64 ABI, DPI awareness, monitor
geometry, window enumeration, and whether a global hotkey can actually be
claimed and released.  It sends no input and does not need Roblox running.

Exits non-zero if a check fails, so CI can use it as a gate.
"""

from __future__ import annotations

import ctypes
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from tds_macro.input import win32_defs as w32  # noqa: E402
from tds_macro.input.coords import (  # noqa: E402
    NormPoint,
    Rect,
    absolute_to_screen,
    norm_to_absolute,
    round_trip_error,
)
from tds_macro.input.hotkeys import HotkeyManager  # noqa: E402
from tds_macro.input.window import (  # noqa: E402
    RobloxWindow,
    choose_best_candidate,
    enumerate_windows,
    set_dpi_awareness,
)

# Expected Win64 sizes.  These are checkable on any platform because the
# structures use explicit fixed-width types rather than ctypes.wintypes.
EXPECTED_SIZES = {
    "INPUT": 40,
    "MOUSEINPUT": 32,
    "KEYBDINPUT": 24,
    "MSG": 48,
    "RECT": 16,
    "POINT": 8,
    "KBDLLHOOKSTRUCT": 24,
    "MSLLHOOKSTRUCT": 32,
}

# A combination nothing realistically owns, used only to prove registration and
# release both work.
PROBE_HOTKEY = "Ctrl+Alt+Shift+F24"


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


def check_abi(report: Report) -> None:
    print("\nWin32 structure sizes (must match the Win64 ABI)")
    for name, expected in EXPECTED_SIZES.items():
        actual = ctypes.sizeof(getattr(w32, name))
        if actual == expected:
            report.ok(name, f"{actual} bytes")
        else:
            report.fail(name, f"{actual} bytes, expected {expected}")
    if w32.MSG.wParam.offset != 16:
        report.fail("MSG.wParam offset", f"{w32.MSG.wParam.offset}, expected 16")
    else:
        report.ok("MSG.wParam offset", "16")


def check_coordinates(report: Report) -> None:
    print("\nCoordinate conversion on this machine's actual monitor layout")
    screen = RobloxWindow().virtual_screen()
    report.ok(
        "virtual desktop",
        f"origin ({screen.left}, {screen.top}) size {screen.width}x{screen.height}",
    )
    if not screen.is_valid():
        report.fail("virtual desktop", "reported no area")
        return
    error = round_trip_error(screen)
    if error == 0:
        report.ok("absolute round trip", "lossless")
    else:
        report.fail("absolute round trip", f"up to {error} px of error")

    corners = [
        (screen.left, screen.top),
        (screen.right - 1, screen.top),
        (screen.left, screen.bottom - 1),
        (screen.right - 1, screen.bottom - 1),
    ]
    for x, y in corners:
        client = Rect(x, y, x + 1, y + 1)
        ax, ay = norm_to_absolute(NormPoint(0.5, 0.5), client, screen)
        back = absolute_to_screen(ax, ay, screen)
        if back == (x, y):
            report.ok(f"corner ({x}, {y})", f"-> absolute ({ax}, {ay}) -> back exactly")
        else:
            report.fail(f"corner ({x}, {y})", f"round-tripped to {back}")


def check_dpi(report: Report) -> None:
    print("\nDPI awareness")
    mode = set_dpi_awareness()
    if "per-monitor" in mode and "v2" in mode:
        report.ok("process DPI mode", mode)
    elif not w32.IS_WINDOWS:
        report.skip("process DPI mode", mode)
    else:
        report.fail(
            "process DPI mode",
            f"{mode} -- clicks may be offset on displays that are not at 100% scaling",
        )


def check_windows(report: Report) -> None:
    print("\nWindow enumeration")
    if not w32.IS_WINDOWS:
        report.skip("enumerate windows", "not running on Windows")
        return
    windows = enumerate_windows()
    report.ok("visible top-level windows", str(len(windows)))
    if not windows:
        report.fail("enumerate windows", "found none, which should be impossible")
        return
    match = choose_best_candidate(windows)
    if match is None:
        report.skip(
            "Roblox window",
            "not found (that is fine if Roblox is not running right now)",
        )
    else:
        report.ok("Roblox window", f"{match.window.describe()} (score {match.score})")
        roblox = RobloxWindow(match.window.hwnd)
        rect = roblox.client_rect()
        if rect is None:
            report.fail("client rect", roblox.last_error or "unavailable")
        else:
            report.ok(
                "client rect",
                f"{rect.width}x{rect.height} at ({rect.left}, {rect.top})",
            )


def check_hotkeys(report: Report) -> None:
    print("\nGlobal hotkeys")
    if not w32.IS_WINDOWS:
        report.skip("register hotkey", "not running on Windows")
        return
    manager = HotkeyManager()
    manager.set_binding("probe", PROBE_HOTKEY, lambda: None)
    problems = manager.start()
    active = manager.active_bindings()
    if problems or len(active) != 1:
        report.fail("register hotkey", "; ".join(problems) or "nothing was registered")
    else:
        report.ok("register hotkey", active[0].status())
    manager.stop()
    if manager.active_bindings():
        report.fail("release hotkey", "still held after stop()")
    else:
        report.ok("release hotkey", "released cleanly")


def check_capture(report: Report) -> None:
    print("\nScreen capture")
    if not w32.IS_WINDOWS and not os.environ.get("DISPLAY"):
        # A headless container has no desktop to capture; that is not a defect.
        report.skip("grab 64x64", "no display available (headless)")
        return
    from tds_macro.vision.capture import ScreenCapture

    capture = ScreenCapture()
    try:
        screen = RobloxWindow().virtual_screen()
        probe = Rect(screen.left, screen.top, screen.left + 64, screen.top + 64)
        frame = capture.grab(probe)
        if frame is None:
            report.fail("grab 64x64", capture.error or "returned nothing")
        elif frame.shape[0] != 64 or frame.shape[1] != 64:
            report.fail("grab 64x64", f"got {frame.shape[1]}x{frame.shape[0]}")
        else:
            report.ok("grab 64x64", f"{frame.shape[2]} channels")
    finally:
        capture.close()


def main() -> int:
    print("TDS Macro -- Windows integration check")
    print(f"Python {sys.version.split()[0]} on {sys.platform}")
    report = Report()
    check_abi(report)
    check_dpi(report)
    check_coordinates(report)
    check_windows(report)
    check_hotkeys(report)
    check_capture(report)

    print("\n" + "-" * 60)
    if report.failures:
        print(f"{len(report.failures)} check(s) FAILED:")
        for failure in report.failures:
            print(f"  - {failure}")
        return 1
    if report.skips:
        print(f"All checks passed ({len(report.skips)} skipped: not on Windows).")
    else:
        print("All checks passed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
