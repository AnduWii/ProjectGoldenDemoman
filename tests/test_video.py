"""Video import: reading purchases out of a recording.

Runs the real pipeline -- OpenCV decoding a lossy-compressed file -- against
synthetic matches whose truth is known exactly. That proves the pipeline; how
well it does on real TDS footage can only be learned from real footage.
"""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import numpy as np
import pytest
from video_fixture import (
    EVENTS,
    FPS,
    HEIGHT,
    WIDTH,
    _background,
    match_calibration,
    render_frame,
    write_match_video,
)

from tds_macro.input.coords import NormPoint
from tds_macro.strategy.steps import Place, Upgrade, Wait, WaitForCash, WaitForWave
from tds_macro.vision.calibration import Calibration
from tds_macro.vision.capture import NormBox
from tds_macro.vision.video import (
    PLACE,
    UPGRADE,
    Purchase,
    VideoAnalysis,
    VideoError,
    VideoInfo,
    VideoSettings,
    analyze_video,
    build_strategy,
    read_frame_at,
    write_video,
)


@pytest.fixture(scope="module")
def match_video(tmp_path_factory: pytest.TempPathFactory) -> Path:
    return write_match_video(tmp_path_factory.mktemp("video") / "match.mp4")


@pytest.fixture(scope="module")
def analysis(match_video: Path) -> VideoAnalysis:
    return analyze_video(match_video, match_calibration(), VideoSettings(sample_fps=4))


def test_every_purchase_is_found_with_its_exact_price(analysis: VideoAnalysis) -> None:
    assert analysis.readable_fraction == 1.0
    assert [p.cost for p in analysis.purchases] == [e.cost for e in EVENTS]
    assert analysis.warnings == []


def test_purchases_are_dated_to_the_moment_they_happened(analysis: VideoAnalysis) -> None:
    for purchase, event in zip(analysis.purchases, EVENTS, strict=True):
        assert abs(purchase.t - event.t) <= 0.3


def test_placements_and_upgrades_are_told_apart(analysis: VideoAnalysis) -> None:
    assert [p.kind for p in analysis.purchases] == [e.kind for e in EVENTS]
    upgrade = analysis.purchases[2]
    assert upgrade.kind == UPGRADE and upgrade.tower_id == "T1"


def test_tower_spots_are_located_within_a_percent(analysis: VideoAnalysis) -> None:
    for purchase, event in zip(analysis.purchases, EVENTS, strict=True):
        assert purchase.position is not None
        assert purchase.position.distance_to(event.position) < 0.01, purchase.describe()


def test_enemies_walking_past_are_never_taken_for_towers(analysis: VideoAnalysis) -> None:
    """Enemies move along y = 0.78 the whole time; no spot may land there."""
    for purchase in analysis.purchases:
        assert purchase.position is not None
        assert abs(purchase.position.y - 0.78) > 0.1


def test_a_tower_close_in_brightness_to_the_ground_is_still_seen(
    analysis: VideoAnalysis,
) -> None:
    """The orange tower: a grey-only comparison missed it entirely."""
    orange = analysis.purchases[1]
    assert orange.kind == PLACE and orange.cost == 350


def test_waves_are_recorded_for_each_purchase(analysis: VideoAnalysis) -> None:
    assert [p.wave for p in analysis.purchases] == [1, 2, 3, 3]


def test_thumbnails_are_kept_for_review(analysis: VideoAnalysis) -> None:
    for purchase in analysis.purchases:
        assert purchase.thumbnail is not None and purchase.thumbnail.shape[1] == 480


def test_the_strategy_waits_for_the_wave_and_enough_cash(analysis: VideoAnalysis) -> None:
    strategy = build_strategy(analysis, {200: 2, 350: 4}, "imported")
    kinds = [type(s).__name__ for s in strategy.steps]
    assert kinds == [
        "WaitForCash", "Place",
        "WaitForWave", "WaitForCash", "Place",
        "WaitForWave", "WaitForCash", "Upgrade",
        "WaitForCash", "Place",
    ]
    places = [s for s in strategy.steps if isinstance(s, Place)]
    assert [p.slot for p in places] == [2, 4, 2]
    upgrade = next(s for s in strategy.steps if isinstance(s, Upgrade))
    assert upgrade.tower_id == "T1"
    cash = [s.amount for s in strategy.steps if isinstance(s, WaitForCash)]
    assert all(amount >= price for amount, price in zip(cash, [200, 350, 150, 200], strict=True))
    assert strategy.validate() == []
    assert abs(strategy.aspect_ratio - WIDTH / HEIGHT) < 0.01


def test_without_hud_reading_the_original_timing_is_used(analysis: VideoAnalysis) -> None:
    strategy = build_strategy(analysis, {200: 1, 350: 2}, "timed", gate_on_hud=False)
    assert not any(isinstance(s, WaitForWave | WaitForCash) for s in strategy.steps)
    waits = [s.ms for s in strategy.steps if isinstance(s, Wait)]
    assert waits and abs(sum(waits) - (EVENTS[-1].t - analysis.match_start_t) * 1000) < 400
    assert strategy.validate() == []


