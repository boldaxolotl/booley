"""Source-directory resolution from the ``.core`` tags."""

from __future__ import annotations

from pathlib import Path

# ---------------------------------------------------------------------------
# Source-directory resolution — now sourced from the .core tags:[tb] partition
# (ADR 0026 follow-through), no longer from booley.toml [sources.*].
# ---------------------------------------------------------------------------


def read_source_dirs_from_toml(work_dir: Path) -> tuple[list[str], list[str]] | None:
    """RTL/TB source-dir entries for *work_dir*, derived from its ``.core`` filesets.

    Historically this read ``[sources.*].source_dirs`` from ``booley.toml``; the
    ``.core`` ``tags:[tb]`` partition is now the single source of truth, so this
    delegates to :func:`fusesoc_registry.source_dirs_from_core`. Returns
    ``(rtl_dirs, tb_dirs)`` — a file-vs-dir mix exactly as ``[sources.*]`` used to
    yield (a file directly at the Project Root appears verbatim) — or
    ``None`` when no ``.core`` is authored under *work_dir* (callers then fall
    back to their own defaults).
    """
    try:
        from booley.fusesoc.fusesoc_registry import discover_cores, source_dirs_from_core
    except Exception:  # noqa: BLE001 — registry unavailable; let callers use defaults
        return None
    if not discover_cores(work_dir):
        return None
    rtl_dirs, tb_dirs, _tb_include = source_dirs_from_core(work_dir)
    return rtl_dirs, tb_dirs
