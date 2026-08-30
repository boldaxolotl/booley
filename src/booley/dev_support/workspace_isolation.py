"""Workspace isolation -- single-owner module for agent workspace boundaries.

Defines what's visible, what's cleaned, and what's blocked during a endpoint run.
Every isolation guarantee lives here; endpoint modules compose them via the public API.

Isolation is defense-in-depth across three stages:

  1. Setup time (worktree_create.sh, harness/setup/workspace.py)
     - eval_debug excluded from worktree copy
     - Pre-copied task dirs cleaned after hook

  2. Endpoint execution (this module)
     - Sim artifacts cleaned  (clean_sim_artifacts)
     - Opposite-category sources hidden  (hide_opposite_sources)
     - Specific files hidden  (hide_specific_files)
     - Shadow packages removed  (remove_shadow_package)

  3. Prompt time (CATEGORY_GUARD messages)
     - Agent instructed not to read/write out-of-scope files

All context managers are crash-safe: a ``finally`` block covers exceptions,
a signal handler covers SIGTERM/SIGHUP/SIGINT, and a stash manifest lets the
next run heal what an uncatchable kill (SIGKILL, power loss) left behind.
"""

from __future__ import annotations

import filecmp
import json
import logging
import os
import shutil
import signal
import stat
import tempfile
import threading
import time
from collections.abc import Callable, Generator
from contextlib import contextmanager, suppress
from pathlib import Path
from typing import Any

from booley.mcp.base import read_source_dirs_from_toml
from booley.runtime.pid import is_pid_alive
from booley.runtime.timefmt import compact_utc_now

logger = logging.getLogger(__name__)

_ARTIFACT_ROOT_NAMES = {
    "eval",
    "eval_tmp",
    "tmp",
    "baselines",
    "baseline_codex",
    "tickets",
    "worktrees",
    # Build tree (edalize work dirs, flow-reports, doctor, payload). Edalize
    # copies filesets — incl. rtl/ tb/ dirs — into its work root, so the nested
    # opposite-category scan must NOT descend here or it would stash cached
    # build output and break incremental builds / cross-category isolation.
    ".runtime",
}


def _is_artifact_path(path: Path) -> bool:
    """True for nested Booley artifact/snapshot paths that isolation must not move."""
    parts = path.parts
    if ".booley_project" not in parts:
        return False
    idx = parts.index(".booley_project")
    if idx + 1 >= len(parts):
        return False
    root = parts[idx + 1]
    return (
        root in _ARTIFACT_ROOT_NAMES
        or root.startswith("eval_")
        or root.startswith("worktrees_disabled")
        or root.endswith("_disabled_for_review")
        or "_disabled_for_" in root
        or root.startswith("stale_workspace_snapshots_")
    )


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

CATEGORY_DIRS_DEFAULT: dict[str, tuple[str, ...]] = {
    "rtl": ("rtl/", "fw/", "util/", "data/"),
    "tb": ("tb/", "data/"),
}

# The category wall is an information barrier, not a tidiness rule: an RTL agent
# that can read the testbench writes code that satisfies the test instead of
# the spec, and a TB agent that can read the RTL writes tests that mirror the
# implementation's bugs. Those files are physically absent from the workspace.
CATEGORY_GUARD: dict[str, str] = {
    "rtl": (
        "Work only within the scope listed above. If another file is required, "
        "stop and request a ticket-scope update before editing it. "
        "Do NOT read, create, modify, or write any testbench or verification "
        "files (anything under verif/, tb/, or similar directories). "
        "These files have been removed from your workspace and are "
        "inaccessible — do not attempt to read them via cat, sed, grep, "
        "or any other command."
    ),
    "tb": (
        "Work only within the scope listed above. If another file is required, "
        "stop and request a ticket-scope update before editing it. "
        "Do NOT read, create, modify, or write any RTL files "
        "(anything under rtl/, fw/, or similar directories). "
        "These files have been removed from your workspace and are "
        "inaccessible — do not attempt to read them via cat, sed, grep, "
        "or any other command."
    ),
}

# ---------------------------------------------------------------------------
# Category directory resolution
# ---------------------------------------------------------------------------


def _resolve_main_repo_root(work_dir: Path) -> Path | None:
    """Follow a worktree's ``.git`` file to find the main repository root.

    In a git worktree, ``.git`` is a *file* containing ``gitdir: <path>``
    (pointing into the main repo's ``.git/worktrees/`` directory).
    Returns the main repo root, or ``None`` if *work_dir* is already the
    main repo or has no recognisable ``.git`` file.
    """
    git_path = work_dir / ".git"
    if not git_path.is_file():
        return None
    try:
        content = git_path.read_text(encoding="utf-8").strip()
        if not content.startswith("gitdir:"):
            return None
        gitdir = Path(content.split(":", 1)[1].strip())
        if not gitdir.is_absolute():
            gitdir = (work_dir / gitdir).resolve()
        # gitdir is e.g. /repo/.git/worktrees/<name> — walk up to .git/
        for parent in gitdir.parents:
            if parent.name == ".git":
                return parent.parent
    except (OSError, ValueError):
        pass
    return None


