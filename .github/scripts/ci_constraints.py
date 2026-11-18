"""Check that ``.github/ci/constraints.txt`` is current for ``pyproject.toml`` (#2707).

CI installs the project with ``-c .github/ci/constraints.txt``. A stale file makes
pip fail with an opaque ``ResolutionImpossible``, so the always-run
``release-semantic`` job runs this module (no project install needed) and prints
the command that regenerates the file.

Scope: catches an added, removed, or re-pinned direct dependency and a wrong
compile command. It does not notice a new extra or marker on an existing direct
requirement. A self-reference (``booley-rtl[test]``, how ``dev`` composes the other
extras) is not a direct requirement: it carries no pin and uv writes no line for it.
"""

from __future__ import annotations

import re
import sys
import tomllib
from dataclasses import dataclass, field
from pathlib import Path

from packaging.requirements import Requirement
from packaging.utils import canonicalize_name
from packaging.version import Version

CONSTRAINTS_PATH = ".github/ci/constraints.txt"
# The ``# via`` annotation uv writes for a direct requirement of this project.
_DIRECT_VIA = "booley-rtl (pyproject.toml)"
_PIN_LINE = re.compile(r"^(?P<name>[A-Za-z0-9][A-Za-z0-9._-]*)==(?P<version>[^\s;]+)")
_PYTHON_FLOOR = re.compile(r">=\s*(\d+\.\d+)")


@dataclass
class Constraints:
    """Parsed view of a uv-compiled constraints file."""

    header_command: str = ""
    pins: dict[str, list[Version]] = field(default_factory=dict)
    direct_names: set[str] = field(default_factory=set)
    has_hashes: bool = False


def regenerate_command(project: dict) -> str:
    """Return the one command that regenerates the constraints file."""
    floor = _PYTHON_FLOOR.search(project.get("requires-python", ""))
    version = floor[1] if floor else "3.11"
    return (
        f"uv pip compile pyproject.toml --all-extras --universal "
        f"--python-version {version} -o {CONSTRAINTS_PATH}"
    )


def parse(text: str) -> Constraints:
    """Parse constraints text: header command, pins per name, direct names."""
    parsed = Constraints()
    current = ""
    in_via = False
    lines = text.splitlines()
    for index, line in enumerate(lines):
        if line.startswith("#    uv pip compile") and index == 1:
            parsed.header_command = line.removeprefix("#").strip()
        if "--hash" in line:
            parsed.has_hashes = True
        match = _PIN_LINE.match(line)
        if match:
            current = canonicalize_name(match["name"])
            parsed.pins.setdefault(current, []).append(Version(match["version"]))
            in_via = False
            continue
        stripped = line.strip()
        if stripped == "# via":
            in_via = True
        elif stripped.startswith("# via "):
            in_via = False
            if stripped.removeprefix("# via ") == _DIRECT_VIA:
                parsed.direct_names.add(current)
        elif in_via and stripped.startswith("#") and stripped.lstrip("# ") == _DIRECT_VIA:
            parsed.direct_names.add(current)
    return parsed


def _direct_requirements(project: dict) -> list[Requirement]:
    """Runtime dependencies plus every optional extra, minus self-references."""
    declared = list(project.get("dependencies", ()))
    for extra in project.get("optional-dependencies", {}).values():
        declared.extend(extra)
    own_name = canonicalize_name(project.get("name", ""))
    requirements = [Requirement(item) for item in declared]
    return [item for item in requirements if canonicalize_name(item.name) != own_name]


def stale_reasons(project: dict, text: str) -> list[str]:
    """Return one sentence per way *text* is out of date with *project*."""
    parsed = parse(text)
    reasons: list[str] = []
    expected = regenerate_command(project)
    if parsed.header_command != expected:
        reasons.append(
            f"header command is {parsed.header_command or 'missing'!r}, expected {expected!r}"
        )
    if parsed.has_hashes:
        reasons.append("file contains --hash lines; pip rejects hashed constraints with -e")
    direct: set[str] = set()
    for requirement in _direct_requirements(project):
        name = canonicalize_name(requirement.name)
        direct.add(name)
        versions = parsed.pins.get(name)
        if not versions:
            reasons.append(f"{requirement} has no pin")
        elif any(not requirement.specifier.contains(v, prereleases=True) for v in versions):
            pinned = ", ".join(str(v) for v in versions)
            reasons.append(f"{requirement} does not admit pinned {pinned}")
    if parsed.direct_names != direct:
        changed = sorted(parsed.direct_names ^ direct)
        reasons.append(f"direct dependency names differ from pyproject: {', '.join(changed)}")
    return reasons


def failure_message(reasons: list[str], command: str) -> str:
    """Join *reasons* and the regenerate command into the CI failure text."""
    lines = [f"{CONSTRAINTS_PATH} is stale:", *(f"  - {reason}" for reason in reasons)]
    lines.append(f"Regenerate with: {command}")
    return "\n".join(lines)


def main(root: Path | None = None) -> int:
    """Check the committed file; print the failure message to stderr and return 1 if stale."""
    root = root or Path(__file__).resolve().parents[2]
    project = tomllib.loads((root / "pyproject.toml").read_text(encoding="utf-8"))["project"]
    text = (root / CONSTRAINTS_PATH).read_text(encoding="utf-8")
    reasons = stale_reasons(project, text)
    if reasons:
        print(failure_message(reasons, regenerate_command(project)), file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
