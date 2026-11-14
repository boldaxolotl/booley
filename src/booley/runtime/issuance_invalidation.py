"""Durable invalidation of host-issued Sandbox authority."""

from __future__ import annotations

import json
from pathlib import Path

from booley.core.boundary import BoundaryError, require_dict, require_str
from booley.core.private_store import PrivateStore, ProjectStore
from booley.core.user_paths import config_dir
from booley.eda.provisioning.licensing import flexnet_docker
from booley.runtime import interactive_docker
from booley.runtime.filesystem_utils import unlink_file
from booley.runtime.private_store import SESSION_STORE_REPAIR


class InvalidationError(RuntimeError):
    """An issuance invalidation journal is corrupt or cleanup is incomplete."""


def prepare(project_root: str) -> None:
    """Persist Runtime work before an EDA grant revocation starts."""
    _decode({"project_root": project_root})
    journal_store().write(project_root, json.dumps({"project_root": project_root}))


def cancel(project_root: str) -> None:
    """Remove one Project's journal once its invalidation is complete."""
    unlink_file(journal_store().path(project_root), missing_ok=True)


def has_pending(project_root: Path) -> bool:
    """Report whether one canonical Project has pending Runtime invalidation."""
    return _load(str(project_root.resolve(strict=True))) is not None


def invalidate(project_root: str) -> None:
    """Withdraw the Project's Sandbox issuance stamp."""
    from booley.runtime.session_issuance import invalidate_project

    try:
        invalidate_project(project_root)
    except OSError as exc:
        raise InvalidationError(
            f"cannot invalidate Sandbox issuance for {project_root}: {exc}"
        ) from exc


def recover_project_locked(project_root: str) -> bool:
    """Complete one pending invalidation while the host lifecycle lock is held."""
    if _load(project_root) is None:
        return False
    invalidate(project_root)
    residual = flexnet_docker.cleanup_project_resources_for_identity(
        project_root, runner=interactive_docker.relay_docker
    )
    if residual:
        raise InvalidationError(
            "EDA authority changed, but Sandbox cleanup left residual objects: "
            + ", ".join(residual)
        )
    cancel(project_root)
    return True


def recover_all_locked() -> tuple[str, ...]:
    """Complete every pending invalidation while the host lifecycle lock is held."""
    return tuple(root for root in pending_invalidations() if recover_project_locked(root))


def shared_recovery_blocks_command(*, read_only: bool) -> bool:
    """Check or complete host-wide invalidations; report whether the command must stop."""
    if read_only:
        return bool(pending_invalidations())
    return bool(recover_all_locked())


def pending_invalidations() -> tuple[str, ...]:
    """Return validated pending Project identities without changing host state."""
    return tuple(_decode(entry.document) for entry in journal_store().scan())


def _load(project_root: str) -> str | None:
    document = journal_store().read(project_root)
    return _decode(document) if document is not None else None


def _decode(raw: object) -> str:
    """Return the canonical Project identity; keys other than it are ignored."""
    try:
        project_root = require_str(
            require_dict(raw, field="issuance invalidation journal"), "project_root"
        )
    except BoundaryError as exc:
        raise InvalidationError(f"issuance invalidation journal is invalid: {exc}") from exc
    identity = Path(project_root)
    if not identity.is_absolute() or ".." in identity.parts or str(identity) != project_root:
        raise InvalidationError("issuance invalidation journal Project identity is not canonical")
    return project_root


def journal_store() -> ProjectStore:
    """The per-Project invalidation journals; Doctor scans it read-only."""
    root = config_dir() / "runtime" / "issuance-invalidations"
    private = PrivateStore(
        root,
        config_dir().parent,
        "issuance invalidation",
        InvalidationError,
    )
    return ProjectStore(private, SESSION_STORE_REPAIR)
