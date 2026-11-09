#!/usr/bin/env python3
"""Build the app for the platform this runs on, and zip it.

    python build/build.py            # Windows: TDSMacro folder + zip
                                     # macOS:   TDSMacro.app + zip
    python build/build.py --onefile  # Windows: a single TDSMacro.exe as well

Produces everything under ``build/dist``. PyInstaller does not cross-compile:
build the Windows zip on Windows and the Mac zip on a Mac (CI does both).
"""

from __future__ import annotations

import argparse
import os
import platform
import shutil
import subprocess
import sys
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
BUILD = ROOT / "build"
DIST = BUILD / "dist"
WORK = BUILD / "work"
SPEC = BUILD / "TDSMacro.spec"


def read_version() -> str:
    text = (ROOT / "tds_macro" / "__init__.py").read_text(encoding="utf-8")
    for line in text.splitlines():
        if line.startswith("__version__"):
            return line.split("=", 1)[1].strip().strip("\"'")
    return "0.0.0"


def run_pyinstaller(onefile: bool) -> None:
    environment = dict(os.environ, TDS_ONEFILE="1" if onefile else "0")
    command = [
        sys.executable,
        "-m",
        "PyInstaller",
        "--noconfirm",
        "--clean",
        "--distpath",
        str(DIST),
        "--workpath",
        str(WORK),
        str(SPEC),
    ]
    print(f"$ {' '.join(command)}")
    subprocess.run(command, check=True, cwd=ROOT, env=environment)


def make_zip(version: str) -> Path:
    folder = DIST / "TDSMacro"
    if not folder.is_dir():
        raise SystemExit(f"expected {folder} to exist; did the onedir build run?")
    archive = DIST / f"TDSMacro-{version}-windows.zip"
    if archive.exists():
        archive.unlink()
    with zipfile.ZipFile(archive, "w", zipfile.ZIP_DEFLATED) as zipped:
        for path in sorted(folder.rglob("*")):
            if path.is_file():
                zipped.write(path, Path("TDSMacro") / path.relative_to(folder))
        for extra in ("README.md", "LICENSE"):
            source = ROOT / extra
            if source.exists():
                zipped.write(source, Path("TDSMacro") / extra)
        examples = ROOT / "strategies"
        for example in sorted(examples.glob("*.json")):
            zipped.write(example, Path("TDSMacro") / "strategies" / example.name)
    return archive


def make_mac_zip(version: str) -> Path:
    """Zip TDSMacro.app with ``ditto``.

    Python's zipfile drops symlinks and executable bits, and a .app bundle
    relies on both -- an app zipped that way unzips into something macOS
    reports as damaged. ``ditto`` is what Finder's own "Compress" uses.
    """
    app = DIST / "TDSMacro.app"
    if not app.is_dir():
        raise SystemExit(f"expected {app} to exist; did the build run on macOS?")
    stage = DIST / "TDSMacro"
    if stage.exists():
        shutil.rmtree(stage)
    stage.mkdir()
    subprocess.run(["ditto", str(app), str(stage / "TDSMacro.app")], check=True)
    for extra in ("README.md", "LICENSE"):
        source = ROOT / extra
        if source.exists():
            shutil.copy2(source, stage / extra)
    examples = ROOT / "strategies"
    if examples.is_dir():
        shutil.copytree(examples, stage / "strategies")
    archive = DIST / f"TDSMacro-{version}-mac-{platform.machine()}.zip"
    if archive.exists():
        archive.unlink()
    subprocess.run(
        ["ditto", "-c", "-k", "--sequesterRsrc", "--keepParent", str(stage), str(archive)],
        check=True,
    )
    return archive


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Build TDS Macro for Windows.")
    parser.add_argument(
        "--onefile",
        action="store_true",
        help="also build a single-file exe (slower to start, more likely to be "
        "flagged by antivirus)",
    )
    parser.add_argument("--keep-work", action="store_true", help="keep intermediate files")
    args = parser.parse_args(argv)

    version = read_version()
    if sys.platform not in ("win32", "darwin"):
        print(
            "WARNING: neither Windows nor macOS. PyInstaller does not cross-compile, so "
            "this produces a binary for the current platform only.",
            file=sys.stderr,
        )

    shutil.rmtree(DIST, ignore_errors=True)
    run_pyinstaller(onefile=False)
    if sys.platform == "darwin":
        archive = make_mac_zip(version)
        print(f"\napp bundle:    {DIST / 'TDSMacro.app'}")
    else:
        archive = make_zip(version)
        print(f"\nonedir bundle: {DIST / 'TDSMacro'}")
    print(f"zip:           {archive}  ({archive.stat().st_size / 1e6:.1f} MB)")

    if args.onefile:
        run_pyinstaller(onefile=True)
        single = DIST / ("TDSMacro.exe" if sys.platform == "win32" else "TDSMacro")
        if single.exists():
            print(f"onefile:       {single}  ({single.stat().st_size / 1e6:.1f} MB)")

    if not args.keep_work:
        shutil.rmtree(WORK, ignore_errors=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
