"""``booley worktree rm <name> [--force]``: remove a linked worktree, keeping its evidence.

A worktree has one or two *components*: the outer worktree (registered in the
main checkout's repository) and, for a Versioned Booley Directory, the Paired
Checkout (registered in the Booley Directory's repository). Each is *intact*
(registered, present, a working Git checkout), *gone* (neither registered nor
present), or *broken* (anything else).

Without ``--force`` the command refuses to remove work that exists nowhere else
(see :func:`_busy_reasons`). Then it moves the worktree's Common Booley State and
Flow reports into ``<Main Booley Directory>/worktrees/.evidence/<name>-<time>/``,
removes the Paired Checkout, then the outer worktree, and finally deletes the
last-used stamp. Branches are kept and other registrations are never touched
(no ``git worktree prune``).

``<Main Booley Directory>/worktrees/.removing/<name>.json`` records which
removals began. It is the only proof that a *broken* component is a removal an
interruption cut short rather than a damaged worktree, so re-running finishes
it. Each removal drops the component's ``.git`` pointer first (see
:func:`detach_checkout`), so an
interruption always leaves it broken. An *intact* component is always checked
again, whatever the record says: a removal interrupted before anything was
deleted, or a stale record of an earlier worktree of the same name, never
removes work added since.
"""

from __future__ import annotations

import argparse
import filecmp
import json
import re
import subprocess
import sys
import time
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal, cast, get_args

from booley.config.goals import quiet_after
from booley.core.booley_dir import resolve_common_booley_dir, resolve_main_checkout_root
from booley.core.booley_layout import BooleyLayoutError
from booley.core.layout_lock import layout_gate
from booley.goals.store import GoalStore, GoalStoreError
from booley.harness.worktree_list import (
    Registration,
    WorktreeListError,
    count_uncommitted,
    count_unmerged,
    merge_target,
    registered_worktrees,
)
from booley.mcp.session_registry import DEAD_PROCESS_STATES, SessionRegistry, namespace
from booley.runtime.atomic_files import atomic_replace_bytes
from booley.runtime.booley_dir import own_booley_dir, own_booley_dir_repo_path
from booley.runtime.handles import HandleStore
from booley.runtime.pid import observe_process
from booley.runtime.project_repositories import run_git
from booley.runtime.project_worktree_pairing import (
    ProjectPairingError,
    detach_checkout,
    project_pairing_source,
    remove_paired_checkout,
    remove_worktree_registration,
)
from booley.runtime.session_activity import process_identity_from_session_key
from booley.runtime.submodule_materialization import SubmoduleMaterializationError, submodule_paths
from booley.runtime.worktree_evidence import tracked_booley_paths, worktree_evidence_paths

_RECORD_SCHEMA = 1

Label = Literal["outer", "paired"]
State = Literal["intact", "gone", "broken"]

_LABELS: tuple[Label, ...] = get_args(Label)
# One evidence directory per attempt: ``<name>-<UTC time>``.
_STAMP_FORMAT = "%Y%m%dT%H%M%S%fZ"
_STAMP_PATTERN = r"\d{8}T\d{12}Z"
_SUBJECT: dict[Label, str] = {"outer": "the worktree", "paired": "its Paired Checkout"}


class WorktreeRemovalRefusedError(Exception):
    """Removal would drop work that exists nowhere else; ``--force`` overrides it."""

    def __init__(self, name: str, reasons: list[str], earlier_evidence: list[Path]) -> None:
        super().__init__(f"refusing to remove worktree {name}")
        self.name = name
        self.reasons = reasons
        self.earlier_evidence = earlier_evidence


class WorktreeRemovalError(Exception):
    """Removal cannot start or continue; nothing destructive happened for this cause."""


class WorktreeRemovalBlockedError(WorktreeRemovalError):
    """The worktree's layout makes removal unsafe; fix the layout first.

    ``--force`` does not override it. Like a refusal, it keeps the worktree.
    """


@dataclass(frozen=True)
class Removal:
    """What a completed removal left behind, for the report."""

    evidence: Path | None
    kept_branches: tuple[str, ...]
    detached_head: str | None


