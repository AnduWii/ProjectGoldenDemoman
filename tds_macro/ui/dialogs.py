"""Settings dialog.

Hotkeys are validated as you type (parse errors and duplicate bindings shown
inline) because the alternative is a hotkey that simply never fires, with no
indication why.
"""

from __future__ import annotations

from collections.abc import Callable

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QFormLayout,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QPushButton,
    QSpinBox,
    QTabWidget,
    QVBoxLayout,
    QWidget,
)

from ..config import ActionBinding, Settings
from ..input.coords import NormPoint
from ..input.keys import KeyParseError, parse_hotkey, validate_keybind
from .theme import ERROR, OK, TEXT_DIM

# Supplied by the main window: capture the game window, let the user click a
# menu button, and hand back its normalized position (or None if cancelled).
PickPoint = Callable[[], NormPoint | None]

ACTION_LABELS = {
    "upgrade": "Upgrade",
    "sell": "Sell",
    "ability": "Ability",
    "skip_wave": "Skip wave",
}


class BindingRow(QWidget):
    """Edits one :class:`ActionBinding` -- a key, or a click on a menu button."""

    def __init__(
        self,
        binding: ActionBinding,
        pick: PickPoint,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self.binding = binding
        self._pick = pick

        self.mode = QComboBox()
        self.mode.addItem("Press a key", "key")
        self.mode.addItem("Click a menu button", "click")
        self.mode.setCurrentIndex(0 if binding.mode == "key" else 1)
        self.mode.setToolTip(
            "TDS exposes some actions as on-screen buttons rather than keys. "
            "Pick whichever your game actually uses."
        )
        self.mode.currentIndexChanged.connect(self._on_mode)

        self.key = QLineEdit(binding.key)
        self.key.setPlaceholderText("e.g. e")
        self.key.setMaximumWidth(90)
        self.key.setToolTip("The key you have bound in TDS for this action.")
        self.key.textEdited.connect(self._on_key)

        self.point_button = QPushButton("Pick button...")
        self.point_button.setToolTip(
            "Capture the Roblox window and click where the button is. Stored as a "
            "fraction of the window."
        )
        self.point_button.clicked.connect(self._on_pick)

        self.status = QLabel()
        self.status.setObjectName("hint")

        layout = QHBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.addWidget(self.mode)
        layout.addWidget(self.key)
        layout.addWidget(self.point_button)
        layout.addWidget(self.status, 1)
        self._sync()

    def _on_mode(self) -> None:
        self.binding.mode = str(self.mode.currentData())
        self._sync()

    def _on_key(self, text: str) -> None:
        self.binding.key = text.strip()
        self._sync()

    def _on_pick(self) -> None:
        point = self._pick()
        if point is not None:
            self.binding.point = point
            self.binding.mode = "click"
            self.mode.setCurrentIndex(1)
        self._sync()

    def _sync(self) -> None:
        is_key = self.binding.mode == "key"
        self.key.setVisible(is_key)
        self.point_button.setVisible(not is_key)
        if is_key and self.binding.key:
            try:
                validate_keybind(self.binding.key)
                self.status.setText("ok")
                self.status.setStyleSheet(f"color: {OK};")
                return
            except KeyParseError as exc:
                self.status.setText(str(exc))
                self.status.setStyleSheet(f"color: {ERROR};")
                return
        self.status.setText(self.binding.describe())
        self.status.setStyleSheet(f"color: {TEXT_DIM};")


class SettingsDialog(QDialog):
    """Edits a copy of :class:`Settings`; the caller commits on accept."""

    def __init__(
        self, settings: Settings, pick_point: PickPoint, parent: QWidget | None = None
    ) -> None:
        super().__init__(parent)
        self.setWindowTitle("TDS Macro -- Settings")
        self.setModal(True)
        self.settings = settings
        self._pick = pick_point

        tabs = QTabWidget()
        tabs.addTab(self._hotkey_tab(), "Hotkeys")
        tabs.addTab(self._keybind_tab(), "Game keybinds")
        tabs.addTab(self._timing_tab(), "Timing")
        tabs.addTab(self._loop_tab(), "Loop mode")
        tabs.addTab(self._safety_tab(), "Safety")

        self.buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Save | QDialogButtonBox.StandardButton.Cancel
        )
        self.buttons.accepted.connect(self.accept)
        self.buttons.rejected.connect(self.reject)

        layout = QVBoxLayout(self)
        layout.addWidget(tabs)
        layout.addWidget(self.buttons)
        self.resize(720, 540)
        self._validate_hotkeys()

    # -- tabs -----------------------------------------------------------------
    def _hotkey_tab(self) -> QWidget:
        page = QWidget()
        form = QFormLayout(page)
        self.hotkey_edits: dict[str, QLineEdit] = {}
        descriptions = {
            "record": "Start and stop recording",
            "play": "Start and stop playback",
            "stop": "Emergency stop -- works from anywhere",
        }
        for name, spec in self.settings.hotkeys.as_map().items():
            edit = QLineEdit(spec)
            edit.setToolTip(
                f"{descriptions[name]}. Examples: F6, Ctrl+Shift+P, Alt+Num5. "
                "Avoid keys you use in game."
            )
            edit.textEdited.connect(self._validate_hotkeys)
            self.hotkey_edits[name] = edit
            form.addRow(descriptions[name], edit)

        self.hotkey_status = QLabel()
        self.hotkey_status.setWordWrap(True)
        form.addRow(self.hotkey_status)
        note = QLabel(
            "Hotkeys are claimed from Windows when the app starts. If another "
            "program already owns one, the log will say so and you can pick another."
        )
        note.setObjectName("hint")
        note.setWordWrap(True)
        form.addRow(note)
        return page

    def _keybind_tab(self) -> QWidget:
        page = QWidget()
        layout = QVBoxLayout(page)

        intro = QLabel(
            "These must match your own TDS keybinds -- nothing here is assumed. "
            "Check them in the game's settings screen first."
        )
        intro.setObjectName("hint")
        intro.setWordWrap(True)
        layout.addWidget(intro)

        slots_box = QGroupBox("Hotbar slots")
        slots_form = QFormLayout(slots_box)
        self.slot_edits: list[QLineEdit] = []
        for index in range(5):
            current = (
                self.settings.keybinds.slots[index]
                if index < len(self.settings.keybinds.slots)
                else ""
            )
            edit = QLineEdit(current)
            edit.setMaximumWidth(90)
            edit.setToolTip(f"Key that selects hotbar slot {index + 1} in TDS.")
            self.slot_edits.append(edit)
            slots_form.addRow(f"Slot {index + 1}", edit)
        layout.addWidget(slots_box)

        actions_box = QGroupBox("Tower actions")
        actions_form = QFormLayout(actions_box)
        self.binding_rows: dict[str, BindingRow] = {}
        for attribute, label in ACTION_LABELS.items():
            binding: ActionBinding = getattr(self.settings.keybinds, attribute)
            row = BindingRow(binding, self._pick)
            self.binding_rows[attribute] = row
            actions_form.addRow(label, row)
        layout.addWidget(actions_box)
        layout.addStretch(1)
        return page

    def _timing_tab(self) -> QWidget:
        page = QWidget()
        form = QFormLayout(page)
        timing = self.settings.timing
        self.timing_spins: dict[str, QSpinBox] = {}
        fields = [
            ("action_delay_ms", "Delay between actions", 0, 10_000,
             "Pause between consecutive inputs. Raise it if the game drops presses."),
            ("jitter_ms", "Random jitter", 0, 5_000,
             "Random variation added to each delay. Identical back-to-back timings "
             "make dropped inputs reproducible; a little spread avoids that."),
            ("key_hold_ms", "Key hold time", 1, 2_000,
             "How long a key stays down. Too short and the game may not register it."),
            ("click_hold_ms", "Click hold time", 1, 2_000, "How long the mouse button stays down."),
            ("move_settle_ms", "Pause after moving", 0, 5_000,
             "Wait after moving the cursor before clicking, so the game sees the "
             "new position first."),
            ("menu_open_ms", "Wait for tower menu", 0, 10_000,
             "Pause after clicking a tower, before pressing upgrade or sell."),
            ("place_confirm_ms", "Placement check delay", 0, 30_000,
             "How long to wait before checking whether cash dropped, which is how a "
             "failed placement is detected."),
            ("place_verify_ms", "Placement check window", 250, 30_000,
             "How long to keep watching for that drop. The HUD reader needs a few "
             "frames to settle, and giving up too early makes the app retry and place "
             "a second tower."),
            ("place_retries", "Placement retries", 0, 10,
             "Extra attempts when cash did not drop after a placement."),
            ("poll_interval_ms", "Condition poll interval", 10, 5_000,
             "How often to re-read the HUD while waiting for a wave or cash amount."),
            ("default_timeout_ms", "Default wait timeout (ms)", 0, 86_400_000,
             "Used by Wait-for steps set to 'use default'. 0 (the default) means wait "
             "forever, since a TDS wave gate is often ten minutes or more away."),
        ]
        for name, label, low, high, tip in fields:
            spin = QSpinBox()
            spin.setRange(low, high)
            spin.setValue(getattr(timing, name))
            spin.setToolTip(tip)
            self.timing_spins[name] = spin
            form.addRow(label, spin)

        reset = QPushButton("Reset to defaults")
        reset.clicked.connect(self._reset_timing)
        form.addRow(reset)
        return page

    def _reset_timing(self) -> None:
        from ..config import Timing

        defaults = Timing()
        for name, spin in self.timing_spins.items():
            spin.setValue(getattr(defaults, name))

    def _loop_tab(self) -> QWidget:
        page = QWidget()
        form = QFormLayout(page)
        loop = self.settings.loop

        self.loop_enabled = QCheckBox("Repeat matches automatically")
        self.loop_enabled.setChecked(loop.enabled)
        self.loop_enabled.setToolTip(
            "After the strategy finishes, wait for the end-of-match screen, return "
            "to the lobby, re-queue and run again. Needs the lobby screens captured "
            "in the calibration wizard."
        )
        form.addRow(self.loop_enabled)

        self.loop_runs = QSpinBox()
        self.loop_runs.setRange(0, 10_000)
        self.loop_runs.setValue(loop.runs_target)
        self.loop_runs.setSpecialValueText("forever")
        self.loop_runs.setToolTip("How many matches to play. 0 means keep going until stopped.")
        form.addRow("Number of runs", self.loop_runs)

        self.loop_play_again = QCheckBox("Try 'Play Again' before the map picker")
        self.loop_play_again.setChecked(loop.use_play_again)
        self.loop_play_again.setToolTip(
            "Faster when offered. If it is not on screen the app falls back to "
            "selecting the map and pressing Play."
        )
        form.addRow(self.loop_play_again)

        self.loop_timeout = QSpinBox()
        self.loop_timeout.setRange(5, 1_800)
        self.loop_timeout.setValue(loop.requeue_timeout_ms // 1000)
        self.loop_timeout.setSuffix(" s")
        self.loop_timeout.setToolTip(
            "How long to wait for each lobby screen (Play Again, the map, Play) "
            "before giving up and stopping."
        )
        form.addRow("Lobby screen timeout", self.loop_timeout)

        self.match_timeout = QSpinBox()
        self.match_timeout.setRange(0, 24 * 60)
        self.match_timeout.setValue(loop.match_end_timeout_ms // 60_000)
        self.match_timeout.setSuffix(" min")
        self.match_timeout.setSpecialValueText("forever")
        self.match_timeout.setToolTip(
            "How long the match may keep going after the strategy's last step "
            "before the loop gives up waiting for the end screen. Strategies "
            "usually finish many waves before the map does."
        )
        form.addRow("Longest match after the last step", self.match_timeout)

        self.loop_delay = QSpinBox()
        self.loop_delay.setRange(0, 60_000)
        self.loop_delay.setValue(loop.between_match_delay_ms)
        self.loop_delay.setSuffix(" ms")
        self.loop_delay.setToolTip("Pause after the match ends before clicking anything.")
        form.addRow("Pause after a match", self.loop_delay)
        return page

    def _safety_tab(self) -> QWidget:
        page = QWidget()
        form = QFormLayout(page)

        self.dry_run = QCheckBox("Dry run -- log actions without sending any input")
        self.dry_run.setChecked(self.settings.dry_run)
        self.dry_run.setToolTip(
            "Nothing is sent to Roblox; the log shows exactly what would have been "
            "clicked and pressed. Use this to check a strategy before trusting it."
        )
        form.addRow(self.dry_run)

        self.require_foreground = QCheckBox("Only send input while Roblox is focused")
        self.require_foreground.setChecked(self.settings.require_foreground)
        self.require_foreground.setToolTip(
            "Strongly recommended. With this off, the macro will keep clicking into "
            "whatever window you switch to."
        )
        form.addRow(self.require_foreground)

        self.use_scancodes = QCheckBox("Send hardware scan codes")
        self.use_scancodes.setChecked(self.settings.use_scancodes)
        self.use_scancodes.setToolTip(
            "More games read scan codes than virtual key codes. Turn this off only "
            "if key presses are being ignored."
        )
        form.addRow(self.use_scancodes)

        self.log_to_file = QCheckBox("Write a log file next to the app")
        self.log_to_file.setChecked(self.settings.log_to_file)
        form.addRow(self.log_to_file)

        warning = QLabel(
            "Automating Roblox is against its terms of service. Accounts have been "
            "banned for it. Use this on an account you are willing to lose."
        )
        warning.setObjectName("banner")
        warning.setWordWrap(True)
        form.addRow(warning)
        return page

    # -- validation and commit -------------------------------------------------
    def _validate_hotkeys(self) -> None:
        problems: list[str] = []
        seen: dict[tuple[int, int], str] = {}
        for name, edit in self.hotkey_edits.items():
            try:
                spec = parse_hotkey(edit.text())
            except KeyParseError as exc:
                problems.append(f"{name}: {exc}")
                continue
            key = (spec.modifiers, spec.vk)
            if key in seen:
                problems.append(
                    f"{name} and {seen[key]} are both {spec.describe()} -- only one will work"
                )
            else:
                seen[key] = name
        if problems:
            self.hotkey_status.setText("\n".join(problems))
            self.hotkey_status.setStyleSheet(f"color: {ERROR};")
        else:
            self.hotkey_status.setText("All hotkeys look valid.")
            self.hotkey_status.setStyleSheet(f"color: {OK};")
        self.buttons.button(QDialogButtonBox.StandardButton.Save).setEnabled(not problems)

    def accept(self) -> None:
        settings = self.settings
        settings.hotkeys.record = self.hotkey_edits["record"].text().strip()
        settings.hotkeys.play = self.hotkey_edits["play"].text().strip()
        settings.hotkeys.stop = self.hotkey_edits["stop"].text().strip()
        settings.keybinds.slots = [edit.text().strip() for edit in self.slot_edits]
        for name, spin in self.timing_spins.items():
            setattr(settings.timing, name, spin.value())
        settings.timing = settings.timing.clamped()
        settings.loop.enabled = self.loop_enabled.isChecked()
        settings.loop.runs_target = self.loop_runs.value()
        settings.loop.use_play_again = self.loop_play_again.isChecked()
        settings.loop.requeue_timeout_ms = self.loop_timeout.value() * 1000
        settings.loop.match_end_timeout_ms = self.match_timeout.value() * 60_000
        settings.loop.between_match_delay_ms = self.loop_delay.value()
        settings.dry_run = self.dry_run.isChecked()
        settings.require_foreground = self.require_foreground.isChecked()
        settings.use_scancodes = self.use_scancodes.isChecked()
        settings.log_to_file = self.log_to_file.isChecked()
        super().accept()


class AboutDialog(QDialog):
    """What the app does and, more importantly, what it does not do."""

    def __init__(
        self,
        version: str,
        dpi_mode: str,
        config_path: str,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self.setWindowTitle("About TDS Macro")
        self.setModal(True)
        body = QLabel(
            f"<h3>TDS Macro {version}</h3>"
            "<p>Records and replays Tower Defense Simulator strategies using "
            "<b>external input only</b>: mouse and keyboard are simulated through "
            "the Windows <code>SendInput</code> API and the game is read from "
            "screenshots.</p>"
            "<p>It does <b>not</b> inject code into Roblox, attach a debugger, or "
            "read the game's memory. It needs no administrator rights, installs "
            "nothing, and writes only to its own folder.</p>"
            f"<p><b>DPI mode:</b> {dpi_mode}<br>"
            f"<b>Settings file:</b> {config_path}</p>"
            "<p style='color:#f0b429'><b>Automating Roblox breaks its terms of "
            "service and accounts have been banned for it. Use an account you are "
            "willing to lose.</b></p>"
        )
        body.setWordWrap(True)
        body.setTextFormat(Qt.TextFormat.RichText)
        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Close)
        buttons.rejected.connect(self.reject)
        buttons.accepted.connect(self.accept)
        layout = QVBoxLayout(self)
        layout.addWidget(body)
        layout.addWidget(buttons)
        self.resize(500, 340)
