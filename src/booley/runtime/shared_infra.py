"""Shared infrastructure for simulation, Yosys, and Booley Flow scripts.

Extracts common functions (file-list building, path checking, delay
parsing) so each Flow script doesn't duplicate them.
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
from booley.core.config_paths import main_repository_root, resolve_toml
from booley.runtime.booley_dir import resolve_booley_dir

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Project Root resolution (shared convention)
# ---------------------------------------------------------------------------


def resolve_project_root(fallback_dir: Path | None = None) -> Path:
    """Resolve env, checkout, fallback, then cwd without eager import selection."""
    from booley.runtime.project_discovery import discover_project_root

    root = discover_project_root(required=True)
    return root if root is not None else (fallback_dir or Path.cwd()).resolve()


# ============================================================================
# booley.toml — dynamic RTL file discovery
# ============================================================================

# Module-level cache for the CWD-resolved config. ONLY consulted when no
# project_root is threaded through — see _load_rtl_config(project_root).
# project_root-scoped reads bypass this cache so that one project's CWD-
# cached toml can't leak into another project's work_dir context.
_TOML_CACHE: dict | None = None


def _load_toml_from_root(root: Path) -> dict | None:
    """Load booley.toml under the checkout's Booley Directory (legacy fallback included).

    The directory is ``root``'s own ``[project].dir``, else ``root/.booley``.

    Returns the parsed dict, or None if no readable TOML is present. A file
    that exists but fails to parse/read is logged (not silently equated with
    "no file here") so a malformed config is visibly wrong rather than a
    quiet fallback to whatever the caller does when this returns None.
    Never touches the CWD-derived module cache — pure function over *root*.
    """
    from booley.core.booley_dir import own_booley_dir

    booley_dir = own_booley_dir(root)
    candidates = (booley_dir / "booley.toml", booley_dir / "pipeline.toml")
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
        main_root = main_repository_root(project_root)
        if main_root is not None:
            return _load_toml_from_root(main_root)
        return None

    global _TOML_CACHE
    if _TOML_CACHE is not None:
        return _TOML_CACHE
    project_path = resolve_toml(resolve_booley_dir())
    toml_path = project_path if project_path.exists() else resolve_toml(resolve_project_root())
    if not toml_path.exists():
        return None
    with toml_path.open("rb") as f:
        _TOML_CACHE = tomllib.load(f)
    return _TOML_CACHE


def load_job_budget_config(
    project_root: Path | None = None, *, booley_dir: Path | None = None
) -> dict:
    """Load the Project configuration that owns the shared Job Slot budget.

    *booley_dir* reads that Booley Directory's ``booley.toml`` (what a Flow run
    with ``BOOLEY_DIR`` set to it reads first), for callers without that
    environment such as the detached baseline supervisor.
    """
    try:
        if booley_dir is not None:
            toml_path = resolve_toml(booley_dir)
            if not toml_path.exists():
                return {}
            with toml_path.open("rb") as handle:
                return tomllib.load(handle)
        return _load_rtl_config(project_root) or {}
    except (OSError, tomllib.TOMLDecodeError) as exc:
        logger.warning("Failed to load the shared Project job budget: %s", exc)
        return {}


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
    (the Mutation Flow, diff classification) call
    ``fusesoc_registry.source_dirs_from_core`` directly and get the hard
    ADR 0039 no-``.core`` error instead of a guessed partition.
    """
    root = project_root or resolve_project_root()
    try:
        from booley.fusesoc.fusesoc_registry import source_dirs_from_core

        return source_dirs_from_core(root)
    except Exception:  # noqa: BLE001 — no project context; legacy defaults
        return (["rtl"], ["tb"], [])


def get_rtl_dir(project_root: Path | None = None) -> Path:
    """First RTL source entry from the ``.core`` RTL filesets (fallback: 'rtl').

    A flat-repo (ADR 0026) entry is a file (e.g. ``picorv32.v``); a structured
    repo yields its directory. When *project_root* is given the ``.core`` under
    that root is read, so worktrees can't shadow each other.
    """
    root = project_root or resolve_project_root()
    rtl_dirs, _tb, _incl = _core_source_dirs(project_root)
    return root / rtl_dirs[0] if rtl_dirs else root / "rtl"


def get_rtl_source_dirs(project_root: Path | None = None) -> list[str]:
    """RTL source entries from the ``.core`` RTL filesets (fallback: ['rtl'])."""
    return _core_source_dirs(project_root)[0]


def get_tb_source_dirs(project_root: Path | None = None) -> list[str]:
    """TB source entries from the ``.core`` tb-tagged filesets (fallback: ['tb'])."""
    return _core_source_dirs(project_root)[1]


def get_syn_output_dir(project_root: Path | None = None) -> Path:
    """Synthesis output directory from [flows.synth].output_dir (fallback: 'util/syn')."""
    root = project_root or resolve_project_root()
    cfg = _load_rtl_config(project_root)
    if cfg:
        from booley.core.boundary import require_str_value
        from booley.targets.flow_names import config_section

        raw = config_section(cfg.get("flows", {}), "synth").get("output_dir", "util/syn")
        return root / require_str_value(raw, field="[flows.synth].output_dir", allow_empty=True)
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
    0026 flat repos) yield an exact-path prefix. Fallback: ('rtl/', 'rtl\\\\').
    """
    dirs = _core_source_dirs(project_root)[0]
    return source_dir_prefixes(dirs, project_root or resolve_project_root())


def get_tb_prefixes(project_root: Path | None = None) -> tuple[str, ...]:
    """TB prefixes for diff classification, from the ``.core`` tb-tagged filesets.

    Directory entries yield trailing '/' and '\\' variants; file entries (ADR
    0026 flat repos) yield an exact-path prefix. Fallback: ('tb/', 'tb\\\\').
    """
    return source_dir_prefixes(
        _core_source_dirs(project_root)[1], project_root or resolve_project_root()
    )


# ============================================================================
# check_paths — unified path verification
# ============================================================================


def check_paths(
    project_root: Path,
    key_dirs: dict[str, Path],
    *,
    extra_checks: dict | None = None,
) -> None:
    """Verify Project Root and key directories, print JSON report.

    Args:
        project_root: Repository root.
        key_dirs: ``{name: path}`` of directories to check.
        extra_checks: Optional extra entries to merge into the JSON output
                      (e.g. DPI library existence check).
    """
    from_env = "RTL_PROJECT_ROOT" in os.environ
    checks: dict = {}

    # 1. resolve_project_root() source
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
