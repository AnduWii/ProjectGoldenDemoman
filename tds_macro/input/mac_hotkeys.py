"""Global hotkeys on macOS, through a Quartz event tap.

Windows has RegisterHotKey; macOS's closest public equivalent for an app like
this is an event tap that watches key-downs and matches them. Same public
interface as :class:`~tds_macro.input.hotkeys.HotkeyManager`, so the
controller does not care which it holds.

Differences worth knowing:

* There is no registration that can *fail*, so a combination another app
  already uses cannot be detected -- both will react. Duplicates within this
  app are still caught by the shared validation.
* With the Accessibility permission the tap is *active* and swallows the
  hotkey, so Roblox never sees it. Without it, macOS only allows a listen-only
  tap and Roblox receives the keystroke too.
"""

from __future__ import annotations

import atexit
import logging
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from .hotkeys import HotkeyBinding, validate_bindings
from .mac_keys import hotkey_matches
from .mac_quartz import QUARTZ, EventTapThread, is_autorepeat, is_our_event

log = logging.getLogger(__name__)


@dataclass
class MacHotkeyManager:
    bindings: list[HotkeyBinding] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)
    _tap: EventTapThread | None = None
    _atexit_registered: bool = False

    def set_binding(self, name: str, spec_text: str, callback: Callable[[], None]) -> None:
        self.bindings = [b for b in self.bindings if b.name != name]
        self.bindings.append(HotkeyBinding(name=name, spec_text=spec_text, callback=callback))

    def validate(self) -> list[str]:
        return validate_bindings(self.bindings, "darwin")

    def start(self, timeout: float = 3.0) -> list[str]:
        self.errors = self.validate()
        if QUARTZ is None:
            self.errors.append("global hotkeys need the macOS frameworks (pyobjc)")
            return self.errors
        if self._tap is not None and self._tap.running:
            return self.errors
        self._tap = EventTapThread(
            "tds-hotkeys",
            [int(QUARTZ.kCGEventKeyDown)],
            self._on_key,
            want_filter=True,
        )
        error = self._tap.start(timeout)
        if error:
            self.errors.append(error)
            self._tap = None
            return self.errors
        for binding in self.bindings:
            binding.registered = binding.spec is not None and not binding.error
        if self._tap.mode == "listen":
            self.errors.append(
                "Hotkeys work, but without the Accessibility permission Roblox also "
                "receives the hotkey keystrokes."
            )
        if not self._atexit_registered:
            atexit.register(self.stop)
            self._atexit_registered = True
        return self.errors

    def stop(self) -> None:
        tap, self._tap = self._tap, None
        if tap is not None:
            tap.stop()
        for binding in self.bindings:
            binding.registered = False

    def active_bindings(self) -> list[HotkeyBinding]:
        return [b for b in self.bindings if b.registered]

    def _on_key(self, event_type: int, event: Any) -> bool:  # pragma: no cover - macOS only
        """Runs on the tap thread. True swallows the keystroke."""
        del event_type
        if is_our_event(event) or is_autorepeat(event):
            # Auto-repeat is ignored for the same reason Windows uses
            # MOD_NOREPEAT: holding the key must not queue repeated toggles.
            return False
        keycode = int(QUARTZ.CGEventGetIntegerValueField(event, QUARTZ.kCGKeyboardEventKeycode))
        flags = int(QUARTZ.CGEventGetFlags(event))
        for binding in self.bindings:
            spec = binding.spec
            if binding.registered and spec is not None and hotkey_matches(spec, keycode, flags):
                try:
                    binding.callback()
                except Exception:
                    log.exception("hotkey callback %s raised", binding.name)
                return True
        return False
