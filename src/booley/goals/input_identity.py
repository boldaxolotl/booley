"""Physical input-root authority and proven relative mapping across mount spellings."""

from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path
from typing import Any

from booley.core.boundary import require_dict, require_int, require_list, require_str_value
from booley.runtime.git import git_common_dir
from booley.runtime.project_dir import contains, resolve_checkout_project_dir
from booley.runtime.project_repositories import paired_project_repository


class InputIdentityError(ValueError):
    """A previously observed input root is unavailable or physically substituted."""


def directory_identity(path: Path) -> list[int]:
    """Directory identities bind roots; identical leaf hardlinks never grant membership."""
    if not path.is_dir():
        raise InputIdentityError(f"input root is not a directory: {path}")
    metadata = path.stat()
    return [metadata.st_dev, metadata.st_ino]


def root_bindings(roots: Mapping[str, Path]) -> dict[str, Any]:
    """Retain spelling only as a logical mapping, alongside physical root authority."""
    return {
        name: {"path": str(path), "identity": directory_identity(path)}
        for name, path in roots.items()
    }


def parse_bindings(raw: object) -> dict[str, Any]:
    """A stored root is an absolute logical anchor plus a two-integer physical ID."""
    values = require_dict(raw, field="input path bindings")
    if not {"rtl", "project"} <= values.keys() or values.keys() - {
        "rtl",
        "project",
        "control",
        "main",
        "paired",
    }:
        raise ValueError("invalid input path binding names")
    for name, raw_value in values.items():
        value = require_dict(raw_value, field=name + " binding")
        if (
            set(value) != {"path", "identity"}
            or not Path(require_str_value(value["path"], field=name + " path")).is_absolute()
        ):
            raise ValueError("invalid logical input root")
        identity = require_list(value["identity"], field=name + " identity")
        if len(identity) != 2:
            raise ValueError("invalid physical input root identity")
        for item in identity:
            if require_int(item, field=name + " identity") < 0:
                raise ValueError("invalid physical input root identity")
    return values


def require_bindings(saved: Mapping[str, Any], current: Mapping[str, Any]) -> None:
    """Different spellings are allowed only for the exact same physical participant set."""
    parse_bindings(dict(saved))
    parse_bindings(dict(current))
    if saved.keys() != current.keys() or any(
        saved[key]["identity"] != current[key]["identity"] for key in saved
    ):
        raise InputIdentityError("input path topology/physical identity was substituted")


def rebase_path(path: Path, saved: Mapping[str, Any], current: Mapping[str, Any]) -> Path:
    """Map a saved logical suffix under its proven current physical root."""
    require_bindings(saved, current)
    for name in sorted(saved, key=lambda name: len(Path(saved[name]["path"]).parts), reverse=True):
        old, new = Path(saved[name]["path"]), Path(current[name]["path"])
        if path.is_relative_to(old):
            return new / path.relative_to(old)
    return current_path(path, current)


def current_path(path: Path, current: Mapping[str, Any]) -> Path:
    """Select the innermost proven physical root, including a missing descendant."""
    for name in sorted(
        current, key=lambda name: len(Path(current[name]["path"]).parts), reverse=True
    ):
        root = Path(current[name]["path"])
        mapped = contains(path, project_dir=root)
        if mapped is not None:
            return mapped
    return path


def logical_roots(saved: Mapping[str, Any], current: Mapping[str, Any]) -> dict[Path, Path]:
    """Fingerprint labels retain entry authority while reading the current mount."""
    require_bindings(saved, current)
    # Input roles own fingerprint labels when control/administrative aliases coalesce.
    # Preserve the first role rather than overwriting Project with its control alias.
    labels: dict[Path, Path] = {}
    for key in ("rtl", "project", "paired", "control", "main"):
        if key in saved:
            labels.setdefault(Path(current[key]["path"]), Path(saved[key]["path"]))
    return labels


def tests_label(saved: Mapping[str, Any]) -> str:
    """The Project tests label is relative only to the entry RTL spelling."""
    rtl = Path(saved["rtl"]["path"])
    tests = Path(saved["project"]["path"]) / "tests.toml"
    return (tests.relative_to(rtl) if tests.is_relative_to(rtl) else tests).as_posix()


def capture_roots(root: Path, control_project: Path) -> dict[str, Any]:
    """The input and control roots, including Git's actual primary checkout."""
    roots = {
        "rtl": root,
        "project": resolve_checkout_project_dir(root).resolve(),
        "control": control_project,
    }
    common = git_common_dir(root)
    if common.name == ".git":
        roots["main"] = common.parent
    paired = paired_project_repository(root)
    if paired is not None:
        roots["paired"] = paired.worktree
    return root_bindings(roots)
