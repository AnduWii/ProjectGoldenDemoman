"""Qt model for the step list.

Drag-to-reorder is implemented through explicit ``mimeData``/``dropMimeData``
plus :meth:`moveRows` rather than relying on Qt's default InternalMove, which
for a list model works by removing and reinserting rows -- that path silently
drops the step objects and rebuilds them from display strings.
"""

from __future__ import annotations

from dataclasses import replace as dataclass_replace
from typing import Any

from PySide6.QtCore import QAbstractListModel, QMimeData, QModelIndex, Qt
from PySide6.QtGui import QColor, QFont

from ..strategy.steps import Place, Step, Strategy
from .theme import ERROR, TEXT, TEXT_DIM, WARN

MIME_TYPE = "application/x-tdsmacro-steps"


class StepListModel(QAbstractListModel):
    """Presents a :class:`Strategy`'s steps, editable and reorderable."""

    def __init__(self, strategy: Strategy, parent: Any = None) -> None:
        super().__init__(parent)
        self._strategy = strategy
        self._problems: dict[int, list[str]] = {}
        self.refresh_problems()

    # -- strategy binding -----------------------------------------------------
    @property
    def strategy(self) -> Strategy:
        return self._strategy

    def set_strategy(self, strategy: Strategy) -> None:
        self.beginResetModel()
        self._strategy = strategy
        self.refresh_problems()
        self.endResetModel()

    def refresh_problems(self) -> None:
        self._problems = {}
        for index, message in self._strategy.problems_by_step():
            self._problems.setdefault(index, []).append(message)

    def problems_for(self, row: int) -> list[str]:
        return self._problems.get(row, [])

    def step_at(self, row: int) -> Step | None:
        if 0 <= row < len(self._strategy.steps):
            return self._strategy.steps[row]
        return None

    # -- QAbstractListModel ---------------------------------------------------
    def rowCount(self, parent: QModelIndex | None = None) -> int:
        if parent is not None and parent.isValid():
            return 0
        return len(self._strategy.steps)

    def data(self, index: QModelIndex, role: int = Qt.ItemDataRole.DisplayRole) -> Any:
        if not index.isValid():
            return None
        row = index.row()
        step = self.step_at(row)
        if step is None:
            return None
        problems = self.problems_for(row)

        if role == Qt.ItemDataRole.DisplayRole:
            marker = "  !" if problems else ""
            muted = "  (disabled)" if not step.enabled else ""
            note = f"   -- {step.note}" if step.note else ""
            return f"{row + 1:>3}.  {step.describe()}{note}{muted}{marker}"
        if role == Qt.ItemDataRole.ToolTipRole:
            lines = [step.describe()]
            if step.note:
                lines.append(f"Note: {step.note}")
            if not step.enabled:
                lines.append("Disabled -- skipped during playback.")
            lines.extend(f"Problem: {p}" for p in problems)
            lines.append("Drag to reorder. Double-click to edit.")
            return "\n".join(lines)
        if role == Qt.ItemDataRole.ForegroundRole:
            if problems:
                return QColor(ERROR)
            if not step.enabled:
                return QColor(TEXT_DIM)
            if isinstance(step, Place):
                return QColor(TEXT)
            return QColor(WARN if step.describe().startswith("Wait") else TEXT)
        if role == Qt.ItemDataRole.FontRole and not step.enabled:
            font = QFont()
            font.setStrikeOut(True)
            return font
        return None

    def flags(self, index: QModelIndex) -> Qt.ItemFlag:
        base = (
            Qt.ItemFlag.ItemIsEnabled
            | Qt.ItemFlag.ItemIsSelectable
            | Qt.ItemFlag.ItemIsDragEnabled
            | Qt.ItemFlag.ItemIsUserCheckable
        )
        if not index.isValid():
            return base | Qt.ItemFlag.ItemIsDropEnabled
        return base | Qt.ItemFlag.ItemIsDropEnabled

    # -- drag and drop --------------------------------------------------------
    def supportedDropActions(self) -> Qt.DropAction:
        return Qt.DropAction.MoveAction

    def mimeTypes(self) -> list[str]:
        return [MIME_TYPE]

    def mimeData(self, indexes: list[QModelIndex]) -> QMimeData:
        rows = sorted({i.row() for i in indexes if i.isValid()})
        payload = QMimeData()
        payload.setData(MIME_TYPE, ",".join(str(r) for r in rows).encode("ascii"))
        return payload

    def canDropMimeData(
        self,
        data: QMimeData,
        action: Qt.DropAction,
        row: int,
        column: int,
        parent: QModelIndex,
    ) -> bool:
        del action, row, column, parent
        return data.hasFormat(MIME_TYPE)

    def dropMimeData(
        self,
        data: QMimeData,
        action: Qt.DropAction,
        row: int,
        column: int,
        parent: QModelIndex,
    ) -> bool:
        del column
        if action == Qt.DropAction.IgnoreAction:
            return True
        if not data.hasFormat(MIME_TYPE):
            return False
        try:
            rows = [int(part) for part in bytes(data.data(MIME_TYPE)).decode("ascii").split(",")]
        except ValueError:
            return False
        target = row if row >= 0 else (parent.row() if parent.isValid() else self.rowCount())
        return self.move_rows(rows, target)

    def move_rows(self, rows: list[int], target: int) -> bool:
        """Move ``rows`` so they land before ``target``, preserving their order."""
        steps = self._strategy.steps
        rows = [r for r in sorted(set(rows)) if 0 <= r < len(steps)]
        if not rows:
            return False
        moving = [steps[r] for r in rows]
        # Count how many removed rows sit before the target so the insertion
        # point still refers to the same gap after removal.
        shift = sum(1 for r in rows if r < target)
        self.beginResetModel()
        for r in reversed(rows):
            steps.pop(r)
        insert_at = max(0, min(len(steps), target - shift))
        steps[insert_at:insert_at] = moving
        self.refresh_problems()
        self.endResetModel()
        return True

    # -- editing helpers -------------------------------------------------------
    def _reset(self) -> None:
        self.beginResetModel()
        self.refresh_problems()
        self.endResetModel()

    def append_step(self, step: Step) -> int:
        row = len(self._strategy.steps)
        self.beginInsertRows(QModelIndex(), row, row)
        self._strategy.steps.append(step)
        self.endInsertRows()
        self.refresh_problems()
        return row

    def insert_step(self, row: int, step: Step) -> int:
        row = max(0, min(row, len(self._strategy.steps)))
        self.beginInsertRows(QModelIndex(), row, row)
        self._strategy.steps.insert(row, step)
        self.endInsertRows()
        self.refresh_problems()
        return row

    def duplicate_step(self, row: int) -> int | None:
        step = self.step_at(row)
        if step is None:
            return None
        copy = dataclass_replace(step)
        if isinstance(copy, Place):
            # A duplicated placement must not reuse the tower id, or every later
            # Upgrade/Sell becomes ambiguous.
            copy.tower_id = self._strategy.next_tower_id()
        return self.insert_step(row + 1, copy)

    def remove_step(self, row: int) -> bool:
        if self.step_at(row) is None:
            return False
        self.beginRemoveRows(QModelIndex(), row, row)
        self._strategy.steps.pop(row)
        self.endRemoveRows()
        self.refresh_problems()
        return True

    def toggle_enabled(self, row: int) -> None:
        step = self.step_at(row)
        if step is None:
            return
        step.enabled = not step.enabled
        self._reset()

    def shift_step(self, row: int, delta: int) -> int | None:
        target = row + delta
        if self.step_at(row) is None or not 0 <= target < len(self._strategy.steps):
            return None
        steps = self._strategy.steps
        self.beginResetModel()
        steps[row], steps[target] = steps[target], steps[row]
        self.refresh_problems()
        self.endResetModel()
        return target

    def step_changed(self, row: int) -> None:
        """Call after editing a step in place."""
        self.refresh_problems()
        index = self.index(row, 0)
        self.dataChanged.emit(index, index)
        # A changed tower id can invalidate *other* rows, so repaint them all.
        if self.rowCount():
            self.dataChanged.emit(self.index(0, 0), self.index(self.rowCount() - 1, 0))
