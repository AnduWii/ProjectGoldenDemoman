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

from ..config import SettingsStore
from ..input.window import set_dpi_awareness

log = logging.getLogger("tds_macro")


def configure_logging(folder: Path, to_file: bool, verbose: bool) -> None:
    level = logging.DEBUG if verbose else logging.INFO
    handlers: list[logging.Handler] = [logging.StreamHandler(sys.stderr)]
    if to_file:
        try:
            folder.mkdir(parents=True, exist_ok=True)
            handlers.append(logging.FileHandler(folder / "tds_macro.log", encoding="utf-8"))
        except OSError as exc:  # pragma: no cover - depends on the filesystem
            print(f"could not open log file: {exc}", file=sys.stderr)
    logging.basicConfig(
        level=level,
        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
        handlers=handlers,
        force=True,
    )


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
        "--no-wizard", action="store_true", help="skip the first-run setup wizard"
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
    faulthandler.enable()

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

        log.info("self-test: quitting in %d ms", args.self_test)
        QTimer.singleShot(args.self_test, app.quit)

    if args.strategy is not None:
        controller.load_strategy(args.strategy)
        window._reload_strategy()

    if not controller.settings.setup_complete and not args.no_wizard and not args.self_test:
        from .wizard import SetupWizard

        controller.emit_log("info", "First run: opening the setup wizard.")
        SetupWizard(controller, window).exec()
        controller.apply_hotkeys()

    try:
        return app.exec()
    finally:
        controller.shutdown()


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
