"""Keep Python's runtime artifacts in Booley-owned directories."""

from __future__ import annotations

import hashlib
import shlex
from collections.abc import Mapping
from pathlib import Path


def python_artifact_root(environment: Mapping[str, str], fallback: Path) -> Path:
    """Return a stable Booley-owned root from an already-composed environment."""
    if runtime := environment.get("BOOLEY_RUNTIME_DIR"):
        return Path(runtime) / "python-artifacts"
    if project := environment.get("BOOLEY_PROJECT_DIR"):
        return Path(project) / ".runtime" / "python-artifacts"
    return fallback


def relocate_python_artifacts(
    environment: Mapping[str, str],
    cache_root: Path,
    *,
    pytest_scope: str | None = None,
    write_bytecode: bool = True,
) -> dict[str, str]:
    """Copy *environment*, control bytecode, and redirect pytest caches."""
    relocated = dict(environment)
    if write_bytecode:
        relocated.pop("PYTHONDONTWRITEBYTECODE", None)
        relocated["PYTHONPYCACHEPREFIX"] = str(cache_root / "bytecode")
    else:
        relocated.pop("PYTHONPYCACHEPREFIX", None)
        relocated["PYTHONDONTWRITEBYTECODE"] = "1"
    options = _pytest_options(relocated.get("PYTEST_ADDOPTS", ""))
    pytest_root = cache_root / "pytest"
    if pytest_scope:
        digest = hashlib.sha256(pytest_scope.encode()).hexdigest()[:16]
        pytest_root /= digest
    options.extend(("-o", f"cache_dir={pytest_root}"))
    relocated["PYTEST_ADDOPTS"] = shlex.join(options)
    return relocated


def _pytest_options(raw: str) -> list[str]:
    """Parse inherited options without breaking non-pytest child processes."""
    try:
        return _without_pytest_cache_override(shlex.split(raw))
    except ValueError:
        return []


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


__all__ = ["python_artifact_root", "relocate_python_artifacts"]
