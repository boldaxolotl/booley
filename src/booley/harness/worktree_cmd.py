"""``booley worktree new <name>``: create a linked worktree with a Worktree Booley Directory.

The command wraps the packaged ``worktree_create.sh`` with its default
``refuse`` policy, so an existing destination (possibly an active worktree
holding someone's work) is never deleted. The worktree lands at
``<Booley Directory>/worktrees/<name>`` (``.booley`` unless ``[project].dir`` relocates
it inside the checkout), on a detached
HEAD at the workspace's current commit unless the name uses the
``<branch>--<description>`` convention to check out an existing branch.
A Versioned Booley Directory gets a Paired Checkout on ``booley-worktree/<name>``
at its last commit (uncommitted files are named in a warning and left out); a
non-versioned Booley Directory gets a Snapshot.
Selected git submodules (and the Paired Checkout's) are then populated offline
from the main checkout and the Project's post-setup hook runs in the new
worktree; any failure rolls the whole creation back.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

from booley.flows.execution import FlowConfigError, flow_enabled
from booley.runtime.booley_dir import BooleyDirOutsideCheckoutError, own_booley_dir
from booley.runtime.docs_links import docs_url
from booley.runtime.paths import worktree_create_script
from booley.runtime.platform_paths import bash_bin
from booley.runtime.project_prepare import find_post_setup_hook, prepare_project
from booley.runtime.project_repositories import run_git
from booley.runtime.project_worktree_pairing import (
    ProjectPairing,
    ProjectPairingError,
    pair_project_worktree,
    project_pairing_source,
    remove_creation_lock,
    rollback_outer_worktree,
    rollback_project_pairing,
    validate_project_pairing,
    worktree_removal_instructions,
)
from booley.runtime.submodule_materialization import (
    SubmoduleMaterializationError,
    materialize_worktree_submodules,
)
from booley.runtime.worktree_paths import BooleyDirMismatchError, worktree_booley_dir
from booley.runtime.worktree_snapshot import SKIPPED_PREFIX

# Same single-path-component rule the script enforces; checking it at the
# CLI boundary gives an argparse error before any process starts.
_WORKTREE_NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]*")

# Bounds the script so a wedged Git call cannot hang the CLI.
_CREATE_TIMEOUT_S = 300

# Left-out Project files named in the warning before it falls back to a count.
_LEFT_OUT_LISTED = 10


def worktree_name(value: str) -> str:
    """Argparse type: accept only a safe single path component."""
    if not _WORKTREE_NAME.fullmatch(value):
        raise argparse.ArgumentTypeError(
            f"invalid worktree name {value!r}: use letters, digits, '.', '_' or '-', "
            "starting with a letter or digit"
        )
    return value


def add_subparser(subparsers: argparse._SubParsersAction) -> None:
    """Register ``booley worktree`` and its ``new`` command."""
    parser = subparsers.add_parser(
        "worktree",
        help="Create a linked worktree with a Worktree Booley Directory",
        description="Create linked Git worktrees for this Project.",
    )
    commands = parser.add_subparsers(dest="worktree_command", metavar="{new}", required=True)
    new = commands.add_parser(
        "new",
        help="Create .booley/worktrees/<name> and print its path",
        description=(
            "Create a linked Git worktree at .booley/worktrees/<name> with a "
            "Paired Checkout on booley-worktree/<name> for a Versioned Booley Directory, "
            "or a Snapshot for a non-versioned Booley Directory "
            "(live run and session state stays behind). The outer worktree starts on a "
            "detached HEAD at the current commit; a "
            "<branch>--<description> name checks out that existing branch instead. "
            "The Paired Checkout comes from the Booley Directory repository's last "
            "commit: uncommitted and untracked files there are named in a warning and "
            "left out, and a repository with no commits is refused. "
            "Selected git submodules are populated offline from the main checkout, "
            "then the Project's post-setup hook runs in the new worktree; if either "
            "fails, the creation is rolled back. "
            "An existing destination is refused and left untouched. Prints the "
            "worktree path."
        ),
    )
    new.add_argument(
        "name",
        type=worktree_name,
        metavar="NAME",
        help="Worktree directory name (letters, digits, '.', '_', '-')",
    )


def worktree_path(project_root: Path, name: str) -> Path:
    """Return where ``booley worktree new <name>`` creates the worktree."""
    return own_booley_dir(project_root) / "worktrees" / name


class WorktreeCreationError(Exception):
    """The outer worktree script could not create the requested checkout."""


# Failures reported as one ERROR line; anything else is a defect and propagates.
_REPORTED_FAILURES = (WorktreeCreationError, ProjectPairingError, OSError)


def _create_outer(name: str, project_root: Path, *, paired_project: bool) -> None:
    script = worktree_create_script()
    if not script.is_file():
        raise WorktreeCreationError(f"worktree script not found: {script}")
    payload = {"name": name, "cwd": str(project_root), "on_existing": "refuse"}
    environment = {**os.environ}
    environment["BOOLEY_WORKTREE_PAIRED_PROJECT"] = "1" if paired_project else "0"
    environment.setdefault("BOOLEY_PYTHON", sys.executable)
    try:
        result = subprocess.run(
            [bash_bin(), str(script)],
            input=json.dumps(payload),
            capture_output=True,
            text=True,
            encoding="utf-8",
            env=environment,
            timeout=_CREATE_TIMEOUT_S,
            check=False,
        )
    except subprocess.TimeoutExpired as exc:
        raise WorktreeCreationError(
            f"worktree creation timed out after {_CREATE_TIMEOUT_S} s"
        ) from exc
    except OSError as exc:
        raise WorktreeCreationError(f"worktree creation failed to start: {exc}") from exc
    if result.returncode != 0:
        errors = [
            line.removeprefix("ERROR:").lstrip()
            for line in result.stderr.splitlines()
            if line.startswith("ERROR:")
        ]
        detail = "\n".join(errors) or result.stderr.strip() or "(no output)"
        raise WorktreeCreationError(detail)
    # Name the Booley Directory paths the Snapshot left behind for lack of a classification.
    for line in result.stderr.splitlines():
        if line.startswith(SKIPPED_PREFIX):
            print(line, file=sys.stderr)


@dataclass(frozen=True)
class _Created:
    """A worktree whose registration and submodule fill succeeded; setup remains."""

    worktree: Path
    source: Path | None
    paired: bool


def run(args: argparse.Namespace, project_root: Path) -> int:
    """Keep checkout/pair registration atomic with respect to directory migration."""
    from booley.core.layout_lock import layout_gate

    try:
        worktree_booley_dir(project_root)
    except (BooleyDirOutsideCheckoutError, BooleyDirMismatchError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1
    with layout_gate(project_root):
        created = _run_current(args, project_root)
    if isinstance(created, int):
        return created
    # The post-setup hook can take minutes, so it runs after the gate is released.
    return _finish_setup(args.name, project_root, created)


def _run_current(args: argparse.Namespace, project_root: Path) -> _Created | int:
    """Create the outer and paired checkouts atomically; print removal steps on failure."""
    worktree = worktree_path(project_root, args.name)
    created = False
    pairing: ProjectPairing | None = None
    source = None
    try:
        source = project_pairing_source(project_root)
        validate_project_pairing(source, args.name)
        _create_outer(args.name, project_root, paired_project=source is not None)
        created = True
        pairing = pair_project_worktree(project_root, worktree, args.name, source=source)
        if pairing is not None and pairing.left_out and source is not None:
            print(_left_out_warning(pairing.left_out, source), file=sys.stderr)
        print("Filling git submodules from the main checkout...", file=sys.stderr)
        _populate_submodules(worktree)
    except BaseException as exc:
        if created:
            _rollback_creation(
                project_root, worktree, args.name, exc, source if pairing is not None else None
            )
        if not isinstance(exc, _REPORTED_FAILURES):
            raise
        return _report_failure(project_root, worktree, args.name, exc, source)
    return _Created(worktree, source, paired=pairing is not None)


def _finish_setup(name: str, project_root: Path, created: _Created) -> int:
    """Run the post-setup hook; on any failure take the gate again and roll back."""
    try:
        _run_post_setup(project_root, created.worktree, name)
    except BaseException as exc:
        _rollback_creation(
            project_root,
            created.worktree,
            name,
            exc,
            created.source if created.paired else None,
            regate=True,
        )
        if not isinstance(exc, _REPORTED_FAILURES):
            raise
        return _report_failure(project_root, created.worktree, name, exc, created.source)
    print(created.worktree)
    print(_ready_notice(name, paired=created.paired), file=sys.stderr)
    return 0


def _report_failure(
    project_root: Path, worktree: Path, name: str, failure: BaseException, source: Path | None
) -> int:
    """Print a rolled-back creation's error, plus removal steps if a rollback left the pair."""
    print(f"ERROR: {failure}", file=sys.stderr)
    if source is not None and _is_named_pair(worktree, name):
        print(worktree_removal_instructions(project_root, worktree, name), file=sys.stderr)
    return 1


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
        raise WorktreeCreationError(
            f"cannot read the new worktree's Flow settings: {exc}"
        ) from exc
    result = prepare_project(worktree, worktree, slug=name, sim_flow_enabled=sim_enabled, log=log)
    if not result.ok:
        raise WorktreeCreationError(f"{result.error}; log: {log}")


