#!/usr/bin/env python3
"""Export ``project.dependencies`` unchanged as a pip requirements file."""

from __future__ import annotations

import argparse
import sys
import tomllib
from pathlib import Path


def project_dependency_requirements(pyproject_text: str) -> str:
    """Render ``[project].dependencies`` as pip requirements text, one per line.

    The base image build and the base compatibility contract both use this
    function, so they cannot disagree about which part of ``pyproject.toml``
    the base image installs.
    """
    project = tomllib.loads(pyproject_text).get("project", {})
    if not isinstance(project, dict):
        raise ValueError("project must be a table")
    dependencies = project.get("dependencies")
    if not isinstance(dependencies, list) or not all(
        isinstance(dependency, str) for dependency in dependencies
    ):
        raise ValueError("project.dependencies must be a list of strings")
    return "".join(f"{dependency}\n" for dependency in dependencies)


def export_project_dependencies(pyproject: Path, destination: Path) -> None:
    """Write the requirements file, with LF endings on every platform."""
    requirements = project_dependency_requirements(pyproject.read_bytes().decode("utf-8"))
    destination.write_text(requirements, encoding="utf-8", newline="\n")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("pyproject", type=Path)
    parser.add_argument("destination", type=Path)
    args = parser.parse_args()
    try:
        export_project_dependencies(args.pyproject, args.destination)
    except (OSError, ValueError) as error:
        print(f"error: {error}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
