"""Versioned immutable presentation bytes, independent of future renderer releases."""

from __future__ import annotations

import hashlib
import html
import json
from collections.abc import Callable
from typing import Any

from booley.core.boundary import require_dict, require_list, require_str_value
from booley.flows.test_runs import rerun_command
from booley.goals.lifecycle import LifecycleOperation
from booley.goals.model import GoalRecord
from booley.goals.presentation import present_spec
from booley.goals.proposals import encode
from booley.review.goal_package import GoalCompletionPackage, observation_facts, proposal_facts
from booley.review.goal_presentation_v1 import render_goal_html as legacy_html


def response(operation: LifecycleOperation, frozen: dict[str, Any]) -> dict[str, Any]:
    """Paths belong to this operation; message bytes are the literal frozen summary."""
    return {
        "status": "finished",
        "record_id": operation.request.record_id,
        "operation_id": operation.request.operation_id,
        "package": str(operation.directory / "review-package.json"),
        "summary": str(operation.directory / "SUMMARY.md"),
        "history_path": frozen["path"] if frozen["skip_publication"] is None else None,
        "publication_note": frozen["skip_publication"],
        "running_jobs": frozen["running_jobs"],
        "html": str(operation.directory / "explanation.html")
        if operation.request.explain_html
        else None,
        "message": bytes.fromhex(frozen["summary_hex"]).decode("utf-8"),
    }


def freeze_presentation(operation: LifecycleOperation, frozen: dict[str, Any]) -> None:
    """Freeze requested HTML and the complete saved response before the publication fence."""
    names = operation.read_sealed("goal-names")
    html = render_goal_html(
        GoalCompletionPackage.from_json(frozen["package"]),
        present=lambda raw: present_spec(raw, names=names),
    ).encode("utf-8")
    frozen["presentation"] = {
        "recipe": "booley.goal-presentation/v2",
        "html_hex": html.hex() if operation.request.explain_html else None,
        "html_digest": hashlib.sha256(html).hexdigest()
        if operation.request.explain_html
        else None,
        "response_hex": encode(response(operation, frozen)).hex(),
        "response_digest": hashlib.sha256(encode(response(operation, frozen))).hexdigest(),
    }


def html_bytes(frozen: dict[str, Any]) -> bytes:
    """Legacy attempts use their frozen recipe, never the current renderer."""
    if frozen["schema"] == "booley.goal-finish-attempt/v1":
        return legacy_html(GoalCompletionPackage.from_json(frozen["package"])).encode("utf-8")
    return bytes.fromhex(frozen["presentation"]["html_hex"])


def frozen_response(operation: LifecycleOperation, frozen: dict[str, Any]) -> dict[str, Any]:
    if frozen["schema"] == "booley.goal-finish-attempt/v1":
        return response(operation, frozen)
    return require_dict(
        json.loads(bytes.fromhex(frozen["presentation"]["response_hex"])), field="frozen response"
    )


def validate_presentation(operation: LifecycleOperation, frozen: dict[str, Any]) -> None:
    presentation = require_dict(frozen["presentation"], field="frozen presentation")
    if (
        set(presentation)
        != {"recipe", "html_hex", "html_digest", "response_hex", "response_digest"}
        or presentation["recipe"] != "booley.goal-presentation/v2"
    ):
        raise ValueError("invalid frozen presentation schema")
    for name in ("html", "response"):
        value = presentation[name + "_hex"]
        if value is None and name == "html" and not operation.request.explain_html:
            if presentation["html_digest"] is not None:
                raise ValueError("unrequested HTML has a digest")
            continue
        content = bytes.fromhex(require_str_value(value, field=name + " frozen bytes"))
        if hashlib.sha256(content).hexdigest() != presentation[name + "_digest"]:
            raise ValueError("frozen presentation digest differs")
    saved = frozen_response(operation, frozen)
    expected = response(operation, frozen)
    # Storage relocation changes the containing prefix, never the logical artifacts.
    for name, leaf in (
        ("summary", "SUMMARY.md"),
        ("package", "review-package.json"),
        ("html", "explanation.html"),
    ):
        if saved.get(name) is not None:
            if saved[name] != str(operation.sealed_directory("attempt") / leaf):
                raise ValueError("frozen response path differs from its logical operation")
            expected[name] = saved[name]
    if saved != expected:
        raise ValueError("frozen response differs from the complete completion identity")


