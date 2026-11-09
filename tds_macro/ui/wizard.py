"""First-run setup wizard: find Roblox, calibrate the HUD, test a click.

The HUD step asks the user to *type what the readout says* rather than to label
anonymous glyph bitmaps.  Same result, far less to explain, and it teaches
several glyphs per capture.
"""

from __future__ import annotations

import numpy as np
from PySide6.QtCore import Qt, QTimer
from PySide6.QtGui import QPixmap
from PySide6.QtWidgets import (
    QCheckBox,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QPushButton,
    QVBoxLayout,
    QWidget,
    QWizard,
    QWizardPage,
)

from ..input.coords import NormPoint
from ..input.sendinput import InputBackend, MouseButton
from ..input.window import enumerate_windows
from ..vision.calibration import (
    LOOP_TEMPLATES,
    TEMPLATE_END_SCREEN,
    TEMPLATE_MAP_TILE,
    TEMPLATE_PLAY_AGAIN,
    TEMPLATE_PLAY_BUTTON,
    HudRegion,
)
from ..vision.capture import NormBox, crop_norm
from ..vision.digits import glyph_previews, learn_from_text, missing_digits, read_number
from ..vision.templates import capture_template
from .controller import AppController
from .pickers import PickerDialog, numpy_to_qimage
from .theme import ERROR, OK, TEXT_DIM, WARN

TEMPLATE_PROMPTS = {
    TEMPLATE_END_SCREEN: (
        "End-of-match screen",
        "Something that only appears when a match has ended -- the results panel "
        "heading works well.",
    ),
    TEMPLATE_PLAY_AGAIN: (
        "'Play Again' button",
        "Drag a box around the Play Again button itself.",
    ),
    TEMPLATE_MAP_TILE: (
        "Map tile in the lobby",
        "The map you want to replay, as it appears in the map list.",
    ),
    TEMPLATE_PLAY_BUTTON: (
        "'Play' / confirm button",
        "The button that actually starts the match after choosing the map.",
    ),
}


def _status(label: QLabel, text: str, colour: str) -> None:
    label.setText(text)
    label.setStyleSheet(f"color: {colour};")


class _Page(QWizardPage):
    def __init__(self, controller: AppController, title: str, subtitle: str) -> None:
        super().__init__()
        self.controller = controller
        self.setTitle(title)
        self.setSubTitle(subtitle)


class IntroPage(_Page):
    def __init__(self, controller: AppController) -> None:
        super().__init__(
            controller,
            "Welcome to TDS Macro",
            "Three short steps: find Roblox, teach the app to read your HUD, and test a click.",
        )
        body = QLabel(
            "<p>This app plays Tower Defense Simulator by moving your mouse and "
            "pressing keys, and by reading the screen. It never touches the Roblox "
            "program itself.</p>"
            "<p><b>Before you continue:</b> start Roblox, join a TDS match, and leave "
            "it running in a window you can see. Do not use fullscreen-exclusive "
            "mode.</p>"
            "<p style='color:#f0b429'><b>Automating Roblox is against its terms of "
            "service and accounts have been banned for it. Use an account you are "
            "willing to lose.</b></p>"
        )
        body.setWordWrap(True)
        body.setTextFormat(Qt.TextFormat.RichText)
        layout = QVBoxLayout(self)
        layout.addWidget(body)


