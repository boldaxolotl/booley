"""Inventory-authorized keeper release with host lifecycle coordination."""

from __future__ import annotations

import hashlib
import json
import logging
from collections.abc import Callable, Iterable
from dataclasses import asdict
from pathlib import Path

from booley.core.private_store import ProjectStoreEntry
from booley.projects import inventory
from booley.projects.image_keepers import (
    Candidate,
    KeeperRelease,
    Leftover,
    PruneResult,
    PurgeResult,
    StampPrune,
)
from booley.runtime import host_docker as docker
from booley.runtime import project_resources, session_admission
from booley.runtime.image_lifecycle import sweep_absent_owned_images
from booley.runtime.issuance_invalidation import shared_recovery_blocks_command
from booley.runtime.lifecycle_lock import host_lifecycle_lock
from booley.runtime.session_issuance import (
    invalidate_project,
    keeper_image_for_identity,
    stamp_store,
)

logger = logging.getLogger(__name__)


def _image_id(tag: str) -> str | None:
    value = docker.image_id(tag)
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


def forget_project(
    project: Path, *, purge: bool = False
) -> tuple[Path, KeeperRelease, PurgeResult | None]:
    """Validate Grants, release only this root's unused tag, then persist forgetting.

    ``purge`` also accepts an already-forgotten absolute root and then removes
    the root's unused images and state volumes, returning what it removed and kept.
    Forgetting also withdraws the root's Sandbox issuance stamp and admission
    claim, so a Session of a forgotten Project must be reissued before it runs.
    """
    outcomes: list[KeeperRelease] = []

    def release_and_withdraw(root: Path) -> None:
        # Release first: a refused release leaves the stamp, so a retry starts clean.
        outcomes.append(_release_root(root))
        invalidate_project(str(root))
        session_admission.withdraw_claims(str(root))

    try:
        with host_lifecycle_lock("projects forget"):
            _require_recovered_host()
            forgotten = inventory.forget_project(
                project,
                before_forget=release_and_withdraw,
                allow_forgotten=purge,
            )
            if purge:  # purge collects recorded images itself, after releasing tags
                purged = project_resources.purge(str(forgotten))
                with inventory.locked_protected_roots() as roots:
                    sweep_absent_owned_images(roots())
                return forgotten, outcomes[0], purged
            from booley.runtime.image_lifecycle import TAG_RETAINED, cleanup_owned_images

            for line in cleanup_owned_images(forgotten):
                # A still-tagged image is already in forget's leftover table.
                if not line.endswith(TAG_RETAINED):
                    logger.warning("forget image cleanup: %s", line)
    except (RuntimeError, OSError) as exc:
        raise inventory.ProjectInventoryError(str(exc)) from exc
    return forgotten, outcomes[0], None


def project_leftovers(project: Path) -> tuple[Leftover, ...]:
    """List what a forgotten root still holds in Docker; never mutates."""
    try:
        return project_resources.leftovers(str(project))
    except (RuntimeError, OSError, ValueError) as exc:
        raise inventory.ProjectInventoryError(
            f"forgot {project}, but could not list its leftover Docker resources: {exc}"
        ) from exc


def _protected_tags(roots: frozenset[str]) -> frozenset[str]:
    return frozenset(keeper_image_for_identity(root) for root in roots)


def _preview(roots: frozenset[str]) -> PruneResult:
    protected = _protected_tags(roots)
    containers = docker.container_image_ids_strict()
    candidates = []
    for tag in docker.issued_image_tags():
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
    # A snapshot with nothing eligible has nothing to confirm, so it carries no digest.
    digest = (
        hashlib.sha256(
            json.dumps(
                [asdict(value) for value in values], sort_keys=True, separators=(",", ":")
            ).encode()
        ).hexdigest()
        if any(value.eligible for value in values)
        else None
    )
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
    """Preview only by default; confirm exactly the rebuilt candidate digest to apply.

    A snapshot with no eligible candidate has no digest, so any confirmation is refused.
    """
    try:
        if confirm is None:
            roots = frozenset(entry.project_root for entry in inventory.project_inventory())
            return _preview(roots)
        with host_lifecycle_lock("projects prune-keepers"):
            _require_recovered_host()
            with inventory.locked_protected_roots() as roots:
                result = _preview(roots())
                if result.digest is None:
                    sweep_absent_owned_images(roots())
                    result.errors.append(
                        "nothing to release: no keeper is eligible; nothing changed"
                    )
                    return result
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


def _root_is_gone(root: str) -> bool:
    """Return whether *root* is positively absent; an unreadable path is not proof it is gone."""
    try:
        Path(root).stat()
    except (FileNotFoundError, NotADirectoryError):
        return True
    except OSError:
        return False  # EACCES, ESTALE, an unmounted drive: the Project may still exist
    return False


def orphan_stamp_roots(
    stamps: Iterable[ProjectStoreEntry], known: Iterable[str]
) -> tuple[str, ...]:
    """Sort the roots of stamps whose Project is deleted and absent from the inventory.

    Roots that still exist stay out: ``booley init --seed`` worktrees and Projects
    initialized before the inventory existed hold live stamps without being remembered.
    """
    store = stamp_store()
    remembered = {store.filename(root) for root in known}
    return tuple(
        sorted(
            entry.project_root
            for entry in stamps
            if store.filename(entry.project_root) not in remembered
            and _root_is_gone(entry.project_root)
        )
    )


def prune_stamps(roots: tuple[str, ...]) -> StampPrune:
    """List the orphan stamps; remove the stamp and claim of each named candidate.

    Runs no Docker command and leaves the Project Inventory alone. A named root that
    is not a candidate is kept untouched. Claims go first: the stamp selects the
    root, so a failed claim removal leaves it for a rerun to retry both.
    """
    try:
        with host_lifecycle_lock("projects prune-stamps"):
            _require_recovered_host()
            with inventory.locked_protected_roots() as protected:
                store = stamp_store()
                candidates = orphan_stamp_roots(store.scan_each()[0], protected())
                by_name = {store.filename(root): root for root in candidates}
                withdrawn: list[str] = []
                kept: list[str] = []
                for root in dict.fromkeys(roots):
                    candidate = by_name.get(store.filename(root))
                    if candidate is None:
                        kept.append(root)
                        continue
                    session_admission.withdraw_claims(candidate)
                    invalidate_project(candidate)
                    withdrawn.append(candidate)
                return StampPrune(candidates, tuple(withdrawn), tuple(kept))
    except (RuntimeError, OSError) as exc:
        raise inventory.ProjectInventoryError(str(exc)) from exc
