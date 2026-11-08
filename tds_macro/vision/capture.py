"""Screen capture of the Roblox client area.

Uses ``mss``, which reads the desktop through the normal GDI/DXGI path -- the
same thing a screenshot tool does.  Nothing is read from the Roblox process.

Capture is restricted to the client rectangle, so the cost scales with the game
window rather than the whole desktop, and every crop can be expressed in
normalized coordinates that survive a resolution change.
"""

from __future__ import annotations

import contextlib
import logging
from dataclasses import dataclass
from typing import Any, Protocol

import numpy as np

from ..input.coords import NormPoint, Rect

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class NormBox:
    """A rectangle in normalized client coordinates.

    Stored this way so a HUD calibration done in a 1280x720 window still points
    at the wave counter after the user goes fullscreen at 2560x1440.
    """

    x: float
    y: float
    width: float
    height: float

    def is_valid(self) -> bool:
        return self.width > 0 and self.height > 0

    def to_pixels(self, client: Rect) -> Rect:
        left = client.left + int(self.x * client.width)
        top = client.top + int(self.y * client.height)
        right = left + max(1, int(self.width * client.width))
        bottom = top + max(1, int(self.height * client.height))
        return Rect(
            max(client.left, left),
            max(client.top, top),
            min(client.right, right),
            min(client.bottom, bottom),
        )

    def contains(self, point: NormPoint) -> bool:
        return (
            self.x <= point.x <= self.x + self.width
            and self.y <= point.y <= self.y + self.height
        )

    def to_dict(self) -> dict[str, float]:
        return {
            "x": round(self.x, 6),
            "y": round(self.y, 6),
            "width": round(self.width, 6),
            "height": round(self.height, 6),
        }

    @classmethod
    def from_dict(cls, data: dict[str, float]) -> NormBox:
        return cls(
            float(data["x"]), float(data["y"]), float(data["width"]), float(data["height"])
        )

    @classmethod
    def from_corners(cls, a: NormPoint, b: NormPoint) -> NormBox:
        """Build from two dragged corners, in any order."""
        left, right = sorted((a.x, b.x))
        top, bottom = sorted((a.y, b.y))
        return cls(left, top, right - left, bottom - top)


class FrameSource(Protocol):
    """Anything that can hand back pixels for a screen rectangle."""

    def grab(self, rect: Rect) -> np.ndarray | None: ...

    def close(self) -> None: ...


class ScreenCapture:
    """``mss``-backed capture.

    One ``mss`` instance is kept per thread: the library's handles are not
    thread-safe, and sharing one across the UI thread and the playback thread
    produces intermittent blank frames.
    """

    def __init__(self) -> None:
        self._mss: Any = None
        self._error = ""

    @property
    def error(self) -> str:
        return self._error

    def _ensure(self) -> Any:
        if self._mss is None:
            import mss  # imported lazily so the module loads without a display

            self._mss = mss.mss()
        return self._mss

    def grab(self, rect: Rect) -> np.ndarray | None:
        """Capture ``rect`` as a BGRA array, or ``None`` on failure."""
        if not rect.is_valid():
            self._error = "capture region has no area"
            return None
        try:
            shot = self._ensure().grab(
                {
                    "left": rect.left,
                    "top": rect.top,
                    "width": rect.width,
                    "height": rect.height,
                }
            )
        except Exception as exc:  # pragma: no cover - needs a real display
            self._error = f"screen capture failed: {exc}"
            log.warning("%s", self._error)
            # Drop the handle so the next attempt reconnects; this recovers
            # from the display changing under us (resolution switch, RDP).
            self._mss = None
            return None
        self._error = ""
        return np.asarray(shot)

    def close(self) -> None:
        if self._mss is not None:
            # Closing a handle whose display already went away raises; there is
            # nothing useful to do about it at shutdown.
            with contextlib.suppress(Exception):
                self._mss.close()
            self._mss = None


class StaticFrameSource:
    """Serves one fixed image in place of live capture.

    Used by the tests to drive the HUD reader deterministically, and available
    for replaying a saved screenshot through the vision layer when diagnosing a
    calibration problem.
    """

    def __init__(self, image: np.ndarray, origin: Rect | None = None) -> None:
        self.image = image
        self.origin = origin or Rect(0, 0, image.shape[1], image.shape[0])

    def grab(self, rect: Rect) -> np.ndarray | None:
        x0 = rect.left - self.origin.left
        y0 = rect.top - self.origin.top
        crop = self.image[y0 : y0 + rect.height, x0 : x0 + rect.width]
        return crop if crop.size else None

    def close(self) -> None:
        return None


def crop_norm(image: np.ndarray, box: NormBox) -> np.ndarray:
    """Crop a normalized box out of a full client-area image."""
    height, width = image.shape[:2]
    left = max(0, min(width - 1, int(box.x * width)))
    top = max(0, min(height - 1, int(box.y * height)))
    right = max(left + 1, min(width, left + int(box.width * width)))
    bottom = max(top + 1, min(height, top + int(box.height * height)))
    return image[top:bottom, left:right]
