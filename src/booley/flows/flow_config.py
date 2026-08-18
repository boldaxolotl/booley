"""Project-config readers shared by deterministic Booley Flows.

The generic ``[flows.<name>]`` / ``.core`` / ``tests.toml`` reads that every
Flow leans on: the declared Target selection, declared test lists, Target
enumeration, and the cheap TB-top read. These lived in
the former project-native adapter module for historical reasons; when the
project-native adapters were dropped (ADR 0039) the generic readers moved
here and the adapter dispatch died with its module.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from booley.fusesoc import fusesoc_registry
from booley.targets.flow_names import DEFAULT_TARGET_KEY, config_section

_UNSET = object()


def resolve_flow_default_target(flow_name: str, work_dir: Path) -> str:
    """Read ``[flows.<flow_name>].default_target`` from project config.

    The project's declared Target selection for *flow_name* — a bare Target name,
    a ``vlnv#name`` qualifier, or a comma-separated list of either. This is the
    single source of truth for "which Targets are mine" (ADR 0030 dec 3): a Flow
    invoked with no ``--target`` falls back to this, and refuses when it too is
    empty rather than sweeping every core. Returns ``""`` when unset.
    """
    try:
        from booley.runtime.shared_infra import _load_rtl_config

        cfg = _load_rtl_config(work_dir)
    except Exception:  # noqa: BLE001 — best-effort config read; empty → caller refuses
        cfg = {}
    if not cfg:
        return ""
    flows = cfg.get("flows", {})
    if not isinstance(flows, dict):
        return ""
    return str(config_section(flows, flow_name).get(DEFAULT_TARGET_KEY, "")).strip()


def discover_target_names(work_dir: Path | None = None) -> list[str]:
    """Return selectable config names — the project's ``.core`` Target names."""
    try:
        return list(fusesoc_registry.available_targets(work_dir)) if work_dir else []
    except Exception:  # noqa: BLE001 — best-effort Target enumeration; degrades to an empty list
        return []


def tb_top_for_target(target: str, work_dir: Path | None = None, *, resolved: Any = _UNSET) -> str:
    """Return the testbench top for one Target — the sim Target's toplevel (.core).

    A sim Target's ``toplevel`` is its TB top (decision 4). Pass a
    once-resolved Target as *resolved* to reuse it. When *resolved* is ``None``
    (the cheap, no-subprocess path used by dry-run and schema discovery), the
    toplevel is read straight from the ``.core`` YAML via
    :func:`fusesoc_registry.core_target_toplevel` — no ``fusesoc run``. The
    legacy ``configs.toml`` ``tb_top`` source was removed.
    """
    if resolved is _UNSET:
        resolved = _maybe_resolve(target, work_dir)
    if resolved is not None:
        return str(resolved.toplevel)
    if work_dir is None:
        return ""
    try:
        ref = fusesoc_registry.resolve_ref(work_dir, target)
        return fusesoc_registry.core_target_toplevel(
            fusesoc_registry.read_core(ref.core_file),
            ref.name,
        )
    except Exception:  # noqa: BLE001 — best-effort .core toplevel read (unknown/ambiguous too); degrades to an empty TB top
        return ""


def _maybe_resolve(target: str, work_dir: Path | None) -> Any:
    """Resolve *target*'s ``.core`` Target, or ``None`` when it cannot be.

    ``None`` (no ``.core`` declares *target*, no ``work_dir`` to anchor
    discovery, or ``fusesoc`` unavailable) routes the caller to the cheap
    no-resolve read.
    """
    if work_dir is None:
        return None
    return fusesoc_registry.try_resolve_target(target, project_root=work_dir)


def _load_flow_config(flow_name: str, work_dir: Path) -> dict[str, Any]:
    try:
        from booley.runtime.shared_infra import _load_rtl_config

        cfg = _load_rtl_config(work_dir) or {}
    except Exception:  # noqa: BLE001 — best-effort config read; degrades to empty Flow config
        cfg = {}
    flows = cfg.get("flows", {})
    return config_section(flows, flow_name) if isinstance(flows, dict) else {}
