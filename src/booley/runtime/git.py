"""Git helper utilities for Booley Flows and Specialists."""

from __future__ import annotations

import fnmatch
import logging
import os
import subprocess
import sys
from collections.abc import Iterable
from pathlib import Path

from booley.dev_support.validate_commit_msg import validate_message

from .agent_errors import BlockingError

logger = logging.getLogger(__name__)

# Header that groups Booley-generated entries inside ``.git/info/exclude``.
BOOLEY_EXCLUDE_HEADER = "# Booley (generated; local, uncommitted)"


def git_run(wt: Path, args: list[str], timeout: int = 30) -> subprocess.CompletedProcess:
    """Run a git command in a working tree, returning CompletedProcess.

    Centralizes the common pattern: subprocess.run(["git", ...], cwd=str(wt),
    capture_output=True, text=True, encoding="utf-8", timeout=N).
    """
    result = subprocess.run(
        ["git", *args],
        cwd=str(wt),
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=timeout,
        check=False,
    )
    if _is_index_lock_failure(result) and _remove_stale_index_lock(wt):
        logger.warning("Removed stale git index.lock for %s; retrying git %s", wt, " ".join(args))
        result = subprocess.run(
            ["git", *args],
            cwd=str(wt),
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout,
            check=False,
        )
    return result


def _is_index_lock_failure(result: subprocess.CompletedProcess) -> bool:
    """Return True when Git failed because this worktree's index lock exists."""
    if result.returncode == 0:
        return False
    combined = f"{getattr(result, 'stdout', '')}\n{getattr(result, 'stderr', '')}".lower()
    return "index.lock" in combined and (
        "another git process" in combined
        or "unable to create" in combined
        or "file exists" in combined
    )


def _git_dir_for_worktree(wt: Path) -> Path | None:
    """Resolve the real git dir for a worktree without using git_run recursion."""
    try:
        result = subprocess.run(
            ["git", "rev-parse", "--git-dir"],
            cwd=str(wt),
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=10,
            check=False,
        )
    except (FileNotFoundError, OSError, subprocess.TimeoutExpired):
        return None
    if result.returncode != 0:
        return None
    raw = result.stdout.strip()
    if not raw:
        return None
    git_dir = Path(raw)
    if not git_dir.is_absolute():
        git_dir = wt / git_dir
    try:
        return git_dir.resolve()
    except OSError:
        return git_dir


def _git_common_dir(wt: Path) -> Path | None:
    """Resolve the *shared* git dir, where the honored ``info/exclude`` lives.

    Git treats ``info/`` as a path shared across all worktrees: it reads
    ``info/exclude`` from ``$GIT_COMMON_DIR`` even inside a linked worktree, and
    the per-worktree ``.git/worktrees/<id>/info/exclude`` is silently ignored
    (verified on git 2.53; ``info`` is in git's common-path list). So resolve
    the common dir via ``git rev-parse --git-common-dir`` to write excludes that
    are actually honored from every worktree.
    """
    try:
        result = subprocess.run(
            ["git", "rev-parse", "--git-common-dir"],
            cwd=str(wt),
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=10,
            check=False,
        )
    except (FileNotFoundError, OSError, subprocess.TimeoutExpired):
        result = None
    if result is not None and result.returncode == 0 and result.stdout.strip():
        common = Path(result.stdout.strip())
        if not common.is_absolute():
            common = wt / common
        try:
            return common.resolve()
        except OSError:
            return common
    # Fallback: inspect the filesystem when git is unavailable or the dir is
    # not a fully-initialised repo (resolves linked worktrees via ``commondir``).
    return _git_common_dir_fs(wt)


def git_common_dir(wt: Path) -> Path:
    """Return the shared Git metadata directory for a checkout or worktree."""
    common = _git_common_dir(Path(wt).resolve())
    if common is None:
        raise RuntimeError(f"cannot resolve Git's shared metadata directory for {wt}")
    return common


def _git_common_dir_fs(wt: Path) -> Path | None:  # noqa: PLR0911 — ordered resolution ladder; each early return is a distinct .git layout case
    """Filesystem-only resolution of the shared git dir (no ``git`` invocation)."""
    git = wt / ".git"
    if git.is_dir():
        return git
    if not git.is_file():
        return None
    # Worktree/submodule: the ".git" file holds "gitdir: <per-worktree dir>".
    try:
        text = git.read_text(encoding="utf-8").strip()
    except OSError:
        return None
    prefix = "gitdir:"
    if not text.startswith(prefix):
        return None
    gitdir = Path(text[len(prefix) :].strip())
    if not gitdir.is_absolute():
        gitdir = (wt / gitdir).resolve()
    # ``<gitdir>/commondir`` points (relatively) at the shared dir for worktrees.
    commondir_file = gitdir / "commondir"
    if commondir_file.is_file():
        try:
            rel = commondir_file.read_text(encoding="utf-8").strip()
        except OSError:
            return gitdir
        common = Path(rel)
        if not common.is_absolute():
            common = (gitdir / common).resolve()
        return common
    return gitdir


