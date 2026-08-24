"""Pure ownership planning for initialization filesystem mutations."""

from __future__ import annotations

import hashlib
import os
import stat
from collections.abc import Callable
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import Protocol


class Ownership(Enum):
    """Why an initialization target may or may not be changed."""

    ABSENT = "absent"
    MANAGED_CURRENT = "managed_current"
    MANAGED_STALE = "managed_stale"
    USER_OWNED = "user_owned"
    TRACKED_MATCH = "tracked_match"
    CONFLICT = "conflict"


class FilesystemMutation(Enum):
    """A filesystem operation proposed by an initialization plan."""

    NONE = "none"
    WRITE_FILE = "write_file"
    LINK_FILE = "link_file"


class NodeKind(Enum):
    """A no-follow filesystem node classification."""

    ABSENT = "absent"
    FILE = "file"
    SYMLINK = "symlink"
    DIRECTORY = "directory"
    OTHER = "other"


@dataclass(frozen=True)
class ObservedPrecondition:
    """The exact node state that must still hold immediately before apply."""

    kind: NodeKind
    digest: str = ""
    link_target: str = ""
    mode: int = 0
    tracked: bool = False


@dataclass(frozen=True)
class InitTarget:
    """One desired filesystem endpoint supplied to the pure planner."""

    path: Path
    mutation: FilesystemMutation
    reason: str
    content: bytes = b""
    owner_marker: bytes | None = None
    desired_identity: Path | None = None
    link_target: Path | None = None
    recognized_legacy_digests: tuple[str, ...] = ()
    recognized_legacy_link_targets: tuple[str, ...] = ()

    @classmethod
    def write_file(
        cls,
        path: Path,
        content: bytes,
        *,
        owner_marker: bytes | None,
        reason: str,
        recognized_legacy_digests: tuple[str, ...] = (),
    ) -> InitTarget:
        return cls(
            path=path,
            mutation=FilesystemMutation.WRITE_FILE,
            reason=reason,
            content=content,
            owner_marker=owner_marker,
            recognized_legacy_digests=recognized_legacy_digests,
        )

    @classmethod
    def link_file(
        cls,
        path: Path,
        desired_identity: Path,
        link_target: Path,
        *,
        reason: str,
        recognized_legacy_digests: tuple[str, ...] = (),
        recognized_legacy_link_targets: tuple[str, ...] = (),
    ) -> InitTarget:
        return cls(
            path=path,
            mutation=FilesystemMutation.LINK_FILE,
            reason=reason,
            desired_identity=desired_identity,
            link_target=link_target,
            recognized_legacy_digests=recognized_legacy_digests,
            recognized_legacy_link_targets=recognized_legacy_link_targets,
        )


@dataclass(frozen=True)
class InitFilesystemRequest:
    """The complete filesystem decision set for one initialization phase."""

    root: Path
    targets: tuple[InitTarget, ...]


@dataclass(frozen=True)
class InitAction:
    """One classified target and its proposed mutation."""

    target: InitTarget
    ownership: Ownership
    observed: ObservedPrecondition
    mutation: FilesystemMutation
    reason: str
    blocker: str = ""

    @property
    def path(self) -> Path:
        return self.target.path


class HostProbe(Protocol):
    """Read-only host facts that are not intrinsic filesystem metadata."""

    def is_tracked(self, root: Path, path: Path) -> bool: ...


@dataclass(frozen=True)
class InitPlan:
    """A complete read-only decision set ready for validation and application."""

    request: InitFilesystemRequest
    actions: tuple[InitAction, ...]
    host_probe: HostProbe

    @property
    def blockers(self) -> tuple[InitAction, ...]:
        return tuple(action for action in self.actions if action.blocker)


class InitPlanBlockedError(RuntimeError):
    """The plan contains user-owned or conflicting targets."""


class InitPreconditionError(RuntimeError):
    """A target changed between inspection and application."""


ActionApplier = Callable[[InitAction], None]


def plan_init_filesystem(
    request: InitFilesystemRequest,
    host_probe: HostProbe,
) -> InitPlan:
    """Classify every target without mutating the filesystem."""
    root = request.root.resolve()
    actions: list[InitAction] = []
    seen: set[Path] = set()
    for target in request.targets:
        path = target.path.parent.resolve(strict=False) / target.path.name
        if path in seen:
            raise ValueError(f"duplicate initialization target: {path}")
        seen.add(path)
        if not path.is_relative_to(root):
            raise ValueError(f"initialization target is outside project root: {path}")
        observed = _observe(root, path, host_probe)
        actions.append(_classify(target, observed))
    return InitPlan(request=request, actions=tuple(actions), host_probe=host_probe)


def apply_init_plan(plan: InitPlan, apply_action: ActionApplier | None = None) -> None:
    """Revalidate the complete plan, then apply its non-blocked mutations."""
    if plan.blockers:
        details = "; ".join(f"{action.path}: {action.blocker}" for action in plan.blockers)
        raise InitPlanBlockedError(details)
    root = plan.request.root.resolve()
    for action in plan.actions:
        current = _observe(root, action.path, plan.host_probe)
        if current != action.observed:
            raise InitPreconditionError(
                f"initialization target changed after planning: {action.path}"
            )
    applier = apply_action or _apply_action
    for action in plan.actions:
        if action.mutation is not FilesystemMutation.NONE:
            applier(action)


