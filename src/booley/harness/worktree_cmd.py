"""``booley worktree new <name>``: create a linked worktree with paired or snapshot Project inputs.

The command wraps the packaged ``worktree_create.sh`` with its default
``refuse`` policy, so an existing destination (possibly an active worktree
holding someone's work) is never deleted. The worktree lands where Ticket
setup puts its worktrees, ``.booley_project/worktrees/<name>``, on a detached
HEAD at the workspace's current commit unless the name uses the
``<branch>--<description>`` convention to check out an existing branch.
A standalone versioned Project gets a paired checkout on ``booley-worktree/<name>``
at its clean HEAD; a non-versioned Project gets a clean snapshot.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
from pathlib import Path

from booley.runtime.paths import worktree_create_script
from booley.runtime.platform_paths import bash_bin
from booley.runtime.project_dir import PROJECT_DIR_NAME
from booley.runtime.project_worktree_pairing import (
    pair_project_worktree,
    project_pairing_source,
    rollback_outer_worktree,
)

# Same single-path-component rule the script enforces; checking it at the
# CLI boundary gives an argparse error before any process starts.
_WORKTREE_NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]*")

# Matches the Ticket setup budget for the same script.
_CREATE_TIMEOUT_S = 300


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
        help="Create a linked worktree with paired or snapshot Project inputs",
        description="Create linked Git worktrees for this Project.",
    )
    commands = parser.add_subparsers(dest="worktree_command", metavar="{new}", required=True)
    new = commands.add_parser(
        "new",
        help="Create .booley_project/worktrees/<name> and print its path",
        description=(
            "Create a linked Git worktree at .booley_project/worktrees/<name> with a "
            "paired Project checkout on booley-worktree/<name> for a versioned "
            "Project, or a clean .booley_project snapshot for a non-versioned Project "
            "(live run and session state stays behind). The outer worktree starts on a "
            "detached HEAD at the current commit; a "
            "<branch>--<description> name checks out that existing branch instead. "
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
    return project_root / PROJECT_DIR_NAME / "worktrees" / name


def _create_outer(name: str, project_root: Path) -> None:
    script = worktree_create_script()
    if not script.is_file():
        raise RuntimeError(f"worktree script not found: {script}")
    payload = {"name": name, "cwd": str(project_root), "on_existing": "refuse"}
    environment = {**os.environ}
    environment["BOOLEY_WORKTREE_PAIRED_PROJECT"] = (
        "1" if project_pairing_source(project_root) is not None else "0"
    )
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
        raise RuntimeError(f"worktree creation timed out after {_CREATE_TIMEOUT_S} s") from exc
    except OSError as exc:
        raise RuntimeError(f"worktree creation failed to start: {exc}") from exc
    if result.returncode != 0:
        errors = [line for line in result.stderr.splitlines() if line.startswith("ERROR:")]
        detail = "\n".join(errors) or result.stderr.strip() or "(no output)"
        raise RuntimeError(detail)


def run(args: argparse.Namespace, project_root: Path) -> int:
    """Create the outer and paired checkouts atomically; print paths and removal order."""
    worktree = worktree_path(project_root, args.name)
    created = False
    try:
        project_pairing_source(project_root)
        _create_outer(args.name, project_root)
        created = True
        paired = pair_project_worktree(project_root, worktree, args.name)
    except (OSError, RuntimeError, ValueError) as exc:
        if created:
            try:
                rollback_outer_worktree(project_root, worktree)
            except (OSError, RuntimeError, ValueError) as cleanup:
                print(f"ERROR: outer rollback failed: {cleanup}", file=sys.stderr)
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1
    print(worktree)
    if paired:
        print(
            f"Remove paired Project first: git -C {PROJECT_DIR_NAME} worktree remove "
            f"'{worktree / PROJECT_DIR_NAME}'\nThen: git worktree remove '{worktree}'",
            file=sys.stderr,
        )
    return 0
