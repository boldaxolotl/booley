"""Category isolation for disposable source snapshots and workspace cleanup."""

from __future__ import annotations

import logging
import shutil
from dataclasses import dataclass
from pathlib import Path

from booley.mcp.base import read_source_dirs_from_toml

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

CATEGORY_DIRS_DEFAULT: dict[str, tuple[str, ...]] = {
    "rtl": ("rtl/", "fw/", "util/", "data/"),
    "tb": ("tb/", "data/"),
}

# ---------------------------------------------------------------------------
# Category directory resolution
# ---------------------------------------------------------------------------


def _resolve_main_repo_root(work_dir: Path) -> Path | None:
    """Follow a worktree's ``.git`` file to find the main repository root.

    In a git worktree, ``.git`` is a *file* containing ``gitdir: <path>``
    (pointing into the main repo's ``.git/worktrees/`` directory).
    Returns the main Project Root, or ``None`` if *work_dir* is already the
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
    Falls back to the main Project Root (for git worktrees where
    .booley/ is gitignored), then shared_infra, then hardcoded defaults.
    Directory prefixes end in ``/``; root-level flat-repository files are exact
    paths so snapshot isolation can remove the file itself.
    """
    parsed = read_source_dirs_from_toml(work_dir) if work_dir else None
    if parsed is None and work_dir is not None:
        main_root = _resolve_main_repo_root(work_dir)
        if main_root is not None:
            parsed = read_source_dirs_from_toml(main_root)
            if parsed is not None:
                logger.debug(
                    "Resolved category dirs from main Project Root %s "
                    "(worktree %s lacks .booley/)",
                    main_root,
                    work_dir,
                )
    if parsed is None and work_dir is not None:
        # Caller explicitly scoped to work_dir and neither it nor its main
        # Project Root had usable TOML — use hardcoded defaults rather than
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
            Exception  # legacy CWD fallback must remain best-effort
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


def _find_nested_opposite_dirs(
    work_dir: Path,
    hide_prefixes: set[str],
) -> list[Path]:
    """Find opposite-category dirs nested inside .booley/.

    The top-level hide handles work_dir/rtl/ etc., but dirs like
    .booley/eval_debug/*/rtl/ leak cross-category sources past
    the guard.
    """
    bp = work_dir / ".booley"
    if not bp.is_dir():
        return []
    nested: list[Path] = []
    dir_names = {p.rstrip("/\\") for p in hide_prefixes if p.endswith(("/", "\\"))}
    for child in bp.rglob("*"):
        if child.is_dir() and child.name in dir_names:
            nested.append(child)
    return nested


@dataclass(frozen=True)
class WorkspaceSnapshot:
    """A disposable workspace plus the real path it represents."""

    real_root: Path
    snapshot_root: Path


def _validate_snapshot_path(root: Path, path: Path) -> None:
    """Reject removal outside the copy, including through symlink ancestors."""
    if path == root or ".." in path.parts or not path.is_relative_to(root):
        raise ValueError(f"source path is outside the disposable snapshot: {path}")
    if not path.parent.resolve().is_relative_to(root):
        raise ValueError(f"source parent escapes the disposable snapshot: {path}")


def hide_opposite_sources(
    snapshot: WorkspaceSnapshot,
    category: str,
    *,
    category_dirs: dict[str, tuple[str, ...]],
    cwd: Path | None = None,
) -> list[str]:
    """Permanently remove opposite-category sources from a disposable copy.

    The owner discards the entire snapshot after the agent call. No live
    sources are moved and no restoration is needed.
    """
    root = snapshot.snapshot_root.resolve()
    real = snapshot.real_root.resolve()
    if root.is_relative_to(real) or real.is_relative_to(root):
        raise ValueError("disposable snapshot must be separate from the live worktree")
    if category not in ("rtl", "tb"):
        raise ValueError(f"unknown isolation category {category!r}")
    opposite = "tb" if category == "rtl" else "rtl"
    prefixes = set(category_dirs[opposite]) - set(category_dirs[category])
    paths = [root / prefix.rstrip("/\\") for prefix in sorted(prefixes)]
    paths.extend(_find_nested_opposite_dirs(root, prefixes))
    own_paths = [root / prefix.rstrip("/\\") for prefix in category_dirs[category]]
    protected = [*own_paths, cwd or root, root / ".git"]
    for path in paths:
        _validate_snapshot_path(root, path)
        if any(own.is_relative_to(path) for own in protected):
            raise ValueError(f"opposite source contains own-category sources or agent cwd: {path}")
    hidden: list[str] = []
    for path in paths:
        if path.is_symlink() or path.is_file():
            path.unlink()
        elif path.is_dir():
            shutil.rmtree(path)
        else:
            continue
        hidden.append(path.name)
        logger.info("Isolation: removed %s from %s snapshot", path.relative_to(root), category)
    return hidden


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
