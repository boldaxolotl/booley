"""Synthesis adapter for baseline Jobs: the standalone ASIC synthesis Flow at the base.

The Job runs the ordinary synthesis Flow in the baseline checkout and reads the
canonical report the Flow publishes. Only a run that completed under the frozen
recipe is a result; everything else is unavailable and is never stored.
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from booley.goals.freshness import DEFAULT_RESOLVERS
from booley.goals.sim_baseline_store import SYNTH_BASELINE
from booley.targets.catalog import TargetCatalog
from booley.targets.domain import FuseSocError

#: The Flow's own timeout ends this long before the watchdog, so a slow
#: synthesis reports ``timeout`` in its report instead of dying without one.
FLOW_MARGIN_S = 120


def classify_synth(
    checkout: Path, _request: Mapping[str, Any], units: tuple[dict[str, Any], ...]
) -> tuple[str | None, list[dict[str, Any]], tuple[dict[str, Any], ...]]:
    """Resolve the baseline Target at the base; an unresolvable one is unavailable."""
    (unit,) = units
    try:
        identity = TargetCatalog.build(checkout).select(unit["name"]).identity
    except FuseSocError as exc:
        row = {**unit, "grade": "unavailable", "reason": f"baseline Target unavailable: {exc}"}
        return None, [row], ()
    return identity, [], units


def synth_job_class() -> str:
    """The synthesis Flow's own Job Class declaration."""
    from booley.flows.synth.flow import AsicSynthesizeFlow

    return AsicSynthesizeFlow()._resolve_job_class()


def _synthesize(
    checkout: Path, selector: str, report_dir: Path, timeout_ms: int
) -> dict[str, Any]:
    """The actual synthesis Flow seam: standalone execution, then its canonical report."""
    from booley.flows.execution_persistence import StandaloneFlowExecution
    from booley.flows.synth.flow import AsicSynthesizeFlow
    from booley.flows.synth.request import SynthRequest

    AsicSynthesizeFlow().execute(
        SynthRequest(
            target=selector, work_dir=checkout, report_dir=report_dir, timeout_ms=timeout_ms
        ),
        adapter=StandaloneFlowExecution(),
    )
    (report,) = report_dir.glob("synth_*.json")
    return json.loads(report.read_text(encoding="utf-8"))["implementation"]


def synth_row(unit: Mapping[str, Any], envelope: Mapping[str, Any]) -> dict[str, Any]:
    """Map a canonical envelope to a graded row, or to an unavailable one with its reason."""
    status, recipe = envelope["status"], envelope["recipe"]
    if status["infra_error"] or status["termination"] != "completed":
        reason = status["infra_error"] or f"synthesis ended with {status['termination']}"
    elif recipe["fingerprint"] != unit["fingerprint"]:
        reason = "baseline synthesis recipe differs from the recipe frozen at entry"
    else:
        measurement = {key: envelope[key] for key in ("status", "metrics", "recipe", "provenance")}
        grade = "pass" if status["passed"] else "fail"
        return {**unit, "grade": grade, "measurement": measurement}
    return {**unit, "grade": "unavailable", "reason": reason}


def measure_synth(request: Mapping[str, Any]) -> dict[str, Any]:
    """Run inside the watchdog child: synthesise the baseline Target once."""
    from booley.mcp.sim_baseline_execution import _remaining_seconds, contents_stamp

    checkout, output, target = (
        Path(request["checkout"]),
        Path(request["output"]),
        request["target"],
    )
    (unit,) = request["identity"]["runs"]
    budget_s = _remaining_seconds(output) - FLOW_MARGIN_S
    if budget_s <= 0:
        raise TimeoutError("baseline Job has no budget left for synthesis")

    def stamp() -> str:
        fingerprint = DEFAULT_RESOLVERS.fingerprint(checkout, target=target)
        return contents_stamp(fingerprint, SYNTH_BASELINE.freshness_key)

    before = stamp()
    envelope = _synthesize(checkout, unit["name"], output / "reports", int(budget_s * 1000))
    return {
        "schema": request["schema"],
        "rows": [synth_row(unit, envelope)],
        "stamps": [before, stamp()],
        "reason": "",
    }
