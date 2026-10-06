"""Shared prerequisite for native B-Wave contract and session tests."""

import argparse
import sys
from pathlib import Path

import pytest

from booley.dev_support.test_profiles import BWAVE_BUILD, native_test_binary


def require_native_binary() -> Path:
    """Select the current checkout's debug build or explain its absence."""
    binary = native_test_binary(Path(__file__).resolve().parents[2])
    if not binary.is_file():
        pytest.skip(f"native B-Wave debug binary not built; run {' '.join(BWAVE_BUILD)}")
    return binary


def require_exact_binary(binary: Path) -> Path:
    """Fail without resolving another binary or triggering a Cargo build."""
    if not binary.is_file():
        raise FileNotFoundError(f"native B-Wave debug binary is missing: {binary}")
    return binary


def main() -> None:
    """Run a child CLI with a strict, test-only native resolver."""
    parser = argparse.ArgumentParser(add_help=False)
    parser.add_argument("binary", type=Path)
    args, remaining = parser.parse_known_args()
    sys.argv[1:] = remaining

    from booley.bwave import cli
    from booley.runtime import paths

    def resolve() -> Path:
        return require_exact_binary(args.binary)

    paths.native_bwave_binary = resolve
    cli.native_bwave_binary = resolve
    try:
        cli.main()
    except FileNotFoundError as error:
        raise SystemExit(str(error)) from error


if __name__ == "__main__":
    main()
