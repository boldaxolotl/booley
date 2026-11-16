"""Pair a persisted report.json with the MCP projection of the same run."""

import json
from pathlib import Path

from booley.mcp.job_results import _latest_report
from booley.mcp.report_encoding import ReportEncoder

CRITERION_KEYS = ("criterion_key", "criterion_met")


def assert_paired_headline(report_dir: Path, endpoint: str) -> dict:
    """Assert both public surfaces omit the Criterion keys and agree on the run.

    The persisted side is the newest numbered ``<endpoint>/<N>/report.json``. The MCP
    side is the report ``_latest_report`` selects from disk, passed through the encoder.
    """
    numbered = report_dir.glob(f"{endpoint}/*/report.json")
    persisted = json.loads(max(numbered, key=lambda path: int(path.parent.name)).read_text())
    payload = ReportEncoder.structured_report(_latest_report(endpoint, report_dirs=(report_dir,)))
    assert payload is not None and not payload.get("truncated")
    (projected,) = payload["reports"]
    for same_run in ("run_id", "timestamp"):
        assert projected.get(same_run) == persisted.get(same_run)
    for report in (persisted, projected):
        found = sorted(set(CRITERION_KEYS) & set(report))
        assert not found, f"the report carries {found}"
    return persisted


def final_report_omits_headline(endpoint, result) -> dict:
    """Write the final report of an endpoint and check both surfaces omit the Criterion keys."""
    path = endpoint.write_report(result)
    # write_report returns <report_dir>/<endpoint>/<N>/report.json.
    return assert_paired_headline(path.parents[2], endpoint.name)