def _classify(target: InitTarget, observed: ObservedPrecondition) -> InitAction:
    if observed.kind is NodeKind.ABSENT:
        return InitAction(
            target,
            Ownership.ABSENT,
            observed,
            target.mutation,
            target.reason,
        )
    if target.mutation is FilesystemMutation.WRITE_FILE:
        return _classify_write(target, observed)
    if target.mutation is FilesystemMutation.LINK_FILE:
        return _classify_link(target, observed)
    raise ValueError(f"unsupported desired mutation: {target.mutation.value}")


def _classify_write(target: InitTarget, observed: ObservedPrecondition) -> InitAction:
    desired_digest = _digest(target.content)
    if observed.tracked:
        ownership = (
            Ownership.TRACKED_MATCH if observed.digest == desired_digest else Ownership.CONFLICT
        )
        blocker = "" if ownership is Ownership.TRACKED_MATCH else "tracked file differs"
        return InitAction(
            target,
            ownership,
            observed,
            FilesystemMutation.NONE,
            target.reason,
            blocker,
        )
    if observed.kind is NodeKind.FILE and observed.digest == desired_digest:
        return InitAction(
            target,
            Ownership.MANAGED_CURRENT,
            observed,
            FilesystemMutation.NONE,
            target.reason,
        )
    if observed.kind is NodeKind.FILE and target.owner_marker is not None:
        try:
            existing = target.path.read_bytes()
            managed = _digest(existing) == observed.digest and target.owner_marker in existing
        except OSError:
            managed = False
        if managed:
            return InitAction(
                target,
                Ownership.MANAGED_STALE,
                observed,
                target.mutation,
                target.reason,
            )
    if observed.kind is NodeKind.FILE and observed.digest in target.recognized_legacy_digests:
        return InitAction(
            target,
            Ownership.MANAGED_STALE,
            observed,
            target.mutation,
            target.reason,
        )
    return InitAction(
        target,
        Ownership.USER_OWNED,
        observed,
        FilesystemMutation.NONE,
        target.reason,
        "existing entry is not recognized as Booley-managed",
    )


def _classify_link(target: InitTarget, observed: ObservedPrecondition) -> InitAction:
    assert target.desired_identity is not None
    if _points_to(target.path, target.desired_identity):
        return InitAction(
            target,
            Ownership.MANAGED_CURRENT,
            observed,
            FilesystemMutation.NONE,
            target.reason,
        )
    identity_digest = _file_digest(target.desired_identity)
    if observed.tracked:
        ownership = (
            Ownership.TRACKED_MATCH
            if observed.kind is NodeKind.FILE and observed.digest == identity_digest
            else Ownership.CONFLICT
        )
        blocker = "" if ownership is Ownership.TRACKED_MATCH else "tracked file differs"
        return InitAction(
            target,
            ownership,
            observed,
            FilesystemMutation.NONE,
            target.reason,
            blocker,
        )
    recognized_link = (
        observed.kind is NodeKind.SYMLINK
        and observed.link_target in target.recognized_legacy_link_targets
    )
    if recognized_link or observed.digest in target.recognized_legacy_digests:
        return InitAction(
            target,
            Ownership.MANAGED_STALE,
            observed,
            target.mutation,
            target.reason,
        )
    return InitAction(
        target,
        Ownership.USER_OWNED,
        observed,
        FilesystemMutation.NONE,
        target.reason,
        "foreign untracked guidance entry",
    )


def _observe(root: Path, path: Path, host_probe: HostProbe) -> ObservedPrecondition:
    tracked = host_probe.is_tracked(root, path)
    try:
        metadata = path.lstat()
    except FileNotFoundError:
        return ObservedPrecondition(NodeKind.ABSENT, tracked=tracked)
    mode = stat.S_IMODE(metadata.st_mode)
    if stat.S_ISLNK(metadata.st_mode):
        return ObservedPrecondition(
            NodeKind.SYMLINK,
            link_target=str(path.readlink()),
            mode=mode,
            tracked=tracked,
        )
    if stat.S_ISREG(metadata.st_mode):
        return ObservedPrecondition(
            NodeKind.FILE,
            digest=_file_digest(path),
            mode=mode,
            tracked=tracked,
        )
    if stat.S_ISDIR(metadata.st_mode):
        return ObservedPrecondition(NodeKind.DIRECTORY, mode=mode, tracked=tracked)
    return ObservedPrecondition(NodeKind.OTHER, mode=mode, tracked=tracked)


def _apply_action(action: InitAction) -> None:
    target = action.target
    target.path.parent.mkdir(parents=True, exist_ok=True)
    if action.mutation is FilesystemMutation.WRITE_FILE:
        target.path.write_bytes(target.content)
        return
    if action.mutation is FilesystemMutation.LINK_FILE:
        assert target.link_target is not None
        if target.path.is_symlink() or target.path.exists():
            target.path.unlink()
        target.path.symlink_to(os.path.relpath(target.link_target, target.path.parent))
        return
    raise ValueError(f"unsupported filesystem mutation: {action.mutation.value}")


def _points_to(path: Path, identity: Path) -> bool:
    try:
        if path.resolve(strict=True) == identity.resolve(strict=True):
            return True
    except OSError:
        return False
    try:
        return path.samefile(identity)
    except OSError:
        return False


def _digest(content: bytes) -> str:
    return hashlib.sha256(content).hexdigest()


def _file_digest(path: Path) -> str:
    try:
        return _digest(path.read_bytes())
    except OSError:
        return ""