def get_category_dirs(work_dir: Path | None = None) -> dict[str, tuple[str, ...]]:
    """Resolve category source prefixes in *work_dir*, merging with defaults.

    Reads the worktree's authored cores directly so isolation works even
    when the shared_infra module's CWD-based resolution would miss it.
    Falls back to the main repo root (for git worktrees where
    .booley_project/ is gitignored), then shared_infra, then hardcoded defaults.
    Directory prefixes end in ``/``; root-level flat-repository files are exact
    paths so the isolation layer can move the file itself.
    """
    parsed = read_source_dirs_from_toml(work_dir) if work_dir else None
    if parsed is None and work_dir is not None:
        main_root = _resolve_main_repo_root(work_dir)
        if main_root is not None:
            parsed = read_source_dirs_from_toml(main_root)
            if parsed is not None:
                logger.debug(
                    "Resolved category dirs from main repo root %s "
                    "(worktree %s lacks .booley_project/)",
                    main_root,
                    work_dir,
                )
    if parsed is None and work_dir is not None:
        # Caller explicitly scoped to work_dir and neither it nor its main
        # repo root had usable TOML — use hardcoded defaults rather than
        # falling back to a CWD-resolved config that may belong to a
        # different project entirely.
        return CATEGORY_DIRS_DEFAULT
    if parsed is None:
        # No work_dir was supplied — caller is operating in pure CWD mode
        # (legacy human-mode invocation). It is safe to consult the
        # CWD-resolved shared_infra cache here.
        try:
            from booley.runtime.shared_infra import get_rtl_source_dirs, get_tb_source_dirs

            parsed = get_rtl_source_dirs(), get_tb_source_dirs()
        except (
            Exception
        ):  # legacy CWD path unavailable; warn and fall back to default category dirs
            logger.warning(
                "Could not resolve category dirs from project config — "
                "falling back to defaults; isolation may miss project-specific "
                "TB directories (e.g. verif/)",
                exc_info=True,
            )
            return CATEGORY_DIRS_DEFAULT

    from booley.runtime.shared_infra import source_dir_prefixes

    rtl_dirs, tb_dirs = parsed
    rtl_extra = tuple(p for p in source_dir_prefixes(rtl_dirs, work_dir) if "\\" not in p)
    rtl_merged = set(CATEGORY_DIRS_DEFAULT["rtl"]) | set(rtl_extra)
    tb_extra = tuple(p for p in source_dir_prefixes(tb_dirs, work_dir) if "\\" not in p)
    tb_merged = set(CATEGORY_DIRS_DEFAULT["tb"]) | set(tb_extra)
    return {
        "rtl": tuple(sorted(rtl_merged)),
        "tb": tuple(sorted(tb_merged)),
    }


def validate_scope_category(
    scope_files: list[str],
    category: str,
    work_dir: Path | None = None,
) -> str | None:
    """Return error message if any scope file mismatches the category, else None."""
    from booley.runtime.shared_infra import source_path_matches

    allowed_prefixes = get_category_dirs(work_dir)[category]
    bad = [path for path in scope_files if not source_path_matches(path, allowed_prefixes)]
    if bad:
        return (
            f"Scope files don't match category '{category}': "
            + ", ".join(bad[:5])
            + (f" ... and {len(bad) - 5} more" if len(bad) > 5 else "")
        )
    return None


# ---------------------------------------------------------------------------
# Disallowed agent-capability deny patterns (defense-in-depth for category isolation)
# ---------------------------------------------------------------------------


def build_category_deny_patterns(
    category: str,
    work_dir: Path | None = None,
) -> list[str]:
    """Build ``disallowed_agent_capabilities`` patterns blocking access to opposite-category dirs.

    Returns patterns suitable for ``ClaudeAgentOptions.disallowed_agent_capabilities``.
    Covers Bash commands referencing opposite-category paths — the filesystem
    hide is the primary defense; these patterns are a second layer that catches
    commands even if the hide fails or the agent discovers stashed files.
    """
    cat_dirs = get_category_dirs(work_dir)
    opposite = "tb" if category == "rtl" else "rtl"
    deny_prefixes = set(cat_dirs[opposite]) - set(cat_dirs[category])

    patterns: list[str] = []
    for prefix in sorted(deny_prefixes):
        if prefix.endswith(("/", "\\")):
            dir_name = prefix.rstrip("/\\")
            patterns.append(f"Bash(*{dir_name}/*)")
            patterns.append(f"Bash(*{dir_name}\\\\*)")
        else:
            patterns.append(f"Bash(*{prefix}*)")
    return patterns


# ---------------------------------------------------------------------------
# Development-state projection (hides opposite-category review findings)
# ---------------------------------------------------------------------------
#
# Why this exists: the filesystem hide (``hide_opposite_sources``) keeps the
# agent from reading e.g. ``rtl/*`` while running a TB-category Specialist, but
# ``/ticket-logs/.runtime/booley_state.json`` is mounted independently and contains
# reviewer-authored findings like::
#
#     "summary": "RTL extracts and zero-extends byte/halfword load data ...
#                 forwarding dmem_rsp_rdata_i unchanged to wb_if_rdata_o."
#     "fix_suggestion": "Update the named port connections to clk, rst_n,
#                        ex_if_addr_base_i, ex_if_addr_offset_i, ..."
#
# That text leaks RTL implementation/port names into the TB coder's context
# even with filesystem isolation in place — exactly the channel that lets a
# TB coder edit the testbench to *match the RTL* instead of the spec.
#
# Projection strategy: keep summary counts and gating metadata (``met``,
# ``mandatory``, ``ever_met``, ``CRITICAL/MAJOR/MINOR``, ``verify_attempts``),
# drop the prose payload (``pending``, ``resolved``, ``checks``, ``issue_list``)
# from opposite-category criteria. Other state sections pass through unchanged.

# Criterion-key prefixes that describe RTL-side artifacts (their detail leaks
# RTL signal names, code references, fix suggestions naming RTL ports).
# ``review_rtl_*`` is the worst offender; ``lint_/synthesis_/fpga_impl_*``
# carry RTL diagnostics, ``elaborate_*`` carries RTL compile errors.
_RTL_SIDE_PREFIXES: tuple[str, ...] = (
    "review_rtl_",
    "lint_",
    "synthesis_",
    "fpga_impl_",
    "elaborate_",
)