def _ready_notice(name: str, *, paired: bool) -> str:
    """One calm line for a successful creation; full removal steps print only on failure."""
    kind = "Paired Checkout" if paired else "Snapshot"
    return (
        f"Created worktree {name!r} with a {kind}. "
        f"To remove it later, see {docs_url('CLI-REFERENCE.md', 'worktrees')}"
    )


def _populate_submodules(worktree: Path) -> None:
    try:
        materialize_worktree_submodules(worktree)
    except SubmoduleMaterializationError as exc:
        raise WorktreeCreationError(f"Submodule setup failed offline: {exc}") from exc


def _rollback_creation(
    root: Path,
    worktree: Path,
    name: str,
    failure: BaseException,
    paired_source: Path | None = None,
    *,
    regate: bool = False,
) -> None:
    """Undo creation: the Paired Checkout and its branch first, then the outer worktree.

    *regate* takes the layout gate for the rollback, for a caller that released
    it (the post-setup hook runs outside the gate).
    """
    from booley.core.booley_layout import BooleyLayoutError
    from booley.core.layout_lock import layout_gate

    try:
        if regate:
            with layout_gate(root):
                _undo_creation(root, worktree, name, paired_source)
        else:
            _undo_creation(root, worktree, name, paired_source)
    except (OSError, ProjectPairingError, BooleyLayoutError) as cleanup:
        failure.add_note(f"outer rollback failed: {cleanup}")
        print(f"ERROR: outer rollback failed: {cleanup}", file=sys.stderr)


def _undo_creation(root: Path, worktree: Path, name: str, paired_source: Path | None) -> None:
    if paired_source is not None:
        rollback_project_pairing(paired_source, worktree, name)
    rollback_outer_worktree(root, worktree)
    remove_creation_lock(root, name)


def _is_named_pair(worktree: Path, name: str) -> bool:
    nested = own_booley_dir(worktree)
    if not (nested / ".git").is_file():
        return False
    branch = run_git(nested, "symbolic-ref", "--short", "HEAD")
    return branch.returncode == 0 and branch.stdout.strip() == f"booley-worktree/{name}"
