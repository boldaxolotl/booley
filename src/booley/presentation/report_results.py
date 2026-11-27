"""Pure, bounded human projections of Flow reports.

The web Dashboard's result cards and the MCP Job notice read the same presentation.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass

from booley.core.boundary import as_float, as_int
from booley.core.exit_codes import (
    EXIT_CANCELLED,
    EXIT_ERROR,
    EXIT_FAILURE,
    EXIT_SUCCESS,
)

_ROW_LIMIT = 24
_ITEM_LIMIT = 5
# Headline for a Simulation report an older Booley wrote; matches the Flow's refusal in wording only.
_OLDER_SIMULATION_REPORT = "written by an older Booley; no longer supported"
# Endpoints whose reports are Flow reports; any other endpoint is an MCP tool.
_FLOW_ENDPOINTS = frozenset({"sim", "lint", "synth", "fpga", "mutation"})


@dataclass(frozen=True)
class ResultLine:
    """A literal label/value pair with a UI-independent semantic style key."""

    label: str
    value: str
    semantic: str = ""


@dataclass(frozen=True)
class ReportPresentation:
    """The execution result, alone and with the report's headline."""

    flow_result: ResultLine
    flow_word: ResultLine


def _mapping(value: object) -> Mapping[str, object]:
    return value if isinstance(value, Mapping) else {}


def _sequence(value: object) -> Sequence[object]:
    return value if isinstance(value, (list, tuple)) else ()


def literal(value: object, *, limit: int = 180) -> str:
    """Accept only finite scalar values; terminal controls never reach the UI."""
    if isinstance(value, str):
        if any(
            (ord(char) < 32 and char not in "\n\r\t") or 127 <= ord(char) < 160 for char in value
        ):
            return ""
        value = value.replace("\r\n", " ").replace("\n", " ").replace("\r", " ").replace("\t", " ")
        return value[:limit] + ("…" if len(value) > limit else "")
    if isinstance(value, bool):
        return "yes" if value else "no"
    if as_float(value) is not None:
        return str(value)
    return ""


def _value(value: object) -> str:
    return literal(value) or "unavailable"


def _row(label: str, value: object, semantic: str = "") -> ResultLine:
    return ResultLine(label, _value(value), semantic)


def _targets(value: object) -> list[tuple[str, Mapping[str, object]]]:
    if isinstance(value, Mapping):
        return [(literal(key), _mapping(row)) for key, row in value.items()]
    return [
        (
            literal(row) if isinstance(row, str) else _value(_mapping(row).get("target")),
            _mapping(row),
        )
        for row in _sequence(value)
    ]


def _cap(rows: list[ResultLine]) -> tuple[ResultLine, ...]:
    if len(rows) <= _ROW_LIMIT:
        return tuple(rows)
    kept = rows[: _ROW_LIMIT - 1]
    return (*kept, _row("More", f"{len(rows) - len(kept)} facts in report"))


def _functional(row: Mapping[str, object]) -> Mapping[str, object]:
    return _mapping(_mapping(row.get("observation_counts")).get("functional"))


def _counts(counts: Mapping[str, object]) -> tuple[int, int] | None:
    values = [as_int(value) for value in counts.values()]
    if not counts or any(value is None or value < 0 for value in values):
        return None
    return as_int(counts.get("pass")) or 0, sum(value for value in values if value is not None)


def _ratio(counts: tuple[int, int] | None) -> str:
    return "test counts unavailable" if counts is None else f"{counts[0]}/{counts[1]} tests passed"


def _simulation_records(detail: Mapping[str, object]) -> str:
    targets = _targets(detail.get("records"))
    counts = [_counts(_functional(row)) for _, row in targets]
    known = [count for count in counts if count is not None]
    total = (
        (sum(count[0] for count in known), sum(count[1] for count in known))
        if len(known) == len(targets) and targets
        else None
    )
    grades = [literal(row.get("grade")) for _, row in targets]
    severity = {"pass": 0, "warn": 1, "inconclusive": 2, "fail": 3, "error": 4}
    grade = (
        max(grades, key=lambda value: severity.get(value, -1))
        if grades and all(value in severity for value in grades)
        else "unavailable"
    )
    return _ratio(total) + f" · grade {grade}"


def _coverage(detail: Mapping[str, object]) -> str:
    targets = _targets(detail.get("targets"))
    if not any(literal(row.get("evaluation")) for _, row in targets):
        return "Coverage: evaluation unavailable"
    met = sum(row.get("evaluation") == "pass" for _, row in targets)
    return f"Coverage: {met}/{len(targets)} Targets met"


