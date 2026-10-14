"""Project-declared Protected Inputs: strict configuration and checkout selection."""

from __future__ import annotations

import tomllib
from collections.abc import Mapping
from pathlib import Path, PureWindowsPath
from typing import Any

from booley.core.booley_dir import resolve_checkout_booley_dir
from booley.core.boundary import require_dict, require_list, require_str_value
from booley.core.config_paths import resolve_toml


def parse_protected_inputs(data: Mapping[str, Any]) -> tuple[str, ...]:
    """Validate `[project].protected_inputs`; omission and an empty list add nothing."""
    project = require_dict(data.get("project", {}), field="[project]")
    values = require_list(project.get("protected_inputs", []), field="[project].protected_inputs")
    declarations = tuple(
        validate_declaration(require_str_value(value, field="[project].protected_inputs entry"))
        for value in values
    )
    return tuple(dict.fromkeys(declarations))


def validate_declaration(value: str) -> str:
    """A nonempty Project-relative POSIX file, directory, or segment glob."""
    validate_input_path(value)
    parts = value.removesuffix("/").split("/")
    if any(char in value for char in "[]{}") or any(
        "**" in part and part != "**" for part in parts
    ):
        raise ValueError(
            f"[project].protected_inputs entry {value!r} must use *, ?, or whole-segment ** globs"
        )
    return value


def validate_input_path(value: str) -> str:
    """A relative POSIX path, also usable for a literal file captured from a glob."""
    parts = value.removesuffix("/").split("/")
    if (
        not value.strip()
        or value.startswith("/")
        or PureWindowsPath(value).drive
        or any(char in value for char in "\\\0")
        or any(part in {"", ".", "..", ".git"} for part in parts)
    ):
        raise ValueError(
            f"[project].protected_inputs entry {value!r} must be a relative POSIX path "
            "or glob; .git and parent paths are forbidden"
        )
    return value


def protected_input_declarations(
    worktree: Path, booley_dir: Path | None = None
) -> tuple[str, ...]:
    """Read the selected checkout's effective config, without process-global memoization."""
    try:
        selected = booley_dir or resolve_checkout_booley_dir(worktree)
    except FileNotFoundError:
        return ()
    path = resolve_toml(selected)
    try:
        with path.open("rb") as stream:
            return parse_protected_inputs(tomllib.load(stream))
    except FileNotFoundError:
        return ()