def add_git_excludes(
    wt: Path,
    names: Iterable[str],
    *,
    header: str = BOOLEY_EXCLUDE_HEADER,
) -> bool:
    """Idempotently add ``/<name>`` entries to the repo's honored ``info/exclude``.

    Worktree-aware: writes to ``$GIT_COMMON_DIR/info/exclude`` (the file git
    actually consults), not the per-worktree ``info`` dir, so the exclusions
    take effect from linked worktrees too. Entries are anchored with a leading
    ``/`` (repo-root relative) and grouped under *header*.

    Best-effort: an absent or unusual ``.git`` is logged and skipped, not fatal.
    Returns ``True`` if the exclude file was modified.
    """
    common = _git_common_dir(wt)
    if common is None:
        logger.debug("no git common dir for %s; skipping exclude update", wt)
        return False
    info_dir = common / "info"
    exclude = info_dir / "exclude"
    existing = exclude.read_text(encoding="utf-8").splitlines() if exclude.is_file() else []
    missing = [f"/{n}" for n in names if f"/{n}" not in existing]
    duplicate_headers = existing.count(header) > 1
    if not missing and not duplicate_headers:
        return False
    info_dir.mkdir(parents=True, exist_ok=True)
    lines: list[str] = []
    header_kept = False
    for line in existing:
        if line == header:
            if header_kept:
                continue
            header_kept = True
        lines.append(line)
    if header_kept:
        insert_at = lines.index(header) + 1
        while insert_at < len(lines) and lines[insert_at].startswith("/"):
            insert_at += 1
        lines[insert_at:insert_at] = missing
    else:
        if lines and lines[-1].strip() != "":
            lines.append("")
        lines.extend([header, *missing])
    exclude.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return True


def _remove_stale_index_lock(wt: Path) -> bool:
    """Remove a stale index.lock after confirming no live Git owns this worktree."""
    git_dir = _git_dir_for_worktree(wt)
    if git_dir is None:
        return False
    lock_path = git_dir / "index.lock"
    if not lock_path.exists():
        return False
    if _git_process_owns_worktree(wt, git_dir):
        logger.warning("Refusing to remove %s; a live git process appears to own it", lock_path)
        return False
    try:
        lock_path.unlink()
        return True
    except OSError as exc:
        logger.warning("Failed to remove stale git index lock %s: %s", lock_path, exc)
        return False


def _git_process_owns_worktree(wt: Path, git_dir: Path) -> bool:
    """Best-effort process inspection for live Git commands using this worktree."""
    wt_s = _normalize_process_path(wt)
    git_dir_s = _normalize_process_path(git_dir)
    for cmdline in _iter_git_process_cmdlines():
        normalized = _normalize_process_text(cmdline)
        if wt_s in normalized or git_dir_s in normalized:
            return True
    return False


def _iter_git_process_cmdlines() -> list[str]:
    """Return live git process command lines for stale-lock safety checks."""
    if sys.platform == "win32":
        return _iter_git_process_cmdlines_windows()
    return _iter_git_process_cmdlines_procfs()


def _iter_git_process_cmdlines_procfs() -> list[str]:
    """Collect git command lines from /proc on Linux-like systems."""
    proc_root = Path("/proc")
    if not proc_root.is_dir():
        return []
    current_pid = os.getpid()
    cmdlines: list[str] = []
    for entry in proc_root.iterdir():
        if not entry.name.isdigit() or int(entry.name) == current_pid:
            continue
        try:
            raw = (entry / "cmdline").read_bytes()
        except OSError:
            continue
        if not raw:
            continue
        text = raw.replace(b"\x00", b" ").decode("utf-8", errors="replace").strip()
        exe = Path(text.split(" ", 1)[0]).name.lower() if text else ""
        if exe in {"git", "git.exe"}:
            try:
                cwd = (entry / "cwd").resolve()
                text = f"{text} {cwd}"
            except OSError:
                pass
            cmdlines.append(text)
    return cmdlines


def _iter_git_process_cmdlines_windows() -> list[str]:
    """Collect git command lines on Windows via PowerShell/CIM."""
    script = (
        "Get-CimInstance Win32_Process "
        "-Filter \"Name = 'git.exe' OR Name = 'git'\" | "
        "ForEach-Object { $_.CommandLine }"
    )
    try:
        result = subprocess.run(
            ["powershell", "-NoProfile", "-Command", script],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=5,
            check=False,
        )
    except (FileNotFoundError, OSError, subprocess.TimeoutExpired):
        return []
    if result.returncode != 0:
        return []
    return [line.strip() for line in result.stdout.splitlines() if line.strip()]


