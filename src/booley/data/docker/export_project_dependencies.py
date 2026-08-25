#!/usr/bin/env python3
"""Export ``project.dependencies`` unchanged as a pip requirements file."""

from __future__ import annotations

import argparse
import sys
import tomllib
from pathlib import Path


def export_project_dependencies(pyproject: Path, destination: Path) -> None:
    project = tomllib.loads(pyproject.read_text(encoding="utf-8")).get("project", {})
    if not isinstance(project, dict):
        raise ValueError("project must be a table")
    dependencies = project.get("dependencies")
    if not isinstance(dependencies, list) or not all(
        isinstance(dependency, str) for dependency in dependencies
    ):
        raise ValueError("project.dependencies must be a list of strings")
    destination.write_text(
        "".join(f"{dependency}\n" for dependency in dependencies), encoding="utf-8"
    )


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("pyproject", type=Path)
    parser.add_argument("destination", type=Path)
    args = parser.parse_args()
    try:
        export_project_dependencies(args.pyproject, args.destination)
    except (OSError, tomllib.TOMLDecodeError, ValueError) as error:
        print(f"error: {error}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
