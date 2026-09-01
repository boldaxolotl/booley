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

import re
import sys
from pathlib import Path

try:
    from ..core.run_command import run_command
    from .commit_msg_utils import enforce_convention, find_banned, max_body_lines, stealth_enabled
except ImportError:
    # When run as a standalone script (e.g. inside Docker), the package-relative
    # import fails.  Ensure this file's dir and the package root are on sys.path
    # so the bare module names resolve.
    _support_dir = str(Path(__file__).resolve().parent)
    _pkg_dir = str(Path(__file__).resolve().parent.parent)  # src/booley
    for _p in (_support_dir, _pkg_dir):
        if _p not in sys.path:
            sys.path.insert(0, _p)
    # Resolve run_command by bare name: `core.run_command` when the package root is on
    # sys.path, else `run_command` — init vendors the stdlib-only runner flat beside
    # the hook scripts (single source of truth, no divergent reimplementation),
    # resolved via the hook dir already on sys.path.
    import importlib

    from commit_msg_utils import enforce_convention, find_banned, max_body_lines, stealth_enabled

    run_command = None
    for _mod in ("core.run_command", "run_command"):
        try:
            run_command = importlib.import_module(_mod).run_command
            break
        except ImportError:
            continue

    if run_command is None:
        # Last-resort insurance for a *stale* vendored hooks dir predating the
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


# Max length for the summary part only (after "type(scope): " prefix)
MAX_SUMMARY_LEN = 72

# Allowed commit types — single source of truth for both the SUBJECT_RE regex
# and the "doesn't match" error message, so the two can never drift apart.
# `chore` covers mundane housekeeping (gitignore tweaks, dep bumps, ...) that
# would otherwise have to masquerade as fix/docs (F-15, verilog-pcie setup).
ALLOWED_TYPES = ("feat", "fix", "refactor", "test", "review", "wip", "docs", "chore")

# Format: type(scope): summary  OR  type: summary
# Scope allows [a-zA-Z0-9_-] so ticket slugs (which may contain uppercase
# letters and hyphens) can be used verbatim as the scope.
SUBJECT_RE = re.compile(
    r"^(?P<prefix>(?:" + "|".join(ALLOWED_TYPES) + r")"  # type
    r"(?:\([a-zA-Z0-9_-]+\))?"  # optional (scope)
    r": )"  # colon + space
    r"(?P<summary>.+)$"  # summary
)

# Alias for internal use
_find_banned = find_banned

_PROJECT_CONFIG_DIRS = (Path(".booley_project"), Path(".booley") / "project")
_PROJECT_CONFIG_NAMES = ("booley.toml", "pipeline.toml")


def _has_project_config(repo_root: Path) -> bool:
    """Whether *repo_root* is a configured design or project-state repository."""
    try:
        from booley.runtime.checkout_role import source_checkout_root
    except ImportError:
        source_checkout_root = None
    if source_checkout_root is not None and source_checkout_root(repo_root) is not None:
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


def _body_content_lines(msg: str) -> list[str]:
    """Body lines that carry content: everything after the subject, minus git's
    ``#`` comment lines and blank lines.

    Comments and blanks are excluded because neither reaches history — git
    strips comments itself, and blank lines are just the paragraph spacing the
    author happened to use. Counting them would make the cap depend on
    formatting rather than on how much prose the message actually carries.
    """
    normalized = msg.replace("\r\n", "\n").replace("\r", "\n")
    parts = normalized.split("\n", 1)
    if len(parts) < 2:
        return []
    return [ln for ln in parts[1].split("\n") if ln.strip() and not ln.lstrip().startswith("#")]


def validate_message(msg: str, *, project_root: Path | None = None) -> list[str]:
    """Validate commit message. Return list of errors (empty = valid)."""
    errors = []
    subject = msg.split("\n", 1)[0]
    is_merge = subject.startswith("Merge ") or subject.startswith("merge(")

    # --- format and length: opt-in convention, merge commits exempt ---
    # The type(scope): summary convention is off by default ([stealth]
    # enforce_convention). A design repo carries human- and upstream-style
    # commits on code the team doesn't own, and forcing every one through this
    # format is noise; a team that wants it opts in. Banned-word and body-cap
    # checks below follow stealth mode — those are leak/hygiene, not convention.
    if enforce_convention(project_root) and not is_merge:
        m = SUBJECT_RE.match(subject)
        if not m:
            errors.append(
                f"Subject doesn't match '<type>(<scope>): <summary>' "
                f"(allowed types: {', '.join(ALLOWED_TYPES)}): "
                f"'{subject}'"
            )
        else:
            summary = m.group("summary")
            if len(summary) > MAX_SUMMARY_LEN:
                errors.append(
                    f"Summary is {len(summary)} chars (max {MAX_SUMMARY_LEN}): '{summary}'"
                )

    # --- body length (opt-in cap) ---
    # A body is allowed by default. The old unconditional rule ("single-line
    # messages only") existed to guarantee no banned content reached history,
    # but the sanitizer already redacts the body in place, so the guarantee
    # holds without the amputation — and enforcing it made the hook throw
    # authored rationale away (taxi port, F-11). The banned-word check below
    # still covers the body.
    #
    # A project that wants terse history opts back in with [stealth]
    # max_body_lines. This REJECTS an over-long message rather than truncating
    # it: that keeps the F-11 lesson intact (nothing the author wrote is ever
    # silently destroyed) while still enforcing the cap — the author is told,
    # and moves the rationale somewhere it will survive.
    #
    # Merge commits are NOT exempt, unlike the format check above. A merge body
    # is exactly where the long messages that motivated the knob accumulate
    # (a merge carrying a hand-written port narrative), and git's own generated
    # merge messages have no body once its `#` comment lines are dropped.
    cap = max_body_lines(project_root)
    if cap is not None:
        body = _body_content_lines(msg)
        if len(body) > cap:
            detail = "commit messages must be a single subject line" if cap == 0 else f"max {cap}"
            errors.append(
                f"Body is {len(body)} line(s) ([stealth] max_body_lines = {cap}): {detail}"
            )

    # --- banned words (checked only while stealth mode is enabled) ---
    if stealth_enabled(project_root):
        for phrase in _find_banned(msg, project_root):
            errors.append(f"Banned phrase in commit message: '{phrase}'")

    return errors


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
        for phrase in _find_banned(added_text, project_root):
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
