"""Git-diff RTL/TB classification helpers for MCP endpoints.

Principle 8 (Single Responsibility): these module-level free functions and
constants read project source-dir config (booley.toml) and classify changed
file paths into RTL vs TB categories. Extracted from ``base.py`` so the
``McpTool`` ABC keeps only its core run/report/state responsibility; ``base.py``
re-imports the entry points it uses. ``read_source_dirs_from_toml`` stays a
public API (no leading underscore) and remains importable from
``booley.dev_support.base`` via the re-import for backward compatibility.

Self-contained: depends on ``as_str_list``/category constants from
``development_state``, ``booley.shared_infra`` (imported lazily), and
``tomllib``. Never imports ``base`` — one-way dependency. The module-level
``_RTL_DIRS, _TB_DIRS`` cache resolves at import time via
``_load_classification_dirs()``, which is self-contained.
"""

from __future__ import annotations

from pathlib import Path

from booley.dev_support.development_state import (
    CATEGORY_RTL,
    CATEGORY_TB,
)

# ---------------------------------------------------------------------------
# Source-directory resolution — now sourced from the .core tags:[tb] partition
# (ADR 0026 follow-through), no longer from booley.toml [sources.*].
# ---------------------------------------------------------------------------


def read_source_dirs_from_toml(work_dir: Path) -> tuple[list[str], list[str]] | None:
    """RTL/TB source-dir entries for *work_dir*, derived from its ``.core`` filesets.

    Historically this read ``[sources.*].source_dirs`` from ``booley.toml``; the
    ``.core`` ``tags:[tb]`` partition is now the single source of truth, so this
    delegates to :func:`fusesoc_registry.source_dirs_from_core`. The name is kept
    because it is a public re-export from ``mcp_tools.base`` and several callers
    import it. Returns ``(rtl_dirs, tb_dirs)`` — a file-vs-dir mix exactly as
    ``[sources.*]`` used to yield (a flat-repo root file appears verbatim) — or
    ``None`` when no ``.core`` is authored under *work_dir* (callers then fall
    back to their own defaults).
    """
    try:
        from booley.fusesoc_registry import discover_cores, source_dirs_from_core
    except Exception:  # noqa: BLE001 — registry unavailable; let callers use defaults
        return None
    if not discover_cores(work_dir):
        return None
    rtl_dirs, tb_dirs, _tb_include = source_dirs_from_core(work_dir)
    return rtl_dirs, tb_dirs


# ---------------------------------------------------------------------------
# Directory prefixes for classifying git diffs into RTL vs TB categories.
# ---------------------------------------------------------------------------

_RTL_DIRS_DEFAULT = ("rtl/", "fw/")
_TB_DIRS_DEFAULT = ("tb/",)


def _load_classification_dirs(
    work_dir: Path | None = None,
) -> tuple[tuple[str, ...], tuple[str, ...]]:
    """Load RTL and TB directory prefixes from project config.

    Tries booley.toml in *work_dir* first. When *work_dir* is provided but
    its config is absent, returns hardcoded defaults rather than leaking the
    CWD-cached shared_infra config (which may belong to another project).
    Only when *work_dir* is ``None`` is the CWD-resolved shared_infra path
    consulted — that is the legacy human-mode entry point.
    """
    if work_dir is not None:
        parsed = read_source_dirs_from_toml(work_dir)
        if parsed is None:
            return _RTL_DIRS_DEFAULT, _TB_DIRS_DEFAULT
    else:
        try:
            from booley.shared_infra import get_rtl_source_dirs, get_tb_source_dirs

            parsed = get_rtl_source_dirs(), get_tb_source_dirs()
        except Exception:  # noqa: BLE001 — legacy CWD path unavailable; fall back to default source dirs
            return _RTL_DIRS_DEFAULT, _TB_DIRS_DEFAULT
    rtl_names, tb_names = parsed
    # 'fw' is always an RTL prefix. Keep entry order stable, dedup, add fw once.
    rtl_list = list(rtl_names)
    if "fw" not in {n.rstrip("/\\") for n in rtl_list}:
        rtl_list.append("fw")
    # File-aware prefixes (ADR 0026): a source_dirs entry that is a file under
    # work_dir classifies by exact path, so flat single-file repos work. Legacy
    # CWD path (work_dir None) can't stat, so entries stay directory prefixes.
    from booley.shared_infra import source_dir_prefixes

    rtl_dirs = source_dir_prefixes(rtl_list, work_dir)
    tb_dirs = source_dir_prefixes(tb_names, work_dir)
    return rtl_dirs, tb_dirs


