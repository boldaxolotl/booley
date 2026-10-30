"""SDK-independent report encoding and the MCP output budget policy.

The encoder preserves the historical character-based text tails and JSON byte
budget. Oversized structured reports retain verdicts and recovery pointers,
then shed optional detail deterministically. Input reports are never modified.
"""

from __future__ import annotations

import copy
import json
import logging
import os
from typing import Any

from booley.flows.endpoint_reporting import _contains_whole_line_block

logger = logging.getLogger(__name__)
_DEFAULT_MAX_STDOUT_BYTES = 12_000
_DEFAULT_MAX_STDERR_BYTES = 4_000
_MAX_STRUCTURED_REPORT_BYTES = 64 * 1024


def _env_positive_int(name: str, default: int) -> int:
    """Read a positive-integer env var; invalid or non-positive falls back.

    Same tolerant style as ``_env_timeout_seconds`` (warn + default on garbage),
    except non-positive values also fall back: a zero/negative byte cap would
    render every MCP tool result empty, which is never what an operator wants.
    """
    raw = os.environ.get(name)
    if raw is None or raw == "":
        return default
    try:
        value = int(raw)
    except ValueError:
        logger.warning("Ignoring invalid %s=%r", name, raw)
        return default
    if value <= 0:
        logger.warning("Ignoring non-positive %s=%r", name, raw)
        return default
    return value


def _max_stdout_bytes() -> int:
    """Byte cap for the stdout section of an MCP tool result."""
    return _env_positive_int("BOOLEY_MCP_MAX_STDOUT_BYTES", _DEFAULT_MAX_STDOUT_BYTES)


def _max_stderr_bytes() -> int:
    """Byte cap for the stderr section of an MCP tool result."""
    return _env_positive_int("BOOLEY_MCP_MAX_STDERR_BYTES", _DEFAULT_MAX_STDERR_BYTES)


def _implementation_from_report(report: dict[str, Any]) -> dict[str, Any] | None:
    """Return the canonical implementation envelope from a target or aggregate."""
    direct = report.get("implementation")
    if isinstance(direct, dict):
        return direct
    detail = report.get("detail")
    if isinstance(detail, dict) and isinstance(detail.get("implementation"), dict):
        return detail["implementation"]
    return None


def _implementation_artifacts(implementation: dict[str, Any]) -> dict[str, Any]:
    """Collect canonical target artifacts from a target or aggregate envelope."""
    artifacts = implementation.get("artifacts")
    if isinstance(artifacts, dict) and artifacts:
        return artifacts
    found: dict[str, Any] = {}
    results = implementation.get("results")
    if not isinstance(results, dict):
        return found
    for target, result in results.items():
        nested = result.get("artifacts") if isinstance(result, dict) else None
        if isinstance(nested, dict) and nested:
            found[str(target)] = nested
    return found


def _report_artifacts(report: dict[str, Any]) -> dict[str, Any]:
    """Pull the artifact pointers out of a run report, wherever an endpoint put them.

    Endpoints emit ``artifacts`` at the top level of their per-target report and
    inside ``detail`` (the copy that rides in the flat report this function
    usually sees). Both are checked; the top-level one wins on a key clash
    because it is the more specific of the two.

    ``detail`` is also scanned ONE level deep, because a multi-target Flow
    keys its detail by target and hangs the block off each entry
    (``detail["sim_fast"]["artifacts"]`` — asic_synthesize's aggregate shape).
    Without this the rescue silently found nothing for exactly the Flows whose
    reports grow large enough to be truncated.

    Values are paths, or (for a multi-target Flow such as ``simulate``) a
    per-target mapping of them — either way a few hundred bytes, which is the
    only property the truncation fallback depends on. Returns ``{}`` rather
    than raising on a malformed report: this runs on an already-oversized
    payload, where an exception would cost the agent the text card too.
    """
    found: dict[str, Any] = {}
    implementation = _implementation_from_report(report)
    if implementation is not None:
        found.update(_implementation_artifacts(implementation))
    detail = report.get("detail")
    if isinstance(detail, dict):
        for key, value in detail.items():
            # One level down: a per-target entry carrying its own block.
            if key != "artifacts" and isinstance(value, dict):
                nested = value.get("artifacts")
                if isinstance(nested, dict) and nested:
                    found[key] = nested
        if isinstance(detail.get("artifacts"), dict):
            found.update(detail["artifacts"])
    if isinstance(report.get("artifacts"), dict):
        found.update(report["artifacts"])
    return found