def _normalize_process_path(path: Path) -> str:
    try:
        text = str(path.resolve())
    except OSError:
        text = str(path)
    return _normalize_process_text(text)


def _normalize_process_text(text: str) -> str:
    return text.replace("\\", "/").lower()


SCOPE_UNKNOWN = "*"
"""Sentinel value: scope = ["*"] means 'any file is valid' (unknown-scope bugfix)."""

NEW_SCOPE_SUFFIX = " [new]"
"""Suffix used in ticket scope entries for files expected to be created."""


def strip_scope_new_tag(entry: str) -> str:
    """Return a scope entry without the optional `` [new]`` marker."""
    return entry.removesuffix(NEW_SCOPE_SUFFIX)


def is_new_scope_entry(entry: str) -> bool:
    """True when a raw ticket scope entry is marked as expected-new."""
    return entry.endswith(NEW_SCOPE_SUFFIX)


def _get_submodule_paths(wt: Path) -> list[str]:
    """Discover submodule paths from .gitmodules in the worktree."""
    result = git_run(
        wt,
        ["config", "--file", ".gitmodules", "--get-regexp", r"^submodule\..*\.path$"],
        timeout=5,
    )
    if result.returncode != 0 or not result.stdout.strip():
        return []
    return [
        line.split(maxsplit=1)[1] for line in result.stdout.strip().splitlines() if " " in line
    ]


def is_scope_unknown(scope: list[str]) -> bool:
    """True when scope is the wildcard sentinel (unknown/any file allowed)."""
    return scope == [SCOPE_UNKNOWN]


def _has_glob_chars(pattern: str) -> bool:
    """Check if a scope entry contains glob metacharacters."""
    return any(c in pattern for c in ("*", "?", "["))


def _matches_scope_pattern(filepath: str, pattern: str) -> bool:
    """Match one scope entry against a path: glob, exact file, or dir prefix.

    A non-glob entry is an exact path *or* a directory prefix: ``rtl/verilog``
    and ``rtl/verilog/`` both own every file beneath ``rtl/verilog/``. Without
    the prefix rule a bare directory entry — the most natural thing to write
    in ``scope:`` — silently matches nothing (F-14).
    """
    if _has_glob_chars(pattern):
        return fnmatch.fnmatch(filepath, pattern)
    return filepath == pattern or filepath.startswith(pattern.rstrip("/") + "/")


def expand_scope_globs(wt: Path, scope: list[str]) -> list[str]:
    """Expand glob patterns in scope entries against the worktree.

    Non-glob entries pass through unchanged.  Glob entries (containing *, ?, [)
    are expanded against the worktree filesystem.  If a glob matches nothing,
    it is omitted so ``git add`` does not fail on an unmatched pathspec.

    Unknown scope (["*"]) is returned as-is — caller handles it specially.

    Returns deduplicated list with forward-slash paths (git-compatible).
    """
    if is_scope_unknown(scope):
        return list(scope)

    expanded: list[str] = []
    seen: set[str] = set()
    for raw_entry in scope:
        entry = strip_scope_new_tag(raw_entry)
        if _has_glob_chars(entry):
            matches = sorted(p.relative_to(wt).as_posix() for p in wt.glob(entry) if p.is_file())
            items = matches
        else:
            items = [entry]
        for item in items:
            if item not in seen:
                seen.add(item)
                expanded.append(item)
    return expanded


def scope_matches_file(scope: list[str], filepath: str) -> bool:
    """Check if a filepath matches any scope entry (literal or glob).

    Unknown scope (["*"]) matches everything.
    Intentional duplication of dev_support.scope_precommit_hook._matches_scope —
    the hook must run standalone without harness imports.
    """
    if is_scope_unknown(scope):
        return True
    return any(_matches_scope_pattern(filepath, strip_scope_new_tag(e)) for e in scope)


def scope_matches_dirty_file(scope: list[str], filepath: str, status: str) -> bool:
    """Check whether a dirty path belongs to scope, considering its status.

    `` [new]`` entries are allowed to own added/untracked/modified files. They
    do not own deletions, because deleting an already-tracked file that happens
    to match a broad "new file" glob is almost always sparse-worktree or
    restore fallout rather than ticket-owned work.
    """
    if is_scope_unknown([strip_scope_new_tag(e) for e in scope]):
        return True
    deleted = "D" in status[:2]
    for entry in scope:
        pattern = strip_scope_new_tag(entry)
        if _matches_scope_pattern(filepath, pattern) and not (
            deleted and is_new_scope_entry(entry)
        ):
            return True
    return False


