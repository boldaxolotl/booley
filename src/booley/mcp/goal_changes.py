"""Goal approval forms and exact-ID fallback through the application request seam."""

from __future__ import annotations

import json
from collections.abc import Mapping
from pathlib import Path
from typing import Any, Literal, cast

from mcp.types import TextContent

from booley.core.boundary import require_dict
from booley.goals.apply import ChangeEnvironment
from booley.goals.change_service import (
    create_proposal,
    record_decision,
    resolve_record,
    resume_proposal,
)
from booley.goals.changes import Approval
from booley.goals.entry import EntryEnvironment
from booley.goals.model import goal_arg_json_schema
from booley.goals.proposals import ProposalError, ProposalView, text
from booley.goals.session_key import session_key
from booley.goals.store import GoalStore
from booley.mcp.application import McpDispatchResult, McpInputRequired, McpRequestContext
from booley.mcp.goal_waivers import GoalWaiverService

RESPONSE_KEY = "goal_change_decision"


def proposal_schema(work_dir: Mapping[str, Any]) -> dict[str, Any]:
    """No requestState/inputResponses argument can spoof the request envelope."""
    return {
        "type": "object",
        "properties": {
            "work_dir": dict(work_dir),
            "operation": {"enum": ["create", "resume", "approve", "reject"]},
            "proposal_id": {"type": "string", "format": "uuid"},
            "kind": {"enum": ["add", "relax", "retarget", "waiver"]},
            "goal_key": {"type": "string", "minLength": 1},
            "after": goal_arg_json_schema(),
            "candidate_id": {"type": "string", "minLength": 1},
            "rationale": {"type": "string", "minLength": 1},
            "reason": {"type": "string", "minLength": 1},
            "approval_quote": {"type": "string", "minLength": 1},
        },
        "required": ["work_dir", "operation"],
        "additionalProperties": False,
        "allOf": [
            {
                "if": {"properties": {"operation": {"const": "create"}}},
                "then": {"required": ["kind", "rationale"]},
            },
            {
                "if": {"properties": {"operation": {"enum": ["resume", "approve", "reject"]}}},
                "then": {"required": ["proposal_id"]},
            },
            {
                "if": {"properties": {"operation": {"enum": ["approve", "reject"]}}},
                "then": {"required": ["reason", "approval_quote"]},
            },
        ],
    }


def propose_change(
    arguments: Mapping[str, Any], env: EntryEnvironment, context: McpRequestContext | None
) -> McpDispatchResult | McpInputRequired:
    """SDK-free lifecycle orchestration; production dispatch runs this in one worker."""
    if any(
        key in arguments
        for key in ("request_state", "input_responses", "requestState", "inputResponses")
    ):
        raise ProposalError(
            "approval inputs belong to the verified request envelope, never tool arguments"
        )
    context = context or McpRequestContext()
    preliminary = ChangeEnvironment(env)
    work_dir = Path(text(arguments.get("work_dir"), "work_dir"))
    record = resolve_record(preliminary, work_dir)
    service = GoalWaiverService(env.project_dir, record)
    change_env = ChangeEnvironment(env, service)
    acting = (
        context.attribution.key
        if context.attribution
        else session_key(GoalStore(env.project_dir), work_dir)
    )
    if context.resume_state is not None:
        return _resume_decision(change_env, record.id, context, acting)
    operation = arguments.get("operation")
    if operation == "create":
        view = create_proposal(change_env, record.id, arguments, session_key=acting)
    elif operation in {"resume", "approve", "reject"}:
        proposal_id = text(arguments.get("proposal_id"), "proposal_id")
        view = resume_proposal(change_env, record.id, proposal_id)
        if operation != "resume":
            result = record_decision(
                change_env,
                record.id,
                proposal_id,
                answer=cast('Literal["approve", "reject"]', operation),
                reason=text(arguments.get("reason"), "reason"),
                source=Approval.AGENT_RECORDED,
                quote=text(arguments.get("approval_quote"), "approval_quote"),
                session_key=acting,
            )
            return _result(result)
    else:
        raise ProposalError("operation must be create, resume, approve or reject")
    return _pending_response(view, record.id, context)


