"""Category isolation for disposable source snapshots and workspace cleanup."""

from __future__ import annotations

import json
import logging
import os
import shutil
import tomllib
from dataclasses import dataclass
from pathlib import Path

from booley.flows.endpoint_diff import read_source_dirs_from_toml

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


_SOURCE_SUFFIXES = frozenset(
    {".v", ".vh", ".sv", ".svh", ".c", ".cc", ".cpp", ".cxx", ".h", ".hh", ".hpp", ".hxx", ".py"}
)


def _source_like(path: Path) -> bool:
    return bool(
        {suffix.lower() for suffix in path.suffixes} & _SOURCE_SUFFIXES
    ) or path.name.lower().rstrip("~").endswith(tuple(_SOURCE_SUFFIXES))


def _configured_outputs(value: object) -> list[Path]:
    if not isinstance(value, dict):
        return []
    return [
        path
        for key, item in value.items()
        for path in (
            [Path(item)]
            if key in {"output_dir", "report_dir"} and isinstance(item, str)
            else _configured_outputs(item)
        )
    ]


def _booley_copy_roots(directory: Path) -> list[Path]:
    """Known disposable copies in one authoritative or checkout Booley Directory."""
    from booley.goals.paths import GoalIdError, goals_root, record_paths

    roots = [
        directory / name
        for name in (
            ".runtime",
            "runtime",
            "selftest",
            "worktrees",
            "flow-reports",
            "logs",
            ".interactive_logs",
        )
    ]
    roots.extend(directory.glob(".baseline-wt-*"))
    goals = goals_root(directory)
    for child in goals.iterdir() if goals.is_dir() else ():
        try:
            paths = record_paths(directory, child.name)
        except GoalIdError:
            continue  # history and locks are not per-Goal artifact trees
        roots.extend((paths.runtime_dir, paths.logs_dir))
    return roots


def owned_copy_roots(real_root: Path) -> tuple[Path, ...]:
    """Read artifact authorities without creating runtime state in the live tree."""
    from booley.core.config_paths import resolve_toml
    from booley.runtime.booley_dir import resolve_booley_dir, resolve_checkout_booley_dir

    roots = [
        Path(value)
        for key in ("BOOLEY_RUNTIME_DIR", "BOOLEY_LOGS_DIR")
        if (value := os.environ.get(key))
    ]
    directories: set[Path] = set()
    for resolver in (resolve_booley_dir, resolve_checkout_booley_dir):
        try:
            directories.add(resolver(real_root))
        except FileNotFoundError:
            continue
    for directory in directories:
        roots.extend(_booley_copy_roots(directory))
    for config in (resolve_toml(path) for path in (*directories, real_root)):
        if config.is_file():
            roots.extend(_configured_outputs(tomllib.loads(config.read_text(encoding="utf-8"))))
    return tuple(path if path.is_absolute() else real_root / path for path in roots)


def _campaign_roots(root: Path) -> list[Path]:
    roots: list[Path] = []
    for manifest in root.rglob("manifest.json"):
        if manifest.is_symlink() or not manifest.is_file():
            continue
        try:
            value = json.loads(manifest.read_text(encoding="utf-8"))
        except (ValueError, OSError):
            continue
        if (
            isinstance(value, dict)
            and value.get("schema_version") == 2
            and isinstance(value.get("artifacts"), dict)
            and value["artifacts"].get("specs") == "mutation-specs.md"
            and "scope_hashes" in value
            and "mutants" in value
        ):
            roots.append(manifest.parent)
    return roots


