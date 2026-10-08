"""Immutable Goal change requests and separately audited human decisions.

All writers require the record lock. Pending payloads survive transport token
expiry and process restart; neither a transport token nor a rejection is apply
authority. Applied decisions remain readable for the Phase 5 review package.
"""

from __future__ import annotations

import hashlib
import json
import re
import tempfile
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any, Literal, cast
from uuid import uuid4

from booley.core.boundary import (
    require_dict,
    require_positive_int,
    require_str_value,
    require_uuid4,
)
from booley.goals.changes import Approval, ChangeKind
from booley.goals.model import (
    GoalRecord,
    GoalSpec,
    GoalState,
    WorktreeIdentity,
    parse_goal_arg,
    record_timestamp,
)
from booley.goals.store import RecordLock
from booley.goals.translate import translate_goals
from booley.runtime.atomic_files import atomic_replace_bytes, atomic_write_once, fsync_directory
from booley.runtime.pid import ProcessIdentity
from booley.runtime.timefmt import utc_now_rfc3339

ProposalState = Literal["pending", "approved", "rejected", "applied"]


class ProposalError(ValueError):
    """A saved request or decision is invalid or no longer applicable."""


def encode(value: object) -> bytes:
    """Canonical persisted bytes and digest input for lifecycle values."""
    return (json.dumps(value, sort_keys=True, indent=2, allow_nan=False) + "\n").encode()


def digest(value: object) -> str:
    """The exact canonical payload binding, independent of dictionary ordering."""
    return hashlib.sha256(encode(value)).hexdigest()


def text(value: object, label: str) -> str:
    """A nonblank string at a public or storage boundary."""
    result = require_str_value(value, field=label).strip()
    if not result:
        raise ProposalError(f"{label} must not be blank")
    return result


@dataclass(frozen=True)
class Proposal:
    """An immutable request; runtime params and waiver bindings are displayed too."""

    id: str
    record_id: str
    worktree: WorktreeIdentity
    kind: ChangeKind
    goal_key: str
    before: GoalSpec | None
    after: GoalSpec
    after_arg: dict[str, Any] | None
    runtime_params: dict[str, Any]
    rationale: str
    record_revision: int
    session_key: str | None
    created_at: str
    waiver: dict[str, Any] | None = None

    def to_json(self) -> dict[str, Any]:
        """The complete immutable payload."""
        return {
            "schema": "booley.goal-proposal/v1",
            "id": self.id,
            "record_id": self.record_id,
            "worktree": self.worktree.to_json(),
            "kind": self.kind.value,
            "goal_key": self.goal_key,
            "before": None if self.before is None else self.before.to_json(),
            "after": self.after.to_json(),
            "after_arg": self.after_arg,
            "runtime_params": self.runtime_params,
            "rationale": self.rationale,
            "record_revision": self.record_revision,
            "session_key": self.session_key,
            "created_at": self.created_at,
            "waiver": self.waiver,
        }

    @property
    def payload_digest(self) -> str:
        """The binding every durable decision and issued form echoes."""
        return digest(self.to_json())

    @classmethod
    def from_json(cls, value: object) -> Proposal:
        """Validate a saved payload, including its typed translated after-value."""
        raw = require_dict(value, field="proposal")
        if frozenset(raw) != _PAYLOAD_FIELDS or raw["schema"] != "booley.goal-proposal/v1":
            raise ProposalError("unsupported Goal proposal fields or schema")
        after = GoalSpec.from_json(raw["after"], where="proposal.after")
        arg = (
            None if raw["after_arg"] is None else require_dict(raw["after_arg"], field="after_arg")
        )
        if arg is not None and translate_goals((parse_goal_arg(arg),)).goals[0] != after:
            raise ProposalError("proposal after-value disagrees with its typed Goal arguments")
        kind = ChangeKind(raw["kind"])
        if (kind is ChangeKind.WAIVER) != (arg is None):
            raise ProposalError("only a waiver proposal omits typed after arguments")
        before = (
            None if raw["before"] is None else GoalSpec.from_json(raw["before"], where="before")
        )
        if (kind is ChangeKind.ADD) != (before is None):
            raise ProposalError("proposal before-value disagrees with its kind")
        return cls(
            require_uuid4(raw["id"], field="proposal id"),
            text(raw["record_id"], "record id"),
            WorktreeIdentity.from_json(raw["worktree"], where="proposal.worktree"),
            kind,
            text(raw["goal_key"], "goal_key"),
            before,
            after,
            arg,
            require_dict(raw["runtime_params"], field="runtime_params"),
            text(raw["rationale"], "rationale"),
            require_positive_int(raw["record_revision"], field="record_revision"),
            None if raw["session_key"] is None else text(raw["session_key"], "session_key"),
            record_timestamp(raw["created_at"], "created_at"),
            None if raw["waiver"] is None else require_dict(raw["waiver"], field="waiver"),
        )


