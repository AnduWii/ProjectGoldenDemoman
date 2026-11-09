"""Portable settings: keybinds, timing, hotkeys, calibration, paths.

Everything lives in one JSON file beside the executable.  No registry keys, no
services, no startup entries, nothing written outside the app's own folder --
and if that folder turns out to be read-only (the user dropped the app in
``C:\\Program Files``), settings fall back to ``%LOCALAPPDATA%`` rather than
failing to save.
"""

from __future__ import annotations

import contextlib
import json
import logging
import os
import sys
import tempfile
from dataclasses import asdict, dataclass, field, fields
from pathlib import Path
from typing import Any

from .input.coords import NormPoint

log = logging.getLogger(__name__)

# 2: default_timeout_ms 180 s -> 0 (wait forever); pinned_hwnd no longer saved.
SETTINGS_VERSION = 2
SETTINGS_FILENAME = "settings.json"
APP_FOLDER_NAME = "TDSMacro"


# --- keybinds -----------------------------------------------------------------
@dataclass
class ActionBinding:
    """How to trigger one in-game action once a tower is selected.

    TDS exposes upgrade/sell as buttons in the tower menu rather than as keys on
    every build, so an action is either a key press or a click on a calibrated
    menu position.  Nothing about the game's own bindings is assumed: both the
    key and the point come from settings.
    """

    mode: str = "key"  # "key" or "click"
    key: str = ""
    point: NormPoint | None = None

    def is_configured(self) -> bool:
        return (self.mode == "key" and bool(self.key)) or (
            self.mode == "click" and self.point is not None
        )

    def describe(self) -> str:
        if self.mode == "key":
            return f"press {self.key.upper()}" if self.key else "not configured"
        if self.point is None:
            return "not configured"
        return f"click ({self.point.x:.3f}, {self.point.y:.3f})"

    def to_dict(self) -> dict[str, Any]:
        data: dict[str, Any] = {"mode": self.mode, "key": self.key}
        if self.point is not None:
            data["point"] = self.point.to_dict()
        return data

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> ActionBinding:
        point = data.get("point")
        return cls(
            mode=str(data.get("mode", "key")),
            key=str(data.get("key", "")),
            point=NormPoint.from_dict(point) if isinstance(point, dict) else None,
        )


