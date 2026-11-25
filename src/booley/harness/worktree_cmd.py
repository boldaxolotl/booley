"""``booley worktree prepare [PATH]``: prepare a linked worktree made by Git or T3 Code.

The worktree gets a Booley Directory, Git settings, and the post-setup hook, and the
run undoes what it created when a step fails. A Versioned Booley Directory gets a Paired
Checkout on ``booley-worktree/<folder>`` at its last commit (uncommitted files are named in
a warning and left out); a non-versioned Booley Directory gets a Snapshot.
"""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

from booley.core.booley_layout import BooleyLayoutError
from booley.flows.execution import FlowConfigError, flow_enabled
from booley.runtime.booley_dir import own_booley_dir
from booley.runtime.project_prepare import find_post_setup_hook, prepare_project
from booley.runtime.project_worktree_pairing import (
    ProjectPairingError,
    project_pairing_source,
    prune_missing_checkouts,
)
from booley.runtime.worktree_paths import worktree_booley_dir
from booley.runtime.worktree_prepare import (
    PrepareTarget,
    WorktreeBooleyDir,
    WorktreePrepareError,
    check_booley_dir,
    give_booley_dir,
    prepare_target,
    remove_booley_dir,
)
from booley.runtime.worktree_setup import (
    WorktreeSetupError,
    configure_worktree_git,
    copy_root_fusesoc_ignore,
    mark_fusesoc_ignore,
)
from booley.runtime.worktree_snapshot import SnapshotError, skipped_line

# Left-out Project files named in the warning before it falls back to a count.
_LEFT_OUT_LISTED = 10


def add_subparser(subparsers: argparse._SubParsersAction) -> None:
    """Register ``booley worktree`` with its ``prepare`` command."""
    parser = subparsers.add_parser(
        "worktree",
        help="Prepare a linked worktree made with Git or T3 Code",
        description="Prepare a linked Git worktree for this Project.",
    )
    commands = parser.add_subparsers(dest="worktree_command", metavar="{prepare}", required=True)
    _add_prepare_parser(commands)


def _add_prepare_parser(commands: argparse._SubParsersAction) -> None:
    prepare = commands.add_parser(
        "prepare",
        help="Give a worktree made with Git or T3 Code its own Booley Directory",
        description=(
            "Prepare the linked worktree at PATH (default: the current directory), made "
            "by Git or T3 Code. It gets a Paired Checkout on booley-worktree/<folder> from the Booley Directory repository's "
            "last commit (uncommitted files are named in a warning and left out), or a "
            "Snapshot, or nothing when Git tracks the Booley Directory. It also prunes "
            "the Booley Directory repository's registrations whose folders are gone, "
            "applies the worktree Git settings, and runs the post-setup hook. "
            "It does nothing outside a Booley Project and refuses the main checkout and "
            "a Booley Directory outside the checkout. If a step fails, the run deletes what it "
            "created. A second run creates nothing new. Submodules are not filled."
        ),
    )
    prepare.add_argument(
        "path",
        nargs="?",
        type=Path,
        metavar="PATH",
        help="a folder in the linked worktree (default: the current directory)",
    )


class WorktreePostSetupError(Exception):
    """The post-setup step failed."""


class WorktreeMountRepairError(Exception):
    """A worktree registration naming a former mount point cannot be repaired."""


def _repair_mount_registrations(project_root: Path) -> None:
    """Repair registrations naming a former mount before a prune can delete them."""
    from booley.runtime.mount_history import MountHistoryError
    from booley.runtime.worktree_mount_repair import repair_mount_registrations

    try:
        result = repair_mount_registrations(project_root)
    except MountHistoryError as exc:
        raise WorktreeMountRepairError(
            f"cannot check worktree registrations after a Sandbox mount point change: {exc}"
        ) from exc
    if result.problems:
        raise WorktreeMountRepairError(
            "worktree registrations name a former Sandbox mount point and cannot be repaired; "
            "nothing was created:\n" + "\n".join(p.message() for p in result.problems)
        )


def run(args: argparse.Namespace, project_root: Path) -> int:
    """Dispatch ``prepare``."""
    return _prepare(args.path or project_root)