# Module-level fallback prefixes for pre-migration (no-``.core``) projects.
# Kept as patchable names — ``mcp_tools.base`` re-exports them for tests.
_RTL_DIRS, _TB_DIRS = _RTL_DIRS_DEFAULT, _TB_DIRS_DEFAULT


def _norm_diff_path(path: str) -> str:
    """Normalize a git-diff path to project-root-relative POSIX (drop ``./``)."""
    s = path.strip().replace("\\", "/")
    return s[2:] if s.startswith("./") else s


def _core_classified_sets(
    work_dir: Path | None,
) -> tuple[frozenset[str], frozenset[str]] | None:
    """Exact RTL/TB file sets from the ``.core`` ``tags:[tb]`` partition.

    ``None`` when no ``.core`` is authored (pre-migration project) — the caller
    then falls back to directory-prefix classification.
    """
    try:
        from booley.fusesoc_registry import classified_sources
    except Exception:  # noqa: BLE001 — registry unavailable; use prefix fallback
        return None
    root = work_dir
    if root is None:
        try:
            from booley.shared_infra import PROJECT_ROOT

            root = PROJECT_ROOT
        except Exception:  # noqa: BLE001 — no CWD root resolvable
            return None
    try:
        cs = classified_sources(root)
    except Exception:  # noqa: BLE001 — malformed .core; fall back to prefixes
        return None
    if not cs.rtl_source_files and not cs.tb_files:
        return None
    return frozenset(cs.rtl_source_files), frozenset(cs.tb_files)


def _classify_files(
    paths: list[str],
    work_dir: Path | None = None,
) -> set[str]:
    """Classify changed file paths into RTL/TB categories.

    The ``.core`` ``tags:[tb]`` partition is the source of truth: a changed path
    is TB iff it is a tb-tagged file, RTL iff it is any other declared source
    (include headers count as RTL). A path in neither set is *unclassified* — an
    unregistered source, which the reviewer surfaces separately (ADR 0026
    follow-through). Only a project with no ``.core`` at all falls back to
    directory-prefix matching against hardcoded defaults.
    """
    core = _core_classified_sets(work_dir)
    categories: set[str] = set()
    if core is not None:
        rtl_files, tb_files = core
        for p in paths:
            n = _norm_diff_path(p)
            if not n:
                continue
            if n in tb_files:
                categories.add(CATEGORY_TB)
            elif n in rtl_files:
                categories.add(CATEGORY_RTL)
        return categories
    # Fallback: no .core — classify by directory prefix (hardcoded defaults).
    rtl_dirs, tb_dirs = (
        _load_classification_dirs(work_dir) if work_dir is not None else (_RTL_DIRS, _TB_DIRS)
    )
    for p in paths:
        stripped = p.strip()
        if not stripped:
            continue
        if _matches_prefix(stripped, rtl_dirs):
            categories.add(CATEGORY_RTL)
        elif _matches_prefix(stripped, tb_dirs):
            categories.add(CATEGORY_TB)
    return categories


def _matches_prefix(path: str, prefixes: tuple[str, ...]) -> bool:
    """True when *path* falls under any classification prefix.

    A directory prefix (ends with '/' or '\\') matches by ``startswith``; a file
    prefix (ADR 0026 flat repos, no trailing separator) matches by *exact* path
    so ``cpu.v`` classifies ``cpu.v`` but not a sibling ``cpu.vh``.
    """
    for pre in prefixes:
        if pre.endswith(("/", "\\")):
            if path.startswith(pre):
                return True
        elif path == pre:
            return True
    return False


def _verification_fingerprint_categories(key: str) -> set[str]:
    """Return source categories that a passing verification criterion depends on."""
    if key.startswith(("sim_", "elab_")):
        return {CATEGORY_RTL, CATEGORY_TB}
    if key.startswith(("lint_", "synthesis_", "fpga_impl_", "elaborate_standalone")):
        return {CATEGORY_RTL}
    return set()
