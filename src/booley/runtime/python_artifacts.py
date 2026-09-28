"""Keep Python's runtime artifacts in Booley-owned directories."""

from __future__ import annotations

import shlex
from collections.abc import Mapping
from pathlib import Path


def relocate_python_artifacts(environment: Mapping[str, str], cache_root: Path) -> dict[str, str]:
    """Copy *environment* and redirect bytecode and pytest caches."""
    relocated = dict(environment)
    relocated["PYTHONPYCACHEPREFIX"] = str(cache_root / "bytecode")
    options = _without_pytest_cache_override(shlex.split(relocated.get("PYTEST_ADDOPTS", "")))
    options.extend(("-o", f"cache_dir={cache_root / 'pytest'}"))
    relocated["PYTEST_ADDOPTS"] = shlex.join(options)
    return relocated


def _without_pytest_cache_override(options: list[str]) -> list[str]:
    """Remove cache_dir overrides while retaining every other pytest option."""
    kept: list[str] = []
    index = 0
    while index < len(options):
        option = options[index]
        if (
            option in {"-o", "--override-ini"}
            and index + 1 < len(options)
            and options[index + 1].startswith("cache_dir=")
        ):
            index += 2
            continue
        if option.startswith(("-o=cache_dir=", "--override-ini=cache_dir=")):
            index += 1
            continue
        kept.append(option)
        index += 1
    return kept


__all__ = ["relocate_python_artifacts"]