def _simulation(detail: Mapping[str, object]) -> str:
    if not _mapping(detail.get("records")) and _mapping(detail.get("campaigns")):
        # Booley 0.4.0 wrote ``detail.campaigns``; say so rather than render it as a result.
        return _OLDER_SIMULATION_REPORT
    if detail.get("coverage") is True and _targets(detail.get("targets")):
        return _coverage(detail)
    if _mapping(detail.get("records")):
        return _simulation_records(detail)
    if literal(detail.get("mode")).replace("_", "-").startswith("elab-only"):
        targets = _targets(detail.get("targets"))
        passed = sum(row.get("passed") is True for _, row in targets)
        return f"{passed}/{len(targets)} Targets elaborated"
    return _legacy_simulation(detail)


def _legacy_simulation(detail: Mapping[str, object]) -> str:
    total, passed = as_int(detail.get("targets")), as_int(detail.get("targets_passed"))
    if total is None:
        return ""
    return f"{passed if passed is not None else 'unavailable'}/{total} Targets passed"


_METRICS = {
    "area_kge": "Area",
    "area_um2": "Area µm²",
    "cell_count": "Cells",
    # Synth reports written by v0.4.0 and earlier name the field `cells`.
    "cells": "Cells",
    "wns_ns": "WNS",
    "whs_ns": "Hold slack",
    "estimated_fmax_mhz": "Estimated Fmax",
    "fmax_mhz": "Fmax",
    "reg2reg_fmax_mhz": "Register Fmax",
    "latches": "Latches",
    "unexpected_latches": "Unexpected latches",
    "unexpected_comb_loops": "Unexpected combinational loops",
    "lut_count": "LUTs",
    "ff_count": "FFs",
    "bram_count": "BRAMs",
    "dsp_count": "DSPs",
    "comb_loops": "Combinational loops",
    "multi_driven": "Multiple drivers",
    "critical_path_ps": "Critical path",
    "warnings": "Warnings",
    "has_critical": "Critical",
}


def _implementation(endpoint: str, detail: Mapping[str, object]) -> str:
    targets = _targets(_mapping(detail.get("implementation")).get("results"))
    if not targets:
        return ""
    grades = [literal(_mapping(data.get("status")).get("grade")) for _, data in targets]
    summary = f"grade {grades[0]}"
    if len(targets) > 1:
        passed = sum(grade in {"pass", "warn"} for grade in grades)
        summary = (
            f"{passed}/{len(targets)} Targets passed"
            if all(grade in {"pass", "warn", "fail", "error"} for grade in grades)
            else "Target results unavailable"
        )
    return (
        f"{'Synthesis' if endpoint == 'synth' else 'FPGA'}: {len(targets)} Target{'s' if len(targets) != 1 else ''}"
        + (f" · {summary}" if summary else "")
    )


def _lint(detail: Mapping[str, object]) -> str:
    if "plan" in detail:  # preparation failed: no EDA tool ran
        return ""
    warnings = as_int(detail.get("total_warnings"))
    tools = _mapping(detail.get("eda_tools"))
    if warnings is None and not (
        tools
        or literal(detail.get("total_warnings"))
        or any(literal(target) for target in _sequence(detail.get("targets")))
    ):
        return ""
    headline = (
        f"Lint: {warnings} warnings" if warnings is not None else "Lint: warning count unavailable"
    )
    names = list(dict.fromkeys(literal(tool) for tool in tools.values() if literal(tool)))
    return headline + (" · " + ", ".join(names[:_ITEM_LIMIT]) if names else "")


def _review(detail: Mapping[str, object], mode: str) -> str:
    outcome = literal(detail.get("review_outcome")) if mode == "done" else ""
    issues = as_int(detail.get("issues"))
    counted = ("CRITICAL", "MAJOR", "MINOR", "observation_count")
    if not (
        outcome
        or issues is not None
        or any(literal(detail.get(key)) for key in counted)
        or isinstance(detail.get("gate_passed"), bool)
    ):
        return ""
    return (
        f"Review: {outcome}"
        if outcome
        else f"Review: {issues if issues is not None else 'unavailable'} pending"
    )


def _mutation(detail: Mapping[str, object]) -> str:
    if not (
        any(as_int(detail.get(key)) is not None for key in ("detected", "total_valid"))
        or any(literal(detail.get(key)) for key in ("not_detected", "min_detected"))
        or any(literal(item) for item in _sequence(detail.get("survivors")))
        or any(
            _mapping(item).get("status") == "not_detected"
            for item in _sequence(detail.get("classified"))
        )
    ):
        return ""
    headline = (
        f"Mutation: {_value(detail.get('detected'))}/{_value(detail.get('total_valid'))} detected"
    )
    minimum = literal(detail.get("min_detected"))
    return headline + (f" (min {minimum})" if minimum else "")


