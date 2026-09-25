#!/usr/bin/env python3
"""Validate a commit message against project conventions.

Usage:
  python validate_commit_msg.py "commit message"
  python validate_commit_msg.py --no-diff "commit message"

Checks run only in a configured project repository.  The framework source
repository has no project configuration, so this command is intentionally a
no-op there.  In a project repository, the default also scans the staged diff
(git diff --cached) for banned words.
Use --no-diff to skip the diff check (e.g. for retroactive message validation).
Exit code 0 = valid, 1 = invalid (errors printed to stderr).
"""

from __future__ import annotations

import sys
from pathlib import Path

try:
    from booley import commit_policy as _shared_validation
    from booley.commit_policy.policy import (
        find_banned,
        source_checkout_policy_owner,
        stealth_enabled,
    )

    from ..core.run_command import run_command
except ImportError:
    # When run as a standalone script (e.g. inside Docker), the package-relative
    # import fails.  Ensure this file's dir and the package root are on sys.path
    # so the bare module names resolve.
    _support_dir = str(Path(__file__).resolve().parent)
    _pkg_dir = str(Path(__file__).resolve().parents[2])  # source root containing booley/
    for _p in (_support_dir, _pkg_dir):
        if _p not in sys.path:
            sys.path.insert(0, _p)
    # Resolve run_command by bare name: `core.run_command` when the package root is on
    # sys.path, else `run_command` — init vendors the stdlib-only runner flat beside
    # the hook scripts (single source of truth, no divergent reimplementation),
    # resolved via the hook dir already on sys.path.
    import importlib

    try:
        from booley import commit_policy as _shared_validation
        from booley.commit_policy.policy import (
            find_banned,
            source_checkout_policy_owner,
            stealth_enabled,
        )
    except ImportError:
        import booley_commit_validation as _shared_validation
        from booley_commit_policy import find_banned, source_checkout_policy_owner, stealth_enabled

    run_command = None
    for _mod in ("booley.core.run_command", "core.run_command", "run_command"):
        try:
            run_command = importlib.import_module(_mod).run_command
            break
        except ImportError:
            continue

    if run_command is None:
        # Last-resort insurance for a *stale* Project bundle predating the
        # flat run_command.py vendoring (onboarded before that init change and not
        # re-run): shim the one call we make (git diff) with subprocess,
        # mirroring CommandRun's used fields so a host commit still validates
        # instead of crashing on ModuleNotFoundError.
        import subprocess
        from dataclasses import dataclass

        @dataclass
        class _CommandRun:
            returncode: int
            stdout: str
            stderr: str

            @property
            def ok(self) -> bool:
                return self.returncode == 0

            def failure_excerpt(self, limit: int = 400) -> str:
                text = (self.stderr or self.stdout).strip()
                return f"rc={self.returncode}: {text[:limit]}"

        def run_command(argv, **kwargs):
            proc = subprocess.run(
                argv,
                capture_output=True,
                text=True,
                check=False,
            )
            return _CommandRun(proc.returncode, proc.stdout, proc.stderr)


ALLOWED_TYPES = _shared_validation.ALLOWED_TYPES
MAX_SUMMARY_LEN = _shared_validation.MAX_SUMMARY_LEN
SUBJECT_RE = _shared_validation.SUBJECT_RE
validate_message = _shared_validation.validate_message


_PROJECT_CONFIG_DIRS = (Path(".booley_project"), Path(".booley") / "project")
_PROJECT_CONFIG_NAMES = ("booley.toml", "pipeline.toml")


def _has_project_config(repo_root: Path) -> bool:
    """Whether *repo_root* is a configured design or project-state repository."""
    if source_checkout_policy_owner(repo_root):
        return False

    nested = (
        repo_root / subdir / name
        for subdir in _PROJECT_CONFIG_DIRS
        for name in _PROJECT_CONFIG_NAMES
    )
    if any(path.is_file() for path in nested):
        return True

    is_state_repo = repo_root.name == ".booley_project" or (
        repo_root.name == "project" and repo_root.parent.name == ".booley"
    )
    return is_state_repo and any((repo_root / name).is_file() for name in _PROJECT_CONFIG_NAMES)


def _configured_project_repo() -> bool:
    """Whether the current Git repository carries project configuration."""
    root = _current_repo_root()
    return root is not None and _has_project_config(root)


def _current_repo_root() -> Path | None:
    """Return the current checkout root rather than this module's checkout."""
    run = run_command(["git", "rev-parse", "--show-toplevel"])
    if not run.ok or not run.stdout.strip():
        return None
    return Path(run.stdout.strip()).resolve()


def validate_diff(project_root: Path | None = None) -> list[str]:
    """Scan staged diff for banned words. Return list of errors."""
    run = run_command(["git", "diff", "--cached", "-U0"])
    if not run.ok:
        # Surface git's own stderr instead of a bare "failed" — a swallowed
        # error here (no repo, bad object) is otherwise indistinguishable.
        return [f"Failed to run git diff --cached: {run.failure_excerpt()}"]
    diff = run.stdout

    if not diff:
        return []

    errors = []
    for _line_num, line in enumerate(diff.splitlines(), 1):
        # Only check added lines (start with "+", but not diff headers "+++")
        if not line.startswith("+") or line.startswith("+++"):
            continue
        added_text = line[1:]  # strip the leading "+"
        for phrase in find_banned(added_text, project_root):
            # Truncate long lines for readability
            display = added_text.strip()
            if len(display) > 80:
                display = display[:77] + "..."
            errors.append(f"Banned phrase '{phrase}' in staged diff: {display}")

    # Deduplicate (same phrase may appear many times)
    return sorted(set(errors))


def main() -> int:
    import argparse

    parser = argparse.ArgumentParser(
        description="Validate a commit message against project conventions.",
        epilog="Exit code 0 = valid, 1 = invalid (errors printed to stderr).",
    )
    parser.add_argument(
        "message",
        help="commit message to validate (e.g. 'fix(core): handle edge case')",
    )
    parser.add_argument(
        "--no-diff",
        action="store_true",
        help="skip scanning staged diff for banned words",
    )
    parsed = parser.parse_args()

    if not _configured_project_repo():
        print(
            "Commit message checks skipped: current repository is not a configured project.",
            file=sys.stderr,
        )
        return 0

    project_root = _current_repo_root()
    check_diff = not parsed.no_diff and stealth_enabled(project_root)
    msg = parsed.message
    errors = validate_message(msg, project_root=project_root)

    if check_diff:
        errors.extend(validate_diff(project_root))

    if errors:
        print("Commit message validation FAILED:", file=sys.stderr)
        for e in errors:
            print(f"  - {e}", file=sys.stderr)
        return 1

    print("Commit message OK.", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