@dataclass
class Keybinds:
    """In-game bindings.  All user-editable; none of these are TDS constants.

    The defaults are only a starting guess shown in the settings dialog -- the
    first-run wizard asks the user to confirm each one against their own TDS
    keybind screen, because guessing wrong here produces a macro that looks
    like it is running but does nothing.
    """

    slots: list[str] = field(default_factory=lambda: ["1", "2", "3", "4", "5"])
    upgrade: ActionBinding = field(default_factory=lambda: ActionBinding("key", "e"))
    sell: ActionBinding = field(default_factory=lambda: ActionBinding("key", "x"))
    ability: ActionBinding = field(default_factory=lambda: ActionBinding("key", "f"))
    skip_wave: ActionBinding = field(default_factory=lambda: ActionBinding("key", "g"))

    def slot_key(self, slot: int) -> str | None:
        """Key for hotbar ``slot`` (1-based), or ``None`` if unbound."""
        if 1 <= slot <= len(self.slots):
            key = self.slots[slot - 1]
            return key or None
        return None

    def to_dict(self) -> dict[str, Any]:
        return {
            "slots": list(self.slots),
            "upgrade": self.upgrade.to_dict(),
            "sell": self.sell.to_dict(),
            "ability": self.ability.to_dict(),
            "skip_wave": self.skip_wave.to_dict(),
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> Keybinds:
        default = cls()
        slots = data.get("slots")

        def binding(name: str) -> ActionBinding:
            raw = data.get(name)
            if isinstance(raw, dict):
                return ActionBinding.from_dict(raw)
            return getattr(default, name)

        return cls(
            slots=[str(s) for s in slots] if isinstance(slots, list) and slots else default.slots,
            upgrade=binding("upgrade"),
            sell=binding("sell"),
            ability=binding("ability"),
            skip_wave=binding("skip_wave"),
        )


# --- timing -------------------------------------------------------------------
@dataclass
class Timing:
    """Delays used during playback, in milliseconds.

    Jitter exists for reliability, not concealment: a game that is mid-frame
    when two events arrive in the same millisecond will drop one, and identical
    back-to-back timings make that failure reproducible rather than rare.
    """

    action_delay_ms: int = 120
    jitter_ms: int = 40
    key_hold_ms: int = 35
    click_hold_ms: int = 45
    move_settle_ms: int = 60
    menu_open_ms: int = 220
    place_confirm_ms: int = 450
    # How long to keep watching for the cash drop after that initial pause.
    # The HUD reader needs a few frames to publish a new value, so a single
    # read taken the instant the pause ends can still show the old figure --
    # and reading "cash unchanged" means retrying, which places a second tower.
    place_verify_ms: int = 1_500
    place_retries: int = 2
    poll_interval_ms: int = 100
    # 0 = wait forever. A TDS wave gate is routinely ten-plus minutes away, so
    # any fixed default here aborts real strategies mid-match; the live status
    # line ("Waiting for wave 12 - detected wave 11") shows a stalled read.
    default_timeout_ms: int = 0

    def clamped(self) -> Timing:
        """Keep values sane; a negative or absurd delay is a footgun, not a feature."""
        return Timing(
            action_delay_ms=max(0, min(self.action_delay_ms, 10_000)),
            jitter_ms=max(0, min(self.jitter_ms, 5_000)),
            key_hold_ms=max(1, min(self.key_hold_ms, 2_000)),
            click_hold_ms=max(1, min(self.click_hold_ms, 2_000)),
            move_settle_ms=max(0, min(self.move_settle_ms, 5_000)),
            menu_open_ms=max(0, min(self.menu_open_ms, 10_000)),
            place_confirm_ms=max(0, min(self.place_confirm_ms, 30_000)),
            # Never 0: an AwaitState timeout of 0 means "no deadline", so a
            # zero window would make a failed placement wait forever instead
            # of retrying.
            place_verify_ms=max(250, min(self.place_verify_ms, 30_000)),
            place_retries=max(0, min(self.place_retries, 10)),
            poll_interval_ms=max(10, min(self.poll_interval_ms, 5_000)),
            default_timeout_ms=max(0, self.default_timeout_ms),
        )


# On a Mac keyboard F6-F8 are media keys (keyboard brightness, previous track,
# play/pause) unless the user changes a system setting, so the Mac defaults use
# a modifier combination instead.
DEFAULT_HOTKEYS = {
    "windows": {"record": "F6", "play": "F7", "stop": "F8"},
    "mac": {"record": "Ctrl+Option+R", "play": "Ctrl+Option+P", "stop": "Ctrl+Option+S"},
}


def default_hotkey(action: str, platform: str | None = None) -> str:
    family = "mac" if (platform or sys.platform) == "darwin" else "windows"
    return DEFAULT_HOTKEYS[family][action]


@dataclass
class Hotkeys:
    """Global hotkeys. Defaults depend on the platform; all are user-editable."""

    record: str = field(default_factory=lambda: default_hotkey("record"))
    play: str = field(default_factory=lambda: default_hotkey("play"))
    stop: str = field(default_factory=lambda: default_hotkey("stop"))

    def as_map(self) -> dict[str, str]:
        return {"record": self.record, "play": self.play, "stop": self.stop}


@dataclass
class LoopSettings:
    """Between-match behaviour for loop mode."""

    enabled: bool = False
    runs_target: int = 0  # 0 means "forever, until stopped"
    use_play_again: bool = True
    # How long to wait for each lobby screen (Play Again, map, Play).
    requeue_timeout_ms: int = 120_000
    # How long a match may run *after the strategy's last step* before the loop
    # gives up waiting for the end screen. Separate from the lobby timeout: the
    # strategy usually finishes long before the map does, and reusing the 2 min
    # lobby limit here failed the loop on nearly every real match. 0 = forever.
    match_end_timeout_ms: int = 3_600_000
    between_match_delay_ms: int = 2_000


@dataclass
class CalibrationPaths:
    """Where the vision data lives.  Relative to the settings folder."""

    folder: str = "calibration"

    def resolve(self, base: Path) -> Path:
        return base / self.folder


@dataclass
class Settings:
    """Everything the app persists."""

    version: int = SETTINGS_VERSION
    hotkeys: Hotkeys = field(default_factory=Hotkeys)
    keybinds: Keybinds = field(default_factory=Keybinds)
    timing: Timing = field(default_factory=Timing)
    loop: LoopSettings = field(default_factory=LoopSettings)
    calibration: CalibrationPaths = field(default_factory=CalibrationPaths)
    dry_run: bool = False
    require_foreground: bool = True
    use_scancodes: bool = True
    setup_complete: bool = False
    last_strategy_path: str = ""
    # No pinned window handle is saved: Windows reuses HWND values, so a handle
    # from a previous session can belong to Discord or a browser today.
    log_to_file: bool = True
    theme: str = "dark"
    # "simple" (TinyTask-style home screen) or "advanced" (the step editor).
    ui_mode: str = "simple"

    def to_dict(self) -> dict[str, Any]:
        return {
            "version": SETTINGS_VERSION,
            "hotkeys": asdict(self.hotkeys),
            "keybinds": self.keybinds.to_dict(),
            "timing": asdict(self.timing),
            "loop": asdict(self.loop),
            "calibration": asdict(self.calibration),
            "dry_run": self.dry_run,
            "require_foreground": self.require_foreground,
            "use_scancodes": self.use_scancodes,
            "setup_complete": self.setup_complete,
            "last_strategy_path": self.last_strategy_path,
            "log_to_file": self.log_to_file,
            "theme": self.theme,
            "ui_mode": self.ui_mode,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> Settings:
        """Rebuild from JSON, ignoring unknown keys and keeping defaults for gaps.

        Deliberately forgiving: a settings file from a slightly different build
        should degrade to defaults for the parts it does not understand, never
        prevent the app from starting.
        """
        settings = cls()
        settings.hotkeys = _load_dataclass(Hotkeys, data.get("hotkeys"))
        settings.timing = _load_dataclass(Timing, data.get("timing")).clamped()
        settings.loop = _load_dataclass(LoopSettings, data.get("loop"))
        settings.calibration = _load_dataclass(CalibrationPaths, data.get("calibration"))
        if isinstance(data.get("keybinds"), dict):
            settings.keybinds = Keybinds.from_dict(data["keybinds"])
        for name in (
            "dry_run",
            "require_foreground",
            "use_scancodes",
            "setup_complete",
            "log_to_file",
        ):
            if isinstance(data.get(name), bool):
                setattr(settings, name, data[name])
        if isinstance(data.get("last_strategy_path"), str):
            settings.last_strategy_path = data["last_strategy_path"]
        if isinstance(data.get("theme"), str):
            settings.theme = data["theme"]
        if data.get("ui_mode") in ("simple", "advanced"):
            settings.ui_mode = data["ui_mode"]
        _migrate(settings, data)
        return settings


# The default that shipped in settings version 1 and aborted long wave gates.
_V1_DEFAULT_TIMEOUT_MS = 180_000


def _migrate(settings: Settings, data: dict[str, Any]) -> None:
    """Upgrade values saved by older builds.

    Only values still equal to an old *default* are changed: a number the user
    chose deliberately is theirs to keep.
    """
    version = data.get("version", 1)
    is_v1 = not isinstance(version, int) or version < 2
    if is_v1 and settings.timing.default_timeout_ms == _V1_DEFAULT_TIMEOUT_MS:
        settings.timing.default_timeout_ms = 0


def _load_dataclass(cls: type, data: Any) -> Any:
    """Build a flat dataclass from a dict, dropping unknown keys and bad types.

    Values are coerced to the type of the field's default, so a hand-edited
    settings file with ``"place_retries": "2"`` loads instead of crashing the
    app at the first placement.
    """
    instance = cls()
    if not isinstance(data, dict):
        return instance
    for spec in fields(cls):
        if spec.name not in data:
            continue
        value = data[spec.name]
        expected = type(getattr(instance, spec.name))
        if isinstance(value, expected) and not (expected is int and isinstance(value, bool)):
            setattr(instance, spec.name, value)
            continue
        try:
            setattr(instance, spec.name, expected(value))
        except (TypeError, ValueError):
            log.warning(
                "ignoring settings value %s=%r (expected %s)", spec.name, value, expected.__name__
            )
    return instance


# --- storage location ----------------------------------------------------------
def _is_writable(folder: Path) -> bool:
    try:
        folder.mkdir(parents=True, exist_ok=True)
        with tempfile.NamedTemporaryFile(dir=folder, prefix=".wtest", delete=True):
            return True
    except OSError:
        return False


def app_dir(executable: str | None = None, frozen: bool | None = None) -> Path:
    """Folder the app is running from: beside the exe, or beside the .app.

    On macOS the executable lives at TDSMacro.app/Contents/MacOS/TDSMacro.
    Settings go beside the .app, never inside it: writing into the bundle
    invalidates its code signature, after which macOS can refuse to open it
    and forgets the privacy permissions the user granted.
    """
    is_frozen = getattr(sys, "frozen", False) if frozen is None else frozen
    if not is_frozen:
        return Path(__file__).resolve().parent.parent
    exe = Path(executable or sys.executable).resolve()
    for parent in exe.parents:
        if parent.suffix == ".app":
            return parent.parent
    return exe.parent


def fallback_dir(platform: str | None = None) -> Path:
    """Where settings go when the app's own folder is read-only.

    On a Mac that includes every launch straight from Downloads: macOS runs a
    quarantined app from a randomized read-only copy ("app translocation").
    """
    if (platform or sys.platform) == "darwin":
        return Path.home() / "Library" / "Application Support" / APP_FOLDER_NAME
    base = os.environ.get("LOCALAPPDATA") or os.environ.get("XDG_CONFIG_HOME")
    if base:
        return Path(base) / APP_FOLDER_NAME
    return Path.home() / f".{APP_FOLDER_NAME.lower()}"


def resolve_config_dir(prefer: Path | None = None) -> tuple[Path, bool]:
    """Pick where settings live.

    Returns ``(folder, is_portable)``.  Portable (next to the exe) is preferred
    so the whole app can be moved or shared on a USB stick; the fallback only
    kicks in when that folder cannot be written to.
    """
    candidate = prefer or app_dir()
    if _is_writable(candidate):
        return candidate, True
    fallback = fallback_dir()
    log.warning("%s is not writable; storing settings in %s", candidate, fallback)
    return fallback, False


class SettingsStore:
    """Loads and saves :class:`Settings`, atomically."""

    def __init__(self, folder: Path | None = None) -> None:
        self.folder, self.portable = resolve_config_dir(folder)
        self.path = self.folder / SETTINGS_FILENAME
        self.load_error: str = ""

    def load(self) -> Settings:
        if not self.path.exists():
            return Settings()
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            # A corrupt settings file must not brick the app: keep the bad file
            # for inspection and carry on with defaults.
            self.load_error = f"could not read {self.path.name}: {exc}"
            log.error("%s", self.load_error)
            self._quarantine()
            return Settings()
        if not isinstance(data, dict):
            self.load_error = f"{self.path.name} does not contain a settings object"
            self._quarantine()
            return Settings()
        return Settings.from_dict(data)

    def save(self, settings: Settings) -> bool:
        """Write via a temp file + replace, so a crash mid-write cannot truncate."""
        try:
            self.folder.mkdir(parents=True, exist_ok=True)
            temp = self.path.with_suffix(".json.tmp")
            temp.write_text(json.dumps(settings.to_dict(), indent=2), encoding="utf-8")
            temp.replace(self.path)
            return True
        except OSError as exc:
            log.error("could not save settings to %s: %s", self.path, exc)
            self.load_error = f"could not save settings: {exc}"
            return False

    def calibration_dir(self, settings: Settings) -> Path:
        return settings.calibration.resolve(self.folder)

    def strategies_dir(self) -> Path:
        return self.folder / "strategies"

    def _quarantine(self) -> None:
        """Move a bad settings file aside so the user can still inspect it."""
        with contextlib.suppress(OSError):
            self.path.replace(self.path.with_suffix(".json.broken"))
