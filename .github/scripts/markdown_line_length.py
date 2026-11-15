#!/usr/bin/env python3
"""Launcher for the Markdown line-length check (readiness and CI's docs-check)."""

from __future__ import annotations

import os
import sys
from pathlib import Path


def main() -> int:
    """Load the source owner without allowing checkout bytecode writes."""
    sys.dont_write_bytecode = True
    checkout = Path(__file__).resolve().parents[2]
    sys.path.insert(0, str(checkout / "src"))
    from booley.dev_support.markdown_line_length import main as line_length_main

    return line_length_main()


if __name__ == "__main__":
    os.environ.setdefault("PYTHONDONTWRITEBYTECODE", "1")
    raise SystemExit(main())
