"""macOS virtual keycodes and modifier flags.

macOS identifies keys by *hardware position* (Carbon's ``kVK_*`` codes from
HIToolbox/Events.h), not by the Windows virtual-key numbers in
:mod:`tds_macro.input.keys`. Both tables share the same key *names*, so a
strategy or a keybind written on one platform means the same physical key on
the other.

Pure data and string handling: testable anywhere.
"""

from __future__ import annotations

from .keys import (
    MOD_ALT,
    MOD_CONTROL,
    MOD_SHIFT,
    MOD_WIN,
    HotkeySpec,
    KeyParseError,
    normalize_key_name,
)

# kVK_* from HIToolbox/Events.h. Letters and digits are ANSI positions.
MAC_KEYCODES: dict[str, int] = {
    "a": 0x00, "s": 0x01, "d": 0x02, "f": 0x03, "h": 0x04, "g": 0x05, "z": 0x06,
    "x": 0x07, "c": 0x08, "v": 0x09, "b": 0x0B, "q": 0x0C, "w": 0x0D, "e": 0x0E,
    "r": 0x0F, "y": 0x10, "t": 0x11, "o": 0x1F, "u": 0x20, "i": 0x22, "p": 0x23,
    "l": 0x25, "j": 0x26, "k": 0x28, "n": 0x2D, "m": 0x2E,
    "1": 0x12, "2": 0x13, "3": 0x14, "4": 0x15, "5": 0x17, "6": 0x16,
    "7": 0x1A, "8": 0x1C, "9": 0x19, "0": 0x1D,
    "=": 0x18, "-": 0x1B, "]": 0x1E, "[": 0x21, "'": 0x27, ";": 0x29,
    "\\": 0x2A, ",": 0x2B, "/": 0x2C, ".": 0x2F, "`": 0x32,
    "enter": 0x24, "return": 0x24, "tab": 0x30, "space": 0x31,
    "backspace": 0x33, "esc": 0x35, "escape": 0x35,
    "shift": 0x38, "capslock": 0x39, "alt": 0x3A, "ctrl": 0x3B,
    "delete": 0x75, "home": 0x73, "end": 0x77, "pageup": 0x74, "pagedown": 0x79,
    "left": 0x7B, "right": 0x7C, "down": 0x7D, "up": 0x7E,
    "f1": 0x7A, "f2": 0x78, "f3": 0x63, "f4": 0x76, "f5": 0x60, "f6": 0x61,
    "f7": 0x62, "f8": 0x64, "f9": 0x65, "f10": 0x6D, "f11": 0x67, "f12": 0x6F,
    "f13": 0x69, "f14": 0x6B, "f15": 0x71, "f16": 0x6A, "f17": 0x40, "f18": 0x4F,
    "f19": 0x50, "f20": 0x5A,
    "num0": 0x52, "num1": 0x53, "num2": 0x54, "num3": 0x55, "num4": 0x56,
    "num5": 0x57, "num6": 0x58, "num7": 0x59, "num8": 0x5B, "num9": 0x5C,
}
# Canonical name for each code, for turning recorded key events back into names.
_CANONICAL = ("enter", "esc", "ctrl", "alt")
MAC_KEYCODE_NAMES: dict[int, str] = {}
for _name, _code in MAC_KEYCODES.items():
    MAC_KEYCODE_NAMES.setdefault(_code, _name)
for _name in _CANONICAL:
    MAC_KEYCODE_NAMES[MAC_KEYCODES[_name]] = _name

# CGEventFlags masks (CGEventTypes.h).
FLAG_SHIFT = 0x00020000
FLAG_CONTROL = 0x00040000
FLAG_OPTION = 0x00080000
FLAG_COMMAND = 0x00100000
_MODIFIER_FLAGS = (
    (MOD_SHIFT, FLAG_SHIFT),
    (MOD_CONTROL, FLAG_CONTROL),
    (MOD_ALT, FLAG_OPTION),
    (MOD_WIN, FLAG_COMMAND),
)
RELEVANT_FLAGS = FLAG_SHIFT | FLAG_CONTROL | FLAG_OPTION | FLAG_COMMAND


def mac_keycode(name: str) -> int:
    """The macOS keycode for a key name, or :class:`KeyParseError`.

    A handful of PC keys -- Pause, Insert, Num Lock, Scroll Lock, F21-F24 --
    have no key on a Mac keyboard, so binding one is rejected up front rather
    than producing a hotkey or keybind that can never fire.
    """
    canonical = normalize_key_name(name)
    if canonical not in MAC_KEYCODES:
        raise KeyParseError(f"{name.upper()} has no equivalent on a Mac keyboard")
    return MAC_KEYCODES[canonical]


def mac_key_name(keycode: int) -> str | None:
    """The key name for a macOS keycode, or None for keys we do not track."""
    return MAC_KEYCODE_NAMES.get(keycode)


def mac_flags(modifiers: int) -> int:
    """Translate the shared MOD_* bitmask into CGEventFlags."""
    flags = 0
    for modifier, flag in _MODIFIER_FLAGS:
        if modifiers & modifier:
            flags |= flag
    return flags


def hotkey_matches(spec: HotkeySpec, keycode: int, flags: int) -> bool:
    """Whether a key-down event is this hotkey.

    Modifiers must match *exactly* -- Ctrl+Option+P must not also fire on
    Ctrl+Option+Shift+P -- while Caps Lock, Fn and the numeric-pad bit, which
    the system sets on its own, are ignored.
    """
    try:
        wanted = mac_keycode(spec.key)
    except KeyParseError:
        return False
    return keycode == wanted and (flags & RELEVANT_FLAGS) == mac_flags(spec.modifiers)