# Criterion-key prefixes that describe TB-side artifacts.
_TB_SIDE_PREFIXES: tuple[str, ...] = ("review_tb_",)

# ``detail`` keys that carry only summary/gating info (safe to expose across
# the boundary). Anything not in this set is stripped from opposite-category
# criteria — better to be over-aggressive than to ship a new leak channel.
_DETAIL_SUMMARY_KEYS: frozenset[str] = frozenset(
    {
        "CRITICAL",
        "MAJOR",
        "MINOR",
        "issues",
        "original_issues",
        "verify_attempts",
        "total_verify_cycles",
        "elapsed_s",
        "warnings",
    }
)


def _is_opposite_category_criterion(key: str, category: str) -> bool:
    """True if *key* belongs to the category opposite *category*."""
    if category == "tb":
        return key.startswith(_RTL_SIDE_PREFIXES)
    if category == "rtl":
        return key.startswith(_TB_SIDE_PREFIXES)
    return False


def _strip_detail_findings(detail: dict[str, Any]) -> dict[str, Any]:
    """Return a copy of *detail* containing only summary/count keys."""
    return {k: v for k, v in detail.items() if k in _DETAIL_SUMMARY_KEYS}


def project_state_for_category(
    state: dict[str, Any],
    category: str,
) -> dict[str, Any]:
    """Return a category-projected copy of a ``booley_state.json`` dict.

    Strips ``detail.pending`` / ``detail.resolved`` / ``detail.checks`` etc.
    from criteria whose key prefix names the opposite category. Counts and
    gating fields survive so threshold logic still works for an outside
    observer. Other state sections, including ``timeline``, pass through
    unchanged; the agent-capability-call ledger is not the leak channel.

    Idempotent and safe to call on non-dict input (returns *state* unchanged).
    """
    if not isinstance(state, dict) or category not in ("rtl", "tb"):
        return state
    out = dict(state)
    criteria = dict(out.get("criteria") or {})
    if not criteria:
        return out
    projected: dict[str, Any] = {}
    for key, entry in criteria.items():
        if (
            isinstance(entry, dict)
            and _is_opposite_category_criterion(key, category)
            and isinstance(entry.get("detail"), dict)
        ):
            new_entry = dict(entry)
            new_entry["detail"] = _strip_detail_findings(entry["detail"])
            projected[key] = new_entry
        else:
            projected[key] = entry
    out["criteria"] = projected
    return out


