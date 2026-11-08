"""Input emission via ``SendInput``, plus a no-op backend for dry runs.

Backends expose *primitives only* -- move, press, release -- and never sleep.
All timing lives in the strategy engine, which keeps the engine non-blocking and
makes a dry run a faithful trace of what would have been sent.

Two independent safety gates apply to every emission:

* the Roblox window must resolve to a valid client rectangle, and
* Roblox must be the foreground window.

The engine checks the second one too; duplicating it here means a bug in the
engine still cannot type into the user's browser.
"""

from __future__ import annotations

import ctypes
import logging
from collections.abc import Callable
from dataclasses import dataclass, field
from enum import Enum
from typing import Protocol

from .coords import NormPoint, norm_to_absolute
from .keys import is_extended, key_to_vk
from .win32_defs import (
    INPUT,
    INPUT_KEYBOARD,
    INPUT_MOUSE,
    IS_WINDOWS,
    KEYEVENTF_EXTENDEDKEY,
    KEYEVENTF_KEYUP,
    KEYEVENTF_SCANCODE,
    KEYBDINPUT,
    MAPVK_VK_TO_VSC,
    MOUSEEVENTF_ABSOLUTE,
    MOUSEEVENTF_LEFTDOWN,
    MOUSEEVENTF_LEFTUP,
    MOUSEEVENTF_MIDDLEDOWN,
    MOUSEEVENTF_MIDDLEUP,
    MOUSEEVENTF_MOVE,
    MOUSEEVENTF_RIGHTDOWN,
    MOUSEEVENTF_RIGHTUP,
    MOUSEEVENTF_VIRTUALDESK,
    MOUSEINPUT,
    TDS_MACRO_SIGNATURE,
    user32,
)
from .window import GeometryProvider

log = logging.getLogger(__name__)


class MouseButton(Enum):
    LEFT = "left"
    RIGHT = "right"
    MIDDLE = "middle"


_BUTTON_FLAGS: dict[MouseButton, tuple[int, int]] = {
    MouseButton.LEFT: (MOUSEEVENTF_LEFTDOWN, MOUSEEVENTF_LEFTUP),
    MouseButton.RIGHT: (MOUSEEVENTF_RIGHTDOWN, MOUSEEVENTF_RIGHTUP),
    MouseButton.MIDDLE: (MOUSEEVENTF_MIDDLEDOWN, MOUSEEVENTF_MIDDLEUP),
}


@dataclass(frozen=True)
class InputIntent:
    """One primitive an engine asked for -- the unit of a dry-run trace."""

    kind: str
    key: str | None = None
    button: MouseButton | None = None
    pos: NormPoint | None = None

    def describe(self) -> str:
        if self.kind == "move" and self.pos is not None:
            return f"move to ({self.pos.x:.4f}, {self.pos.y:.4f})"
        if self.kind in {"mouse_down", "mouse_up"} and self.button is not None:
            return f"{self.kind.replace('_', ' ')} {self.button.value}"
        if self.kind in {"key_down", "key_up"}:
            return f"{self.kind.replace('_', ' ')} {self.key}"
        return self.kind


class InputBackend(Protocol):
    """Primitive input emission.  Implementations must never block."""

    @property
    def dry_run(self) -> bool: ...

    def move(self, point: NormPoint) -> bool: ...

    def mouse_down(self, button: MouseButton = MouseButton.LEFT) -> bool: ...

    def mouse_up(self, button: MouseButton = MouseButton.LEFT) -> bool: ...

    def key_down(self, key: str) -> bool: ...

    def key_up(self, key: str) -> bool: ...

    def release_all(self) -> None: ...

    def last_error(self) -> str: ...


@dataclass
class NullBackend:
    """Dry-run backend: records intents, sends nothing.

    Also used as the test double for the engine, which is why it tracks held
    keys the same way the real backend does -- a test can assert that an
    emergency stop actually released everything.
    """

    on_intent: Callable[[InputIntent], None] | None = None
    intents: list[InputIntent] = field(default_factory=list)
    held_keys: set[str] = field(default_factory=set)
    held_buttons: set[MouseButton] = field(default_factory=set)
    cursor: NormPoint | None = None

    @property
    def dry_run(self) -> bool:
        return True

    def _record(self, intent: InputIntent) -> bool:
        self.intents.append(intent)
        if self.on_intent is not None:
            self.on_intent(intent)
        return True

    def move(self, point: NormPoint) -> bool:
        self.cursor = point.clamped()
        return self._record(InputIntent("move", pos=self.cursor))

    def mouse_down(self, button: MouseButton = MouseButton.LEFT) -> bool:
        self.held_buttons.add(button)
        return self._record(InputIntent("mouse_down", button=button))

    def mouse_up(self, button: MouseButton = MouseButton.LEFT) -> bool:
        self.held_buttons.discard(button)
        return self._record(InputIntent("mouse_up", button=button))

    def key_down(self, key: str) -> bool:
        self.held_keys.add(key)
        return self._record(InputIntent("key_down", key=key))

    def key_up(self, key: str) -> bool:
        self.held_keys.discard(key)
        return self._record(InputIntent("key_up", key=key))

    def release_all(self) -> None:
        for key in sorted(self.held_keys):
            self._record(InputIntent("key_up", key=key))
        self.held_keys.clear()
        for button in list(self.held_buttons):
            self._record(InputIntent("mouse_up", button=button))
        self.held_buttons.clear()

    def last_error(self) -> str:
        return ""

    def trace(self) -> list[str]:
        return [intent.describe() for intent in self.intents]


