"""Win32 structure layout.

These assertions are the reason the input layer spells its widths out with
``c_int32``/``c_uint16`` instead of using ``ctypes.wintypes``, whose ``LONG`` is
64-bit under LP64: one definition that is correct on Windows and checkable
here. A wrong size means ``SendInput`` silently rejects every event, which on
Windows looks like "the macro does nothing" with no error at all.
"""

from __future__ import annotations

import ctypes

import pytest

from tds_macro.input import win32_defs as w32

# Sizes and offsets as the Win64 ABI defines them.
EXPECTED_SIZES = {
    "INPUT": 40,
    "MOUSEINPUT": 32,
    "KEYBDINPUT": 24,
    "HARDWAREINPUT": 8,
    "MSG": 48,
    "RECT": 16,
    "POINT": 8,
    "KBDLLHOOKSTRUCT": 24,
    "MSLLHOOKSTRUCT": 32,
}


@pytest.mark.parametrize(("name", "expected"), sorted(EXPECTED_SIZES.items()))
def test_structure_sizes_match_the_win64_abi(name: str, expected: int) -> None:
    assert ctypes.sizeof(getattr(w32, name)) == expected


def test_field_offsets_that_the_code_depends_on() -> None:
    # The hotkey pump reads the hotkey id out of MSG.wParam.
    assert w32.MSG.message.offset == 8
    assert w32.MSG.wParam.offset == 16
    # INPUT's union must start on the 8-byte boundary after the type tag.
    assert w32.INPUT.u.offset == 8


def test_input_union_members_overlap() -> None:
    """All three members share the same storage; if they did not, a keyboard
    event written through .ki would land in the wrong bytes."""
    assert w32.INPUT.mi.offset == w32.INPUT.ki.offset == w32.INPUT.hi.offset


def test_mouse_flags_are_the_documented_values() -> None:
    assert w32.MOUSEEVENTF_MOVE == 0x0001
    assert w32.MOUSEEVENTF_LEFTDOWN == 0x0002
    assert w32.MOUSEEVENTF_LEFTUP == 0x0004
    assert w32.MOUSEEVENTF_ABSOLUTE == 0x8000
    # Without VIRTUALDESK, absolute coordinates are normalized to the primary
    # monitor, so every click on a second screen lands on the first.
    assert w32.MOUSEEVENTF_VIRTUALDESK == 0x4000


def test_keyboard_and_hook_constants() -> None:
    assert w32.KEYEVENTF_KEYUP == 0x0002
    assert w32.KEYEVENTF_SCANCODE == 0x0008
    assert w32.KEYEVENTF_EXTENDEDKEY == 0x0001
    assert w32.WH_KEYBOARD_LL == 13
    assert w32.WH_MOUSE_LL == 14
    assert w32.WM_HOTKEY == 0x0312
    assert w32.LLKHF_INJECTED == 0x10


def test_dll_handles_are_absent_off_windows() -> None:
    """The module must import anywhere; only the handles are platform-gated."""
    if w32.IS_WINDOWS:  # pragma: no cover - only on Windows
        assert w32.user32 is not None
        assert w32.kernel32 is not None
    else:
        assert w32.user32 is None
        assert w32.kernel32 is None


def test_virtual_desk_and_absolute_are_distinct_bits() -> None:
    assert w32.MOUSEEVENTF_ABSOLUTE & w32.MOUSEEVENTF_VIRTUALDESK == 0
