"""Low-level keyboard/mouse hooks used only while recording.

``WH_KEYBOARD_LL``/``WH_MOUSE_LL`` are the documented way to observe input
system-wide without elevation.  They observe only; nothing is swallowed, so the
user's clicks reach Roblox normally while being recorded.

Two constraints shape this module:

* The hook procedure runs on the thread that installed the hook, and that
  thread must pump messages -- hence a dedicated thread, as with hotkeys.
* Windows kills a hook that is slow to return (``LowLevelHooksTimeout``), so the
  callback does the absolute minimum: push a tuple onto a queue.  Conversion to
  normalized coordinates happens on the consumer thread in :meth:`drain`.
"""

from __future__ import annotations

import atexit
import ctypes
import logging
import queue
import threading
import time

from .events import RawEvent, RawKind, build_raw_event
from .keys import vk_to_key
from .win32_defs import (
    HOOKPROC,
    IS_WINDOWS,
    KBDLLHOOKSTRUCT,
    LLKHF_INJECTED,
    MSG,
    MSLLHOOKSTRUCT,
    TDS_MACRO_SIGNATURE,
    WH_KEYBOARD_LL,
    WH_MOUSE_LL,
    WM_APP_SHUTDOWN,
    WM_KEYDOWN,
    WM_KEYUP,
    WM_LBUTTONDOWN,
    WM_LBUTTONUP,
    WM_MBUTTONDOWN,
    WM_MBUTTONUP,
    WM_MOUSEMOVE,
    WM_MOUSEWHEEL,
    WM_RBUTTONDOWN,
    WM_RBUTTONUP,
    WM_SYSKEYDOWN,
    WM_SYSKEYUP,
    user32,
)
from .window import GeometryProvider

log = logging.getLogger(__name__)

_MOUSE_MESSAGES: dict[int, tuple[RawKind, str]] = {
    WM_LBUTTONDOWN: (RawKind.MOUSE_DOWN, "left"),
    WM_LBUTTONUP: (RawKind.MOUSE_UP, "left"),
    WM_RBUTTONDOWN: (RawKind.MOUSE_DOWN, "right"),
    WM_RBUTTONUP: (RawKind.MOUSE_UP, "right"),
    WM_MBUTTONDOWN: (RawKind.MOUSE_DOWN, "middle"),
    WM_MBUTTONUP: (RawKind.MOUSE_UP, "middle"),
}
_KEY_MESSAGES: dict[int, RawKind] = {
    WM_KEYDOWN: RawKind.KEY_DOWN,
    WM_SYSKEYDOWN: RawKind.KEY_DOWN,
    WM_KEYUP: RawKind.KEY_UP,
    WM_SYSKEYUP: RawKind.KEY_UP,
}