@contextmanager
def filter_state_file_for_category(
    state_path: Path | None,
    category: str,
) -> Generator[bool]:
    """Swap ``state_path`` for a category-projected copy for the duration.

    On enter: read the original bytes, write a projected copy in place.
    On exit (finally): restore the original bytes verbatim.

    Yields True if filtering was applied, False if skipped (no path, file
    missing, category neither rtl nor tb, or projection produced no change).

    Safety: the agent (LLM) is the only consumer of this file during the
    block; nested MCP tools the agent may invoke (``sim`` in elab-only mode for the
    coder, none for the reviewer per ``nested_mcp_capabilities``) do not
    write to state, so the unconditional restore-on-exit cannot clobber
    concurrent writes. If that invariant ever breaks, switch to a merging
    restore that re-reads and diffs criteria before overwriting.
    """
    if state_path is None or category not in ("rtl", "tb"):
        yield False
        return
    try:
        if not state_path.is_file():
            yield False
            return
    except OSError:
        yield False
        return

    try:
        original_bytes = state_path.read_bytes()
    except OSError as exc:
        logger.warning(
            "filter_state_file_for_category: could not read %s (%s) — passing through unfiltered",
            state_path,
            exc,
        )
        yield False
        return

    try:
        data = json.loads(original_bytes.decode("utf-8-sig"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        logger.warning(
            "filter_state_file_for_category: %s is not valid JSON (%s) — "
            "passing through unfiltered",
            state_path,
            exc,
        )
        yield False
        return

    projected = project_state_for_category(data, category)

    applied = False
    restored = False

    def _restore() -> None:
        # Idempotent: the signal handler restores first, then the finally
        # runs on the way out — the second call must not re-write the file.
        nonlocal restored
        if applied and not restored:
            restored = True
            _restore_state_bytes(state_path, original_bytes)

    try:
        # Same kill hazard as the stash (F-34), worse payload: a projected
        # state file left in place has had the opposite category's findings
        # stripped out for good.
        with _restore_on_fatal_signal(_restore):
            if projected is not data:
                _write_projected_state(state_path, projected, category)
                applied = True
            yield applied
    finally:
        _restore()


def _write_projected_state(state_path: Path, projected: dict, category: str) -> None:
    """Atomically overwrite ``state_path`` with the category-projected copy.

    Writes to a ``.filter_tmp`` sibling then replaces, mirroring
    ``DevelopmentState.save`` semantics.
    """
    tmp_path = state_path.with_suffix(state_path.suffix + ".filter_tmp")
    tmp_path.write_text(
        json.dumps(projected, indent=2),
        encoding="utf-8",
    )
    tmp_path.replace(state_path)
    logger.info(
        "Isolation: projected %s for %s endpoint run (opposite-category detail stripped)",
        state_path.name,
        category,
    )


def _restore_state_bytes(state_path: Path, original_bytes: bytes) -> None:
    """Restore the original state bytes verbatim; log on failure (no raise)."""
    try:
        state_path.write_bytes(original_bytes)
    except OSError:
        logger.exception(
            "filter_state_file_for_category: could not restore %s — "
            "manual recovery may be required",
            state_path,
        )


# ---------------------------------------------------------------------------
# Stash locking (prevents agent from reading hidden files via /tmp)
# ---------------------------------------------------------------------------


def _lock_stash(stash_dir: Path) -> None:
    """Remove all permissions on *stash_dir* so agent subprocesses can't read it.

    When the developer and agent share a filesystem (e.g. both run inside the
    same Docker container), moving files to /tmp is not enough -- the agent can
    ``find /tmp/booley_isolation_*`` and read the stashed sources.  Stripping
    permissions blocks this: on Linux the owner cannot read a mode-000 directory.
    The owner *can* always ``chmod`` it back (no capability required), so the
    ``finally`` restore path still works.
    """
    try:
        stash_dir.chmod(0o000)
    except OSError:
        logger.warning("Could not lock stash dir %s — agent may read hidden files", stash_dir)


def _unlock_stash(stash_dir: Path) -> None:
    """Restore owner rwx on *stash_dir* so the finally block can move files back."""
    try:
        stash_dir.chmod(0o700)
    except OSError:
        logger.error("Could not unlock stash dir %s — manual cleanup needed", stash_dir)


# ---------------------------------------------------------------------------
# Special-file removal (FIFOs / sockets break cross-filesystem moves)
# ---------------------------------------------------------------------------


def _remove_special_files(directory: Path) -> None:
    """Remove FIFOs and sockets that break cross-filesystem shutil.move()."""
    for p in directory.rglob("*"):
        try:
            if not p.is_file() and not p.is_dir() and not p.is_symlink():
                mode = p.lstat().st_mode
                if stat.S_ISFIFO(mode) or stat.S_ISSOCK(mode):
                    p.unlink()
                    logger.debug("Removed special file before isolation: %s", p)
        except OSError:
            pass


# ---------------------------------------------------------------------------
# Nested opposite-category directory detection
# ---------------------------------------------------------------------------


def _find_nested_opposite_dirs(
    work_dir: Path,
    hide_prefixes: set[str],
) -> list[Path]:
    """Find opposite-category dirs nested inside .booley_project/.

    The top-level hide handles work_dir/rtl/ etc., but dirs like
    .booley_project/eval_debug/*/rtl/ leak cross-category sources past
    the guard.
    """
    bp = work_dir / ".booley_project"
    if not bp.is_dir():
        return []
    nested: list[Path] = []
    dir_names = {p.rstrip("/\\") for p in hide_prefixes if p.endswith(("/", "\\"))}
    for child in bp.rglob("*"):
        if _is_artifact_path(child):
            continue
        if child.is_dir() and child.name in dir_names:
            nested.append(child)
    return nested


# ---------------------------------------------------------------------------
# Stash crash-safety: manifests, healing, signal handling
# ---------------------------------------------------------------------------
#
# The stash moves *tracked worktree files* into /tmp. The ``finally`` block
# covers exceptions, but not a kill: SIGTERM/SIGHUP terminate the process
# without unwinding, and SIGKILL cannot be caught at all. A killed reviewer
# left six tracked files deleted in ``git status`` with nothing pointing at
# the stash, and the next run did not heal it (SETUP-F-34). Two defenses:
#
#   1. A sidecar manifest written next to each stash (the stash dir itself is
#      chmod 000) records what moved where, updated after every move.
#   2. Every stash entry point first heals manifests left by a dead process,
#      so a later run repairs an earlier run's damage even after SIGKILL.
#
# Signal handlers close the SIGTERM/SIGHUP gap directly, so healing is only
# needed for the uncatchable cases.

_MANIFEST_SUFFIX = ".manifest.json"
_STASH_PREFIXES = ("booley_isolation_", "booley_hide_files_")

# Manifests owned by a *live* context in this process. Healing skips these —
# tb_coder nests hide_specific_files inside hide_opposite_sources, so "my own
# pid" is not enough to tell a leftover from an in-flight stash.
_ACTIVE_MANIFESTS: set[Path] = set()

# A stash stops being trustworthy evidence of *current* damage once it is a
# day old: by then the user has long since noticed the missing files, and a
# hole in the worktree is far more likely to be their own intent (deleted the
# dir, switched branches) than the leftover of a run they no longer remember.
_HEAL_MAX_AGE_S = 24 * 3600

# Consecutive failed heal attempts before a manifest is taken out of rotation.
_HEAL_MAX_ATTEMPTS = 3


def _manifest_path(stash_dir: Path) -> Path:
    """Sidecar manifest path for *stash_dir* (outside it — the dir is chmod 000)."""
    return stash_dir.with_name(stash_dir.name + _MANIFEST_SUFFIX)


def _write_stash_manifest(
    stash_dir: Path,
    work_dir: Path,
    moved: list[tuple[Path, Path]],
) -> None:
    """Record the current stash contents so a killed run can be healed later.

    Rewritten after every move: a process killed mid-stash still leaves an
    accurate record of what it had already taken out of the worktree.
    """
    payload = {
        "pid": os.getpid(),
        "work_dir": str(work_dir),
        "stash_dir": str(stash_dir),
        "created": time.time(),
        "moved": [[str(src), str(dst)] for src, dst in moved],
    }
    path = _manifest_path(stash_dir)
    try:
        path.write_text(json.dumps(payload), encoding="utf-8")
    except OSError as exc:
        logger.warning("Could not write isolation stash manifest %s: %s", path, exc)
        return
    _ACTIVE_MANIFESTS.add(path)


def _clear_stash_manifest(stash_dir: Path) -> None:
    """Drop the manifest — the stash it described is fully restored."""
    path = _manifest_path(stash_dir)
    _ACTIVE_MANIFESTS.discard(path)
    with suppress(OSError):
        path.unlink(missing_ok=True)


def _pid_alive(pid: Any) -> bool:
    """True when *pid* still names a running process (unknown ⇒ assume alive)."""
    if not isinstance(pid, int) or pid <= 0:
        return False
    return is_pid_alive(pid)


def _manifest_is_trustworthy(manifest: Path) -> bool:
    """True when *manifest* is a plain file this user could have written.

    ``/tmp`` is world-writable, so on a shared host anyone can drop a
    ``booley_isolation_*.manifest.json`` there — and healing *moves files into
    the worktree*, which would make a forged manifest a file-write primitive
    against whoever runs the next endpoint. One ``lstat`` closes that: the manifest
    must be a regular file (not a symlink aimed at someone else's data) owned
    by this uid.
    """
    try:
        st = manifest.lstat()
    except OSError:
        return False
    if stat.S_ISLNK(st.st_mode) or not stat.S_ISREG(st.st_mode):
        return False
    getuid = getattr(os, "getuid", None)
    return getuid is None or st.st_uid == getuid()


def _manifest_age_s(created: Any) -> float | None:
    """Seconds since the stash was taken; ``None`` when the manifest won't say."""
    if isinstance(created, bool) or not isinstance(created, (int, float)):
        return None
    return max(0.0, time.time() - float(created))


def _expected_stash_dir(manifest: Path, recorded: Any) -> Path | None:
    """The stash dir *manifest* describes, when it is the one its name implies.

    ``_write_stash_manifest`` always names the sidecar ``<stash_dir>.manifest.json``,
    so a recorded ``stash_dir`` that disagrees is corrupt or forged.
    """
    if not isinstance(recorded, str) or not recorded:
        return None
    expected = manifest.with_name(manifest.name[: -len(_MANIFEST_SUFFIX)])
    return expected if Path(recorded) == expected else None


def _heal_entries(
    work_dir: Path,
    stash_dir: Path,
    recorded: Any,
) -> list[tuple[Path, Path]] | None:
    """Validate a manifest's move list; ``None`` when anything is out of bounds.

    Every ``src`` must land inside *work_dir* and every ``dst`` inside
    *stash_dir*, with no ``..`` to walk back out lexically. A manifest that
    says otherwise is corrupt or hostile, and healing it would write outside
    the worktree — so the whole list is refused rather than partly applied.
    The check also catches plain bugs: a path that escaped the worktree is
    never something we put there.
    """
    if not isinstance(recorded, list):
        return None
    entries: list[tuple[Path, Path]] = []
    for pair in recorded:
        if not isinstance(pair, list) or len(pair) != 2:
            return None
        src, dst = Path(str(pair[0])), Path(str(pair[1]))
        if not src.is_absolute() or not dst.is_absolute():
            return None
        if ".." in src.parts or ".." in dst.parts:
            return None
        if not src.is_relative_to(work_dir) or not dst.is_relative_to(stash_dir):
            return None
        entries.append((src, dst))
    return entries


def _retire_manifest(manifest: Path, reason: str) -> None:
    """Take *manifest* out of the healing rotation without losing the record.

    Renamed rather than deleted (the ``.retired`` name no longer matches the
    heal glob) so the stash it describes stays traceable for a human.
    """
    retired = manifest.with_name(manifest.name + ".retired")
    try:
        manifest.replace(retired)
    except OSError:
        with suppress(OSError):
            manifest.unlink(missing_ok=True)
        retired = manifest
    logger.error(
        "Isolation: not healing stash manifest %s (%s) — renamed to %s; "
        "if files are still missing from the worktree, restore them from git",
        manifest,
        reason,
        retired,
    )


def _note_heal_failure(manifest: Path, exc: BaseException) -> None:
    """Count a failed heal attempt and eventually stop retrying it.

    Healing runs *before* the caller's own isolation, so an escaping error
    would kill the very run it exists to protect — and because the manifest is
    only cleared after a successful restore, it would kill every future run
    too, until someone deleted ``/tmp/*.manifest.json`` by hand. So a failure
    is logged, counted, and after a few attempts the manifest is retired.
    """
    logger.warning("Isolation: could not heal stranded stash %s: %s", manifest, exc)
    try:
        data = json.loads(manifest.read_text(encoding="utf-8"))
        attempts = int(data.get("heal_failures", 0)) + 1
        data["heal_failures"] = attempts
        manifest.write_text(json.dumps(data), encoding="utf-8")
    except (OSError, ValueError, TypeError):
        # Unreadable or unwritable manifest: it can never be healed *or*
        # counted, so retire it now instead of tripping over it forever.
        attempts = _HEAL_MAX_ATTEMPTS
    if attempts >= _HEAL_MAX_ATTEMPTS:
        _retire_manifest(manifest, f"healing failed {attempts}x: {exc}")


def _discard_stale_stash_copy(work_dir: Path, src: Path, dst: Path) -> None:
    """Dispose of a stashed copy whose worktree original is back in place.

    The live tree wins, always: it is what the user has been working on since
    the crash. An identical stash copy is simply dropped; one that differs is
    parked under ``isolation_conflicts/`` so the older bytes stay recoverable.
    """
    if _same_content(src, dst):
        logger.info("Isolation: dropped stale identical stash copy of %s", src)
        return  # caller rmtree's the stash dir
    conflict_root = work_dir / ".booley_project" / "isolation_conflicts" / compact_utc_now()
    rel = src.name
    with suppress(ValueError):
        rel = src.relative_to(work_dir).as_posix()
    conflict_dst = conflict_root / rel
    conflict_dst.parent.mkdir(parents=True, exist_ok=True)
    shutil.move(str(dst), str(conflict_dst))
    logger.warning(
        "Isolation: %s is back in the worktree and differs from the stranded "
        "stash copy — keeping YOUR file, parked the stashed one at %s",
        src,
        conflict_dst,
    )


def _heal_restore(work_dir: Path, stash_dir: Path, moved: list[tuple[Path, Path]]) -> None:
    """Move stashed entries back, but only into holes the stash actually left.

    Cross-run healing is a different problem from same-run restore. By the
    time it runs, the usual thing has already happened: the user saw six
    tracked files deleted in ``git status``, ran ``git restore .``, and kept
    working for days. Restoring unconditionally there would bury that work
    under a snapshot taken before the killed run — so a *present* ``src`` is
    read as "already recovered", the live tree is never touched, and only the
    stash copy is disposed of. A missing ``src`` is the damage the stash
    caused, and that is the only case healing repairs.
    """
    _unlock_stash(stash_dir)
    for src, dst in moved:
        if not dst.exists() and not dst.is_symlink():
            continue  # never made it into the stash, or already restored
        if src.exists() or src.is_symlink():
            _discard_stale_stash_copy(work_dir, src, dst)
            continue
        src.parent.mkdir(parents=True, exist_ok=True)
        shutil.move(str(dst), str(src))
        logger.warning("Isolation: healed %s from a stash stranded by a killed run", src)


def _read_heal_candidate(work_dir: Path, manifest: Path) -> dict | None:
    """Load *manifest* when it describes a stash this run should heal.

    ``None`` means "not my business": unreadable, another worktree's, or still
    owned by a process that is very much alive.
    """
    if not _manifest_is_trustworthy(manifest):
        logger.warning(
            "Isolation: ignoring stash manifest %s — not a regular file owned by this user",
            manifest,
        )
        return None
    try:
        data = json.loads(manifest.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    if not isinstance(data, dict) or str(data.get("work_dir", "")) != str(work_dir):
        return None
    if _pid_alive(data.get("pid")):
        return None
    return data


def _heal_plan(
    work_dir: Path,
    manifest: Path,
    data: dict,
) -> tuple[Path, list[tuple[Path, Path]]] | None:
    """Validated (stash_dir, moved) for a heal, or ``None`` after retiring it.

    Everything that fails here is unfixable by retrying — too old to trust, or
    naming paths we would refuse to touch — so the manifest is retired rather
    than left to be re-examined by every future run.
    """
    age = _manifest_age_s(data.get("created"))
    if age is None or age > _HEAL_MAX_AGE_S:
        _retire_manifest(
            manifest,
            "unknown age" if age is None else f"stash is {age / 3600:.1f}h old",
        )
        return None
    stash_dir = _expected_stash_dir(manifest, data.get("stash_dir"))
    if stash_dir is None:
        _retire_manifest(manifest, "recorded stash_dir does not match the manifest name")
        return None
    moved = _heal_entries(work_dir, stash_dir, data.get("moved"))
    if moved is None:
        _retire_manifest(manifest, "move list points outside the worktree or the stash")
        return None
    return stash_dir, moved


def _heal_one_stash(work_dir: Path, manifest: Path) -> Path | None:
    """Heal a single stranded stash. Returns its dir, or ``None`` when skipped."""
    data = _read_heal_candidate(work_dir, manifest)
    if data is None:
        return None
    plan = _heal_plan(work_dir, manifest, data)
    if plan is None:
        return None
    stash_dir, moved = plan

    logger.warning(
        "Healing stranded isolation stash %s from dead pid %s (%d entr%s)",
        stash_dir,
        data.get("pid"),
        len(moved),
        "y" if len(moved) == 1 else "ies",
    )
    if stash_dir.is_dir():
        _heal_restore(work_dir, stash_dir, moved)
    shutil.rmtree(stash_dir, ignore_errors=True)
    manifest.unlink(missing_ok=True)
    return stash_dir


def heal_stranded_stashes(work_dir: Path) -> list[Path]:
    """Restore stashes left behind by killed runs for *work_dir*.

    Best-effort by construction — this function must never raise. It runs
    before the caller's own isolation and outside its ``try``, so an escaping
    exception would abort the run it is meant to help (and, with the manifest
    left in place, every run after it). Each manifest is therefore healed
    inside its own guard: one that cannot be restored is logged, counted and
    eventually retired, while the rest still heal.

    A manifest is only acted on when its owning process is gone, it is not
    owned by a live context in this process, it is young enough to still
    describe the current worktree, and it names only paths inside *work_dir*.
    Returns the healed stash dirs.
    """
    healed: list[Path] = []
    try:
        candidates = sorted(Path(tempfile.gettempdir()).glob(f"*{_MANIFEST_SUFFIX}"))
    except OSError as exc:
        logger.warning("Isolation: could not scan for stranded stashes: %s", exc)
        return healed
    for manifest in candidates:
        if not manifest.name.startswith(_STASH_PREFIXES):
            continue
        if manifest in _ACTIVE_MANIFESTS:
            continue
        try:
            stash_dir = _heal_one_stash(work_dir, manifest)
        except Exception as exc:  # noqa: BLE001 — healing must never break the run
            _note_heal_failure(manifest, exc)
            continue
        if stash_dir is not None:
            healed.append(stash_dir)
    return healed


@contextmanager
def _restore_on_fatal_signal(restore: Callable[[], None]) -> Generator[None]:
    """Run *restore* when a fatal signal would otherwise skip the ``finally``.

    SIGTERM/SIGHUP default to terminating the process outright — no stack
    unwind, no ``finally``, worktree left mutilated. Handling them restores
    the stash first, then re-raises the signal with the default disposition
    so the caller's kill semantics are preserved. SIGINT chains to Python's
    own handler, which raises KeyboardInterrupt as usual.

    Only the main thread can install handlers; elsewhere this is a no-op
    (the ``finally`` path still covers ordinary exceptions).
    """
    if threading.current_thread() is not threading.main_thread():
        yield
        return

    signals = [
        s
        for s in (
            getattr(signal, "SIGTERM", None),
            getattr(signal, "SIGHUP", None),
            getattr(signal, "SIGINT", None),
        )
        if s is not None
    ]
    previous: dict[int, Any] = {}

    def _handler(signum: int, frame: Any) -> None:
        try:
            restore()
        finally:
            prev = previous.get(signum)
            if callable(prev):
                prev(signum, frame)  # e.g. default_int_handler -> KeyboardInterrupt
            else:
                signal.signal(signum, prev if prev is not None else signal.SIG_DFL)
                os.kill(os.getpid(), signum)

    installed: list[int] = []
    for sig in signals:
        try:
            previous[sig] = signal.signal(sig, _handler)
        except (OSError, ValueError):  # pragma: no cover — platform-dependent
            continue
        installed.append(sig)
    try:
        yield
    finally:
        for sig in installed:
            with suppress(OSError, ValueError):
                signal.signal(sig, previous[sig])


# ---------------------------------------------------------------------------
# Stash / restore helpers
# ---------------------------------------------------------------------------


def _stash_opposite_dirs(
    work_dir: Path,
    category: str,
    hide_prefixes: set[str],
    moved: list[tuple[Path, Path]],
) -> Path | None:
    """Move top-level opposite sources and nested source dirs to a stash."""
    stash_dir: Path | None = None

    for prefix in sorted(hide_prefixes):
        src = work_dir / prefix.rstrip("/\\")
        if src.is_dir() or src.is_file() or src.is_symlink():
            if stash_dir is None:
                stash_dir = Path(tempfile.mkdtemp(prefix="booley_isolation_"))
            if src.is_dir():
                _remove_special_files(src)
            dst = stash_dir / prefix.rstrip("/\\")
            # A prefix may be multi-component (stealth-cores layouts, ADR 0036,
            # resolve opposite-category source dirs to paths like
            # ``.booley_project/cores/sim/`` -- themselves discovery symlinks).
            # ``shutil.move`` recreates the entry (incl. the symlink) under
            # ``dst``, so its parent must exist first, exactly as the nested
            # loop below already does -- otherwise the move raises
            # FileNotFoundError and the whole endpoint run dies.
            dst.parent.mkdir(parents=True, exist_ok=True)
            shutil.move(str(src), str(dst))
            moved.append((src, dst))
            # Manifest after every move: a kill between two moves must still
            # leave a complete record of what has left the worktree (F-34).
            _write_stash_manifest(stash_dir, work_dir, moved)
            logger.info("Isolation: hid %s from %s agent", prefix, category)

    for nested_src in _find_nested_opposite_dirs(work_dir, hide_prefixes):
        if stash_dir is None:
            stash_dir = Path(tempfile.mkdtemp(prefix="booley_isolation_"))
        _remove_special_files(nested_src)
        rel = nested_src.relative_to(work_dir)
        dst = stash_dir / rel
        dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.move(str(nested_src), str(dst))
        moved.append((nested_src, dst))
        _write_stash_manifest(stash_dir, work_dir, moved)
        logger.info("Isolation: hid nested %s from %s agent", rel, category)

    return stash_dir


def _same_content(a: Path, b: Path) -> bool:
    """True when *a* and *b* are byte-identical trees / files / symlinks.

    Deliberately strict: same entry names at every level, same symlink targets,
    same file bytes (``shallow=False`` — mtime and size alone are not enough
    when a re-staged copy is the thing being compared).
    """
    if a.is_symlink() or b.is_symlink():
        return a.is_symlink() and b.is_symlink() and a.readlink() == b.readlink()
    if a.is_dir() != b.is_dir():
        return False
    if not a.is_dir():
        return filecmp.cmp(a, b, shallow=False)
    names_a = sorted(p.name for p in a.iterdir())
    if names_a != sorted(p.name for p in b.iterdir()):
        return False
    return all(_same_content(a / name, b / name) for name in names_a)


def _resolve_restore_conflict(work_dir: Path, src: Path, dst: Path) -> None:
    """Clear *src* so the stashed *dst* can move back into its place.

    FuseSoC re-stages sources during a endpoint run, so the hidden tree is often
    recreated byte-for-byte while it is stashed. Quarantining that identical
    copy accreted a full tb-tree under ``isolation_conflicts/`` on every single
    run for no information gain (SETUP-F-43) — drop it instead, and quarantine
    only a tree that genuinely differs from what was stashed.
    """
    if _same_content(src, dst):
        if src.is_dir() and not src.is_symlink():
            shutil.rmtree(src, ignore_errors=True)
        else:
            src.unlink(missing_ok=True)
        logger.debug("Isolation: dropped byte-identical recreated %s", src)
        return

    conflict_root = work_dir / ".booley_project" / "isolation_conflicts" / compact_utc_now()
    rel = src.name
    with suppress(ValueError):
        rel = src.relative_to(work_dir).as_posix()
    conflict_dst = conflict_root / rel
    conflict_dst.parent.mkdir(parents=True, exist_ok=True)
    shutil.move(str(src), str(conflict_dst))
    logger.warning(
        "Isolation restore conflict: quarantined recreated %s at %s",
        src,
        conflict_dst,
    )


def _restore_stashed(
    work_dir: Path,
    stash_dir: Path | None,
    moved: list[tuple[Path, Path]],
) -> None:
    """Restore stashed dirs and clean up the stash directory.

    Idempotent: ``moved`` is emptied as entries come back, so a second call
    (signal handler first, then the ``finally``) is a no-op rather than a
    second round of moves.
    """
    if stash_dir is not None:
        _unlock_stash(stash_dir)
    while moved:
        src, dst = moved.pop(0)
        # A partially-restored stash (killed mid-restore) has entries whose
        # dst is already gone — skip rather than raise, or one missing entry
        # would strand the rest.
        if not dst.exists() and not dst.is_symlink():
            logger.warning("Isolation: nothing to restore at %s (already moved back?)", dst)
            continue
        # BUG FIX: was dst.parent (stash dir, already exists) -- must be
        # src.parent (worktree destination, may have been deleted by agent).
        src.parent.mkdir(parents=True, exist_ok=True)
        if src.exists() or src.is_symlink():
            _resolve_restore_conflict(work_dir, src, dst)
        shutil.move(str(dst), str(src))
        logger.info("Isolation: restored %s", src.name)
    if stash_dir is not None:
        _clear_stash_manifest(stash_dir)
        shutil.rmtree(stash_dir, ignore_errors=True)


# ---------------------------------------------------------------------------
# Public context managers
# ---------------------------------------------------------------------------


@contextmanager
def hide_opposite_sources(
    work_dir: Path,
    category: str,
    *,
    category_dirs: dict[str, tuple[str, ...]] | None = None,
) -> Generator[list[str]]:
    """Move sources of the *opposite* category out of the worktree.

    Inside a Docker sandbox the worktree is the only volume mounted, so moving
    configured directories and flat-repository files makes them inaccessible
    to the agent. Also hides matching dirs nested under .booley_project/.

    Restoration is guaranteed by a ``finally`` block (exceptions), a signal
    handler (SIGTERM/SIGHUP/SIGINT) and, for kills nothing can catch, by
    healing the manifest on the next run (see the crash-safety section).

    Yields the list of hidden directory names (for logging/testing).
    """
    heal_stranded_stashes(work_dir)

    cat_dirs = category_dirs or get_category_dirs(work_dir)
    opposite = "tb" if category == "rtl" else "rtl"
    hide_prefixes = set(cat_dirs[opposite]) - set(cat_dirs[category])

    stash_dir: Path | None = None
    moved: list[tuple[Path, Path]] = []

    def _restore() -> None:
        _restore_stashed(work_dir, stash_dir, moved)

    try:
        with _restore_on_fatal_signal(_restore):
            stash_dir = _stash_opposite_dirs(
                work_dir,
                category,
                hide_prefixes,
                moved,
            )
            if stash_dir is not None:
                _lock_stash(stash_dir)
            yield [src.name for src, _ in moved]
    finally:
        _restore()


@contextmanager
def hide_specific_files(
    work_dir: Path,
    files: list[str],
) -> Generator[list[str]]:
    """Move specific files out of the worktree for isolation.

    Same pattern as ``hide_opposite_sources`` but at file level — including
    its crash safety: manifest, signal handler, heal-on-next-run (F-34).
    Stash dir is chmod-locked (see ``_lock_stash``).
    Yields the list of hidden file names.
    """
    heal_stranded_stashes(work_dir)

    stash_dir: Path | None = None
    moved: list[tuple[Path, Path]] = []

    def _restore() -> None:
        _restore_stashed(work_dir, stash_dir, moved)

    try:
        with _restore_on_fatal_signal(_restore):
            for rel_path in files:
                src = work_dir / rel_path
                if not src.is_file():
                    logger.warning("hide_specific_files: %s not found, skipping", rel_path)
                    continue
                if stash_dir is None:
                    stash_dir = Path(tempfile.mkdtemp(prefix="booley_hide_files_"))
                dst = stash_dir / rel_path
                dst.parent.mkdir(parents=True, exist_ok=True)
                shutil.move(str(src), str(dst))
                moved.append((src, dst))
                _write_stash_manifest(stash_dir, work_dir, moved)
                logger.info("File isolation: hid %s", rel_path)
            if stash_dir is not None:
                _lock_stash(stash_dir)
            yield [src.name for src, _ in moved]
    finally:
        _restore()


# ---------------------------------------------------------------------------
# Artifact cleanup
# ---------------------------------------------------------------------------


def clean_sim_artifacts(work_dir: Path) -> None:
    """Remove simulator compilation artifacts that embed RTL implementation.

    Compiled binaries (Icarus .vvp, Verilator obj_dir/) contain full design
    structure -- signal names, parameters, logic bytecode -- which lets a coder
    reverse-engineer hidden sources.  Cleaning these before isolation prevents
    that leakage channel.  Only the ``work/`` subdirectory is removed; config
    files and the output_dir itself are preserved.
    """
    try:
        from booley.runtime.shared_infra import get_sim_output_dir

        sim_dir = get_sim_output_dir(work_dir)
    except Exception:  # noqa: BLE001 — best-effort sim-output-dir read; falls back to work_dir/"sim"
        sim_dir = work_dir / "sim"

    work_subdir = sim_dir / "work"
    if work_subdir.is_dir():
        shutil.rmtree(work_subdir, ignore_errors=True)
        logger.info("Isolation: cleaned sim artifacts %s", work_subdir)

    obj_dir = sim_dir / "obj_dir"
    if obj_dir.is_dir():
        shutil.rmtree(obj_dir, ignore_errors=True)
        logger.info("Isolation: cleaned Verilator obj_dir %s", obj_dir)


# ---------------------------------------------------------------------------
# Shadow package removal
# ---------------------------------------------------------------------------


def remove_shadow_package(work_dir: Path) -> None:
    """Remove agent-created booley/ directory that shadows the installed package.

    Agents running inside Docker at /work may create /work/booley/ (e.g. via
    file writes or package installs).  This persists on the host worktree and
    causes Python's import machinery to resolve ``import booley`` to the
    partial copy instead of the installed package -- breaking style-guide
    lookups and anything else that uses ``importlib.resources.files("booley")``.
    """
    shadow = Path(work_dir) / "booley"
    if not shadow.is_dir():
        return
    if (shadow / "data" / "refs").is_dir():
        return
    try:
        shutil.rmtree(shadow)
        logger.info("Removed shadow booley/ package from %s", work_dir)
    except OSError as exc:
        logger.warning("Failed to remove shadow booley/ from %s: %s", work_dir, exc)
