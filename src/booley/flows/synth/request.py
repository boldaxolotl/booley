"""Structured input for the synth Flow."""

from dataclasses import dataclass

from booley.flows.request import FlowRequest


@dataclass(kw_only=True)
class SynthRequest(FlowRequest):
    baseline: str | None = None
    flatten: bool | None = None
    frontend: str | None = None
    ppa_profile: str | None = None
    abc_recipe: str | None = None
    abc_script: str | None = None
    generic_abc_before_mapping: bool | None = None
    abc_delay_ps: int | None = None
    utilization_pct: float | None = None
    placement_density: float | None = None
    repair_setup: bool | None = None
    repair_hold: bool | None = None
    gate_cloning: bool | None = None
    setup_margin_ns: float | None = None
    repair_tns_percent: float | None = None