def _resume_decision(
    env: ChangeEnvironment, record_id: str, context: McpRequestContext, acting: str | None
) -> McpDispatchResult | McpInputRequired:
    assert context.resume_state is not None
    binding = require_dict(json.loads(context.resume_state), field="resume state")
    view = resume_proposal(env, record_id, text(binding.get("proposal_id"), "proposal_id"))
    _require_binding(binding, view, record_id)
    answer = _form_answer(context.input_responses)
    if answer is None:
        return _approval_required(view)
    return _result(
        record_decision(
            env,
            record_id,
            view.proposal.id,
            answer=answer[0],
            reason=answer[1],
            source=Approval.ELICITED,
            quote=None,
            session_key=acting,
            peer_process=(
                None
                if context.attribution is None or context.attribution.process is None
                else json.dumps(context.attribution.process.to_payload(), sort_keys=True)
            ),
        )
    )


def _pending_response(
    view: ProposalView, record_id: str, context: McpRequestContext
) -> McpDispatchResult | McpInputRequired:
    if view.state != "pending":
        return _result(view)
    if context.form_capability and context.protocol == "2026-07-28":
        binding = {
            "proposal_id": view.proposal.id,
            "payload_digest": view.proposal.payload_digest,
            "record_id": record_id,
            "worktree": view.proposal.worktree.to_json(),
        }
        return McpInputRequired(
            json.dumps(binding, sort_keys=True),
            RESPONSE_KEY,
            _summary(view),
            {
                "type": "object",
                "properties": {
                    "decision": {"type": "string", "enum": ["approve", "reject"]},
                    "reason": {"type": "string", "minLength": 1},
                },
                "required": ["decision", "reason"],
                "additionalProperties": False,
            },
        )
    return _approval_required(view)


def _require_binding(binding: Mapping[str, Any], view: ProposalView, record_id: str) -> None:
    proposal = view.proposal
    expected = {
        "proposal_id": proposal.id,
        "payload_digest": proposal.payload_digest,
        "record_id": record_id,
        "worktree": proposal.worktree.to_json(),
    }
    if dict(binding) != expected or view.state != "pending":
        raise ProposalError("resume state does not bind this pending proposal/record/worktree")


def _form_answer(
    responses: Mapping[str, Any] | None,
) -> tuple[Literal["approve", "reject"], str] | None:
    if responses is None or set(responses) != {RESPONSE_KEY}:
        return None
    response = responses[RESPONSE_KEY]
    if not isinstance(response, Mapping):
        return None
    response = cast("Mapping[str, Any]", response)
    if response.get("action") != "accept":
        return None
    content = response.get("content")
    if not isinstance(content, Mapping):
        return None
    content = cast("Mapping[str, Any]", content)
    if set(content) != {"decision", "reason"} or content.get("decision") not in {
        "approve",
        "reject",
    }:
        return None
    reason = content.get("reason")
    return (
        (cast('Literal["approve", "reject"]', content["decision"]), reason.strip())
        if isinstance(reason, str) and reason.strip()
        else None
    )


def _summary(view: ProposalView) -> str:
    proposal = view.proposal
    value: dict[str, Any] = {
        "proposal_id": proposal.id,
        "state": view.state,
        "kind": proposal.kind.value,
        "goal_key": proposal.goal_key,
        "before": None if proposal.before is None else proposal.before.to_json(),
        "after": proposal.after.to_json(),
        "prepared_runtime_params": proposal.runtime_params,
        "rationale": proposal.rationale,
    }
    if proposal.waiver is not None:
        value["waiver"] = proposal.waiver
        value["commit_files"] = "Commit the installed approval/proof files under " + str(
            proposal.waiver["approval_root"]
        )
    if view.decision is not None:
        value["decision"] = view.decision.to_json()
    return json.dumps(value, sort_keys=True)


def _approval_required(view: ProposalView) -> McpDispatchResult:
    result = {
        "status": "approval_required",
        "proposal_id": view.proposal.id,
        "proposal": json.loads(_summary(view)),
        "instructions": "Obtain the human's instruction. Make a fresh goal_propose_change call without a sealed token: operation=approve or reject, this exact proposal_id, reason and approval_quote. Resume by proposal_id to reissue an expired/restarted form.",
    }
    return McpDispatchResult(
        [TextContent(type="text", text=json.dumps(result, sort_keys=True))], False
    )


def _result(view: ProposalView) -> McpDispatchResult:
    return McpDispatchResult([TextContent(type="text", text=_summary(view))], False)
