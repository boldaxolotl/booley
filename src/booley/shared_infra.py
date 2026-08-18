"""Shared infrastructure for simulation, Yosys, and Booley Flow scripts.

Extracts common functions (file-list building, work-dir derivation, path
checking, delay parsing) so each Flow script doesn't duplicate them.
"""

from __future__ import annotations

import json
import logging
import os
import posixpath
import sys
import tomllib
from collections.abc import Iterable
from pathlib import Path

# ---------------------------------------------------------------------------
# Predefined configurations (single source of truth)
# ---------------------------------------------------------------------------
from booley.core.config_paths import resolve_toml
from booley.project_dir import resolve_project_dir

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Project root resolution (shared convention)
# ---------------------------------------------------------------------------


def _has_git_worktree_marker(path: Path) -> bool:
    """Whether *path* has real Git metadata, not just an empty ``.git`` mount."""
    marker = path / ".git"
    try:
        if marker.is_dir():
            return (marker / "HEAD").is_file()
        return marker.is_file() and marker.read_text(
            encoding="utf-8", errors="replace"
        ).startswith("gitdir:")
    except OSError:
        return False


def resolve_project_root(fallback_dir: Path | None = None) -> Path:
    """Resolve project root from RTL_PROJECT_ROOT env var or cwd walk-up.

    Priority: env var → CWD walk-up → fallback → CWD.
    CWD walk-up runs before fallback so that local execution (debugger
    agents running sims from a worktree) resolves to the project root
    rather than to the Booley package directory encoded in SCRIPT_DIR.
    """
    if "RTL_PROJECT_ROOT" in os.environ:
        return Path(os.environ["RTL_PROJECT_ROOT"]).resolve()
    # Walk up from cwd to find a real worktree root. Merely finding a path named
    # .git is insufficient: sandbox launchers may mount an empty sentinel at a
    # broad ancestor such as /tmp/.git, which must not capture every project
    # beneath it.
    p = Path.cwd().resolve()
    while p != p.parent:
        if _has_git_worktree_marker(p) and p.name != ".booley":
            return p
        p = p.parent
    if fallback_dir is not None:
        return fallback_dir.resolve()
    return Path.cwd().resolve()


PROJECT_ROOT = resolve_project_root()

RTL_DIR = PROJECT_ROOT / "rtl"  # DEPRECATED: use get_rtl_dir() for configurable path


# ============================================================================
# booley.toml — dynamic RTL file discovery
# ============================================================================

# Module-level cache for the CWD-resolved config. ONLY consulted when no
# project_root is threaded through — see _load_rtl_config(project_root).
# project_root-scoped reads bypass this cache so that one project's CWD-
# cached toml can't leak into another project's work_dir context.
_TOML_CACHE: dict | None = None