def _compact_target_implementation(value: dict[str, Any]) -> dict[str, Any]:
    """Keep target verdict, QoR, comparison, cache, provenance, and recovery paths."""
    compact = {
        key: copy.deepcopy(value[key])
        for key in (
            "schema_version",
            "identity",
            "status",
            "metrics",
            "conditions",
            "recipe",
            "provenance",
            "comparison",
            "cache",
            "artifacts",
        )
        if key in value
    }
    status = compact.get("status")
    if isinstance(status, dict):
        status.pop("diagnostic_excerpt", None)
    recipe = compact.get("recipe")
    if isinstance(recipe, dict):
        recipe.pop("snapshot", None)
    comparison = compact.get("comparison")
    if isinstance(comparison, dict):
        baseline = comparison.get("baseline")
        if isinstance(baseline, dict):
            baseline.pop("recipe", None)
    return compact


def _compact_implementation(value: dict[str, Any]) -> dict[str, Any]:
    """Build the deterministic high-value implementation fallback."""
    results = value.get("results")
    if not isinstance(results, dict):
        return _compact_target_implementation(value)
    compact = {
        key: copy.deepcopy(value[key])
        for key in ("schema_version", "targets", "grade", "passed", "baseline_ref")
        if key in value
    }
    compact["results"] = {
        str(target): _compact_target_implementation(result)
        for target, result in results.items()
        if isinstance(result, dict)
    }
    return compact


def _payload_size(payload: dict[str, Any]) -> int:
    return len(json.dumps(payload).encode("utf-8"))


def _record_omitted_field(value: dict[str, Any], path: str) -> None:
    fields = value.setdefault("omitted_fields", [])
    if isinstance(fields, list) and path not in fields:
        fields.append(path)


def _drop_per_clock(value: dict[str, Any]) -> None:
    metrics = value.get("metrics")
    if isinstance(metrics, dict) and metrics.pop("per_clock", None) is not None:
        _record_omitted_field(value, "metrics.per_clock")
    comparison = value.get("comparison")
    baseline = comparison.get("baseline") if isinstance(comparison, dict) else None
    baseline_metrics = baseline.get("metrics") if isinstance(baseline, dict) else None
    if isinstance(baseline_metrics, dict) and baseline_metrics.pop("per_clock", None) is not None:
        _record_omitted_field(value, "comparison.baseline.metrics.per_clock")


def _drop_warning_representatives(value: dict[str, Any]) -> None:
    """Drop bounded diagnostic prose while retaining warning counts."""
    conditions = value.get("conditions")
    summary = conditions.get("warning_summary") if isinstance(conditions, dict) else None
    if isinstance(summary, dict) and summary.pop("representatives", None) is not None:
        _record_omitted_field(value, "conditions.warning_summary.representatives")
    comparison = value.get("comparison")
    baseline = comparison.get("baseline") if isinstance(comparison, dict) else None
    baseline_conditions = baseline.get("conditions") if isinstance(baseline, dict) else None
    baseline_summary = (
        baseline_conditions.get("warning_summary")
        if isinstance(baseline_conditions, dict)
        else None
    )
    if (
        isinstance(baseline_summary, dict)
        and baseline_summary.pop("representatives", None) is not None
    ):
        _record_omitted_field(
            value, "comparison.baseline.conditions.warning_summary.representatives"
        )


def _bounded_scalar_map(value: Any) -> dict[str, Any]:
    if not isinstance(value, dict):
        return {}
    scalars: dict[str, Any] = {}
    for index, (key, item) in enumerate(value.items()):
        if index >= 64:
            break
        bounded_key = str(key)[:200]
        if item is None or isinstance(item, (bool, int, float)):
            scalars[bounded_key] = item
        elif isinstance(item, str):
            scalars[bounded_key] = item[:1_000]
    return scalars


