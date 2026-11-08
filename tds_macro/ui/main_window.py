"""Main window: step list, editor, log, and the big Start/Stop buttons."""

from __future__ import annotations

import html
from pathlib import Path

import numpy as np
from PySide6.QtCore import Qt
from PySide6.QtGui import QAction, QCloseEvent, QKeySequence
from PySide6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QFileDialog,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QListView,
    QMainWindow,
    QMessageBox,
    QPlainTextEdit,
    QPushButton,
    QSplitter,
    QVBoxLayout,
    QWidget,
)

from .. import __version__
from ..input.coords import NormPoint
from ..strategy.steps import (
    Ability,
    Place,
    Sell,
    SkipWave,
    Step,
    Strategy,
    Upgrade,
    Wait,
    WaitForCash,
    WaitForWave,
)
from ..vision.hud import HudSnapshot
from .controller import AppController
from .dialogs import AboutDialog, SettingsDialog
from .editor import StepEditor
from .models import StepListModel
from .pickers import PickerDialog, numpy_to_qimage
from .theme import ACCENT, ERROR, OK, STYLESHEET, TEXT, TEXT_DIM, WARN
from .wizard import SetupWizard

LEVEL_COLOURS = {"error": ERROR, "warn": WARN, "info": TEXT, "step": ACCENT}
MAX_LOG_LINES = 2000

NEW_STEPS: list[tuple[str, type[Step]]] = [
    ("Place a tower", Place),
    ("Upgrade a tower", Upgrade),
    ("Sell a tower", Sell),
    ("Use an ability", Ability),
    ("Skip a wave", SkipWave),
    ("Wait a fixed time", Wait),
    ("Wait for a wave", WaitForWave),
    ("Wait for cash", WaitForCash),
]