@dataclass(frozen=True)
class _Component:
    label: Label
    path: Path
    state: State
    detail: str  # Why a broken component is broken
    registration: Registration | None


@dataclass
class _Target:
    """Everything the removal of one worktree decides from."""

    name: str
    main: Path
    common: Path
    path: Path
    source: Path | None  # The Versioned Booley Directory's repository, if any
    outer: _Component
    paired: _Component | None
    registrations: list[Registration]  # Both repositories', for the nested check
    record_path: Path
    started: list[str] = field(default_factory=lambda: [])
    record_exists: bool = False


# ---------------------------------------------------------------------------
# Locating the worktree
# ---------------------------------------------------------------------------


def _is_checkout_root(path: Path) -> bool:
    """Whether *path* is the top level of a working Git checkout, not a subdirectory of one."""
    result = run_git(path, "rev-parse", "--show-toplevel")
    return result.returncode == 0 and Path(result.stdout.strip()).resolve() == path.resolve()


def _component(label: Label, path: Path, registrations: list[Registration]) -> _Component:
    registration = next((r for r in registrations if r.path.resolve() == path.resolve()), None)
    if not path.exists():
        if registration is None:
            return _Component(label, path, "gone", "", None)
        return _Component(
            label, path, "broken", "registered, but its directory is missing", registration
        )
    if not _is_checkout_root(path):
        return _Component(label, path, "broken", "its directory has no working .git", registration)
    if registration is None:
        return _Component(
            label, path, "broken", "its directory is not a registered worktree", None
        )
    return _Component(label, path, "intact", "", registration)


def _paired_component(path: Path, registrations: list[Registration]) -> _Component | None:
    """The Paired Checkout of worktree *path*; ``None`` when its Booley Directory is a copy.

    A Snapshot made before the Booley Directory became versioned holds a plain
    copy that the outer worktree owns: no pointer, no registration.
    """
    nested = own_booley_dir(path)
    component = _component("paired", nested, registrations)
    plain_copy = component.state == "broken" and component.registration is None and nested.is_dir()
    return None if plain_copy and not (nested / ".git").exists() else component


def _read_started(record_path: Path) -> list[str] | None:
    """The components the record says began removal; ``None`` without a record.

    An unreadable record counts as one that proves nothing, which only makes the
    checks stricter.
    """
    try:
        raw = record_path.read_bytes()
    except FileNotFoundError:
        return None
    try:
        payload: object = json.loads(raw)
    except ValueError:
        return []
    if not isinstance(payload, dict):
        return []
    items = cast("dict[str, object]", payload).get("started")
    if not isinstance(items, list):
        return []
    return [
        item for item in cast("list[object]", items) if isinstance(item, str) and item in _LABELS
    ]


def _is_inside(candidate: Path, directory: Path) -> bool:
    return candidate.resolve().is_relative_to(directory.resolve())


def _locate(project_root: Path, main: Path, name: str) -> _Target:
    """Find the worktree's components; refuse a missing worktree or a self-removal."""
    common = resolve_common_booley_dir(project_root)
    path = common / "worktrees" / name
    source = project_pairing_source(main)
    outer_registrations = registered_worktrees(main)
    paired_registrations = [] if source is None else registered_worktrees(source)
    outer = _component("outer", path, outer_registrations)
    paired = None if source is None else _paired_component(path, paired_registrations)
    record_path = common / "worktrees" / ".removing" / f"{name}.json"
    started = _read_started(record_path)
    target = _Target(
        name, main, common, path, source, outer, paired,
        [*outer_registrations, *paired_registrations], record_path,
        started or [], started is not None,
    )  # fmt: skip
    gone = outer.state == "gone" and (paired is None or paired.state == "gone")
    if gone and not target.record_exists:
        raise WorktreeRemovalError(f"no worktree named {name}")
    if running_inside(project_root, path):
        raise WorktreeRemovalError(
            "run booley worktree rm from the main checkout or another worktree"
        )
    return target


def running_inside(project_root: Path, path: Path) -> bool:
    """Whether the project root or the current directory is inside worktree *path*."""
    if _is_inside(project_root, path):
        return True
    try:
        return _is_inside(Path.cwd(), path)
    except OSError:
        return False


