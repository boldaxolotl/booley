"""Rules for the `[tool.pyright]` scope in `pyproject.toml` (#3805).

Pyright checks the whole `src/booley` package. `exclude` lists the modules that still
fail basic mode; it may shrink but never grow. `tests/ci/test_pyright_scope.py` runs
these rules against the committed configuration.
"""

from __future__ import annotations

from typing import Any

PACKAGE = "src/booley"


def pyright_scope_violations(config: dict[str, Any], ceiling: int) -> list[str]:
    """Return one message per rule the `[tool.pyright]` table breaks."""
    violations: list[str] = []
    if config.get("include") != [PACKAGE]:
        violations.append(f'include must be exactly ["{PACKAGE}"] to cover the whole package')
    exclude: list[str] = list(config.get("exclude", []))
    strict: list[str] = list(config.get("strict", []))
    for entry in exclude:
        parts = entry.split("/")
        if not entry.startswith(f"{PACKAGE}/") or ".." in parts or not entry.endswith(".py"):
            violations.append(f"exclude entry {entry!r} must be one module path under {PACKAGE}/")
        elif any(char in entry for char in "*?["):
            violations.append(f"exclude entry {entry!r} must not be a glob")
        if any(entry == path or entry.startswith(path + "/") for path in strict):
            violations.append(f"exclude entry {entry!r} lies under a strict path")
    if len(set(exclude)) != len(exclude):
        violations.append("exclude has duplicate entries")
    if exclude != sorted(exclude):
        violations.append("exclude entries must be sorted")
    if len(exclude) > ceiling:
        violations.append(
            f"exclude grew from {ceiling} to {len(exclude)}: if you renamed an excluded "
            "module, replace its old entry instead of adding one; otherwise fix the "
            "module's type errors"
        )
    elif len(exclude) < ceiling:
        violations.append(
            f"exclude shrank to {len(exclude)}: lower EXCLUDE_CEILING to "
            f"{len(exclude)} in this change"
        )
    return violations
