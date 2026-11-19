"""Project-config readers shared by deterministic Booley Flows.

The generic ``[flows.<name>]`` / ``.core`` / ``tests.toml`` reads that every
Flow leans on: Flow policy, declared test lists, Target
enumeration, and the cheap TB-top read. These lived in
the former project-native adapter module for historical reasons; when the
project-native adapters were dropped (ADR 0039) the generic readers moved
here and the adapter dispatch died with its module.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from booley.targets.catalog import TargetCatalog
from booley.targets.flow_names import config_section


def tb_top_for_target(target: str, work_dir: Path | None = None) -> str:
    """Return the testbench top for one Target — the sim Target's toplevel (.core).

    A sim Target's ``toplevel`` is its TB top (decision 4). It is read
    straight from the ``.core`` YAML through the Target Catalog, with no
    ``fusesoc run``, which keeps dry-run and schema discovery cheap. The
    legacy ``configs.toml`` ``tb_top`` source was removed.
    """
    if work_dir is None:
        return ""
    try:
        return TargetCatalog.build(work_dir).select(target).declared_toplevel
    except (
        Exception  # noqa: BLE001 — best-effort .core read; degrades to an empty TB top
    ):
        return ""


def _load_flow_config(flow_name: str, work_dir: Path) -> dict[str, Any]:
    try:
        from booley.runtime.shared_infra import _load_rtl_config

        cfg = _load_rtl_config(work_dir) or {}
    except Exception:  # noqa: BLE001 — best-effort config read; degrades to empty Flow config
        cfg = {}
    flows = cfg.get("flows", {})
    return config_section(flows, flow_name) if isinstance(flows, dict) else {}


def expected_count(flow_name: str, work_dir: Path, key: str) -> int:
    """Read one declared structural allowance, ``[flows.<flow_name>].<key>``.

    One validation rule serves every allowance (``expected_latches`` and
    ``expected_comb_loops`` under ``synth`` and ``fpga``). A missing,
    negative, boolean, or non-integer value is treated as 0 rather than
    silently widening the gate.

    An inferred latch or combinational loop normally fails the run. A deliberate
    one (an integrated clock-gating cell built from an ``always_latch``, as in
    lowRISC's ``prim_clock_gating``) is declared here as a count. The allowance
    is not a mute: the raw count is always reported and one more than declared
    still fails. Multi-driven nets have no allowance.
    """
    raw = _load_flow_config(flow_name, work_dir).get(key, 0)
    if isinstance(raw, bool) or not isinstance(raw, int):
        return 0
    return max(0, raw)
