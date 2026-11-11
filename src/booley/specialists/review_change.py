"""Git facts for a diff-scoped Reviewer run (``reviewer --base``).

Given a base commit and the declared review scope, answer which scope files
changed since the merge-base of the base and HEAD (uncommitted changes
included), which unchanged scope files use them, and what the Reviewer is told
about the change: its diff and the commit messages of the range. Everything
here is Git and lexical text; nothing elaborates HDL.
"""

from __future__ import annotations

import re
import subprocess
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path, PurePosixPath

from booley.core.hdl import declared_module_names, strip_comments
from booley.runtime.platform_paths import run_captured_bounded

from .review_contract import normalize_scope_path

_GIT_TIMEOUT_S = 30
MAX_DIFF_CHARS = 60_000
MAX_COMMITS_CHARS = 10_000
_TRUNCATED = "\n[{what} TRUNCATED]\n"
_HDL_SUFFIXES = frozenset({".v", ".vh", ".sv", ".svh"})
_COMMENTED_SUFFIXES = _HDL_SUFFIXES | frozenset(
    {".c", ".cc", ".cpp", ".cxx", ".h", ".hh", ".hpp", ".hxx"}
)
_PACKAGE_OR_INTERFACE = re.compile(
    r"^\s*(?:package|interface)\s+(?:automatic\s+|static\s+)?([A-Za-z_]\w*)", re.MULTILINE
)


class ReviewChangeError(ValueError):
    """The base commit or the checkout cannot answer a change query."""


@dataclass(frozen=True)
class ReviewChange:
    """What changed in the review scope since the merge-base of a base and HEAD."""

    base: str
    merge_base: str
    changed: tuple[str, ...] = ()
    context: tuple[str, ...] = ()
    diff: str = ""
    commits: str = ""


def _git(work_dir: Path, *args: str) -> str:
    """Run Git in *work_dir*; any failure becomes a ``ReviewChangeError``."""
    argv = ["git", "-C", str(work_dir), *args]
    command = " ".join(["git", *args])
    try:
        proc = run_captured_bounded(argv, timeout=_GIT_TIMEOUT_S)
    except subprocess.TimeoutExpired as exc:
        raise ReviewChangeError(f"`{command}` timed out after {_GIT_TIMEOUT_S}s") from exc
    except OSError as exc:
        raise ReviewChangeError(f"`{command}` could not run: {exc}") from exc
    if proc.returncode != 0:
        raise ReviewChangeError(f"`{command}` failed: {proc.stderr.strip()}")
    return proc.stdout


def _merge_base(work_dir: Path, base: str) -> str:
    """Resolve *base* to a commit, then to its merge-base with HEAD."""
    try:
        commit = _git(work_dir, "rev-parse", "--verify", "--end-of-options", f"{base}^{{commit}}")
        return _git(work_dir, "merge-base", commit.strip(), "HEAD").strip()
    except ReviewChangeError as exc:
        raise ReviewChangeError(
            f"--base {base}: not a commit with a merge-base with HEAD in this checkout: {exc}"
        ) from exc


def _nul_paths(output: str) -> set[str]:
    return {normalize_scope_path(name) for name in output.split("\0") if name}


def _changed_paths(work_dir: Path, merge_base: str) -> tuple[set[str], set[str]]:
    """Return (every changed path, untracked subset), existing files only.

    The merge-base is diffed against the live working tree, so committed,
    staged and unstaged edits count and commits that reached the base branch
    afterwards do not. Deleted files are dropped: they cannot be reviewed.
    """
    tracked = _nul_paths(
        _git(
            work_dir,
            "diff",
            "-z",
            "--name-only",
            "--relative",
            "--no-renames",
            "--diff-filter=d",
            merge_base,
        )
    )
    untracked = _nul_paths(_git(work_dir, "ls-files", "-z", "--others", "--exclude-standard"))
    return tracked | untracked, untracked


def _provided_names(path: str, text: str) -> set[str]:
    """Names other files use to refer to *path*: file name, stem, declared units."""
    posix = PurePosixPath(path)
    names = {posix.name, posix.stem}
    if posix.suffix.lower() in _HDL_SUFFIXES:
        code = strip_comments(text)
        names.update(declared_module_names(text))
        names.update(_PACKAGE_OR_INTERFACE.findall(code))
    return {name for name in names if name}


def _read_scope_text(work_dir: Path, path: str) -> str:
    """Read one scope file for lexical matching; unreadable files match nothing."""
    try:
        text = (work_dir / path).read_text(encoding="utf-8", errors="replace")
    except OSError:
        return ""
    if PurePosixPath(path).suffix.lower() in _COMMENTED_SUFFIXES:
        return strip_comments(text)
    return text


def _users_of(work_dir: Path, changed: Sequence[str], unchanged: Sequence[str]) -> tuple[str, ...]:
    """Unchanged scope files whose text mentions a name a changed file provides."""
    names: set[str] = set()
    for path in changed:
        names |= _provided_names(path, _read_scope_text(work_dir, path))
    pattern = re.compile(
        "|".join(rf"(?<![\w$]){re.escape(name)}(?![\w$])" for name in sorted(names))
    )
    return tuple(path for path in unchanged if pattern.search(_read_scope_text(work_dir, path)))


def _cap(text: str, limit: int, what: str) -> str:
    """Bound *text* to *limit* characters, marking the cut."""
    if len(text) <= limit:
        return text
    return text[:limit] + _TRUNCATED.format(what=what)


def _diff_text(
    work_dir: Path, merge_base: str, changed: Sequence[str], untracked: set[str]
) -> str:
    """Unified diff of the tracked changed files plus a note per new untracked file."""
    tracked = [path for path in changed if path not in untracked]
    parts: list[str] = []
    if tracked:
        parts.append(
            _git(
                work_dir,
                "diff",
                "--no-color",
                "--no-ext-diff",
                "--no-textconv",
                "--relative",
                "--no-renames",
                merge_base,
                "--",
                *tracked,
            )
        )
    parts.extend(
        f"new untracked file: {path} (read it in full)\n" for path in changed if path in untracked
    )
    return _cap("".join(parts), MAX_DIFF_CHARS, "DIFF")


def resolve_review_change(work_dir: Path, base: str, scope: Sequence[str]) -> ReviewChange:
    """Describe the change under review, or raise ``ReviewChangeError``.

    ``changed`` and ``context`` hold normalized scope entries only; with no
    changed scope file the result carries no diff and no commit messages.
    """
    merge_base = _merge_base(work_dir, base)
    every_change, untracked = _changed_paths(work_dir, merge_base)
    entries = sorted({normalize_scope_path(entry) for entry in scope})
    changed = tuple(entry for entry in entries if entry in every_change)
    if not changed:
        return ReviewChange(base=base, merge_base=merge_base)
    unchanged = [entry for entry in entries if entry not in every_change]
    commits = _git(work_dir, "log", "--reverse", "--format=commit %h%n%B", f"{merge_base}..HEAD")
    return ReviewChange(
        base=base,
        merge_base=merge_base,
        changed=changed,
        context=_users_of(work_dir, changed, unchanged),
        diff=_diff_text(work_dir, merge_base, changed, untracked),
        commits=_cap(commits.strip(), MAX_COMMITS_CHARS, "COMMITS"),
    )
