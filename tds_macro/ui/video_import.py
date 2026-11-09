"""Import a screen recording as a draft strategy.

Two pages. **Set up**: choose the video, mark the game area if the video has
borders, and make sure the wave and cash numbers can be read (taught from the
video's own frames if needed). **Review**: what was found -- for each tower
price, which of *your* hotbar slots holds it; for each purchase, whether it was
a placement or an upgrade and where, fixable by clicking the frame it came
from. Nothing is trusted blind: the analysis is inference from pixels.
"""

from __future__ import annotations

import copy
import logging
import threading
from pathlib import Path

import numpy as np
from PySide6.QtCore import QObject, Qt, QThread, Signal
from PySide6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QDialog,
    QFileDialog,
    QFormLayout,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QListWidget,
    QMessageBox,
    QProgressBar,
    QPushButton,
    QSlider,
    QSpinBox,
    QStackedWidget,
    QVBoxLayout,
    QWidget,
)

from ..input.coords import NormPoint
from ..vision.calibration import Calibration, HudRegion
from ..vision.capture import NormBox, crop_norm
from ..vision.digits import learn_from_text, missing_digits, read_number
from ..vision.video import (
    PLACE,
    PRICE_TOLERANCE,
    SKIP,
    UNKNOWN,
    UPGRADE,
    VIDEO_EXTENSIONS,
    WHOLE_FRAME,
    Purchase,
    VideoAnalysis,
    VideoError,
    VideoInfo,
    VideoSettings,
    analyze_video,
    build_strategy,
    game_area,
    open_video,
    read_frame_at,
)
from .controller import AppController
from .pickers import PickerCanvas, PickerDialog, numpy_to_qimage
from .theme import ERROR, OK, TEXT_DIM, WARN

log = logging.getLogger(__name__)

KIND_LABELS = {PLACE: "Placed a tower", UPGRADE: "Upgraded a tower", SKIP: "Ignore this one"}


def _status(label: QLabel, text: str, colour: str) -> None:
    label.setText(text)
    label.setStyleSheet(f"color: {colour};")


class AnalysisWorker(QObject):
    """Runs the analysis off the UI thread; a long video takes minutes."""

    progress = Signal(float)
    done = Signal(object)
    failed = Signal(str)

    def __init__(self, path: Path, calibration: Calibration, settings: VideoSettings) -> None:
        super().__init__()
        self.path = path
        self.calibration = calibration
        self.settings = settings
        self._stop = threading.Event()

    def cancel(self) -> None:
        self._stop.set()

    def run(self) -> None:
        try:
            result = analyze_video(
                self.path,
                self.calibration,
                self.settings,
                progress=self.progress.emit,
                should_stop=self._stop.is_set,
            )
        except VideoError as exc:
            self.failed.emit(str(exc))
            return
        except Exception as exc:  # pragma: no cover - defensive
            log.exception("video analysis failed")
            self.failed.emit(f"The analysis failed: {exc}")
            return
        self.done.emit(result)