def _left_out_warning(paths: tuple[str, ...], source: Path) -> str:
    """One warning naming the first uncommitted Project files the Paired Checkout lacks."""
    listed = ", ".join(paths[:_LEFT_OUT_LISTED])
    if len(paths) > _LEFT_OUT_LISTED:
        listed += f", and {len(paths) - _LEFT_OUT_LISTED} more"
    return (
        f"WARNING: the Paired Checkout was made from the last commit of {source.name}; "
        f"{len(paths)} uncommitted or untracked file(s) there were left out: {listed}. "
        f"Commit them in {source.name} to include them in later worktrees."
    )


def _run_post_setup(project_root: Path, worktree: Path, name: str) -> None:
    """Run the Project's post-setup hook in *worktree*, logging its output."""
    hook = find_post_setup_hook(worktree)
    if hook is None:
        return
    log = own_booley_dir(project_root) / "worktrees" / ".logs" / f"{name}.log"
    print(
        f"Running post-setup hook {os.path.relpath(hook, worktree)} (log: {log})...",
        file=sys.stderr,
    )
    try:
        sim_enabled = flow_enabled("sim", worktree)
    except FlowConfigError as exc:
        raise WorktreePostSetupError(f"cannot read the worktree's Flow settings: {exc}") from exc
    result = prepare_project(worktree, worktree, slug=name, sim_flow_enabled=sim_enabled, log=log)
    if not result.ok:
        raise WorktreePostSetupError(f"{result.error}; log: {log}")


# Failures reported as one ERROR line; anything else is a defect and propagates.
_PREPARE_FAILURES = (
    WorktreePostSetupError,
    WorktreeMountRepairError,
    ProjectPairingError,
    WorktreeSetupError,
    OSError,
    WorktreePrepareError,
    SnapshotError,
    BooleyLayoutError,
    ValueError,
)


def _prepare(path: Path) -> int:
    """``booley worktree prepare``: set up the linked worktree at *path*; 0 also outside a Project."""
    from booley.core.layout_lock import layout_gate

    try:
        target = prepare_target(path)
        if target is None:
            print("not in a Booley Project; nothing to prepare", file=sys.stderr)
            return 0
        worktree_booley_dir(target.main)
        with layout_gate(target.main):
            made, source = _prepare_locked(target)
    except _PREPARE_FAILURES as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1
    for skipped in made.skipped:
        print(skipped_line(skipped), file=sys.stderr)
    if made.left_out and source is not None:
        print(_left_out_warning(made.left_out, source), file=sys.stderr)
    try:
        _run_post_setup(target.main, target.worktree, target.name)
    except BaseException as exc:
        _undo_prepare(target, made, source, exc)
        if not isinstance(exc, _PREPARE_FAILURES):
            raise
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1
    kept = "" if made.created else " (kept)"
    print(f"Prepared worktree {target.name!r}: {made.kind}{kept}", file=sys.stderr)
    return 0


def _prepare_locked(target: PrepareTarget) -> tuple[WorktreeBooleyDir, Path | None]:
    """Run the prepare steps that need the layout gate; return the Booley Directory and its source."""
    main_dir = own_booley_dir(target.main)
    _repair_mount_registrations(target.main)
    source = project_pairing_source(target.main)
    check_booley_dir(target, source)  # refuse before anything is written
    if source is not None:
        prune_missing_checkouts(source)
    mark_fusesoc_ignore(main_dir, paired=source is not None)
    configure_worktree_git(target.main, target.worktree, booley_dir=main_dir)
    copy_root_fusesoc_ignore(target.main, target.worktree)
    return give_booley_dir(target, source), source


def _undo_prepare(
    target: PrepareTarget, made: WorktreeBooleyDir, source: Path | None, failure: BaseException
) -> None:
    """Remove what the run created after the hook failed; the post-setup hook runs outside the gate."""
    from booley.core.layout_lock import layout_gate

    try:
        with layout_gate(target.main):
            remove_booley_dir(target, made, source)
    except _PREPARE_FAILURES as cleanup:
        failure.add_note(f"rollback failed: {cleanup}")
        print(f"ERROR: rollback failed: {cleanup}", file=sys.stderr)
