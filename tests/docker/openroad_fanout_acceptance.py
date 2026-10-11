#!/usr/bin/env python3
"""Required native acceptance entrypoint; never silently skips EDA inputs."""

import runpy
import sys
from pathlib import Path

sys.argv.append("--standalone")
runpy.run_path(
    str(Path(__file__).resolve().parents[2] / ".github/scripts/verify_openroad_fanout.py"),
    run_name="__main__",
)