def _analysis(detail: Mapping[str, object]) -> str:
    outcome = literal(detail.get("closure_recommendation"))
    listed = ("recommendations", "waiver_candidates", "hypotheses")
    if not (
        outcome
        or literal(detail.get("eligibility"))
        or any(isinstance(detail.get(key), (list, tuple)) for key in listed)
    ):
        return ""
    return f"Coverage analysis: {outcome or 'unavailable'}"


def configuration(argv: object) -> tuple[ResultLine, ...]:
    """Expose only public selection options, never interpreter or internal flags."""
    args = _sequence(argv)
    labels = {"--target": "Target", "--test": "Test", "--tests": "Tests", "--mode": "Mode"}
    rows = []
    for index, arg in enumerate(args):
        flag, _, inline = arg.partition("=") if isinstance(arg, str) else ("", "", "")
        if flag in labels:
            value = inline or (args[index + 1] if index + 1 < len(args) else None)
            if literal(value):
                rows.append(_row(labels[flag], value))
    return tuple(rows)


def _mode(report: Mapping[str, object]) -> str:
    detail = _mapping(report.get("detail"))
    return (
        literal(detail.get("mode"))
        or literal(_mapping(detail.get("contract")).get("mode"))
        or next(
            (row.value for row in configuration(report.get("argv")) if row.label == "Mode"), ""
        )
    )


def _headline(report: Mapping[str, object]) -> str:
    endpoint = literal(report.get("flow")) or literal(report.get("mcp_tool"))
    detail = _mapping(report.get("detail"))
    if not detail:
        return ""
    mode = _mode(report)
    if endpoint == "sim":
        return _simulation(detail)
    if endpoint in {"synth", "fpga"}:
        return _implementation(str(endpoint), detail)
    if endpoint == "lint":
        return _lint(detail)
    if endpoint == "reviewer":
        return _review(detail, mode)
    renderer = {"mutation": _mutation, "coverage_analyst": _analysis}.get(str(endpoint))
    return renderer(detail) if renderer is not None else ""


def _warning(report: Mapping[str, object]) -> bool:
    detail = _mapping(report.get("detail"))
    endpoint = literal(report.get("flow"))
    if endpoint == "lint":
        return (as_int(detail.get("total_warnings")) or 0) > 0
    return (
        endpoint in {"synth", "fpga"}
        and _mapping(detail.get("implementation")).get("grade") == "warn"
    )


def _success_result(report: Mapping[str, object]) -> tuple[str, str]:
    endpoint = literal(report.get("mcp_tool"))
    detail = _mapping(report.get("detail"))
    key = (
        "closure_recommendation"
        if endpoint == "coverage_analyst"
        else "review_outcome"
        if endpoint == "reviewer" and _mode(report) == "done"
        else ""
    )
    outcome = literal(detail.get(key))
    if outcome:
        return outcome, "unknown"
    return ("PASSED with warnings", "warn") if _warning(report) else ("PASSED", "pass")


def present_job_report(
    report: Mapping[str, object], *, endpoint: str, exit_code: int | None, argv: Sequence[str]
) -> ReportPresentation:
    """Project a Job's report with the Job's own exit code, Flow or tool, and arguments.

    The record's facts override whatever the report carries. The Dashboard and the
    MCP notice both use it; each supplies the exit code it resolved for the Job.
    """
    stamped = dict(report)
    stamped["exit_code"] = exit_code
    stamped["argv"] = list(argv)
    stamped["flow" if endpoint in _FLOW_ENDPOINTS else "mcp_tool"] = endpoint
    return present_report(stamped)


def present_report(report: Mapping[str, object]) -> ReportPresentation:
    """Project producer-owned report fields without IO, UI objects, or inference."""
    headline = _headline(report)
    code = as_int(report.get("exit_code"))
    unavailable = ("unavailable", "unknown")
    word, semantic = (
        {
            EXIT_SUCCESS: ("PASSED", "pass"),
            EXIT_FAILURE: ("FAILED", "fail"),
            EXIT_ERROR: ("ERROR", "attention"),
            EXIT_CANCELLED: ("CANCELLED", "unknown"),
        }.get(code, unavailable)
        if code is not None
        else unavailable
    )
    if code == EXIT_SUCCESS:
        word, semantic = _success_result(report)
    flow_result = _row("Flow result", word + (f" · {headline}" if headline else ""), semantic)
    return ReportPresentation(flow_result, _row("Flow result", word, semantic))


