"""``booley worktree new <name>``: create a linked worktree with a Worktree Booley Directory.

``booley worktree prepare [PATH]`` gives a linked worktree made by Git or T3 Code the
same Booley Directory, Git settings, and post-setup hook, and undoes what it created
when a step fails.

The packaged ``worktree_create.sh`` creates the outer worktree with its default
``refuse`` policy, so an existing destination (possibly an active worktree
holding someone's work) is never deleted; Python then sets it up (Git config,
Snapshot, markers). The worktree lands at
``<Booley Directory>/worktrees/<name>`` (``.booley`` unless ``[project].dir`` relocates
it inside the checkout), on a new branch
``booley/<name>`` (``wt/<name>`` in Stealth Mode) at the workspace's current commit
(a branch occupying that name is refused), unless the name uses the ``<branch>--<description>`` convention
to check out an existing branch instead. ``booley worktree list`` reports each
worktree's state, ``booley worktree rm`` removes one, keeping its evidence, and
``booley worktree gc`` removes the idle ones the same way.
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
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

from booley.config.worktree import GcAge
from booley.core.booley_dir import resolve_common_booley_dir
from booley.core.booley_layout import BooleyLayoutError
from booley.flows.execution import FlowConfigError, flow_enabled
from booley.runtime.booley_dir import (
    BooleyDirOutsideCheckoutError,
    own_booley_dir,
    own_booley_dir_relative,
)
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
    prune_missing_checkouts,
    refuse_paired_checkout,
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
    set_up_linked_worktree,
)
from booley.runtime.worktree_snapshot import SnapshotError, skipped_line
from booley.runtime.worktree_usage import WORKTREE_NAME, stamp_worktree, worktree_branch

# Bounds the script so a wedged Git call cannot hang the CLI.
_CREATE_TIMEOUT_S = 300

# Left-out Project files named in the warning before it falls back to a count.
_LEFT_OUT_LISTED = 10


def worktree_name(value: str) -> str:
    """Argparse type: accept only a safe single path component."""
    if not WORKTREE_NAME.fullmatch(value):
        raise argparse.ArgumentTypeError(
            f"invalid worktree name {value!r}: use letters, digits, '.', '_' or '-', "
            "starting with a letter or digit"
        )
    return value


def add_subparser(subparsers: argparse._SubParsersAction) -> None:
    """Register ``booley worktree`` with its ``new``, ``prepare``, ``list``, ``rm`` and ``gc`` commands."""
    parser = subparsers.add_parser(
        "worktree",
        help="Create, prepare, list, remove, or clean up linked worktrees",
        description=(
            "Create, prepare, list, remove, or clean up linked Git worktrees for this Project."
        ),
    )
    commands = parser.add_subparsers(
        dest="worktree_command", metavar="{new,prepare,list,rm,gc}", required=True
    )
    new = commands.add_parser(
        "new",
        help="Create .booley/worktrees/<name> and print its path",
        description=(
            "Create a linked Git worktree at .booley/worktrees/<name> with a "
            "Paired Checkout on booley-worktree/<name> for a Versioned Booley Directory, "
            "or a Snapshot for a non-versioned Booley Directory "
            "(live run and session state stays behind). The outer worktree starts on a "
            "new branch booley/<name> (wt/<name> in Stealth Mode) at the current "
            "commit; a branch that occupies that name is refused. A "
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
    _add_prepare_parser(commands)
    commands.add_parser(
        "list",
        help="Show every worktree's branch, changes, Session, and last use",
        description=(
            "List the worktrees under .booley/worktrees with their branch (or "
            "detached), uncommitted and unmerged work, live Sessions, "
            "and last use. Read-only."
        ),
    )
    _add_removal_parsers(commands)


def _add_prepare_parser(commands: argparse._SubParsersAction) -> None:
    prepare = commands.add_parser(
        "prepare",
        help="Give a worktree made with Git or T3 Code its own Booley Directory",
        description=(
            "Prepare the linked worktree at PATH (default: the current directory), made "
            "by Git or T3 Code instead of `booley worktree new`. It gets a Paired "
            "Checkout on booley-worktree/<folder> from the Booley Directory repository's "
            "last commit (uncommitted files are named in a warning and left out), or a "
            "Snapshot, or nothing when Git tracks the Booley Directory. It also prunes "
            "the Booley Directory repository's registrations whose folders are gone, "
            "applies the worktree Git settings, and runs the post-setup hook. "
            "It does nothing outside a Booley Project and refuses the main checkout and "
            "a Booley Directory outside the checkout. If a step fails, what the run "
            "created is removed. A second run creates nothing new. Submodules are not filled."
        ),
    )
    prepare.add_argument(
        "path",
        nargs="?",
        type=Path,
        metavar="PATH",
        help="a folder in the linked worktree (default: the current directory)",
    )


def _add_removal_parsers(commands: argparse._SubParsersAction) -> None:
    """Register ``rm`` and ``gc``."""
    remove = commands.add_parser(
        "rm",
        help="Remove .booley/worktrees/<name>, keeping its evidence and branches",
        description=(
            "Remove the linked worktree at .booley/worktrees/<name>: its Paired "
            "Checkout first, then the outer worktree. Common Booley State and Flow "
            "reports in the worktree move to .booley/worktrees/.evidence/<name>-<time>/ "
            "first, and the branches stay. Refuses, naming every reason, while the "
            "worktree has a live Session, uncommitted or unmerged "
            "work, a populated submodule commit that main lacks, a Git lock, or a "
            "damaged checkout; another worktree registered inside it is always "
            "refused. Re-run after an interruption to finish."
        ),
    )
    remove.add_argument("name", type=worktree_name, metavar="NAME", help="Worktree directory name")
    remove.add_argument(
        "--force",
        action="store_true",
        help=(
            "remove even with a live Session, uncommitted or "
            "unmerged work, or a Git lock; evidence is still kept"
        ),
    )
    garbage = commands.add_parser(
        "gc",
        help="Remove worktrees not used for a while, keeping any `rm` would refuse",
        description=(
            "Remove every worktree whose last use is older than AGE (default 7d, or "
            "[worktree].gc_older_than in the main checkout's .booley/booley.toml) with "
            "the same checks as `booley worktree rm`, never forced: a worktree with a "
            "live Session or unsaved work is kept, as is one "
            "with no last-used record or that you are inside. Prints each worktree "
            "removed or kept and why."
        ),
    )
    garbage.add_argument(
        "--older-than",
        type=_gc_age,
        metavar="AGE",
        help="idle age such as 7d, 36h or 1d12h (overrides [worktree].gc_older_than)",
    )


def _gc_age(value: str) -> GcAge:
    """Argparse type: an idle age in d/h/m/s components."""
    try:
        return GcAge.of(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError(str(exc)) from exc


def worktree_path(project_root: Path, name: str) -> Path:
    """Return where ``booley worktree new <name>`` creates the worktree."""
    return own_booley_dir(project_root) / "worktrees" / name


class WorktreeCreationError(Exception):
    """The outer worktree script could not create the requested checkout."""


class WorktreeMountRepairError(Exception):
    """A worktree registration naming a former mount point cannot be repaired."""


# Failures reported as one ERROR line; anything else is a defect and propagates.
_REPORTED_FAILURES = (
    WorktreeCreationError,
    WorktreeMountRepairError,
    ProjectPairingError,
    WorktreeSetupError,
    OSError,
)


def _repair_mount_registrations(project_root: Path) -> None:
    """Repair registrations naming a former mount before the script's prune can delete them."""
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


