"""Structured input for the Mutation Flow."""

from dataclasses import dataclass

from booley.flows.request import FlowRequest


@dataclass(kw_only=True)
class MutationRequest(FlowRequest):
    """One mutation run.

    Value rules (positive ``count``, non-negative ``seed``, ``min_detected``
    within ``count``) stay in the Flow's run validation, so a bad value yields
    the run's own report and exit code 2 on every surface.
    """

    scope: str | None = None
    count: int | str | None = None
    min_detected: int | None = None
    seed: int = 0