_PAYLOAD_FIELDS = frozenset(
    {
        "schema",
        "id",
        "record_id",
        "worktree",
        "kind",
        "goal_key",
        "before",
        "after",
        "after_arg",
        "runtime_params",
        "rationale",
        "record_revision",
        "session_key",
        "created_at",
        "waiver",
    }
)


@dataclass(frozen=True)
class Decision:
    """One human decision, durable independently of application."""

    decision: Literal["approve", "reject"]
    reason: str
    source: Approval
    quote: str | None
    at: str
    payload_digest: str
    session_key: str | None
    transaction_id: str | None = None
    peer_process: str | None = None

    def to_json(self) -> dict[str, Any]:
        """Advisory audit facts; neither attribution nor peer proves human approval."""
        return {
            "decision": self.decision,
            "reason": self.reason,
            "source": self.source.value,
            "quote": self.quote,
            "at": self.at,
            "payload_digest": self.payload_digest,
            "session_key": self.session_key,
            "transaction_id": self.transaction_id,
            "peer_process": self.peer_process,
        }

    @classmethod
    def from_json(cls, value: object) -> Decision:
        """Fail closed on malformed decisions and advisory peer facts."""
        raw = require_dict(value, field="decision")
        if frozenset(raw) != _DECISION_FIELDS or raw["decision"] not in {"approve", "reject"}:
            raise ProposalError("invalid Goal proposal decision")
        source = Approval(raw["source"])
        quote = None if raw["quote"] is None else text(raw["quote"], "approval_quote")
        if source is Approval.AGENT_RECORDED and quote is None:
            raise ProposalError("an agent-recorded decision needs the human's instruction quote")
        return cls(
            cast('Literal["approve", "reject"]', raw["decision"]),
            text(raw["reason"], "reason"),
            source,
            quote,
            record_timestamp(raw["at"], "decision.at"),
            text(raw["payload_digest"], "payload_digest"),
            None if raw["session_key"] is None else text(raw["session_key"], "session_key"),
            None
            if raw["transaction_id"] is None
            else require_uuid4(raw["transaction_id"], field="transaction_id"),
            _peer_process(raw["peer_process"]),
        )


_DECISION_FIELDS = frozenset(
    {
        "decision",
        "reason",
        "source",
        "quote",
        "at",
        "payload_digest",
        "session_key",
        "transaction_id",
        "peer_process",
    }
)


def _peer_process(raw: object) -> str | None:
    if raw is None:
        return None
    value = text(raw, "peer_process")
    try:
        identity = ProcessIdentity.from_payload(json.loads(value))
    except ValueError as exc:
        raise ProposalError("malformed advisory peer process") from exc
    if identity is None or identity.pid <= 0 or identity.start_token < 0:
        raise ProposalError("malformed advisory peer process")
    return json.dumps(identity.to_payload(), sort_keys=True)


@dataclass(frozen=True)
class ProposalView:
    """Read-only payload plus mutable lifecycle and decision."""

    proposal: Proposal
    state: ProposalState = "pending"
    decision: Decision | None = None
    applied_at: str | None = None
    resolved_at: str | None = None
    transaction_digest: str | None = None
    closed_by_abandonment: str | None = None

    def metadata(self) -> dict[str, Any]:
        """Separate mutable metadata, bound to the immutable payload."""
        result: dict[str, Any] = {
            "schema": "booley.goal-proposal-state/v1",
            "payload_digest": self.proposal.payload_digest,
            "state": self.state,
            "decision": None if self.decision is None else self.decision.to_json(),
            "applied_at": self.applied_at,
            "resolved_at": self.resolved_at,
            "closed_by_abandonment": self.closed_by_abandonment,
        }
        if self.transaction_digest is not None:
            result["transaction_digest"] = self.transaction_digest
        return result