def _test_run_lines(record: GoalRecord, row: dict[str, Any]) -> list[str]:
    """Render outcomes and reruns from frozen package facts, without live declarations."""
    from booley.goals.test_run_evidence import HistoryScope, run_outcomes

    resolution = record.test_run_resolution(row["key"])
    if resolution is None:
        return []
    goal = next(goal for goal in record.goals if goal.spec.key == row["key"])
    assert goal.spec.target is not None
    selected = row["selected_observation"]
    runs = run_outcomes(
        HistoryScope.of(record, goal),
        resolution.test_runs,
        selected["detail"],
        row["original_observations"],
        before=selected["sequence"],
    )
    shown = ", ".join(f"{run['name']} {run['outcome']}" for run in runs)
    return [
        f"  Test Runs: {shown}",
        *(
            f"  rerun {run.name}: {rerun_command(goal.spec.target, run)}"
            for run in resolution.test_runs
        ),
    ]


def _deferred_resolution_lines(record: dict[str, Any], names: dict[str, str]) -> list[str]:
    """Every Test Run that was frozen after Goal Mode entry, by Goal (ADR 0072).

    *names* gives the public name of each Goal key.
    """
    lines: list[str] = []
    resolutions = require_dict(record.get("test_run_resolutions") or {}, field="resolutions")
    for key in sorted(resolutions):
        resolution = require_dict(resolutions[key], field="resolution")
        for raw in require_list(resolution.get("runs"), field="resolution runs"):
            run = require_dict(raw, field="resolution run")
            if run.get("resolved_by") == "deferred":
                lines.append(
                    f"- {names.get(key, _unavailable_goal(key))}: {run['name']} (slot {run['slot']}, resolved {run['resolved_at']})"
                )
    return ["", "Test Runs resolved after entry", *lines] if lines else []


def render_goal_briefing(
    package: GoalCompletionPackage, *, present: Callable[[object], str] | None = None
) -> str:
    """Render neutral completion facts and open findings, with full audit provenance."""
    facts = package.facts
    presenter = present or _unavailable_goal
    lines = [
        f"Goal Mode {facts['record']['id']} finished.",
        "",
        "Session Summary",
        facts["session_summary"],
        "",
        "Goals",
    ]
    record = GoalRecord.from_json(facts["record"])
    for row in facts["goals"]:
        lines.extend(_goal_lines(row, presenter))
        lines.extend(_test_run_lines(record, row))
    names = {row["key"]: presenter(row["spec"]) for row in facts["goals"]}
    lines += _deferred_resolution_lines(facts["record"], names)
    for title, key in (
        ("Diff summary", "diff_summary"),
        ("Target changes", "target_changes"),
        ("Constraint edits", "constraint_edits"),
        ("Goal Changes", "change_log"),
        ("Proposal decisions", "proposal_decisions"),
        ("Applied decisions", "applied_proposal_decisions"),
        ("Rejected decisions", "rejected_proposal_decisions"),
        ("Agent-recorded decisions", "agent_recorded_decisions"),
        ("Evidence transactions", "evidence_transactions"),
        ("Input proof", "input_proof"),
    ):
        lines += [
            "",
            title,
            json.dumps(
                _human_facts(key, facts[key], presenter),
                indent=2,
                sort_keys=True,
                ensure_ascii=False,
            ),
        ]
    lines += [*_unsettled_campaign_lines(facts), *_earlier_attempt_lines(facts)]
    return "\n".join(lines)


