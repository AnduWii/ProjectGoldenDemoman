"""The video import window, driven end to end on a synthetic match.

Offscreen Qt, the real analysis on its real worker thread, and the real
hand-over to the controller -- the path a user takes from "Import a video..."
to a strategy they can press Play on.
"""

from __future__ import annotations

import json
import os
import time
from pathlib import Path

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
pytest.importorskip("PySide6", reason="PySide6 is not installed")

from PySide6.QtWidgets import QApplication  # noqa: E402
from video_fixture import (  # noqa: E402
    CASH_BOX,
    EVENTS,
    WAVE_BOX,
    cash_at,
    match_calibration,
    wave_at,
    write_match_video,
)

from tds_macro.config import SettingsStore  # noqa: E402
from tds_macro.input.coords import NormPoint  # noqa: E402
from tds_macro.strategy.steps import Place, Strategy, Upgrade, WaitForCash  # noqa: E402
from tds_macro.ui.controller import AppController  # noqa: E402
from tds_macro.ui.video_import import VideoHudDialog, VideoImportDialog  # noqa: E402
from tds_macro.vision.calibration import Calibration  # noqa: E402
from tds_macro.vision.capture import crop_norm  # noqa: E402
from tds_macro.vision.digits import read_number  # noqa: E402
from tds_macro.vision.video import PLACE, SKIP, UPGRADE, WHOLE_FRAME, VideoInfo  # noqa: E402


@pytest.fixture(scope="module")
def qt_app() -> QApplication:
    return QApplication.instance() or QApplication([])


@pytest.fixture(scope="module")
def match_video(tmp_path_factory: pytest.TempPathFactory) -> Path:
    return write_match_video(tmp_path_factory.mktemp("video") / "match.mp4")


@pytest.fixture
def controller(qt_app: QApplication, tmp_path: Path) -> AppController:
    made = AppController(SettingsStore(tmp_path))
    yield made
    made.shutdown()
    qt_app.processEvents()


@pytest.fixture
def dialog(controller: AppController, qt_app: QApplication) -> VideoImportDialog:
    built = VideoImportDialog(controller)
    yield built
    built.hide()
    built.deleteLater()
    qt_app.processEvents()


def analysed(dialog: VideoImportDialog, video: Path) -> VideoImportDialog:
    assert dialog.load_video(video)
    dialog.calibration = match_calibration()
    dialog.run_analysis_now()
    assert dialog.analysis is not None
    return dialog


def test_choosing_a_video_shows_a_frame_and_says_what_is_missing(
    dialog: VideoImportDialog, match_video: Path
) -> None:
    assert not dialog.analyze_button.isEnabled()
    assert dialog.load_video(match_video)
    assert dialog.info is not None and dialog.frame is not None
    assert dialog.analyze_button.isEnabled()
    assert "match.mp4" in dialog.file_label.text()
    # A fresh install has no wave/cash reading: the window says what to do
    # rather than failing later.
    assert "Teach" in dialog.hud_label.text()
    dialog._analyze()
    assert "Teach" in dialog.setup_status.text()
    assert dialog.analysis is None


def test_a_file_that_is_not_a_video_is_refused_in_words(
    dialog: VideoImportDialog, tmp_path: Path
) -> None:
    junk = tmp_path / "notes.mp4"
    junk.write_text("not a video")
    assert not dialog.load_video(junk)
    assert dialog.setup_status.text()
    assert not dialog.analyze_button.isEnabled()


def test_the_current_frame_is_read_back_to_the_user(
    dialog: VideoImportDialog, match_video: Path
) -> None:
    dialog.load_video(match_video)
    dialog.calibration = match_calibration()
    dialog.slider.setValue(70)  # 7.0 s
    dialog._load_preview()
    text = dialog.hud_label.text()
    assert f"wave {wave_at(7.0)}" in text and f"${cash_at(7.0):,}" in text


def test_the_live_calibration_is_never_touched_by_the_import(
    dialog: VideoImportDialog, controller: AppController, match_video: Path
) -> None:
    dialog.load_video(match_video)
    dialog.calibration.wave.box = WAVE_BOX
    assert controller.calibration.wave.box is None


def test_analysis_on_the_worker_thread_reaches_the_review_page(
    dialog: VideoImportDialog, match_video: Path, qt_app: QApplication
) -> None:
    dialog.load_video(match_video)
    dialog.calibration = match_calibration()
    dialog._analyze()
    assert not dialog.analyze_button.isEnabled()
    deadline = time.monotonic() + 60
    while dialog.analysis is None and time.monotonic() < deadline:
        qt_app.processEvents()
        time.sleep(0.01)
    assert dialog.analysis is not None, "analysis never finished"
    assert dialog._thread is not None
    dialog._thread.wait(5000)
    assert dialog.pages.currentIndex() == 1
    assert dialog.analyze_button.isEnabled()
    assert len(dialog.analysis.purchases) == len(EVENTS)


def test_review_lists_every_purchase_and_one_row_per_tower_price(
    dialog: VideoImportDialog, match_video: Path
) -> None:
    analysed(dialog, match_video)
    assert dialog.pages.currentIndex() == 1
    assert dialog.purchase_list.count() == len(EVENTS)
    assert [measured for measured, *_ in dialog._price_rows] == [200, 350]
    assert dialog.canvas is not None, "the first purchase's frame should be shown"
    assert "4 purchases" in dialog.summary.text()


