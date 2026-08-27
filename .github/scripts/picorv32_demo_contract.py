#!/usr/bin/env python3
"""Repository entry point for the CI-owned PicoRV32 demo contract."""

from __future__ import annotations

import sys
from pathlib import Path

_ROOT = Path(__file__).parents[2]
sys.path.insert(0, str(_ROOT / "src"))

from booley.dev_support.demo_contract import main

if __name__ == "__main__":
    raise SystemExit(main())
