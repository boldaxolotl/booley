"""Structured input for the fpga Flow."""

from dataclasses import dataclass

from booley.flows.request import FlowRequest


@dataclass(kw_only=True)
class FpgaRequest(FlowRequest):
    baseline: str | None = None
    no_cache: bool = False
    ppa_profile: str | None = None