def proposal_path(root: Path, proposal_id: str) -> Path:
    """A validated proposal id cannot escape its storage directory."""
    return root / "proposals" / require_uuid4(proposal_id, field="proposal_id")


def load_proposal(root: Path, proposal_id: str) -> ProposalView:
    """Read a completely published payload/metadata pair without repair."""
    path = proposal_path(root, proposal_id)
    try:
        proposal = Proposal.from_json(json.loads((path / "payload.json").read_bytes()))
        raw = json.loads((path / "state.json").read_bytes())
        return _view(proposal, raw)
    except (OSError, ValueError, TypeError) as exc:
        raise ProposalError(f"cannot read proposal {proposal_id}: {exc}") from exc


def _view(proposal: Proposal, value: object) -> ProposalView:
    raw = _state_metadata(value)
    if raw["payload_digest"] != proposal.payload_digest:
        raise ProposalError("Goal proposal payload was substituted")
    state = raw["state"]
    if state not in {"pending", "approved", "rejected", "applied"}:
        raise ProposalError("invalid Goal proposal state")
    decision = None if raw["decision"] is None else Decision.from_json(raw["decision"])
    if (state == "pending") != (decision is None):
        raise ProposalError("proposal state disagrees with durable decision")
    if decision is not None and (
        decision.payload_digest != proposal.payload_digest
        or (state == "rejected") != (decision.decision == "reject")
    ):
        raise ProposalError("proposal decision does not bind this payload/state")
    at = None if raw["applied_at"] is None else record_timestamp(raw["applied_at"], "applied_at")
    if (state == "applied") != (at is not None):
        raise ProposalError("proposal application timestamp disagrees with state")
    resolved_at = (
        None if raw["resolved_at"] is None else record_timestamp(raw["resolved_at"], "resolved_at")
    )
    if resolved_at is not None and state != "rejected":
        raise ProposalError("only rejection effects have a resolution timestamp")
    return ProposalView(
        proposal,
        cast("ProposalState", state),
        decision,
        at,
        resolved_at,
        _transaction_digest(raw["transaction_digest"], decision),
        _closure_timestamp(raw["closed_by_abandonment"]),
    )


def _state_metadata(value: object) -> dict[str, Any]:
    raw = {
        "closed_by_abandonment": None,
        "resolved_at": None,
        "transaction_digest": None,
        **require_dict(value, field="proposal state"),
    }
    if (
        set(raw)
        != {
            "schema",
            "payload_digest",
            "state",
            "decision",
            "applied_at",
            "resolved_at",
            "transaction_digest",
            "closed_by_abandonment",
        }
        or raw["schema"] != "booley.goal-proposal-state/v1"
    ):
        raise ProposalError("invalid Goal proposal lifecycle schema")
    return raw


def _closure_timestamp(value: object) -> str | None:
    return None if value is None else record_timestamp(value, "closed_by_abandonment")


def _transaction_digest(value: object, decision: Decision | None) -> str | None:
    if value is None:
        return None  # Terminal legacy metadata remains readable; recovery requires a binding.
    result = text(value, "transaction_digest")
    if (
        decision is None
        or decision.transaction_id is None
        or re.fullmatch(r"[0-9a-f]{64}", result) is None
    ):
        raise ProposalError("invalid prepared transaction authority digest")
    return result


def list_proposals(root: Path) -> tuple[ProposalView, ...]:
    """Discover exact saved ids after restart; never create or repair files."""
    directory = root / "proposals"
    if not directory.exists():
        return ()
    return tuple(
        load_proposal(root, path.name)
        for path in sorted(directory.iterdir())
        if path.is_dir() and not path.name.startswith(".staging-")
    )


