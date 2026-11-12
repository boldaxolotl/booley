#!/usr/bin/env python3
"""Launcher that syncs or checks the packaged CHANGELOG mirror without installing Booley."""

from __future__ import annotations

import sys
from pathlib import Path


def main() -> int:
    """Run the CHANGELOG-mirror sync from this checkout's ``src/``, writing no bytecode."""
    sys.dont_write_bytecode = True
    checkout = Path(__file__).resolve().parents[2]
    # First on the path, so this checkout is read even if another one is installed.
    sys.path.insert(0, str(checkout / "src"))
    from booley.dev_support import reference_docs

    return reference_docs.main(
        sys.argv[1:], sync_command="python3 .github/scripts/sync_packaged_copies.py"
    )


if __name__ == "__main__":
    raise SystemExit(main())
