#!/usr/bin/env python3
"""Load the checkout's contributor development-wheel builder."""

from __future__ import annotations

import os
import sys
from pathlib import Path


def main() -> int:
    """Run the source owner independently of the caller's working directory."""
    sys.dont_write_bytecode = True
    checkout = Path(__file__).resolve().parents[2]
    sys.path.insert(0, str(checkout / "src"))
    from booley.dev_support.build_development_wheel import main as build_main

    return build_main(checkout)


if __name__ == "__main__":
    os.environ.setdefault("PYTHONDONTWRITEBYTECODE", "1")
    raise SystemExit(main())
