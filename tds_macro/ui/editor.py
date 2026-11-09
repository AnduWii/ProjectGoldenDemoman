"""Inline editor for the selected step.

Rebuilds its form for whatever step type is selected.  Tower references are
offered as a dropdown of the ids actually placed earlier in the strategy, so a
dangling reference is hard to create by accident rather than only caught by
validation afterwards.
"""

from __future__ import annotations

from collections.abc import Callable

from PySide6.QtCore import Signal
from PySide6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QDoubleSpinBox,
    QFormLayout,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QPushButton,
    QSpinBox,
    QVBoxLayout,
    QWidget,
)

from ..input.coords import NormPoint
from ..strategy.steps import (
    Ability,
    Place,
    Sell,
    SkipWave,
    Step,
    Strategy,
    TowerRefStep,
    Upgrade,
    Wait,
    WaitForCash,
    WaitForWave,
)
from .theme import TEXT_DIM

# 0 on a step defers to Settings > Timing > Default wait timeout, which is
# itself "wait forever" (0) unless the user changes it.
USE_DEFAULT_TEXT = "use default"


class StepEditor(QWidget):
    """Edits one step in place."""

    changed = Signal()
    pickRequested = Signal()

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.step: Step | None = None
        self.strategy: Strategy | None = None
        self.row = -1
        self._loading = False

        self.title = QLabel("Nothing selected")
        self.title.setStyleSheet("font-weight: 700; font-size: 14px;")
        self.hint = QLabel("Select a step on the left, or add one below.")
        self.hint.setObjectName("hint")
        self.hint.setWordWrap(True)

        self.form = QFormLayout()
        self.form.setLabelAlignment(self.form.labelAlignment())
        self.form.setContentsMargins(0, 8, 0, 0)

        layout = QVBoxLayout(self)
        layout.addWidget(self.title)
        layout.addWidget(self.hint)
        layout.addLayout(self.form)
        layout.addStretch(1)

    # -- population -----------------------------------------------------------
    def clear(self) -> None:
        while self.form.rowCount():
            self.form.removeRow(0)
        self.step = None
        self.row = -1
        self.title.setText("Nothing selected")
        self.hint.setText("Select a step on the left, or add one below.")

    def set_step(self, strategy: Strategy, row: int, problems: list[str]) -> None:
        self._loading = True
        try:
            while self.form.rowCount():
                self.form.removeRow(0)
            self.strategy = strategy
            self.step = strategy.steps[row] if 0 <= row < len(strategy.steps) else None
            self.row = row
            if self.step is None:
                self.clear()
                return

            self.title.setText(f"Step {row + 1}: {type(self.step).__name__}")
            self.hint.setText(
                "  |  ".join(f"Problem: {p}" for p in problems)
                if problems
                else self.step.describe()
            )
            self.hint.setStyleSheet(
                "color: #ff6b6b;" if problems else f"color: {TEXT_DIM};"
            )
            self._build_common()
            self._build_specific()
        finally:
            self._loading = False

    def _emit(self) -> None:
        if not self._loading:
            self.changed.emit()

    def _spin(
        self,
        value: int,
        setter: Callable[[int], None],
        low: int = 0,
        high: int = 1_000_000,
        suffix: str = "",
        tooltip: str = "",
    ) -> QSpinBox:
        box = QSpinBox()
        box.setRange(low, high)
        box.setValue(value)
        box.setSuffix(suffix)
        if tooltip:
            box.setToolTip(tooltip)
        def on_change(value: int) -> None:
            setter(int(value))
            self._emit()

        box.valueChanged.connect(on_change)
        return box

    def _build_common(self) -> None:
        assert self.step is not None
        step = self.step

        enabled = QCheckBox("Run this step")
        enabled.setChecked(step.enabled)
        enabled.setToolTip(
            "Unticked steps are skipped during playback. Handy for bisecting a "
            "strategy that misbehaves, without deleting anything."
        )

        def set_enabled(state: bool) -> None:
            step.enabled = state
            self._emit()

        enabled.toggled.connect(set_enabled)
        self.form.addRow(enabled)

        note = QLineEdit(step.note)
        note.setPlaceholderText("optional note to yourself")
        note.setToolTip("Shown in the step list. Saved with the strategy.")

        def set_note(text: str) -> None:
            step.note = text
            self._emit()

        note.textEdited.connect(set_note)
        self.form.addRow("Note", note)

    def _tower_combo(self, step: TowerRefStep) -> QComboBox:
        """Dropdown of tower ids placed before this step."""
        combo = QComboBox()
        combo.setEditable(True)
        combo.setToolTip(
            "Which placed tower this step acts on. Only towers placed earlier in "
            "the strategy are valid."
        )
        available: list[str] = []
        if self.strategy is not None:
            for index, candidate in enumerate(self.strategy.steps):
                if index >= self.row:
                    break
                if isinstance(candidate, Place):
                    label = (
                        f"{candidate.tower_id} ({candidate.tower_name})"
                        if candidate.tower_name
                        else candidate.tower_id
                    )
                    available.append(candidate.tower_id)
                    combo.addItem(label, candidate.tower_id)
        if step.tower_id not in available:
            combo.addItem(f"{step.tower_id} (not placed yet)", step.tower_id)
        index = combo.findData(step.tower_id)
        combo.setCurrentIndex(index if index >= 0 else combo.count() - 1)

        def on_change(position: int) -> None:
            data = combo.itemData(position)
            step.tower_id = str(data if data is not None else combo.currentText()).strip()
            self._emit()

        combo.currentIndexChanged.connect(on_change)
        def on_typed(text: str) -> None:
            # The combo shows "T1 (Farm)" but the id is just the first token.
            step.tower_id = text.strip().split(" ")[0]
            self._emit()

        combo.editTextChanged.connect(on_typed)
        return combo

    def _timeout_row(self, step: WaitForWave | WaitForCash) -> QSpinBox:
        box = self._spin(
            step.timeout_ms // 1000,
            lambda v: setattr(step, "timeout_ms", v * 1000),
            0,
            24 * 3_600,
            " s",
            "Give up after this long and stop the run. 'use default' follows "
            "Settings > Timing > Default wait timeout, which waits forever unless "
            "you change it. The status line shows what the HUD is reading while "
            "it waits, so a stalled read is visible.",
        )
        box.setSpecialValueText(USE_DEFAULT_TEXT)
        return box

    def _build_specific(self) -> None:
        step = self.step
        if isinstance(step, Place):
            self.form.addRow(
                "Hotbar slot",
                self._spin(
                    step.slot,
                    lambda v: setattr(step, "slot", v),
                    1,
                    5,
                    "",
                    "Which hotbar slot to select before clicking. The key for each "
                    "slot is set in Settings > Keybinds.",
                ),
            )

            tower_id = QLineEdit(step.tower_id)
            tower_id.setToolTip(
                "Name used by later Upgrade/Sell/Ability steps to refer to this tower."
            )
            def set_tower_id(text: str, target: Place = step) -> None:
                target.tower_id = text.strip()
                self._emit()

            tower_id.textEdited.connect(set_tower_id)
            self.form.addRow("Tower id", tower_id)

            name = QLineEdit(step.tower_name)
            name.setPlaceholderText("e.g. Farm, Scout -- for your own readability")
            def set_tower_name(text: str, target: Place = step) -> None:
                target.tower_name = text
                self._emit()

            name.textEdited.connect(set_tower_name)
            self.form.addRow("Tower name", name)

            self.position_label = QLabel(
                f"x {step.position.x:.4f}   y {step.position.y:.4f}"
            )
            self.position_label.setObjectName("hud")
            pick = QPushButton("Pick on screenshot...")
            pick.setToolTip(
                "Takes a fresh capture of the Roblox window and lets you click the "
                "exact spot. Stored as a fraction of the window, so it survives a "
                "resolution change."
            )
            pick.clicked.connect(self.pickRequested.emit)
            row = QHBoxLayout()
            row.addWidget(self.position_label, 1)
            row.addWidget(pick)
            holder = QWidget()
            holder.setLayout(row)
            self.form.addRow("Position", holder)

        elif isinstance(step, Upgrade):
            self.form.addRow("Tower", self._tower_combo(step))
            self.form.addRow(
                "Times",
                self._spin(
                    step.times,
                    lambda v: setattr(step, "times", v),
                    1,
                    20,
                    "",
                    "How many upgrade presses to send. Each one costs cash in game; "
                    "the macro does not check you can afford them all.",
                ),
            )
        elif isinstance(step, Sell | Ability):
            self.form.addRow("Tower", self._tower_combo(step))
        elif isinstance(step, SkipWave):
            self.form.addRow(
                "Times",
                self._spin(
                    step.times, lambda v: setattr(step, "times", v), 1, 20, "", "Skip presses."
                ),
            )
        elif isinstance(step, Wait):
            seconds = QDoubleSpinBox()
            seconds.setRange(0.0, 3_600.0)
            seconds.setDecimals(2)
            seconds.setSingleStep(0.25)
            seconds.setSuffix(" s")
            seconds.setValue(step.ms / 1000)
            seconds.setToolTip(
                "A fixed pause. Prefer 'Wait for wave' or 'Wait for cash' where you "
                "can -- those adapt when the game runs faster or slower."
            )
            def set_duration(value: float, target: Wait = step) -> None:
                target.ms = int(value * 1000)
                self._emit()

            seconds.valueChanged.connect(set_duration)
            self.form.addRow("Duration", seconds)
        elif isinstance(step, WaitForWave):
            self.form.addRow(
                "Wave",
                self._spin(
                    step.wave,
                    lambda v: setattr(step, "wave", v),
                    1,
                    999,
                    "",
                    "Blocks until the HUD reports this wave or higher. Needs HUD "
                    "calibration.",
                ),
            )
            self.form.addRow("Timeout", self._timeout_row(step))
        elif isinstance(step, WaitForCash):
            self.form.addRow(
                "Cash",
                self._spin(
                    step.amount,
                    lambda v: setattr(step, "amount", v),
                    0,
                    10_000_000,
                    "",
                    "Blocks until the HUD reports at least this much cash. Needs HUD "
                    "calibration.",
                ),
            )
            self.form.addRow("Timeout", self._timeout_row(step))

    def update_position(self, point: NormPoint) -> None:
        """Called after the user picks a new placement point."""
        if isinstance(self.step, Place):
            self.step.position = point
            if hasattr(self, "position_label"):
                self.position_label.setText(f"x {point.x:.4f}   y {point.y:.4f}")
            self._emit()
