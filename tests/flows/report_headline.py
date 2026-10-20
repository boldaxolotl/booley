"""Pair a persisted report.json headline with the MCP projection of the same run."""

import json
from pathlib import Path

from booley.flows.endpoint_report_criteria import freeze
from booley.mcp.job_results import _latest_report
from booley.mcp.report_encoding import ReportEncoder

HEADLINE = ("criterion_key", "criterion_met")


def assert_paired_headline(
    report_dir: Path, endpoint: str, expected: tuple[str | None, bool | None] | None
) -> dict:
    """Assert both public surfaces carry *expected*; ``None`` means both omit the fields.

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
    want = {} if expected is None else dict(zip(HEADLINE, expected, strict=True))
    for report in (persisted, projected):
        assert {key: report[key] for key in HEADLINE if key in report} == want
    return persisted


def final_report_omits_headline(endpoint, result) -> dict:
    """Freeze and write the final report of an endpoint without an acceptance owner.

    Both the persisted report and the MCP projection must omit the headline fields.
    """
    freeze(endpoint)
    path = endpoint.write_report(result)
    # write_report returns <report_dir>/<endpoint>/<N>/report.json.
    return assert_paired_headline(path.parents[2], endpoint.name, None)