def commit_scope(
    wt: Path,
    scope: list[str],
    message: str,
    *,
    literal: bool = False,
) -> None:
    """Stage scope files and commit. Uses explicit paths, never git add -A.

    Raises ValueError if scope is empty -- callers must provide explicit paths.
    Safety: refuses to commit in the main worktree (Principle 3).
    Any unrelated paths already in the index are unstaged without changing
    their working-tree content, so they cannot block the authorized commit.

    *literal* switches off glob interpretation, for callers passing real paths
    read off the worktree rather than ticket-authored Scope patterns.  A real
    filename may contain ``[``, ``*`` or ``?`` -- ``rtl/mem[0].sv`` is ordinary
    Verilog -- and treating one as a pattern silently expands it to nothing,
    dropping the file from the commit while every step still reports success.
    """
    if not _guard_main_worktree(wt):
        return

    if not scope:
        logger.warning("commit_scope called with empty scope -- skipping commit")
        return

    scope = list(scope) if literal else expand_scope_globs(wt, list(scope))
    _stage_scope_files(wt, scope, literal=literal)
    _isolate_scope_staging(wt, scope, literal=literal)

    errors = validate_message(message, project_root=wt)
    if errors:
        raise BlockingError(
            f"Commit message validation failed: {'; '.join(errors)} — message={message!r}"
        )

    _run_commit(wt, scope, message)


def _guard_main_worktree(wt: Path) -> bool:
    """Return False (and log error) if wt is the main worktree."""
    try:
        git_file = Path(wt) / ".git"
        if git_file.is_dir():
            logger.error("commit_scope called on main worktree (%s) -- refusing", wt)
            return False
    except OSError as exc:
        logger.warning(
            "Main-worktree guard check failed (%s) -- refusing to commit as a safety precaution",
            exc,
        )
        return False
    return True


def _stage_scope_files(wt: Path, scope: list[str], *, literal: bool = False) -> None:
    """Stage files for the given scope. Raises BlockingError on failure."""
    if literal:
        add_result = git_run(wt, ["--literal-pathspecs", "add", "--", *scope])
    elif is_scope_unknown(scope):
        sub_excludes = [f":(exclude){p}" for p in _get_submodule_paths(wt)]
        add_result = git_run(wt, ["add", "--all", "--", ".", *sub_excludes])
    else:
        add_result = git_run(wt, ["add", *scope])
    if add_result.returncode != 0:
        raise BlockingError(
            f"git add failed in commit_scope (rc={add_result.returncode}): "
            f"{add_result.stderr.strip()} -- scope={list(scope)}"
        )


def _isolate_scope_staging(wt: Path, scope: list[str], *, literal: bool = False) -> None:
    """Unstage unrelated paths while preserving their working-tree changes."""
    if not literal and is_scope_unknown(scope):
        return
    # ``-z`` so a path with a space or a non-ASCII byte arrives unquoted and
    # compares equal to the same path as git reported it to the caller.
    staged_result = git_run(wt, ["diff", "--cached", "--name-only", "-z", "--diff-filter=ACMRD"])
    if staged_result.returncode == 0 and (staged_result.stdout or "").strip():
        staged_files = [f for f in staged_result.stdout.split("\0") if f.strip()]
        if literal:
            # Literal paths are compared by identity: a filename holding glob
            # metacharacters must not be re-read as a pattern here either.
            allowed = set(scope)
            out_of_scope = [f for f in staged_files if f not in allowed]
        else:
            out_of_scope = [f for f in staged_files if not scope_matches_file(scope, f)]
        if not out_of_scope:
            return
        reset_result = git_run(
            wt,
            ["--literal-pathspecs", "reset", "--quiet", "HEAD", "--", *out_of_scope],
        )
        if reset_result.returncode != 0:
            raise BlockingError(
                "Could not unstage out-of-scope files before scoped commit "
                f"(rc={reset_result.returncode}): {reset_result.stderr.strip()} "
                f"-- paths={out_of_scope}"
            )
        logger.warning(
            "Leaving %d out-of-scope staged file(s) uncommitted: %s",
            len(out_of_scope),
            ", ".join(out_of_scope[:5]),
        )


def _run_commit(wt: Path, scope: list[str], message: str) -> None:
    """Execute git commit, handling 'nothing to commit' gracefully."""
    commit_result = git_run(wt, ["commit", "-m", message])
    if commit_result.returncode != 0:
        combined = (commit_result.stdout + commit_result.stderr).strip()
        if "nothing to commit" in combined or "no changes added to commit" in combined:
            logger.info("commit_scope: nothing to commit for scope=%s", list(scope))
            return
        raise BlockingError(
            f"git commit failed in commit_scope (rc={commit_result.returncode}): {combined}"
        )
