"""Structured input for the Mutation Flow."""

from dataclasses import dataclass

from booley.flows.request import FlowRequest


@dataclass(kw_only=True)
class MutationRequest(FlowRequest):
    """One mutation run.

    The argument parser rejects a ``count`` that is neither a positive integer
    nor ``auto`` (any case, normalized to ``"auto"``). Run validation still
    covers criterion-supplied counts, ``seed`` (non-negative) and
    ``min_detected`` (within ``count``), so a bad value yields the run's own
    report and exit code 2 on every surface.
    """

    scope: str | None = None
    count: int | str | None = None
    min_detected: int | None = None
    seed: int = 0
