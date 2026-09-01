"""Host-owned Remembered Project Root inventory."""

from __future__ import annotations

import json
import os
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path

from booley.core.boundary import BoundaryError, as_str, require_dict, require_list
from booley.eda.provisioning import authority
from booley.runtime.auth_token import config_dir
from booley.runtime.private_store import PrivateStore

SCHEMA_VERSION = 1
_STATE_FILENAME = "projects.json"
_LOCK_FILENAME = "projects.lock"


class ProjectInventoryError(RuntimeError):
    """Remembered Project Root persistence or discovery failed."""


class ProjectStatus(StrEnum):
    """Observed availability of one Remembered Project Root."""

    PRESENT = "present"
    MISSING = "missing"
    UNINITIALIZED = "uninitialized"


@dataclass(frozen=True, slots=True)
class ProjectGrantSummary:
    """One Project Grant rendered through the inventory interface."""

    kind: str
    installation: str | None
    license_profile: str | None


@dataclass(frozen=True, slots=True)
class ProjectInventoryEntry:
    """One Remembered Project Root and its joined host administration state."""

    project_root: str
    status: ProjectStatus
    remembered: bool
    grants: tuple[ProjectGrantSummary, ...]


def remember_project(project_root: Path) -> Path:
    """Remember one strictly resolved initialized Project root idempotently."""
    project = _initialized_project(project_root)
    with _locked_roots() as roots:
        roots.add(str(project))
    return project


def discover_projects(search_roots: tuple[Path, ...]) -> tuple[Path, ...]:
    """Discover initialized Projects beneath explicit roots without following symlinks."""
    if not search_roots:
        raise ProjectInventoryError("Project discovery requires at least one search root")
    discovered: set[Path] = set()
    for candidate in search_roots:
        root = _existing_search_root(candidate)
        discovered.update(_discover_under(root))
    with _locked_roots() as roots:
        roots.update(str(project) for project in discovered)
    return tuple(sorted(discovered, key=str))


def forget_project(project_root: Path) -> Path:
    """Forget one exact Remembered Project Root."""
    with _locked_roots() as roots:
        identity = _remembered_identity(project_root, roots)
        if any(grant.project_root == identity for grant in _authority_grants()):
            raise ProjectInventoryError(
                f"cannot forget {identity}: revoke its live Project Grant first"
            )
        roots.remove(identity)
    return Path(identity)


def project_inventory() -> tuple[ProjectInventoryEntry, ...]:
    """Return every Remembered Project Root in deterministic path order."""
    remembered = _load_roots()
    grants_by_root: dict[str, list[ProjectGrantSummary]] = {}
    for grant in _authority_grants():
        grants_by_root.setdefault(grant.project_root, []).append(
            ProjectGrantSummary(grant.kind, grant.installation, grant.license_profile)
        )
    roots = remembered | set(grants_by_root)
    return tuple(
        ProjectInventoryEntry(
            root,
            _status(Path(root)),
            root in remembered,
            tuple(sorted(grants_by_root.get(root, ()), key=lambda grant: grant.kind)),
        )
        for root in sorted(roots)
    )


def state_path() -> Path:
    """Return the XDG-aware private Project Inventory path."""
    return config_dir() / _STATE_FILENAME


def _store() -> PrivateStore:
    root = config_dir()
    return PrivateStore(root, root.parent, "Project Inventory", ProjectInventoryError)


def _initialized_project(project_root: Path) -> Path:
    try:
        project = project_root.resolve(strict=True)
    except OSError as exc:
        raise ProjectInventoryError(
            f"Project root is unavailable: {project_root} ({exc})"
        ) from exc
    if not project.is_dir() or not (project / ".booley_project").is_dir():
        raise ProjectInventoryError(f"not an initialized Booley Project: {project}")
    return project


