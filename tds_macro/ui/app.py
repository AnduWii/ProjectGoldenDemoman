"""Application entry point.

Order matters here: DPI awareness must be set before Qt is instantiated, and the
shutdown path must run even on a crash, because a leaked global hotkey stays
claimed from Windows until the process really dies -- leaving the user with a
dead F6 and no explanation.
"""

from __future__ import annotations

import argparse
import atexit
import contextlib
import faulthandler
import logging
import signal
import sys
import traceback
from pathlib import Path
from types import TracebackType
from typing import TextIO

from ..config import SettingsStore
from ..input.window import set_dpi_awareness

log = logging.getLogger("tds_macro")

# Kept open for the life of the process: faulthandler writes to the raw file
# descriptor when the interpreter is already crashing, so it must not be closed
# or garbage collected.
_crash_log: TextIO | None = None


def configure_logging(folder: Path, to_file: bool, verbose: bool) -> None:
    level = logging.DEBUG if verbose else logging.INFO
    handlers: list[logging.Handler] = []
    # The windowed exe has no console: PyInstaller sets sys.stderr to None, and
    # a StreamHandler pointed at None fails on every log call.
    if sys.stderr is not None:
        handlers.append(logging.StreamHandler(sys.stderr))
    if to_file:
        try:
            folder.mkdir(parents=True, exist_ok=True)
            handlers.append(logging.FileHandler(folder / "tds_macro.log", encoding="utf-8"))
        except OSError as exc:  # pragma: no cover - depends on the filesystem
            if sys.stderr is not None:
                print(f"could not open log file: {exc}", file=sys.stderr)
    if not handlers:
        handlers.append(logging.NullHandler())
    logging.basicConfig(
        level=level,
        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
        handlers=handlers,
        force=True,
    )


def enable_crash_reports(folder: Path) -> None:
    """Turn on faulthandler without assuming a console exists.

    ``faulthandler.enable()`` writes to sys.stderr by default, and in the
    windowed exe sys.stderr is None -- the call raises RuntimeError and the app
    dies before showing a window. Native crashes go to crash.log beside the
    settings instead, which is also where a user can find them.
    """
    global _crash_log
    if sys.stderr is not None:
        faulthandler.enable()
        return
    try:
        folder.mkdir(parents=True, exist_ok=True)
        _crash_log = open(folder / "crash.log", "a", encoding="utf-8")  # noqa: SIM115
        faulthandler.enable(file=_crash_log)
    except (OSError, RuntimeError) as exc:  # pragma: no cover - filesystem dependent
        log.warning("crash reports disabled: %s", exc)


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="tds-macro",
        description="Record and replay Tower Defense Simulator strategies (external input only).",
    )
    parser.add_argument(
        "strategy", nargs="?", type=Path, help="strategy .json file to open on startup"
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="force dry-run mode: log every action without sending any input",
    )
    parser.add_argument(
        "--settings-dir",
        type=Path,
        help="folder for settings.json (defaults to the folder containing the app)",
    )
    parser.add_argument("--verbose", action="store_true", help="debug logging")
    parser.add_argument(
        "--no-wizard",
        action="store_true",
        help="do not open any setup window on start (the macOS permissions prompt)",
    )
    parser.add_argument(
        "--self-test",
        type=int,
        default=0,
        metavar="MS",
        help=(
            "build the whole UI, run the event loop for MS milliseconds, then exit "
            "cleanly. Used by CI to prove the app starts; also handy for checking a "
            "build without touching Roblox."
        ),
    )
    return parser.parse_args(argv)


