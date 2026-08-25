"""Validate an installed Booley artifact without importing from the checkout."""

from __future__ import annotations

import argparse
import email
import importlib.metadata
import subprocess
import sys
import sysconfig
import zipfile
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
    "booley/data/docker/export_project_dependencies.py",
    "booley/data/edalize/verible.py",
    "booley/dev_support/pre-commit-ruff.sh",
    "booley/dev_support/worktree_create.sh",
    "booley/harness/console/console.tcss",
    "booley/vivado/xdc/timing.xdc",
    "booley/yosys/abc_config.json",
    "booley/yosys/abc_scripts/abc_balanced.script",
    "booley/yosys/sdc/abc_simple.sdc",
}


class ArtifactValidationError(ValueError):
    """The installed distribution does not match its declared artifact contract."""


def _require(condition: bool, message: str) -> None:
    if not condition:
        raise ArtifactValidationError(message)


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--inventory", type=Path, required=True)
    parser.add_argument("--forbid-root", type=Path, required=True)
    parser.add_argument("--wheel", type=Path)
    return parser.parse_args()


def _assert_installed_origin(forbid_root: Path) -> None:
    module_path = Path(booley.__file__).resolve()
    environment_root = Path(sys.prefix).resolve()
    checkout_root = forbid_root.resolve()
    _require(
        module_path.is_relative_to(environment_root),
        f"booley imported outside clean environment {environment_root}: {module_path}",
    )
    _require(
        not module_path.is_relative_to(checkout_root),
        f"booley imported from checkout {checkout_root}: {module_path}",
    )
    _require(
        not Path.cwd().resolve().is_relative_to(checkout_root),
        f"artifact smoke must run outside checkout: {Path.cwd()}",
    )


def _distribution_files() -> set[str]:
    distribution = importlib.metadata.distribution("booley-rtl")
    files = distribution.files
    if files is None:
        raise ArtifactValidationError("installed distribution has no file inventory")
    return {str(path) for path in files}


def _assert_resources(files: set[str]) -> None:
    missing = EXPECTED_RESOURCES - files
    _require(not missing, f"installed artifact is missing resources: {sorted(missing)}")
    forbidden = {
        path
        for path in files
        if path.startswith("booley/data/docker/pdk/")
        or path in {"booley/data/bin/bwave", "booley/data/bin/bwave.exe"}
    }
    _require(
        not forbidden,
        f"installed artifact contains forbidden payloads: {sorted(forbidden)}",
    )


def _entry_point_executable(name: str) -> Path:
    """Return an entry point from the interpreter's active install scheme."""
    return Path(sysconfig.get_path("scripts"), name)


def _assert_entry_points() -> None:
    entry_points = {
        entry.name: entry.value
        for entry in importlib.metadata.distribution("booley-rtl").entry_points
        if entry.group == "console_scripts"
    }
    for name, expected_value in EXPECTED_ENTRY_POINTS.items():
        _require(
            entry_points.get(name) == expected_value,
            f"entry point {name!r} is {entry_points.get(name)!r}, expected {expected_value!r}",
        )
        executable = _entry_point_executable(name)
        result = subprocess.run(
            [str(executable), "--help"],
            capture_output=True,
            text=True,
            timeout=30,
            check=False,
        )
        _require(
            result.returncode == 0,
            f"{name} --help failed with {result.returncode}:\n{result.stdout}{result.stderr}",
        )


def _package_files(paths: set[str]) -> set[str]:
    return {
        path
        for path in paths
        if path.startswith("booley/") and "/__pycache__/" not in path and not path.endswith(".pyc")
    }


def _assert_wheel_matches_install(wheel: Path, installed_files: set[str]) -> None:
    with zipfile.ZipFile(wheel) as archive:
        names = set(archive.namelist())
        metadata_paths = [name for name in names if name.endswith(".dist-info/METADATA")]
        _require(
            len(metadata_paths) == 1,
            f"candidate wheel has {len(metadata_paths)} METADATA files",
        )
        wheel_metadata = email.message_from_bytes(archive.read(metadata_paths[0]))

    installed_version = importlib.metadata.version("booley-rtl")
    _require(
        wheel_metadata["Version"] == installed_version,
        f"installed version {installed_version} does not match wheel {wheel_metadata['Version']}",
    )
    wheel_files = _package_files(names)
    installed_package_files = _package_files(installed_files)
    _require(
        installed_package_files == wheel_files,
        "installed package inventory differs from candidate wheel: "
        f"missing={sorted(wheel_files - installed_package_files)}, "
        f"extra={sorted(installed_package_files - wheel_files)}",
    )


def main() -> None:
    args = _parse_args()
    _assert_installed_origin(args.forbid_root)
    files = _distribution_files()
    _assert_resources(files)
    _assert_entry_points()
    if args.wheel is not None:
        _assert_wheel_matches_install(args.wheel, files)
    args.inventory.write_text("\n".join(sorted(files)) + "\n", encoding="utf-8")


if __name__ == "__main__":
    main()
