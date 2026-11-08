"""Picking points and regions on a screenshot of the game.

Design choice worth stating: these work on a *frozen capture* of the Roblox
client area shown inside a normal window, not on a transparent overlay laid
over the live game.  Two reasons:

* A click-through overlay risks the click reaching Roblox and actually placing
  a tower while the user is only trying to mark a spot.
* Positioning a window exactly over the client area means converting physical
  pixels to Qt's logical coordinates, which is precisely the DPI-dependent
  arithmetic this app exists to avoid getting wrong.

The cost is that the user marks points on a still frame rather than live.  In
exchange they get a magnifier, a coordinate readout and the ability to see all
existing placement points at once.
"""

from __future__ import annotations

import numpy as np
from PySide6.QtCore import QEvent, QPoint, QRect, QSize, Qt, Signal
from PySide6.QtGui import (
    QColor,
    QCursor,
    QFont,
    QImage,
    QMouseEvent,
    QPainter,
    QPaintEvent,
    QPen,
    QPixmap,
)
from PySide6.QtWidgets import (
    QCheckBox,
    QDialog,
    QDialogButtonBox,
    QHBoxLayout,
    QLabel,
    QVBoxLayout,
    QWidget,
)

from ..input.coords import NormPoint
from ..vision.capture import NormBox
from .theme import ACCENT, ERROR, OK, WARN

MAGNIFIER = 112
ZOOM = 5


def numpy_to_qimage(frame: np.ndarray) -> QImage:
    """Convert an ``mss`` BGRA capture to a QImage.

    ``copy()`` is essential: QImage would otherwise reference the numpy buffer,
    which is freed as soon as the capture goes out of scope -- producing a
    window full of garbage or a hard crash.
    """
    array = np.ascontiguousarray(frame)
    height, width = array.shape[:2]
    if array.ndim == 2:
        return QImage(
            array.data, width, height, width, QImage.Format.Format_Grayscale8
        ).copy()
    channels = array.shape[2]
    if channels == 3:
        return QImage(
            array.data, width, height, width * 3, QImage.Format.Format_BGR888
        ).copy()
    return QImage(
        array.data, width, height, width * 4, QImage.Format.Format_ARGB32
    ).copy()