def _teardown(app: object, window: object, controller: object) -> None:
    """Destroy Qt objects deterministically, before the QApplication goes away.

    PySide6 aborts with an access violation if a QObject is finalized *after*
    the QApplication has been destroyed. That is exactly what happened here: the
    atexit handler and the exception hook both hold a reference to the
    controller, which is a QObject owning two QTimers, so Python freed it during
    interpreter shutdown -- long after Qt had torn itself down. The app had
    already done everything correctly and saved its settings; it simply crashed
    on the way out, which on Windows means a "has stopped working" dialog.

    So: shut down, drop the references that would outlive Qt, and delete the
    widgets while the event loop can still process the deletions.
    """
    controller.shutdown()  # type: ignore[attr-defined]
    atexit.unregister(controller.shutdown)  # type: ignore[attr-defined]
    sys.excepthook = sys.__excepthook__
    with contextlib.suppress(RuntimeError):
        window.hide()  # type: ignore[attr-defined]
        window.setParent(None)  # type: ignore[attr-defined]
        window.deleteLater()  # type: ignore[attr-defined]
        controller.deleteLater()  # type: ignore[attr-defined]
        app.processEvents()  # type: ignore[attr-defined]


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)

    # Before Qt: Qt otherwise sets its own, lower, DPI awareness first, and then
    # GetClientRect returns scaled logical pixels while mss captures physical
    # ones -- silently offsetting every click on a display above 100% scaling.
    dpi_mode = set_dpi_awareness()

    store = SettingsStore(args.settings_dir)
    settings = store.load()
    configure_logging(store.folder, settings.log_to_file, args.verbose)
    log.info("TDS Macro starting; DPI mode: %s", dpi_mode)
    enable_crash_reports(store.folder)

    from PySide6.QtWidgets import QApplication

    from .controller import AppController
    from .main_window import MainWindow
    from .theme import STYLESHEET

    app = QApplication(sys.argv[:1])
    app.setApplicationName("TDS Macro")
    app.setStyleSheet(STYLESHEET)

    controller = AppController(store)
    if args.dry_run:
        controller.settings.dry_run = True
        controller.emit_log("warn", "Dry run forced from the command line.")

    # Three independent paths to a clean shutdown: normal quit, interpreter
    # exit, and Ctrl+C. Any one of them releases hotkeys and held input.
    atexit.register(controller.shutdown)
    app.aboutToQuit.connect(controller.shutdown)

    def on_signal(signum: int, frame: object) -> None:
        del frame
        log.warning("signal %s received; shutting down", signum)
        controller.shutdown()
        app.quit()

    for sig in (signal.SIGINT, signal.SIGTERM):
        # Not installable from a non-main thread, and SIGTERM is absent on some
        # Windows builds; losing one handler is not worth failing startup over.
        with contextlib.suppress(ValueError, OSError):  # pragma: no cover
            signal.signal(sig, on_signal)

    def on_exception(
        kind: type[BaseException], value: BaseException, tb: TracebackType | None
    ) -> None:
        """Release input before anything else -- a crash mid-run must not leave
        a key held down or a hotkey claimed."""
        log.critical(
            "unhandled exception: %s", "".join(traceback.format_exception(kind, value, tb))
        )
        try:
            controller.emergency_stop()
        finally:
            controller.shutdown()
            sys.__excepthook__(kind, value, tb)

    sys.excepthook = on_exception

    window = MainWindow(controller)
    window.show()

    if args.self_test:
        from PySide6.QtCore import QTimer

        from ..vision.video import check_video_support

        ok, detail = check_video_support(store.folder)
        (log.info if ok else log.error)("video decoding: %s", detail)
        log.info("self-test: quitting in %d ms", args.self_test)
        QTimer.singleShot(args.self_test, app.quit)

    if args.strategy is not None:
        controller.load_strategy(args.strategy)
        window._reload_strategy()

    # No setup wizard on first run any more: the simple screen works straight
    # away, and the wizard is an optional extra (wave/cash reading, loop mode).
    # The one exception is a Mac without Accessibility, where nothing can reach
    # Roblox until the user switches it on -- so say so up front.
    if not args.no_wizard and not args.self_test:
        blocked = any(
            p.key == "accessibility" and not p.granted for p in controller.permissions()
        )
        if blocked:
            from .simple import PermissionsDialog

            PermissionsDialog(controller, window).exec()
            window.simple.refresh_permissions()

    code = app.exec()
    _teardown(app, window, controller)
    return code


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
