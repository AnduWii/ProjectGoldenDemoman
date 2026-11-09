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
from collections.abc import Callable, Iterable
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

    # Samples per second of video. High enough that two purchases a fraction
    # of a second apart are each seen; decoding every frame dominates anyway.
    sample_fps: float = 8.0
    game_box: NormBox = WHOLE_FRAME
    # Areas of the game view to ignore (relative to the game area), such as
    # the tower panel: its text changes with every purchase.
    ignore_boxes: list[NormBox] = field(default_factory=list)
    work_width: int = 480  # change detection runs on a downscaled grey copy
    change_threshold: int = 28  # grey-level difference that counts as "changed"
    min_blob_fraction: float = 0.0004  # smallest change worth calling a tower
    tower_radius: float = 0.045  # how close a change must be to a tower to be an upgrade
    thumbnail_width: int = 480
    # A spot that changes along with at least this share of all purchases (and
    # at least interface_min_purchases of them) is the interface, not a tower.
    interface_share: float = 0.5
    interface_min_purchases: int = 6
    interface_grid: int = 24  # cells per side of the change-frequency map


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
    # Purchases split out of one cash drop share this number (see _split_drop).
    drop: int = 0

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
    # Areas found to be the interface and ignored (see _interface_areas).
    interface_areas: list[NormBox] = field(default_factory=list)

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
# Formats tried, in order, for video the app writes itself (the self-test, and
# the synthetic matches in the tests). OpenCV builds differ: most bundle FFmpeg
# (MPEG-4 and Motion-JPEG); some Mac builds have only Apple's AVFoundation,
# which writes H.264 or JPEG into MP4/MOV and nothing else.
WRITE_FORMATS = (("mp4v", ".mp4"), ("avc1", ".mp4"), ("MJPG", ".avi"), ("jpeg", ".mov"))


def video_backends() -> str:
    """The decoders this OpenCV build can read files with, for diagnostics."""
    try:
        names = [
            cv2.videoio_registry.getBackendName(b)
            for b in cv2.videoio_registry.getStreamBackends()
        ]
    except (AttributeError, cv2.error):  # pragma: no cover - very old OpenCV
        return "unknown"
    return ", ".join(names) or "none"


def count_frames(path: Path) -> int:
    capture = cv2.VideoCapture(str(path))
    try:
        if not capture.isOpened():
            return 0
        frames = 0
        while capture.grab():
            frames += 1
        return frames
    finally:
        capture.release()


def write_video(
    stem: Path, fps: float, size: tuple[int, int], frames: Callable[[], Iterable[np.ndarray]]
) -> Path:
    """Write ``frames()`` in the first format this OpenCV build can write *and*
    read back. ``stem``'s suffix is replaced by the format's. Raises VideoError
    if no format round-trips."""
    tried = []
    for fourcc, suffix in WRITE_FORMATS:
        path = Path(stem).with_suffix(suffix)
        writer = cv2.VideoWriter(str(path), cv2.VideoWriter.fourcc(*fourcc), fps, size)
        written = 0
        if writer.isOpened():
            for frame in frames():
                writer.write(frame)
                written += 1
        writer.release()
        if written and count_frames(path) >= written // 2:
            return path
        path.unlink(missing_ok=True)
        tried.append(f"{fourcc}{suffix}")
    raise VideoError(
        f"this OpenCV build cannot write and re-read video (tried {', '.join(tried)}; "
        f"decoders: {video_backends()})"
    )


def check_video_support(folder: Path) -> tuple[bool, str]:
    """Write a few frames and decode them back.

    Proves the bundled OpenCV can actually read video files -- a packaged app
    can import cv2 fine and still lack a decoder. Used by ``--self-test``.
    """

    def gradient() -> Iterable[np.ndarray]:
        for value in range(0, 250, 25):
            yield np.full((48, 64, 3), value, dtype=np.uint8)

    try:
        Path(folder).mkdir(parents=True, exist_ok=True)
        path = write_video(Path(folder) / "video-self-test", 10, (64, 48), gradient)
    except (OSError, VideoError, cv2.error) as exc:
        return False, str(exc)
    try:
        frames = count_frames(path)
    finally:
        path.unlink(missing_ok=True)
    return True, f"available ({path.suffix}, {frames} frames; decoders: {video_backends()})"


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
        # By time, not frame number: screen recordings are often variable
        # frame rate, where frame N is not at N / fps seconds.
        capture.set(cv2.CAP_PROP_POS_MSEC, max(0.0, seconds) * 1000.0)
        ok, frame = capture.read()
        if not ok or frame is None:
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