class Win32Backend:
    """Real input emission through ``SendInput``.

    Mouse moves are absolute and flagged ``MOUSEEVENTF_VIRTUALDESK``, so pointer
    acceleration, mouse speed and "Enhance pointer precision" have no effect on
    where a click lands, and nothing about the user's system settings is read or
    modified.
    """

    def __init__(
        self,
        geometry: GeometryProvider,
        use_scancodes: bool = True,
        require_foreground: bool = True,
    ) -> None:
        self.geometry = geometry
        self.use_scancodes = use_scancodes
        self.require_foreground = require_foreground
        self.held_keys: set[str] = set()
        self.held_buttons: set[MouseButton] = set()
        self._last_error = ""
        self._extra = ctypes.c_void_p(TDS_MACRO_SIGNATURE)

    @property
    def dry_run(self) -> bool:
        return False

    def last_error(self) -> str:
        return self._last_error

    # -- gating ---------------------------------------------------------------
    def _ready(self) -> bool:
        if not IS_WINDOWS or user32 is None:
            self._last_error = "input is only available on Windows"
            return False
        if self.require_foreground and not self.geometry.is_foreground():
            self._last_error = "Roblox is not the foreground window"
            return False
        return True

    def _send(self, *events: INPUT) -> bool:
        assert user32 is not None
        array = (INPUT * len(events))(*events)
        sent = user32.SendInput(len(events), array, ctypes.sizeof(INPUT))
        if sent != len(events):
            code = ctypes.get_last_error()
            self._last_error = (
                f"SendInput sent {sent}/{len(events)} events (WinError {code}). "
                "A UAC prompt or an elevated window in the foreground will block input."
            )
            log.warning("%s", self._last_error)
            return False
        return True

    # -- primitives -----------------------------------------------------------
    def move(self, point: NormPoint) -> bool:
        if not self._ready():
            return False
        client = self.geometry.client_rect()
        if client is None:
            self._last_error = "Roblox window geometry unavailable"
            return False
        try:
            ax, ay = norm_to_absolute(point, client, self.geometry.virtual_screen())
        except ValueError as exc:
            self._last_error = str(exc)
            return False
        event = INPUT(type=INPUT_MOUSE)
        event.mi = MOUSEINPUT(
            dx=ax,
            dy=ay,
            mouseData=0,
            dwFlags=MOUSEEVENTF_MOVE | MOUSEEVENTF_ABSOLUTE | MOUSEEVENTF_VIRTUALDESK,
            time=0,
            dwExtraInfo=self._extra,
        )
        return self._send(event)

    def _mouse_button(self, button: MouseButton, down: bool) -> bool:
        if not self._ready():
            return False
        flags = _BUTTON_FLAGS[button][0 if down else 1]
        event = INPUT(type=INPUT_MOUSE)
        event.mi = MOUSEINPUT(
            dx=0, dy=0, mouseData=0, dwFlags=flags, time=0, dwExtraInfo=self._extra
        )
        ok = self._send(event)
        if ok:
            if down:
                self.held_buttons.add(button)
            else:
                self.held_buttons.discard(button)
        return ok

    def mouse_down(self, button: MouseButton = MouseButton.LEFT) -> bool:
        return self._mouse_button(button, True)

    def mouse_up(self, button: MouseButton = MouseButton.LEFT) -> bool:
        return self._mouse_button(button, False)

    def _key_event(self, key: str, down: bool) -> bool:
        if not self._ready():
            return False
        assert user32 is not None
        try:
            vk = key_to_vk(key)
        except ValueError as exc:
            self._last_error = str(exc)
            return False
        flags = 0 if down else KEYEVENTF_KEYUP
        scan = 0
        if self.use_scancodes:
            # Games that read raw/DirectInput-style state respond to scan codes
            # far more reliably than to virtual-key-only events.
            scan = int(user32.MapVirtualKeyW(vk, MAPVK_VK_TO_VSC))
            if scan:
                flags |= KEYEVENTF_SCANCODE
        if is_extended(vk):
            flags |= KEYEVENTF_EXTENDEDKEY
        event = INPUT(type=INPUT_KEYBOARD)
        event.ki = KEYBDINPUT(
            wVk=0 if flags & KEYEVENTF_SCANCODE else vk,
            wScan=scan,
            dwFlags=flags,
            time=0,
            dwExtraInfo=self._extra,
        )
        ok = self._send(event)
        if ok:
            if down:
                self.held_keys.add(key)
            else:
                self.held_keys.discard(key)
        return ok

    def key_down(self, key: str) -> bool:
        return self._key_event(key, True)

    def key_up(self, key: str) -> bool:
        return self._key_event(key, False)

    def release_all(self) -> None:
        """Release everything we are holding, ignoring the foreground gate.

        Called on every stop path including the emergency stop.  The gate is
        bypassed deliberately: if Roblox lost focus while we held a key down,
        refusing to send the key-up would leave that key stuck for the user.
        """
        previous = self.require_foreground
        self.require_foreground = False
        try:
            for key in sorted(self.held_keys):
                self._key_event(key, False)
            self.held_keys.clear()
            for button in list(self.held_buttons):
                self._mouse_button(button, False)
            self.held_buttons.clear()
        finally:
            self.require_foreground = previous
