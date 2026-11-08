"""Live HUD reading: the engine's view of wave and cash.

Two safeguards shape this module:

* **Smoothing.** HUD numbers animate (cash ticks up, the wave counter flips),
  and a single frame caught mid-transition can read as something wrong.  A
  value is only published once the same reading appears in a majority of a
  small rolling window, so one bad frame cannot make the engine act early.
* **Unknown is not zero.**  Every failure path yields ``None`` with a reason,
  which the engine treats as "cannot verify" rather than as a satisfied gate.

Capture is throttled: the engine polls far more often than the screen changes,
and re-grabbing on every poll would burn CPU the game needs.
"""

from __future__ import annotations

import logging
from collections import deque
from collections.abc import Callable
from dataclasses import dataclass, field

import numpy as np

from ..input.coords import Rect
from ..input.window import GeometryProvider
from ..strategy.engine import GameState
from .calibration import TEMPLATE_END_SCREEN, Calibration, HudRegion
from .capture import FrameSource, crop_norm
from .digits import ReadResult, read_number
from .templates import match_template

log = logging.getLogger(__name__)


@dataclass
class Stabilizer:
    """Publishes a value only once a rolling window agrees on it.

    ``window=3, agree=2`` means a reading must show up twice in the last three
    frames.  Pure and clock-free, so the behaviour is unit-testable.
    """

    window: int = 3
    agree: int = 2
    _recent: deque[int | None] = field(default_factory=lambda: deque(maxlen=3))
    _value: int | None = None

    def __post_init__(self) -> None:
        self.window = max(1, self.window)
        self.agree = max(1, min(self.agree, self.window))
        self._recent = deque(maxlen=self.window)

    @property
    def value(self) -> int | None:
        return self._value

    def push(self, reading: int | None) -> int | None:
        self._recent.append(reading)
        if reading is None:
            # Only forget the last good value once the window is all failures,
            # so a single dropped frame does not blank the UI.
            if all(item is None for item in self._recent) and len(self._recent) >= self.window:
                self._value = None
            return self._value
        if sum(1 for item in self._recent if item == reading) >= self.agree:
            self._value = reading
        return self._value

    def reset(self) -> None:
        self._recent.clear()
        self._value = None


@dataclass
class HudSnapshot:
    """A full read, including why anything failed -- surfaced in the UI."""

    wave: int | None = None
    cash: int | None = None
    wave_raw: int | None = None
    cash_raw: int | None = None
    wave_confidence: float = 0.0
    cash_confidence: float = 0.0
    end_screen: bool = False
    end_screen_score: float = 0.0
    problems: list[str] = field(default_factory=list)
    captured_ms: int = 0

    def to_game_state(self) -> GameState:
        return GameState(
            wave=self.wave,
            cash=self.cash,
            confidence=min(self.wave_confidence, self.cash_confidence),
            end_screen=self.end_screen,
            timestamp_ms=self.captured_ms,
        )

    def describe(self) -> str:
        wave = self.wave if self.wave is not None else "--"
        cash = f"${self.cash:,}" if self.cash is not None else "$--"
        return f"Wave {wave}   {cash}"


class HudReader:
    """Reads the HUD on demand, throttled and smoothed.

    Implements the engine's ``GameStateProvider`` protocol via :meth:`snapshot`.
    """

    def __init__(
        self,
        geometry: GeometryProvider,
        frames: FrameSource,
        calibration: Calibration,
        clock: Callable[[], int] | None = None,
        interval_ms: int = 120,
        window: int = 3,
        agree: int = 2,
        detect_end_screen: bool = False,
    ) -> None:
        self.geometry = geometry
        self.frames = frames
        self.calibration = calibration
        self.clock = clock
        self.interval_ms = max(0, interval_ms)
        self.detect_end_screen = detect_end_screen
        self._wave = Stabilizer(window, agree)
        self._cash = Stabilizer(window, agree)
        self._last = HudSnapshot(problems=["HUD has not been read yet"])
        self._last_read_ms = -10**9
        self.reads = 0

    def now(self) -> int:
        if self.clock is not None:
            return self.clock()
        import time

        return int(time.monotonic() * 1000)

    def reset(self) -> None:
        """Clear smoothing -- called between matches so last match's wave 40
        cannot satisfy the next run's 'wait for wave 2'."""
        self._wave.reset()
        self._cash.reset()
        self._last = HudSnapshot(problems=["HUD has not been read yet"])
        self._last_read_ms = -10**9

    @property
    def last(self) -> HudSnapshot:
        return self._last

    def snapshot(self) -> GameState:
        return self.read().to_game_state()

    def read(self, force: bool = False) -> HudSnapshot:
        now = self.now()
        if not force and now - self._last_read_ms < self.interval_ms:
            return self._last
        self._last_read_ms = now
        self._last = self._read_now(now)
        return self._last

    def _read_now(self, now: int) -> HudSnapshot:
        snapshot = HudSnapshot(captured_ms=now)
        client = self.geometry.client_rect()
        if client is None or not client.is_valid():
            snapshot.problems.append("Roblox window is not available")
            snapshot.wave = self._wave.push(None)
            snapshot.cash = self._cash.push(None)
            return snapshot

        frame = self.frames.grab(client)
        if frame is None or frame.size == 0:
            snapshot.problems.append("screen capture returned nothing")
            snapshot.wave = self._wave.push(None)
            snapshot.cash = self._cash.push(None)
            return snapshot

        self.reads += 1
        wave_result = self._read_region(self.calibration.wave, frame, "wave", snapshot)
        cash_result = self._read_region(self.calibration.cash, frame, "cash", snapshot)
        snapshot.wave_raw = wave_result.value
        snapshot.cash_raw = cash_result.value
        snapshot.wave_confidence = wave_result.confidence
        snapshot.cash_confidence = cash_result.confidence
        snapshot.wave = self._wave.push(wave_result.value)
        snapshot.cash = self._cash.push(cash_result.value)

        if self.detect_end_screen:
            template = self.calibration.templates.get(TEMPLATE_END_SCREEN)
            if template is not None:
                outcome = match_template(frame, template)
                snapshot.end_screen = outcome.found
                snapshot.end_screen_score = outcome.score
        return snapshot

    def _read_region(
        self,
        region: HudRegion,
        frame: np.ndarray,
        name: str,
        snapshot: HudSnapshot,
    ) -> ReadResult:
        if region.box is None:
            snapshot.problems.append(f"{name} region is not calibrated")
            return ReadResult(None, "", 0.0, reason="not calibrated")
        result = read_number(crop_norm(frame, region.box), region.classifier, region.invert)
        if not result.ok and result.reason:
            snapshot.problems.append(f"{name}: {result.reason}")
        return result

    def grab_client_frame(self) -> tuple[Rect | None, np.ndarray | None]:
        """Full client-area capture, for the calibration wizard and previews."""
        client = self.geometry.client_rect()
        if client is None:
            return None, None
        return client, self.frames.grab(client)