class WindowPage(_Page):
    """Find the Roblox window, with a manual picker as a fallback."""

    def __init__(self, controller: AppController) -> None:
        super().__init__(
            controller,
            "Find the Roblox window",
            "The app needs to know which window is the game.",
        )
        self.status = QLabel("Not searched yet.")
        self.status.setWordWrap(True)

        find = QPushButton("Find Roblox automatically")
        find.setToolTip("Looks for the Roblox game window by its program name.")
        find.clicked.connect(self._auto)

        self.list = QListWidget()
        self.list.setToolTip(
            "Every open window. Use this if the automatic search picks the wrong "
            "one, which can happen after a Roblox update."
        )
        self.list.itemSelectionChanged.connect(self._manual)

        refresh = QPushButton("Refresh list")
        refresh.clicked.connect(self._fill_list)

        row = QHBoxLayout()
        row.addWidget(find)
        row.addWidget(refresh)
        row.addStretch(1)

        layout = QVBoxLayout(self)
        layout.addLayout(row)
        layout.addWidget(self.status)
        layout.addWidget(QLabel("Or choose the window yourself:"))
        layout.addWidget(self.list, 1)

    def initializePage(self) -> None:
        self._auto()
        self._fill_list()

    def _auto(self) -> None:
        if self.controller.refresh_window():
            self.controller.window.unpin()
            _status(self.status, f"Found it: {self.controller.window.describe()}", OK)
        else:
            _status(
                self.status,
                self.controller.window.last_error
                or "Could not find Roblox. Is it running, and not minimized?",
                ERROR,
            )
        self.completeChanged.emit()

    def _fill_list(self) -> None:
        self.list.clear()
        for window in enumerate_windows():
            if not window.title:
                continue
            item = QListWidgetItem(window.describe())
            item.setData(Qt.ItemDataRole.UserRole, window.hwnd)
            self.list.addItem(item)

    def _manual(self) -> None:
        items = self.list.selectedItems()
        if not items:
            return
        hwnd = int(items[0].data(Qt.ItemDataRole.UserRole))
        # Pinned for this session only; window handles are reused by Windows,
        # so a saved one could name an unrelated window next time.
        self.controller.window.pin(hwnd)
        rect = self.controller.client_rect()
        if rect is None:
            _status(self.status, "That window cannot be measured -- pick another.", ERROR)
        else:
            _status(
                self.status,
                f"Using '{items[0].text()}' -- client area {rect.width}x{rect.height}",
                OK,
            )
        self.completeChanged.emit()

    def isComplete(self) -> bool:
        return self.controller.client_rect() is not None