@dataclass(frozen=True)
class NewBranch:
    """The branch ``booley worktree new`` creates, and the commit it starts at."""

    name: str
    start_point: str


def _new_branch_start(project_root: Path, name: str) -> NewBranch | None:
    """Return the branch to create for *name*, refusing an invalid one.

    ``None`` for a ``<branch>--<description>`` name, which checks out an existing
    branch instead. The script refuses an existing branch under its lock, after
    its destination checks, so a taken name reports the more specific cause.
    """
    if "--" in name:
        return None
    branch = worktree_branch(project_root, name)
    if run_git(project_root, "check-ref-format", "--branch", branch).returncode != 0:
        raise WorktreeCreationError(f"invalid branch name {branch!r}; choose another name")
    head = run_git(project_root, "rev-parse", "--verify", "HEAD^{commit}")
    if head.returncode != 0:
        raise WorktreeCreationError(f"cannot read the current commit: {head.stderr.strip()}")
    return NewBranch(branch, head.stdout.strip())


def _create_outer(
    name: str, project_root: Path, *, paired_project: bool, new_branch: NewBranch | None
) -> None:
    script = worktree_create_script()
    if not script.is_file():
        raise WorktreeCreationError(f"worktree script not found: {script}")
    mark_fusesoc_ignore(own_booley_dir(project_root), paired=paired_project)
    payload = {"name": name, "cwd": str(project_root), "on_existing": "refuse"}
    if new_branch is not None:
        payload["new_branch"] = new_branch.name
        payload["start_point"] = new_branch.start_point
    environment = {**os.environ}
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
    worktree = worktree_path(project_root, name)
    try:
        skipped = set_up_linked_worktree(project_root, worktree, paired=paired_project)
    except BaseException as exc:
        _rollback_creation(project_root, worktree, name, exc, new_branch=new_branch)
        raise
    # Name the Booley Directory paths the Snapshot left behind for lack of a classification.
    for path in skipped:
        print(skipped_line(path), file=sys.stderr)