def _refuse_nested(target: _Target) -> None:
    """Never delete another worktree's files: refuse while one is registered inside."""
    own = {target.path.resolve()}
    if target.paired is not None:
        own.add(target.paired.path.resolve())
    nested = [
        registration.path
        for registration in target.registrations
        if registration.path.resolve() not in own and _is_inside(registration.path, target.path)
    ]
    if nested:
        listed = "\n".join(f"  - {path}" for path in nested)
        raise WorktreeRemovalBlockedError(
            f"worktree {target.name} contains other worktrees:\n{listed}\n"
            "Remove them first with booley worktree rm or git worktree remove."
        )


def _refuse_foreign_booley_dir(target: _Target) -> None:
    """Never move or delete a Booley Directory that is not the worktree's own.

    A symlink to the Main Booley Directory would send the whole Project's Common
    Booley State into the evidence directory.
    """
    booley_dir = own_booley_dir(target.path)
    if not booley_dir.is_symlink() and not booley_dir.exists():
        return
    if (
        booley_dir.is_symlink()
        or not _is_inside(booley_dir, target.path)
        or booley_dir.resolve() == target.common.resolve()
    ):
        raise WorktreeRemovalBlockedError(
            f"the Booley Directory of worktree {target.name} is a link or lies outside it: "
            f"{booley_dir}\nRemove that link first; removal would otherwise touch the "
            "Main Booley Directory."
        )


def _resuming(target: _Target, component: _Component) -> bool:
    """Whether the record proves an interruption cut short *component*'s removal.

    Only a component that is no longer intact counts: an intact one may hold work
    added since, so every check and the evidence move apply to it again.
    """
    return component.label in target.started and component.state != "intact"


# ---------------------------------------------------------------------------
# Refusal check
# ---------------------------------------------------------------------------


def _session_reasons(target: _Target) -> list[str]:
    registry = SessionRegistry(target.common, quiet_after=quiet_after(target.common))
    snapshot = registry.visible_snapshot(now=time.time(), scope=namespace())
    # A project that never ran a Session has no registry directory: nothing to read.
    if registry.root.exists() and snapshot.diagnostics:
        return ["Session Registry is unreadable: " + "; ".join(snapshot.diagnostics)]
    handles, _ = HandleStore(target.common).snapshot()
    reasons: list[str] = []
    for row in snapshot.rows:
        work_dir = row.attribution.work_dir or row.attribution.default_work_dir
        if work_dir and _is_inside(Path(work_dir), target.path):
            key = row.attribution.key
            reasons.append(f"live Session {handles.get(key, key)}")
    for reason in _handle_holder_reasons(target, handles):
        if reason not in reasons:
            reasons.append(reason)
    return reasons


def _handle_holder_reasons(target: _Target, handles: dict[str, str]) -> list[str]:
    """A running agent holds the handle `S<N>` that names worktree `s<N>`.

    The agent of a worktree session may make no Booley tool call for a long time, so
    it has no registry row yet; its handle, reserved under its process key, still
    proves it lives.
    """
    reasons: list[str] = []
    for key, label in handles.items():
        identity = process_identity_from_session_key(key)
        if identity is None or label.lower() != target.name:
            continue
        if observe_process(identity).state not in DEAD_PROCESS_STATES:
            reasons.append(f"live Session {label}")
    return reasons


def _goal_reasons(target: _Target) -> list[str]:
    try:
        record = GoalStore(target.common, lock_timeout_s=0).active_for_worktree(target.path)
    except GoalStoreError as exc:
        return [f"Goal Mode state is unreadable: {exc}"]
    return [] if record is None else [f"active Goal Record {record.id}"]


def _submodule_reasons(checkout: Path, source: Path, subject: str) -> list[str]:
    """Populated submodules whose HEAD commit the source checkout's submodule lacks."""
    try:
        paths = submodule_paths(checkout)
    except SubmoduleMaterializationError as exc:
        return [f"cannot read the submodules of {subject}: {exc}"]
    reasons: list[str] = []
    for relative in paths:
        populated = checkout / relative
        if not (populated / ".git").exists():
            continue
        head = run_git(populated, "rev-parse", "HEAD").stdout.strip()
        there = source / relative
        has_commit = (there / ".git").exists() and (
            run_git(there, "cat-file", "-e", f"{head}^{{commit}}").returncode == 0
        )
        if not has_commit:
            where = relative.as_posix()
            reasons.append(
                f"submodule {where}: commit {head[:7]} is not in the main checkout's {where}"
            )
    return reasons


