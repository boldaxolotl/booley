"""Frozen Goal completion facts, with no Ticket assessment or approval disposition."""

from __future__ import annotations

import html
import json
from dataclasses import dataclass
from typing import Any

from booley.core.boundary import require_dict, require_int, require_list, require_str_value


class GoalPackageError(ValueError):
    """The completion artifact is malformed or has the wrong purpose."""


@dataclass(frozen=True)
class GoalReviewContext:
    """A completion snapshot selected while holding the finish record lock."""

    record: dict[str, Any]
    base_sha: str
    head_sha: str
    branch: str
    log_dir: str
    session_summary: str
    input_proof: dict[str, Any]
    target_changes: list[dict[str, Any]]
    diff_summary: list[dict[str, Any]]
    target_identities: dict[str, str]


@dataclass(frozen=True)
class GoalCompletionPackage:
    """Explicit presentation purpose: deterministic proof plus Session Summary."""

    facts: dict[str, Any]

    def to_json(self) -> dict[str, Any]:
        """Round-trip without selecting any newer evidence or report."""
        return json.loads(json.dumps(self.facts, allow_nan=False))

    @classmethod
    def from_json(cls, raw: object) -> GoalCompletionPackage:
        """Validate Goal purpose rather than fabricating a Ticket SemanticAssessment."""
        try:
            facts = require_dict(raw, field="Goal completion package")
            _validate_facts(facts)
            return cls(facts)
        except (ValueError, TypeError, KeyError) as exc:
            raise GoalPackageError(f"invalid Goal completion package: {exc}") from exc


def _validate_facts(facts: dict[str, Any]) -> None:
    arrays = (
        "goals",
        "change_log",
        "proposal_decisions",
        "applied_proposal_decisions",
        "rejected_proposal_decisions",
        "agent_recorded_decisions",
        "evidence_transactions",
        "target_changes",
        "diff_summary",
        "constraint_edits",
    )
    fields = {
        "purpose",
        "record",
        "base_sha",
        "head_sha",
        "branch",
        "input_proof",
        "session_summary",
        *arrays,
    }
    if (
        set(facts) not in (fields, fields | {"earlier_attempts"})
        or facts["purpose"] != "booley.goal-completion/v1"
    ):
        raise ValueError("artifact is not a complete Goal completion package")
    for name in ("base_sha", "head_sha", "branch", "session_summary"):
        if not require_str_value(facts[name], field=name).strip():
            raise ValueError(f"{name} must be nonblank")
    record = require_dict(facts["record"], field="record")
    require_str_value(record.get("id"), field="record.id")
    require_dict(facts["input_proof"], field="input_proof")
    for name in arrays:
        for row in require_list(facts[name], field=name):
            require_dict(row, field=name + " row")
    for row in facts.get("earlier_attempts", []):
        require_dict(row, field="earlier attempt")
    for row in facts["goals"]:
        _validate_goal_row(row)
    for row in (*facts["diff_summary"], *facts["constraint_edits"]):
        require_str_value(row.get("path"), field="diff path")
        if row.get("change") not in {"added", "removed", "modified"}:
            raise ValueError("invalid diff change")


def _validate_goal_row(row: dict[str, Any]) -> None:
    fields = {
        "record_id",
        "key",
        "spec_revision",
        "spec",
        "met",
        "fresh",
        "metric",
        "modified_target",
        "selected_observation",
        "observation_sha256",
        "original_observations",
        "selected_transaction",
    }
    if (
        set(row) != fields
        or row["met"] is not True
        or row["fresh"] is not True
        or type(row["modified_target"]) is not bool
    ):
        raise ValueError("invalid completion Goal row")
    for name in ("record_id", "key", "metric", "observation_sha256"):
        require_str_value(row[name], field=name)
    if require_int(row["spec_revision"], field="spec_revision") < 1:
        raise ValueError("invalid Goal spec revision")
    require_dict(row["spec"], field="spec")
    selected = require_dict(row["selected_observation"], field="selected_observation")
    if require_int(selected.get("sequence"), field="observation sequence") < 1:
        raise ValueError("invalid observation sequence")
    for name in ("producer", "recorded_at"):
        require_str_value(selected.get(name), field=name)
    invocation = selected.get("invocation_id")
    if not (
        (isinstance(invocation, str) and invocation)
        or (type(invocation) is int and invocation >= 0)
    ):
        raise ValueError("invalid observation invocation_id")
    detail = require_dict(selected.get("detail"), field="observation detail")
    if "pending" in detail or "issue_list" in detail:
        require_list(detail.get("pending", detail.get("issue_list")), field="open findings")
    originals = require_list(row["original_observations"], field="original_observations")
    for original in originals:
        require_dict(original, field="original observation")
    if originals:
        require_dict(detail.get("goal_derivation"), field="goal_derivation")
    if row["selected_transaction"] is not None:
        require_dict(row["selected_transaction"], field="selected_transaction")


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
    lines += _earlier_attempt_lines(facts)
    return "\n".join(lines)


def render_goal_html(package: GoalCompletionPackage) -> str:
    """Requested explanation uses the exact frozen Goal facts and escaped content."""
    body = html.escape(render_goal_briefing(package))
    return (
        '<!doctype html><html lang="en"><meta charset="utf-8"><title>Goal completion</title><body><pre>'
        + body
        + "</pre></body></html>"
    )


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
