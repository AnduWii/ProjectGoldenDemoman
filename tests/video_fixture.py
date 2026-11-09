"""A synthetic Tower Defense match, written as a real video file.

Deliberately not a toy: the map is textured, enemies walk across it the whole
time (so "something changed" alone would point at them), cash ticks up with
income between purchases, and the video is compressed with a lossy codec. What
it cannot do is look like TDS -- it proves the pipeline, not TDS realism.
"""

from __future__ import annotations

from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path

import cv2
import numpy as np
from test_digits import FONT, trained_classifier

from tds_macro.input.coords import NormPoint
from tds_macro.vision.calibration import Calibration
from tds_macro.vision.capture import NormBox
from tds_macro.vision.video import write_video

WIDTH, HEIGHT, FPS = 640, 360, 10
SCALE = 3
WAVE_BOX = NormBox(0.45, 0.02, 0.12, 0.09)
CASH_BOX = NormBox(0.03, 0.87, 0.22, 0.09)


@dataclass(frozen=True)
class Event:
    t: float
    kind: str  # "place" or "upgrade"
    cost: int
    position: NormPoint
    colour: tuple[int, int, int]


# Purchases fall between income ticks (which land on whole seconds), as most
# real ones do. The orange tower is deliberately close in brightness to the
# ground: a grey-only comparison does not see it.
EVENTS = [
    Event(3.4, "place", 200, NormPoint(0.30, 0.40), (40, 200, 240)),
    Event(8.4, "place", 350, NormPoint(0.66, 0.36), (40, 120, 240)),
    Event(12.4, "upgrade", 150, NormPoint(0.30, 0.40), (40, 40, 250)),
    Event(16.4, "place", 200, NormPoint(0.46, 0.58), (40, 200, 240)),
]
DURATION = 20.0
WAVE_EVERY = 6.0
START_CASH = 600
INCOME_PER_S = 20


def wave_at(t: float) -> int:
    return 1 + int(t // WAVE_EVERY)


def cash_at(t: float) -> int:
    spent = sum(e.cost for e in EVENTS if e.t <= t)
    return START_CASH + int(t) * INCOME_PER_S - spent


def _draw_text(frame: np.ndarray, text: str, box: NormBox) -> None:
    left = int(box.x * WIDTH) + 3
    top = int(box.y * HEIGHT) + 4
    for char in text:
        rows = FONT[char]
        for r, row in enumerate(rows):
            for c, cell in enumerate(row):
                if cell == "#":
                    y, x = top + r * SCALE, left + c * SCALE
                    frame[y : y + SCALE, x : x + SCALE] = 245
        left += 3 * SCALE + 3


def _background() -> np.ndarray:
    rng = np.random.default_rng(5)
    noise = rng.integers(0, 255, (HEIGHT // 8, WIDTH // 8, 3), dtype=np.uint8)
    base = cv2.resize(noise, (WIDTH, HEIGHT), interpolation=cv2.INTER_CUBIC)
    base = (base * 0.35 + 50).astype(np.uint8)
    # A dark strip behind each readout, as a real HUD has.
    for box in (WAVE_BOX, CASH_BOX):
        x0, y0 = int(box.x * WIDTH), int(box.y * HEIGHT)
        x1, y1 = int((box.x + box.width) * WIDTH), int((box.y + box.height) * HEIGHT)
        base[y0:y1, x0:x1] = 15
    return base


def render_frame(t: float, background: np.ndarray) -> np.ndarray:
    frame = background.copy()
    # Enemies walking along the path the whole time.
    for i in range(4):
        x = int(((t * 60 + i * 170) % (WIDTH + 40)) - 20)
        cv2.circle(frame, (x, int(HEIGHT * 0.78)), 9, (60, 60, 220), -1)
    # Towers that exist at time t, in their current colour.
    towers: dict[tuple[float, float], tuple[int, int, int]] = {}
    for event in EVENTS:
        if event.t <= t:
            towers[event.position.as_tuple()] = event.colour
    for (x, y), colour in towers.items():
        cx, cy = int(x * WIDTH), int(y * HEIGHT)
        cv2.rectangle(frame, (cx - 11, cy - 11), (cx + 11, cy + 11), colour, -1)
    _draw_text(frame, str(wave_at(t)), WAVE_BOX)
    _draw_text(frame, f"${cash_at(t):,}", CASH_BOX)
    return frame


def write_match_video(path: Path) -> Path:
    """Write the synthetic match; returns the file actually written, whose
    extension depends on which formats this OpenCV build can write."""
    background = _background()

    def frames() -> Iterator[np.ndarray]:
        for index in range(int(DURATION * FPS)):
            yield render_frame(index / FPS, background)

    return write_video(path, FPS, (WIDTH, HEIGHT), frames)


def match_calibration() -> Calibration:
    calibration = Calibration(client_size=(WIDTH, HEIGHT))
    calibration.wave.box = WAVE_BOX
    calibration.cash.box = CASH_BOX
    calibration.wave.classifier = trained_classifier(SCALE)
    calibration.cash.classifier = trained_classifier(SCALE)
    return calibration
