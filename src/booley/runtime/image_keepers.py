"""Inventory-authorized keeper release with host lifecycle coordination."""

from __future__ import annotations

import hashlib
import json
import logging
from collections.abc import Callable
from dataclasses import asdict, replace
from pathlib import Path
from typing import Literal

from booley.projects import inventory
from booley.projects.image_keepers import Candidate, KeeperRelease, PruneResult
from booley.runtime import interactive_docker as docker
from booley.runtime.lifecycle_lock import host_lifecycle_lock
from booley.runtime.session_issuance import keeper_image_for_identity
from booley.runtime.session_refresh import shared_recovery_blocks_command

logger = logging.getLogger(__name__)


def _image_id(tag: str) -> str | None:
    value = docker.image_id_strict(tag)
    return docker.validated_image_id(value) if value is not None else None


def _keeper_release_reason(tag: str, image: str, identity: str | None = None) -> str:
    from booley.runtime import image_lifecycle, project_resources

    return project_resources.alias_release_reason(
        identity, tag, image, image_lifecycle._docker_adapter()
    )


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
    reason = _keeper_release_reason(tag, image, str(project))
    if reason:
        return KeeperRelease(
            "retained-unproven" if reason.startswith("unproven") else "retained-protected",
            tag,
            image,
        )
    if _image_id(tag) != image:
        raise RuntimeError("keeper changed during forget; retry")
    docker.remove_image_tag(tag)
    return KeeperRelease("released", tag, image)


def _require_recovered_host() -> None:
    if shared_recovery_blocks_command(read_only=True):
        raise RuntimeError(
            "interrupted Sandbox host state requires recovery before keeper release"
        )


def forget_project(project: Path, *, purge: bool = False) -> tuple[Path, KeeperRelease]:
    """Inspect and clean under both lifecycle and live-Grant authority guards."""
    from booley.runtime import image_lifecycle, project_resources

    outcomes: list[KeeperRelease] = []

    def cleanup(root: Path) -> None:
        identity = str(root)
        adapter = image_lifecycle._docker_adapter()
        project_resources.snapshot(identity, docker=adapter)
        project_resources.retain_cleanup_identity(identity)
        project_resources.observe_keeper(identity, adapter)
        release = _release_root(root)
        try:
            for retained in image_lifecycle.cleanup_owned_images(root, docker=adapter):
                logger.warning("refresh image cleanup: %s", retained)
            removed = project_resources.purge(identity, docker=adapter) if purge else []
            report = project_resources.snapshot(identity, docker=adapter)
            report.removed = removed
            project_resources.compact(adapter)
            project_resources.finish_cleanup_identity(identity, report)
        except (RuntimeError, OSError) as exc:
            raise RuntimeError(f"keeper {release.status}: {release.tag}; {exc}") from exc
        outcomes.append(replace(release, resources=project_resources.report_dict(report)))

    try:
        with host_lifecycle_lock("projects forget"):
            _require_recovered_host()
            if purge and project_resources.has_cleanup_identity(str(project)):
                forgotten = inventory.with_retained_cleanup_identity(str(project), cleanup)
                # An interrupted first forget may still have an inventory entry.
                remembered = any(
                    entry.remembered and entry.project_root == str(project)
                    for entry in inventory.project_inventory()
                )
                if remembered:
                    inventory.forget_project(project)
            else:
                forgotten = inventory.forget_project(project, before_forget=cleanup)
    except (RuntimeError, OSError) as exc:
        raise inventory.ProjectInventoryError(str(exc)) from exc
    return forgotten, outcomes[0]


def _protected_tags(roots: frozenset[str]) -> frozenset[str]:
    return frozenset(keeper_image_for_identity(root) for root in roots)


def _candidate_reason(
    tag: str, image: str, protected: frozenset[str], containers: frozenset[str]
) -> Literal["inventoried", "in-use", "unproven", "protected", "inventory-absent"]:
    if tag in protected:
        return "inventoried"
    if image in containers:
        return "in-use"
    reason = _keeper_release_reason(tag, image)
    if reason:
        return "unproven" if reason.startswith("unproven") else "protected"
    return "inventory-absent"


def _preview(roots: frozenset[str]) -> PruneResult:
    protected = _protected_tags(roots)
    containers = docker.container_image_ids_strict()
    candidates = []
    for tag in docker.issued_image_tags_strict():
        image = _image_id(tag)
        if image is None:
            raise RuntimeError("keeper disappeared during preview; retry")
        reason = _candidate_reason(tag, image, protected, containers)
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
            if _keeper_release_reason(candidate.tag, candidate.image_id):
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
                from booley.runtime.image_lifecycle import cleanup_owned_images

                result.retained.extend(cleanup_owned_images())
                return result
    except (RuntimeError, OSError) as exc:
        raise inventory.ProjectInventoryError(str(exc)) from exc
