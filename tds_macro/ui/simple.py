"""The simple, TinyTask-style home screen.

Record, play, stop -- and nothing else is required. Every recording saves
itself and appears in the list. Everything else the app can do (HUD reading,
the step editor, loop set-up) is one optional click away instead of standing
between the user and their first replay.
"""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QHBoxLayout,
    QLabel,
    QPlainTextEdit,
    QPushButton,
    QSpinBox,
    QVBoxLayout,
    QWidget,
)

from ..input import platform as host
from .controller import AppController
from .theme import OK, TEXT_DIM, WARN

MAX_LOG_LINES = 200


class SimplePanel(QWidget):
    """Record / Play / Stop plus the few things a beginner might want."""

    advancedRequested = Signal()
    importVideoRequested = Signal()
    hudSetupRequested = Signal()
    loopSetupRequested = Signal()
    permissionsRequested = Signal()

    def __init__(self, controller: AppController, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.controller = controller
        self._loading_list = False

        self.status = QLabel("Ready. Press Record, play a match, press Record again.")
        self.status.setObjectName("status")
        self.status.setWordWrap(True)
        self.hud = QLabel("Wave --   $--")
        self.hud.setObjectName("hud")
        self.hud.setToolTip("What the app currently reads from your HUD, if set up.")

        self.permission_banner = QLabel()
        self.permission_banner.setObjectName("banner")
        self.permission_banner.setWordWrap(True)
        self.permission_button = QPushButton("Allow TDS Macro on this Mac...")
        self.permission_button.clicked.connect(self.permissionsRequested.emit)

        self.record_button = QPushButton()
        self.record_button.setObjectName("primary")
        self.record_button.setToolTip(
            "Records every click, key press, camera drag and scroll you make in "
            "Roblox. Press again to stop; the recording saves itself."
        )
        self.play_button = QPushButton()
        self.play_button.setObjectName("primary")
        self.play_button.setToolTip(
            "Replays the selected recording. Click on Roblox first -- input only "
            "goes to Roblox while it is the active window."
        )
        self.stop_button = QPushButton()
        self.stop_button.setObjectName("danger")
        self.stop_button.setToolTip("Stops everything immediately and lets go of all keys.")

        self.loop_box = QCheckBox("Repeat matches")
        self.loop_box.setToolTip(
            "Play again automatically after each match. Needs the lobby screens set "
            "up once (the 'Set up...' button beside this)."
        )
        self.loop_runs = QSpinBox()
        self.loop_runs.setRange(0, 10_000)
        self.loop_runs.setSpecialValueText("forever")
        self.loop_runs.setSuffix(" times")
        self.loop_runs.setToolTip("How many matches to play. 'forever' runs until you stop it.")
        loop_setup = QPushButton("Set up...")
        loop_setup.setToolTip("Capture the end-of-match and lobby screens loop mode clicks.")
        loop_setup.clicked.connect(self.loopSetupRequested.emit)
        self.dry_run_box = QCheckBox("Practice (send nothing)")
        self.dry_run_box.setToolTip(
            "Shows in the log what would be clicked and pressed, without touching Roblox."
        )

        self.recordings = QComboBox()
        self.recordings.setToolTip("Your recordings and saved strategies, newest first.")
        self.recordings.currentIndexChanged.connect(self._on_pick_recording)

        import_video = QPushButton("Import a video...")
        import_video.setToolTip(
            "Turn a screen recording -- yours or someone else's -- into a recording "
            "you can replay. You check the result before playing it."
        )
        import_video.clicked.connect(self.importVideoRequested.emit)
        self.hud_button = QPushButton()
        self.hud_button.setToolTip(
            "Optional. Teach the app to read your wave and cash, so replays wait for "
            "the right wave instead of a fixed time, and failed placements are retried."
        )
        self.hud_button.clicked.connect(self.hudSetupRequested.emit)
        advanced = QPushButton("Advanced editor")
        advanced.setToolTip("Edit recordings step by step, and every other setting.")
        advanced.clicked.connect(self.advancedRequested.emit)

        self.log = QPlainTextEdit()
        self.log.setObjectName("log")
        self.log.setReadOnly(True)
        self.log.setMaximumBlockCount(MAX_LOG_LINES)
        self.log.setMinimumHeight(120)

        big = QHBoxLayout()
        for button in (self.record_button, self.play_button, self.stop_button):
            button.setMinimumHeight(64)
            big.addWidget(button)

        options = QHBoxLayout()
        options.addWidget(self.loop_box)
        options.addWidget(self.loop_runs)
        options.addWidget(loop_setup)
        options.addSpacing(18)
        options.addWidget(self.dry_run_box)
        options.addStretch(1)

        chooser = QHBoxLayout()
        chooser.addWidget(QLabel("Recording:"))
        chooser.addWidget(self.recordings, 1)

        extras = QHBoxLayout()
        extras.addWidget(import_video)
        extras.addWidget(self.hud_button)
        extras.addStretch(1)
        extras.addWidget(advanced)

        banner = QHBoxLayout()
        banner.addWidget(self.permission_banner, 1)
        banner.addWidget(self.permission_button)

        top = QHBoxLayout()
        top.addWidget(self.status, 1)
        top.addWidget(self.hud)

        layout = QVBoxLayout(self)
        layout.addLayout(banner)
        layout.addLayout(top)
        layout.addLayout(big)
        layout.addLayout(chooser)
        layout.addLayout(options)
        layout.addLayout(extras)
        layout.addWidget(self.log, 1)

        self.sync_options()
        self.refresh()

    # -- state ------------------------------------------------------------------
    def sync_options(self) -> None:
        settings = self.controller.settings
        self.loop_box.setChecked(settings.loop.enabled)
        self.loop_runs.setValue(settings.loop.runs_target)
        self.dry_run_box.setChecked(settings.dry_run)

    def apply_options(self) -> None:
        settings = self.controller.settings
        settings.loop.enabled = self.loop_box.isChecked()
        settings.loop.runs_target = self.loop_runs.value()
        settings.dry_run = self.dry_run_box.isChecked()

    def refresh(self) -> None:
        """Re-read everything that can change outside this panel."""
        self.refresh_recordings()
        self.refresh_permissions()
        ready = self.controller.calibration.hud_ready
        self.hud_button.setText(
            "Wave && cash reading: ON" if ready else "Set up wave && cash reading (optional)..."
        )
        self.hud_button.setStyleSheet(f"color: {OK};" if ready else "")

    def refresh_permissions(self) -> None:
        missing = [
            p for p in self.controller.permissions() if p.granted is not True
        ]
        show = self.controller.platform == host.MAC and bool(missing)
        self.permission_banner.setVisible(show)
        self.permission_button.setVisible(show)
        if show:
            names = ", ".join(p.title for p in missing)
            self.permission_banner.setText(
                f"macOS has not allowed TDS Macro yet ({names}). Until you switch these "
                "on, recording and playback cannot reach Roblox."
            )

    def refresh_recordings(self) -> None:
        self._loading_list = True
        try:
            self.recordings.clear()
            current = self.controller.strategy_path
            for path in self.controller.recent_strategies():
                self.recordings.addItem(path.stem, str(path))
            if current is not None:
                index = self.recordings.findData(str(current))
                if index < 0:
                    self.recordings.insertItem(0, current.stem, str(current))
                    index = 0
                self.recordings.setCurrentIndex(index)
            elif self.controller.strategy.steps:
                self.recordings.insertItem(0, f"{self.controller.strategy.name} (unsaved)", "")
                self.recordings.setCurrentIndex(0)
            if self.recordings.count() == 0:
                self.recordings.addItem("No recordings yet -- press Record", "")
        finally:
            self._loading_list = False

    def _on_pick_recording(self, index: int) -> None:
        if self._loading_list or index < 0:
            return
        path = self.recordings.itemData(index)
        if path and Path(path) != self.controller.strategy_path:
            self.controller.load_strategy(Path(path), backup_unsaved=True)

    def set_labels(self, label: Callable[[str], str], recording: bool) -> None:
        self.record_button.setText(
            "Stop recording" if recording else f"Record\n({label('record')})"
        )
        self.play_button.setText(f"Play\n({label('play')})")
        self.stop_button.setText(f"Stop\n({label('stop')})")

    def set_busy(self, busy: bool, recording: bool) -> None:
        self.play_button.setEnabled(not busy)
        self.record_button.setEnabled(not busy or recording)

    def set_status(self, text: str, colour: str) -> None:
        self.status.setText(text)
        self.status.setStyleSheet(f"color: {colour};")

    def set_hud(self, text: str, live: bool) -> None:
        self.hud.setText(text)
        self.hud.setStyleSheet(f"color: {OK if live else TEXT_DIM};")

    def append_log(self, html: str) -> None:
        self.log.appendHtml(html)


class PermissionsDialog(QDialog):
    """The wizard's macOS permissions page, on its own."""

    def __init__(self, controller: AppController, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        from .wizard import MacPermissionsPage

        self.setWindowTitle("Allow TDS Macro on your Mac")
        self.page = MacPermissionsPage(controller)
        self.page.initializePage()
        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Close)
        buttons.rejected.connect(self.reject)
        note = QLabel("Quit and reopen TDS Macro after switching anything on.")
        note.setStyleSheet(f"color: {WARN};")
        layout = QVBoxLayout(self)
        layout.addWidget(self.page)
        layout.addWidget(note, 0, Qt.AlignmentFlag.AlignLeft)
        layout.addWidget(buttons)
        self.resize(640, 420)
