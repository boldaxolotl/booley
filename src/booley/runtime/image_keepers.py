"""Inventory-authorized keeper release with host lifecycle coordination."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Callable
from dataclasses import asdict
from pathlib import Path

from booley.projects import inventory
from booley.projects.image_keepers import Candidate, KeeperRelease, PruneResult
from booley.runtime import interactive_docker as docker
from booley.runtime.lifecycle_lock import host_lifecycle_lock
from booley.runtime.session_issuance import keeper_image_for_identity
from booley.runtime.session_refresh import shared_recovery_blocks_command


def _image_id(tag: str) -> str | None:
    value = docker.image_id_strict(tag)
    return docker.validated_image_id(value) if value is not None else None


def _release_root(project: Path) -> KeeperRelease:
    tag = keeper_image_for_identity(str(project))
    image = _image_id(tag)
    if image is None:
        return KeeperRelease("absent", tag)
    if image in docker.container_image_ids_strict():
        return KeeperRelease("retained-in-use", tag, image)
    if _image_id(tag) != image:
        raise RuntimeError("keeper changed during forget; retry")
    if image in docker.container_image_ids_strict():
        return KeeperRelease("retained-in-use", tag, image)
    docker.remove_image_tag(tag)
    return KeeperRelease("released", tag, image)


def _require_recovered_host() -> None:
    if shared_recovery_blocks_command(read_only=True):
        raise RuntimeError(
            "interrupted Sandbox host state requires recovery before keeper release"
        )


def forget_project(project: Path) -> tuple[Path, KeeperRelease]:
    """Validate Grants, release only this root's unused tag, then persist forgetting."""
    outcomes: list[KeeperRelease] = []
    try:
        with host_lifecycle_lock("projects forget"):
            _require_recovered_host()
            forgotten = inventory.forget_project(
                project, before_forget=lambda root: outcomes.append(_release_root(root))
            )
    except (RuntimeError, OSError) as exc:
        raise inventory.ProjectInventoryError(str(exc)) from exc
    return forgotten, outcomes[0]


def _protected_tags(roots: frozenset[str]) -> frozenset[str]:
    return frozenset(keeper_image_for_identity(root) for root in roots)


def _preview(roots: frozenset[str]) -> PruneResult:
    protected = _protected_tags(roots)
    containers = docker.container_image_ids_strict()
    candidates = []
    for tag in docker.issued_image_tags_strict():
        image = _image_id(tag)
        if image is None:
            raise RuntimeError("keeper disappeared during preview; retry")
        reason = (
            "inventoried"
            if tag in protected
            else "in-use"
            if image in containers
            else "inventory-absent"
        )
        candidates.append(Candidate(tag, image, reason == "inventory-absent", reason))
    values = tuple(candidates)
    digest = hashlib.sha256(
        json.dumps(
            [asdict(value) for value in values], sort_keys=True, separators=(",", ":")
        ).encode()
    ).hexdigest()
    return PruneResult(
        digest, values, retained=[value.tag for value in values if not value.eligible]
    )


def _apply(result: PruneResult, roots: Callable[[], frozenset[str]]) -> None:
    approved = [candidate for candidate in result.candidates if candidate.eligible]
    for index, candidate in enumerate(approved):
        try:
            protected = _protected_tags(roots())
            image = _image_id(candidate.tag)
            containers = docker.container_image_ids_strict()
            if candidate.tag in protected or image != candidate.image_id or image in containers:
                result.retained.append(candidate.tag)
                continue
            docker.remove_image_tag(candidate.tag)
            result.released.append(candidate.tag)
        except (RuntimeError, OSError) as exc:
            result.errors.append(f"{candidate.tag}: {exc}")
            result.retained.extend(value.tag for value in approved[index:])
            return


def prune_keepers(confirm: str | None = None) -> PruneResult:
    """Preview only by default; confirm exactly the rebuilt candidate digest to apply."""
    try:
        if confirm is None:
            roots = frozenset(entry.project_root for entry in inventory.project_inventory())
            return _preview(roots)
        with host_lifecycle_lock("projects prune-keepers"):
            _require_recovered_host()
            with inventory.locked_protected_roots() as roots:
                result = _preview(roots())
                if confirm != result.digest:
                    result.errors.append(
                        "keeper preview digest changed or confirmation is invalid; preview again"
                    )
                    return result
                _apply(result, roots)
                return result
    except (RuntimeError, OSError) as exc:
        raise inventory.ProjectInventoryError(str(exc)) from exc