class VideoHudDialog(QDialog):
    """Teach wave and cash reading from the video's own frames."""

    def __init__(
        self,
        path: Path,
        info: VideoInfo,
        game_box: NormBox,
        calibration: Calibration,
        seconds: float,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self.setWindowTitle("Teach the app this video's wave and cash")
        self.path, self.info, self.game_box = path, info, game_box
        self.calibration = calibration
        self.frame: np.ndarray | None = None

        intro = QLabel(
            "Move to a moment where both numbers are clear, mark each one with a tight "
            "box, type what it shows, and press Learn. Repeat at a few other moments "
            "until every digit is known."
        )
        intro.setWordWrap(True)
        self.slider = QSlider(Qt.Orientation.Horizontal)
        self.slider.setRange(0, max(1, int(info.duration_s * 10)))
        self.slider.setValue(int(seconds * 10))
        self.slider.sliderReleased.connect(self._load_frame)
        self.time_label = QLabel()
        self.preview = QLabel()
        self.preview.setMinimumHeight(240)
        self.preview.setAlignment(Qt.AlignmentFlag.AlignCenter)

        self.wave_text = QLineEdit()
        self.wave_text.setPlaceholderText("wave shows, e.g. 12")
        self.cash_text = QLineEdit()
        self.cash_text.setPlaceholderText("cash shows, e.g. $1,250")
        mark_wave = QPushButton("Mark the WAVE number...")
        mark_wave.clicked.connect(lambda: self._mark(self.calibration.wave, "wave"))
        mark_cash = QPushButton("Mark the CASH number...")
        mark_cash.clicked.connect(lambda: self._mark(self.calibration.cash, "cash"))
        learn = QPushButton("Learn")
        learn.clicked.connect(self._learn)
        done = QPushButton("Done")
        done.clicked.connect(self.accept)
        self.status = QLabel()
        self.status.setWordWrap(True)
        self.progress = QLabel()

        form = QFormLayout()
        row = QHBoxLayout()
        row.addWidget(mark_wave)
        row.addWidget(self.wave_text, 1)
        form.addRow(row)
        row = QHBoxLayout()
        row.addWidget(mark_cash)
        row.addWidget(self.cash_text, 1)
        form.addRow(row)
        buttons = QHBoxLayout()
        buttons.addWidget(learn)
        buttons.addStretch(1)
        buttons.addWidget(done)

        layout = QVBoxLayout(self)
        layout.addWidget(intro)
        scrub = QHBoxLayout()
        scrub.addWidget(self.slider, 1)
        scrub.addWidget(self.time_label)
        layout.addLayout(scrub)
        layout.addWidget(self.preview, 1)
        layout.addLayout(form)
        layout.addWidget(self.progress)
        layout.addWidget(self.status)
        layout.addLayout(buttons)
        self.resize(820, 640)
        self._load_frame()

    def _load_frame(self) -> None:
        seconds = self.slider.value() / 10
        minutes, secs = divmod(int(seconds), 60)
        self.time_label.setText(f"{minutes}:{secs:02d}")
        try:
            self.frame = game_area(read_frame_at(self.path, seconds), self.game_box)
        except VideoError as exc:
            _status(self.status, str(exc), ERROR)
            return
        image = numpy_to_qimage(self.frame)
        from PySide6.QtGui import QPixmap

        self.preview.setPixmap(
            QPixmap.fromImage(image).scaled(
                780, 300, Qt.AspectRatioMode.KeepAspectRatio,
                Qt.TransformationMode.SmoothTransformation,
            )
        )
        self._update_progress()

    def _mark(self, region: HudRegion, name: str) -> None:
        if self.frame is None:
            return
        dialog = PickerDialog(
            numpy_to_qimage(self.frame),
            f"Mark the {name} number",
            f"Drag a tight box around the {name.upper()} number only.",
            mode="region",
            parent=self,
        )
        if dialog.exec() == PickerDialog.DialogCode.Accepted and dialog.result_box:
            region.box = dialog.result_box
            self._update_progress()

    def _learn(self) -> None:
        if self.frame is None:
            return
        messages = []
        for name, region, edit in (
            ("wave", self.calibration.wave, self.wave_text),
            ("cash", self.calibration.cash, self.cash_text),
        ):
            if region.box is None or not edit.text().strip():
                continue
            outcome = learn_from_text(
                crop_norm(self.frame, region.box), edit.text(), region.classifier, region.invert
            )
            result = f"learned {outcome.added} digit(s)" if outcome.ok else outcome.error
            messages.append(f"{name}: {result}")
        _status(
            self.status,
            " | ".join(messages) or "Mark a number and type what it shows first.",
            OK if messages and all("learned" in m for m in messages) else WARN,
        )
        self._update_progress()

    def _update_progress(self) -> None:
        parts = []
        for name, region in (("Wave", self.calibration.wave), ("Cash", self.calibration.cash)):
            if region.box is None:
                parts.append(f"{name}: not marked")
                continue
            missing = missing_digits(region.classifier)
            needs = f"still needs {' '.join(missing)}" if missing else "all digits known"
            parts.append(f"{name}: {needs}")
        self.progress.setText("   ".join(parts))


class VideoImportDialog(QDialog):
    def __init__(self, controller: AppController, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.controller = controller
        self.setWindowTitle("Import a video")
        self.path: Path | None = None
        self.info: VideoInfo | None = None
        self.frame: np.ndarray | None = None
        self.game_box: NormBox = WHOLE_FRAME
        # A private copy: teaching the video's HUD must not overwrite live
        # calibration unless the user asks.
        self.calibration = copy.deepcopy(controller.calibration)
        self.analysis: VideoAnalysis | None = None
        self.created: Path | None = None
        self._thread: QThread | None = None
        self._worker: AnalysisWorker | None = None
        self._price_rows: list[tuple[int, list[Purchase], QSpinBox, QComboBox]] = []
        self._selected: Purchase | None = None

        self.pages = QStackedWidget()
        self.pages.addWidget(self._setup_page())
        self.pages.addWidget(self._review_page())
        layout = QVBoxLayout(self)
        layout.addWidget(self.pages)
        self.resize(980, 720)

    # -- page 1: set up --------------------------------------------------------------
    def _setup_page(self) -> QWidget:
        page = QWidget()
        intro = QLabel(
            "<p>Choose a recording of a TDS match -- yours or someone else's. The app "
            "watches the wave and cash numbers, and every time cash drops it works out "
            "what was bought and where. <b>You check the result before playing it</b>: "
            "it is worked out from the picture, so it can be wrong.</p>"
        )
        intro.setWordWrap(True)
        intro.setTextFormat(Qt.TextFormat.RichText)

        choose = QPushButton("Choose video...")
        choose.clicked.connect(self._choose)
        self.file_label = QLabel("No video chosen.")
        self.file_label.setWordWrap(True)

        self.slider = QSlider(Qt.Orientation.Horizontal)
        self.slider.setEnabled(False)
        self.slider.sliderReleased.connect(self._load_preview)
        self.time_label = QLabel("0:00")
        self.preview = QLabel("The video's first frame will show here.")
        self.preview.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.preview.setMinimumHeight(260)
        self.preview.setStyleSheet(f"color: {TEXT_DIM}; border: 1px solid #2a3038;")

        self.area_label = QLabel("Game area: the whole video")
        mark_area = QPushButton("Mark the game area...")
        mark_area.setToolTip(
            "If the video has black bars, a webcam or other borders, drag a box around "
            "just the game."
        )
        mark_area.clicked.connect(self._mark_area)
        reset_area = QPushButton("Whole video")
        reset_area.clicked.connect(self._reset_area)

        self.hud_label = QLabel()
        self.hud_label.setWordWrap(True)
        teach = QPushButton("Teach wave && cash from this video...")
        teach.setToolTip(
            "Needed when the app cannot read this video's numbers yet -- for example a "
            "video from another player, at another size."
        )
        teach.clicked.connect(self._teach)

        self.analyze_button = QPushButton("Find the purchases")
        self.analyze_button.setObjectName("primary")
        self.analyze_button.setEnabled(False)
        self.analyze_button.clicked.connect(self._analyze)
        self.cancel_button = QPushButton("Cancel")
        self.cancel_button.clicked.connect(self._cancel)
        self.progress = QProgressBar()
        self.progress.setRange(0, 100)
        self.progress.setVisible(False)
        self.setup_status = QLabel()
        self.setup_status.setWordWrap(True)

        top = QHBoxLayout()
        top.addWidget(choose)
        top.addWidget(self.file_label, 1)
        scrub = QHBoxLayout()
        scrub.addWidget(self.slider, 1)
        scrub.addWidget(self.time_label)
        area = QHBoxLayout()
        area.addWidget(self.area_label, 1)
        area.addWidget(mark_area)
        area.addWidget(reset_area)
        hud = QHBoxLayout()
        hud.addWidget(self.hud_label, 1)
        hud.addWidget(teach)
        go = QHBoxLayout()
        go.addWidget(self.analyze_button)
        go.addWidget(self.progress, 1)
        go.addWidget(self.cancel_button)

        layout = QVBoxLayout(page)
        layout.addWidget(intro)
        layout.addLayout(top)
        layout.addLayout(scrub)
        layout.addWidget(self.preview, 1)
        layout.addLayout(area)
        layout.addLayout(hud)
        layout.addWidget(self.setup_status)
        layout.addLayout(go)
        return page

    def _choose(self) -> None:
        patterns = " ".join(VIDEO_EXTENSIONS)
        path, _ = QFileDialog.getOpenFileName(
            self, "Choose a video", str(Path.home()), f"Videos ({patterns})"
        )
        if path:
            self.load_video(Path(path))

    def load_video(self, path: Path) -> bool:
        try:
            capture, info = open_video(path)
            capture.release()
        except VideoError as exc:
            _status(self.setup_status, str(exc), ERROR)
            return False
        self.path, self.info = path, info
        self.file_label.setText(f"{path.name}  ({info.describe()})")
        self.slider.setRange(0, max(1, int(info.duration_s * 10)))
        self.slider.setValue(int(min(info.duration_s, 5.0) * 10))
        self.slider.setEnabled(True)
        self.analyze_button.setEnabled(True)
        self._load_preview()
        return True

    def _seconds(self) -> float:
        return self.slider.value() / 10

    def _load_preview(self) -> None:
        if self.path is None:
            return
        seconds = self._seconds()
        minutes, secs = divmod(int(seconds), 60)
        self.time_label.setText(f"{minutes}:{secs:02d}")
        try:
            self.frame = read_frame_at(self.path, seconds)
        except VideoError as exc:
            _status(self.setup_status, str(exc), ERROR)
            return
        from PySide6.QtGui import QPixmap

        self.preview.setPixmap(
            QPixmap.fromImage(numpy_to_qimage(self.frame)).scaled(
                900, 320, Qt.AspectRatioMode.KeepAspectRatio,
                Qt.TransformationMode.SmoothTransformation,
            )
        )
        self._check_hud()

    def _mark_area(self) -> None:
        if self.frame is None:
            return
        dialog = PickerDialog(
            numpy_to_qimage(self.frame),
            "Mark the game area",
            "Drag a box around just the game -- leave out black bars, webcams and overlays.",
            mode="region",
            parent=self,
        )
        if dialog.exec() == PickerDialog.DialogCode.Accepted and dialog.result_box:
            self.game_box = dialog.result_box
            self.area_label.setText(
                f"Game area: {self.game_box.width:.0%} x {self.game_box.height:.0%} of the video"
            )
            self._check_hud()

    def _reset_area(self) -> None:
        self.game_box = WHOLE_FRAME
        self.area_label.setText("Game area: the whole video")
        self._check_hud()

    def _check_hud(self) -> None:
        """Try reading this frame with the current wave/cash set-up."""
        if self.frame is None:
            return
        if not self.calibration.hud_ready:
            _status(
                self.hud_label,
                "Wave & cash: not set up yet -- press 'Teach...' to mark them on this video.",
                WARN,
            )
            return
        game = game_area(self.frame, self.game_box)
        wave = read_number(
            crop_norm(game, self.calibration.wave.box),  # type: ignore[arg-type]
            self.calibration.wave.classifier,
            self.calibration.wave.invert,
        )
        cash = read_number(
            crop_norm(game, self.calibration.cash.box),  # type: ignore[arg-type]
            self.calibration.cash.classifier,
            self.calibration.cash.invert,
        )
        if wave.ok and cash.ok:
            _status(
                self.hud_label,
                f"Wave & cash: reading this frame as wave {wave.value}, ${cash.value:,}. If "
                "that is right, press 'Find the purchases'.",
                OK,
            )
        else:
            _status(
                self.hud_label,
                "Wave & cash: cannot read this frame ("
                + "; ".join(r.reason for r in (wave, cash) if r.reason)
                + "). Teach them from this video.",
                WARN,
            )

    def _teach(self) -> None:
        if self.path is None or self.info is None:
            _status(self.setup_status, "Choose a video first.", WARN)
            return
        if not self.calibration.hud_ready:
            # Start the video's set-up from scratch rather than editing a live
            # calibration made for a different screen.
            self.calibration = Calibration()
        VideoHudDialog(
            self.path, self.info, self.game_box, self.calibration, self._seconds(), self
        ).exec()
        self._check_hud()

    # -- analysis -------------------------------------------------------------------
    def _analyze(self) -> None:
        if self.path is None:
            return
        if not self.calibration.hud_ready:
            _status(
                self.setup_status,
                "The app has to be able to read this video's wave and cash first -- press "
                "'Teach...'.",
                ERROR,
            )
            return
        self.analyze_button.setEnabled(False)
        self.progress.setVisible(True)
        self.progress.setValue(0)
        _status(self.setup_status, "Watching the video -- long ones take a while.", TEXT_DIM)
        worker = AnalysisWorker(
            self.path, self.calibration, VideoSettings(game_box=self.game_box)
        )
        thread = QThread(self)
        worker.moveToThread(thread)
        thread.started.connect(worker.run)
        worker.progress.connect(lambda value: self.progress.setValue(int(value * 100)))
        worker.done.connect(self._analysis_done)
        worker.failed.connect(self._analysis_failed)
        worker.done.connect(thread.quit)
        worker.failed.connect(thread.quit)
        self._worker, self._thread = worker, thread
        thread.start()

    def run_analysis_now(self) -> None:
        """Synchronous analysis, for tests and scripted use."""
        assert self.path is not None
        try:
            settings = VideoSettings(game_box=self.game_box)
            result = analyze_video(self.path, self.calibration, settings)
        except VideoError as exc:
            self._analysis_failed(str(exc))
            return
        self._analysis_done(result)

    def _cancel(self) -> None:
        if self._worker is not None and self._thread is not None and self._thread.isRunning():
            self._worker.cancel()
            return
        self.reject()

    def _analysis_failed(self, message: str) -> None:
        self.progress.setVisible(False)
        self.analyze_button.setEnabled(True)
        _status(self.setup_status, message, ERROR)

    def _analysis_done(self, result: object) -> None:
        self.progress.setVisible(False)
        self.analyze_button.setEnabled(True)
        if not isinstance(result, VideoAnalysis):
            return
        self.analysis = result
        self._fill_review()
        self.pages.setCurrentIndex(1)

    # -- page 2: review ----------------------------------------------------------------
    def _review_page(self) -> QWidget:
        page = QWidget()
        self.summary = QLabel()
        self.summary.setWordWrap(True)

        self.prices_box = QGroupBox("Your hotbar: which slot holds each tower?")
        self.prices_form = QFormLayout(self.prices_box)

        self.purchase_list = QListWidget()
        self.purchase_list.currentRowChanged.connect(self._select_purchase)
        self.kind_combo = QComboBox()
        for kind, label in KIND_LABELS.items():
            self.kind_combo.addItem(label, kind)
        self.kind_combo.currentIndexChanged.connect(self._kind_changed)
        self.tower_combo = QComboBox()
        self.tower_combo.setToolTip("Which tower this upgrade was for.")
        self.tower_combo.currentIndexChanged.connect(self._tower_changed)
        self.canvas_holder = QVBoxLayout()
        self.canvas: PickerCanvas | None = None
        hint = QLabel("Click the picture to move this tower's spot.")
        hint.setObjectName("hint")

        detail = QVBoxLayout()
        row = QHBoxLayout()
        row.addWidget(self.kind_combo, 1)
        row.addWidget(self.tower_combo)
        detail.addLayout(row)
        detail.addLayout(self.canvas_holder, 1)
        detail.addWidget(hint)

        middle = QHBoxLayout()
        middle.addWidget(self.purchase_list, 2)
        middle.addLayout(detail, 3)

        self.gate_box = QCheckBox(
            "Wait for the right wave and enough cash before each purchase"
        )
        self.gate_box.setToolTip(
            "Adapts to however your own match goes. Needs wave & cash reading set up in "
            "this app. Untick to use the video's original timing instead (TinyTask-style)."
        )
        self.gate_box.setChecked(self.controller.calibration.hud_ready)
        self.use_live = QCheckBox("Also use this video's wave && cash set-up for live play")
        self.use_live.setToolTip(
            "Only if the video is your own, at the same window size. You can redo it from "
            "the Advanced editor's Tools menu at any time."
        )
        self.name_edit = QLineEdit()
        create = QPushButton("Create recording")
        create.setObjectName("primary")
        create.clicked.connect(self._create)
        back = QPushButton("< Back")
        back.clicked.connect(lambda: self.pages.setCurrentIndex(0))

        bottom = QHBoxLayout()
        bottom.addWidget(back)
        bottom.addWidget(QLabel("Name:"))
        bottom.addWidget(self.name_edit, 1)
        bottom.addWidget(create)

        layout = QVBoxLayout(page)
        layout.addWidget(self.summary)
        layout.addWidget(self.prices_box)
        layout.addLayout(middle, 1)
        layout.addWidget(self.gate_box)
        layout.addWidget(self.use_live)
        layout.addLayout(bottom)
        return page

    def _fill_review(self) -> None:
        analysis = self.analysis
        assert analysis is not None
        text = analysis.summary()
        if analysis.warnings:
            text += "<br><span style='color:#f0b429'>" + "<br>".join(analysis.warnings) + "</span>"
        self.summary.setText(text)
        self.summary.setTextFormat(Qt.TextFormat.RichText)

        self._fill_prices()
        self.name_edit.setText(f"Video {analysis.info.path.stem}")
        self.use_live.setVisible(not self.controller.calibration.hud_ready)
        self.use_live.setChecked(False)
        self._refresh_list()
        if analysis.purchases:
            self.purchase_list.setCurrentRow(0)

    def _fill_prices(self) -> None:
        """One row per tower price. Rebuilt when purchase types change, keeping
        the slot and price already chosen for a price that is still there."""
        assert self.analysis is not None
        previous = [(measured, spin.value(), slot.currentIndex())
                    for measured, _, spin, slot in self._price_rows]
        while self.prices_form.rowCount():
            self.prices_form.removeRow(0)
        self._price_rows = []
        for index, (price, group) in enumerate(self.analysis.price_groups().items(), start=1):
            kept = next(
                (old for old in previous if abs(old[0] - price) <= old[0] * PRICE_TOLERANCE),
                None,
            )
            spin = QSpinBox()
            spin.setRange(1, 10_000_000)
            spin.setPrefix("$")
            spin.setValue(kept[1] if kept else price)
            spin.setToolTip("Correct this if you know the real price.")
            slot = QComboBox()
            for number in range(1, 6):
                slot.addItem(f"Slot {number}", number)
            slot.setCurrentIndex(kept[2] if kept else min(4, index - 1))
            row = QHBoxLayout()
            row.addWidget(spin)
            row.addWidget(QLabel(f"placed {len(group)}x  ->  your"))
            row.addWidget(slot)
            row.addStretch(1)
            holder = QWidget()
            holder.setLayout(row)
            self.prices_form.addRow(f"Tower {index}:", holder)
            self._price_rows.append((price, group, spin, slot))
        if not self._price_rows:
            self.prices_form.addRow(QLabel("No tower placements were found."))

    def _refresh_list(self) -> None:
        assert self.analysis is not None
        row = self.purchase_list.currentRow()
        self.purchase_list.blockSignals(True)
        self.purchase_list.clear()
        for purchase in self.analysis.purchases:
            text = purchase.describe() + (f"  ({purchase.note})" if purchase.note else "")
            self.purchase_list.addItem(text)
            if purchase.kind == UNKNOWN:
                self.purchase_list.item(self.purchase_list.count() - 1).setForeground(
                    Qt.GlobalColor.yellow
                )
        self.purchase_list.blockSignals(False)
        if 0 <= row < self.purchase_list.count():
            self.purchase_list.setCurrentRow(row)

    def _towers_before(self, purchase: Purchase) -> list[str]:
        assert self.analysis is not None
        towers = []
        for other in self.analysis.purchases:
            if other is purchase:
                break
            if other.kind == PLACE and other.tower_id:
                towers.append(other.tower_id)
        return towers

    def _select_purchase(self, row: int) -> None:
        if self.analysis is None or not 0 <= row < len(self.analysis.purchases):
            return
        purchase = self.analysis.purchases[row]
        self._selected = purchase
        self.kind_combo.blockSignals(True)
        index = self.kind_combo.findData(purchase.kind)
        self.kind_combo.setCurrentIndex(index if index >= 0 else 0)
        self.kind_combo.blockSignals(False)
        self.tower_combo.blockSignals(True)
        self.tower_combo.clear()
        for tower in self._towers_before(purchase):
            self.tower_combo.addItem(tower, tower)
        if purchase.tower_id:
            self.tower_combo.setCurrentIndex(max(0, self.tower_combo.findData(purchase.tower_id)))
        self.tower_combo.setVisible(purchase.kind == UPGRADE)
        self.tower_combo.blockSignals(False)
        self._show_canvas(purchase)

    def _show_canvas(self, purchase: Purchase) -> None:
        if self.canvas is not None:
            self.canvas.setParent(None)
            self.canvas.deleteLater()
            self.canvas = None
        if purchase.thumbnail is None:
            return
        markers = [
            (p.tower_id, p.position)
            for p in self.analysis.purchases  # type: ignore[union-attr]
            if p.kind == PLACE and p.position is not None and p is not purchase
        ]
        canvas = PickerCanvas(numpy_to_qimage(purchase.thumbnail), mode="point", markers=markers)
        canvas.show_magnifier = False
        if purchase.position is not None:
            canvas.point = purchase.position
        canvas.picked.connect(self._spot_picked)
        self.canvas_holder.addWidget(canvas, 1)
        self.canvas = canvas

    def _spot_picked(self, value: object) -> None:
        if self._selected is None or not isinstance(value, NormPoint):
            return
        purchase = self._selected
        if purchase.kind in (UNKNOWN, SKIP):
            purchase.kind = PLACE
            self._fill_prices()
        if purchase.kind == PLACE:
            purchase.position = value
            purchase.note = "spot set by hand"
            self._renumber()

    def _kind_changed(self, index: int) -> None:
        if self._selected is None:
            return
        kind = str(self.kind_combo.itemData(index))
        purchase = self._selected
        purchase.kind = kind
        if kind == UPGRADE:
            towers = self._towers_before(purchase)
            purchase.tower_id = towers[-1] if towers else ""
        self._fill_prices()
        self._renumber()

    def _tower_changed(self, index: int) -> None:
        if self._selected is not None and self._selected.kind == UPGRADE and index >= 0:
            self._selected.tower_id = str(self.tower_combo.itemData(index))
            self._refresh_list()

    def _renumber(self) -> None:
        """Placements are T1, T2... in order; upgrades follow their tower."""
        assert self.analysis is not None
        mapping: dict[str, str] = {}
        number = 1
        for purchase in self.analysis.purchases:
            if purchase.kind == PLACE:
                new_id = f"T{number}"
                if purchase.tower_id:
                    mapping[purchase.tower_id] = new_id
                purchase.tower_id = new_id
                number += 1
        for purchase in self.analysis.purchases:
            if purchase.kind == UPGRADE and purchase.tower_id in mapping:
                purchase.tower_id = mapping[purchase.tower_id]
        self._refresh_list()
        if self._selected is not None:
            self._select_purchase(self.analysis.purchases.index(self._selected))

    # -- create -----------------------------------------------------------------------
    def slot_mapping(self) -> dict[int, int]:
        """Measured price -> chosen slot, applying any price the user corrected."""
        assert self.analysis is not None
        mapping: dict[int, int] = {}
        for _, group, spin, slot in self._price_rows:
            corrected = spin.value()
            for purchase in group:
                purchase.cost = corrected
            mapping[corrected] = int(slot.currentData())
        return mapping

    def _create(self) -> None:
        if self.analysis is None:
            return
        mapping = self.slot_mapping()
        strategy = build_strategy(
            self.analysis,
            mapping,
            self.name_edit.text().strip() or "Imported video",
            gate_on_hud=self.gate_box.isChecked(),
        )
        if not strategy.steps:
            QMessageBox.warning(
                self, "Nothing to create", "No placements or upgrades are left to replay."
            )
            return
        if self.use_live.isVisible() and self.use_live.isChecked():
            self.controller.calibration = copy.deepcopy(self.calibration)
            self.controller.save_calibration()
        path = self.controller.adopt_strategy(strategy, "video")
        if path is None:
            return
        problems = strategy.validate()
        if problems:
            self.controller.emit_log("warn", "Imported with problems: " + "; ".join(problems))
        self.controller.emit_log(
            "info",
            f"Created '{strategy.name}' from the video: {len(strategy.steps)} steps. Check "
            "it with Practice mode before playing it for real.",
        )
        self.created = path
        self.accept()
