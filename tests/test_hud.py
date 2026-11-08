"""HUD reading: smoothing, throttling and failure reporting."""

from __future__ import annotations

import numpy as np
import pytest
from conftest import FakeClock
from test_digits import render, trained_classifier

from tds_macro.input.coords import Rect
from tds_macro.vision.calibration import TEMPLATE_END_SCREEN, Calibration
from tds_macro.vision.capture import NormBox
from tds_macro.vision.hud import HudReader, Stabilizer
from tds_macro.vision.templates import capture_template


class FakeGeometry:
    def __init__(self, rect: Rect | None = Rect(0, 0, 400, 300)) -> None:
        self.rect = rect

    def client_rect(self) -> Rect | None:
        return self.rect

    def virtual_screen(self):  # pragma: no cover - unused here
        raise NotImplementedError

    def is_foreground(self) -> bool:
        return True

    def refresh(self) -> bool:
        return self.rect is not None

    def describe(self) -> str:
        return "fake"


class FakeFrames:
    """Serves whatever image the test sets, counting grabs."""

    def __init__(self, image: np.ndarray | None) -> None:
        self.image = image
        self.grabs = 0

    def grab(self, rect: Rect):
        self.grabs += 1
        return self.image

    def close(self) -> None:
        return None


def hud_frame(wave: str, cash: str, size: tuple[int, int] = (300, 400)) -> np.ndarray:
    """Build a client-area frame with a wave and a cash readout placed in it."""
    frame = np.zeros((size[0], size[1], 4), dtype=np.uint8)
    frame[:, :, 3] = 255
    wave_img = render(wave)
    cash_img = render(cash)
    frame[10 : 10 + wave_img.shape[0], 150 : 150 + wave_img.shape[1]] = wave_img
    frame[250 : 250 + cash_img.shape[0], 20 : 20 + cash_img.shape[1]] = cash_img
    return frame


def build_calibration(frame: np.ndarray) -> Calibration:
    height, width = frame.shape[:2]
    wave_img = render("8")
    cash_img = render("8")
    calibration = Calibration(client_size=(width, height))
    calibration.wave.box = NormBox(
        150 / width, 10 / height, (wave_img.shape[1] + 60) / width, wave_img.shape[0] / height
    )
    calibration.cash.box = NormBox(
        20 / width, 250 / height, (cash_img.shape[1] + 120) / width, cash_img.shape[0] / height
    )
    calibration.wave.classifier = trained_classifier()
    calibration.cash.classifier = trained_classifier()
    return calibration


@pytest.fixture
def reader_parts():
    frame = hud_frame("12", "$1,250")
    calibration = build_calibration(frame)
    frames = FakeFrames(frame)
    clock = FakeClock()
    geometry = FakeGeometry(Rect(0, 0, frame.shape[1], frame.shape[0]))
    reader = HudReader(geometry, frames, calibration, clock=clock, interval_ms=100, agree=1)
    return reader, frames, clock, geometry, calibration


def test_reads_wave_and_cash_from_a_calibrated_frame(reader_parts) -> None:
    reader, *_ = reader_parts
    snapshot = reader.read(force=True)
    assert snapshot.wave == 12, snapshot.problems
    assert snapshot.cash == 1250, snapshot.problems
    assert snapshot.describe() == "Wave 12   $1,250"


def test_reads_are_throttled_so_polling_does_not_hammer_the_screen(reader_parts) -> None:
    reader, frames, clock, *_ = reader_parts
    reader.read(force=True)
    for _ in range(19):  # 95 ms of polling, inside the 100 ms interval
        reader.read()
        clock.advance(5)
    assert frames.grabs == 1, "polling inside the interval must reuse the last read"

    clock.advance(10)  # now past the interval
    reader.read()
    assert frames.grabs == 2


def test_force_bypasses_the_throttle(reader_parts) -> None:
    reader, frames, *_ = reader_parts
    reader.read(force=True)
    reader.read(force=True)
    assert frames.grabs == 2


def test_missing_window_reports_a_problem_rather_than_a_value(reader_parts) -> None:
    reader, _, _, geometry, _ = reader_parts
    geometry.rect = None
    snapshot = reader.read(force=True)
    assert snapshot.wave is None and snapshot.cash is None
    assert "window is not available" in " ".join(snapshot.problems)


def test_failed_capture_reports_a_problem(reader_parts) -> None:
    reader, frames, *_ = reader_parts
    frames.image = None
    snapshot = reader.read(force=True)
    assert snapshot.wave is None
    assert "capture returned nothing" in " ".join(snapshot.problems)


def test_uncalibrated_regions_are_named_in_the_problem_list() -> None:
    frame = hud_frame("3", "$5")
    geometry = FakeGeometry(Rect(0, 0, frame.shape[1], frame.shape[0]))
    reader = HudReader(geometry, FakeFrames(frame), Calibration(), clock=FakeClock())
    snapshot = reader.read(force=True)
    assert "wave region is not calibrated" in snapshot.problems
    assert "cash region is not calibrated" in snapshot.problems


def test_snapshot_adapts_to_the_engine_game_state(reader_parts) -> None:
    reader, *_ = reader_parts
    state = reader.snapshot()
    assert (state.wave, state.cash) == (12, 1250)
    assert state.confidence > 0.9


def test_reset_clears_the_previous_match_reading(reader_parts) -> None:
    reader, *_ = reader_parts
    reader.read(force=True)
    reader.reset()
    assert reader.last.wave is None
    assert "not been read" in " ".join(reader.last.problems)


def test_end_screen_detection_is_off_unless_requested() -> None:
    frame = hud_frame("5", "$10")
    calibration = build_calibration(frame)
    calibration.templates[TEMPLATE_END_SCREEN] = capture_template(
        frame, NormBox(0.3, 0.1, 0.2, 0.1), TEMPLATE_END_SCREEN
    )
    geometry = FakeGeometry(Rect(0, 0, frame.shape[1], frame.shape[0]))

    quiet = HudReader(geometry, FakeFrames(frame), calibration, clock=FakeClock())
    assert quiet.read(force=True).end_screen is False

    watching = HudReader(
        geometry, FakeFrames(frame), calibration, clock=FakeClock(), detect_end_screen=True
    )
    assert watching.read(force=True).end_screen is True


# --- smoothing -----------------------------------------------------------------
def test_stabilizer_needs_agreement_before_publishing() -> None:
    s = Stabilizer(window=3, agree=2)
    assert s.push(10) is None  # one sighting is not enough
    assert s.push(10) == 10
    assert s.push(11) == 10  # a single disagreeing frame does not move it
    assert s.push(11) == 11


def test_stabilizer_ignores_a_single_dropped_frame() -> None:
    s = Stabilizer(window=3, agree=2)
    s.push(7)
    s.push(7)
    assert s.push(None) == 7
    assert s.push(7) == 7


def test_stabilizer_gives_up_after_a_full_window_of_failures() -> None:
    s = Stabilizer(window=3, agree=2)
    s.push(7)
    s.push(7)
    assert s.push(None) == 7
    assert s.push(None) == 7
    assert s.push(None) is None


def test_a_single_misread_frame_cannot_trigger_a_wave_gate() -> None:
    """The failure this exists to prevent: one bad frame reading 12 while the
    game is on 11 would make the engine place towers a wave early."""
    s = Stabilizer(window=3, agree=2)
    for _ in range(3):
        s.push(11)
    assert s.push(12) == 11
    assert s.value == 11
