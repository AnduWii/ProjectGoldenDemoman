"""Turning a screen recording into a draft strategy.

A video holds no clicks and no key presses, only pixels, so every action here is
*inferred* -- and the result is a draft for the user to check, not something to
trust blind. What can be read reliably and what has to be guessed:

* **Reliable:** the wave and the cash over time, read with the same HUD reader
  as live play. Every cash *drop* is a purchase, and the size of the drop is
  what it cost.
* **Inferred:** where a purchase happened. At the moment of a drop, the frame
  is compared with the one just before; anything that was already moving (the
  enemies) is masked out by also comparing the two frames before that, which
  leaves what newly appeared and stayed: a placed tower, or an upgraded one
  changing appearance. Both a new tower and the placement circle that vanishes
  with it are centred on the spot.
* **Inferred:** placement or upgrade. A change on top of an already-placed
  tower, at a price no placement has cost, is an upgrade; anything else is a
  new tower.
* **Not readable at all:** which hotbar slot a tower came from (the user maps
  each tower *price* to one of their own slots -- another player's loadout is
  not theirs anyway), sells, abilities and wave skips.

Pure numpy and OpenCV; no Qt, so the whole pipeline is unit-tested against
synthetic videos.
"""

from __future__ import annotations

import logging
import math
from collections import deque
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path

import cv2
import numpy as np

from ..input.coords import NormPoint
from ..strategy.steps import Place, Step, Strategy, Upgrade, Wait, WaitForCash, WaitForWave
from .calibration import Calibration
from .capture import NormBox, crop_norm
from .digits import read_number
from .hud import Stabilizer

log = logging.getLogger(__name__)

WHOLE_FRAME = NormBox(0.0, 0.0, 1.0, 1.0)
PLACE = "place"
UPGRADE = "upgrade"
UNKNOWN = "unknown"
SKIP = "skip"
VIDEO_EXTENSIONS = ("*.mp4", "*.mov", "*.mkv", "*.webm", "*.avi", "*.m4v")
# Kill income keeps arriving, so the same tower bought twice can show slightly
# different drops; drops within this fraction of each other are one price.
PRICE_TOLERANCE = 0.15
# A measured drop under-states the price by whatever income landed in the same
# moment, so a cash wait asks for a little more than was seen.
CASH_MARGIN = 0.05


class VideoError(Exception):
    """A video that cannot be opened or read, with a message for the user."""


@dataclass(frozen=True)
class VideoInfo:
    path: Path
    fps: float
    frame_count: int
    width: int
    height: int

    @property
    def duration_s(self) -> float:
        return self.frame_count / self.fps if self.fps > 0 else 0.0

    def describe(self) -> str:
        minutes, seconds = divmod(int(self.duration_s), 60)
        return f"{self.width}x{self.height}, {minutes}:{seconds:02d}, {self.fps:.0f} fps"


@dataclass
class VideoSettings:
    """Tunables for the analysis. Defaults suit a normal 30-60 fps recording."""

    sample_fps: float = 4.0
    game_box: NormBox = WHOLE_FRAME
    work_width: int = 480  # change detection runs on a downscaled grey copy
    change_threshold: int = 28  # grey-level difference that counts as "changed"
    min_blob_fraction: float = 0.0004  # smallest change worth calling a tower
    tower_radius: float = 0.045  # how close a change must be to a tower to be an upgrade
    thumbnail_width: int = 480


@dataclass
class Purchase:
    """One cash drop, and what the app thinks it was."""

    t: float
    wave: int | None
    cost: int
    cash_before: int
    kind: str = UNKNOWN
    position: NormPoint | None = None
    tower_id: str = ""
    confidence: float = 0.0
    thumbnail: np.ndarray | None = None
    note: str = ""

    def timestamp(self) -> str:
        minutes, seconds = divmod(int(self.t), 60)
        return f"{minutes}:{seconds:02d}"

    def describe(self) -> str:
        wave = f"wave {self.wave}" if self.wave is not None else "wave ?"
        what = {
            PLACE: f"place {self.tower_id}",
            UPGRADE: f"upgrade {self.tower_id}",
            SKIP: "skipped",
        }.get(self.kind, "unclear")
        return f"{self.timestamp()}  {wave}  ${self.cost:,}  {what}"


