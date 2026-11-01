"""Durable invalidation of host-issued Sandbox authority."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

from booley.core.boundary import BoundaryError, require_dict, require_str
from booley.core.private_store import PrivateStore
from booley.core.user_paths import config_dir
from booley.eda.provisioning.licensing import flexnet_docker
from booley.runtime.filesystem_utils import unlink_file

_SUFFIX = ".json"


class InvalidationError(RuntimeError):
    """An issuance invalidation journal is corrupt or cleanup is incomplete."""


def prepare(project_root: str) -> None:
    """Persist Runtime work before an EDA grant revocation starts."""
    _decode({"project_root": project_root})
    store = _store()
    store.ensure_directory()
    store.atomic_write_text(_filename(project_root), json.dumps({"project_root": project_root}))


def cancel(project_root: str) -> None:
    """Remove one Project's journal once its invalidation is complete."""
    unlink_file(_path(project_root), missing_ok=True)


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
    residual = flexnet_docker.cleanup_project_resources_for_identity(project_root)
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


def pending_invalidations() -> tuple[str, ...]:
    """Return validated pending Project identities without changing host state."""
    store = _store()
    if not store.validate_existing_directory():
        return ()
    roots = []
    for path in sorted(store.root.iterdir()):
        if path.name.startswith("."):
            continue
        if path.is_symlink() or not path.is_file() or not path.name.endswith(_SUFFIX):
            raise InvalidationError(f"unexpected issuance invalidation journal entry: {path}")
        roots.append(_read(path))
    return tuple(roots)


def _load(project_root: str) -> str | None:
    if not _store().validate_existing_directory():
        return None
    path = _path(project_root)
    return _read(path) if path.exists() else None


def _read(path: Path) -> str:
    """Read one journal; its filename must be the hash of the identity it holds."""
    try:
        project_root = _decode(_store().read_json(path.name))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise InvalidationError(
            f"cannot read issuance invalidation journal {path}: {exc}"
        ) from exc
    if path.name != _filename(project_root):
        raise InvalidationError(f"issuance invalidation journal identity mismatch: {path}")
    return project_root


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
