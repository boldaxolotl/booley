#!/usr/bin/env python3
"""Export public-demo checkout fields in GitHub Actions output format."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

_ROOT = Path(__file__).parents[2]
sys.path.insert(0, str(_ROOT / "src"))

from booley.dev_support.demo_contract import load_contract

OUTPUT_KEYS = (
    "upstream_repository",
    "upstream_ref",
    "project_repository",
    "project_ref",
    "ticket_fixture",
    "ticket_slug",
    "toolchain_url",
    "toolchain_sha256",
)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--contract",
        type=Path,
        default=Path(".github/contracts/picorv32-demo.toml"),
    )
    args = parser.parse_args()
    contract = load_contract(args.contract)
    for key in OUTPUT_KEYS:
        value = getattr(contract, key)
        print(f"{key}={value}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
