# -*- mode: python ; coding: utf-8 -*-
"""PyInstaller spec for TDS Macro.

Builds a **onedir** bundle by default: a folder holding TDSMacro.exe next to its
libraries, which the build script zips.  That choice is deliberate:

* startup is instant, where a onefile exe self-extracts to a temp folder on
  every launch (several seconds for a Qt app);
* ``settings.json`` genuinely lives beside the exe, which is what makes the app
  portable;
* Windows Defender and SmartScreen flag onefile PyInstaller binaries far more
  readily, and an app that synthesises input and captures the screen is already
  an awkward shape for a heuristic scanner.

Set ``TDS_ONEFILE=1`` to build a single file anyway -- handier for handing to a
friend, at the cost of the above.

Run through ``build/build.py`` rather than directly, so the version and the
output layout stay consistent.
"""

import os
import sys
from pathlib import Path

ROOT = Path(SPECPATH).resolve().parent
sys.path.insert(0, str(ROOT))

ONEFILE = os.environ.get("TDS_ONEFILE", "") not in ("", "0", "false", "False")
IS_MAC = sys.platform == "darwin"


def read_version() -> str:
    for line in (ROOT / "tds_macro" / "__init__.py").read_text(encoding="utf-8").splitlines():
        if line.startswith("__version__"):
            return line.split("=", 1)[1].strip().strip("\"'")
    return "0.0.0"


if IS_MAC:
    # pyobjc frameworks are imported inside try/except so the package loads on
    # every platform; naming them here guarantees they are bundled even if the
    # analysis treats those imports as optional.
    HIDDEN_IMPORTS = [
        "mss.darwin",
        "Quartz",
        "AppKit",
        "ApplicationServices",
        "HIServices",
        "Foundation",
        "CoreFoundation",
        "objc",
    ]
else:
    HIDDEN_IMPORTS = ["mss.windows"]

# Qt ships a great deal this app never touches.  Excluding it cuts the bundle
# roughly in half and removes DLLs that scanners like least.
EXCLUDES = [
    "PySide6.QtWebEngineCore",
    "PySide6.QtWebEngineWidgets",
    "PySide6.QtWebEngineQuick",
    "PySide6.QtWebChannel",
    "PySide6.QtWebSockets",
    "PySide6.Qt3DCore",
    "PySide6.Qt3DRender",
    "PySide6.Qt3DAnimation",
    "PySide6.Qt3DExtras",
    "PySide6.QtCharts",
    "PySide6.QtDataVisualization",
    "PySide6.QtMultimedia",
    "PySide6.QtMultimediaWidgets",
    "PySide6.QtQuick",
    "PySide6.QtQuick3D",
    "PySide6.QtQml",
    "PySide6.QtQuickWidgets",
    "PySide6.QtBluetooth",
    "PySide6.QtNfc",
    "PySide6.QtPositioning",
    "PySide6.QtSerialPort",
    "PySide6.QtSql",
    "PySide6.QtTest",
    "PySide6.QtDesigner",
    "PySide6.QtHelp",
    "PySide6.QtPdf",
    "PySide6.QtPdfWidgets",
    "PySide6.QtSvgWidgets",
    "PySide6.QtOpenGL",
    "PySide6.QtOpenGLWidgets",
    # Never exclude shiboken6.Shiboken: PySide6/__init__.py imports it
    # unconditionally, and without it the frozen app cannot import Qt at all.
    "tkinter",
    "matplotlib",
    "scipy",
    "pandas",
    "PIL",
    "IPython",
    "pytest",
    "setuptools",
    "pip",
]

analysis = Analysis(
    [str(ROOT / "tds_macro" / "__main__.py")],
    pathex=[str(ROOT)],
    binaries=[],
    # The theme is a Python string and the glyph templates live in the user's
    # own calibration folder, so there is nothing to bundle -- a missing data
    # file would otherwise ship an app that looks broken instead of failing
    # loudly.
    datas=[],
    hiddenimports=HIDDEN_IMPORTS,
    hookspath=[],
    runtime_hooks=[],
    excludes=EXCLUDES,
    noarchive=False,
    optimize=0,
)
pyz = PYZ(analysis.pure)

COMMON = dict(
    name="TDSMacro",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,  # UPX-packed binaries trip antivirus heuristics almost every time
    console=False,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
)

if ONEFILE:
    exe = EXE(
        pyz,
        analysis.scripts,
        analysis.binaries,
        analysis.datas,
        upx_exclude=[],
        runtime_tmpdir=None,
        **COMMON,
    )
else:
    exe = EXE(pyz, analysis.scripts, exclude_binaries=True, **COMMON)
    collected = COLLECT(
        exe,
        analysis.binaries,
        analysis.datas,
        strip=False,
        upx=False,
        upx_exclude=[],
        name="TDSMacro",
    )

if IS_MAC and not ONEFILE:
    # A real .app bundle. macOS ties the privacy permissions the user grants to
    # this bundle's identifier and signature, so both are kept stable.
    app = BUNDLE(
        collected,
        name="TDSMacro.app",
        icon=None,
        bundle_identifier="com.tdsmacro.app",
        info_plist={
            "CFBundleName": "TDS Macro",
            "CFBundleDisplayName": "TDS Macro",
            "CFBundleShortVersionString": read_version(),
            "CFBundleVersion": read_version(),
            "LSMinimumSystemVersion": "11.0",
            "NSHighResolutionCapable": True,
        },
    )