class MainWindow(QMainWindow):
    def __init__(self, controller: AppController) -> None:
        super().__init__()
        self.controller = controller
        self.setWindowTitle("TDS Macro")
        self.setStyleSheet(STYLESHEET)
        self.resize(1180, 820)

        self.model = StepListModel(controller.strategy)
        self._build_ui()
        self._build_menu()
        self._connect()

        controller.apply_hotkeys()
        controller.refresh_window()
        controller.start_hud_monitor()
        self._refresh_run_state()
        if controller.settings.last_strategy_path:
            path = Path(controller.settings.last_strategy_path)
            if path.exists():
                controller.load_strategy(path)

    # -- construction ---------------------------------------------------------
    def _build_ui(self) -> None:
        self.status_label = QLabel("Idle")
        self.status_label.setObjectName("status")
        self.hud_label = QLabel("Wave --   $--")
        self.hud_label.setObjectName("hud")
        self.hud_label.setToolTip(
            "Live values read from your HUD. If these are wrong or blank, re-run the "
            "calibration wizard."
        )
        self.window_label = QLabel("Roblox: not found")
        self.window_label.setObjectName("hint")

        self.start_button = QPushButton("Start  (F7)")
        self.start_button.setObjectName("primary")
        self.start_button.setToolTip("Replay the strategy. Roblox must be the focused window.")
        self.stop_button = QPushButton("STOP  (F8)")
        self.stop_button.setObjectName("danger")
        self.stop_button.setToolTip("Stop immediately and release every held key.")
        self.record_button = QPushButton("Record  (F6)")
        self.record_button.setToolTip(
            "Play normally and your actions become steps. Press again to stop."
        )

        self.dry_run_box = QCheckBox("Dry run")
        self.dry_run_box.setChecked(self.controller.settings.dry_run)
        self.dry_run_box.setToolTip(
            "Log what would happen without sending any input. Use this for the first "
            "replay of a new strategy."
        )
        self.loop_box = QCheckBox("Loop matches")
        self.loop_box.setChecked(self.controller.settings.loop.enabled)
        self.loop_box.setToolTip(
            "Repeat automatically: wait for the end screen, re-queue, run again."
        )

        top = QHBoxLayout()
        top.addWidget(self.start_button)
        top.addWidget(self.stop_button)
        top.addWidget(self.record_button)
        top.addSpacing(16)
        top.addWidget(self.dry_run_box)
        top.addWidget(self.loop_box)
        top.addStretch(1)
        top.addWidget(self.hud_label)

        # -- strategy pane
        self.name_edit = QLineEdit(self.controller.strategy.name)
        self.name_edit.setToolTip("Shown in the log and saved inside the strategy file.")
        self.name_edit.textEdited.connect(self._on_name_edited)

        self.list_view = QListView()
        self.list_view.setModel(self.model)
        self.list_view.setDragDropMode(QListView.DragDropMode.InternalMove)
        self.list_view.setDefaultDropAction(Qt.DropAction.MoveAction)
        self.list_view.setSelectionMode(QListView.SelectionMode.SingleSelection)
        self.list_view.setToolTip("Drag to reorder. Steps run top to bottom.")
        self.list_view.setAlternatingRowColors(False)

        self.add_combo = QComboBox()
        for label, _ in NEW_STEPS:
            self.add_combo.addItem(label)
        self.add_combo.setToolTip("Choose a step type, then press Add.")
        add_button = QPushButton("Add")
        add_button.clicked.connect(self._add_step)
        duplicate = QPushButton("Duplicate")
        duplicate.clicked.connect(self._duplicate_step)
        delete = QPushButton("Delete")
        delete.clicked.connect(self._delete_step)
        up = QPushButton("Up")
        up.clicked.connect(lambda: self._shift(-1))
        down = QPushButton("Down")
        down.clicked.connect(lambda: self._shift(1))
        toggle = QPushButton("Enable/disable")
        toggle.setToolTip("Mute a step without deleting it.")
        toggle.clicked.connect(self._toggle_step)

        tools = QHBoxLayout()
        tools.addWidget(self.add_combo, 1)
        for button in (add_button, duplicate, delete, up, down, toggle):
            tools.addWidget(button)

        self.summary_label = QLabel()
        self.summary_label.setObjectName("hint")

        strategy_box = QGroupBox("Strategy")
        strategy_layout = QVBoxLayout(strategy_box)
        name_row = QHBoxLayout()
        name_row.addWidget(QLabel("Name"))
        name_row.addWidget(self.name_edit, 1)
        strategy_layout.addLayout(name_row)
        strategy_layout.addWidget(self.list_view, 1)
        strategy_layout.addLayout(tools)
        strategy_layout.addWidget(self.summary_label)

        self.editor = StepEditor()
        editor_box = QGroupBox("Selected step")
        editor_layout = QVBoxLayout(editor_box)
        editor_layout.addWidget(self.editor)

        middle = QSplitter(Qt.Orientation.Horizontal)
        middle.addWidget(strategy_box)
        middle.addWidget(editor_box)
        middle.setSizes([660, 460])

        # -- log
        self.log_view = QPlainTextEdit()
        self.log_view.setObjectName("log")
        self.log_view.setReadOnly(True)
        self.log_view.setMaximumBlockCount(MAX_LOG_LINES)
        self.log_view.setToolTip("Timestamped record of everything the app did.")
        clear_log = QPushButton("Clear")
        clear_log.clicked.connect(self.log_view.clear)
        log_box = QGroupBox("Log")
        log_layout = QVBoxLayout(log_box)
        log_layout.addWidget(self.log_view)
        log_row = QHBoxLayout()
        log_row.addStretch(1)
        log_row.addWidget(clear_log)
        log_layout.addLayout(log_row)

        outer = QSplitter(Qt.Orientation.Vertical)
        outer.addWidget(middle)
        outer.addWidget(log_box)
        outer.setSizes([540, 230])

        central = QWidget()
        layout = QVBoxLayout(central)
        layout.addLayout(top)
        layout.addWidget(self.status_label)
        layout.addWidget(self.window_label)
        layout.addWidget(outer, 1)
        self.setCentralWidget(central)
        self._refresh_summary()

    def _build_menu(self) -> None:
        file_menu = self.menuBar().addMenu("&File")
        actions = [
            ("&New strategy", QKeySequence.StandardKey.New, self._new_strategy),
            ("&Open...", QKeySequence.StandardKey.Open, self._open_strategy),
            ("&Save", QKeySequence.StandardKey.Save, self._save_strategy),
            ("Save &as...", QKeySequence.StandardKey.SaveAs, self._save_strategy_as),
        ]
        for label, shortcut, handler in actions:
            action = QAction(label, self)
            if shortcut is not None:
                action.setShortcut(shortcut)
            action.triggered.connect(handler)
            file_menu.addAction(action)
        file_menu.addSeparator()
        share = QAction("&Export a copy to share...", self)
        share.triggered.connect(self._save_strategy_as)
        file_menu.addAction(share)
        quit_action = QAction("&Quit", self)
        quit_action.setShortcut(QKeySequence.StandardKey.Quit)
        quit_action.triggered.connect(self.close)
        file_menu.addAction(quit_action)

        tools_menu = self.menuBar().addMenu("&Tools")
        for label, handler, tip in [
            ("Setup / calibration &wizard...", self._run_wizard, "Re-run first-run setup."),
            ("&Settings...", self._open_settings, "Hotkeys, keybinds, timing, safety."),
            (
                "&Preview placement points...",
                self._preview_points,
                "See every Place step marked on a screenshot.",
            ),
            ("&Re-find Roblox window", self._refind, "Use after restarting Roblox."),
            (
                "&Validate strategy",
                self._validate,
                "Check tower references and slots without running.",
            ),
        ]:
            action = QAction(label, self)
            action.setStatusTip(tip)
            action.triggered.connect(handler)
            tools_menu.addAction(action)

        help_menu = self.menuBar().addMenu("&Help")
        about = QAction("&About", self)
        about.triggered.connect(self._about)
        help_menu.addAction(about)

    def _connect(self) -> None:
        controller = self.controller
        controller.logged.connect(self._append_log)
        controller.statusChanged.connect(self._set_status)
        controller.hudChanged.connect(self._set_hud)
        controller.runStateChanged.connect(self._refresh_run_state)
        controller.strategyChanged.connect(self._reload_strategy)
        controller.windowChanged.connect(self._set_window_label)

        self.start_button.clicked.connect(self._start)
        self.stop_button.clicked.connect(controller.emergency_stop)
        self.record_button.clicked.connect(controller.toggle_recording)
        self.dry_run_box.toggled.connect(self._set_dry_run)
        self.loop_box.toggled.connect(self._set_loop)

        self.list_view.selectionModel().selectionChanged.connect(self._on_selection)
        self.model.dataChanged.connect(lambda *_: self._refresh_summary())
        self.model.modelReset.connect(self._refresh_summary)
        self.editor.changed.connect(self._on_step_edited)
        self.editor.pickRequested.connect(self._pick_position)

    # -- helpers ---------------------------------------------------------------
    def _selected_row(self) -> int:
        indexes = self.list_view.selectionModel().selectedIndexes()
        return indexes[0].row() if indexes else -1

    def _select_row(self, row: int) -> None:
        if 0 <= row < self.model.rowCount():
            index = self.model.index(row, 0)
            self.list_view.setCurrentIndex(index)
            self.list_view.scrollTo(index)

    def _append_log(self, level: str, message: str) -> None:
        colour = LEVEL_COLOURS.get(level, TEXT)
        self.log_view.appendHtml(
            f'<span style="color:{colour}">{html.escape(message)}</span>'
        )

    def _set_status(self, text: str) -> None:
        self.status_label.setText(text)
        lowered = text.lower()
        if "fail" in lowered or "cannot" in lowered or "emergency" in lowered:
            colour = ERROR
        elif "wait" in lowered or "paus" in lowered or "lost focus" in lowered:
            colour = WARN
        elif "finish" in lowered or "complete" in lowered:
            colour = OK
        else:
            colour = TEXT
        self.status_label.setStyleSheet(f"color: {colour};")

    def _set_hud(self, snapshot: object) -> None:
        if not isinstance(snapshot, HudSnapshot):
            return
        self.hud_label.setText(snapshot.describe())
        if snapshot.wave is None or snapshot.cash is None:
            self.hud_label.setStyleSheet(f"color: {TEXT_DIM};")
            self.hud_label.setToolTip(
                "\n".join(snapshot.problems) or "Nothing read from the HUD yet."
            )
        else:
            self.hud_label.setStyleSheet(f"color: {OK};")
            self.hud_label.setToolTip(
                f"Confidence: wave {snapshot.wave_confidence:.2f}, "
                f"cash {snapshot.cash_confidence:.2f}"
            )

    def _set_window_label(self, text: str) -> None:
        self.window_label.setText(f"Roblox: {text}")

    def _refresh_run_state(self) -> None:
        busy = self.controller.busy
        recording = self.controller.recorder is not None
        self.start_button.setEnabled(not busy)
        self.record_button.setText("Stop recording" if recording else "Record  (F6)")
        self.stop_button.setEnabled(True)  # always available
        if not busy:
            self.status_label.setText(self.status_label.text() or "Idle")

    def _refresh_summary(self) -> None:
        strategy = self.controller.strategy
        enabled = sum(1 for s in strategy.steps if s.enabled)
        problems = strategy.problems_by_step()
        estimate = strategy.estimated_ms() / 1000
        parts = [f"{enabled} of {len(strategy.steps)} step(s) enabled"]
        if estimate:
            parts.append(f"about {estimate:.1f}s of fixed timing")
        if problems:
            parts.append(f"{len(problems)} problem(s) -- see Tools > Validate")
        self.summary_label.setText("   |   ".join(parts))
        self.summary_label.setStyleSheet(f"color: {ERROR if problems else TEXT_DIM};")

    # -- actions ---------------------------------------------------------------
    def _start(self) -> None:
        self.controller.start_playback()

    def _set_dry_run(self, on: bool) -> None:
        self.controller.settings.dry_run = on
        self.controller.emit_log(
            "info", "Dry run on -- nothing will be sent." if on else "Dry run off."
        )

    def _set_loop(self, on: bool) -> None:
        self.controller.settings.loop.enabled = on
        if on:
            missing = self.controller.calibration.missing_for_loop()
            if missing:
                self.controller.emit_log(
                    "warn",
                    "Loop mode needs these screens captured first: " + ", ".join(missing),
                )

    def _on_name_edited(self, text: str) -> None:
        self.controller.strategy.name = text
        self.controller.dirty = True

    def _on_selection(self) -> None:
        row = self._selected_row()
        if row < 0:
            self.editor.clear()
            return
        self.editor.set_step(self.controller.strategy, row, self.model.problems_for(row))

    def _on_step_edited(self) -> None:
        row = self.editor.row
        self.controller.dirty = True
        self.model.step_changed(row)
        step = self.model.step_at(row)
        problems = "  |  ".join(f"Problem: {p}" for p in self.model.problems_for(row))
        self.editor.hint.setText(problems or (step.describe() if step is not None else ""))
        self._refresh_summary()

    def _add_step(self) -> None:
        _, cls = NEW_STEPS[self.add_combo.currentIndex()]
        strategy = self.controller.strategy
        if cls is Place:
            step: Step = Place(
                slot=1, position=NormPoint(0.5, 0.5), tower_id=strategy.next_tower_id()
            )
        elif cls in (Upgrade, Sell, Ability):
            placements = list(strategy.placements())
            step = cls(tower_id=placements[-1] if placements else "T1")
        else:
            step = cls()
        row = self._selected_row()
        inserted = (
            self.model.insert_step(row + 1, step) if row >= 0 else self.model.append_step(step)
        )
        self.controller.dirty = True
        self._select_row(inserted)
        self._refresh_summary()

    def _duplicate_step(self) -> None:
        row = self._selected_row()
        if row < 0:
            return
        new_row = self.model.duplicate_step(row)
        self.controller.dirty = True
        if new_row is not None:
            self._select_row(new_row)
        self._refresh_summary()

    def _delete_step(self) -> None:
        row = self._selected_row()
        if row < 0:
            return
        if self.model.remove_step(row):
            self.controller.dirty = True
            self._select_row(min(row, self.model.rowCount() - 1))
            self._on_selection()
            self._refresh_summary()

    def _shift(self, delta: int) -> None:
        row = self._selected_row()
        if row < 0:
            return
        target = self.model.shift_step(row, delta)
        if target is not None:
            self.controller.dirty = True
            self._select_row(target)
            self._refresh_summary()

    def _toggle_step(self) -> None:
        row = self._selected_row()
        if row < 0:
            return
        self.model.toggle_enabled(row)
        self.controller.dirty = True
        self._select_row(row)
        self._refresh_summary()

    def _pick_position(self) -> None:
        client, frame = self.controller.hud.grab_client_frame()
        if client is None or frame is None:
            QMessageBox.warning(
                self,
                "No screenshot",
                "Could not capture the Roblox window. Make sure Roblox is running and "
                "not minimized, then try again.",
            )
            return
        dialog = PickerDialog(
            numpy_to_qimage(np.asarray(frame)),
            "Pick a placement point",
            "Click where this tower should be placed.",
            mode="point",
            markers=self.controller.placement_points(),
            parent=self,
        )
        if dialog.exec() == PickerDialog.DialogCode.Accepted and dialog.result_point:
            self.editor.update_position(dialog.result_point)

    def _preview_points(self) -> None:
        client, frame = self.controller.hud.grab_client_frame()
        if client is None or frame is None:
            QMessageBox.warning(
                self, "No screenshot", "Could not capture the Roblox window."
            )
            return
        points = self.controller.placement_points()
        boxes = [
            (name, region.box)
            for name, region in (
                ("wave", self.controller.calibration.wave),
                ("cash", self.controller.calibration.cash),
            )
            if region.box is not None
        ]
        PickerDialog(
            numpy_to_qimage(np.asarray(frame)),
            "Placement points",
            f"{len(points)} placement point(s) and the calibrated HUD regions.",
            mode="view",
            markers=points,
            boxes=boxes,
            parent=self,
        ).exec()

    def _validate(self) -> None:
        problems = self.controller.strategy.validate()
        if not problems:
            QMessageBox.information(
                self, "Strategy looks fine", "No problems found in this strategy."
            )
            return
        QMessageBox.warning(self, "Problems found", "\n".join(problems))
        for problem in problems:
            self.controller.emit_log("warn", problem)

    def _refind(self) -> None:
        if self.controller.refresh_window():
            self.controller.emit_log("info", f"Found {self.controller.window.describe()}")
        else:
            self.controller.emit_log("error", self.controller.window.last_error)

    def _run_wizard(self) -> None:
        SetupWizard(self.controller, self).exec()
        self.controller.apply_hotkeys()
        self._refresh_summary()

    def _open_settings(self) -> None:
        dialog = SettingsDialog(self.controller.settings, self._pick_menu_point, self)
        if dialog.exec() != SettingsDialog.DialogCode.Accepted:
            return
        self.controller.save_settings()
        self.dry_run_box.setChecked(self.controller.settings.dry_run)
        self.loop_box.setChecked(self.controller.settings.loop.enabled)
        self.controller.apply_hotkeys()

    def _pick_menu_point(self) -> NormPoint | None:
        client, frame = self.controller.hud.grab_client_frame()
        if client is None or frame is None:
            QMessageBox.warning(self, "No screenshot", "Could not capture the Roblox window.")
            return None
        dialog = PickerDialog(
            numpy_to_qimage(np.asarray(frame)),
            "Pick a menu button",
            "Click the middle of the button this action should press.",
            mode="point",
            parent=self,
        )
        if dialog.exec() == PickerDialog.DialogCode.Accepted:
            return dialog.result_point
        return None

    def _about(self) -> None:
        AboutDialog(
            __version__, self.controller.dpi_mode, str(self.controller.store.path), self
        ).exec()

    # -- strategy files ---------------------------------------------------------
    def _confirm_discard(self) -> bool:
        if not self.controller.dirty:
            return True
        answer = QMessageBox.question(
            self,
            "Unsaved changes",
            "This strategy has unsaved changes. Discard them?",
            QMessageBox.StandardButton.Discard | QMessageBox.StandardButton.Cancel,
        )
        return answer == QMessageBox.StandardButton.Discard

    def _reload_strategy(self) -> None:
        self.model.set_strategy(self.controller.strategy)
        self.name_edit.setText(self.controller.strategy.name)
        self.editor.clear()
        self._refresh_summary()

    def _new_strategy(self) -> None:
        if not self._confirm_discard():
            return
        self.controller.strategy = Strategy()
        self.controller.strategy_path = None
        self.controller.dirty = False
        self._reload_strategy()

    def _strategies_dir(self) -> str:
        folder = self.controller.store.strategies_dir()
        folder.mkdir(parents=True, exist_ok=True)
        return str(folder)

    def _open_strategy(self) -> None:
        if not self._confirm_discard():
            return
        path, _ = QFileDialog.getOpenFileName(
            self, "Open strategy", self._strategies_dir(), "Strategy files (*.json)"
        )
        if path and self.controller.load_strategy(Path(path)):
            self.controller.save_settings()

    def _save_strategy(self) -> None:
        if self.controller.strategy_path is None:
            self._save_strategy_as()
            return
        if self.controller.save_strategy(self.controller.strategy_path):
            self.controller.save_settings()

    def _save_strategy_as(self) -> None:
        suggested = self.controller.strategy.name.strip() or "strategy"
        path, _ = QFileDialog.getSaveFileName(
            self,
            "Save strategy",
            str(Path(self._strategies_dir()) / f"{suggested}.json"),
            "Strategy files (*.json)",
        )
        if path and self.controller.save_strategy(Path(path)):
            self.controller.save_settings()

    # -- shutdown ---------------------------------------------------------------
    def closeEvent(self, event: QCloseEvent) -> None:
        if self.controller.busy:
            answer = QMessageBox.question(
                self,
                "Still running",
                "A macro is still running. Stop it and quit?",
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            )
            if answer != QMessageBox.StandardButton.Yes:
                event.ignore()
                return
        if not self._confirm_discard():
            event.ignore()
            return
        self.controller.save_settings()
        event.accept()
