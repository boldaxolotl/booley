"""Register a traced simulation run's waveforms as bwave sessions (issue #2578).

An agent that ran ``booley flow sim --trace`` should be able to query the
waveform with ``bwave @<target> ...`` (or without any path) instead of hunting
for the trace file. Registration runs in the process that owns the Flow
result, after acceptance, and never changes the run's verdict or exit code.

The bwave session store is one file per Sandbox temporary directory, so the
latest traced run in the Sandbox owns ``@<target>`` and the default session.
"""

from __future__ import annotations

import logging
from collections.abc import Sequence
from pathlib import Path

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


def register_trace_sessions(outcomes: Sequence[CampaignOutcome]) -> None:
    """Register each traced Target's waveform as bwave session ``<target>``.

    ``register_trace`` also makes every registration the default session, so
    the last Target in run order (the newest trace) ends as the default. An
    untraced run never opens the store. Any failure is logged and swallowed:
    the store is external JSON on a shared temp directory, and a session is a
    convenience that must not turn a finished simulation into an error.
    """
    # Imported here, not at module level: ``booley.bwave.sessions`` resolves its
    # session store from the temp directory at import, and importing the Flow
    # must not fix that location before the process has set up its own.
    from booley.bwave.sessions import register_trace

    try:
        for name, trace in session_traces(outcomes):
            register_trace(trace, alias=name)
    except Exception:
        logger.warning("bwave: could not register this run's waveforms as sessions", exc_info=True)