def _hud_mask(
    shape: tuple[int, int], calibration: Calibration, ignore: list[NormBox] | None = None
) -> np.ndarray:
    """True over the wave and cash readouts (padded) -- their digits change all
    the time and must never be mistaken for a tower -- and over any area the
    user chose to ignore."""
    height, width = shape
    mask = np.zeros(shape, dtype=bool)
    for area in ignore or []:
        left, top = max(0, int(area.x * width)), max(0, int(area.y * height))
        right = min(width, int((area.x + area.width) * width) + 1)
        bottom = min(height, int((area.y + area.height) * height) + 1)
        mask[top:bottom, left:right] = True
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
    return find_blobs(change_mask(before2, before, after, hud, settings), settings)


def change_mask(
    before2: np.ndarray,
    before: np.ndarray,
    after: np.ndarray,
    hud: np.ndarray,
    settings: VideoSettings,
) -> np.ndarray:
    """Pixels that newly changed and stayed (see :func:`locate_new_object`)."""
    threshold = settings.change_threshold
    new = _difference(after, before) > threshold
    motion = (_difference(before, before2) > threshold).astype(np.uint8)
    motion = cv2.dilate(motion, np.ones((7, 7), np.uint8), iterations=2).astype(bool)
    candidate = (new & ~motion & ~hud).astype(np.uint8)
    return cv2.morphologyEx(candidate, cv2.MORPH_OPEN, np.ones((3, 3), np.uint8))


def find_blobs(candidate: np.ndarray, settings: VideoSettings) -> list[tuple[NormPoint, float]]:
    """Connected areas of change, as centroids with a strength in [0, 1]."""
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
        # Towers are compact. A long thin change is text or a bar, and one
        # touching the edge of the view is the interface.
        w, h = int(stats[label, cv2.CC_STAT_WIDTH]), int(stats[label, cv2.CC_STAT_HEIGHT])
        x0, y0 = int(stats[label, cv2.CC_STAT_LEFT]), int(stats[label, cv2.CC_STAT_TOP])
        if max(w, h) > 2.5 * max(1, min(w, h)):
            strength *= 0.3
        if x0 <= 0 or y0 <= 0 or x0 + w >= width or y0 + h >= height:
            strength *= 0.5
        blobs.append((NormPoint(float(cx) / width, float(cy) / height), strength))
    blobs.sort(key=lambda blob: blob[1], reverse=True)
    return blobs


def _near_placement_price(cost: int, placement_costs: set[int]) -> bool:
    """Same tower price as an earlier placement, allowing for income landing
    in the same moment (see PRICE_TOLERANCE)."""
    return any(abs(cost - price) <= price * PRICE_TOLERANCE for price in placement_costs)


def classify(
    purchase: Purchase,
    blobs: list[tuple[NormPoint, float]],
    towers: dict[str, NormPoint],
    placement_costs: set[int],
    settings: VideoSettings,
) -> None:
    """Decide placement vs upgrade, and where. Mutates ``purchase``.

    Every clear change is considered, not just the biggest: the interface
    can change more than the tower does. A change on an existing tower is an
    upgrade. At a price a placement has had, it must be right on the tower --
    a second copy can be placed beside the first, but never on top of it.
    """
    if not blobs:
        purchase.kind = UNKNOWN
        purchase.note = "nothing visibly changed at the moment of this purchase"
        return
    same_price = _near_placement_price(purchase.cost, placement_costs)
    radius = settings.tower_radius / 2 if same_price else settings.tower_radius
    strongest = blobs[0][1]
    nearest: tuple[float, str, float] | None = None
    for position, strength in blobs:
        if strength < max(0.15, strongest * 0.25):
            continue
        for tower_id, spot in towers.items():
            distance = spot.distance_to(position)
            if distance <= radius and (nearest is None or distance < nearest[0]):
                nearest = (distance, tower_id, strength)
    if nearest is not None:
        purchase.kind = UPGRADE
        purchase.tower_id = nearest[1]
        purchase.position = towers[nearest[1]]
        purchase.confidence = nearest[2]
    else:
        purchase.kind = PLACE
        purchase.position, purchase.confidence = blobs[0]
    if purchase.confidence < 0.35:
        purchase.note = "weak change on screen -- check the spot"


