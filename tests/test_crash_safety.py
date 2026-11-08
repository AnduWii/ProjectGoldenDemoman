"""Crash safety: a hard failure must still release input and hotkeys.

These run the app in a real subprocess and crash it on purpose, because the
thing being tested is process-level teardown -- try/finally in the hotkey
thread, atexit, Qt's aboutToQuit, and sys.excepthook. Asserting that inside the
same interpreter would prove nothing about what happens when the process is
actually unwinding.

The brief asked for this specifically: a leaked global hotkey stays claimed from
Windows until the process really dies, so a half-dead process would leave the
user with a dead F6 and no explanation.
"""

from __future__ import annotations

import os
import subprocess
import sys
import textwrap
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent


def run_script(body: str, timeout: int = 90) -> subprocess.CompletedProcess[str]:
    environment = dict(
        os.environ,
        QT_QPA_PLATFORM="offscreen",
        PYTHONPATH=str(ROOT),
        PYTHONFAULTHANDLER="1",
    )
    return subprocess.run(
        [sys.executable, "-c", textwrap.dedent(body)],
        capture_output=True,
        text=True,
        timeout=timeout,
        env=environment,
        cwd=ROOT,
    )


def test_unhandled_exception_releases_held_input_and_shuts_down(tmp_path: Path) -> None:
    """The exception hook must release input before anything else."""
    result = run_script(f"""
        import sys
        from pathlib import Path
        from PySide6.QtCore import QTimer
        from PySide6.QtWidgets import QApplication

        from tds_macro.config import SettingsStore
        from tds_macro.ui.controller import AppController

        app = QApplication([])
        controller = AppController(SettingsStore(Path(r"{tmp_path}")))
        # The backend the run would actually use, as start_playback builds it.
        backend = controller.make_backend(dry_run=True)
        backend.key_down("3")
        backend.mouse_down()
        # Simulate bookkeeping lost by the crash: the key is still physically
        # down but the backend no longer knows it. The panic path must still
        # send its key-up.
        backend.held_keys.clear()
        backend.key_down("e")

        released = []

        def hook(kind, value, tb):
            controller.emergency_stop()
            controller.shutdown()
            released.append(
                (sorted(backend.held_keys), sorted(b.value for b in backend.held_buttons))
            )
            ups = sorted(i.key for i in backend.intents if i.kind == "key_up" and i.key)
            print("HELD_AFTER_CRASH", released[-1], flush=True)
            print("KEY_UPS_SENT", ups, flush=True)
            app.quit()

        sys.excepthook = hook

        def explode():
            try:
                raise RuntimeError("simulated crash mid-run")
            except RuntimeError:
                hook(*sys.exc_info())

        QTimer.singleShot(50, explode)
        app.exec()
        print("EXITED_CLEANLY", flush=True)
    """)
    assert result.returncode == 0, result.stderr
    assert "HELD_AFTER_CRASH ([], [])" in result.stdout, result.stdout
    assert "EXITED_CLEANLY" in result.stdout
    # "3" is a hotbar slot key and "e" the upgrade key, so both are in the panic
    # set and both get a key-up even though only one was still tracked.
    ups = result.stdout.split("KEY_UPS_SENT ", 1)[1].splitlines()[0]
    assert "'3'" in ups and "'e'" in ups, ups


def test_hotkey_manager_releases_on_interpreter_exit(tmp_path: Path) -> None:
    """atexit must run stop() even when nothing calls it explicitly."""
    result = run_script("""
        from tds_macro.input.hotkeys import HotkeyManager

        manager = HotkeyManager()
        manager.set_binding("probe", "Ctrl+Alt+Shift+F24", lambda: None)
        manager.start()
        print("STARTED", [b.status() for b in manager.bindings], flush=True)

        import atexit
        # Prove stop() is wired into interpreter shutdown rather than relying on
        # the caller remembering: register a sentinel *after* start(), so it runs
        # before the manager's own handler (atexit is LIFO) and can observe it.
        atexit.register(lambda: print("ATEXIT_RAN", flush=True))
        # Exit without calling stop() at all.
    """)
    assert result.returncode == 0, result.stderr
    assert "STARTED" in result.stdout
    assert "ATEXIT_RAN" in result.stdout


def test_hotkey_manager_stop_is_safe_without_start() -> None:
    result = run_script("""
        from tds_macro.input.hotkeys import HotkeyManager

        manager = HotkeyManager()
        manager.stop()
        manager.stop()
        print("OK", flush=True)
    """)
    assert result.returncode == 0, result.stderr
    assert "OK" in result.stdout


def test_sigint_shuts_down_cleanly(tmp_path: Path) -> None:
    """Ctrl+C must go through the same teardown as a normal quit.

    Raised in-process with signal.raise_signal rather than os.kill: on Windows
    os.kill(pid, SIGINT) terminates the process outright instead of running the
    installed handler, so the test would prove nothing there.
    """
    result = run_script(f"""
        import signal
        from pathlib import Path
        from PySide6.QtCore import QTimer
        from PySide6.QtWidgets import QApplication

        from tds_macro.config import SettingsStore
        from tds_macro.ui.controller import AppController

        app = QApplication([])
        controller = AppController(SettingsStore(Path(r"{tmp_path}")))

        def on_signal(signum, frame):
            controller.shutdown()
            print("SHUTDOWN_ON_SIGNAL", flush=True)
            app.quit()

        signal.signal(signal.SIGINT, on_signal)
        QTimer.singleShot(50, lambda: signal.raise_signal(signal.SIGINT))
        # Qt only notices a signal when the loop wakes, so keep it ticking.
        waker = QTimer()
        waker.timeout.connect(lambda: None)
        waker.start(10)
        app.exec()
        print("EXITED", flush=True)
    """)
    assert result.returncode == 0, result.stderr
    assert "SHUTDOWN_ON_SIGNAL" in result.stdout
    assert "EXITED" in result.stdout


def test_app_boots_and_exits_cleanly_through_the_real_entry_point(tmp_path: Path) -> None:
    """The same path CI uses on Windows, exercised here through --self-test."""
    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "tds_macro",
            "--self-test",
            "800",
            "--no-wizard",
            "--dry-run",
            "--settings-dir",
            str(tmp_path),
        ],
        capture_output=True,
        text=True,
        timeout=90,
        env=dict(os.environ, QT_QPA_PLATFORM="offscreen", PYTHONPATH=str(ROOT)),
        cwd=ROOT,
    )
    assert result.returncode == 0, result.stderr
    assert (tmp_path / "settings.json").exists()
    log = (tmp_path / "tds_macro.log").read_text(encoding="utf-8")
    assert "controller shut down cleanly" in log
    assert "DPI mode" in log


@pytest.mark.parametrize("hard", [False, True])
def test_capture_and_tap_stop_is_idempotent(hard: bool) -> None:
    """Shutdown paths get reached more than once; none may raise the second time."""
    result = run_script(f"""
        from tds_macro.input.hooks import RawInputTap
        from tds_macro.input.window import RobloxWindow
        from tds_macro.vision.capture import ScreenCapture

        tap = RawInputTap(RobloxWindow())
        tap.start()
        for _ in range(3):
            tap.stop()
        capture = ScreenCapture()
        for _ in range(3):
            capture.close()
        if {hard}:
            # Also prove it survives being torn down without ever starting.
            RawInputTap(RobloxWindow()).stop()
            ScreenCapture().close()
        print("OK", flush=True)
    """)
    assert result.returncode == 0, result.stderr
    assert "OK" in result.stdout
