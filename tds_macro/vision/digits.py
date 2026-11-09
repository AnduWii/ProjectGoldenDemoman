"""Reading numbers off the HUD with a template classifier.

Why not OCR: Tesseract is tuned for document text and misreads stylized game
fonts (``$1,250`` comes back as ``$I25O`` often enough to break a cash gate),
and bundling it would add tens of megabytes plus a language-data folder to an
app that is supposed to be a portable drop-in.  TDS draws its HUD in one fixed
font at a fixed size, so matching against ten labelled glyph templates captured
during calibration is both smaller and more accurate.

The cost is a one-time labelling step in the setup wizard, and the need to
re-calibrate if the HUD is restyled.  Both are stated in the README.

Pure numpy: no OpenCV, no display, fully unit-testable.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field

import numpy as np

log = logging.getLogger(__name__)

GLYPH_SIZE = (16, 12)  # (height, width) every glyph is normalized to
# A readout like "12/40" is current/total: the value is what comes before it.
RATIO_SEPARATOR = "/"


@dataclass(frozen=True)
class GlyphBox:
    """Horizontal extent of one glyph within a cropped region."""

    left: int
    right: int

    @property
    def width(self) -> int:
        return self.right - self.left


@dataclass(frozen=True)
class ReadResult:
    """Outcome of reading one HUD region."""

    value: int | None
    text: str
    confidence: float
    glyphs: int = 0
    reason: str = ""

    @property
    def ok(self) -> bool:
        return self.value is not None


def to_gray(image: np.ndarray) -> np.ndarray:
    """Luminance from BGR/BGRA/RGB or an already-grayscale array."""
    if image.ndim == 2:
        return image.astype(np.float32)
    channels = image.shape[2]
    if channels < 3:
        return image[:, :, 0].astype(np.float32)
    # mss hands back BGRA; the weights are symmetric enough that channel order
    # only shifts luminance slightly, and the threshold is computed per image.
    blue, green, red = (image[:, :, i].astype(np.float32) for i in range(3))
    return 0.114 * blue + 0.587 * green + 0.299 * red


def otsu_threshold(gray: np.ndarray) -> float:
    """Otsu's method: pick the threshold that best separates two populations.

    Chosen over a fixed cutoff because HUD brightness changes with the map and
    with whatever is rendered behind the text.
    """
    flat = gray.reshape(-1)
    if flat.size == 0:
        return 0.0
    low, high = float(flat.min()), float(flat.max())
    if high - low < 1e-6:
        return low
    hist, edges = np.histogram(flat, bins=64, range=(low, high))
    hist = hist.astype(np.float64)
    total = hist.sum()
    weights_bg = np.cumsum(hist)
    weights_fg = total - weights_bg
    centres = (edges[:-1] + edges[1:]) / 2.0
    sum_total = float((hist * centres).sum())
    sum_bg = np.cumsum(hist * centres)
    with np.errstate(invalid="ignore", divide="ignore"):
        mean_bg = np.where(weights_bg > 0, sum_bg / weights_bg, 0.0)
        mean_fg = np.where(weights_fg > 0, (sum_total - sum_bg) / weights_fg, 0.0)
        variance = weights_bg * weights_fg * (mean_bg - mean_fg) ** 2
    variance[~np.isfinite(variance)] = 0.0
    return float(centres[int(np.argmax(variance))])


def binarize(gray: np.ndarray, invert: bool | None = None) -> np.ndarray:
    """Boolean mask of the text pixels.

    ``invert=None`` decides automatically from the border, which is nearly
    always background: whichever polarity leaves the border mostly empty is the
    right one.  That makes the reader work on both light-on-dark and
    dark-on-light HUD themes without a setting.
    """
    threshold = otsu_threshold(gray)
    mask = gray > threshold
    if invert is None:
        border = np.concatenate(
            [mask[0, :].ravel(), mask[-1, :].ravel(), mask[:, 0].ravel(), mask[:, -1].ravel()]
        )
        invert = bool(border.mean() > 0.5) if border.size else False
    return ~mask if invert else mask


def segment_glyphs(
    mask: np.ndarray,
    min_width: int = 1,
    max_gap: int = 1,
    min_fill: int = 1,
    min_ink_ratio: float = 0.1,
) -> list[GlyphBox]:
    """Split a text mask into glyphs by column projection.

    ``max_gap`` bridges the one-pixel holes inside a font's strokes without
    merging neighbouring characters.  ``min_ink_ratio`` then drops speckles:
    a box holding less than that fraction of the inkiest box's pixels is
    noise, not a character.  It is a *ratio* rather than an absolute count so
    the same value works at any HUD size, and it is low enough to keep genuinely
    thin glyphs such as "1" and ",".
    """
    if mask.size == 0:
        return []
    columns = mask.sum(axis=0)
    active = columns >= min_fill
    boxes: list[GlyphBox] = []
    start: int | None = None
    gap = 0
    for index, is_on in enumerate(active):
        if is_on:
            if start is None:
                start = index
            gap = 0
        elif start is not None:
            gap += 1
            if gap > max_gap:
                boxes.append(GlyphBox(start, index - gap + 1))
                start = None
                gap = 0
    if start is not None:
        boxes.append(GlyphBox(start, len(active)))

    boxes = [b for b in boxes if b.width >= min_width]
    if not boxes or min_ink_ratio <= 0:
        return boxes
    ink = [int(mask[:, b.left : b.right].sum()) for b in boxes]
    floor = max(ink) * min_ink_ratio
    return [b for b, amount in zip(boxes, ink, strict=True) if amount >= floor]


def crop_glyph(mask: np.ndarray, box: GlyphBox) -> np.ndarray:
    """Tightly crop one glyph, trimming blank rows as well as columns."""
    strip = mask[:, box.left : box.right]
    rows = np.flatnonzero(strip.any(axis=1))
    if rows.size == 0:
        return strip
    return strip[rows[0] : rows[-1] + 1, :]


def resize_nearest(mask: np.ndarray, size: tuple[int, int] = GLYPH_SIZE) -> np.ndarray:
    """Nearest-neighbour resize to an exact size.

    Nearest rather than interpolated on purpose: these are 1-bit shapes a dozen
    pixels tall, and smoothing them blurs the very strokes that tell 6 from 8.
    """
    height, width = size
    if mask.size == 0 or height <= 0 or width <= 0:
        return np.zeros((max(height, 0), max(width, 0)), dtype=bool)
    src_h, src_w = mask.shape
    rows = (np.arange(height) * src_h // height).clip(0, src_h - 1)
    cols = (np.arange(width) * src_w // width).clip(0, src_w - 1)
    return mask[rows][:, cols]


def tight_crop(mask: np.ndarray) -> np.ndarray:
    """Trim blank rows *and* columns.

    Applied on both sides of the comparison so a template stays valid however
    the caller cropped it -- without this, a template captured with padding
    never matched the tightly segmented glyph it was meant to represent.
    """
    if mask.size == 0:
        return mask
    rows = np.flatnonzero(mask.any(axis=1))
    cols = np.flatnonzero(mask.any(axis=0))
    if rows.size == 0 or cols.size == 0:
        return mask[:0, :0]
    return mask[rows[0] : rows[-1] + 1, cols[0] : cols[-1] + 1]


def fit_glyph(mask: np.ndarray, size: tuple[int, int] = GLYPH_SIZE) -> np.ndarray:
    """Scale a glyph into a fixed box **preserving its aspect ratio**, centred.

    Stretching each glyph to fill the box would make a "1" (a thin bar) occupy
    the same footprint as a "0", which is exactly the confusion that breaks a
    cash readout.  Letter-boxing keeps the proportions that tell them apart,
    while staying invariant to the HUD's absolute size.
    """
    height, width = size
    cropped = tight_crop(mask)
    if cropped.size == 0:
        return np.zeros(size, dtype=bool)
    src_h, src_w = cropped.shape
    scale = min(height / src_h, width / src_w)
    new_h = max(1, min(height, int(round(src_h * scale))))
    new_w = max(1, min(width, int(round(src_w * scale))))
    resized = resize_nearest(cropped, (new_h, new_w))
    out = np.zeros(size, dtype=bool)
    top = (height - new_h) // 2
    left = (width - new_w) // 2
    out[top : top + new_h, left : left + new_w] = resized
    return out


def normalize_template(glyph: np.ndarray) -> np.ndarray:
    """Zero-mean, unit-norm float vector, so matching is plain cosine similarity."""
    vector = fit_glyph(glyph).astype(np.float32).reshape(-1)
    vector -= vector.mean()
    norm = float(np.linalg.norm(vector))
    if norm < 1e-6:
        return np.zeros_like(vector)
    return vector / norm


@dataclass
class DigitClassifier:
    """Labelled glyph templates plus nearest-match lookup.

    ``min_confidence`` is deliberately strict: returning no value is far safer
    than returning a wrong wave number, which would make the engine act early.
    """

    templates: dict[str, list[np.ndarray]] = field(default_factory=dict)
    min_confidence: float = 0.72

    @property
    def labels(self) -> list[str]:
        return sorted(self.templates)

    @property
    def is_trained(self) -> bool:
        return any(self.templates.values())

    def add(self, label: str, glyph: np.ndarray) -> None:
        """Teach one example.  Several examples per label are allowed and help
        with antialiasing differences between positions on screen."""
        self.templates.setdefault(label, []).append(normalize_template(glyph))

    def classify(self, glyph: np.ndarray) -> tuple[str, float]:
        """Best matching label and its cosine similarity in ``[-1, 1]``."""
        vector = normalize_template(glyph)
        best_label, best_score = "", -1.0
        for label, examples in self.templates.items():
            for example in examples:
                score = float(np.dot(vector, example))
                if score > best_score:
                    best_label, best_score = label, score
        return best_label, best_score

    def to_dict(self) -> dict[str, list[list[float]]]:
        return {
            label: [example.tolist() for example in examples]
            for label, examples in self.templates.items()
        }

    @classmethod
    def from_dict(
        cls, data: dict[str, list[list[float]]], min_confidence: float = 0.72
    ) -> DigitClassifier:
        templates = {
            str(label): [np.asarray(e, dtype=np.float32) for e in examples]
            for label, examples in data.items()
        }
        return cls(templates=templates, min_confidence=min_confidence)


def read_number(
    image: np.ndarray,
    classifier: DigitClassifier,
    invert: bool | None = None,
    min_glyph_width: int = 1,
) -> ReadResult:
    """Read an integer out of a cropped HUD region.

    Returns ``value=None`` with a reason whenever anything is uncertain --
    no glyphs found, a glyph below the confidence floor, or no digits at all.
    The engine treats that as "unknown", never as zero.
    """
    if not classifier.is_trained:
        return ReadResult(None, "", 0.0, reason="HUD not calibrated yet")
    if image.size == 0:
        return ReadResult(None, "", 0.0, reason="empty capture region")

    mask = binarize(to_gray(image), invert=invert)
    boxes = segment_glyphs(mask, min_width=min_glyph_width)
    if not boxes:
        return ReadResult(None, "", 0.0, reason="no text found in the region")

    labels: list[str] = []
    worst = 1.0
    for box in boxes:
        label, score = classifier.classify(crop_glyph(mask, box))
        if score < classifier.min_confidence:
            return ReadResult(
                None,
                "".join(labels),
                max(0.0, score),
                glyphs=len(boxes),
                reason=f"glyph {len(labels) + 1} of {len(boxes)} matched only {score:.2f}",
            )
        worst = min(worst, score)
        labels.append(label)

    text = "".join(labels)
    digits = parse_readout(text)
    if not digits:
        return ReadResult(None, text, worst, glyphs=len(boxes), reason="no digits recognised")
    return ReadResult(int(digits), text, worst, glyphs=len(boxes))


def parse_readout(text: str) -> str:
    """The digits of a readout's value, as a string ("" if there are none).

    "$1,250" -> "1250": currency signs and thousands separators are dropped.
    "12/40" -> "12": only the part before a ratio separator is the value.
    Joining every digit instead turned wave 12 of 40 into wave 1240, which
    satisfied every WaitForWave at once and ran the whole strategy immediately.
    """
    value = text.split(RATIO_SEPARATOR, 1)[0]
    return "".join(ch for ch in value if ch.isdigit())


@dataclass(frozen=True)
class LearnOutcome:
    """Result of teaching the classifier from a typed readout."""

    added: int
    glyphs: int
    error: str = ""

    @property
    def ok(self) -> bool:
        return not self.error


def learn_from_text(
    image: np.ndarray,
    text: str,
    classifier: DigitClassifier,
    invert: bool | None = None,
    min_glyph_width: int = 1,
) -> LearnOutcome:
    """Teach glyphs by pairing a captured region with what the user says it reads.

    This is the whole calibration interaction: rather than asking someone to
    label a dozen anonymous glyph bitmaps, they type what is on screen ("12",
    "$1,250") and the characters are zipped onto the segmented glyphs in order.

    A count mismatch is reported instead of guessed at -- silently pairing five
    characters onto six glyphs would poison every later reading, and a poisoned
    classifier is far worse than an uncalibrated one.
    """
    cleaned = "".join(text.split())
    if not cleaned:
        return LearnOutcome(0, 0, error="type what the readout currently shows first")
    if image.size == 0:
        return LearnOutcome(0, 0, error="the captured region is empty")

    mask = binarize(to_gray(image), invert=invert)
    boxes = segment_glyphs(mask, min_width=min_glyph_width)
    if not boxes:
        return LearnOutcome(
            0,
            0,
            error=(
                "no characters were found in that region -- redraw the box tightly "
                "around just the number"
            ),
        )
    if len(boxes) != len(cleaned):
        return LearnOutcome(
            0,
            len(boxes),
            error=(
                f"found {len(boxes)} character(s) in the region but you typed "
                f"{len(cleaned)} ({cleaned!r}). Redraw the box around only the "
                "number, or correct what you typed."
            ),
        )

    for character, box in zip(cleaned, boxes, strict=True):
        classifier.add(character, crop_glyph(mask, box))
    return LearnOutcome(len(cleaned), len(boxes))


def missing_digits(classifier: DigitClassifier) -> list[str]:
    """Digits 0-9 the classifier has never seen.

    Shown in the wizard so the user knows to keep playing until the HUD has
    displayed every digit, rather than discovering a gap mid-run.
    """
    known = set(classifier.templates)
    return [digit for digit in "0123456789" if digit not in known]


def glyph_previews(
    image: np.ndarray, invert: bool | None = None, min_glyph_width: int = 1
) -> list[np.ndarray]:
    """Segmented glyph masks, for showing the user what was detected."""
    if image.size == 0:
        return []
    mask = binarize(to_gray(image), invert=invert)
    return [crop_glyph(mask, box) for box in segment_glyphs(mask, min_width=min_glyph_width)]
