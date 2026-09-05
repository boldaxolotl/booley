#!/usr/bin/env python3
"""Export public-demo checkout fields in GitHub Actions output format."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

_ROOT = Path(__file__).parents[2]
sys.path.insert(0, str(_ROOT / "src"))

from booley.dev_support.demo_contract_codec import DemoContract, DemoContractError, load_contract

OUTPUT_KEYS = (
    "upstream_repository",
    "upstream_ref",
    "project_repository",
    "project_ref",
    "ticket_fixture",
    "ticket_slug",
)


def _render_outputs(contract: DemoContract) -> str:
    lines: list[str] = []
    for key in OUTPUT_KEYS:
        value = getattr(contract, key)
        if not value or "\n" in value or "\r" in value:
            raise DemoContractError(f"{key} must be a single non-empty output line")
        lines.append(f"{key}={value}")
    return "\n".join(lines) + "\n"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--contract",
        type=Path,
        default=Path(".github/contracts/picorv32-demo.toml"),
    )
    args = parser.parse_args(argv)
    try:
        output = _render_outputs(load_contract(args.contract))
    except DemoContractError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 2
    sys.stdout.write(output)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
