"""Input emission on macOS through Quartz events.

The macOS counterpart of :class:`~tds_macro.input.sendinput.Win32Backend`,
with the same primitives-only, never-blocking contract and the same two safety
gates (the window must be measurable, and Roblox must be the active app).

Two Mac-specific details:

* Positions are global points, posted directly -- there is no 0..65535
  normalization step as on Windows, and pointer acceleration does not apply to
  posted absolute positions.
* Every event's modifier flags are cleared explicitly. Otherwise an event
  inherits whatever modifiers are physically held, and the user is holding
  Ctrl+Option at exactly the moment they press the start hotkey -- on a Mac
  Ctrl+click is a *right* click, and Ctrl+Option+1 is not the hotbar key.
"""

from __future__ import annotations

import logging
import time
from collections.abc import Iterable
from typing import Any

from .coords import NormPoint, norm_to_screen
from .keys import KeyParseError
from .mac_keys import mac_keycode
from .mac_quartz import QUARTZ, TDS_MACRO_SIGNATURE, accessibility_granted, responsible_app_name
from .sendinput import MouseButton
from .window import GeometryProvider

log = logging.getLogger(__name__)

ACCESSIBILITY_CACHE_S = 1.0


class QuartzBackend:
    """Real input emission on macOS."""

    def __init__(self, geometry: GeometryProvider, require_foreground: bool = True) -> None:
        self.geometry = geometry
        self.require_foreground = require_foreground
        self.held_keys: set[str] = set()
        self.held_buttons: set[MouseButton] = set()
        self._last_error = ""
        self._cursor: tuple[float, float] | None = None
        self._trusted = False
        self._trusted_checked = 0.0

    @property
    def dry_run(self) -> bool:
        return False

    def last_error(self) -> str:
        return self._last_error

    # -- gating ---------------------------------------------------------------
    def _accessibility(self) -> bool:
        """Cached permission check: the engine sends dozens of primitives a
        second, and the answer only changes when the user flips the switch."""
        now = time.monotonic()
        if now - self._trusted_checked > ACCESSIBILITY_CACHE_S:
            self._trusted = bool(accessibility_granted())
            self._trusted_checked = now
        return self._trusted

    def _ready(self) -> bool:
        if QUARTZ is None:
            self._last_error = "macOS input APIs are unavailable (pyobjc is not installed)"
            return False
        if not self._accessibility():
            # Without this, macOS silently drops every posted event -- the
            # macro would appear to run while doing nothing at all.
            self._last_error = (
                f"macOS is blocking input from {responsible_app_name()}. Switch it on under "
                "System Settings > Privacy & Security > Accessibility."
            )
            return False
        if self.require_foreground and not self.geometry.is_foreground():
            self._last_error = "Roblox is not the active window"
            return False
        return True

    def _post(self, event: Any) -> bool:  # pragma: no cover - macOS only
        if event is None:
            self._last_error = "macOS could not create the input event"
            return False
        QUARTZ.CGEventSetFlags(event, 0)
        QUARTZ.CGEventSetIntegerValueField(
            event, QUARTZ.kCGEventSourceUserData, TDS_MACRO_SIGNATURE
        )
        QUARTZ.CGEventPost(QUARTZ.kCGHIDEventTap, event)
        return True

    def _location(self) -> tuple[float, float]:  # pragma: no cover - macOS only
        if self._cursor is not None:
            return self._cursor
        here = QUARTZ.CGEventGetLocation(QUARTZ.CGEventCreate(None))
        return (float(here.x), float(here.y))

    # -- primitives -----------------------------------------------------------
    def move(self, point: NormPoint) -> bool:  # pragma: no cover - macOS only
        if not self._ready():
            return False
        client = self.geometry.client_rect()
        if client is None:
            self._last_error = "Roblox window geometry unavailable"
            return False
        try:
            x, y = norm_to_screen(point.clamped(), client)
        except ValueError as exc:
            self._last_error = str(exc)
            return False
        q = QUARTZ
        target = (float(x), float(y))
        held = next(iter(self.held_buttons), None)
        if held is None:
            event = q.CGEventCreateMouseEvent(
                None, q.kCGEventMouseMoved, target, q.kCGMouseButtonLeft
            )
        else:
            # With a button held, macOS expects a *dragged* event, and Roblox
            # turns the camera from the event's delta fields -- a plain move
            # with no delta would leave the camera where it was.
            kind, cg_button = {
                MouseButton.LEFT: (q.kCGEventLeftMouseDragged, q.kCGMouseButtonLeft),
                MouseButton.RIGHT: (q.kCGEventRightMouseDragged, q.kCGMouseButtonRight),
                MouseButton.MIDDLE: (q.kCGEventOtherMouseDragged, q.kCGMouseButtonCenter),
            }[held]
            event = q.CGEventCreateMouseEvent(None, kind, target, cg_button)
            if event is not None:
                start = self._location()
                q.CGEventSetIntegerValueField(
                    event, q.kCGMouseEventDeltaX, int(round(target[0] - start[0]))
                )
                q.CGEventSetIntegerValueField(
                    event, q.kCGMouseEventDeltaY, int(round(target[1] - start[1]))
                )
        ok = self._post(event)
        if ok:
            self._cursor = target
        return ok

    def _button_event(self, button: MouseButton, down: bool) -> Any:  # pragma: no cover
        q = QUARTZ
        events = {
            MouseButton.LEFT: (
                q.kCGEventLeftMouseDown,
                q.kCGEventLeftMouseUp,
                q.kCGMouseButtonLeft,
            ),
            MouseButton.RIGHT: (
                q.kCGEventRightMouseDown,
                q.kCGEventRightMouseUp,
                q.kCGMouseButtonRight,
            ),
            MouseButton.MIDDLE: (
                q.kCGEventOtherMouseDown,
                q.kCGEventOtherMouseUp,
                q.kCGMouseButtonCenter,
            ),
        }
        down_kind, up_kind, cg_button = events[button]
        event = q.CGEventCreateMouseEvent(
            None, down_kind if down else up_kind, self._location(), cg_button
        )
        if event is not None:
            # A click count of 1 marks this as a real single click; some apps
            # ignore button events that carry 0.
            q.CGEventSetIntegerValueField(event, q.kCGMouseEventClickState, 1)
            if button is MouseButton.MIDDLE:
                q.CGEventSetIntegerValueField(event, q.kCGMouseEventButtonNumber, 2)
        return event

    def _mouse_button(self, button: MouseButton, down: bool) -> bool:  # pragma: no cover
        if not self._ready():
            return False
        ok = self._post(self._button_event(button, down))
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

    def _key_event(self, key: str, down: bool) -> bool:  # pragma: no cover - macOS only
        if not self._ready():
            return False
        try:
            code = mac_keycode(key)
        except KeyParseError as exc:
            self._last_error = str(exc)
            return False
        ok = self._post(QUARTZ.CGEventCreateKeyboardEvent(None, code, down))
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

    def scroll(self, clicks: int) -> bool:  # pragma: no cover - macOS only
        """Turn the wheel ``clicks`` lines; positive is up, as on Windows."""
        if not self._ready():
            return False
        event = QUARTZ.CGEventCreateScrollWheelEvent(
            None, QUARTZ.kCGScrollEventUnitLine, 1, int(clicks)
        )
        return self._post(event)

    def release_all(self) -> None:
        """Release everything held, ignoring the foreground gate (see Win32Backend)."""
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

    def release_keys(self, keys: Iterable[str], buttons: Iterable[MouseButton] = ()) -> None:
        """Send key-up for ``keys`` regardless of bookkeeping (see Win32Backend)."""
        previous = self.require_foreground
        self.require_foreground = False
        try:
            for key in keys:
                self._key_event(key, False)
                self.held_keys.discard(key)
            for button in buttons:
                self._mouse_button(button, False)
                self.held_buttons.discard(button)
        finally:
            self.require_foreground = previous