def _bounded_nested_map(value: Any) -> dict[str, Any]:
    if not isinstance(value, dict):
        return {}
    bounded: dict[str, Any] = {}
    for index, (key, item) in enumerate(value.items()):
        if index >= 64:
            break
        bounded_key = str(key)[:200]
        if isinstance(item, dict):
            bounded[bounded_key] = _bounded_scalar_map(item)
        elif item is None or isinstance(item, (bool, int, float)):
            bounded[bounded_key] = item
        elif isinstance(item, str):
            bounded[bounded_key] = item[:1_000]
    return bounded


def _minimum_target_implementation(value: dict[str, Any]) -> dict[str, Any]:
    """Retain bounded verdict, scalar QoR, comparison, and recovery paths."""
    minimum: dict[str, Any] = {}
    if "schema_version" in value:
        minimum["schema_version"] = value["schema_version"]
    for key in ("identity", "status", "recipe", "cache"):
        if key in value:
            minimum[key] = _bounded_scalar_map(value[key])
    minimum["metrics"] = _bounded_scalar_map(value.get("metrics"))
    conditions = _minimum_conditions(value.get("conditions"))
    if conditions:
        minimum["conditions"] = conditions
    if "artifacts" in value:
        minimum["artifacts"] = _bounded_nested_map(value["artifacts"])
    comparison = value.get("comparison")
    if isinstance(comparison, dict):
        minimum["comparison"] = _minimum_comparison(comparison)
    if value.get("omitted_fields"):
        minimum["omitted_fields"] = copy.deepcopy(value["omitted_fields"])
    return minimum


def _minimum_conditions(value: Any) -> dict[str, Any]:
    """Retain scalar conditions and bounded warning-count maps."""
    minimum = _bounded_scalar_map(value)
    if not isinstance(value, dict):
        return minimum
    summary = value.get("warning_summary")
    if not isinstance(summary, dict):
        return minimum
    bounded = _bounded_scalar_map(summary)
    for key in ("by_tool", "by_category", "by_disposition"):
        counts = _bounded_scalar_map(summary.get(key))
        if counts:
            bounded[key] = counts
    minimum["warning_summary"] = bounded
    return minimum


def _minimum_comparison(value: dict[str, Any]) -> dict[str, Any]:
    keep = (
        "requested_ref",
        "resolved_ref",
        "baseline_target",
        "candidate_target",
        "basis_valid",
    )
    minimum = {key: copy.deepcopy(value[key]) for key in keep if key in value}
    errors = value.get("basis_errors")
    if isinstance(errors, list):
        minimum["basis_errors"] = [str(error)[:1_000] for error in errors[:10]]
    if "deltas" in value:
        minimum["deltas"] = _bounded_nested_map(value["deltas"])
    baseline = value.get("baseline")
    if isinstance(baseline, dict):
        minimum["baseline"] = {}
        for key in ("status", "cache"):
            if key in baseline:
                minimum["baseline"][key] = _bounded_scalar_map(baseline[key])
        if "artifacts" in baseline:
            minimum["baseline"]["artifacts"] = _bounded_nested_map(baseline["artifacts"])
        minimum["baseline"]["metrics"] = _bounded_scalar_map(baseline.get("metrics"))
        conditions = _minimum_conditions(baseline.get("conditions"))
        if conditions:
            minimum["baseline"]["conditions"] = conditions
    return minimum


def _drop_result_expansions(results: dict[str, Any]) -> None:
    for result in results.values():
        if isinstance(result, dict):
            _drop_warning_representatives(result)
            _drop_per_clock(result)


def _fit_implementation_payload(payload: dict[str, Any], implementation: dict[str, Any]) -> None:
    """Attach canonical detail while respecting the structured-content cap."""
    compact = _compact_implementation(implementation)
    payload["implementation"] = compact
    if _payload_size(payload) <= _MAX_STRUCTURED_REPORT_BYTES:
        return
    results = compact.get("results")
    if not isinstance(results, dict):
        _drop_warning_representatives(compact)
        _drop_per_clock(compact)
        payload["implementation"] = _minimum_target_implementation(compact)
        if _payload_size(payload) <= _MAX_STRUCTURED_REPORT_BYTES:
            return
        payload.pop("implementation", None)
        return
    _drop_result_expansions(results)
    omitted: list[str] = []
    while len(results) > 1 and _payload_size(payload) > _MAX_STRUCTURED_REPORT_BYTES:
        target = next(reversed(results))
        results.pop(target)
        omitted.insert(0, target)
        compact["omitted_targets"] = omitted
    if _payload_size(payload) > _MAX_STRUCTURED_REPORT_BYTES and results:
        target = next(iter(results))
        result = results[target]
        if isinstance(result, dict):
            results[target] = _minimum_target_implementation(result)
    if _payload_size(payload) > _MAX_STRUCTURED_REPORT_BYTES:
        payload.pop("implementation", None)


