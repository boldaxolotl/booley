"""Validate an installed Booley artifact without importing from the checkout."""

from __future__ import annotations

import argparse
import importlib.metadata
import os
import subprocess
import sys
from pathlib import Path

import booley

EXPECTED_ENTRY_POINTS = {
    "booley": "booley.harness.booley:main",
    "booley-mcp": "booley.mcp.server:main",
    "bwave": "booley.bwave.cli:main",
}
EXPECTED_RESOURCES = {
    "booley/data/docker/Dockerfile",
    "booley/data/docker/build.sh",
    "booley/data/edalize/verible.py",
    "booley/dev_support/pre-commit-ruff.sh",
    "booley/dev_support/worktree_create.sh",
    "booley/harness/console/console.tcss",
    "booley/vivado/xdc/timing.xdc",
    "booley/yosys/abc_config.json",
    "booley/yosys/abc_scripts/abc_balanced.script",
    "booley/yosys/sdc/abc_simple.sdc",
}


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--inventory", type=Path, required=True)
    parser.add_argument("--forbid-root", type=Path, required=True)
    return parser.parse_args()


def _assert_installed_origin(forbid_root: Path) -> None:
    module_path = Path(booley.__file__).resolve()
    environment_root = Path(sys.prefix).resolve()
    checkout_root = forbid_root.resolve()
    assert module_path.is_relative_to(environment_root), (
        f"booley imported outside clean environment {environment_root}: {module_path}"
    )
    assert not module_path.is_relative_to(checkout_root), (
        f"booley imported from checkout {checkout_root}: {module_path}"
    )
    assert not Path.cwd().resolve().is_relative_to(checkout_root), (
        f"artifact smoke must run outside checkout: {Path.cwd()}"
    )


def _distribution_files() -> set[str]:
    distribution = importlib.metadata.distribution("booley-rtl")
    files = distribution.files
    assert files is not None, "installed distribution has no file inventory"
    return {str(path) for path in files}


def _assert_resources(files: set[str]) -> None:
    missing = EXPECTED_RESOURCES - files
    assert not missing, f"installed artifact is missing resources: {sorted(missing)}"
    forbidden = {
        path
        for path in files
        if path.startswith("booley/data/docker/pdk/")
        or path in {"booley/data/bin/bwave", "booley/data/bin/bwave.exe"}
    }
    assert not forbidden, f"installed artifact contains forbidden payloads: {sorted(forbidden)}"


def _assert_entry_points() -> None:
    entry_points = {
        entry.name: entry.value
        for entry in importlib.metadata.distribution("booley-rtl").entry_points
        if entry.group == "console_scripts"
    }
    for name, expected_value in EXPECTED_ENTRY_POINTS.items():
        assert entry_points.get(name) == expected_value, (
            f"entry point {name!r} is {entry_points.get(name)!r}, expected {expected_value!r}"
        )
        executable = Path(sys.prefix, "Scripts" if os.name == "nt" else "bin", name)
        result = subprocess.run(
            [str(executable), "--help"],
            capture_output=True,
            text=True,
            timeout=30,
            check=False,
        )
        assert result.returncode == 0, (
            f"{name} --help failed with {result.returncode}:\n{result.stdout}{result.stderr}"
        )


def main() -> None:
    args = _parse_args()
    _assert_installed_origin(args.forbid_root)
    files = _distribution_files()
    _assert_resources(files)
    _assert_entry_points()
    args.inventory.write_text("\n".join(sorted(files)) + "\n", encoding="utf-8")


if __name__ == "__main__":
    main()