def _split_drop(
    readings: list[tuple[int, float, int | None]], published: int, new: int
) -> list[tuple[int, float, int, int]]:
    """The purchases behind one confirmed cash drop: (sample, t, cost, before).

    A drop is dated to the start of the *latest* run of the new value -- not
    an older sample that happened to show the same figure before income
    raised it again. Readings between the old and the new value that never
    lasted long enough to be confirmed are purchases made in quick
    succession (two farms dropped a fraction of a second apart); each becomes
    its own purchase instead of one at the summed price.
    """
    run_start = len(readings) - 1
    for index in range(len(readings) - 1, -1, -1):
        value = readings[index][2]
        if value == new:
            run_start = index
        elif value is not None:
            break
    earlier = [r for r in readings[:run_start] if r[2] is not None]
    since: list[tuple[int, float, int | None]] = []
    for reading in reversed(earlier):
        value = reading[2]
        assert value is not None
        if value >= published:
            break
        since.append(reading)
    since.reverse()
    chain: list[tuple[int, float, int]] = []
    for sample, t, value in since:
        assert value is not None
        if new < value and (not chain or value < chain[-1][2]):
            chain.append((sample, t, value))
    prior_index = len(earlier) - len(since) - 1
    prior = earlier[prior_index][2] if prior_index >= 0 else None
    first_after = chain[0][2] if chain else new
    start = prior if prior is not None and prior > first_after else published
    steps = [*chain, (readings[run_start][0], readings[run_start][1], new)]
    pieces: list[tuple[int, float, int, int]] = []
    before = start
    for sample, t, value in steps:
        pieces.append((sample, t, before - value, before))
        before = value
    return pieces


def _coarse(mask: np.ndarray, cells: int) -> np.ndarray:
    """Which cells of a ``cells`` x ``cells`` grid the change touched."""
    small = cv2.resize(mask.astype(np.float32), (cells, cells), interpolation=cv2.INTER_AREA)
    return np.asarray(small > 0.02)


def _interface_areas(grids: list[np.ndarray], settings: VideoSettings) -> list[NormBox]:
    """Parts of the view that change along with most purchases: the interface.

    A tower panel or an on-screen counter changes with nearly every purchase,
    wherever the tower is -- though not always in the same few pixels, so the
    count is kept per area of a coarse grid, and neighbouring busy areas are
    merged into one rectangle. A tower's own spot changes only when that
    tower is placed or upgraded, a small share of a whole match.
    """
    total = len(grids)
    if total < settings.interface_min_purchases:
        return []
    needed = max(settings.interface_min_purchases, math.ceil(total * settings.interface_share))
    busy = (np.sum(np.stack(grids), axis=0) >= needed).astype(np.uint8)
    if not busy.any():
        return []
    # Join busy cells that touch or nearly touch: one panel, one rectangle.
    joined = cv2.dilate(busy, np.ones((3, 3), np.uint8))
    count, _, stats, _ = cv2.connectedComponentsWithStats(joined, connectivity=8)
    cells = settings.interface_grid
    areas: list[NormBox] = []
    for label in range(1, count):
        x, y = int(stats[label, cv2.CC_STAT_LEFT]), int(stats[label, cv2.CC_STAT_TOP])
        w, h = int(stats[label, cv2.CC_STAT_WIDTH]), int(stats[label, cv2.CC_STAT_HEIGHT])
        areas.append(NormBox(x / cells, y / cells, w / cells, h / cells))
    return areas


def _inside(point: NormPoint, box: NormBox) -> bool:
    return box.x <= point.x <= box.x + box.width and box.y <= point.y <= box.y + box.height