def test_created_strategy_uses_the_chosen_slots_and_becomes_the_open_one(
    dialog: VideoImportDialog, controller: AppController, match_video: Path
) -> None:
    analysed(dialog, match_video)
    dialog._price_rows[0][3].setCurrentIndex(2)  # $200 -> slot 3
    dialog._price_rows[1][3].setCurrentIndex(0)  # $350 -> slot 1
    dialog.gate_box.setChecked(True)
    dialog.name_edit.setText("Molten from video")
    dialog._create()

    assert dialog.result() == VideoImportDialog.DialogCode.Accepted
    assert dialog.created is not None and dialog.created.name.startswith("video-")
    saved = Strategy.from_json(dialog.created.read_text(encoding="utf-8"))
    assert controller.strategy.name == saved.name == "Molten from video"
    assert controller.strategy_path == dialog.created and not controller.dirty
    places = [s for s in saved.steps if isinstance(s, Place)]
    assert [p.slot for p in places] == [3, 1, 3]
    assert any(isinstance(s, Upgrade) for s in saved.steps)
    assert any(isinstance(s, WaitForCash) for s in saved.steps)
    assert saved.validate() == []


def test_unsaved_work_is_backed_up_before_the_import_replaces_it(
    dialog: VideoImportDialog, controller: AppController, match_video: Path
) -> None:
    controller.strategy = Strategy(
        name="mine", steps=[Place(slot=1, position=NormPoint(0.5, 0.5), tower_id="A")]
    )
    controller.dirty = True
    analysed(dialog, match_video)
    dialog._create()
    backups = list(controller.store.strategies_dir().glob("unsaved-backup-*.json"))
    assert len(backups) == 1
    assert json.loads(backups[0].read_text(encoding="utf-8"))["name"] == "mine"


def test_a_corrected_price_is_used_for_the_cash_wait(
    dialog: VideoImportDialog, controller: AppController, match_video: Path
) -> None:
    analysed(dialog, match_video)
    dialog._price_rows[1][2].setValue(400)  # the user knows it really costs $400
    dialog.gate_box.setChecked(True)
    dialog._create()
    amounts = [s.amount for s in controller.strategy.steps if isinstance(s, WaitForCash)]
    assert max(amounts) >= 400


def test_skipping_a_purchase_leaves_it_out(
    dialog: VideoImportDialog, controller: AppController, match_video: Path
) -> None:
    analysed(dialog, match_video)
    dialog.purchase_list.setCurrentRow(3)  # the last placement
    dialog.kind_combo.setCurrentIndex(dialog.kind_combo.findData(SKIP))
    assert dialog.analysis is not None and dialog.analysis.purchases[3].kind == SKIP
    dialog._create()
    assert len([s for s in controller.strategy.steps if isinstance(s, Place)]) == 2


def test_turning_an_upgrade_into_a_placement_gives_it_a_tower_and_a_slot_row(
    dialog: VideoImportDialog, match_video: Path
) -> None:
    analysed(dialog, match_video)
    analysis = dialog.analysis
    assert analysis is not None
    dialog.purchase_list.setCurrentRow(2)  # the $150 upgrade
    assert analysis.purchases[2].kind == UPGRADE
    assert dialog.tower_combo.isVisibleTo(dialog)
    dialog.kind_combo.setCurrentIndex(dialog.kind_combo.findData(PLACE))
    assert [p.tower_id for p in analysis.purchases if p.kind == PLACE] == ["T1", "T2", "T3", "T4"]
    # A new price appeared, so it needs its own hotbar row -- not a silent slot 1.
    assert [measured for measured, *_ in dialog._price_rows] == [150, 200, 350]


def test_slots_already_chosen_survive_a_type_change(
    dialog: VideoImportDialog, match_video: Path
) -> None:
    analysed(dialog, match_video)
    dialog._price_rows[1][3].setCurrentIndex(4)  # $350 -> slot 5
    dialog.purchase_list.setCurrentRow(3)
    dialog.kind_combo.setCurrentIndex(dialog.kind_combo.findData(SKIP))
    row = next(r for r in dialog._price_rows if r[0] == 350)
    assert row[3].currentData() == 5


def test_clicking_the_frame_moves_the_tower(dialog: VideoImportDialog, match_video: Path) -> None:
    analysed(dialog, match_video)
    dialog.purchase_list.setCurrentRow(0)
    assert dialog.canvas is not None
    dialog.canvas.picked.emit(NormPoint(0.2, 0.3))
    assert dialog.analysis is not None
    first = dialog.analysis.purchases[0]
    assert first.position == NormPoint(0.2, 0.3) and first.note == "spot set by hand"


def test_the_video_can_teach_wave_and_cash_reading(
    match_video: Path, qt_app: QApplication
) -> None:
    calibration = Calibration()
    info = VideoInfo(match_video, 10.0, 200, 640, 360)
    teacher = VideoHudDialog(match_video, info, WHOLE_FRAME, calibration, 7.0)
    try:
        assert "not marked" in teacher.progress.text()
        calibration.wave.box = WAVE_BOX
        calibration.cash.box = CASH_BOX
        teacher.wave_text.setText(str(wave_at(7.0)))
        teacher.cash_text.setText(f"${cash_at(7.0):,}")
        teacher._learn()
        assert "learned" in teacher.status.text()
        assert calibration.hud_ready
        assert teacher.frame is not None
        cash = read_number(
            crop_norm(teacher.frame, CASH_BOX), calibration.cash.classifier, calibration.cash.invert
        )
        assert cash.value == cash_at(7.0)
    finally:
        teacher.deleteLater()
        qt_app.processEvents()
