"""Frozen legacy completion presentation recipe; upgrade compatibility is intentional."""

from __future__ import annotations

import html
import json

from booley.review.goal_package import GoalCompletionPackage


def render_goal_briefing(package: GoalCompletionPackage) -> str:
    """Render neutral completion facts and open findings, with full audit provenance."""
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
        lines += [
            f"- {row['key']}: met and fresh; {row['metric']}"
            + ("; bound Target modified" if row["modified_target"] else ""),
            f"  Evidence {selected['sequence']} ({row['observation_sha256']}), producer {selected['producer']}, invocation {selected['invocation_id']}, recorded {selected['recorded_at']}, spec revision {row['spec_revision']}.",
        ]
        detail = selected["detail"]
        for finding in detail.get("pending", detail.get("issue_list", [])):
            lines.append(
                "  Open finding: " + json.dumps(finding, sort_keys=True, ensure_ascii=False)
            )
        if row["original_observations"]:
            lines.append(
                "  Approved derivation: " + json.dumps(detail["goal_derivation"], sort_keys=True)
            )
            lines.append(
                "  Original observations: "
                + json.dumps(row["original_observations"], sort_keys=True, ensure_ascii=False)
            )
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
        lines += ["", title, json.dumps(facts[key], indent=2, sort_keys=True, ensure_ascii=False)]
    return "\n".join(lines)


def render_goal_html(package: GoalCompletionPackage) -> str:
    """Requested explanation uses the exact frozen Goal facts and escaped content."""
    body = html.escape(render_goal_briefing(package))
    return (
        '<!doctype html><html lang="en"><meta charset="utf-8"><title>Goal completion</title><body><pre>'
        + body
        + "</pre></body></html>"
    )