def _snapshot_removals(
    snapshot: WorkspaceSnapshot, own: set[str], opposite: set[str], excluded: list[Path]
) -> set[Path]:
    root = snapshot.snapshot_root
    identities = {
        (stat.st_dev, stat.st_ino)
        for name in opposite
        if (source := snapshot.real_root / name).is_file()
        for stat in [source.stat()]
    }
    paths = {root / name for name in opposite}
    paths.update(excluded)
    for path in root.rglob("*"):
        relative = path.relative_to(root).as_posix()
        if ".git" in path.relative_to(root).parts:
            continue
        live = snapshot.real_root / relative
        alias = False
        if live.is_file():
            stat = live.stat()
            alias = (stat.st_dev, stat.st_ino) in identities
        if alias and relative in own:
            raise ValueError(f"source alias conflicts with opposite role: {relative}")
        if alias:
            paths.add(path)
        elif path.is_symlink():
            # Directory aliases can reveal an opposite file even when their name
            # has no source suffix. Remove the alias without following it.
            paths.add(path)
        elif (
            path.is_file()
            and relative not in own
            and relative not in opposite
            and _source_like(path)
        ):
            paths.add(path)
    return paths


def _remove_snapshot_paths(root: Path, paths: set[Path], category: str) -> list[str]:
    hidden: list[str] = []
    for path in sorted(paths, key=lambda p: (-len(p.parts), str(p))):
        if path.is_symlink() or path.is_file():
            path.unlink()
            hidden.append(path.relative_to(root).as_posix())
        elif path.is_dir():
            hidden.extend(
                child.relative_to(root).as_posix()
                for child in path.rglob("*")
                if child.is_file() or child.is_symlink()
            )
            shutil.rmtree(path)
    for name in hidden:
        logger.info("Isolation: removed %s from %s snapshot", name, category)
    return hidden


def _category_membership(
    root: Path, category: str, category_files: dict[str, tuple[str, ...]], scope: tuple[str, ...]
) -> tuple[set[str], set[str]]:
    """Validate exact declarations and grant only explicit unregistered files."""
    if category not in ("rtl", "tb"):
        raise ValueError(f"unknown isolation category {category!r}")
    opposite = set(category_files["tb" if category == "rtl" else "rtl"])
    own = set(category_files[category])
    for path in (*own, *opposite, *scope):
        _validate_snapshot_path(root, root / path)
        if (root / path).is_dir():
            raise ValueError(f"declared source is a directory: {path}")
    for name in scope:
        if name in opposite:
            raise ValueError(f"explicit scope is opposite-category source: {name}")
        own.add(name)  # unregistered explicit scope is a narrow own-category grant
    return own, opposite


def hide_opposite_sources(
    snapshot: WorkspaceSnapshot,
    category: str,
    *,
    category_files: dict[str, tuple[str, ...]],
    cwd: Path | None = None,
    scope: tuple[str, ...] = (),
    excluded_roots: tuple[Path, ...] = (),
) -> list[str]:
    """Sanitize exact sources and owned copies before recording snapshot history."""
    root = snapshot.snapshot_root.resolve()
    real = snapshot.real_root.resolve()
    if root.is_relative_to(real) or real.is_relative_to(root):
        raise ValueError("disposable snapshot must be separate from the live worktree")
    own, opposite = _category_membership(root, category, category_files, scope)
    excluded = []
    for path in excluded_roots:
        resolved = (path if path.is_absolute() else real / path).resolve()
        if resolved.is_relative_to(real):
            excluded.append(root / resolved.relative_to(real))
    excluded.extend(_campaign_roots(root))
    paths = _snapshot_removals(snapshot, own, opposite, excluded)
    protected = [cwd or root, root / ".git", *(root / name for name in scope)]
    for path in paths:
        _validate_snapshot_path(root, path)
        if any(keep.is_relative_to(path) for keep in protected):
            raise ValueError(
                f"source removal conflicts with own-category scope or agent cwd: {path.relative_to(root)}"
            )
    for name in scope:
        if not (root / name).is_file():
            raise ValueError(f"review scope unavailable in snapshot: {name}")
    hidden = _remove_snapshot_paths(root, paths, category)
    present = [name for name in opposite if (root / name).exists()]
    if present:
        raise ValueError(f"opposite sources remain in snapshot: {present}")
    logger.info(
        "Isolation %s: opposite=%d absent=%d removed=%d",
        category,
        len(opposite),
        len(opposite - set(hidden)),
        len(hidden),
    )
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