def _upstream_repo(target: _Target, component: _Component) -> Path:
    """The repository whose merge target the component's commits are compared with."""
    return target.main if component.label == "outer" else target.common


def _work_reasons(target: _Target, component: _Component) -> list[str]:
    """Uncommitted changes and unmerged commits of an intact component."""
    subject = _SUBJECT[component.label]
    excluded = (
        own_booley_dir_repo_path(component.path).as_posix() if component.label == "outer" else None
    )
    registration = component.registration
    assert registration is not None  # An intact component is registered.
    reasons: list[str] = []
    uncommitted = count_uncommitted(component.path, excluded)
    if uncommitted:
        reasons.append(f"{uncommitted} uncommitted change(s) in {subject}")
    upstream = merge_target(_upstream_repo(target, component))
    unmerged = count_unmerged(component.path, upstream)
    if unmerged:
        branch = (registration.branch or "detached HEAD").removeprefix("refs/heads/")
        reasons.append(
            f"{unmerged} commit(s) on {branch} not in {upstream.removeprefix('refs/heads/')}"
        )
    return reasons


def _lock_reasons(component: _Component) -> list[str]:
    """The Git lock on an intact component: a blocker, not work that removal would lose."""
    registration = component.registration
    assert registration is not None  # An intact component is registered.
    if registration.locked is None:
        return []
    subject = _SUBJECT[component.label]
    return [f"{subject} is locked by Git ({registration.locked or 'no reason given'})"]


def _loss_reasons(target: _Target, component: _Component) -> list[str]:
    """Work an intact component holds that removal would lose: changes, commits, submodules."""
    upstream_repo = _upstream_repo(target, component)
    return _work_reasons(target, component) + _submodule_reasons(
        component.path, upstream_repo, _SUBJECT[component.label]
    )


def _intact_reasons(target: _Target, component: _Component) -> list[str]:
    """Uncommitted, unmerged, locked, and submodule-only work of an intact component."""
    submodules = _submodule_reasons(
        component.path, _upstream_repo(target, component), _SUBJECT[component.label]
    )
    return _work_reasons(target, component) + _lock_reasons(component) + submodules


def _broken_reasons(target: _Target, component: _Component) -> list[str]:
    """A broken component that no recorded removal explains is damage, not a removal."""
    if component.state == "broken" and not _resuming(target, component):
        return [f"{_SUBJECT[component.label]} is not a working Git checkout: {component.detail}"]
    return []


def _component_reasons(target: _Target, component: _Component) -> list[str]:
    if component.state == "intact":
        return _intact_reasons(target, component)
    return _broken_reasons(target, component)


def _component_losses(target: _Target, component: _Component) -> list[str]:
    if component.state == "intact":
        return _loss_reasons(target, component)
    return _broken_reasons(target, component)


def _booley_dir_reasons(target: _Target) -> list[str]:
    """Untracked files of a Snapshot's Booley Directory that removal would lose.

    The outer worktree's change count skips them, because a Snapshot's copy of the
    Booley Directory is mostly untracked noise. Evidence is moved and a file equal
    to the Main Booley Directory's is a copy; anything else exists nowhere else.
    """
    if target.paired is not None or target.outer.state != "intact":
        return []
    booley_dir = own_booley_dir(target.path)
    if not booley_dir.is_dir():
        return []
    relative = booley_dir.relative_to(target.path).as_posix()
    listed = run_git(target.path, "ls-files", "-o", "--exclude-standard", "-z", "--", relative)
    if listed.returncode != 0:
        raise WorktreeRemovalError(f"cannot list the untracked files of {booley_dir}")
    saved = _evidence_entries(target)
    lost = 0
    for name in listed.stdout.split("\0"):
        file = name.removeprefix(f"{relative}/")
        if not name or any(file == entry or file.startswith(f"{entry}/") for entry in saved):
            continue
        copy = target.common / file
        if not (copy.is_file() and filecmp.cmp(booley_dir / file, copy, shallow=False)):
            lost += 1
    if not lost:
        return []
    return [f"{lost} untracked file(s) in its Booley Directory that are not evidence or copies"]