@dataclass(frozen=True)
class _Created:
    """A worktree whose registration and submodule fill succeeded; setup remains."""

    worktree: Path
    source: Path | None
    paired: bool
    new_branch: NewBranch | None


def run(args: argparse.Namespace, project_root: Path) -> int:
    """Dispatch ``new``, ``prepare``, ``list``, ``rm`` or ``gc``; ``new`` keeps registration atomic with directory migration."""
    if args.worktree_command == "list":
        from booley.harness import worktree_list

        return worktree_list.run(project_root)
    if args.worktree_command == "rm":
        from booley.harness import worktree_rm

        return worktree_rm.run(args, project_root)
    if args.worktree_command == "gc":
        from booley.harness import worktree_gc

        return worktree_gc.run(args, project_root)
    if args.worktree_command == "prepare":
        return _prepare(args.path or project_root)

    created = _create(project_root, args.name)
    if created is None:
        return 1
    print(created.worktree)
    print(_ready_notice(args.name, paired=created.paired), file=sys.stderr)
    return 0


def create_worktree(project_root: Path, name: str) -> Path | None:
    """Create worktree *name* exactly as ``booley worktree new`` does; return its path.

    ``None`` means the creation failed after its error was printed and everything it
    made was rolled back. Nothing is written to stdout.
    """
    created = _create(project_root, name)
    return None if created is None else created.worktree


def _create(project_root: Path, name: str) -> _Created | None:
    """Create and set up a worktree atomically; ``None`` after a printed, rolled-back failure."""
    from booley.core.layout_lock import layout_gate

    try:
        # Placement first: an escaping [project].dir keeps its own advice even when it names a checkout.
        refuse_paired_checkout(project_root / own_booley_dir_relative(project_root))
        worktree_booley_dir(project_root)
    except (ProjectPairingError, BooleyDirOutsideCheckoutError, BooleyDirMismatchError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return None
    with layout_gate(project_root):
        created = _run_current(name, project_root)
    if isinstance(created, int):
        return None
    # The post-setup hook can take minutes, so it runs after the gate is released.
    return created if _finish_setup(name, project_root, created) == 0 else None


def _run_current(name: str, project_root: Path) -> _Created | int:
    """Create the outer and paired checkouts atomically; print removal steps on failure."""
    worktree = worktree_path(project_root, name)
    created = False
    pairing: ProjectPairing | None = None
    source = None
    new_branch: NewBranch | None = None
    try:
        _repair_mount_registrations(project_root)
        source = project_pairing_source(project_root)
        validate_project_pairing(source, name)
        new_branch = _new_branch_start(project_root, name)
        _create_outer(name, project_root, paired_project=source is not None, new_branch=new_branch)
        created = True
        pairing = pair_project_worktree(project_root, worktree, name, source=source)
        if pairing is not None and pairing.left_out and source is not None:
            print(_left_out_warning(pairing.left_out, source), file=sys.stderr)
        print("Filling git submodules from the main checkout...", file=sys.stderr)
        _populate_submodules(worktree)
    except BaseException as exc:
        if created:
            _rollback_creation(
                project_root,
                worktree,
                name,
                exc,
                source if pairing is not None else None,
                new_branch=new_branch,
            )
        if not isinstance(exc, _REPORTED_FAILURES):
            raise
        return _report_failure(project_root, worktree, name, exc, source, new_branch)
    return _Created(worktree, source, paired=pairing is not None, new_branch=new_branch)


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
            new_branch=created.new_branch,
            regate=True,
        )
        if not isinstance(exc, _REPORTED_FAILURES):
            raise
        return _report_failure(
            project_root, created.worktree, name, exc, created.source, created.new_branch
        )
    # The stamp is bookkeeping: a failure must not hide a worktree that exists.
    try:
        stamp_worktree(resolve_common_booley_dir(project_root), name)
    except OSError as exc:
        print(f"WARNING: could not record the last-used time of {name}: {exc}", file=sys.stderr)
    return 0


