"""Durable invalidation of host-issued Session Runtime authority."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Callable
from contextlib import AbstractContextManager
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import TypeVar

from booley.runtime.auth_token import config_dir
from booley.runtime.private_store import PrivateStore

_SCHEMA_VERSION = 1
_SUFFIX = ".json"
T = TypeVar("T")
CleanupResources = Callable[[str], tuple[str, ...]]


class InvalidationError(RuntimeError):
    """An issuance invalidation journal is corrupt or cleanup is incomplete."""


@dataclass(frozen=True, slots=True)
class PendingInvalidation:
    """One authority mutation whose Runtime effects must be completed."""

    schema_version: int
    project_root: str
    cleanup_resources: bool


def prepare(project_root: str, *, cleanup_resources: bool) -> PendingInvalidation:
    """Persist Runtime work before an EDA grant mutation starts."""
    pending = PendingInvalidation(_SCHEMA_VERSION, project_root, cleanup_resources)
    store = _store()
    store.ensure_directory()
    store.atomic_write_text(_filename(project_root), json.dumps(asdict(pending), sort_keys=True))
    return pending


def cancel(pending: PendingInvalidation) -> None:
    """Remove work prepared for an authority mutation that did not commit."""
    _path(pending.project_root).unlink(missing_ok=True)


def has_pending(project_root: Path) -> bool:
    """Report whether one canonical Project has pending Runtime invalidation."""
    return _load(str(project_root.resolve(strict=True))) is not None


def recover_project_locked(
    project_root: str,
    *,
    cleanup_resources: CleanupResources,
) -> bool:
    """Complete one pending invalidation while the host lifecycle lock is held."""
    pending = _load(project_root)
    if pending is None:
        return False
    from booley.runtime.session_issuance import invalidate_project

    try:
        invalidate_project(pending.project_root)
    except OSError as exc:
        raise InvalidationError(
            f"cannot invalidate Session Runtime issuance for {pending.project_root}: {exc}"
        ) from exc
    if pending.cleanup_resources:
        residual = cleanup_resources(pending.project_root)
        if residual:
            raise InvalidationError(
                "EDA authority changed, but Session Runtime cleanup left residual objects: "
                + ", ".join(residual)
            )
    cancel(pending)
    return True


def recover_all_locked(*, cleanup_resources: CleanupResources) -> tuple[str, ...]:
    """Complete every pending invalidation while the host lifecycle lock is held."""
    recovered = []
    for pending in pending_invalidations():
        if recover_project_locked(
            pending.project_root,
            cleanup_resources=cleanup_resources,
        ):
            recovered.append(pending.project_root)
    return tuple(recovered)


def coordinate_mutation(
    *,
    operation: str,
    resolve_project_identity: Callable[[], str],
    mutation: Callable[[str], AbstractContextManager[Callable[[], T]]],
    cleanup_resources: CleanupResources,
    invalidate_before_mutation: bool,
    cleanup_after_mutation: bool,
) -> T:
    """Run one authority mutation inside Runtime's durable lifecycle transaction."""
    from booley.runtime.lifecycle_lock import host_lifecycle_lock
    from booley.runtime.session_issuance import invalidate_project
    from booley.runtime.session_refresh import shared_recovery_blocks_command

    with host_lifecycle_lock(operation):
        shared_recovery_blocks_command(
            read_only=False,
            cleanup_resources=cleanup_resources,
        )
        project_identity = resolve_project_identity()
        with mutation(project_identity) as commit:
            prepare(
                project_identity,
                cleanup_resources=cleanup_after_mutation,
            )
            if invalidate_before_mutation:
                try:
                    invalidate_project(project_identity)
                except OSError as exc:
                    raise InvalidationError(
                        f"cannot invalidate Session Runtime issuance for {project_identity}: {exc}"
                    ) from exc
            result = commit()
        recover_project_locked(
            project_identity,
            cleanup_resources=cleanup_resources,
        )
        return result


def pending_invalidations() -> tuple[PendingInvalidation, ...]:
    """Return validated pending invalidations without changing host state."""
    store = _store()
    if not store.validate_existing_directory():
        return ()
    pending = []
    for path in sorted(store.root.iterdir()):
        if path.name.startswith("."):
            continue
        if path.is_symlink() or not path.is_file() or not path.name.endswith(_SUFFIX):
            raise InvalidationError(f"unexpected issuance invalidation journal entry: {path}")
        try:
            raw = store.read_json(path.name)
        except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise InvalidationError(
                f"cannot read issuance invalidation journal {path}: {exc}"
            ) from exc
        item = _decode(raw)
        if path.name != _filename(item.project_root):
            raise InvalidationError(f"issuance invalidation journal identity mismatch: {path}")
        pending.append(item)
    return tuple(pending)


def _load(project_root: str) -> PendingInvalidation | None:
    store = _store()
    if not store.validate_existing_directory():
        return None
    path = _path(project_root)
    if not path.exists():
        return None
    try:
        return _decode(store.read_json(path.name))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise InvalidationError(
            f"cannot read issuance invalidation journal {path}: {exc}"
        ) from exc


def _decode(raw: object) -> PendingInvalidation:
    if not isinstance(raw, dict) or set(raw) != {
        "schema_version",
        "project_root",
        "cleanup_resources",
    }:
        raise InvalidationError("issuance invalidation journal has an invalid shape")
    if raw["schema_version"] != _SCHEMA_VERSION:
        raise InvalidationError("issuance invalidation journal has an unsupported schema")
    if not isinstance(raw["project_root"], str) or not raw["project_root"]:
        raise InvalidationError("issuance invalidation journal has an invalid Project identity")
    if not isinstance(raw["cleanup_resources"], bool):
        raise InvalidationError("issuance invalidation journal has an invalid cleanup policy")
    return PendingInvalidation(
        raw["schema_version"], raw["project_root"], raw["cleanup_resources"]
    )


def _store() -> PrivateStore:
    root = config_dir() / "runtime" / "issuance-invalidations"
    return PrivateStore(
        root,
        config_dir().parent,
        "issuance invalidation",
        InvalidationError,
    )


def _filename(project_root: str) -> str:
    return hashlib.sha256(project_root.encode()).hexdigest() + _SUFFIX


def _path(project_root: str) -> Path:
    return _store().root / _filename(project_root)