@dataclass
class VideoAnalysis:
    info: VideoInfo
    purchases: list[Purchase] = field(default_factory=list)
    aspect_ratio: float = 0.0
    match_start_t: float | None = None
    samples: int = 0
    unreadable_samples: int = 0
    warnings: list[str] = field(default_factory=list)

    @property
    def readable_fraction(self) -> float:
        if not self.samples:
            return 0.0
        return 1.0 - self.unreadable_samples / self.samples

    def price_groups(self) -> dict[int, list[Purchase]]:
        """Placements grouped by price: price -> the placements at that price.

        Each group stands for one kind of tower. The price is the largest drop
        in the group, because income landing at the same moment can only make
        a drop look *smaller* than the real price, never larger.
        """
        places = sorted((p for p in self.purchases if p.kind == PLACE), key=lambda p: p.cost)
        groups: list[list[Purchase]] = []
        for purchase in places:
            if groups and purchase.cost <= groups[-1][0].cost * (1 + PRICE_TOLERANCE):
                groups[-1].append(purchase)
            else:
                groups.append([purchase])
        return {max(p.cost for p in group): group for group in groups}

    def price_for(self, purchase: Purchase) -> int:
        """The price of the tower group a placement belongs to."""
        for price, group in self.price_groups().items():
            if any(member is purchase for member in group):
                return price
        return purchase.cost

    def tower_costs(self) -> dict[int, int]:
        """Tower price -> how many towers were placed at that price."""
        return {price: len(group) for price, group in self.price_groups().items()}

    def summary(self) -> str:
        places = sum(1 for p in self.purchases if p.kind == PLACE)
        upgrades = sum(1 for p in self.purchases if p.kind == UPGRADE)
        unclear = sum(1 for p in self.purchases if p.kind == UNKNOWN)
        waves = [p.wave for p in self.purchases if p.wave is not None]
        span = f", waves {min(waves)}-{max(waves)}" if waves else ""
        def count(n: int, noun: str) -> str:
            return f"{n} {noun}" + ("" if n == 1 else "s")

        return (
            f"{count(len(self.purchases), 'purchase')}{span}: "
            f"{count(places, 'tower')} placed, {count(upgrades, 'upgrade')}, {unclear} unclear"
        )


# --- reading the video ---------------------------------------------------------------
def check_video_support(folder: Path) -> tuple[bool, str]:
    """Write a few frames and decode them back.

    Proves the bundled OpenCV can actually read video files -- a packaged app
    can import cv2 fine and still lack the decoder. Used by ``--self-test``.
    """
    try:
        backends = ", ".join(
            cv2.videoio_registry.getBackendName(b) for b in cv2.videoio_registry.getStreamBackends()
        )
    except (AttributeError, cv2.error):  # pragma: no cover - very old OpenCV
        backends = "unknown"
    path = Path(folder) / "video-self-test.avi"
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        writer = cv2.VideoWriter(str(path), cv2.VideoWriter.fourcc(*"MJPG"), 10, (64, 48))
        if not writer.isOpened():
            return False, f"cannot write a test video (backends: {backends})"
        for value in range(0, 250, 25):
            writer.write(np.full((48, 64, 3), value, dtype=np.uint8))
        writer.release()
        capture, info = open_video(path)
        frames = 0
        while capture.grab():
            frames += 1
        capture.release()
    except (OSError, VideoError, cv2.error) as exc:
        return False, f"{exc} (backends: {backends})"
    finally:
        path.unlink(missing_ok=True)
    if frames < 5:
        return False, f"decoded only {frames} of 10 frames (backends: {backends})"
    return True, f"available ({info.width}x{info.height}, {frames} frames; backends: {backends})"

def open_video(path: Path) -> tuple[cv2.VideoCapture, VideoInfo]:
    capture = cv2.VideoCapture(str(path))
    if not capture.isOpened():
        raise VideoError(
            f"Could not open {Path(path).name}. If it plays elsewhere, convert it to an "
            "MP4 (H.264) file and try again."
        )
    fps = float(capture.get(cv2.CAP_PROP_FPS) or 0.0)
    frames = int(capture.get(cv2.CAP_PROP_FRAME_COUNT) or 0)
    width = int(capture.get(cv2.CAP_PROP_FRAME_WIDTH) or 0)
    height = int(capture.get(cv2.CAP_PROP_FRAME_HEIGHT) or 0)
    if fps <= 0 or width <= 0 or height <= 0:
        capture.release()
        raise VideoError(f"{Path(path).name} does not look like a video this app can read.")
    return capture, VideoInfo(Path(path), fps, frames, width, height)