def _existing_search_root(search_root: Path) -> Path:
    try:
        root = search_root.resolve(strict=True)
    except OSError as exc:
        raise ProjectInventoryError(
            f"discovery root is unavailable: {search_root} ({exc})"
        ) from exc
    if not root.is_dir():
        raise ProjectInventoryError(f"discovery root is not a directory: {root}")
    return root


def _discover_under(search_root: Path) -> set[Path]:
    discovered: set[Path] = set()
    pending = [search_root]
    while pending:
        current = pending.pop()
        if _is_initialized_checkout(current):
            discovered.add(current)
            continue
        pending.extend(_child_directories(current))
    return discovered


def _is_initialized_checkout(path: Path) -> bool:
    return (path / ".git").exists() and (path / ".booley_project").is_dir()


def _child_directories(path: Path) -> list[Path]:
    children: list[Path] = []
    try:
        with os.scandir(path) as entries:
            for entry in entries:
                if entry.name in {".git", ".booley_project"}:
                    continue
                if entry.is_dir(follow_symlinks=False):
                    children.append(Path(entry.path))
    except OSError as exc:
        raise ProjectInventoryError(f"cannot scan discovery root {path}: {exc}") from exc
    return children


def _status(project_root: Path) -> ProjectStatus:
    if not project_root.exists():
        return ProjectStatus.MISSING
    if (project_root / ".booley_project").is_dir():
        return ProjectStatus.PRESENT
    return ProjectStatus.UNINITIALIZED


def _remembered_identity(project_root: Path, roots: set[str]) -> str:
    if project_root.is_absolute():
        lexical = os.path.normpath(str(project_root))
        if lexical in roots:
            return lexical
    if project_root.exists():
        canonical = str(project_root.resolve(strict=True))
        if canonical in roots:
            return canonical
    raise ProjectInventoryError(f"Project root is not remembered: {project_root}")


def _load_roots() -> set[str]:
    store = _store()
    store.validate_existing_directory()
    path = state_path()
    if not path.exists():
        return set()
    try:
        raw = store.read_json(_STATE_FILENAME)
        return _decode_roots(raw)
    except (OSError, UnicodeDecodeError, json.JSONDecodeError, BoundaryError) as exc:
        raise ProjectInventoryError(f"Project Inventory is unreadable: {exc}") from exc


def _decode_roots(raw: object) -> set[str]:
    document = require_dict(raw, field="Project Inventory")
    if set(document) != {"schema", "projects"}:
        raise BoundaryError("Project Inventory has an invalid top-level schema")
    if document["schema"] != SCHEMA_VERSION:
        raise BoundaryError("Project Inventory has an unsupported schema")
    values = require_list(document["projects"], field="Project Inventory projects")
    roots: list[str] = []
    for index, value in enumerate(values):
        root = as_str(value)
        if root is None:
            raise BoundaryError(f"Project Inventory projects[{index}] must be a string")
        if not Path(root).is_absolute() or os.path.normpath(root) != root:
            raise BoundaryError(f"Project Inventory projects[{index}] must be an absolute root")
        roots.append(root)
    if len(roots) != len(set(roots)):
        raise BoundaryError("Project Inventory contains a duplicate root")
    return set(roots)


def _authority_grants() -> tuple[authority.ProjectGrant, ...]:
    try:
        return authority.load_state().grants
    except authority.AuthorityError as exc:
        raise ProjectInventoryError(f"cannot read Project Grants: {exc}") from exc


@contextmanager
def _locked_roots() -> Iterator[set[str]]:
    store = _store()
    store.ensure_directory()
    with store.locked(
        _LOCK_FILENAME,
        busy_message="Project Inventory is busy with another operation; retry later",
    ):
        roots = _load_roots()
        yield roots
        payload = json.dumps(
            {"schema": SCHEMA_VERSION, "projects": sorted(roots)},
            indent=2,
            sort_keys=True,
        )
        store.atomic_write_text(_STATE_FILENAME, payload + "\n")
