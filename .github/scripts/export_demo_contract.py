#!/usr/bin/env python3
"""Export public-demo checkout fields in GitHub Actions output format."""

from __future__ import annotations

import argparse
import re
import sys
import tomllib
import urllib.parse
from pathlib import Path

_ROOT = Path(__file__).parents[2]
sys.path.insert(0, str(_ROOT / "src"))

from booley.core.boundary import require_str

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
    contract = tomllib.loads(args.contract.read_text(encoding="utf-8"))
    toolchain_url = urllib.parse.urlsplit(require_str(contract, "toolchain_url"))
    if (
        toolchain_url.scheme != "https"
        or not toolchain_url.hostname
        or toolchain_url.username is not None
        or toolchain_url.password is not None
        or toolchain_url.query
        or toolchain_url.fragment
    ):
        raise ValueError("toolchain_url must be a plain HTTPS URL")
    if re.fullmatch(r"[0-9a-f]{64}", require_str(contract, "toolchain_sha256")) is None:
        raise ValueError("toolchain_sha256 must be a lowercase SHA-256 digest")
    for key in OUTPUT_KEYS:
        value = require_str(contract, key)
        if not value or "\n" in value:
            raise ValueError(f"{key} must be a single non-empty output line")
        print(f"{key}={value}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