def _busy_reasons(target: _Target) -> list[str]:
    """Every reason removal would drop work that exists nowhere else.

    Fails closed: an unreadable Session Registry or Goal store is a reason. A
    Git failure while counting raises :class:`WorktreeListError` (nothing changed).
    """
    reasons: list[str] = []
    if not _resuming(target, target.outer):
        reasons += _session_reasons(target) + _goal_reasons(target)
    for component in (target.outer, target.paired):
        if component is not None:
            reasons += _component_reasons(target, component)
    return reasons + _booley_dir_reasons(target)


def discard_losses(project_root: Path, name: str) -> list[str]:
    """What removing worktree *name* would lose, in the words of ``rm``'s refusal.

    Read-only: it takes no layout lock and changes nothing. A live Session, an active
    Goal Record and a Git lock are blockers, not losses; ``remove_worktree`` reports
    them as its refusal. Raises the failures in :data:`REMOVAL_FAILURES`.
    """
    main = resolve_main_checkout_root(project_root)
    target = _locate(project_root, main, name)
    reasons: list[str] = []
    for component in (target.outer, target.paired):
        if component is not None:
            reasons += _component_losses(target, component)
    return reasons + _booley_dir_reasons(target)


def _earlier_evidence(target: _Target) -> list[Path]:
    """Evidence directories an earlier, interrupted attempt already filled."""
    if not target.record_exists:
        return []
    pattern = re.compile(f"{re.escape(target.name)}-{_STAMP_PATTERN}")
    root = _evidence_root(target.common)
    if not root.is_dir():
        return []
    return sorted(path for path in root.iterdir() if pattern.fullmatch(path.name))


# ---------------------------------------------------------------------------
# Removal
# ---------------------------------------------------------------------------


def _evidence_root(common: Path) -> Path:
    return common / "worktrees" / ".evidence"


def _write_record(target: _Target) -> None:
    payload = {"schema": _RECORD_SCHEMA, "started": target.started}
    target.record_path.parent.mkdir(parents=True, exist_ok=True)
    atomic_replace_bytes(target.record_path, (json.dumps(payload) + "\n").encode())
    target.record_exists = True


def _begin(target: _Target, label: Label) -> None:
    """Record that *label*'s removal starts, before it deletes anything."""
    if label not in target.started:
        target.started.append(label)
    _write_record(target)


def _move_entry(source: Path, destination: Path) -> None:
    """Move one evidence entry; a rename is atomic, so an entry is never half moved."""
    source.rename(destination)


def _owning_component(target: _Target) -> _Component:
    """The component whose Booley Directory holds the worktree's own state."""
    return target.outer if target.paired is None else target.paired


def _evidence_entries(target: _Target) -> list[str]:
    """Evidence of the owning Booley Directory that removal must still move.

    Paths are relative to that directory. Files Git tracks stay: they are on the
    branch the removal keeps.
    """
    owner = _owning_component(target)
    booley_dir = own_booley_dir(target.path)
    if _resuming(target, owner) or owner.state == "gone" or not booley_dir.is_dir():
        return []
    try:
        tracked = (
            tracked_booley_paths(target.path, booley_dir) if owner.state == "intact" else None
        )
        return worktree_evidence_paths(booley_dir, tracked=tracked)
    except (OSError, ValueError, subprocess.CalledProcessError) as exc:
        raise WorktreeRemovalError(f"cannot list the evidence of {target.name}: {exc}") from exc


def _move_evidence(target: _Target) -> Path | None:
    """Move the worktree's evidence into a new directory of Common Booley State.

    Every attempt gets its own directory, so earlier evidence is never overwritten.
    """
    entries = _evidence_entries(target)
    if not entries:
        return None
    booley_dir = own_booley_dir(target.path)
    stamp = datetime.now(UTC).strftime(_STAMP_FORMAT)
    destination = _evidence_root(target.common) / f"{target.name}-{stamp}"
    try:
        for entry in entries:
            (destination / entry).parent.mkdir(parents=True, exist_ok=True)
            _move_entry(booley_dir / entry, destination / entry)
    except OSError as exc:
        raise WorktreeRemovalError(
            f"cannot move the evidence of {target.name} to {destination}: {exc}; "
            "nothing was removed"
        ) from exc
    return destination