def _report_failure(
    project_root: Path,
    worktree: Path,
    name: str,
    failure: BaseException,
    source: Path | None,
    new_branch: NewBranch | None,
) -> int:
    """Print a rolled-back creation's error, plus removal steps if a rollback left the pair."""
    print(f"ERROR: {failure}", file=sys.stderr)
    if source is not None and _is_named_pair(worktree, name):
        # A <branch>--<description> name checks out an existing branch and creates none.
        # A refusal before the branch was chosen (an existing pair) names the current setting's.
        if new_branch is not None:
            branch: str | None = new_branch.name
        else:
            branch = None if "--" in name else worktree_branch(project_root, name)
        print(
            worktree_removal_instructions(project_root, worktree, name, branch=branch),
            file=sys.stderr,
        )
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


# What a prepare run reports besides a rolled-back creation: bad input, or a step that failed.
_PREPARE_FAILURES = (
    *_REPORTED_FAILURES,
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


def _ready_notice(name: str, *, paired: bool) -> str:
    """One calm line for a successful creation; full removal steps print only on failure."""
    kind = "Paired Checkout" if paired else "Snapshot"
    return (
        f"Created worktree {name!r} with a {kind}. "
        f"Remove it later with: booley worktree rm {name} "
        f"(see {docs_url('WORKTREES.md', 'removing-a-worktree')})"
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
    new_branch: NewBranch | None = None,
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
                _undo_creation(root, worktree, name, paired_source, new_branch)
        else:
            _undo_creation(root, worktree, name, paired_source, new_branch)
    except (OSError, ProjectPairingError, BooleyLayoutError) as cleanup:
        failure.add_note(f"outer rollback failed: {cleanup}")
        print(f"ERROR: outer rollback failed: {cleanup}", file=sys.stderr)


def _undo_creation(
    root: Path,
    worktree: Path,
    name: str,
    paired_source: Path | None,
    new_branch: NewBranch | None,
) -> None:
    if paired_source is not None:
        rollback_project_pairing(paired_source, worktree, name)
    rollback_outer_worktree(root, worktree)
    if new_branch is not None:
        _drop_new_branch(root, new_branch)
    remove_creation_lock(root, name)


def _is_named_pair(worktree: Path, name: str) -> bool:
    nested = own_booley_dir(worktree)
    if not (nested / ".git").is_file():
        return False
    branch = run_git(nested, "symbolic-ref", "--short", "HEAD")
    return branch.returncode == 0 and branch.stdout.strip() == f"booley-worktree/{name}"


def _drop_new_branch(root: Path, new_branch: NewBranch) -> None:
    """Delete the branch this creation made, unless it has moved or is already gone."""
    ref = f"refs/heads/{new_branch.name}"
    current = run_git(root, "rev-parse", "--verify", "--quiet", ref)
    if current.returncode == 0 and current.stdout.strip() == new_branch.start_point:
        run_git(root, "update-ref", "-d", ref, new_branch.start_point)
