#!/usr/bin/env python3
"""Generate and validate the runtime base's package inventory (stdlib only; run as python -I)."""

from __future__ import annotations

import argparse
import importlib.metadata
import json
import platform
import subprocess
import sys
from pathlib import Path

DPKG_FORMAT = "${Package}\\t${Version}\\t${Architecture}\\t${db:Status-Status}\\n"
FIELDS = ("name", "version", "architecture")


def build_document(dpkg_output: str, python_version: str, pip_version: str) -> str:
    """Return deterministic v1 inventory text: installed rows by (name, architecture)."""
    rows = [line.split("\t") for line in dpkg_output.splitlines() if line]
    packages = [dict(zip(FIELDS, row[:3], strict=True)) for row in rows if row[3] == "installed"]
    packages.sort(key=lambda package: (package["name"], package["architecture"]))
    document = {
        "schema": 1,
        "scope": "runtime-base",
        "python": {"version": python_version, "pip_version": pip_version},
        "packages": packages,
    }
    return json.dumps(document, indent=2, sort_keys=True) + "\n"


def _strings(value: object, keys: tuple[str, ...]) -> bool:
    return isinstance(value, dict) and all(isinstance(value.get(key), str) for key in keys)


def validate_document(text: str) -> str | None:
    """Return why ``text`` is not a v1 inventory, or ``None`` when it is."""
    try:
        document = json.loads(text)
    except json.JSONDecodeError as error:
        return f"not JSON: {error}"
    if not isinstance(document, dict) or document.get("schema") != 1:
        return "schema: expected 1"
    if not _strings(document.get("python"), ("version", "pip_version")):
        return "python: expected string version and pip_version"
    packages = document.get("packages")
    if not isinstance(packages, list) or not packages:
        return "packages: expected a non-empty list"
    if not all(_strings(row, FIELDS) for row in packages):
        return "packages: every row needs string name, version and architecture"
    return None


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("generate").add_argument("--output", type=Path, required=True)
    commands.add_parser("validate").add_argument("path", type=Path)
    args = parser.parse_args(argv)
    if args.command == "generate":
        query = ["dpkg-query", "-W", "-f", DPKG_FORMAT]
        dpkg = subprocess.run(query, capture_output=True, check=True, text=True).stdout
        pip = importlib.metadata.version("pip")
        args.output.write_text(build_document(dpkg, platform.python_version(), pip))
        return 0
    try:
        problem = validate_document(args.path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError) as error:
        problem = f"cannot read: {error}"
    if problem:
        print(f"error: {args.path}: {problem}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