def read_frame_at(path: Path, seconds: float) -> np.ndarray:
    """One frame, for previews and for teaching the HUD from the video."""
    capture, info = open_video(path)
    try:
        index = max(0, min(int(seconds * info.fps), max(0, info.frame_count - 1)))
        capture.set(cv2.CAP_PROP_POS_FRAMES, index)
        ok, frame = capture.read()
        if not ok or frame is None:
            raise VideoError(f"Could not read the frame at {seconds:.1f}s.")
        return frame
    finally:
        capture.release()


def game_area(frame: np.ndarray, box: NormBox) -> np.ndarray:
    return crop_norm(frame, box) if box != WHOLE_FRAME else frame


# --- locating what changed --------------------------------------------------------------
def _small_colour(image: np.ndarray, width: int) -> np.ndarray:
    """A downscaled, slightly blurred *colour* copy for change detection.

    Colour, not grey: an orange tower on grey-green terrain can have almost
    the same brightness as the ground, and a grey comparison then sees no
    change at all where a tower obviously appeared.
    """
    height = max(1, int(round(image.shape[0] * width / image.shape[1])))
    small = cv2.resize(image[:, :, :3], (width, height), interpolation=cv2.INTER_AREA)
    return cv2.GaussianBlur(small, (5, 5), 0)


def _difference(a: np.ndarray, b: np.ndarray) -> np.ndarray:
    """Per-pixel change: the largest difference over the colour channels."""
    diff = cv2.absdiff(a, b)
    return diff.max(axis=2) if diff.ndim == 3 else diff


def _hud_mask(shape: tuple[int, int], calibration: Calibration) -> np.ndarray:
    """True over the wave and cash readouts (padded): their digits change all
    the time and must never be mistaken for a tower."""
    height, width = shape
    mask = np.zeros(shape, dtype=bool)
    for region in (calibration.wave, calibration.cash):
        box = region.box
        if box is None:
            continue
        pad_x, pad_y = box.width * 0.5, box.height * 0.5
        left = max(0, int((box.x - pad_x) * width))
        top = max(0, int((box.y - pad_y) * height))
        right = min(width, int((box.x + box.width + pad_x) * width) + 1)
        bottom = min(height, int((box.y + box.height + pad_y) * height) + 1)
        mask[top:bottom, left:right] = True
    return mask


def locate_new_object(
    before2: np.ndarray,
    before: np.ndarray,
    after: np.ndarray,
    hud: np.ndarray,
    settings: VideoSettings,
) -> list[tuple[NormPoint, float]]:
    """Where something newly appeared and stayed, strongest first.

    ``(after vs before)`` holds the purchase *and* everything in motion;
    ``(before vs before2)`` holds only the motion. Removing the second, grown a
    little, from the first leaves the new static change. Returns centroids in
    normalized coordinates with a strength score in [0, 1].
    """
    threshold = settings.change_threshold
    new = _difference(after, before) > threshold
    motion = (_difference(before, before2) > threshold).astype(np.uint8)
    motion = cv2.dilate(motion, np.ones((7, 7), np.uint8), iterations=2).astype(bool)
    candidate = (new & ~motion & ~hud).astype(np.uint8)
    candidate = cv2.morphologyEx(candidate, cv2.MORPH_OPEN, np.ones((3, 3), np.uint8))

    count, _, stats, centroids = cv2.connectedComponentsWithStats(candidate, connectivity=8)
    height, width = candidate.shape
    minimum = max(4, int(settings.min_blob_fraction * height * width))
    blobs: list[tuple[NormPoint, float]] = []
    for label in range(1, count):
        area = int(stats[label, cv2.CC_STAT_AREA])
        if area < minimum:
            continue
        cx, cy = centroids[label]
        strength = min(1.0, area / (minimum * 10))
        blobs.append((NormPoint(float(cx) / width, float(cy) / height), strength))
    blobs.sort(key=lambda blob: blob[1], reverse=True)
    return blobs


