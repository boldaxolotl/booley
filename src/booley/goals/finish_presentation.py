"""Versioned immutable presentation bytes, independent of future renderer releases."""

from __future__ import annotations

import hashlib
import html
import json
from collections.abc import Callable
from pathlib import Path
from typing import Any

from booley.core.boundary import require_dict, require_list, require_str_value
from booley.flows.test_runs import rerun_command
from booley.goals.lifecycle import LifecycleOperation
from booley.goals.model import GoalRecord
from booley.goals.naming import optional_names
from booley.goals.paths import record_paths
from booley.goals.presentation import present_spec
from booley.goals.proposals import encode
from booley.review.goal_package import (
    GoalCompletionPackage,
    GoalPackageError,
    observation_facts,
    proposal_facts,
)

# Changed files listed in the Finish overview; the rest are counted and left to the full briefing.
_DIFF_LINES_SHOWN = 20
_ATTEMPT_SCHEMA = "booley.goal-finish-attempt/v3"


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
    """The frozen presentation bytes, never a fresh render."""
    return bytes.fromhex(frozen["presentation"]["html_hex"])


def frozen_response(operation: LifecycleOperation, frozen: dict[str, Any]) -> dict[str, Any]:
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
        _goal_headline(row, presenter),
        f"  Evidence {selected['sequence']} ({row['observation_sha256']}), producer {selected['producer']}, invocation {selected['invocation_id']}, recorded {selected['recorded_at']}, spec revision {row['spec_revision']}.",
    ]
    baseline = row.get("simulation_baseline")
    if baseline is not None:
        lines.append("  Baseline -> final: " + baseline["summary"])
        lines += _attention_lines(baseline)
    detail = selected["detail"]
    lines += _model_fallback_lines(detail)
    lines += _open_finding_lines(detail)
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


def _model_fallback_lines(detail: dict[str, Any]) -> list[str]:
    """A note when the Specialist behind the evidence ran on a fallback ``[models]`` entry."""
    note = detail.get("specialist_model")
    fallback = note.get("fallback") if isinstance(note, dict) else None
    if not fallback:
        return []
    passed_over = "; ".join(f"{item['model']}: {item['reason']}" for item in fallback["skipped"])
    return [
        f"  Model: ran on {note['model']} instead of {fallback['first_choice']} ({passed_over})."
    ]


def _goal_headline(row: dict[str, Any], present: Callable[[object], str]) -> str:
    """The Goal's one-line verdict, shared by the briefing and the overview."""
    return f"- {present(row['spec'])}: met and fresh; {row['metric']}" + (
        "; bound Target modified" if row["modified_target"] else ""
    )


def _attention_lines(baseline: dict[str, Any]) -> list[str]:
    """The simulation baseline's Review attention line, when it has any."""
    if not baseline["attention"]:
        return []
    return ["  Review attention: " + ", ".join(baseline["attention"])]


def _open_finding_lines(detail: dict[str, Any]) -> list[str]:
    """One line per finding the selected observation left open."""
    return [
        "  Open finding: " + json.dumps(finding, sort_keys=True, ensure_ascii=False)
        for finding in detail.get("pending", detail.get("issue_list", []))
    ]


def render_goal_overview(
    package: GoalCompletionPackage,
    *,
    present: Callable[[object], str],
    briefing: list[str],
) -> str:
    """The short Finish reply: readable facts only, with *briefing* naming where the full text is.

    Rendered from the same frozen package as :func:`render_goal_briefing`, which
    stays the published record; this text is never stored.
    """
    facts = package.facts
    lines = [
        f"Goal Mode {facts['record']['id']} finished.",
        "",
        "Session Summary",
        facts["session_summary"],
        "",
        "Goals",
    ]
    for row in facts["goals"]:
        selected = row["selected_observation"]
        lines.append(_goal_headline(row, present))
        lines += _open_finding_lines(selected["detail"])
        baseline = row.get("simulation_baseline")
        if baseline is not None:
            lines += _attention_lines(baseline)
    lines += ["", *_diff_overview(facts["diff_summary"]), "", *_target_overview(facts)]
    decisions = facts["proposal_decisions"]
    lines += [
        "",
        f"Goal Changes: {len(facts['change_log'])}",
        f"Decisions: {len(decisions)} ({len(facts['applied_proposal_decisions'])} applied, "
        f"{len(facts['rejected_proposal_decisions'])} rejected); "
        f"{len(facts['agent_recorded_decisions'])} agent-recorded",
    ]
    if "unsettled_campaigns" in facts:
        unsettled = require_list(facts["unsettled_campaigns"], field="unsettled_campaigns")
        lines.append(f"Simulation Campaigns not fully accounted for: {len(unsettled)}")
    return "\n".join([*lines, "", *briefing])


def _diff_overview(rows: list[dict[str, Any]]) -> list[str]:
    """Counts by kind, then at most :data:`_DIFF_LINES_SHOWN` changed files."""
    if not rows:
        return ["Diff summary: no files changed"]
    counts = {kind: sum(row["change"] == kind for row in rows) for kind in ("added", "modified")}
    removed = len(rows) - sum(counts.values())
    lines = [
        f"Diff summary: {len(rows)} {'file' if len(rows) == 1 else 'files'} changed ({counts['added']} added, "
        f"{counts['modified']} modified, {removed} removed)",
        *(f"- {row['change']} {row['path']}" for row in rows[:_DIFF_LINES_SHOWN]),
    ]
    if len(rows) > _DIFF_LINES_SHOWN:
        lines.append(f"- ... and {len(rows) - _DIFF_LINES_SHOWN} more in the full briefing")
    return lines


def _target_overview(facts: dict[str, Any]) -> list[str]:
    """One line per changed Target; the changed fields only when it was modified."""
    rows = facts["target_changes"]
    if not rows:
        return ["Target changes: none"]
    return [
        f"Target changes: {len(rows)}",
        *(
            f"- {row['target']}: {row['change']}"
            + (f" ({', '.join(row['fields'])})" if row["change"] == "modified" else "")
            for row in rows
        ),
    ]


def finished_overview(booley_dir: Path, record_id: str, operation_id: str) -> str | None:
    """The Finish overview of the attempt *operation_id* froze, or None if it cannot be loaded.

    Reads the attempt's independent authority (written before ``attempt.json``,
    so it exists even when a crash left no artifact) and names the files the
    finish wrote. A load or render failure returns None so the caller falls
    back to the stored response message.
    """
    paths = record_paths(booley_dir, record_id)
    directory = paths.root / "operations" / operation_id
    try:
        authority = require_dict(
            json.loads((directory / "attempt.authority.json").read_bytes()), field="authority"
        )
        frozen = require_dict(authority["value"], field="attempt")
        if frozen["schema"] != _ATTEMPT_SCHEMA:
            return None
        package = GoalCompletionPackage.from_json(frozen["package"])
        history, skipped, branch = frozen["path"], frozen["skip_publication"], frozen["branch"]
        names = optional_names(paths.root)
        published = (
            f"Not published: {skipped}"
            if skipped is not None
            else f"Published: {history} on Goal Branch {branch.removeprefix('refs/heads/')}"
        )
        briefing = [
            f"Full briefing: {paths.summary_file}",
            published,
            f"Review Package data: {paths.review_package_file}",
        ]
        return render_goal_overview(
            package, present=lambda raw: present_spec(raw, names=names), briefing=briefing
        )
    except (OSError, ValueError, KeyError, TypeError, AttributeError, GoalPackageError):
        # A finish that already succeeded must not look failed because its
        # summary cannot be drawn; callers fall back to the stored message.
        return None


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
