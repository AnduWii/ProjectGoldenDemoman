"""Global hotkeys via ``RegisterHotKey`` on a dedicated message-pump thread.

``RegisterHotKey`` with a ``NULL`` window delivers ``WM_HOTKEY`` to the
*registering thread's* message queue, so the thread that registers must also
pump messages.  Doing that on Qt's main thread would mean competing with Qt's
own event loop, so hotkeys get their own thread; callbacks fire on that thread
and the UI marshals them onto the GUI thread with a queued signal.

Unregistration is covered three ways -- ``try/finally`` in the thread body,
:func:`atexit`, and an explicit call from Qt's ``aboutToQuit`` -- because a
leaked hotkey stays claimed until the process dies, and a half-dead process
would leave F6 silently broken until the user reboots.
"""

from __future__ import annotations

import atexit
import ctypes
import logging
import sys
import threading
from collections.abc import Callable
from dataclasses import dataclass, field

from .keys import HotkeySpec, KeyParseError, parse_hotkey
from .win32_defs import IS_WINDOWS, MSG, WM_APP_SHUTDOWN, WM_HOTKEY, user32

log = logging.getLogger(__name__)


@dataclass
class HotkeyBinding:
    """One requested hotkey and what became of it."""

    name: str
    spec_text: str
    callback: Callable[[], None]
    spec: HotkeySpec | None = None
    hotkey_id: int = 0
    registered: bool = False
    error: str = ""

    def status(self) -> str:
        if self.registered and self.spec is not None:
            return f"{self.name}: {self.spec.describe()}"
        return f"{self.name}: FAILED ({self.error})"


def validate_bindings(bindings: list[HotkeyBinding], platform: str | None = None) -> list[str]:
    """Parse every binding and report the problems a user can act on.

    Catches what the OS cannot tell apart from "works": an unparseable spec,
    the same combination bound to two actions (the second registration just
    fails, leaving one silently dead key), and -- on a Mac -- a PC-only key
    such as Pause that has no key on a Mac keyboard. Shared by the Windows and
    macOS managers so both behave the same.
    """
    mac = (platform or sys.platform) == "darwin"
    problems: list[str] = []
    seen: dict[tuple[int, int], str] = {}
    for binding in bindings:
        binding.error = ""
        try:
            spec = parse_hotkey(binding.spec_text)
            if mac:
                from .mac_keys import mac_keycode

                mac_keycode(spec.key)
        except KeyParseError as exc:
            binding.spec = None
            binding.error = str(exc)
            problems.append(f"{binding.name}: {exc}")
            continue
        binding.spec = spec
        key = (spec.modifiers, spec.vk)
        if key in seen:
            message = (
                f"{binding.name} and {seen[key]} are both bound to "
                f"{spec.describe(platform)}; only one will work"
            )
            binding.error = message
            problems.append(message)
        else:
            seen[key] = binding.name
    return problems


@dataclass
class HotkeyManager:
    """Owns the hotkey thread and the set of registered bindings.

    Registration failures are collected rather than raised: one unavailable
    hotkey (something else on the system already owns F6) must not stop the app
    from starting, but the user has to be told, so :attr:`errors` is surfaced in
    the UI as a visible warning.
    """

    bindings: list[HotkeyBinding] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)
    _thread: threading.Thread | None = None
    _thread_id: int = 0
    _ready: threading.Event = field(default_factory=threading.Event)
    _stopping: bool = False
    _atexit_registered: bool = False

    def set_binding(self, name: str, spec_text: str, callback: Callable[[], None]) -> None:
        """Declare a hotkey.  Call before :meth:`start`."""
        self.bindings = [b for b in self.bindings if b.name != name]
        self.bindings.append(HotkeyBinding(name=name, spec_text=spec_text, callback=callback))

    def validate(self) -> list[str]:
        """Parse all specs and report conflicts *before* touching Win32."""
        return validate_bindings(self.bindings)

    # -- lifecycle ------------------------------------------------------------
    def start(self, timeout: float = 3.0) -> list[str]:
        """Start the hotkey thread and register everything.

        Returns the list of problems (empty on full success).
        """
        self.errors = self.validate()
        if not IS_WINDOWS or user32 is None:
            self.errors.append("global hotkeys require Windows")
            return self.errors
        if self._thread is not None and self._thread.is_alive():
            return self.errors
        self._stopping = False
        self._ready.clear()
        self._thread = threading.Thread(
            target=self._run, name="tds-hotkeys", daemon=True
        )
        self._thread.start()
        if not self._ready.wait(timeout):
            self.errors.append("hotkey thread did not start in time")
        if not self._atexit_registered:
            atexit.register(self.stop)
            self._atexit_registered = True
        self.errors.extend(b.error for b in self.bindings if b.error and b.error not in self.errors)
        return self.errors

    def stop(self) -> None:
        """Ask the thread to unregister and exit; safe to call repeatedly."""
        if self._stopping:
            return
        self._stopping = True
        thread, thread_id = self._thread, self._thread_id
        if IS_WINDOWS and user32 is not None and thread_id:
            user32.PostThreadMessageW(thread_id, WM_APP_SHUTDOWN, 0, 0)
        if thread is not None and thread.is_alive():
            thread.join(timeout=2.0)
            if thread.is_alive():  # pragma: no cover - Windows only
                log.warning("hotkey thread did not exit; hotkeys may stay claimed")
        self._thread = None
        self._thread_id = 0

    def active_bindings(self) -> list[HotkeyBinding]:
        return [b for b in self.bindings if b.registered]

    # -- thread body ----------------------------------------------------------
    def _run(self) -> None:  # pragma: no cover - Windows only
        assert user32 is not None
        self._thread_id = int(ctypes.windll.kernel32.GetCurrentThreadId())
        registered: list[HotkeyBinding] = []
        try:
            for index, binding in enumerate(self.bindings, start=1):
                if binding.spec is None:
                    continue
                binding.hotkey_id = index
                ok = user32.RegisterHotKey(
                    None, index, binding.spec.register_modifiers, binding.spec.vk
                )
                if ok:
                    binding.registered = True
                    registered.append(binding)
                    log.info("registered hotkey %s", binding.status())
                else:
                    code = ctypes.get_last_error()
                    binding.error = (
                        f"{binding.spec.describe()} is already in use by another "
                        f"program (WinError {code}) -- pick a different key"
                    )
                    log.warning("%s", binding.error)
            self._ready.set()
            self._pump(registered)
        finally:
            self._ready.set()
            for binding in registered:
                try:
                    user32.UnregisterHotKey(None, binding.hotkey_id)
                    binding.registered = False
                except OSError as exc:
                    log.warning("failed to unregister %s: %s", binding.name, exc)
            log.info("hotkey thread exited; %d hotkeys released", len(registered))

    def _pump(self, registered: list[HotkeyBinding]) -> None:  # pragma: no cover
        assert user32 is not None
        by_id = {b.hotkey_id: b for b in registered}
        message = MSG()
        while True:
            result = user32.GetMessageW(ctypes.byref(message), None, 0, 0)
            if result in (0, -1):
                return
            if message.message == WM_APP_SHUTDOWN:
                return
            if message.message == WM_HOTKEY:
                binding = by_id.get(int(message.wParam))
                if binding is None:
                    continue
                try:
                    binding.callback()
                except Exception:
                    # A raising callback must never kill the hotkey thread --
                    # that would take the emergency stop down with it.
                    log.exception("hotkey callback %s raised", binding.name)