class PickerCanvas(QWidget):
    """Shows a screenshot scaled to fit, and reports picks in normalized coords."""

    picked = Signal(object)  # NormPoint for point mode, NormBox for region mode
    hovered = Signal(object)  # NormPoint or None

    def __init__(
        self,
        image: QImage,
        mode: str = "point",
        markers: list[tuple[str, NormPoint]] | None = None,
        boxes: list[tuple[str, NormBox]] | None = None,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self.image = image
        self.mode = mode
        self.markers = markers or []
        self.boxes = boxes or []
        self.show_magnifier = True
        self.selection: NormBox | None = None
        self.point: NormPoint | None = None
        self._drag_start: NormPoint | None = None
        self._cursor: QPoint | None = None
        self.setMouseTracking(True)
        self.setCursor(QCursor(Qt.CursorShape.CrossCursor))
        self.setMinimumSize(360, 240)

    # -- geometry -------------------------------------------------------------
    def sizeHint(self) -> QSize:
        return QSize(min(1280, self.image.width()), min(720, self.image.height()))

    def _target_rect(self) -> QRect:
        """Where the image is drawn, letter-boxed inside the widget."""
        if self.image.isNull():
            return QRect(0, 0, self.width(), self.height())
        scale = min(
            self.width() / self.image.width(), self.height() / self.image.height()
        )
        width = max(1, int(self.image.width() * scale))
        height = max(1, int(self.image.height() * scale))
        return QRect((self.width() - width) // 2, (self.height() - height) // 2, width, height)

    def _to_norm(self, pos: QPoint) -> NormPoint | None:
        rect = self._target_rect()
        if not rect.contains(pos) or rect.width() == 0 or rect.height() == 0:
            return None
        return NormPoint(
            (pos.x() - rect.left()) / rect.width(), (pos.y() - rect.top()) / rect.height()
        )

    def _from_norm(self, point: NormPoint) -> QPoint:
        rect = self._target_rect()
        return QPoint(
            rect.left() + int(point.x * rect.width()), rect.top() + int(point.y * rect.height())
        )

    # -- interaction ----------------------------------------------------------
    def mouseMoveEvent(self, event: QMouseEvent) -> None:
        self._cursor = event.position().toPoint()
        norm = self._to_norm(self._cursor)
        self.hovered.emit(norm)
        if self.mode == "region" and self._drag_start is not None and norm is not None:
            self.selection = NormBox.from_corners(self._drag_start, norm)
        self.update()

    def mousePressEvent(self, event: QMouseEvent) -> None:
        if event.button() != Qt.MouseButton.LeftButton:
            return
        norm = self._to_norm(event.position().toPoint())
        if norm is None:
            return
        if self.mode == "region":
            self._drag_start = norm
            self.selection = NormBox(norm.x, norm.y, 0.0, 0.0)
        else:
            self.point = norm
            self.picked.emit(norm)
        self.update()

    def mouseReleaseEvent(self, event: QMouseEvent) -> None:
        if self.mode != "region" or self._drag_start is None:
            return
        norm = self._to_norm(event.position().toPoint()) or self._drag_start
        box = NormBox.from_corners(self._drag_start, norm)
        self._drag_start = None
        if box.width <= 0.001 or box.height <= 0.001:
            # A click rather than a drag: keep whatever was there instead of
            # replacing a good region with a zero-size one.
            self.selection = None
        else:
            self.selection = box
            self.picked.emit(box)
        self.update()

    def leaveEvent(self, event: QEvent) -> None:
        del event
        self._cursor = None
        self.hovered.emit(None)
        self.update()

    # -- painting -------------------------------------------------------------
    def paintEvent(self, event: QPaintEvent) -> None:
        del event
        painter = QPainter(self)
        painter.fillRect(self.rect(), QColor("#0b0d10"))
        rect = self._target_rect()
        if self.image.isNull():
            painter.setPen(QPen(QColor(WARN)))
            painter.drawText(
                self.rect(), Qt.AlignmentFlag.AlignCenter, "No screenshot available"
            )
            return
        painter.drawPixmap(rect, QPixmap.fromImage(self.image))

        self._draw_boxes(painter)
        self._draw_markers(painter)
        if self.selection is not None:
            self._draw_selection(painter)
        if self.mode == "point" and self.point is not None:
            self._draw_crosshair(painter, self._from_norm(self.point), QColor(OK))
        if self.show_magnifier and self._cursor is not None and rect.contains(self._cursor):
            self._draw_magnifier(painter, rect)

    def _draw_markers(self, painter: QPainter) -> None:
        font = QFont()
        font.setPointSize(9)
        font.setBold(True)
        painter.setFont(font)
        for label, point in self.markers:
            centre = self._from_norm(point)
            painter.setPen(QPen(QColor(ACCENT), 2))
            painter.setBrush(QColor(30, 40, 55, 200))
            painter.drawEllipse(centre, 9, 9)
            painter.setPen(QPen(QColor("#ffffff")))
            painter.drawText(
                QRect(centre.x() - 20, centre.y() - 7, 40, 14),
                Qt.AlignmentFlag.AlignCenter,
                label,
            )

    def _draw_boxes(self, painter: QPainter) -> None:
        for label, box in self.boxes:
            top_left = self._from_norm(NormPoint(box.x, box.y))
            bottom_right = self._from_norm(NormPoint(box.x + box.width, box.y + box.height))
            region = QRect(top_left, bottom_right)
            painter.setPen(QPen(QColor(WARN), 2, Qt.PenStyle.DashLine))
            painter.setBrush(Qt.BrushStyle.NoBrush)
            painter.drawRect(region)
            painter.setPen(QPen(QColor(WARN)))
            painter.drawText(region.adjusted(2, -16, 0, 0), Qt.AlignmentFlag.AlignLeft, label)

    def _draw_selection(self, painter: QPainter) -> None:
        assert self.selection is not None
        box = self.selection
        top_left = self._from_norm(NormPoint(box.x, box.y))
        bottom_right = self._from_norm(NormPoint(box.x + box.width, box.y + box.height))
        region = QRect(top_left, bottom_right)
        painter.setPen(QPen(QColor(OK), 2))
        painter.setBrush(QColor(61, 214, 140, 40))
        painter.drawRect(region)

    def _draw_crosshair(self, painter: QPainter, centre: QPoint, colour: QColor) -> None:
        rect = self._target_rect()
        painter.setPen(QPen(colour, 1, Qt.PenStyle.DashLine))
        painter.drawLine(rect.left(), centre.y(), rect.right(), centre.y())
        painter.drawLine(centre.x(), rect.top(), centre.x(), rect.bottom())
        painter.setPen(QPen(colour, 2))
        painter.setBrush(Qt.BrushStyle.NoBrush)
        painter.drawEllipse(centre, 7, 7)

    def _draw_magnifier(self, painter: QPainter, target: QRect) -> None:
        """Zoomed inset, so a point can be placed precisely on a scaled image."""
        assert self._cursor is not None
        norm = self._to_norm(self._cursor)
        if norm is None:
            return
        source_x = int(norm.x * self.image.width())
        source_y = int(norm.y * self.image.height())
        span = max(4, MAGNIFIER // (2 * ZOOM))
        source = QRect(source_x - span, source_y - span, span * 2, span * 2)

        # Keep the inset on screen and away from the cursor.
        left = self._cursor.x() + 18
        top = self._cursor.y() + 18
        if left + MAGNIFIER > target.right():
            left = self._cursor.x() - MAGNIFIER - 18
        if top + MAGNIFIER > target.bottom():
            top = self._cursor.y() - MAGNIFIER - 18
        inset = QRect(left, top, MAGNIFIER, MAGNIFIER)

        painter.fillRect(inset, QColor("#05070a"))
        painter.drawImage(inset, self.image, source)
        painter.setPen(QPen(QColor(ACCENT), 1))
        painter.setBrush(Qt.BrushStyle.NoBrush)
        painter.drawRect(inset)
        painter.setPen(QPen(QColor(ERROR), 1))
        painter.drawLine(
            inset.center().x(), inset.top() + 4, inset.center().x(), inset.bottom() - 4
        )
        painter.drawLine(
            inset.left() + 4, inset.center().y(), inset.right() - 4, inset.center().y()
        )


class PickerDialog(QDialog):
    """Modal wrapper around :class:`PickerCanvas`."""

    def __init__(
        self,
        image: QImage,
        title: str,
        instruction: str,
        mode: str = "point",
        markers: list[tuple[str, NormPoint]] | None = None,
        boxes: list[tuple[str, NormBox]] | None = None,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self.setWindowTitle(title)
        self.setModal(True)
        self.result_point: NormPoint | None = None
        self.result_box: NormBox | None = None

        self.canvas = PickerCanvas(image, mode=mode, markers=markers, boxes=boxes)
        self.canvas.picked.connect(self._on_picked)
        self.canvas.hovered.connect(self._on_hovered)

        self.readout = QLabel("--")
        self.readout.setObjectName("hud")
        self.readout.setToolTip(
            "Position as a fraction of the Roblox window, so it replays correctly "
            "at any resolution."
        )
        hint = QLabel(instruction)
        hint.setObjectName("hint")
        hint.setWordWrap(True)

        magnify = QCheckBox("Magnifier")
        magnify.setChecked(True)
        magnify.setToolTip("Zoomed inset around the cursor, for precise placement.")
        magnify.toggled.connect(self._set_magnifier)

        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel
        )
        self.ok_button = buttons.button(QDialogButtonBox.StandardButton.Ok)
        self.ok_button.setEnabled(mode == "view")
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        if mode == "view":
            buttons.button(QDialogButtonBox.StandardButton.Cancel).hide()

        top = QHBoxLayout()
        top.addWidget(hint, 1)
        top.addWidget(magnify)
        top.addWidget(self.readout)

        layout = QVBoxLayout(self)
        layout.addLayout(top)
        layout.addWidget(self.canvas, 1)
        layout.addWidget(buttons)
        self.resize(1040, 720)

    def _set_magnifier(self, on: bool) -> None:
        self.canvas.show_magnifier = on
        self.canvas.update()

    def _on_picked(self, value: object) -> None:
        if isinstance(value, NormPoint):
            self.result_point = value
            self.readout.setText(f"x {value.x:.4f}   y {value.y:.4f}")
        elif isinstance(value, NormBox):
            self.result_box = value
            self.readout.setText(
                f"{value.width * 100:.1f}% x {value.height * 100:.1f}% "
                f"at {value.x:.3f}, {value.y:.3f}"
            )
        self.ok_button.setEnabled(True)

    def _on_hovered(self, value: object) -> None:
        if isinstance(value, NormPoint) and self.result_point is None and self.result_box is None:
            self.readout.setText(f"x {value.x:.4f}   y {value.y:.4f}")