def classify(
    purchase: Purchase,
    blobs: list[tuple[NormPoint, float]],
    towers: dict[str, NormPoint],
    placement_costs: set[int],
    settings: VideoSettings,
) -> None:
    """Decide placement vs upgrade, and where. Mutates ``purchase``."""
    if not blobs:
        purchase.kind = UNKNOWN
        purchase.note = "nothing visibly changed at the moment of this purchase"
        return
    position, strength = blobs[0]
    nearest: tuple[float, str] | None = None
    for tower_id, spot in towers.items():
        distance = spot.distance_to(position)
        if distance <= settings.tower_radius and (nearest is None or distance < nearest[0]):
            nearest = (distance, tower_id)
    # A change on top of an existing tower, at a price no placement has had,
    # is that tower being upgraded. The same price as a known placement next
    # to an existing tower is more likely a second copy placed beside it.
    if nearest is not None and purchase.cost not in placement_costs:
        purchase.kind = UPGRADE
        purchase.tower_id = nearest[1]
        purchase.position = towers[nearest[1]]
    else:
        purchase.kind = PLACE
        purchase.position = position
    purchase.confidence = strength
    if strength < 0.35:
        purchase.note = "weak change on screen -- check the spot"


# --- the whole pass --------------------------------------------------------------------
def analyze_video(
    path: Path,
    calibration: Calibration,
    settings: VideoSettings | None = None,
    progress: Callable[[float], None] | None = None,
    should_stop: Callable[[], bool] | None = None,
) -> VideoAnalysis:
    settings = settings or VideoSettings()
    capture, info = open_video(path)
    analysis = VideoAnalysis(info=info)
    box = settings.game_box
    analysis.aspect_ratio = (info.width * box.width) / max(1.0, info.height * box.height)
    if not calibration.hud_ready:
        capture.release()
        raise VideoError(
            "The app needs to read the wave and cash numbers in this video first. Mark "
            "them on a frame of the video (the 'Teach' step) and try again."
        )

    step = max(1, int(round(info.fps / max(0.5, settings.sample_fps))))
    wave_stab, cash_stab = Stabilizer(3, 2), Stabilizer(3, 2)
    raw_cash: deque[tuple[int, float, int | None]] = deque(maxlen=6)
    frames: dict[int, tuple[np.ndarray, np.ndarray]] = {}  # sample -> (small, thumbnail)
    pending: list[tuple[int, Purchase]] = []
    towers: dict[str, NormPoint] = {}
    placement_costs: set[int] = set()
    published_cash: int | None = None
    current_wave: int | None = None
    hud_mask: np.ndarray | None = None
    sample = -1
    next_tower = 1

    def resolve(index: int, purchase: Purchase) -> None:
        nonlocal next_tower
        before2 = frames.get(index - 2) or frames.get(index - 1)
        before = frames.get(index - 1)
        after = frames.get(index + 1) or frames.get(index)
        if before is None or before2 is None or after is None or hud_mask is None:
            purchase.kind = UNKNOWN
            purchase.note = "too close to the start or end of the video to see"
        else:
            blobs = locate_new_object(before2[0], before[0], after[0], hud_mask, settings)
            classify(purchase, blobs, towers, placement_costs, settings)
            purchase.thumbnail = after[1]
        if purchase.kind == PLACE:
            purchase.tower_id = f"T{next_tower}"
            next_tower += 1
            assert purchase.position is not None
            towers[purchase.tower_id] = purchase.position
            placement_costs.add(purchase.cost)
        analysis.purchases.append(purchase)

    try:
        for frame_index in range(info.frame_count or 10**9):
            if should_stop is not None and should_stop():
                analysis.warnings.append("Stopped before the end of the video.")
                break
            if frame_index % step:
                if not capture.grab():
                    break
                continue
            ok, frame = capture.read()
            if not ok or frame is None:
                break
            sample += 1
            game = game_area(frame, box)
            grey = _small_colour(game, settings.work_width)
            if hud_mask is None:
                hud_mask = _hud_mask(grey.shape[:2], calibration)
            thumb_height = int(round(game.shape[0] * settings.thumbnail_width / game.shape[1]))
            thumb = cv2.resize(game, (settings.thumbnail_width, max(1, thumb_height)))
            frames[sample] = (grey, thumb)
            frames.pop(sample - 8, None)
            t = frame_index / info.fps

            wave = read_number(
                crop_norm(game, calibration.wave.box), calibration.wave.classifier,  # type: ignore[arg-type]
                calibration.wave.invert,
            ).value
            cash = read_number(
                crop_norm(game, calibration.cash.box), calibration.cash.classifier,  # type: ignore[arg-type]
                calibration.cash.invert,
            ).value
            analysis.samples += 1
            if cash is None:
                analysis.unreadable_samples += 1
            current_wave = wave_stab.push(wave) or current_wave
            if current_wave is not None and analysis.match_start_t is None:
                analysis.match_start_t = t

            raw_cash.append((sample, t, cash))
            smoothed = cash_stab.push(cash)
            if published_cash is not None and smoothed is not None and smoothed < published_cash:
                # Date the purchase to the first frame that showed the new
                # figure, not the later frame where smoothing confirmed it --
                # and measure it from the frame just before that, not from the
                # smoothed value, which can be several frames (and several
                # income ticks) old.
                first = next((s for s, _, v in raw_cash if v == smoothed), sample)
                when = next((tt for s, tt, _ in raw_cash if s == first), t)
                earlier = [v for s, _, v in raw_cash if s < first and v is not None]
                before = earlier[-1] if earlier and earlier[-1] > smoothed else published_cash
                pending.append(
                    (
                        first,
                        Purchase(
                            t=when,
                            wave=current_wave,
                            cost=before - smoothed,
                            cash_before=before,
                        ),
                    )
                )
            if smoothed is not None:
                published_cash = smoothed

            # Resolve purchases once the frame after them has been seen.
            ready = [(i, p) for i, p in pending if i + 1 <= sample]
            for item in ready:
                pending.remove(item)
                resolve(*item)

            if progress is not None and info.frame_count:
                progress(min(1.0, frame_index / info.frame_count))
        for item in pending:
            resolve(*item)
    finally:
        capture.release()

    analysis.purchases.sort(key=lambda p: p.t)
    if analysis.samples and analysis.readable_fraction < 0.5:
        analysis.warnings.append(
            f"Cash was readable in only {analysis.readable_fraction:.0%} of the video. Check "
            "the wave and cash boxes match this video, and teach more digits."
        )
    if not analysis.purchases:
        analysis.warnings.append(
            "No purchases were found. Either the cash number could not be read, or nothing "
            "was bought in this video."
        )
    return analysis


