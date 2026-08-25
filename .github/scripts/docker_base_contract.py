#!/usr/bin/env python3
"""Repository entry point for the stable runtime-base contract module."""

from __future__ import annotations

import sys
from pathlib import Path

_ROOT = Path(__file__).parents[2]
sys.path.insert(0, str(_ROOT / "src"))

from booley.harness.docker_base_contract import main, stable_base_inputs

__all__ = ["main", "stable_base_inputs"]

if __name__ == "__main__":
    raise SystemExit(main())
