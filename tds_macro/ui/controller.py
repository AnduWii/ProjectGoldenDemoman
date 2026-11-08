"""Application controller: owns everything stateful, exposes Qt signals.

Deliberately the only place where the Win32 layer, the vision layer and the
engine meet, so the widgets stay dumb and the orchestration is testable.

Threading: hotkey callbacks arrive on the hotkey thread and are re-emitted as
Qt signals, which Qt delivers to the GUI thread through its queued-connection
path.  Playback itself runs on the GUI thread inside a short ``QTimer`` tick --
safe because no engine operation blocks, and it keeps input emission on one
thread, which SendInput prefers.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from datetime import datetime
from pathlib import Path

from PySide6.QtCore import QObject, QTimer, Signal

from ..config import Settings, SettingsStore
from ..input.coords import NormPoint, Rect
from ..input.hooks import RawInputTap
from ..input.hotkeys import HotkeyManager
from ..input.sendinput import InputBackend, MouseButton, NullBackend, Win32Backend
from ..input.window import RobloxWindow, query_dpi_awareness, set_dpi_awareness
from ..strategy.engine import GameState, RunnerDeps, RunStatus, StrategyRunner
from ..strategy.loop import LoopController, LoopDeps, LoopPhase
from ..strategy.recorder import Recorder, RecorderOptions
from ..strategy.steps import Strategy, StrategyError
from ..vision.calibration import Calibration, CalibrationError
from ..vision.capture import ScreenCapture
from ..vision.hud import HudReader, HudSnapshot
from ..vision.templates import MatchResult, match_template

log = logging.getLogger(__name__)

TICK_MS = 15
HUD_REFRESH_MS = 150


class AppController(QObject):
    """Single owner of app state; widgets talk to this and nothing else."""

    logged = Signal(str, str)  # level, message
    statusChanged = Signal(str)
    hudChanged = Signal(object)  # HudSnapshot
    runStateChanged = Signal()
    strategyChanged = Signal()
    hotkeyPressed = Signal(str)
    windowChanged = Signal(str)

    def __init__(self, store: SettingsStore | None = None) -> None:
        super().__init__()
        # main() sets this before Qt loads; calling again is a no-op that returns
        # the cached answer, and query_dpi_awareness() confirms it from Windows.
        self.dpi_mode = set_dpi_awareness()
        self.dpi_actual = query_dpi_awareness()
        self.store = store or SettingsStore()
        self.settings: Settings = self.store.load()
        self.strategy = Strategy()
        self.strategy_path: Path | None = None
        self.dirty = False

        self.window = RobloxWindow(self.settings.pinned_hwnd or None)
        self.capture = ScreenCapture()
        self.calibration = Calibration()
        self.hud = HudReader(
            self.window,
            self.capture,
            self.calibration,
            interval_ms=HUD_REFRESH_MS,
            detect_end_screen=False,
        )
        self.hotkeys = HotkeyManager()
        self.tap = RawInputTap(self.window)
        self.recorder: Recorder | None = None
        self.runner: StrategyRunner | None = None
        self.loop: LoopController | None = None
        self.backend: InputBackend | None = None
        self._shut_down = False

        self._timer = QTimer(self)
        self._timer.setInterval(TICK_MS)
        self._timer.timeout.connect(self._tick)
        self._hud_timer = QTimer(self)
        self._hud_timer.setInterval(HUD_REFRESH_MS)
        self._hud_timer.timeout.connect(self._refresh_hud)

        self.hotkeyPressed.connect(self._on_hotkey)
        self._load_calibration()
        if self.store.load_error:
            self.emit_log("error", self.store.load_error)
        self.emit_log("info", f"DPI mode: {self.dpi_mode} (process reports: {self.dpi_actual})")
        if not self.store.portable:
            self.emit_log(
                "warn",
                f"The app folder is not writable, so settings are stored in "
                f"{self.store.folder} instead.",
            )

    # -- logging ---------------------------------------------------------------
    def emit_log(self, level: str, message: str) -> None:
        stamp = datetime.now().strftime("%H:%M:%S")
        self.logged.emit(level, f"[{stamp}] {message}")
        severity = {
            "error": logging.ERROR,
            "warn": logging.WARNING,
        }.get(level, logging.INFO)
        log.log(severity, "%s", message)

    # -- calibration -----------------------------------------------------------
    @property
    def calibration_dir(self) -> Path:
        return self.store.calibration_dir(self.settings)

    def _load_calibration(self) -> None:
        try:
            self.calibration = Calibration.load(self.calibration_dir)
        except CalibrationError as exc:
            self.emit_log("error", f"Could not load calibration: {exc}")
            self.calibration = Calibration()
        self.hud.calibration = self.calibration

    def save_calibration(self) -> None:
        try:
            self.calibration.save(self.calibration_dir)
            self.hud.calibration = self.calibration
            self.hud.reset()
            self.emit_log("info", f"Calibration saved ({self.calibration.describe()}).")
        except OSError as exc:
            self.emit_log("error", f"Could not save calibration: {exc}")

    def find_template(self, name: str) -> MatchResult:
        """Look for a calibrated screen template in the live client area."""
        template = self.calibration.templates.get(name)
        if template is None:
            return MatchResult(False, 0.0)
        client, frame = self.hud.grab_client_frame()
        if client is None or frame is None:
            return MatchResult(False, 0.0)
        return match_template(frame, template)  # type: ignore[arg-type]

    # -- settings --------------------------------------------------------------
    def save_settings(self) -> None:
        if not self.store.save(self.settings):
            self.emit_log("error", "Settings could not be saved.")
            return
        self.emit_log("info", f"Settings saved to {self.store.path}.")

    def apply_hotkeys(self) -> list[str]:
        """(Re)register global hotkeys, reporting any that could not be claimed."""
        self.hotkeys.stop()
        self.hotkeys.bindings.clear()
        for name, spec in self.settings.hotkeys.as_map().items():
            self.hotkeys.set_binding(name, spec, self._make_hotkey_emitter(name))
        problems = self.hotkeys.start()
        for problem in problems:
            self.emit_log("warn", f"Hotkey problem: {problem}")
        for binding in self.hotkeys.active_bindings():
            self.emit_log("info", f"Hotkey ready -- {binding.status()}")
        return problems

    def _make_hotkey_emitter(self, name: str) -> Callable[[], None]:
        def emit() -> None:
            # Crosses from the hotkey thread to the GUI thread via Qt's queued
            # connection; never touch widgets from here.
            self.hotkeyPressed.emit(name)

        return emit

    def _on_hotkey(self, name: str) -> None:
        if name == "stop":
            self.emergency_stop()
        elif name == "play":
            self.toggle_playback()
        elif name == "record":
            self.toggle_recording()

    # -- window ----------------------------------------------------------------
    def refresh_window(self) -> bool:
        ok = self.window.refresh()
        self.windowChanged.emit(self.window.describe() if ok else self.window.last_error)
        return ok

    def client_rect(self) -> Rect | None:
        return self.window.client_rect()

    # -- backends ---------------------------------------------------------------
    def make_backend(self, dry_run: bool | None = None) -> InputBackend:
        """Build an input backend and remember it.

        The reference matters: the emergency stop has to release the backend the
        run is actually using, not a fresh one whose bookkeeping is empty.
        """
        dry = self.settings.dry_run if dry_run is None else dry_run
        if dry:
            backend: InputBackend = NullBackend(
                on_intent=lambda i: self.emit_log("step", f"would {i.describe()}")
            )
        else:
            backend = Win32Backend(
                self.window,
                use_scancodes=self.settings.use_scancodes,
                require_foreground=self.settings.require_foreground,
            )
        self.backend = backend
        return backend

    def panic_keys(self) -> list[str]:
        """Every key the app could conceivably be holding.

        Built from the configured keybinds rather than from what we think is
        held, because the panic path must work even when that bookkeeping was
        lost -- which is exactly the case after a crash.
        """
        keybinds = self.settings.keybinds
        keys = [key for key in keybinds.slots if key]
        for binding in (keybinds.upgrade, keybinds.sell, keybinds.ability, keybinds.skip_wave):
            if binding.mode == "key" and binding.key:
                keys.append(binding.key)
        return sorted(set(keys))

    def _make_runner(self, backend: InputBackend) -> StrategyRunner:
        return StrategyRunner(
            self.strategy,
            RunnerDeps(
                backend=backend,
                state=self.hud,
                focus=self.window,
                keybinds=self.settings.keybinds,
                timing=self.settings.timing,
                log=self.emit_log,
                require_foreground=self.settings.require_foreground,
            ),
        )

    # -- playback ---------------------------------------------------------------
    @property
    def busy(self) -> bool:
        if self.recorder is not None:
            return True
        if self.loop is not None and self.loop.phase.is_active:
            return True
        return self.runner is not None and self.runner.status.is_active

    def preflight(self) -> list[str]:
        """Everything that would stop a run, as plain sentences for the user."""
        problems: list[str] = []
        if not self.refresh_window():
            problems.append(self.window.last_error or "Roblox window not found.")
        problems.extend(f"Strategy: {p}" for p in self.strategy.validate())
        if not [s for s in self.strategy.steps if s.enabled]:
            problems.append("The strategy has no enabled steps.")
        if self._needs_hud() and not self.calibration.hud_ready:
            problems.append(
                "This strategy waits on wave or cash, but the HUD is not calibrated: "
                + ", ".join(self.calibration.missing_for_playback())
            )
        if self.settings.loop.enabled:
            missing = self.calibration.missing_for_loop()
            if missing:
                problems.append(
                    "Loop mode needs these screen templates captured: " + ", ".join(missing)
                )
        return problems

    def _needs_hud(self) -> bool:
        """Cash/wave gates and placement verification both depend on the HUD."""
        from ..strategy.steps import Place, WaitForCash, WaitForWave

        return any(
            isinstance(s, WaitForWave | WaitForCash | Place)
            for s in self.strategy.steps
            if s.enabled
        )

    def start_playback(self, dry_run: bool | None = None) -> bool:
        if self.busy:
            self.emit_log("warn", "Already running.")
            return False
        problems = self.preflight()
        if problems:
            for problem in problems:
                self.emit_log("error", problem)
            self.statusChanged.emit("Cannot start -- see the log")
            self.runStateChanged.emit()
            return False

        backend = self.make_backend(dry_run)
        self.hud.reset()
        self.hud.detect_end_screen = self.settings.loop.enabled
        if self.settings.loop.enabled:
            self.loop = LoopController(
                LoopDeps(
                    make_runner=lambda: self._make_runner(backend),
                    backend=backend,
                    state=self.hud,
                    focus=self.window,
                    find_template=self.find_template,
                    on_match_ended=self.hud.reset,
                    on_match_started=self.hud.reset,
                    settings=self.settings.loop,
                    timing=self.settings.timing,
                    log=self.emit_log,
                    require_foreground=self.settings.require_foreground,
                )
            )
            self.loop.start()
            self.runner = self.loop.runner
        else:
            self.loop = None
            self.runner = self._make_runner(backend)
            self.runner.start()
        self._timer.start()
        self._hud_timer.start()
        self.runStateChanged.emit()
        return True

    def toggle_playback(self) -> None:
        if self.busy:
            self.stop_playback("stopped from hotkey")
        else:
            self.start_playback()

    def stop_playback(self, reason: str = "stopped by user") -> None:
        if self.loop is not None:
            self.loop.stop(reason)
        elif self.runner is not None:
            self.runner.stop(reason)
        self._finish()

    def emergency_stop(self) -> None:
        """Stop everything, from anywhere, and release all held input."""
        self.emit_log("warn", "EMERGENCY STOP")
        if self.recorder is not None:
            self.stop_recording(save=False)
        self.stop_playback("emergency stop")
        try:
            backend = self.backend
            if backend is not None:
                backend.release_all()
                # Then send key-up for every configured key regardless of what
                # we believe is held: a crash can lose that bookkeeping while
                # the key is still down, and a redundant key-up costs nothing.
                backend.release_keys(self.panic_keys(), list(MouseButton))
        except Exception:  # pragma: no cover - defensive
            log.exception("releasing input failed during emergency stop")
        self.statusChanged.emit("Emergency stop -- all input released")

    def _finish(self) -> None:
        self._timer.stop()
        self.loop = None
        self.runStateChanged.emit()

    # -- recording --------------------------------------------------------------
    def start_recording(self) -> bool:
        if self.busy:
            self.emit_log("warn", "Stop the current run before recording.")
            return False
        if not self.refresh_window():
            self.emit_log("error", self.window.last_error or "Roblox window not found.")
            return False
        self.recorder = Recorder(
            self.settings.keybinds,
            RecorderOptions(),
            wave_provider=lambda: self.hud.read().wave,
        )
        error = self.tap.start()
        if error:
            self.recorder = None
            self.emit_log("error", f"Could not start recording: {error}")
            return False
        self._timer.start()
        self._hud_timer.start()
        self.emit_log("info", "Recording. Play normally; press the record hotkey again to stop.")
        self.statusChanged.emit("Recording -- play your strategy in Roblox")
        self.runStateChanged.emit()
        return True

    def stop_recording(self, save: bool = True) -> Strategy | None:
        recorder = self.recorder
        self.tap.stop()
        self.recorder = None
        self._timer.stop()
        self.runStateChanged.emit()
        if recorder is None:
            return None
        for event in self.tap.drain():
            recorder.feed(event)
        recorded = recorder.finish(name=self.strategy.name or "Recorded strategy")
        for warning in recorder.stats.warnings():
            self.emit_log("warn", warning)
        self.emit_log(
            "info",
            f"Recording stopped: {len(recorded.steps)} step(s) from "
            f"{recorder.stats.events_seen} input event(s).",
        )
        if save:
            self.strategy.steps = recorded.steps
            self.dirty = True
            self.strategyChanged.emit()
        return recorded

    def toggle_recording(self) -> None:
        if self.recorder is not None:
            self.stop_recording()
        else:
            self.start_recording()

    # -- tick -------------------------------------------------------------------
    def _tick(self) -> None:
        if self.recorder is not None:
            for event in self.tap.drain():
                self.recorder.feed(event)
            return
        if self.loop is not None:
            self.loop.tick()
            self.runner = self.loop.runner
            self.statusChanged.emit(self.loop.describe())
            if not self.loop.phase.is_active:
                phase = self.loop.phase
                self._finish()
                self.statusChanged.emit(
                    "Loop finished" if phase is LoopPhase.COMPLETED else phase.value.capitalize()
                )
            return
        runner = self.runner
        if runner is None:
            self._timer.stop()
            return
        runner.tick()
        self.statusChanged.emit(runner.describe_status())
        if not runner.status.is_active:
            self._finish()
            if runner.status is RunStatus.COMPLETED:
                self.statusChanged.emit("Finished")
            elif runner.status is RunStatus.FAILED:
                self.statusChanged.emit(f"Failed: {runner.failure_reason}")

    def _refresh_hud(self) -> None:
        snapshot: HudSnapshot = self.hud.read()
        self.hudChanged.emit(snapshot)

    def start_hud_monitor(self) -> None:
        self._hud_timer.start()

    def stop_hud_monitor(self) -> None:
        if not self.busy:
            self._hud_timer.stop()

    def current_state(self) -> GameState:
        return self.hud.last.to_game_state()

    # -- strategy files ----------------------------------------------------------
    def load_strategy(self, path: Path) -> bool:
        try:
            strategy = Strategy.from_json(Path(path).read_text(encoding="utf-8"))
        except (OSError, StrategyError) as exc:
            self.emit_log("error", f"Could not load {Path(path).name}: {exc}")
            return False
        self.strategy = strategy
        self.strategy_path = Path(path)
        self.dirty = False
        self.settings.last_strategy_path = str(path)
        problems = strategy.validate()
        self.emit_log(
            "info", f"Loaded '{strategy.name}' ({len(strategy.steps)} steps) from {path}"
        )
        for problem in problems:
            self.emit_log("warn", problem)
        self.strategyChanged.emit()
        return True

    def save_strategy(self, path: Path) -> bool:
        try:
            Path(path).parent.mkdir(parents=True, exist_ok=True)
            Path(path).write_text(self.strategy.to_json(), encoding="utf-8")
        except OSError as exc:
            self.emit_log("error", f"Could not save to {path}: {exc}")
            return False
        self.strategy_path = Path(path)
        self.dirty = False
        self.settings.last_strategy_path = str(path)
        self.emit_log("info", f"Saved '{self.strategy.name}' to {path}")
        return True

    def placement_points(self) -> list[tuple[str, NormPoint]]:
        from ..strategy.steps import Place

        return [
            (step.tower_id, step.position)
            for step in self.strategy.steps
            if isinstance(step, Place)
        ]

    # -- shutdown ----------------------------------------------------------------
    def shutdown(self) -> None:
        """Release every OS resource.

        Reached from several directions on purpose -- ``aboutToQuit``,
        ``atexit``, the signal handler and the exception hook -- so it has to be
        idempotent rather than run its teardown three times over.
        """
        if self._shut_down:
            return
        self._shut_down = True
        try:
            self.stop_playback("application closing")
        finally:
            self.tap.stop()
            self.hotkeys.stop()
            self.capture.close()
            self._timer.stop()
            self._hud_timer.stop()
            log.info("controller shut down cleanly")