def _enforce_structured_budget(payload: dict[str, Any]) -> None:
    """Apply a deterministic final guard after all fallback fields are attached."""
    if _payload_size(payload) <= _MAX_STRUCTURED_REPORT_BYTES:
        return
    artifacts = payload.get("artifacts")
    omitted_count = 0
    omitted_sample: list[str] = []
    while (
        isinstance(artifacts, dict)
        and artifacts
        and _payload_size(payload) > _MAX_STRUCTURED_REPORT_BYTES
    ):
        key = next(reversed(artifacts))
        artifacts.pop(key)
        omitted_count += 1
        if len(omitted_sample) < 10:
            omitted_sample.insert(0, key)
        payload["omitted_artifact_entries"] = {
            "count": omitted_count,
            "sample": omitted_sample,
        }
    if _payload_size(payload) > _MAX_STRUCTURED_REPORT_BYTES:
        payload.pop("artifacts", None)
    if _payload_size(payload) > _MAX_STRUCTURED_REPORT_BYTES:
        payload.pop("implementation", None)
    if _payload_size(payload) > _MAX_STRUCTURED_REPORT_BYTES:
        omission = payload.get("omitted_artifact_entries")
        if isinstance(omission, dict):
            omission["sample"] = []
    if _payload_size(payload) > _MAX_STRUCTURED_REPORT_BYTES:
        passed = payload.get("passed")
        payload.clear()
        payload.update({"reports": [], "truncated": True})
        if isinstance(passed, bool):
            payload["passed"] = passed


def _simulation_transport_report(report: dict[str, Any]) -> dict[str, Any]:
    if report.get("flow") == "sim" and isinstance(report.get("cycle_counts"), dict):
        report = dict(report)
        del report["cycle_counts"]
    return report


def _live_checkpoint_summary(report: dict[str, Any]) -> dict[str, Any] | None:
    """Keep current owned log pointers when completed Target detail exceeds the cap."""
    active = report.get("active")
    if report.get("complete") is not False or not isinstance(active, list):
        return None
    summary = {
        key: report[key]
        for key in ("flow", "run_id", "timestamp", "phase", "complete", "partial")
        if key in report
    }
    summary["active"] = [
        {key: item[key] for key in ("target", "stage", "role", "operation", "log") if key in item}
        for item in active
        if isinstance(item, dict)
    ]
    omitted = 0
    while summary["active"] and _payload_size(summary) > _MAX_STRUCTURED_REPORT_BYTES // 2:
        summary["active"].pop()
        omitted += 1
    if omitted:
        summary["omitted_active_entries"] = omitted
    return summary


def _structured_from_report(report: dict[str, Any] | None) -> dict[str, Any] | None:
    """Bounded ``structuredContent`` payload for a run report, or None.

    Never raises: structured attachment is a best-effort enrichment of the
    text card, so any surprise (unserializable value, odd report shape)
    degrades to text-only rather than failing the MCP tool call.
    """
    try:
        if not isinstance(report, dict) or not report:
            return None
        report = _simulation_transport_report(report)
        payload: dict[str, Any] = {"reports": [report]}
        if isinstance(report.get("passed"), bool):
            payload["passed"] = report["passed"]
        if len(json.dumps(payload).encode("utf-8")) > _MAX_STRUCTURED_REPORT_BYTES:
            # Oversized: keep the cheap scalar verdict, drop the heavy body.
            payload = {"reports": [], "truncated": True}
            live = _live_checkpoint_summary(report)
            if live is not None:
                payload.update(live)
            for key in ("flow", "mcp_tool", "target", "exit_code"):
                if report.get(key) is not None:
                    payload[key] = report[key]
            # ...but never drop the artifact pointers. They are a few hundred
            # bytes and they are the whole recovery path: truncation happens on
            # exactly the big, noisy runs where the agent most needs to open
            # the log, and stripping the paths while keeping the verdict left
            # it with a bare "FAIL" and nowhere to go.
            artifacts = _report_artifacts(report)
            if artifacts:
                payload["artifacts"] = artifacts
            implementation = _implementation_from_report(report)
            if implementation is not None:
                _fit_implementation_payload(payload, implementation)
        if isinstance(report.get("passed"), bool):
            payload["passed"] = report["passed"]
        _enforce_structured_budget(payload)
        return payload
    except Exception:  # best-effort enrichment degrades to text-only
        logger.debug("structuredContent attach failed; returning text-only", exc_info=True)
        return None


