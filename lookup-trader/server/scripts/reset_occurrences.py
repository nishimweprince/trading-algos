#!/usr/bin/env python3
"""Back up and clear the occurrences table.

Prefer running from the repo root:

    ./scripts/reset_database.sh
    ./scripts/reset_database.sh --yes
    ./scripts/reset_database.sh --full --yes
"""

from __future__ import annotations

import runpy
import sys
from pathlib import Path

_ROOT_SCRIPT = Path(__file__).resolve().parents[2] / "scripts" / "reset_database.py"

if __name__ == "__main__":
    sys.argv[0] = str(_ROOT_SCRIPT)
    runpy.run_path(str(_ROOT_SCRIPT), run_name="__main__")
