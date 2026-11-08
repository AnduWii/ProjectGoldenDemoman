"""Stored HUD calibration: where to look, and what the glyphs look like.

Saved beside the settings file, as plain JSON plus numpy arrays, so a
calibration can be copied between machines along with the rest of the portable
folder.  Nothing is written outside the app's own folder.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path

import numpy as np

from .capture import NormBox
from .digits import DigitClassifier
from .templates import DEFAULT_THRESHOLD, ScreenTemplate

log = logging.getLogger(__name__)

CALIBRATION_VERSION = 1
CALIBRATION_FILE = "calibration.json"

# UI screens loop mode needs.  "end_screen" tells us a match is over;
# the rest are the clicks that get us back into one.
TEMPLATE_END_SCREEN = "end_screen"
TEMPLATE_PLAY_AGAIN = "play_again"
TEMPLATE_MAP_TILE = "map_tile"
TEMPLATE_PLAY_BUTTON = "play_button"
TEMPLATE_IN_MATCH = "in_match"

LOOP_TEMPLATES = (
    TEMPLATE_END_SCREEN,
    TEMPLATE_PLAY_AGAIN,
    TEMPLATE_MAP_TILE,
    TEMPLATE_PLAY_BUTTON,
)


class CalibrationError(Exception):
    """Raised when stored calibration cannot be read."""


@dataclass
class HudRegion:
    """One readout: where it is, and the glyphs it uses."""

    box: NormBox | None = None
    invert: bool | None = None
    classifier: DigitClassifier = field(default_factory=DigitClassifier)

    @property
    def is_ready(self) -> bool:
        return self.box is not None and self.box.is_valid() and self.classifier.is_trained

    def status(self) -> str:
        if self.box is None:
            return "region not set"
        if not self.classifier.is_trained:
            return "region set, glyphs not labelled"
        return f"ready ({len(self.classifier.labels)} glyphs)"


@dataclass
class Calibration:
    """Everything the vision layer learned during setup."""

    wave: HudRegion = field(default_factory=HudRegion)
    cash: HudRegion = field(default_factory=HudRegion)
    templates: dict[str, ScreenTemplate] = field(default_factory=dict)
    client_size: tuple[int, int] = (0, 0)
    calibrated_utc: str = ""
    version: int = CALIBRATION_VERSION

    # -- readiness ------------------------------------------------------------
    @property
    def hud_ready(self) -> bool:
        return self.wave.is_ready and self.cash.is_ready

    def missing_for_playback(self) -> list[str]:
        """What still needs calibrating before a strategy can gate on the HUD."""
        gaps: list[str] = []
        if not self.wave.is_ready:
            gaps.append(f"wave readout ({self.wave.status()})")
        if not self.cash.is_ready:
            gaps.append(f"cash readout ({self.cash.status()})")
        return gaps

    def missing_for_loop(self) -> list[str]:
        return [name for name in LOOP_TEMPLATES if name not in self.templates]

    def describe(self) -> str:
        if not self.hud_ready:
            return "not calibrated"
        width, height = self.client_size
        size = f" at {width}x{height}" if width else ""
        return f"calibrated{size}, {len(self.templates)} screen template(s)"

    # -- persistence ----------------------------------------------------------
    def save(self, folder: Path) -> None:
        folder.mkdir(parents=True, exist_ok=True)
        payload = {
            "version": CALIBRATION_VERSION,
            "calibrated_utc": self.calibrated_utc
            or datetime.now(UTC).isoformat(timespec="seconds"),
            "client_size": list(self.client_size),
            "regions": {
                name: {
                    "box": region.box.to_dict() if region.box else None,
                    "invert": region.invert,
                    "labels": region.classifier.labels,
                    "min_confidence": region.classifier.min_confidence,
                }
                for name, region in (("wave", self.wave), ("cash", self.cash))
            },
            "templates": {
                name: {
                    "source_box": template.source_box.to_dict(),
                    "search_box": template.search_box.to_dict()
                    if template.search_box
                    else None,
                    "threshold": template.threshold,
                }
                for name, template in self.templates.items()
            },
        }
        (folder / CALIBRATION_FILE).write_text(json.dumps(payload, indent=2), encoding="utf-8")

        for name, region in (("wave", self.wave), ("cash", self.cash)):
            arrays: dict[str, np.ndarray] = {}
            for label, examples in region.classifier.templates.items():
                for index, example in enumerate(examples):
                    arrays[f"{label}\x1f{index}"] = example
            np.savez_compressed(folder / f"digits_{name}.npz", **arrays)

        template_dir = folder / "templates"
        template_dir.mkdir(exist_ok=True)
        for name, template in self.templates.items():
            np.save(template_dir / f"{name}.npy", template.pixels)

    @classmethod
    def load(cls, folder: Path) -> Calibration:
        path = folder / CALIBRATION_FILE
        if not path.exists():
            return cls()
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise CalibrationError(f"could not read {path.name}: {exc}") from exc

        version = int(payload.get("version", CALIBRATION_VERSION))
        if version > CALIBRATION_VERSION:
            raise CalibrationError(
                f"calibration was saved by a newer version of TDS Macro (format {version})"
            )

        calibration = cls(
            client_size=tuple(payload.get("client_size", [0, 0]))[:2] or (0, 0),  # type: ignore[arg-type]
            calibrated_utc=str(payload.get("calibrated_utc", "")),
            version=version,
        )
        regions = payload.get("regions", {})
        for name, region in (("wave", calibration.wave), ("cash", calibration.cash)):
            stored = regions.get(name) or {}
            box = stored.get("box")
            region.box = NormBox.from_dict(box) if isinstance(box, dict) else None
            region.invert = stored.get("invert")
            region.classifier = _load_classifier(
                folder / f"digits_{name}.npz", float(stored.get("min_confidence", 0.72))
            )

        template_dir = folder / "templates"
        for name, stored in (payload.get("templates") or {}).items():
            pixels_path = template_dir / f"{name}.npy"
            if not pixels_path.exists():
                log.warning("template %s is listed but its pixels are missing", name)
                continue
            search = stored.get("search_box")
            calibration.templates[name] = ScreenTemplate(
                name=name,
                pixels=np.load(pixels_path),
                source_box=NormBox.from_dict(stored["source_box"]),
                search_box=NormBox.from_dict(search) if isinstance(search, dict) else None,
                threshold=float(stored.get("threshold", DEFAULT_THRESHOLD)),
            )
        return calibration


def _load_classifier(path: Path, min_confidence: float) -> DigitClassifier:
    classifier = DigitClassifier(min_confidence=min_confidence)
    if not path.exists():
        return classifier
    try:
        with np.load(path) as data:
            for key in data.files:
                label = key.split("\x1f", 1)[0]
                classifier.templates.setdefault(label, []).append(
                    np.asarray(data[key], dtype=np.float32)
                )
    except (OSError, ValueError) as exc:
        log.error("could not read glyph templates from %s: %s", path, exc)
    return classifier