def _frame_time(capture: cv2.VideoCapture, index: int, fps: float, previous: float) -> float:
    """Seconds into the video of the frame just grabbed.

    From the frame's own timestamp: screen recordings are often variable frame
    rate (macOS drops repeated frames), where frame N is not at N / fps.
    Falls back to N / fps if the decoder reports no timestamp.
    """
    msec = float(capture.get(cv2.CAP_PROP_POS_MSEC) or 0.0)
    t = msec / 1000.0 if msec > 0 or index == 0 else index / fps
    return max(t, previous)


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

    interval = 1.0 / max(0.5, settings.sample_fps)
    wave_stab, cash_stab = Stabilizer(3, 2), Stabilizer(3, 2)
    raw_cash: deque[tuple[int, float, int | None]] = deque(maxlen=8)
    frames: dict[int, tuple[np.ndarray, np.ndarray]] = {}  # sample -> (small, thumbnail)
    # (sample, purchase, use the frame of the drop itself as "after")
    pending: list[tuple[int, Purchase, bool]] = []
    seen: list[tuple[Purchase, list[tuple[NormPoint, float]], np.ndarray | None]] = []
    published_cash: int | None = None
    current_wave: int | None = None
    hud_mask: np.ndarray | None = None
    sample = -1
    drops = 0

    def look(index: int, purchase: Purchase, exact: bool) -> None:
        """Find what changed on screen at a purchase; classified at the end."""
        before2 = frames.get(index - 2) or frames.get(index - 1)
        before = frames.get(index - 1)
        # One of several purchases in quick succession: the frame after it
        # already shows the next one, so compare with the frame of the drop.
        after = frames.get(index) if exact else (frames.get(index + 1) or frames.get(index))
        blobs: list[tuple[NormPoint, float]] = []
        grid: np.ndarray | None = None
        if before is None or before2 is None or after is None or hud_mask is None:
            purchase.note = "too close to the start or end of the video to see"
        else:
            mask = change_mask(before2[0], before[0], after[0], hud_mask, settings)
            blobs = find_blobs(mask, settings)
            grid = _coarse(mask, settings.interface_grid)
            purchase.thumbnail = after[1]
        seen.append((purchase, blobs, grid))

    frame_index = -1
    last_t = 0.0
    next_sample_t = 0.0
    duration = info.duration_s
    try:
        while True:
            if should_stop is not None and should_stop():
                analysis.warnings.append("Stopped before the end of the video.")
                break
            # Until the video really ends, not until the reported frame count:
            # for variable-frame-rate files that count is only an estimate.
            if not capture.grab():
                break
            frame_index += 1
            t = _frame_time(capture, frame_index, info.fps, last_t)
            last_t = t
            if t + 1e-6 < next_sample_t:
                continue
            next_sample_t += interval
            if next_sample_t <= t:
                next_sample_t = t + interval
            ok, frame = capture.retrieve()
            if not ok or frame is None:
                continue
            sample += 1
            game = game_area(frame, box)
            grey = _small_colour(game, settings.work_width)
            if hud_mask is None:
                hud_mask = _hud_mask(grey.shape[:2], calibration, settings.ignore_boxes)
            thumb_height = int(round(game.shape[0] * settings.thumbnail_width / game.shape[1]))
            thumb = cv2.resize(game, (settings.thumbnail_width, max(1, thumb_height)))
            frames[sample] = (grey, thumb)
            frames.pop(sample - 10, None)

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
                drops += 1
                pieces = _split_drop(list(raw_cash), published_cash, smoothed)
                for number, (first, when, cost, before) in enumerate(pieces, start=1):
                    purchase = Purchase(
                        t=when, wave=current_wave, cost=cost, cash_before=before, drop=drops
                    )
                    pending.append((first, purchase, number < len(pieces)))
            if smoothed is not None:
                published_cash = smoothed

            # Look at each purchase once the frame after it has been seen.
            ready = [item for item in pending if item[0] + 1 <= sample]
            for item in ready:
                pending.remove(item)
                look(*item)

            if progress is not None and duration > 0:
                progress(min(1.0, t / duration))
        for item in pending:
            look(*item)
    finally:
        capture.release()

    seen.sort(key=lambda item: item[0].t)
    interface = _interface_areas([grid for _, _, grid in seen if grid is not None], settings)
    analysis.interface_areas = interface
    for area in interface:
        analysis.warnings.append(
            f"Ignored an area of the screen ({area.x:.0%} across, {area.y:.0%} down): it "
            "changed with most purchases, so it is most likely the interface, not a tower."
        )
    towers: dict[str, NormPoint] = {}
    placement_costs: set[int] = set()
    next_tower = 1
    for purchase, blobs, _ in seen:
        kept = [blob for blob in blobs if not any(_inside(blob[0], area) for area in interface)]
        if purchase.note != "too close to the start or end of the video to see":
            classify(purchase, kept, towers, placement_costs, settings)
        if purchase.kind == PLACE:
            purchase.tower_id = f"T{next_tower}"
            next_tower += 1
            assert purchase.position is not None
            towers[purchase.tower_id] = purchase.position
            placement_costs.add(purchase.cost)
        analysis.purchases.append(purchase)
    analysis.purchases = _fold_unseen_splits(analysis.purchases)

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


def _fold_unseen_splits(purchases: list[Purchase]) -> list[Purchase]:
    """Undo a split that nothing on screen supports.

    A reading between two cash values is taken as a separate quick purchase;
    if nothing visibly changed for it, it was more likely a misread, so its
    cost goes back into the next purchase from the same drop.
    """
    result: list[Purchase] = []
    carry: Purchase | None = None
    for purchase in purchases:
        if carry is not None and carry.drop == purchase.drop:
            purchase.cost += carry.cost
            purchase.cash_before = carry.cash_before
        elif carry is not None:
            result.append(carry)
        carry = None
        same_drop_follows = any(
            other.drop == purchase.drop and other.t >= purchase.t and other is not purchase
            for other in purchases
        )
        if purchase.drop and purchase.kind == UNKNOWN and not purchase.position and (
            same_drop_follows
        ):
            carry = purchase
            continue
        result.append(purchase)
    if carry is not None:
        result.append(carry)
    return result


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