class HudPage(_Page):
    """Draw boxes around the readouts, then teach the glyphs by typing them."""

    def __init__(self, controller: AppController) -> None:
        super().__init__(
            controller,
            "Teach the app to read your HUD",
            "Mark the wave and cash numbers, then type what they currently say.",
        )
        self.frame: np.ndarray | None = None
        self.wave_region = HudRegion()
        self.cash_region = HudRegion()

        capture = QPushButton("Take a screenshot of Roblox")
        capture.setToolTip("Grabs the current game window so you can mark the readouts.")
        capture.clicked.connect(self._capture)

        self.wave_button = QPushButton("Mark the WAVE number...")
        self.wave_button.clicked.connect(lambda: self._mark("wave"))
        self.cash_button = QPushButton("Mark the CASH number...")
        self.cash_button.clicked.connect(lambda: self._mark("cash"))
        for button in (self.wave_button, self.cash_button):
            button.setEnabled(False)
            button.setToolTip("Drag a box around only the number -- no icons or labels.")

        self.wave_preview = QLabel("no region")
        self.cash_preview = QLabel("no region")
        for preview in (self.wave_preview, self.cash_preview):
            preview.setStyleSheet(f"border: 1px solid {TEXT_DIM}; padding: 4px;")
            preview.setMinimumHeight(48)

        self.wave_text = QLineEdit()
        self.wave_text.setPlaceholderText("what the wave counter shows, e.g. 12")
        self.cash_text = QLineEdit()
        self.cash_text.setPlaceholderText("what the cash counter shows, e.g. $1,250")
        for edit in (self.wave_text, self.cash_text):
            edit.setToolTip(
                "Type exactly what you can see, including the $ and any commas. "
                "The app pairs each character with the shape it found."
            )

        teach = QPushButton("Learn these characters")
        teach.setToolTip(
            "Teaches the app the shapes in the boxes. Repeat as the numbers change "
            "until every digit 0-9 is known."
        )
        teach.clicked.connect(self._teach)

        verify = QPushButton("Test the reading")
        verify.setToolTip("Takes a fresh screenshot and shows what the app reads.")
        verify.clicked.connect(self._verify)

        self.status = QLabel("Take a screenshot to begin.")
        self.status.setWordWrap(True)
        self.progress = QLabel()
        self.progress.setWordWrap(True)

        layout = QVBoxLayout(self)
        layout.addWidget(capture)
        for button, preview, edit in (
            (self.wave_button, self.wave_preview, self.wave_text),
            (self.cash_button, self.cash_preview, self.cash_text),
        ):
            row = QHBoxLayout()
            row.addWidget(button)
            row.addWidget(preview, 1)
            row.addWidget(edit, 1)
            layout.addLayout(row)
        buttons = QHBoxLayout()
        buttons.addWidget(teach)
        buttons.addWidget(verify)
        buttons.addStretch(1)
        layout.addLayout(buttons)
        layout.addWidget(self.progress)
        layout.addWidget(self.status)
        layout.addStretch(1)

    def initializePage(self) -> None:
        calibration = self.controller.calibration
        self.wave_region = calibration.wave
        self.cash_region = calibration.cash
        self._capture()
        self._update_progress()

    # -- capture and marking ---------------------------------------------------
    def _capture(self) -> bool:
        client, frame = self.controller.grab_game_frame()
        if client is None or frame is None:
            _status(
                self.status,
                "Could not capture the Roblox window. Make sure it is visible and "
                "not minimized.",
                ERROR,
            )
            return False
        self.frame = np.asarray(frame)
        self.wave_button.setEnabled(True)
        self.cash_button.setEnabled(True)
        self.controller.calibration.client_size = (client.width, client.height)
        _status(
            self.status, f"Captured {client.width}x{client.height}. Now mark each number.", OK
        )
        self._refresh_previews()
        return True

    def _mark(self, which: str) -> None:
        if self.frame is None and not self._capture():
            return
        assert self.frame is not None
        existing = [
            (name, region.box)
            for name, region in (("wave", self.wave_region), ("cash", self.cash_region))
            if region.box is not None
        ]
        dialog = PickerDialog(
            numpy_to_qimage(self.frame),
            f"Mark the {which} number",
            f"Drag a box around the {which.upper()} number only -- not the icon or label.",
            mode="region",
            boxes=existing,
            parent=self,
        )
        if dialog.exec() != PickerDialog.DialogCode.Accepted or dialog.result_box is None:
            return
        region = self.wave_region if which == "wave" else self.cash_region
        region.box = dialog.result_box
        self._refresh_previews()
        self._update_progress()

    def _refresh_previews(self) -> None:
        for region, preview in (
            (self.wave_region, self.wave_preview),
            (self.cash_region, self.cash_preview),
        ):
            if self.frame is None or region.box is None:
                preview.setText("no region")
                preview.setPixmap(QPixmap())
                continue
            crop = crop_norm(self.frame, region.box)
            if crop.size == 0:
                preview.setText("region is empty")
                continue
            image = numpy_to_qimage(crop)
            scale = max(1, min(6, 160 // max(1, image.width())))
            preview.setPixmap(
                QPixmap.fromImage(
                    image.scaled(
                        image.width() * scale,
                        image.height() * scale,
                        Qt.AspectRatioMode.KeepAspectRatio,
                        Qt.TransformationMode.FastTransformation,
                    )
                )
            )
            preview.setText("")

    # -- teaching --------------------------------------------------------------
    def _teach(self) -> None:
        if self.frame is None:
            _status(self.status, "Take a screenshot first.", ERROR)
            return
        messages: list[str] = []
        taught = 0
        for name, region, edit in (
            ("wave", self.wave_region, self.wave_text),
            ("cash", self.cash_region, self.cash_text),
        ):
            if region.box is None:
                messages.append(f"{name}: mark the region first")
                continue
            if not edit.text().strip():
                continue
            outcome = learn_from_text(
                crop_norm(self.frame, region.box), edit.text(), region.classifier, region.invert
            )
            if outcome.ok:
                taught += outcome.added
                found = len(glyph_previews(crop_norm(self.frame, region.box), region.invert))
                messages.append(f"{name}: learned {outcome.added} character(s) from {found}")
            else:
                messages.append(f"{name}: {outcome.error}")
        _status(
            self.status,
            " | ".join(messages) or "Type what each readout shows, then try again.",
            OK if taught else WARN,
        )
        self._update_progress()
        self.completeChanged.emit()

    def _verify(self) -> None:
        """Read both regions from a fresh capture taken with the wizard hidden.

        Reads the captured frame directly rather than asking the live HUD
        reader, which would take its own screenshot -- with the wizard on top.
        """
        if not self._capture():
            return
        assert self.frame is not None
        results = []
        for name, region in (("wave", self.wave_region), ("cash", self.cash_region)):
            if region.box is None:
                _status(self.status, f"Mark the {name} region first.", ERROR)
                return
            result = read_number(
                crop_norm(self.frame, region.box), region.classifier, region.invert
            )
            if not result.ok:
                _status(self.status, f"{name}: {result.reason or 'nothing recognised'}", ERROR)
                return
            results.append(result)
        wave, cash = results
        calibration = self.controller.calibration
        calibration.wave = self.wave_region
        calibration.cash = self.cash_region
        self.controller.hud.calibration = calibration
        self.controller.hud.reset()
        _status(
            self.status,
            f"Read wave {wave.value} and cash ${cash.value:,} "
            f"(confidence {min(wave.confidence, cash.confidence):.2f}). "
            "If that is wrong, teach more characters.",
            OK,
        )

    def _update_progress(self) -> None:
        parts: list[str] = []
        for name, region in (("Wave", self.wave_region), ("Cash", self.cash_region)):
            missing = missing_digits(region.classifier)
            if region.box is None:
                parts.append(f"{name}: region not marked")
            elif missing:
                parts.append(f"{name}: still needs {' '.join(missing)}")
            else:
                parts.append(f"{name}: all digits known")
        self.progress.setText("   ".join(parts))

    def validatePage(self) -> bool:
        calibration = self.controller.calibration
        calibration.wave = self.wave_region
        calibration.cash = self.cash_region
        self.controller.hud.calibration = calibration
        self.controller.save_calibration()
        return True

    def isComplete(self) -> bool:
        return self.wave_region.is_ready and self.cash_region.is_ready


class TestClickPage(_Page):
    """Send one real click, so the user sees input working before trusting it.

    The click is sent after a countdown. Input only goes to Roblox while it is
    the focused window, and pressing "Send the click" focuses the wizard -- so
    sending immediately could never succeed. The countdown gives the user time
    to click back into Roblox first.
    """

    COUNTDOWN_S = 3

    def __init__(self, controller: AppController) -> None:
        super().__init__(
            controller,
            "Test a click",
            "Prove the app can actually reach Roblox before you rely on it.",
        )
        self.point: NormPoint | None = None
        self._remaining = 0
        self._backend: InputBackend | None = None

        pick = QPushButton("Choose a harmless spot...")
        pick.setToolTip("Somewhere a click does nothing -- empty sky or an unused corner.")
        pick.clicked.connect(self._pick)

        self.dry = QCheckBox("Dry run (log only, send nothing)")
        self.dry.setChecked(False)
        self.dry.setToolTip("Tick this to check the plumbing without sending a real click.")

        self.send_button = QPushButton("Send the click")
        self.send_button.setToolTip(
            f"Starts a {self.COUNTDOWN_S}-second countdown. Click on the Roblox window "
            "during it; the test click is sent when it reaches zero."
        )
        self.send_button.clicked.connect(self._start_countdown)

        self.status = QLabel("Choose a spot, then send a test click.")
        self.status.setWordWrap(True)

        self._timer = QTimer(self)
        self._timer.setInterval(1000)
        self._timer.timeout.connect(self._countdown_tick)

        layout = QVBoxLayout(self)
        layout.addWidget(pick)
        layout.addWidget(self.dry)
        layout.addWidget(self.send_button)
        layout.addWidget(self.status)
        layout.addStretch(1)

    def _pick(self) -> None:
        client, frame = self.controller.grab_game_frame()
        if client is None or frame is None:
            _status(self.status, "Could not capture the Roblox window.", ERROR)
            return
        dialog = PickerDialog(
            numpy_to_qimage(np.asarray(frame)),
            "Choose a test spot",
            "Click somewhere harmless -- empty sky, or a corner with nothing in it.",
            mode="point",
            parent=self,
        )
        if dialog.exec() == PickerDialog.DialogCode.Accepted and dialog.result_point:
            self.point = dialog.result_point
            _status(
                self.status,
                f"Test spot set to x {self.point.x:.3f}, y {self.point.y:.3f}.",
                OK,
            )

    def _start_countdown(self) -> None:
        if self.point is None:
            _status(self.status, "Choose a spot first.", WARN)
            return
        if not self.controller.refresh_window():
            _status(self.status, self.controller.window.last_error, ERROR)
            return
        self._remaining = self.COUNTDOWN_S
        self.send_button.setEnabled(False)
        self._show_countdown()
        self._timer.start()

    def _show_countdown(self) -> None:
        _status(
            self.status,
            f"Click on the Roblox window now -- sending the test click in {self._remaining}...",
            WARN,
        )

    def _countdown_tick(self) -> None:
        self._remaining -= 1
        if self._remaining > 0:
            self._show_countdown()
            return
        self._timer.stop()
        self.send_now()

    def send_now(self) -> None:
        """Move, press, and release a moment later, like a real click."""
        assert self.point is not None
        backend = self.controller.make_backend(dry_run=self.dry.isChecked())
        self._backend = backend
        timing = self.controller.settings.timing
        if not backend.move(self.point):
            self._finish(False)
            return
        QTimer.singleShot(timing.move_settle_ms, self._press)

    def _press(self) -> None:
        backend = self._backend
        if backend is None or not backend.mouse_down(MouseButton.LEFT):
            self._finish(False)
            return
        QTimer.singleShot(self.controller.settings.timing.click_hold_ms, self._release)

    def _release(self) -> None:
        backend = self._backend
        ok = backend is not None and backend.mouse_up(MouseButton.LEFT)
        self._finish(ok)

    def _finish(self, ok: bool) -> None:
        backend = self._backend
        if backend is not None:
            backend.release_all()
        self.send_button.setEnabled(True)
        if ok:
            _status(
                self.status,
                "Click sent. You should have seen the cursor jump to your spot in Roblox. "
                "If it did not, check Help > About and run tools/win_check.py.",
                OK,
            )
            return
        error = backend.last_error() if backend is not None else ""
        if "foreground" in error:
            error = (
                "Roblox was not the active window when the countdown finished. "
                "Press Send, then click on Roblox before it reaches zero."
            )
        _status(self.status, f"The click did not go through: {error}", ERROR)


class LoopTemplatePage(_Page):
    """Optional: capture the lobby screens loop mode clicks through."""

    def __init__(self, controller: AppController) -> None:
        super().__init__(
            controller,
            "Loop mode screens (optional)",
            "Only needed if you want matches to repeat automatically.",
        )
        self.status = QLabel(
            "Skip this for now if you just want to replay a strategy once. You can "
            "come back from Tools > Calibration wizard."
        )
        self.status.setWordWrap(True)

        layout = QVBoxLayout(self)
        layout.addWidget(self.status)
        self.rows: dict[str, QLabel] = {}
        for name in LOOP_TEMPLATES:
            label, hint = TEMPLATE_PROMPTS[name]
            button = QPushButton(f"Capture: {label}")
            button.setToolTip(hint)
            button.clicked.connect(lambda _=False, key=name: self._capture(key))
            state = QLabel()
            self.rows[name] = state
            row = QHBoxLayout()
            row.addWidget(button)
            row.addWidget(state, 1)
            layout.addLayout(row)
        layout.addStretch(1)

    def initializePage(self) -> None:
        self._refresh()

    def _refresh(self) -> None:
        for name, label in self.rows.items():
            if name in self.controller.calibration.templates:
                _status(label, "captured", OK)
            else:
                _status(label, "not captured", TEXT_DIM)

    def _capture(self, name: str) -> None:
        client, frame = self.controller.grab_game_frame()
        if client is None or frame is None:
            _status(self.status, "Could not capture the Roblox window.", ERROR)
            return
        label, hint = TEMPLATE_PROMPTS[name]
        dialog = PickerDialog(
            numpy_to_qimage(np.asarray(frame)),
            f"Capture: {label}",
            hint,
            mode="region",
            parent=self,
        )
        if dialog.exec() != PickerDialog.DialogCode.Accepted or dialog.result_box is None:
            return
        box = dialog.result_box
        # Search a generous area around the capture rather than the whole screen:
        # a small button matched screen-wide finds the wrong thing.
        search = NormBox(
            max(0.0, box.x - box.width),
            max(0.0, box.y - box.height),
            min(1.0, box.width * 3),
            min(1.0, box.height * 3),
        )
        self.controller.calibration.templates[name] = capture_template(
            np.asarray(frame), box, name, search_box=search
        )
        self.controller.save_calibration()
        self._refresh()
        _status(self.status, f"{label} captured.", OK)


class DonePage(_Page):
    def __init__(self, controller: AppController) -> None:
        super().__init__(controller, "Ready", "Setup is complete.")
        self.body = QLabel()
        self.body.setWordWrap(True)
        self.body.setTextFormat(Qt.TextFormat.RichText)
        layout = QVBoxLayout(self)
        layout.addWidget(self.body)

    def initializePage(self) -> None:
        settings = self.controller.settings
        calibration = self.controller.calibration
        missing = calibration.missing_for_loop()
        self.body.setText(
            "<p><b>What to do next</b></p>"
            f"<ul><li>Press <b>{settings.hotkeys.record}</b> to record while you play, "
            "then stop with the same key.</li>"
            f"<li>Press <b>{settings.hotkeys.play}</b> to replay it.</li>"
            f"<li><b>{settings.hotkeys.stop}</b> stops everything immediately.</li></ul>"
            "<p>Turn on <b>Dry run</b> in Settings for the first replay -- it logs what "
            "would happen without sending anything.</p>"
            f"<p>HUD: {calibration.describe()}.</p>"
            + (
                f"<p style='color:#f0b429'>Loop mode still needs: {', '.join(missing)}.</p>"
                if missing
                else ""
            )
        )


class SetupWizard(QWizard):
    """The whole first-run flow."""

    def __init__(self, controller: AppController, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.controller = controller
        self.setWindowTitle("TDS Macro -- Setup")
        self.setWizardStyle(QWizard.WizardStyle.ModernStyle)
        self.setOption(QWizard.WizardOption.NoBackButtonOnStartPage, True)
        self.addPage(IntroPage(controller))
        self.addPage(WindowPage(controller))
        self.addPage(HudPage(controller))
        self.addPage(TestClickPage(controller))
        self.addPage(LoopTemplatePage(controller))
        self.addPage(DonePage(controller))
        self.resize(760, 620)

    def accept(self) -> None:
        self.controller.settings.setup_complete = True
        self.controller.save_settings()
        self.controller.save_calibration()
        super().accept()
