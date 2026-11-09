"""``python -m tds_macro`` entry point, and the script PyInstaller freezes.

The import is absolute on purpose. PyInstaller runs this file as a top-level
script with no parent package, so a relative ``from .ui.app import main``
raised ImportError the instant the built exe started -- the release shipped
an app that could not open.
"""

from __future__ import annotations

import sys

from tds_macro.ui.app import main

if __name__ == "__main__":
    sys.exit(main())