class RawInputTap:
    """Observes global input while recording and queues it for the recorder."""

    def __init__(self, geometry: GeometryProvider, only_when_foreground: bool = True) -> None:
        self.geometry = geometry
        self.only_when_foreground = only_when_foreground
        self.error: str = ""
        QueuedEvent = tuple[
            int, RawKind, int | None, str | None, tuple[int, int] | None, int
        ]
        self._queue: queue.Queue[QueuedEvent] = queue.Queue(maxsize=4096)
        self._thread: threading.Thread | None = None
        self._thread_id = 0
        self._ready = threading.Event()
        self._running = False
        # Keeping strong references is mandatory: if these are garbage
        # collected while the hook is installed, Windows calls into freed
        # memory and the process dies.
        self._keyboard_proc: object = None
        self._mouse_proc: object = None
        # Buttons the user is holding, so pointer movement is only forwarded
        # during a drag rather than flooding the queue the whole time.
        self._held: set[str] = set()
        self._atexit_registered = False

    @property
    def running(self) -> bool:
        return self._running

    def start(self, timeout: float = 3.0) -> str:
        """Install the hooks.  Returns an empty string on success."""
        if not IS_WINDOWS or user32 is None:
            self.error = "recording requires Windows"
            return self.error
        if self._running:
            return ""
        self.error = ""
        self._ready.clear()
        self._running = True
        self._thread = threading.Thread(target=self._run, name="tds-input-tap", daemon=True)
        self._thread.start()
        if not self._ready.wait(timeout):
            self.error = "input hook thread did not start in time"
        if not self._atexit_registered:
            atexit.register(self.stop)
            self._atexit_registered = True
        return self.error

    def stop(self) -> None:
        if not self._running:
            return
        self._running = False
        if IS_WINDOWS and user32 is not None and self._thread_id:
            user32.PostThreadMessageW(self._thread_id, WM_APP_SHUTDOWN, 0, 0)
        if self._thread is not None:
            self._thread.join(timeout=2.0)
        self._thread = None
        self._thread_id = 0

    def drain(self) -> list[RawEvent]:
        """Pop everything queued so far, converting to normalized coordinates.

        Runs on the caller's thread (never inside a hook) so that a slow
        ``GetClientRect`` can never trip the low-level hook timeout.
        """
        client = self.geometry.client_rect()
        events: list[RawEvent] = []
        while True:
            try:
                t_ms, kind, vk, button, screen_pos, delta = self._queue.get_nowait()
            except queue.Empty:
                break
            key = vk_to_key(vk) if vk is not None else None
            events.append(build_raw_event(t_ms, kind, key, button, screen_pos, client, delta))
        return events

    # -- thread body ----------------------------------------------------------
    def _run(self) -> None:  # pragma: no cover - Windows only
        assert user32 is not None
        self._thread_id = int(ctypes.windll.kernel32.GetCurrentThreadId())
        keyboard_hook = mouse_hook = None
        try:
            self._keyboard_proc = HOOKPROC(self._on_keyboard)
            self._mouse_proc = HOOKPROC(self._on_mouse)
            keyboard_hook = user32.SetWindowsHookExW(
                WH_KEYBOARD_LL, self._keyboard_proc, None, 0
            )
            mouse_hook = user32.SetWindowsHookExW(WH_MOUSE_LL, self._mouse_proc, None, 0)
            if not keyboard_hook or not mouse_hook:
                code = ctypes.get_last_error()
                self.error = f"could not install input hooks (WinError {code})"
                log.error("%s", self.error)
                return
            self._ready.set()
            message = MSG()
            while self._running:
                result = user32.GetMessageW(ctypes.byref(message), None, 0, 0)
                if result in (0, -1) or message.message == WM_APP_SHUTDOWN:
                    break
        finally:
            self._ready.set()
            for hook in (keyboard_hook, mouse_hook):
                if hook:
                    user32.UnhookWindowsHookEx(hook)
            self._keyboard_proc = None
            self._mouse_proc = None
            log.info("input tap stopped and hooks removed")

    def _accepting(self) -> bool:  # pragma: no cover - Windows only
        if not self._running:
            return False
        return not (self.only_when_foreground and not self.geometry.is_foreground())

    def _push(
        self,
        kind: RawKind,
        vk: int | None,
        button: str | None,
        screen_pos: tuple[int, int] | None,
        delta: int = 0,
    ) -> None:  # pragma: no cover - Windows only
        try:
            self._queue.put_nowait(
                (int(time.monotonic() * 1000), kind, vk, button, screen_pos, delta)
            )
        except queue.Full:
            log.warning("input tap queue full; dropping event")

    def _on_keyboard(self, code: int, wparam: int, lparam: int) -> int:  # pragma: no cover
        assert user32 is not None
        if code == 0 and self._accepting():
            data = ctypes.cast(lparam, ctypes.POINTER(KBDLLHOOKSTRUCT)).contents
            injected = bool(data.flags & LLKHF_INJECTED)
            ours = data.dwExtraInfo == TDS_MACRO_SIGNATURE
            kind = _KEY_MESSAGES.get(int(wparam))
            if kind is not None and not (injected and ours):
                self._push(kind, int(data.vkCode), None, None)
        return int(user32.CallNextHookEx(None, code, wparam, lparam))

    def _on_mouse(self, code: int, wparam: int, lparam: int) -> int:  # pragma: no cover
        assert user32 is not None
        if code == 0 and self._accepting():
            data = ctypes.cast(lparam, ctypes.POINTER(MSLLHOOKSTRUCT)).contents
            if data.dwExtraInfo != TDS_MACRO_SIGNATURE:
                message = int(wparam)
                where = (int(data.pt.x), int(data.pt.y))
                entry = _MOUSE_MESSAGES.get(message)
                if entry is not None:
                    kind, button = entry
                    if kind is RawKind.MOUSE_DOWN:
                        self._held.add(button)
                    else:
                        self._held.discard(button)
                    self._push(kind, None, button, where)
                elif message == WM_MOUSEMOVE and self._held:
                    self._push(RawKind.MOUSE_MOVE, None, next(iter(self._held)), where)
                elif message == WM_MOUSEWHEEL:
                    # The wheel delta is the signed high word of mouseData.
                    raw = (int(data.mouseData) >> 16) & 0xFFFF
                    signed = raw - 0x10000 if raw & 0x8000 else raw
                    notches = signed // 120 if signed % 120 == 0 else (1 if signed > 0 else -1)
                    if notches:
                        self._push(RawKind.SCROLL, None, None, where, notches)
        return int(user32.CallNextHookEx(None, code, wparam, lparam))