def _kept(target: _Target) -> tuple[tuple[str, ...], str | None]:
    """Branches and the detached HEAD the removal leaves reachable, read before deleting."""
    branches: list[str] = []
    detached = None
    for component in (target.paired, target.outer):
        registration = None if component is None else component.registration
        if registration is None:
            continue
        if registration.branch is not None:
            branches.append(registration.branch.removeprefix("refs/heads/"))
        elif component is target.outer:
            detached = registration.head
    return tuple(branches), detached


def _remove(target: _Target) -> Removal:
    branches, detached = _kept(target)
    _write_record(target)
    evidence = _move_evidence(target)
    if target.source is not None and target.paired and target.paired.state != "gone":
        _begin(target, "paired")
        remove_paired_checkout(target.source, target.path)
    if target.outer.state != "gone":
        _begin(target, "outer")
        detach_checkout(target.outer.path)
        remove_worktree_registration(target.main, target.path)
    (target.common / "worktrees" / ".last-used" / target.name).unlink(missing_ok=True)
    target.record_path.unlink(missing_ok=True)
    return Removal(evidence, branches, detached)


def remove_worktree(project_root: Path, name: str, *, force: bool) -> Removal:
    """Remove worktree *name*, or raise :class:`WorktreeRemovalRefusedError` / ``Error``.

    Works from the main checkout or any other worktree. ``force`` skips only the
    refusal check: nested worktrees, a Booley Directory that is a link, and a removal from
    inside the worktree are refused regardless.
    """
    main = resolve_main_checkout_root(project_root)
    with layout_gate(main):
        target = _locate(project_root, main, name)
        _refuse_nested(target)
        _refuse_foreign_booley_dir(target)
        if not force:
            reasons = _busy_reasons(target)
            if reasons:
                raise WorktreeRemovalRefusedError(name, reasons, _earlier_evidence(target))
        return _remove(target)


# ---------------------------------------------------------------------------
# Command
# ---------------------------------------------------------------------------


# Failures reported as one line; anything else is a defect and propagates.
REMOVAL_FAILURES = (
    WorktreeRemovalError,
    WorktreeListError,
    ProjectPairingError,
    BooleyLayoutError,
    OSError,
    ValueError,
)


def refusal_details(refusal: WorktreeRemovalRefusedError) -> list[str]:
    """The refusal's reasons, one bullet each, then where an earlier attempt kept evidence."""
    lines = [f"  - {reason}" for reason in refusal.reasons]
    if refusal.earlier_evidence:
        listed = ", ".join(str(path) for path in refusal.earlier_evidence)
        lines.append(f"An earlier attempt already moved evidence to: {listed}")
    return lines


def _refusal_text(refusal: WorktreeRemovalRefusedError) -> str:
    lines = [f"ERROR: refusing to remove worktree {refusal.name}:", *refusal_details(refusal)]
    lines.append("Commit or merge that work, or rerun with --force to remove it anyway.")
    return "\n".join(lines)


def report_removal(name: str, removal: Removal) -> str:
    """Render a completed removal: what was moved aside or kept."""
    lines = [f"Removed worktree {name}."]
    if removal.evidence is not None:
        lines.append(f"Evidence kept in {removal.evidence}")
    if removal.kept_branches:
        lines.append(f"Branches kept: {', '.join(removal.kept_branches)}")
    if removal.detached_head is not None:
        lines.append(f"detached HEAD was {removal.detached_head}")
    return "\n".join(lines)


def run(args: argparse.Namespace, project_root: Path) -> int:
    """Remove ``args.name``; print what was kept, or the refusal and why."""
    try:
        removal = remove_worktree(project_root, args.name, force=args.force)
    except WorktreeRemovalRefusedError as refusal:
        print(_refusal_text(refusal), file=sys.stderr)
        return 1
    except REMOVAL_FAILURES as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1
    print(report_removal(args.name, removal))
    return 0
