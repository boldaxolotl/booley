"""``booley worktree new <name>``: create a linked worktree with a clean Project snapshot.

The command wraps the packaged ``worktree_create.sh`` with its default
``refuse`` policy, so an existing destination (possibly an active worktree
holding someone's work) is never deleted. The worktree lands where Ticket
setup puts its worktrees, ``.booley_project/worktrees/<name>``, on a detached
HEAD at the workspace's current commit unless the name uses the
``<branch>--<description>`` convention to check out an existing branch.
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
        help="Create a linked worktree with a clean Project snapshot",
        description="Create linked Git worktrees for this Project.",
    )
    commands = parser.add_subparsers(dest="worktree_command", metavar="{new}", required=True)
    new = commands.add_parser(
        "new",
        help="Create .booley_project/worktrees/<name> and print its path",
        description=(
            "Create a linked Git worktree at .booley_project/worktrees/<name> with a "
            "clean .booley_project snapshot (live run and session state stays "
            "behind). The worktree starts on a detached HEAD at the current commit; a "
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


def run(args: argparse.Namespace, project_root: Path) -> int:
    """Run the worktree script with the refusing policy; print the new path."""
    script = worktree_create_script()
    if not script.is_file():
        print(f"ERROR: worktree script not found: {script}", file=sys.stderr)
        return 1
    payload = {"name": args.name, "cwd": str(project_root), "on_existing": "refuse"}
    # Pin the script to this interpreter (it otherwise probes PATH for Python).
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
    except subprocess.TimeoutExpired:
        print(
            f"ERROR: worktree creation timed out after {_CREATE_TIMEOUT_S} s",
            file=sys.stderr,
        )
        return 1
    except OSError as exc:
        print(f"ERROR: worktree creation failed to start: {exc}", file=sys.stderr)
        return 1
    if result.returncode != 0:
        # Surface only the script's ERROR lines; the rest is progress chatter.
        errors = [line for line in result.stderr.splitlines() if line.startswith("ERROR:")]
        detail = "\n".join(errors) or result.stderr.strip() or "(no output)"
        print(detail, file=sys.stderr)
        return 1
    # The script prints a POSIX path; report the native one instead.
    print(worktree_path(project_root, args.name))
    return 0
