"""Template matching for UI screens: end-of-match, Play Again, map select.

Used only by loop mode, which has to click through the lobby between runs.
Templates are captured during calibration and stored as normalized-coordinate
boxes plus the pixels they contained, so matching survives a resolution change:
the stored template is rescaled to the current client size before searching.

OpenCV does the correlation when available; a pure-numpy path keeps the module
usable (and testable) without it, at a cost that only matters for large search
areas.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

import numpy as np

from ..input.coords import NormPoint
from .capture import NormBox
from .digits import to_gray

log = logging.getLogger(__name__)

DEFAULT_THRESHOLD = 0.80


@dataclass(frozen=True)
class MatchResult:
    """Where a template was found, in normalized client coordinates."""

    found: bool
    score: float
    centre: NormPoint | None = None

    def describe(self) -> str:
        if not self.found or self.centre is None:
            return f"not found (best {self.score:.2f})"
        return f"found at ({self.centre.x:.3f}, {self.centre.y:.3f}) score {self.score:.2f}"


@dataclass
class ScreenTemplate:
    """One captured piece of UI.

    ``search_box`` narrows where we look.  A full-screen search for a small
    button is both slow and prone to matching the wrong thing, so calibration
    records the region the button lives in.
    """

    name: str
    pixels: np.ndarray  # grayscale float32
    source_box: NormBox
    search_box: NormBox | None = None
    threshold: float = DEFAULT_THRESHOLD

    @property
    def click_point(self) -> NormPoint:
        """Centre of where the template was captured -- the fallback click target
        when matching is not used (fixed-layout menus)."""
        return NormPoint(
            self.source_box.x + self.source_box.width / 2,
            self.source_box.y + self.source_box.height / 2,
        )


def _resize_gray(image: np.ndarray, size: tuple[int, int]) -> np.ndarray:
    """Nearest-neighbour resize of a 2-D float image to ``(height, width)``."""
    height, width = size
    src_h, src_w = image.shape[:2]
    if src_h == 0 or src_w == 0 or height <= 0 or width <= 0:
        return np.zeros((max(height, 0), max(width, 0)), dtype=np.float32)
    rows = (np.arange(height) * src_h // height).clip(0, src_h - 1)
    cols = (np.arange(width) * src_w // width).clip(0, src_w - 1)
    return image[rows][:, cols].astype(np.float32)


def _match_opencv(haystack: np.ndarray, needle: np.ndarray) -> tuple[float, int, int] | None:
    try:
        import cv2
    except ImportError:  # pragma: no cover - cv2 is a declared dependency
        return None
    result = cv2.matchTemplate(haystack, needle, cv2.TM_CCOEFF_NORMED)
    _, max_val, _, max_loc = cv2.minMaxLoc(result)
    return float(max_val), int(max_loc[0]), int(max_loc[1])


def _match_numpy(haystack: np.ndarray, needle: np.ndarray) -> tuple[float, int, int]:
    """Normalized cross-correlation without OpenCV.

    Evaluated with an explicit sliding window; fine for the small search
    regions calibration produces, and it keeps this module importable (and the
    tests runnable) on a machine without OpenCV.
    """
    h_h, h_w = haystack.shape
    n_h, n_w = needle.shape
    if n_h > h_h or n_w > h_w:
        return (-1.0, 0, 0)
    needle_c = needle - needle.mean()
    needle_norm = float(np.linalg.norm(needle_c))
    if needle_norm < 1e-6:
        return (-1.0, 0, 0)

    best = (-1.0, 0, 0)
    for y in range(h_h - n_h + 1):
        strip = haystack[y : y + n_h]
        for x in range(h_w - n_w + 1):
            window = strip[:, x : x + n_w]
            centred = window - window.mean()
            denom = float(np.linalg.norm(centred)) * needle_norm
            if denom < 1e-6:
                continue
            score = float((centred * needle_c).sum() / denom)
            if score > best[0]:
                best = (score, x, y)
    return best


def match_template(
    frame: np.ndarray, template: ScreenTemplate, use_opencv: bool = True
) -> MatchResult:
    """Look for ``template`` in a full client-area ``frame``.

    The template is rescaled from the client size it was captured at to the
    current one, which is what lets a calibration done windowed keep working
    fullscreen.
    """
    gray = to_gray(frame).astype(np.float32)
    frame_h, frame_w = gray.shape
    if frame_h == 0 or frame_w == 0:
        return MatchResult(False, 0.0)

    search = template.search_box or NormBox(0.0, 0.0, 1.0, 1.0)
    x0 = max(0, min(frame_w - 1, int(search.x * frame_w)))
    y0 = max(0, min(frame_h - 1, int(search.y * frame_h)))
    x1 = max(x0 + 1, min(frame_w, x0 + int(search.width * frame_w)))
    y1 = max(y0 + 1, min(frame_h, y0 + int(search.height * frame_h)))
    region = gray[y0:y1, x0:x1]

    target_h = max(1, int(round(template.source_box.height * frame_h)))
    target_w = max(1, int(round(template.source_box.width * frame_w)))
    needle = _resize_gray(template.pixels, (target_h, target_w))
    if needle.shape[0] > region.shape[0] or needle.shape[1] > region.shape[1]:
        return MatchResult(False, 0.0)

    outcome = _match_opencv(region, needle) if use_opencv else None
    if outcome is None:
        outcome = _match_numpy(region, needle)
    score, mx, my = outcome

    if score < template.threshold:
        return MatchResult(False, score)
    centre_x = (x0 + mx + needle.shape[1] / 2) / frame_w
    centre_y = (y0 + my + needle.shape[0] / 2) / frame_h
    return MatchResult(True, score, NormPoint(centre_x, centre_y))


def capture_template(
    frame: np.ndarray, box: NormBox, name: str, search_box: NormBox | None = None,
    threshold: float = DEFAULT_THRESHOLD,
) -> ScreenTemplate:
    """Cut a template out of a client-area frame during calibration."""
    from .capture import crop_norm

    pixels = to_gray(crop_norm(frame, box)).astype(np.float32)
    return ScreenTemplate(
        name=name, pixels=pixels, source_box=box, search_box=search_box, threshold=threshold
    )
