"""Pure, bounded human projections of Flow reports and approval evidence.

Execution success is independent of Criterion satisfaction. The same immutable
presentation can be consumed by detail panes and other observational surfaces.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass

from booley.core.boundary import as_float, as_int
from booley.runtime.endpoint_execution import (
    EXIT_CANCELLED,
    EXIT_ERROR,
    EXIT_FAILURE,
    EXIT_SUCCESS,
)

_ROW_LIMIT = 24
_ITEM_LIMIT = 5


@dataclass(frozen=True)
class ResultLine:
    """A literal label/value pair with a UI-independent semantic style key."""

    label: str
    value: str
    semantic: str = ""


@dataclass(frozen=True)
class ReportPresentation:
    """Independent execution and acceptance results plus compact report facts."""

    flow_result: ResultLine
    verdict: ResultLine
    criterion: ResultLine | None
    execution: ResultLine
    result_rows: tuple[ResultLine, ...]


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


def _fields(data: Mapping[str, object], labels: Mapping[str, str]) -> list[ResultLine]:
    return [_row(label, data[key]) for key, label in labels.items() if literal(data.get(key))]


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
    return (*rows[: _ROW_LIMIT - 1], _row("More", f"{len(rows) - _ROW_LIMIT + 1} facts in report"))


def _more_targets(targets: list[tuple[str, Mapping[str, object]]]) -> list[ResultLine]:
    return (
        [_row("Targets", f"{len(targets) - _ITEM_LIMIT} more Targets in report")]
        if len(targets) > _ITEM_LIMIT
        else []
    )


def _functional(row: Mapping[str, object]) -> Mapping[str, object]:
    return _mapping(_mapping(row.get("observation_counts")).get("functional"))


def _counts(counts: Mapping[str, object]) -> tuple[int, int] | None:
    values = [as_int(value) for value in counts.values()]
    if not counts or any(value is None or value < 0 for value in values):
        return None
    return as_int(counts.get("pass")) or 0, sum(value for value in values if value is not None)


def _ratio(counts: tuple[int, int] | None) -> str:
    return "test counts unavailable" if counts is None else f"{counts[0]}/{counts[1]} tests passed"


_BAD_EXECUTION = {
    "error",
    "timeout",
    "cancelled",
    "crash",
    "aborted",
    "setup_error",
    "blocked_by_build",
    "not_run",
}


def _campaign_failure_count(campaign: Mapping[str, object], preview: int) -> int:
    counts = _mapping(campaign.get("observation_counts"))
    # Axes overlap; the largest marginal count is a lower bound, never their sum.
    bad = {
        "functional": ("fail", "inconclusive", "not_observed"),
        "execution": _BAD_EXECUTION,
        "assertions": ("fail", "dirty"),
    }
    return max(
        preview,
        *(
            sum(as_int(_mapping(counts.get(axis)).get(key)) or 0 for key in keys)
            for axis, keys in bad.items()
        ),
    )


def _campaign_failures(campaign: Mapping[str, object]) -> list[ResultLine]:
    observations = [_mapping(item) for item in _sequence(campaign.get("observations"))]
    failures = [
        row
        for row in observations
        if row.get("functional") == "fail"
        or row.get("functional") == "inconclusive"
        or literal(row.get("execution")) in _BAD_EXECUTION
        or row.get("assertions") in {"fail", "dirty"}
    ]
    rows = []
    for row in failures[:_ITEM_LIMIT]:
        reason = literal(row.get("failure_kind") or row.get("failure_class"))
        rows.append(
            _row(
                "  Failed",
                f"{_value(row.get('test'))}" + (f" · {reason}" if reason else ""),
                "fail",
            )
        )
    total = _campaign_failure_count(campaign, len(failures))
    if total > len(rows):
        rows.append(_row("  Failures", f"at least {total - len(rows)} more failures in report"))
    if campaign.get("observations_truncated") or total > len(failures):
        rows.append(
            _row("  Observations", "truncated preview; full report has remaining observations")
        )
    cycles = [
        literal(row.get("cycle_count")) for row in observations if literal(row.get("cycle_count"))
    ]
    if cycles:
        rows.append(_row("  Cycles", ", ".join(cycles[:_ITEM_LIMIT])))
    return rows


def _campaigns(detail: Mapping[str, object]) -> tuple[str, list[ResultLine]]:
    targets = _targets(detail.get("campaigns"))
    counts = [_counts(_functional(row)) for _, row in targets]
    known = [count for count in counts if count is not None]
    total = (
        (sum(count[0] for count in known), sum(count[1] for count in known))
        if len(known) == len(targets) and targets
        else None
    )
    rows = []
    for name, row in targets[:_ITEM_LIMIT]:
        rows.append(
            _row(
                f"  {name}",
                _ratio(_counts(_functional(row))) + f" · grade {_value(row.get('grade'))}",
            )
        )
    rows.extend(_more_targets(targets))
    for name, row in targets[:_ITEM_LIMIT]:
        for fact in _campaign_failures(row):
            label = f"  {name} · {fact.label.strip()}" if len(targets) > 1 else fact.label
            rows.append(ResultLine(label, fact.value, fact.semantic))
    grades = [literal(row.get("grade")) for _, row in targets]
    severity = {"pass": 0, "warn": 1, "inconclusive": 2, "fail": 3, "error": 4}
    grade = (
        max(grades, key=lambda value: severity.get(value, -1))
        if grades and all(value in severity for value in grades)
        else "unavailable"
    )
    return _ratio(total) + f" · grade {grade}", rows


def _coverage(detail: Mapping[str, object]) -> tuple[str, list[ResultLine]]:
    targets = _targets(detail.get("targets"))
    if not any(literal(row.get("evaluation")) for _, row in targets):
        return "Coverage: evaluation unavailable", [
            *[
                _row(f"  {name}", "evaluation unavailable", "unknown")
                for name, _ in targets[:_ITEM_LIMIT]
            ],
            *_more_targets(targets),
        ]
    met = sum(row.get("evaluation") == "pass" for _, row in targets)
    rows = []
    for name, row in targets[:_ITEM_LIMIT]:
        facts = [
            f"{key} {_value(row.get(key))}" for key in ("simulation", "collection", "evaluation")
        ]
        semantic = (
            "pass"
            if row.get("evaluation") == "pass"
            else "fail"
            if row.get("evaluation") == "fail"
            else "unknown"
        )
        rows.append(_row(f"  {name}", " · ".join(facts), semantic))
    rows.extend(_more_targets(targets))
    if _mapping(detail.get("campaigns")):
        headline, supplementary = _campaigns(detail)
        rows.extend([_row("Campaign simulation", headline), *supplementary])
    return f"Coverage: {met}/{len(targets)} Targets met", rows


def _simulation(detail: Mapping[str, object]) -> tuple[str, list[ResultLine]]:
    if detail.get("coverage") is True and _targets(detail.get("targets")):
        return _coverage(detail)
    if _mapping(detail.get("campaigns")):
        return _campaigns(detail)
    if literal(detail.get("mode")).replace("_", "-").startswith("elab-only"):
        targets = _targets(detail.get("targets"))
        passed = sum(row.get("passed") is True for _, row in targets)
        rows = []
        for name, row in targets[:_ITEM_LIMIT]:
            verdict = "elaborated" if row.get("passed") is True else _value(row.get("verdict"))
            facts = _fields(row, {"failure_class": "Failure class", "reason": "Reason"})
            rows.append(
                _row(
                    f"  {name}",
                    " · ".join([verdict, *[f"{fact.label}: {fact.value}" for fact in facts]]),
                )
            )
        return f"{passed}/{len(targets)} Targets elaborated", rows + _more_targets(targets)
    return _legacy_simulation(detail)


def _legacy_simulation(detail: Mapping[str, object]) -> tuple[str, list[ResultLine]]:
    total, passed = as_int(detail.get("targets")), as_int(detail.get("targets_passed"))
    if total is not None:
        rows = _fields(detail, {"elab_failed": "Elaboration failed"})
        failures = [
            _mapping(item)
            for item in _sequence(detail.get("cycle_counts"))
            if literal(_mapping(item).get("verdict")).upper() not in {"PASS", "PASSED"}
        ]
        for row in failures[:_ITEM_LIMIT]:
            rows.append(
                _row(
                    "  Failed",
                    f"{_value(row.get('target'))} · {_value(row.get('test'))} · {_value(row.get('verdict'))}",
                    "fail",
                )
            )
        if len(failures) > _ITEM_LIMIT:
            rows.append(_row("Failures", f"{len(failures) - _ITEM_LIMIT} more in report"))
        return f"{passed if passed is not None else 'unavailable'}/{total} Targets passed", rows
    return "", []


_METRICS = {
    "area_kge": "Area",
    "area_um2": "Area µm²",
    "cells": "Cells",
    "wns_ns": "WNS",
    "whs_ns": "Hold slack",
    "estimated_fmax_mhz": "Estimated Fmax",
    "fmax_mhz": "Fmax",
    "reg2reg_fmax_mhz": "Register Fmax",
    "latches": "Latches",
    "unexpected_latches": "Unexpected latches",
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


def _implementation_target(name: str, data: Mapping[str, object]) -> list[ResultLine]:
    status, metrics = _mapping(data.get("status")), _mapping(data.get("metrics"))
    rows = [_row(f"  {name}", status.get("grade"))]
    rows.extend(_fields(metrics, _METRICS))
    rows.extend(_fields(_mapping(data.get("conditions")), _METRICS))
    clocks = _targets(metrics.get("per_clock"))
    for clock, timing in clocks[:_ITEM_LIMIT]:
        facts = _fields(timing, _METRICS)
        if facts:
            rows.append(
                _row(f"  Clock {clock}", " · ".join(f"{row.label}: {row.value}" for row in facts))
            )
    for key, label in (("failure_reasons", "  Failure reason"), ("warning_reasons", "  Warning")):
        rows.extend(
            _row(label, reason)
            for reason in _sequence(status.get(key))[:_ITEM_LIMIT]
            if literal(reason)
        )
    return rows


def _implementation(endpoint: str, detail: Mapping[str, object]) -> tuple[str, list[ResultLine]]:
    targets = _targets(_mapping(detail.get("implementation")).get("results"))
    if not targets:
        return "", []
    rows = [
        _row(f"  {name}", _mapping(data.get("status")).get("grade"))
        for name, data in targets[:_ITEM_LIMIT]
    ]
    rows.extend(_more_targets(targets))
    for name, data in targets[:_ITEM_LIMIT]:
        for row in _implementation_target(name, data)[1:]:
            label = f"  {name} · {row.label.strip()}" if len(targets) > 1 else row.label
            rows.append(ResultLine(label, row.value, row.semantic))
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
        + (f" · {summary}" if summary else ""),
        rows,
    )


def _lint(detail: Mapping[str, object]) -> tuple[str, list[ResultLine]]:
    warnings = as_int(detail.get("total_warnings"))
    headline = (
        f"Lint: {warnings} warnings" if warnings is not None else "Lint: warning count unavailable"
    )
    tools = _mapping(detail.get("eda_tools"))
    rows = [
        _row(f"  {literal(target)}", tool)
        for target, tool in list(tools.items())[:_ITEM_LIMIT]
        if literal(tool)
    ]
    rows.extend(_fields(detail, {"total_warnings": "Warnings"}))
    if not tools:
        rows.extend(
            _row("  Target", target)
            for target in _sequence(detail.get("targets"))[:_ITEM_LIMIT]
            if literal(target)
        )
    names = list(dict.fromkeys(literal(tool) for tool in tools.values() if literal(tool)))
    if warnings is None and not rows:
        return "", []
    return headline + (" · " + ", ".join(names[:_ITEM_LIMIT]) if names else ""), rows


def _review(detail: Mapping[str, object], mode: str) -> tuple[str, list[ResultLine]]:
    outcome = literal(detail.get("review_outcome")) if mode == "done" else ""
    issues = as_int(detail.get("issues"))
    headline = (
        f"Review: {outcome}"
        if outcome
        else f"Review: {issues if issues is not None else 'unavailable'} pending"
    )
    rows = _fields(
        detail,
        {
            "CRITICAL": "Critical",
            "MAJOR": "Major",
            "MINOR": "Minor",
            "observation_count": "Observations",
        },
    )
    if isinstance(detail.get("gate_passed"), bool):
        rows.append(_row("Gate", "passed" if detail["gate_passed"] else "failed"))
    return (headline, rows) if outcome or issues is not None or rows else ("", [])


def _mutation(detail: Mapping[str, object]) -> tuple[str, list[ResultLine]]:
    headline = (
        f"Mutation: {_value(detail.get('detected'))}/{_value(detail.get('total_valid'))} detected"
    )
    rows = _fields(
        detail,
        {"not_detected": "Not detected", "invalid": "Invalid", "min_detected": "Minimum detected"},
    )
    survivors = _sequence(detail.get("survivors"))
    classified = [_mapping(item) for item in _sequence(detail.get("classified"))]
    for item in [row for row in classified if row.get("status") == "not_detected"][:_ITEM_LIMIT]:
        rows.append(
            _row(
                "Survivor",
                f"{_value(item.get('category'))} · {_value(item.get('file'))}:{_value(item.get('line'))}",
            )
        )
    rows.extend(_row("Survivor", item) for item in survivors[:_ITEM_LIMIT] if literal(item))
    if len(survivors) > _ITEM_LIMIT:
        rows.append(_row("Survivors", f"{len(survivors) - _ITEM_LIMIT} more in report"))
    if not rows and all(as_int(detail.get(key)) is None for key in ("detected", "total_valid")):
        return "", []
    minimum = literal(detail.get("min_detected"))
    return headline + (f" (min {minimum})" if minimum else ""), rows


def _analysis(detail: Mapping[str, object]) -> tuple[str, list[ResultLine]]:
    rows = _fields(detail, {"eligibility": "Eligibility"})
    for key, label in (
        ("recommendations", "Recommendations"),
        ("waiver_candidates", "Waiver candidates"),
        ("hypotheses", "Hypotheses"),
    ):
        if isinstance(detail.get(key), (list, tuple)):
            rows.append(_row(label, len(_sequence(detail[key]))))
    outcome = literal(detail.get("closure_recommendation"))
    return (
        (f"Coverage analysis: {outcome or 'unavailable'}", rows) if outcome or rows else ("", [])
    )


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


def _report_rows(report: Mapping[str, object]) -> tuple[str, list[ResultLine]]:
    endpoint = literal(report.get("flow")) or literal(report.get("mcp_tool"))
    detail = _mapping(report.get("detail"))
    if not detail:
        return "", []
    mode = _mode(report)
    if endpoint == "sim":
        return _simulation(detail)
    if endpoint in {"synth", "fpga"}:
        return _implementation(str(endpoint), detail)
    if endpoint == "lint":
        return _lint(detail)
    if endpoint == "reviewer":
        return _review(detail, mode)
    specialist = {"mutation_tester": _mutation, "coverage_analyst": _analysis}.get(str(endpoint))
    return specialist(detail) if specialist is not None else ("", [])


def _fallback(report: Mapping[str, object]) -> list[ResultLine]:
    text = report.get("report_text")
    lines = []
    if isinstance(text, str):
        for line in text.splitlines()[:20]:
            clean = literal(line, limit=120)
            if clean and not any(token in clean for token in ("{'", '{"', "[{'", "json.dumps")):
                lines.append(_row("Report", clean))
            if len(lines) == 3:
                break
    return lines or [_row("Result", "Summary unavailable", "unknown")]


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


def present_report(report: Mapping[str, object]) -> ReportPresentation:
    """Project producer-owned report fields without IO, UI objects, or inference."""
    headline, rows = _report_rows(report)
    code = as_int(report.get("exit_code"))
    word, semantic = {
        EXIT_SUCCESS: ("PASSED", "pass"),
        EXIT_FAILURE: ("FAILED", "fail"),
        EXIT_ERROR: ("ERROR", "attention"),
        EXIT_CANCELLED: ("CANCELLED", "unknown"),
    }.get(code, ("unavailable", "unknown"))
    if code == EXIT_SUCCESS:
        word, semantic = _success_result(report)
    raw_key = report.get("criterion_key")
    key = literal(raw_key).strip() if isinstance(raw_key, str) else ""
    met = report.get("criterion_met")
    criterion = (
        _row("Criterion", f"{key} · {'met' if met else 'not met'}", "met" if met else "fail")
        if key and isinstance(met, bool)
        else None
    )
    rows.extend(_fields(report, {**_METRICS, "failure_reason": "Failure reason"}))
    if not headline and not rows:
        rows = _fallback(report)
    flow_result = _row("Flow result", word + (f" · {headline}" if headline else ""), semantic)
    return ReportPresentation(
        flow_result,
        _row("Flow result", word, semantic),
        criterion,
        _row("Execution", f"exit {code if code is not None else 'unavailable'}"),
        _cap(rows),
    )


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
    "campaign_path": "Campaign path",
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
    "campaigns",
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
        if key in {"targets", "campaigns", "per_clock"}:
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