def _goal_lines(row: dict[str, Any], presenter: Callable[[object], str]) -> list[str]:
    lines: list[str] = []
    selected = row["selected_observation"]
    lines += [
        f"- {presenter(row['spec'])}: met and fresh; {row['metric']}"
        + ("; bound Target modified" if row["modified_target"] else ""),
        f"  Evidence {selected['sequence']} ({row['observation_sha256']}), producer {selected['producer']}, invocation {selected['invocation_id']}, recorded {selected['recorded_at']}, spec revision {row['spec_revision']}.",
    ]
    baseline = row.get("simulation_baseline")
    if baseline is not None:
        lines.append("  Baseline -> final: " + baseline["summary"])
        if baseline["attention"]:
            lines.append("  Review attention: " + ", ".join(baseline["attention"]))
    detail = selected["detail"]
    for finding in detail.get("pending", detail.get("issue_list", [])):
        lines.append("  Open finding: " + json.dumps(finding, sort_keys=True, ensure_ascii=False))
    if row["original_observations"]:
        if "goal_derivation" in detail:
            lines.append(
                "  Approved derivation: "
                + json.dumps(_derivation_facts(detail["goal_derivation"]), sort_keys=True)
            )
        lines.append(
            "  Original observations: "
            + json.dumps(
                [observation_facts(item) for item in row["original_observations"]],
                sort_keys=True,
                ensure_ascii=False,
            )
        )
    return lines


def render_goal_html(
    package: GoalCompletionPackage, *, present: Callable[[object], str] | None = None
) -> str:
    """Requested explanation uses the exact frozen Goal facts and escaped content."""
    body = html.escape(render_goal_briefing(package, present=present))
    return (
        '<!doctype html><html lang="en"><meta charset="utf-8"><title>Goal completion</title><body><pre>'
        + body
        + "</pre></body></html>"
    )


def _unsettled_campaign_lines(facts: dict[str, Any]) -> list[str]:
    """Simulation Campaigns whose results were never fully accounted for (ADR 0072)."""
    if "unsettled_campaigns" not in facts:
        return []
    listed = require_list(facts["unsettled_campaigns"], field="unsettled_campaigns")
    return [
        "",
        "Simulation Campaigns not fully accounted for",
        json.dumps(listed, indent=2, sort_keys=True, ensure_ascii=False),
    ]


def _unavailable_goal(_spec: object) -> str:
    return "Goal unavailable (unsupported record)"


def _derivation_facts(row: dict[str, Any]) -> dict[str, Any]:
    return {
        "proposal_id": row.get("proposal_id"),
        "payload_digest": row.get("payload_digest"),
        "source_evidence": [
            {**observation_facts(item), "sha256": item.get("sha256")}
            for item in row.get("source_evidence", [])
        ],
    }


def _human_facts(section: str, value: Any, present: Callable[[object], str]) -> Any:
    if section == "change_log":
        return [_change_facts(row, present) for row in value]
    if section.endswith("decisions"):
        return [_decision_facts(row, present) for row in value]
    if section == "evidence_transactions":
        return [
            {name: row[name] for name in ("transaction_id", "manifest_sha256", "observations")}
            for row in value
        ]
    return value


def _change_facts(row: dict[str, Any], present: Callable[[object], str]) -> dict[str, Any]:
    facts = {
        name: value
        for name, value in row.items()
        if name not in {"goal_key", "before", "after", "runtime_params", "after_arg", "waiver"}
    }
    for name in ("before", "after"):
        if name in row:
            facts[name] = None if row[name] is None else present(row[name])
    return facts


def _decision_facts(row: dict[str, Any], present: Callable[[object], str]) -> dict[str, Any]:
    return {
        **proposal_facts(row["proposal"], present),
        **{
            name: row[name]
            for name in (
                "state",
                "decision",
                "payload_digest",
                "transaction_digest",
                "closed_by_abandonment",
            )
            if name in row
        },
    }


def _earlier_attempt_lines(facts: dict[str, Any]) -> list[str]:
    return (
        []
        if "earlier_attempts" not in facts
        else [
            "",
            "Earlier attempts",
            json.dumps(facts["earlier_attempts"], indent=2, sort_keys=True),
        ]
    )