def _append_stream_section(
    parts: list[str],
    *,
    label: str,
    stream: str,
    limit: int,
    mark_truncation: bool,
) -> tuple[str, bool]:
    """Append one displayed stream tail and return its dedupe inputs."""
    if not stream:
        return "", False
    truncated = len(stream) > limit
    shown = stream[-limit:] if truncated else stream
    rendered = shown
    if truncated and mark_truncation:
        rendered = f"... (truncated, showing last {limit} bytes)\n{shown}"
    parts.append(f"\n--- {label} ---\n{rendered}")
    return shown, truncated


def _append_report_section(
    parts: list[str],
    report: dict[str, Any] | None,
    *,
    shown_stdout: str,
    stdout_truncated: bool,
    shown_stderr: str,
    stderr_truncated: bool,
) -> None:
    """Append durable report fields not already visible in displayed streams."""
    if not report:
        return
    report_lines = []
    for field in ("status", "summary", "errors", "report_text"):
        if field not in report:
            continue
        if field == "report_text":
            report_text = str(report[field])
            shown_in_stdout = _contains_whole_line_block(
                shown_stdout,
                report_text,
                leading_truncated=stdout_truncated,
            )
            shown_in_stderr = _contains_whole_line_block(
                shown_stderr,
                report_text,
                leading_truncated=stderr_truncated,
            )
            if shown_in_stdout or shown_in_stderr:
                continue
        report_lines.append(f"{field}: {report[field]}")
    detail = report.get("detail")
    if isinstance(detail, dict):
        for field in ("reason", "error"):
            if detail.get(field):
                report_lines.append(f"detail.{field}: {detail[field]}")
    if report_lines:
        parts.append("\n--- report ---\n" + "\n".join(report_lines))


def _format_mcp_tool_result(
    exit_code: int,
    stdout: str,
    stderr: str,
    report: dict[str, Any] | None = None,
) -> str:
    """Format hybrid MCP tool result (stdout + report fields)."""
    parts = [f"EXIT_CODE: {exit_code}"]

    shown_stdout, stdout_truncated = _append_stream_section(
        parts,
        label="stdout",
        stream=stdout,
        limit=_max_stdout_bytes(),
        mark_truncation=True,
    )
    shown_stderr, stderr_truncated = _append_stream_section(
        parts,
        label="stderr",
        stream=stderr,
        limit=_max_stderr_bytes(),
        mark_truncation=False,
    )
    _append_report_section(
        parts,
        report,
        shown_stdout=shown_stdout,
        stdout_truncated=stdout_truncated,
        shown_stderr=shown_stderr,
        stderr_truncated=stderr_truncated,
    )

    rendered = "\n".join(parts)
    reason = (report or {}).get("detail", {}).get("evidence_discarded")
    return f"evidence discarded: {reason}\n{rendered}" if reason else rendered


class ReportEncoder:
    """Encode result text and optional structured reports without SDK objects."""

    @staticmethod
    def text_result(
        exit_code: int, stdout: str, stderr: str, report: dict[str, Any] | None = None
    ) -> str:
        """Render bounded stream tails and deduplicated durable report fields."""
        return _format_mcp_tool_result(exit_code, stdout, stderr, report)

    @staticmethod
    def structured_report(report: dict[str, Any] | None) -> dict[str, Any] | None:
        """Return a budgeted JSON payload, or None for text-only fallback."""
        return _structured_from_report(report)