_FACT_LABELS = {
    **_METRICS,
    "wire_count": "Wires",
    "period_ns": "Period",
    "reg2reg_slack_ns": "Register slack",
    "kind": "Kind",
    "state": "State",
    "before": "Before",
    "after": "After",
    "rationale": "Rationale",
    "review_retarget_effect": "Review retarget effect",
    "proposal_id": "Proposal ID",
    "candidate_id": "Candidate ID",
    "decision": "Decision",
    "reason": "Reason",
    "source": "Source",
    "quote": "Quote",
    "at": "At",
    "target_selector": "Target selector",
    "target_identity": "Target identity",
    "campaign_path": "Coverage Record path",
    "point_id": "Point",
    "justification": "Justification",
    "proof_reference": "Proof reference",
    "proposal_count": "Proposal count",
    "first_recorded_at": "First recorded at",
    "last_recorded_at": "Last recorded at",
    "anchor": "Anchor",
    "directory": "Directory",
    "root": "Policy root",
    "file": "File",
    "line": "Line",
    "scope": "Scope",
    "enabled": "enabled",
    "target": "Target",
    "metric": "Metric",
    "threshold": "Threshold",
    "observation": "Observation",
    "recorded_at": "Recorded at",
    "producer": "Producer",
    "sequence": "Sequence",
    "passed": "Result",
    "criterion_met": "Criterion met",
    "warnings": "warnings",
    "evaluation": "Evaluation",
    "simulation": "Simulation",
    "collection": "Collection",
    "status": "Status",
    "test": "Test",
    "name": "Name",
    "cycle_count": "Cycles",
    "grade": "Grade",
    "total": "Total",
    "detected": "Detected",
    "not_detected": "Not detected",
    "min_detected": "Minimum detected",
    "log_path": "Log path",
    "report_path": "Report path",
    "path": "Path",
    "result": "Result",
}
_CONTAINERS = {
    "observations",
    "detail",
    "waiver",
    "candidate",
    "policy",
    "decision",
    "targets",
    "records",
    "metrics",
    "conditions",
    "scope",
    "paths",
    "test_runs",
    "config",
    "evidence_refs",
    "per_clock",
}


def project_facts(data: Mapping[str, object]) -> tuple[ResultLine, ...]:
    """Bound known readable evidence/approval fields; omit authority envelopes."""
    rows: list[ResultLine] = []
    _fact_rows(data, rows, depth=0)
    return _cap(rows) or (_row("Facts", "Summary unavailable", "unknown"),)


def _fact_rows(data: Mapping[str, object], rows: list[ResultLine], *, depth: int) -> None:
    if depth > 3 or len(rows) >= _ROW_LIMIT:
        return
    extra = False
    authority = {
        "approval_root",
        "payload_digest",
        "session_key",
        "transaction_id",
        "peer_process",
        "invocation_id",
    }
    for key, value in data.items():
        if len(rows) >= _ROW_LIMIT:
            break
        if key in _FACT_LABELS and literal(value):
            rows.append(
                ResultLine(
                    _FACT_LABELS[key],
                    literal(value, limit=300 if key == "review_retarget_effect" else 180),
                )
            )
        elif key in _CONTAINERS:
            _container_facts(key, value, rows, depth=depth)
        elif key not in authority and key not in _FACT_LABELS:
            extra = True
    if extra and len(rows) < _ROW_LIMIT:
        rows.append(_row("Facts", "Extra data in report", "unknown"))


def _container_facts(key: str, value: object, rows: list[ResultLine], *, depth: int) -> None:
    if isinstance(value, Mapping):
        if key in {"targets", "records", "per_clock"}:
            for name, target in list(value.items())[:_ITEM_LIMIT]:
                rows.append(_row("Clock" if key in {"per_clock"} else "Target", name))
                _fact_rows(_mapping(target), rows, depth=depth + 1)
        else:
            _fact_rows(value, rows, depth=depth + 1)
        return
    for item in _sequence(value)[:_ITEM_LIMIT]:
        if isinstance(item, Mapping):
            _fact_rows(item, rows, depth=depth + 1)
        elif literal(item):
            rows.append(_row(_FACT_LABELS.get(key, key.capitalize()), item))


def proposal_summary(facts: Mapping[str, object]) -> str:
    """Single clipped row for a selectable proposal; full facts stay in detail."""
    return " · ".join(
        (
            _value(facts.get("kind")),
            f"{_value(facts.get('before'))} → {_value(facts.get('after'))}",
            _value(facts.get("state")),
        )
    )[:160]
