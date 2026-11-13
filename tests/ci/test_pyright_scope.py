"""Pyright checks the whole package; the exclusion list may only shrink (#3805)."""

from __future__ import annotations

import sys
import tomllib
from pathlib import Path
from typing import Any

import pytest

_ROOT = Path(__file__).parents[2]

_PACKAGE = "src/booley"
sys.path.insert(0, str(_ROOT / ".github/scripts"))

# Number of modules `[tool.pyright] exclude` may hold. Lower it in the same change
# that fixes an excluded module and deletes its entry; never raise it to add a module.
EXCLUDE_CEILING = 102


def _violations(config: dict[str, Any], ceiling: int) -> list[str]:
    """Import the checker inside the test body, so a missing module fails the test itself."""
    from pyright_scope import pyright_scope_violations

    return pyright_scope_violations(config, ceiling)


def _valid_config() -> dict[str, Any]:
    return {
        "include": [_PACKAGE],
        "exclude": [f"{_PACKAGE}/a.py", f"{_PACKAGE}/b.py"],
        "strict": [f"{_PACKAGE}/targets"],
    }


def test_pyproject_pyright_scope_is_whole_package_with_capped_exclusions() -> None:
    """The committed Pyright table covers the package and its exclusions never grow."""
    pyproject = tomllib.loads((_ROOT / "pyproject.toml").read_text(encoding="utf-8"))

    assert _violations(pyproject["tool"]["pyright"], EXCLUDE_CEILING) == []


def _case(**changes: Any) -> dict[str, Any]:
    config = _valid_config()
    config.update(changes)
    return config


@pytest.mark.parametrize(
    ("config", "ceiling", "phrase"),
    [
        pytest.param(
            _case(include=["src/booley/core"]), 2, "whole package", id="include-narrowed"
        ),
        pytest.param(
            _case(exclude=[f"{_PACKAGE}/b.py", "tools/a.py"]),
            2,
            "one module path",
            id="outside-package",
        ),
        pytest.param(
            _case(exclude=[f"{_PACKAGE}/a.py", f"{_PACKAGE}/flows"]),
            2,
            "one module path",
            id="directory-entry",
        ),
        pytest.param(
            _case(exclude=[f"{_PACKAGE}/*.py", f"{_PACKAGE}/a.py"]),
            2,
            "must not be a glob",
            id="glob-entry",
        ),
        pytest.param(
            _case(exclude=[f"{_PACKAGE}/a.py", f"{_PACKAGE}/a.py"]),
            2,
            "duplicate",
            id="duplicate",
        ),
        pytest.param(
            _case(exclude=[f"{_PACKAGE}/b.py", f"{_PACKAGE}/a.py"]),
            2,
            "sorted",
            id="unsorted",
        ),
        pytest.param(_valid_config(), 1, "renamed", id="grown"),
        pytest.param(_valid_config(), 3, "lower EXCLUDE_CEILING", id="shrunk"),
        pytest.param(
            _case(exclude=[f"{_PACKAGE}/a.py", f"{_PACKAGE}/targets/x.py"]),
            2,
            "strict path",
            id="under-strict",
        ),
    ],
)
def test_scope_violation_is_reported(config: dict[str, Any], ceiling: int, phrase: str) -> None:
    """Each rule of the scope guard fails on its own violation."""
    violations = _violations(config, ceiling)

    assert len(violations) == 1
    assert phrase in violations[0]
    if phrase == "renamed":
        assert "fix the module's type errors" in violations[0]


@pytest.mark.parametrize(
    "exclude",
    [
        pytest.param([f"{_PACKAGE}/a.py", f"{_PACKAGE}/renamed_b.py"], id="renamed"),
        pytest.param([f"{_PACKAGE}/a.py", f"{_PACKAGE}/deleted_module.py"], id="deleted"),
    ],
)
def test_scope_accepts_renamed_and_deleted_entries(exclude: list[str]) -> None:
    """A renamed entry (same count) and an entry for a deleted module pass."""
    assert _violations(_case(exclude=exclude), 2) == []
