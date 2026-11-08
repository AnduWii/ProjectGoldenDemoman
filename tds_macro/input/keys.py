"""Virtual-key tables and hotkey-spec parsing.

Pure data and string parsing -- no Win32 calls -- so the settings UI and the
tests can validate keybinds on any platform.
"""

from __future__ import annotations

from dataclasses import dataclass

# --- Modifier flags accepted by RegisterHotKey ---------------------------------
MOD_ALT = 0x0001
MOD_CONTROL = 0x0002
MOD_SHIFT = 0x0004
MOD_WIN = 0x0008
# Without MOD_NOREPEAT a held hotkey fires continuously, which would queue
# dozens of "start playback" requests from one long press.
MOD_NOREPEAT = 0x4000

MODIFIER_NAMES: dict[str, int] = {
    "ctrl": MOD_CONTROL,
    "control": MOD_CONTROL,
    "alt": MOD_ALT,
    "shift": MOD_SHIFT,
    "win": MOD_WIN,
    "super": MOD_WIN,
    "meta": MOD_WIN,
}

# --- Virtual-key codes --------------------------------------------------------
VK_NAMES: dict[str, int] = {
    "backspace": 0x08,
    "tab": 0x09,
    "enter": 0x0D,
    "return": 0x0D,
    "shift": 0x10,
    "ctrl": 0x11,
    "alt": 0x12,
    "pause": 0x13,
    "capslock": 0x14,
    "esc": 0x1B,
    "escape": 0x1B,
    "space": 0x20,
    "pageup": 0x21,
    "pagedown": 0x22,
    "end": 0x23,
    "home": 0x24,
    "left": 0x25,
    "up": 0x26,
    "right": 0x27,
    "down": 0x28,
    "insert": 0x2D,
    "delete": 0x2E,
    "numlock": 0x90,
    "scrolllock": 0x91,
    ";": 0xBA,
    "=": 0xBB,
    ",": 0xBC,
    "-": 0xBD,
    ".": 0xBE,
    "/": 0xBF,
    "`": 0xC0,
    "[": 0xDB,
    "\\": 0xDC,
    "]": 0xDD,
    "'": 0xDE,
}

for _i in range(10):  # 0-9
    VK_NAMES[str(_i)] = 0x30 + _i
for _i in range(26):  # a-z
    VK_NAMES[chr(ord("a") + _i)] = 0x41 + _i
for _i in range(1, 25):  # F1-F24
    VK_NAMES[f"f{_i}"] = 0x6F + _i
for _i in range(10):  # numpad 0-9
    VK_NAMES[f"num{_i}"] = 0x60 + _i

VK_TO_NAME: dict[int, str] = {}
for _name, _vk in VK_NAMES.items():
    # Prefer the canonical (first, longest-lived) spelling for display.
    if _vk not in VK_TO_NAME or len(_name) > len(VK_TO_NAME[_vk]):
        VK_TO_NAME.setdefault(_vk, _name)
VK_TO_NAME.update({0x0D: "enter", 0x1B: "esc", 0x11: "ctrl"})

# Keys that require KEYEVENTF_EXTENDEDKEY; sending these without the flag makes
# the arrow keys register as numpad presses on some layouts.
EXTENDED_VKS: frozenset[int] = frozenset(
    {0x21, 0x22, 0x23, 0x24, 0x25, 0x26, 0x27, 0x28, 0x2D, 0x2E, 0x90, 0x6F, 0xA3, 0xA5}
)

# Keys we refuse to bind as a global hotkey because Windows owns them or they
# would make the app impossible to escape from.
RESERVED_HOTKEYS: frozenset[str] = frozenset({"ctrl+alt+delete", "win+l", "ctrl+esc"})


class KeyParseError(ValueError):
    """Raised when a user-entered key or hotkey string cannot be understood."""


def normalize_key_name(name: str) -> str:
    """Canonical lower-case form of a single key name."""
    cleaned = name.strip().lower()
    if not cleaned:
        raise KeyParseError("empty key name")
    if cleaned not in VK_NAMES:
        raise KeyParseError(f"unknown key: {name!r}")
    return cleaned


def key_to_vk(name: str) -> int:
    """Translate a key name such as ``"q"``, ``"3"`` or ``"f6"`` to its VK code."""
    return VK_NAMES[normalize_key_name(name)]


def vk_to_key(vk: int) -> str:
    """Best-effort reverse lookup, used when turning recorded events into steps."""
    return VK_TO_NAME.get(vk, f"vk{vk:#04x}")


def is_extended(vk: int) -> bool:
    return vk in EXTENDED_VKS


@dataclass(frozen=True)
class HotkeySpec:
    """A parsed global hotkey: modifier bitmask plus a virtual-key code."""

    modifiers: int
    vk: int
    text: str

    @property
    def register_modifiers(self) -> int:
        """Modifiers as passed to ``RegisterHotKey``, with auto-repeat off."""
        return self.modifiers | MOD_NOREPEAT

    def describe(self) -> str:
        parts: list[str] = []
        if self.modifiers & MOD_CONTROL:
            parts.append("Ctrl")
        if self.modifiers & MOD_ALT:
            parts.append("Alt")
        if self.modifiers & MOD_SHIFT:
            parts.append("Shift")
        if self.modifiers & MOD_WIN:
            parts.append("Win")
        parts.append(vk_to_key(self.vk).upper())
        return "+".join(parts)


def parse_hotkey(spec: str) -> HotkeySpec:
    """Parse ``"F6"``, ``"Ctrl+Shift+P"``, ``"Alt+Num5"`` into a :class:`HotkeySpec`.

    Rejects modifier-only specs (``"Ctrl"``) and the handful of combinations
    Windows reserves, both of which otherwise fail silently at registration and
    leave the user with a hotkey that simply never fires.
    """
    raw = spec.strip()
    if not raw:
        raise KeyParseError("empty hotkey")
    if raw.lower().replace(" ", "") in RESERVED_HOTKEYS:
        raise KeyParseError(f"{raw} is reserved by Windows and cannot be bound")

    tokens = [t for t in (p.strip().lower() for p in raw.split("+")) if t]
    if not tokens:
        raise KeyParseError(f"could not parse hotkey: {spec!r}")

    *modifier_tokens, key_token = tokens
    modifiers = 0
    for token in modifier_tokens:
        if token not in MODIFIER_NAMES:
            raise KeyParseError(
                f"{token!r} is not a modifier -- a hotkey may name only one normal key"
            )
        modifiers |= MODIFIER_NAMES[token]

    if key_token in MODIFIER_NAMES:
        raise KeyParseError(
            f"{spec!r} is only modifiers -- add a normal key, e.g. Ctrl+Shift+F6"
        )
    return HotkeySpec(modifiers, key_to_vk(key_token), raw)


def validate_keybind(name: str) -> str:
    """Validate an in-game keybind, returning its canonical name."""
    return normalize_key_name(name)
