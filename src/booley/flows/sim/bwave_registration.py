"""Register a traced simulation run's waveforms as bwave sessions (issue #2578).

An agent that ran ``booley flow sim --trace`` should be able to query the
waveform with ``bwave @<target> ...`` (or without any path) instead of hunting
for the trace file. Registration runs in the process that owns the Flow
result, after acceptance, and never changes the run's verdict or exit code.

The bwave session store belongs to the run's checkout, so ``@<target>`` and
the default session are that checkout's latest traced run.
"""

from __future__ import annotations

import logging
from collections.abc import Sequence
from pathlib import Path

from booley.bwave.sessions import register_trace, session_store
from booley.flows.sim.campaign import CampaignOutcome
from booley.flows.sim.campaign.test_run_report import (
    campaign_test_run_files_by_row,
    campaign_test_run_rows,
)

logger = logging.getLogger(__name__)

# The grade of a Test Run that passed; any other grade is worth looking at first.
_PASS_GRADE = "pass"


def session_traces(outcomes: Sequence[CampaignOutcome]) -> list[tuple[str, Path]]:
    """``(Target name, waveform)`` for each Target with a traced Test Run, in run order.

    A Target's waveform is its last non-passing traced Test Run's, else its
    last traced Test Run's. Targets without a trace yield nothing.
    """
    chosen: list[tuple[str, Path]] = []
    for outcome in outcomes:
        rows = campaign_test_run_rows(outcome)
        traces = [
            (row["grade"], files.trace)
            for row, files in zip(rows, campaign_test_run_files_by_row(outcome), strict=True)
            if files.trace is not None
        ]
        failing = [trace for grade, trace in traces if grade != _PASS_GRADE]
        if traces:
            chosen.append((str(outcome.target["name"]), (failing or [t for _, t in traces])[-1]))
    return chosen


def register_trace_sessions(outcomes: Sequence[CampaignOutcome], project_root: Path) -> None:
    """Register each traced Target's waveform as session ``<target>`` of *project_root*.

    The store is the run's own checkout's, so ``@<target>`` and the default
    session belong to that checkout's latest traced run (issue #3483).
    ``register_trace`` makes every registration the default session, so the
    last Target in run order (the newest trace) ends as the default. An
    untraced run never resolves or opens the store. Any failure is logged and
    swallowed: the store is external JSON, and a session is a convenience that
    must not turn a finished simulation into an error.
    """
    try:
        traces = session_traces(outcomes)
        if not traces:
            return
        store = session_store(project_root)
        for name, trace in traces:
            register_trace(trace, alias=name, store=store)
    except Exception:
        logger.warning("bwave: could not register this run's waveforms as sessions", exc_info=True)
