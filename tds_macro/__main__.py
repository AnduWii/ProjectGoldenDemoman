"""``python -m tds_macro`` entry point."""

from __future__ import annotations

import sys

from .ui.app import main

if __name__ == "__main__":
    sys.exit(main())
