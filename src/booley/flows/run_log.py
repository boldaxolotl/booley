"""Flow-neutral durable run-log API.

Flow packages use this module rather than importing one another. The concrete
implementation remains shared with simulation-result persistence so an active
run has one freshness protocol regardless of which Flow produced it.
"""

from booley.flows.sim.result import (
    RUN_LOG_HEADER_PREFIX,
    RUN_LOG_IN_PROGRESS_PREFIX,
    RUN_LOG_NAME,
    RUN_LOG_PENDING,
    RUN_LOG_PROGRESS_MAX_BYTES,
    _cap_log_bytes,
    begin_run_log,
    current_run_token,
    read_run_log_header,
    run_log_is_current,
    write_run_log,
    write_run_log_progress,
)

__all__ = [
    "RUN_LOG_HEADER_PREFIX",
    "RUN_LOG_IN_PROGRESS_PREFIX",
    "RUN_LOG_NAME",
    "RUN_LOG_PENDING",
    "RUN_LOG_PROGRESS_MAX_BYTES",
    "_cap_log_bytes",
    "begin_run_log",
    "current_run_token",
    "read_run_log_header",
    "run_log_is_current",
    "write_run_log",
    "write_run_log_progress",
]