def save_new(lock: RecordLock, proposal: Proposal) -> ProposalView:
    """Publish an immutable, validated request under its record lock."""
    lock.require_owned()
    validated = Proposal.from_json(proposal.to_json())
    if validated.record_id != lock.goal_id:
        raise ProposalError("proposal belongs to another record")
    path = proposal_path(lock.record_dir, validated.id)
    if path.exists():
        raise ProposalError("proposal id is already published")
    path.parent.mkdir(parents=True, exist_ok=True)
    staging = Path(tempfile.mkdtemp(prefix=".staging-", dir=path.parent))
    atomic_write_once(staging / "payload.json", encode(validated.to_json()))
    view = ProposalView(validated)
    atomic_write_once(staging / "state.json", encode(view.metadata()))
    staging.rename(path)
    fsync_directory(path.parent)
    return view


def save_decision(
    lock: RecordLock, view: ProposalView, decision: Decision, *, transaction_digest: str
) -> ProposalView:
    """Only a pending exact payload can receive a decision, including rejection."""
    lock.require_owned()
    current = load_proposal(lock.record_dir, view.proposal.id)
    if (
        current.closed_by_abandonment
        or current.state != "pending"
        or current.proposal != view.proposal
    ):
        raise ProposalError("proposal already decided or payload changed")
    updated = replace(
        view,
        state="approved" if decision.decision == "approve" else "rejected",
        decision=decision,
        transaction_digest=transaction_digest,
    )
    _view(updated.proposal, updated.metadata())
    atomic_replace_bytes(
        proposal_path(lock.record_dir, view.proposal.id) / "state.json", encode(updated.metadata())
    )
    return updated


def finalize(lock: RecordLock, view: ProposalView) -> ProposalView:
    """Idempotently reconcile a completed transaction with its approved decision."""
    lock.require_owned()
    current = load_proposal(lock.record_dir, view.proposal.id)
    if current.state == "applied":
        return current
    if (
        current.state != "approved"
        or current.decision != view.decision
        or current.transaction_digest != view.transaction_digest
    ):
        raise ProposalError("completed apply has no matching approved decision")
    updated = replace(current, state="applied", applied_at=utc_now_rfc3339())
    atomic_replace_bytes(
        proposal_path(lock.record_dir, view.proposal.id) / "state.json", encode(updated.metadata())
    )
    return updated


def new_proposal(record: GoalRecord, **values: Any) -> Proposal:
    """Construct a newly identified proposal bound to this record/worktree."""
    return Proposal(
        id=str(uuid4()),
        record_id=record.id,
        worktree=record.worktree,
        record_revision=record.revision,
        created_at=utc_now_rfc3339(),
        **values,
    )


def finalize_rejection(lock: RecordLock, view: ProposalView) -> ProposalView:
    """Mark only a rejected decision's captured candidate effects complete."""
    lock.require_owned()
    current = load_proposal(lock.record_dir, view.proposal.id)
    if (
        current.state != "rejected"
        or current.decision != view.decision
        or current.transaction_digest != view.transaction_digest
    ):
        raise ProposalError("rejection effects have no matching human rejection")
    if current.resolved_at is not None:
        return current
    updated = replace(current, resolved_at=utc_now_rfc3339())
    atomic_replace_bytes(
        proposal_path(lock.record_dir, view.proposal.id) / "state.json", encode(updated.metadata())
    )
    return updated


def close_by_abandonment(
    lock: RecordLock, view: ProposalView, *, closed_at: str | None = None
) -> ProposalView:
    """Close pre-intent proposals without changing their original human decision."""
    lock.require_owned()
    if view.closed_by_abandonment is not None:
        return view
    if (
        GoalRecord.from_json(json.loads((lock.record_dir / "record.json").read_bytes())).state
        is not GoalState.ABANDONED
    ):
        raise ProposalError("proposal closure requires durable abandonment")
    updated = replace(view, closed_by_abandonment=closed_at or utc_now_rfc3339())
    atomic_replace_bytes(
        proposal_path(lock.record_dir, view.proposal.id) / "state.json", encode(updated.metadata())
    )
    return updated