def _resolve_main_repo_root(work_dir: Path) -> Path | None:
    """Follow a worktree's ``.git`` file to its main repository root.

    Mirrors ``dev_support.workspace_isolation._resolve_main_repo_root`` so that
    worktrees without their own ``.booley_project/`` can still find the
    project config in the main repo.
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
        for parent in gitdir.parents:
            if parent.name == ".git":
                return parent.parent
    except (OSError, ValueError):
        pass
    return None


def _load_toml_from_root(root: Path) -> dict | None:
    """Load booley.toml under ``root/.booley_project/`` (legacy fallback included).

    Returns the parsed dict, or None if no readable TOML is present. A file
    that exists but fails to parse/read is logged (not silently equated with
    "no file here") so a malformed config is visibly wrong rather than a
    quiet fallback to whatever the caller does when this returns None.
    Never touches the CWD-derived module cache — pure function over *root*.
    """
    candidates = (
        root / ".booley_project" / "booley.toml",
        root / ".booley_project" / "pipeline.toml",
    )
    for path in candidates:
        if not path.is_file():
            continue
        try:
            with path.open("rb") as f:
                return tomllib.load(f)
        except (OSError, tomllib.TOMLDecodeError) as e:
            logger.warning("Failed to read %s: %s", path, e)
            return None
    return None


def _load_rtl_config(project_root: Path | None = None) -> dict | None:
    """Load booley.toml. The single CWD-resolution path lives here.

    Threading rules:
      * ``project_root is None`` — fall back to the CWD-walk-up cache.
        Legacy callers (sim/yosys/lint runners) that genuinely run in
        the project CWD use this path.
      * ``project_root is not None`` — load fresh from that root (and
        the main repo if *project_root* is a worktree). Never reads or
        writes the module cache; never falls back to CWD. Returns
        ``None`` rather than leaking another project's config.
    """
    if project_root is not None:
        cfg = _load_toml_from_root(project_root)
        if cfg is not None:
            return cfg
        main_root = _resolve_main_repo_root(project_root)
        if main_root is not None:
            return _load_toml_from_root(main_root)
        return None

    global _TOML_CACHE
    if _TOML_CACHE is not None:
        return _TOML_CACHE
    project_path = resolve_toml(resolve_project_dir())
    root_path = resolve_toml(PROJECT_ROOT)
    toml_path = project_path if project_path.exists() else root_path
    if not toml_path.exists():
        return None
    with toml_path.open("rb") as f:
        _TOML_CACHE = tomllib.load(f)
    return _TOML_CACHE


# ============================================================================
# Configurable directory paths — read from booley.toml, with backward-compat
# defaults matching the previous hardcoded values.
# ============================================================================


def _core_source_dirs(
    project_root: Path | None = None,
) -> tuple[list[str], list[str], list[str]]:
    """``(rtl_dirs, tb_dirs, tb_include_dirs)`` from the project's ``.core`` filesets.

    Single delegation point for every ``[sources.*]``-replacing accessor below:
    the ``.core`` ``tags:[tb]`` partition is the source of truth (ADR 0026
    follow-through). Entries are a file-vs-dir mix exactly as ``[sources.*]``
    used to yield. Falls back to the legacy defaults outside a ``.core``
    project — these accessors feed module-level path constants that must
    stay importable anywhere; the Source-Isolation-critical consumers
    (mutation_tester, diff classification) call
    ``fusesoc_registry.source_dirs_from_core`` directly and get the hard
    ADR 0039 no-``.core`` error instead of a guessed partition.
    """
    root = project_root or PROJECT_ROOT
    try:
        from booley.fusesoc_registry import source_dirs_from_core

        return source_dirs_from_core(root)
    except Exception:  # noqa: BLE001 — no project context; legacy defaults
        return (["rtl", "fw"], ["tb"], [])


def get_rtl_dir(project_root: Path | None = None) -> Path:
    """First RTL source entry from the ``.core`` RTL filesets (fallback: 'rtl').

    A flat-repo (ADR 0026) entry is a file (e.g. ``picorv32.v``); a structured
    repo yields its directory. When *project_root* is given the ``.core`` under
    that root is read, so worktrees can't shadow each other.
    """
    root = project_root or PROJECT_ROOT
    rtl_dirs, _tb, _incl = _core_source_dirs(project_root)
    return root / rtl_dirs[0] if rtl_dirs else root / "rtl"


def get_rtl_source_dirs(project_root: Path | None = None) -> list[str]:
    """RTL source entries from the ``.core`` RTL filesets (fallback: ['rtl', 'fw'])."""
    return _core_source_dirs(project_root)[0]


def get_tb_source_dirs(project_root: Path | None = None) -> list[str]:
    """TB source entries from the ``.core`` tb-tagged filesets (fallback: ['tb'])."""
    return _core_source_dirs(project_root)[1]


def get_sim_output_dir(project_root: Path | None = None) -> Path:
    """Simulation output directory from [flows.sim].output_dir (fallback: 'util/sim')."""
    root = project_root or PROJECT_ROOT
    cfg = _load_rtl_config(project_root)
    if cfg:
        from booley.flow_names import config_section

        raw = config_section(cfg.get("flows", {}), "sim").get("output_dir", "util/sim")
        return root / raw
    return root / "util" / "sim"


def get_syn_output_dir(project_root: Path | None = None) -> Path:
    """Synthesis output directory from [flows.synth].output_dir (fallback: 'util/syn')."""
    root = project_root or PROJECT_ROOT
    cfg = _load_rtl_config(project_root)
    if cfg:
        from booley.flow_names import config_section

        raw = config_section(cfg.get("flows", {}), "synth").get("output_dir", "util/syn")
        return root / raw
    return root / "util" / "syn"


def source_dir_prefixes(
    names: list[str],
    base_dir: Path | None,
) -> tuple[str, ...]:
    """Diff-classification prefixes for ``[sources.*].source_dirs`` entries.

    Each entry may name a *directory* or a *file* (ADR 0026, flat single-file
    repos). An entry that resolves to a file under *base_dir* contributes its
    verbatim path as an exact-match prefix (no trailing separator) so a flat repo
    like ``picorv32.v`` at the root classifies without an ``rtl/``+``tb/``
    restructure. A directory entry — or any entry that cannot be stat'd because
    *base_dir* is ``None`` (legacy CWD path) — keeps the ``stem + '/'`` directory
    prefix. Both ``/`` and ``\\`` variants are emitted for cross-platform diff
    paths; the result is order-preserving and de-duplicated.

    File prefixes carry no trailing separator; callers that need to distinguish
    them from directory prefixes test ``prefix.endswith(('/', '\\\\'))``.
    """
    prefixes: list[str] = []
    for raw in names:
        stem = str(raw).strip().rstrip("/\\")
        if not stem:
            continue
        if base_dir is not None and (base_dir / stem).is_file():
            prefixes.append(stem)  # exact-path (file) prefix
            prefixes.append(stem.replace("/", "\\"))
        else:
            prefixes.append(f"{stem}/")
            prefixes.append(f"{stem}\\")
    return tuple(dict.fromkeys(prefixes))


def source_path_matches(path: str, prefixes: Iterable[str]) -> bool:
    """Return whether *path* belongs to file-aware source *prefixes*.

    Directory prefixes end in a separator and match descendants. A prefix
    without one names a root-level file in a flat repository and matches only
    that exact path. Lexical normalization accepts conventional ``./foo.v``
    while preventing ``rtl/../tb/foo.sv`` from borrowing the ``rtl/`` category.
    """
    normalized = posixpath.normpath(path.strip().replace("\\", "/"))
    for raw in prefixes:
        prefix = raw.replace("\\", "/")
        if prefix.endswith("/"):
            if normalized.startswith(prefix):
                return True
        elif normalized == prefix:
            return True
    return False


def get_rtl_prefixes(project_root: Path | None = None) -> tuple[str, ...]:
    """RTL prefixes for diff classification, from the ``.core`` RTL filesets.

    Directory entries yield trailing '/' and '\\' variants; file entries (ADR
    0026 flat repos) yield an exact-path prefix. ``'fw'`` is always included as
    an RTL prefix. Fallback: ('rtl/', 'rtl\\\\', 'fw/', 'fw\\\\').
    """
    dirs = list(_core_source_dirs(project_root)[0])
    if "fw" not in {d.rstrip("/\\") for d in dirs}:
        dirs.append("fw")
    return source_dir_prefixes(dirs, project_root or PROJECT_ROOT)


def get_tb_prefixes(project_root: Path | None = None) -> tuple[str, ...]:
    """TB prefixes for diff classification, from the ``.core`` tb-tagged filesets.

    Directory entries yield trailing '/' and '\\' variants; file entries (ADR
    0026 flat repos) yield an exact-path prefix. Fallback: ('tb/', 'tb\\\\').
    """
    return source_dir_prefixes(_core_source_dirs(project_root)[1], project_root or PROJECT_ROOT)


def get_tb_dirs() -> tuple[list[Path], list[Path]]:
    """TB source and include directories from the ``.core`` tb-tagged filesets.

    Returns (tb_source_dirs, tb_include_dirs) as resolved absolute paths, keeping
    only entries that resolve to real directories (a flat-repo file entry has no
    directory of its own, mirroring the old ``[sources.*]`` behaviour). Falls
    back to [PROJECT_ROOT / "tb"] when nothing resolves.
    """
    _rtl, tb_names, tb_incl = _core_source_dirs()

    source_dirs: list[Path] = []
    for d in tb_names:
        p = PROJECT_ROOT / d
        if p.is_dir():
            source_dirs.append(p.resolve())
    if not source_dirs:
        fallback = PROJECT_ROOT / "tb"
        if fallback.is_dir():
            source_dirs.append(fallback.resolve())

    include_dirs: list[Path] = []
    for d in tb_incl:
        p = PROJECT_ROOT / d
        if p.is_dir():
            include_dirs.append(p.resolve())

    return source_dirs, include_dirs


# ============================================================================
# derive_work_dir — unified work directory derivation
# ============================================================================


def derive_work_dir(
    project_root: Path,
    flow_name: str,
    config: str,
    top_module: str | None = None,
    *,
    defines: list[str] | None = None,  # DEPRECATED: ignored, remove in Phase 2
    test_id: int | None = None,
    test_name: str | None = None,
) -> Path:
    """Derive a unique, isolated work directory for a Flow run.

    Config name is the full label (e.g. "config_a_prot"), no suffix detection
    from defines.

    Args:
        project_root: Repository root (``PROJECT_ROOT``).
        flow_name: ``"sim"`` or ``"syn"`` — selects the output subtree.
        config: Config name (e.g. ``"config_c"``, ``"config_a_prot"``).
        top_module: Override top module (appended as suffix when not the default).
        defines: DEPRECATED — ignored. Kept for backward compat.
        test_id: DEPRECATED — use test_name. Kept for backward compat.
        test_name: Optional test name for per-test isolation (sim only).

    Returns:
        Absolute Path to the work directory (not yet created).
    """
    name = config

    # Append top module as suffix for work-dir isolation
    if top_module:
        name += f".{top_module}"

    # Append test name (sim only) — fall back to numeric index for compat
    if test_name is not None:
        name += f".{test_name}"
    elif test_id is not None:
        name += f".t{test_id}"

    if flow_name == "sim":
        return get_sim_output_dir(project_root) / "work" / name
    return get_syn_output_dir(project_root) / "syn_result" / name


# ============================================================================
# check_paths — unified path verification
# ============================================================================


def check_paths(
    project_root: Path,
    key_dirs: dict[str, Path],
    *,
    extra_checks: dict | None = None,
) -> None:
    """Verify project root and key directories, print JSON report.

    Args:
        project_root: Repository root.
        key_dirs: ``{name: path}`` of directories to check.
        extra_checks: Optional extra entries to merge into the JSON output
                      (e.g. DPI library existence check).
    """
    from_env = "RTL_PROJECT_ROOT" in os.environ
    checks: dict = {}

    # 1. PROJECT_ROOT source
    checks["project_root"] = {
        "path": str(project_root),
        "source": "env" if from_env else "script-relative",
        "exists": project_root.exists(),
    }

    # 2. Key directories
    dir_checks = {}
    for name, path in key_dirs.items():
        dir_checks[name] = {"path": str(path), "exists": path.exists()}
    checks["directories"] = dir_checks

    # 3. Extra checks (e.g. DPI library)
    if extra_checks:
        checks.update(extra_checks)

    # 4. Overall pass/fail
    all_ok = checks["project_root"]["exists"] and all(d["exists"] for d in dir_checks.values())
    checks["pass"] = all_ok

    print(json.dumps(checks, indent=2))
    if not all_ok:
        sys.exit(1)
