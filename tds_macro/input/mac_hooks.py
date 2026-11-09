"""Observing input on macOS while recording.

The macOS counterpart of :class:`~tds_macro.input.hooks.RawInputTap`: a
listen-only event tap that never blocks or alters the user's input, queues what
it sees, and converts positions on the consumer's thread in :meth:`drain`.
Needs the Input Monitoring permission.
"""

from __future__ import annotations

import logging
import queue
from typing import Any

from .events import DragTracker, RawEvent, RawKind, WheelAccumulator, build_raw_event
from .mac_keys import FLAG_COMMAND, FLAG_CONTROL, FLAG_OPTION, mac_key_name
from .mac_quartz import QUARTZ, EventTapThread, is_autorepeat, is_our_event, monotonic_ms
from .window import GeometryProvider

log = logging.getLogger(__name__)

SHORTCUT_FLAGS = FLAG_CONTROL | FLAG_OPTION | FLAG_COMMAND

QueuedEvent = tuple[int, RawKind, str | None, str | None, tuple[int, int] | None, int]


def _event_kinds() -> dict[int, tuple[RawKind, str | None]]:  # pragma: no cover
    q = QUARTZ
    return {
        int(q.kCGEventKeyDown): (RawKind.KEY_DOWN, None),
        int(q.kCGEventKeyUp): (RawKind.KEY_UP, None),
        int(q.kCGEventLeftMouseDown): (RawKind.MOUSE_DOWN, "left"),
        int(q.kCGEventLeftMouseUp): (RawKind.MOUSE_UP, "left"),
        int(q.kCGEventRightMouseDown): (RawKind.MOUSE_DOWN, "right"),
        int(q.kCGEventRightMouseUp): (RawKind.MOUSE_UP, "right"),
        int(q.kCGEventOtherMouseDown): (RawKind.MOUSE_DOWN, "middle"),
        int(q.kCGEventOtherMouseUp): (RawKind.MOUSE_UP, "middle"),
        # Movement is only delivered as "dragged" while a button is held, so
        # ordinary pointer movement never reaches the queue.
        int(q.kCGEventLeftMouseDragged): (RawKind.MOUSE_MOVE, "left"),
        int(q.kCGEventRightMouseDragged): (RawKind.MOUSE_MOVE, "right"),
        int(q.kCGEventOtherMouseDragged): (RawKind.MOUSE_MOVE, "middle"),
        int(q.kCGEventScrollWheel): (RawKind.SCROLL, None),
    }


class MacRawInputTap:
    """Queues the user's keys and clicks while Roblox is the active app."""

    def __init__(self, geometry: GeometryProvider, only_when_foreground: bool = True) -> None:
        self.geometry = geometry
        self.only_when_foreground = only_when_foreground
        self.error = ""
        self._queue: queue.Queue[QueuedEvent] = queue.Queue(maxsize=4096)
        self._tap: EventTapThread | None = None
        self._kinds: dict[int, tuple[RawKind, str | None]] = {}
        self._drag = DragTracker()
        # macOS reports wheel movement in fractional lines (trackpads send
        # many small fractions, including the momentum after a swipe).
        self._wheel = WheelAccumulator(1.0)

    @property
    def running(self) -> bool:
        return self._tap is not None and self._tap.running

    def start(self, timeout: float = 3.0) -> str:
        if QUARTZ is None:
            self.error = "recording needs the macOS frameworks (pyobjc)"
            return self.error
        if self.running:
            return ""
        self._kinds = _event_kinds()
        self._drag.release()
        self._wheel.reset()
        self._tap = EventTapThread(
            "tds-input-tap", list(self._kinds), self._on_event, want_filter=False
        )
        self.error = self._tap.start(timeout)
        if self.error:
            self._tap = None
        return self.error

    def stop(self) -> None:
        tap, self._tap = self._tap, None
        if tap is not None:
            tap.stop()

    def drain(self) -> list[RawEvent]:
        client = self.geometry.client_rect()
        events: list[RawEvent] = []
        while True:
            try:
                t_ms, kind, key, button, screen_pos, delta = self._queue.get_nowait()
            except queue.Empty:
                break
            events.append(build_raw_event(t_ms, kind, key, button, screen_pos, client, delta))
        return events

    def _on_event(self, event_type: int, event: Any) -> bool:  # pragma: no cover - macOS
        """Runs on the tap thread: classify and queue, nothing slow."""
        entry = self._kinds.get(event_type)
        if entry is None or is_our_event(event):
            return False
        kind, button = entry
        if kind is RawKind.KEY_DOWN and is_autorepeat(event):
            return False
        key: str | None = None
        screen_pos: tuple[int, int] | None = None
        delta = 0
        if kind not in (RawKind.KEY_DOWN, RawKind.KEY_UP):
            where = QUARTZ.CGEventGetLocation(event)
            screen_pos = (int(round(where.x)), int(round(where.y)))
            # Drag state is kept up to date even while Roblox is not in front.
            if kind is RawKind.MOUSE_DOWN:
                self._drag.press(screen_pos)
            elif kind is RawKind.MOUSE_UP:
                self._drag.release()
            elif kind is RawKind.MOUSE_MOVE:
                # Roblox locks the cursor while the camera turns, so the
                # location stands still; the movement is in the delta fields.
                screen_pos = self._drag.move_by(
                    float(QUARTZ.CGEventGetIntegerValueField(event, QUARTZ.kCGMouseEventDeltaX)),
                    float(QUARTZ.CGEventGetIntegerValueField(event, QUARTZ.kCGMouseEventDeltaY)),
                    (where.x, where.y),
                )
            elif kind is RawKind.SCROLL:
                # Fractional lines on the vertical axis; positive is up.
                delta = self._wheel.add(
                    float(
                        QUARTZ.CGEventGetDoubleValueField(
                            event, QUARTZ.kCGScrollWheelEventFixedPtDeltaAxis1
                        )
                    )
                )
                if delta == 0:
                    return False
        if self.only_when_foreground and not self.geometry.is_foreground():
            return False
        if kind in (RawKind.KEY_DOWN, RawKind.KEY_UP):
            # On macOS, Ctrl/Option/Cmd never arrive as key presses (they are
            # "flags changed" events), so the recorder cannot see them held.
            # Drop shortcut presses here instead -- otherwise the Ctrl+Option+R
            # that stops recording would itself be recorded as an R.
            flags = int(QUARTZ.CGEventGetFlags(event))
            if kind is RawKind.KEY_DOWN and flags & SHORTCUT_FLAGS:
                return False
            keycode = int(QUARTZ.CGEventGetIntegerValueField(event, QUARTZ.kCGKeyboardEventKeycode))
            key = mac_key_name(keycode)
            if key is None:
                return False
        try:
            self._queue.put_nowait((monotonic_ms(), kind, key, button, screen_pos, delta))
        except queue.Full:
            log.warning("input tap queue full; dropping event")
        return False