# --- turning the analysis into steps ---------------------------------------------------
def _with_margin(price: int) -> int:
    """Round a price up by CASH_MARGIN to the nearest 5."""
    return int(math.ceil(price * (1 + CASH_MARGIN) / 5.0) * 5)


def build_strategy(
    analysis: VideoAnalysis,
    slot_for_cost: dict[int, int],
    name: str,
    gate_on_hud: bool = True,
) -> Strategy:
    """The draft strategy.

    With ``gate_on_hud`` each purchase waits for its wave and for enough cash,
    which adapts to however the user's own match is going. Without it the
    original timing from the video is used instead (TinyTask-style), measured
    from when the match first showed a wave number.
    """
    steps: list[Step] = []
    last_wave: int | None = None
    origin = analysis.match_start_t or 0.0
    last_t = origin
    for purchase in analysis.purchases:
        if purchase.kind not in (PLACE, UPGRADE):
            continue
        if purchase.kind == PLACE and purchase.position is None:
            continue
        price = analysis.price_for(purchase) if purchase.kind == PLACE else purchase.cost
        if gate_on_hud:
            if purchase.wave is not None and (last_wave is None or purchase.wave > last_wave):
                if purchase.wave > 1 or last_wave is not None:
                    steps.append(WaitForWave(wave=purchase.wave))
                last_wave = purchase.wave
            steps.append(WaitForCash(amount=_with_margin(price)))
        else:
            gap = int(round((purchase.t - last_t) * 1000))
            if gap >= 300:
                steps.append(Wait(ms=gap))
            last_t = purchase.t
        note = f"from the video at {purchase.timestamp()}, ${price:,}"
        if purchase.kind == PLACE:
            assert purchase.position is not None
            steps.append(
                Place(
                    slot=slot_for_cost.get(price, 1),
                    position=purchase.position,
                    tower_id=purchase.tower_id,
                    note=note,
                )
            )
        else:
            steps.append(Upgrade(tower_id=purchase.tower_id, note=note))
    return Strategy(
        name=name,
        steps=steps,
        description=(
            f"Imported from the video {analysis.info.path.name}. Every step was inferred "
            "from the picture -- check tower spots and hotbar slots before trusting it."
        ),
        aspect_ratio=analysis.aspect_ratio,
    )
