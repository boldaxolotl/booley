#!/usr/bin/env python3
"""Export public-demo checkout fields in GitHub Actions output format."""

from __future__ import annotations

import argparse
import sys
import tomllib
from pathlib import Path

_ROOT = Path(__file__).parents[2]
sys.path.insert(0, str(_ROOT / "src"))

from booley.core.boundary import require_str

OUTPUT_KEYS = (
    "upstream_repository",
    "upstream_ref",
    "project_repository",
    "project_ref",
    "project_contract_ref",
    "ticket_fixture",
    "ticket_slug",
)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--contract",
        type=Path,
        default=Path(".github/contracts/picorv32-demo.toml"),
    )
    args = parser.parse_args()
    contract = tomllib.loads(args.contract.read_text(encoding="utf-8"))
    for key in OUTPUT_KEYS:
        value = require_str(contract, key)
        if "\n" in value:
            raise ValueError(f"{key} must be a single non-empty output line")
        print(f"{key}={value}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
