"""Frozen Goal completion facts, with no Ticket assessment or approval disposition."""

from __future__ import annotations

import json
from collections.abc import Callable
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
        not fields <= set(facts) <= fields | {"earlier_attempts", "unsettled_campaigns"}
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
        _validate_goal_row(row, record)
    for row in (*facts["diff_summary"], *facts["constraint_edits"]):
        require_str_value(row.get("path"), field="diff path")
        if row.get("change") not in {"added", "removed", "modified"}:
            raise ValueError("invalid diff change")


def _validate_goal_row(row: dict[str, Any], record: dict[str, Any]) -> None:
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
        not fields <= set(row) <= fields | {"simulation_baseline"}
        or row["met"] is not True
        or row["fresh"] is not True
        or type(row["modified_target"]) is not bool
    ):
        raise ValueError("invalid completion Goal row")
    _validate_baseline_report(row)
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
    if originals and row["key"] not in record.get("test_run_resolutions", {}):
        require_dict(detail.get("goal_derivation"), field="goal_derivation")
    if row["selected_transaction"] is not None:
        require_dict(row["selected_transaction"], field="selected_transaction")


def _validate_baseline_report(row: dict[str, Any]) -> None:
    if "simulation_baseline" in row:
        baseline = require_dict(row["simulation_baseline"], field="simulation baseline")
        require_str_value(baseline.get("summary"), field="baseline summary")
        for run in require_list(baseline.get("runs"), field="baseline transitions"):
            require_dict(run, field="baseline transition")
        for name in require_list(baseline.get("attention"), field="baseline attention"):
            require_str_value(name, field="baseline attention Test Run")


def observation_facts(row: dict[str, Any]) -> dict[str, Any]:
    """Retain readable evidence while projecting out its machine authority envelope."""
    facts = {
        name: row[name]
        for name in ("sequence", "producer", "invocation_id", "recorded_at")
        if name in row
    }
    detail = row.get("detail", {})
    observations = {
        name: value
        for name, value in detail.items()
        if name
        not in {
            "_source_fingerprint",
            "goal_derivation",
            "goal_identity",
            "criterion",
            "goal_key",
            "goal_keys",
            "spec_revisions",
        }
    }
    if observations:
        facts["observations"] = observations
    return facts


def proposal_facts(proposal: dict[str, Any], present: Callable[[object], str]) -> dict[str, Any]:
    """One neutral approval/audit projection shared by all human surfaces."""
    facts: dict[str, Any] = {
        "proposal_id": proposal["id"],
        "kind": proposal["kind"],
        "before": None if proposal["before"] is None else present(proposal["before"]),
        "after": present(proposal["after"]),
        "rationale": proposal["rationale"],
    }
    if proposal["kind"] == "retarget" and proposal["after"].get("family") == "review":
        facts["review_retarget_effect"] = (
            "Discards the prior review evidence and open-finding lifecycle. "
            "Reducing the file scope or specification weakens the review requirement. "
            "A new discovery review is required after approval."
        )
    waiver = proposal.get("waiver")
    if waiver is not None:
        facts["waiver"] = {
            name: waiver[name] for name in ("candidate_id", "candidate", "policy", "approval_root")
        }
    return facts