def test_prices_that_differ_by_income_are_one_tower() -> None:
    """Income landing with a purchase makes the same tower read slightly cheap."""
    info = VideoInfo(Path("x.mp4"), 30.0, 0, 1920, 1080)
    analysis = VideoAnalysis(info=info)
    for cost in (180, 200, 195, 350, 340, 1200):
        analysis.purchases.append(
            Purchase(t=0.0, wave=1, cost=cost, cash_before=0, kind=PLACE,
                     position=NormPoint(0.5, 0.5))
        )
    assert analysis.tower_costs() == {200: 3, 350: 2, 1200: 1}
    cheap = analysis.purchases[0]
    assert analysis.price_for(cheap) == 200


def test_a_letterboxed_video_is_read_inside_its_game_area(tmp_path: Path) -> None:
    """YouTube uploads often frame the game with black bars."""
    pad_x, pad_y = 80, 45
    out_w, out_h = WIDTH + 2 * pad_x, HEIGHT + 2 * pad_y
    background = _background()

    def frames() -> Iterator[np.ndarray]:
        for index in range(int(20 * FPS)):
            canvas = np.zeros((out_h, out_w, 3), dtype=np.uint8)
            game = render_frame(index / FPS, background)
            canvas[pad_y : pad_y + HEIGHT, pad_x : pad_x + WIDTH] = game
            yield canvas

    path = write_video(tmp_path / "letterboxed.mp4", FPS, (out_w, out_h), frames)

    box = NormBox(pad_x / out_w, pad_y / out_h, WIDTH / out_w, HEIGHT / out_h)
    result = analyze_video(path, match_calibration(), VideoSettings(sample_fps=4, game_box=box))
    assert [p.cost for p in result.purchases] == [e.cost for e in EVENTS]
    first = result.purchases[0].position
    assert first is not None and first.distance_to(EVENTS[0].position) < 0.015


def test_analysis_can_be_cancelled(match_video: Path) -> None:
    seen: list[float] = []
    result = analyze_video(
        match_video,
        match_calibration(),
        VideoSettings(sample_fps=4),
        progress=seen.append,
        should_stop=lambda: len(seen) > 5,
    )
    assert "Stopped before the end" in " ".join(result.warnings)
    assert len(result.purchases) < len(EVENTS)


def test_progress_is_reported(analysis: VideoAnalysis, match_video: Path) -> None:
    seen: list[float] = []
    settings = VideoSettings(sample_fps=2)
    analyze_video(match_video, match_calibration(), settings, progress=seen.append)
    assert seen and seen[-1] > 0.9 and seen == sorted(seen)


def test_an_uncalibrated_hud_is_refused_with_instructions(match_video: Path) -> None:
    with pytest.raises(VideoError, match="wave and cash numbers"):
        analyze_video(match_video, Calibration())


def test_a_file_that_is_not_a_video_is_refused(tmp_path: Path) -> None:
    bogus = tmp_path / "notes.mp4"
    bogus.write_text("this is not a video")
    with pytest.raises(VideoError, match="convert it to an MP4"):
        analyze_video(bogus, match_calibration())


def test_frames_can_be_fetched_for_preview(match_video: Path) -> None:
    frame = read_frame_at(match_video, 5.0)
    assert frame.shape[:2] == (HEIGHT, WIDTH)
    with pytest.raises(VideoError):
        read_frame_at(match_video.with_name("missing.mp4"), 1.0)


def test_video_writing_falls_back_when_a_format_is_missing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Some Mac OpenCV builds have no FFmpeg and cannot write MPEG-4 or AVI."""
    import tds_macro.vision.video as video

    monkeypatch.setattr(video, "WRITE_FORMATS", (("XXXX", ".nope"), *video.WRITE_FORMATS))

    def frames() -> Iterator[np.ndarray]:
        for value in range(0, 250, 25):
            yield np.full((48, 64, 3), value, dtype=np.uint8)

    path = write_video(tmp_path / "clip", 10, (64, 48), frames)
    assert path.suffix != ".nope" and path.exists()
    assert not (tmp_path / "clip.nope").exists()


def test_a_build_that_cannot_write_video_says_so(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import tds_macro.vision.video as video

    monkeypatch.setattr(video, "WRITE_FORMATS", (("XXXX", ".nope"),))
    with pytest.raises(VideoError, match="cannot write and re-read video"):
        write_video(tmp_path / "clip", 10, (64, 48), lambda: iter([]))
    ok, detail = video.check_video_support(tmp_path)
    assert not ok and "decoders" in detail


def test_the_self_test_video_check_passes_here(tmp_path: Path) -> None:
    from tds_macro.vision.video import check_video_support

    ok, detail = check_video_support(tmp_path)
    assert ok, detail
    assert list(tmp_path.iterdir()) == [], "the self-test must clean up after itself"
