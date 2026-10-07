"""Ticket composition over the generic Criterion evidence ledger.

Durable evidence storage (V1 appends, V2 Simulation Campaign transactions,
their locks, recovery, and checkpoints) lives in
:mod:`booley.criteria.evidence_ledger`. This module binds it to Ticket Mode:
the ``ticket_acceptance`` purpose, the :class:`TicketIdentity` codec (the
32-hex ``generation`` validator and the Ticket-shaped envelope), and the
``report_submission`` completion fencing applied on projection.

It also owns what only Tickets have: an accepted Ticket is represented by a
content-addressed Criteria Satisfaction Record outside the runtime
directory, bound to its review package, with report projection fenced by the
latest validated observation. Readers return honest missing/corrupt results.
"""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Callable, Mapping
from copy import deepcopy
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal, cast

from booley.core.differences import format_differences
from booley.criteria import evidence_ledger
from booley.criteria.evidence_ledger import (
    MAX_RECORD_BYTES,
    SCHEMA_VERSION,
    AcceptanceLedgerError,
    AcceptanceTransaction,
    EvidenceRef,
    EvidenceScope,
    canonical_json,
    legacy_transaction_records,
    plain_json_value,
    read_evidence_records,
    read_json_document,
    validated_evidence_records,
    write_once,
)
from booley.criteria.state import CriterionChange, DevelopmentState
from booley.runtime.timefmt import utc_now_rfc3339

from .persistence import atomic_replace_bytes

__all__ = [
    "SCHEMA_VERSION",
    "TICKET_ACCEPTANCE_PURPOSE",
    "TICKET_REPORT_PROJECTION",
    "TICKET_SCOPE",
    "AcceptanceLedgerError",
    "AcceptanceReadResult",
    "AcceptanceSnapshot",
    "AcceptanceTransaction",
    "EvidenceRef",
    "TicketIdentity",
    "TicketReportProjection",
    "bind_review_package",
    "current_evidence_records",
    "effective_state_bytes",
    "freeze_acceptance",
    "historical_ticket_identities",
    "project_report_mapping",
    "project_report_state",
    "read_acceptance",
    "record_amendment_observations",
    "record_changes",
    "record_or_verify_transaction",
    "submitted_report",
    "validate_review_package_binding",
]

TICKET_ACCEPTANCE_PURPOSE = "ticket_acceptance"
_HEX_32 = re.compile(r"[0-9a-f]{32}")


@dataclass(frozen=True)
class AcceptanceSnapshot:
    """One immutable accepted projection of a Ticket's Criteria."""

    digest: str
    slug: str
    ticket_type: str
    execution_id: str
    accepted_at: str
    ticket_identity: dict[str, Any]
    participant_heads: dict[str, str]
    criteria: dict[str, dict[str, Any]]
    evidence: tuple[dict[str, Any], ...]


def _snapshot_mapping(snapshot: AcceptanceSnapshot) -> dict[str, object]:
    return {
        "digest": snapshot.digest,
        "slug": snapshot.slug,
        "ticket_type": snapshot.ticket_type,
        "execution_id": snapshot.execution_id,
        "accepted_at": snapshot.accepted_at,
        "ticket_identity": snapshot.ticket_identity,
        "participant_heads": snapshot.participant_heads,
        "criteria": snapshot.criteria,
        "evidence": snapshot.evidence,
    }


@dataclass(frozen=True)
class AcceptanceReadResult:
    """Lifecycle reader result that never turns missing evidence into false."""

    kind: Literal["accepted", "unavailable", "corrupt"]
    snapshot: AcceptanceSnapshot | None = None
    reason: str = ""


def _identity(value: object, generation: object) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        raise AcceptanceLedgerError("ticket identity must be an object")
    identity = plain_json_value(value)
    if (
        not isinstance(generation, str)
        or _HEX_32.fullmatch(generation) is None
        or identity.get("generation") != generation
    ):
        raise AcceptanceLedgerError("ticket generation must match the canonical identity")
    canonical_json(identity)
    return identity


class TicketIdentity:
    """Identity codec for ``ticket_acceptance`` evidence.

    A V2 identity is a JSON object whose ``generation`` is 32 lowercase hex
    digits; envelopes carry ``ticket: {slug, identity, generation}`` and
    records carry ``ticket`` (the slug) and ``ticket_identity``.
    """

    subject_field = "ticket"
    subject_fields = frozenset({"slug", "identity", "generation"})
    identity_field = "ticket_identity"

    def lookup_identity(self, identity: Mapping[str, Any]) -> dict[str, Any]:
        generation = identity.get("generation")
        return {
            "ticket_identity": _identity(identity, generation),
            "ticket_generation": generation,
        }

    def envelope_subject(
        self, state: DevelopmentState, lookup_identity: Mapping[str, Any]
    ) -> dict[str, Any]:
        if not state.slug:
            raise AcceptanceLedgerError("Ticket slug must not be empty")
        return {
            "slug": state.slug,
            "identity": lookup_identity["ticket_identity"],
            "generation": lookup_identity["ticket_generation"],
        }

    def validate_envelope_subject(self, subject: Mapping[str, Any]) -> None:
        if not isinstance(subject.get("slug"), str) or not subject["slug"]:
            raise AcceptanceLedgerError("invalid transaction Ticket slug")
        _identity(subject.get("identity"), subject.get("generation"))

    def subject_lookup_identity(self, subject: Mapping[str, Any]) -> dict[str, Any]:
        return {
            "ticket_identity": subject["identity"],
            "ticket_generation": subject["generation"],
        }

    def record_subject(self, subject: Mapping[str, Any]) -> dict[str, Any]:
        return {"ticket": subject["slug"], "ticket_identity": subject["identity"]}

    def append_subject(
        self, state: DevelopmentState, identity: Mapping[str, Any] | None
    ) -> dict[str, Any]:
        return {"ticket": state.slug, "ticket_identity": dict(identity or {})}

    def validate_observation_identity(self, value: object, current: Mapping[str, Any]) -> None:
        _validate_observation_identity(value, current)


def _validate_observation_identity(value: object, current: Mapping[str, Any]) -> None:
    if not current:
        if not isinstance(value, dict):
            raise AcceptanceLedgerError("Criterion evidence has malformed Ticket identity")
        return
    if not isinstance(value, dict) or not value:
        raise AcceptanceLedgerError(
            "Criterion evidence names another Ticket identity: missing identity"
        )
    _validate_stored_identity(cast("dict[str, Any]", value), current)


def _validate_stored_identity(value: dict[str, Any], current: Mapping[str, Any]) -> None:
    _identity(value, value.get("generation"))
    if "schema" in value:
        from .ticket_baseline import TicketBaselineError, ticket_baseline_from_machine

        try:
            ticket_baseline_from_machine(value)
        except (TicketBaselineError, ValueError) as exc:
            raise AcceptanceLedgerError(
                "Criterion evidence has malformed Ticket identity"
            ) from exc
    authored = value.get("authored_sha256")
    if authored is not None and (
        not isinstance(authored, str) or re.fullmatch(r"[0-9a-f]{64}", authored) is None
    ):
        raise AcceptanceLedgerError("Criterion evidence has malformed authored identity")
    baseline = value.get("baseline")
    if baseline is not None:
        if not isinstance(baseline, dict) or not baseline:
            raise AcceptanceLedgerError("Criterion evidence has malformed baseline identity")
        for participant in cast("dict[str, Any]", baseline).values():
            commit = (
                cast("dict[str, Any]", participant).get("commit")
                if isinstance(participant, dict)
                else None
            )
            if not isinstance(commit, str) or re.fullmatch(r"[0-9a-f]{40}", commit) is None:
                raise AcceptanceLedgerError("Criterion evidence has malformed baseline identity")
    for key in ("authored_sha256", "baseline"):
        if key in current and (key not in value or not isinstance(value[key], type(current[key]))):
            raise AcceptanceLedgerError("Criterion evidence has malformed Ticket identity")


class TicketReportProjection:
    """Completion fencing of the Ticket report Criterion on projection."""

    def current_observations(
        self,
        log_dir: Path,
        state: DevelopmentState,
        identity: Mapping[str, Any],
        records: list[dict[str, Any]],
    ) -> list[dict[str, Any]]:
        """Ticket evidence has one identity per Ticket: its own records stand."""
        return records

    def project_state(
        self, state: DevelopmentState, log_dir: Path, identity: Mapping[str, Any]
    ) -> None:
        from .report_submission import project_state

        project_state(state, log_dir, identity=identity)

    def effective_met(
        self,
        log_dir: Path,
        criterion: str,
        met: object,
        detail: object,
        identity: Mapping[str, Any],
    ) -> object:
        if criterion != "_report_submitted":
            return met
        from .report_submission import effective_met

        # Stored observations pass their raw JSON values, exactly as before the
        # ledger split; report fencing performs its own checks on them.
        return effective_met(
            log_dir, cast("bool", met), cast("Mapping[str, Any]", detail), identity=identity
        )


TICKET_SCOPE = EvidenceScope(TICKET_ACCEPTANCE_PURPOSE, TicketIdentity())
TICKET_REPORT_PROJECTION = TicketReportProjection()


def record_changes(
    log_dir: Path,
    state: DevelopmentState,
    changes: list[CriterionChange],
    *,
    invocation_id: str,
    producer: str,
    execution_id: str,
    ticket_identity: Mapping[str, Any] | None = None,
    recorded_at: str | None = None,
    transaction_id: str = "",
) -> tuple[EvidenceRef, ...]:
    """Persist normalized effective Criterion changes in completion order."""
    return evidence_ledger.record_changes(
        log_dir,
        state,
        changes,
        scope=TICKET_SCOPE,
        invocation_id=invocation_id,
        producer=producer,
        execution_id=execution_id,
        identity=ticket_identity,
        recorded_at=recorded_at,
        transaction_id=transaction_id,
    )


def record_or_verify_transaction(
    log_dir: Path,
    state: DevelopmentState,
    changes: list[CriterionChange],
    *,
    acceptance_facts: Mapping[str, Any],
    ticket_identity: Mapping[str, Any],
    publication_checkpoint: Callable[[str], None] | None = None,
) -> AcceptanceTransaction:
    """Record or verify one intent-frozen Simulation Campaign transaction."""
    return evidence_ledger.record_or_verify_transaction(
        log_dir,
        state,
        changes,
        scope=TICKET_SCOPE,
        projection=TICKET_REPORT_PROJECTION,
        acceptance_facts=acceptance_facts,
        identity=ticket_identity,
        publication_checkpoint=publication_checkpoint,
    )


def historical_ticket_identities(
    log_dir: Path, state: DevelopmentState, ticket_identity: Mapping[str, Any]
) -> list[dict[str, Any]]:
    """Return distinct identities only after validating selected historical observations."""
    identities: list[dict[str, Any]] = []
    for row in validated_evidence_records(TICKET_SCOPE, log_dir, state, ticket_identity):
        identity = row["ticket_identity"]
        if identity != ticket_identity and identity not in identities:
            identities.append(identity)
    return identities


def current_evidence_records(
    log_dir: Path, state: DevelopmentState, ticket_identity: Mapping[str, Any]
) -> list[dict[str, Any]]:
    """Read validated observations for one identity without replaying history."""
    return read_evidence_records(TICKET_SCOPE, log_dir, state, ticket_identity)


def _validate_state_projection(
    log_dir: Path, state: DevelopmentState, ticket_identity: Mapping[str, Any]
) -> None:
    evidence_ledger.validate_state_projection(
        TICKET_SCOPE, TICKET_REPORT_PROJECTION, log_dir, state, ticket_identity
    )


def _read_evidence_refs(
    log_dir: Path, state: DevelopmentState, ticket_identity: Mapping[str, Any]
) -> list[dict[str, Any]]:
    """Return integrity-checked references to every immutable observation."""
    return [
        {
            "sequence": payload["sequence"],
            "digest": hashlib.sha256(canonical_json(payload)).hexdigest(),
            "criterion": payload["criterion"],
            "role": payload["role"],
        }
        for payload in current_evidence_records(log_dir, state, ticket_identity)
    ]


def _verify_amendment_prefix(
    records: list[dict[str, Any]],
    changes: list[CriterionChange],
    ticket_identity: Mapping[str, Any],
) -> None:
    for record, change in zip(records, changes[: len(records)], strict=True):
        if any(
            record.get(key) != value
            for key, value in {
                "ticket_identity": dict(ticket_identity),
                "criterion": change.key,
                "met": change.met,
                "mandatory": change.mandatory,
                "params": change.params,
                "detail": change.detail,
            }.items()
        ):
            raise AcceptanceLedgerError("amendment observation changed during retry")


def record_amendment_observations(
    log_dir: Path,
    state: DevelopmentState,
    changes: list[CriterionChange],
    *,
    operation_id: str,
    ticket_identity: Mapping[str, Any],
) -> None:
    """Publish and select idempotent generic observations of validated retained proof."""
    transaction = hashlib.sha256(
        canonical_json(
            {
                "operation_id": operation_id,
                "ticket_identity": dict(ticket_identity),
            }
        )
    ).hexdigest()
    root = log_dir / "acceptance" / "evidence"
    if root.exists():
        for directory in root.glob(f"*.tx.{transaction}"):
            if not (directory / "record.json").exists():
                for temporary in directory.glob(".record.json.*.tmp"):
                    temporary.unlink()
                try:
                    directory.rmdir()
                except OSError as exc:
                    raise AcceptanceLedgerError(
                        f"invalid amendment proof reservation {directory}: {exc}"
                    ) from exc
    if root.exists() and list(root.glob(f"*.tx.{transaction}")):
        records = legacy_transaction_records(root, transaction)
        if len(records) > len(changes):
            raise AcceptanceLedgerError("incomplete amendment observation transaction")
        _verify_amendment_prefix(records, changes, ticket_identity)
    else:
        records = []
    if len(records) < len(changes):
        record_changes(
            log_dir,
            state,
            changes[len(records) :],
            invocation_id=operation_id,
            producer="amendment",
            execution_id=operation_id,
            ticket_identity=ticket_identity,
            transaction_id=transaction,
        )
    if changes and transaction not in state.acceptance_transactions:
        state.acceptance_transactions.append(transaction)


def _snapshot_from_payload(payload: Mapping[str, Any], digest: str) -> AcceptanceSnapshot:
    if "acceptance_basis" in payload:
        raise AcceptanceLedgerError("unsupported Ticket format: recreate this Ticket")
    try:
        return AcceptanceSnapshot(
            digest=digest,
            slug=str(payload["slug"]),
            ticket_type=str(payload["ticket_type"]),
            execution_id=str(payload["execution_id"]),
            accepted_at=str(payload["accepted_at"]),
            ticket_identity=dict(payload.get("ticket_identity") or {}),
            participant_heads=_participant_heads(payload["participant_heads"]),
            criteria={key: dict(value) for key, value in dict(payload["criteria"]).items()},
            evidence=tuple(dict(value) for value in payload.get("evidence", [])),
        )
    except (KeyError, TypeError, ValueError) as exc:
        raise AcceptanceLedgerError(f"invalid Criteria Satisfaction Record: {exc}") from exc


def _participant_heads(value: Any) -> dict[str, str]:
    if not isinstance(value, Mapping):
        raise AcceptanceLedgerError("participant_heads must be a mapping")
    heads = dict(cast("Mapping[str, Any]", value))
    if not set(heads) <= {"outer", "project"} or any(
        not isinstance(commit, str) or re.fullmatch(r"[0-9a-f]{40}|[0-9a-f]{64}", commit) is None
        for commit in heads.values()
    ):
        raise AcceptanceLedgerError("participant_heads contains an invalid commit identity")
    if "outer" not in heads:
        raise AcceptanceLedgerError("participant_heads requires an outer commit identity")
    return cast("dict[str, str]", heads)


def _validate_current_proof(
    log_dir: Path, state: DevelopmentState, identity: Mapping[str, Any]
) -> None:
    if not identity:
        return
    current = {row["criterion"] for row in current_evidence_records(log_dir, state, identity)}
    root = log_dir / "acceptance" / "evidence"
    if not root.exists():
        return
    historical: set[Any] = set()
    for path in root.glob("*/record.json"):
        if re.fullmatch(r"[0-9]{9}(?:\.tx\.[0-9a-f]{64})?", path.parent.name) is None:
            continue
        row = read_json_document(path, MAX_RECORD_BYTES, "Criterion evidence")
        if row.get("ticket_identity") != identity:
            historical.add(row.get("criterion"))
    for name in historical - current:
        entry = state.criteria.get(name)
        if entry is not None and entry.met:
            raise AcceptanceLedgerError(f"Criterion {name!r} is missing current-generation proof")


def freeze_acceptance(
    log_dir: Path,
    state: DevelopmentState,
    *,
    execution_id: str,
    ticket_identity: Mapping[str, Any] | None,
    participant_heads: Mapping[str, str],
    accepted_at: str | None = None,
) -> AcceptanceSnapshot:
    """Freeze and select one Criteria Satisfaction Record for the current Ticket epoch."""
    identity = dict(ticket_identity or {})
    _validate_state_projection(log_dir, state, identity)
    _validate_current_proof(log_dir, state, identity)
    from .report_submission import project_state, synchronize

    synchronize(log_dir)
    state = deepcopy(state)
    project_state(state, log_dir, identity=identity)
    payload = {
        "schema": SCHEMA_VERSION,
        "slug": state.slug,
        "ticket_type": state.ticket_type,
        "execution_id": execution_id,
        "accepted_at": accepted_at or utc_now_rfc3339(),
        "ticket_identity": identity,
        "participant_heads": _participant_heads(participant_heads),
        "criteria": {key: entry.to_dict() for key, entry in state.criteria.items()},
        "evidence": _read_evidence_refs(log_dir, state, identity),
    }
    encoded = canonical_json(payload)
    digest = hashlib.sha256(encoded).hexdigest()
    root = Path(log_dir) / "acceptance"
    write_once(root / "snapshots" / f"{digest}.json", encoded + b"\n")
    reference = canonical_json(
        {
            "schema": SCHEMA_VERSION,
            "snapshot_digest": digest,
            "execution_id": execution_id,
        }
    )
    write_once(root / "accepted.json", reference + b"\n")
    return _snapshot_from_payload(payload, digest)


def bind_review_package(
    log_dir: Path, snapshot: AcceptanceSnapshot, *, replace_existing: bool = False
) -> bool:
    """Bind an already verified review package to its Criteria Satisfaction Record."""
    root = Path(log_dir)
    manifest_path = root / ".runtime" / "triage-prep" / "manifest.json"
    if not manifest_path.exists():
        return False
    try:
        manifest_bytes = manifest_path.read_bytes()
        manifest = json.loads(manifest_bytes)
        if manifest.get("status") != "ready":
            raise ValueError("review package manifest is not ready")
        _validate_manifest_identity(manifest, snapshot)
        briefing_path = Path(manifest["briefing_path"])
        briefing_bytes = briefing_path.read_bytes()
    except (KeyError, OSError, TypeError, ValueError, json.JSONDecodeError) as exc:
        raise AcceptanceLedgerError(f"cannot bind review package: {exc}") from exc
    binding = canonical_json(
        {
            "schema": SCHEMA_VERSION,
            "snapshot_digest": snapshot.digest,
            "manifest_sha256": hashlib.sha256(manifest_bytes).hexdigest(),
            "briefing_sha256": hashlib.sha256(briefing_bytes).hexdigest(),
        }
    )
    path = root / "acceptance" / "review-package.json"
    if replace_existing:
        # The caller holds the Ticket publication lock; acceptance stays write-once.
        accepted = read_acceptance(root)
        if accepted.snapshot != snapshot:
            recorded = (
                _snapshot_mapping(accepted.snapshot)
                if accepted.snapshot is not None
                else {"snapshot": None}
            )
            actual = (
                _snapshot_mapping(snapshot)
                if accepted.snapshot is not None
                else {"snapshot": _snapshot_mapping(snapshot)}
            )
            raise AcceptanceLedgerError(
                "cannot rebind a different Criteria Satisfaction Record: "
                + format_differences(recorded, actual)
            )
        atomic_replace_bytes(path, binding + b"\n")
    else:
        write_once(path, binding + b"\n")
    return True


def validate_review_package_binding(log_dir: Path, snapshot: AcceptanceSnapshot) -> None:
    """Verify a present package binding still names unchanged artifacts."""
    root = Path(log_dir)
    binding_path = root / "acceptance" / "review-package.json"
    if not binding_path.exists():
        return
    manifest_path = root / ".runtime" / "triage-prep" / "manifest.json"
    try:
        binding = json.loads(binding_path.read_text(encoding="utf-8"))
        manifest_bytes = manifest_path.read_bytes()
        manifest = json.loads(manifest_bytes)
        _validate_manifest_identity(manifest, snapshot)
        briefing_bytes = Path(manifest["briefing_path"]).read_bytes()
        actual = {
            "snapshot_digest": snapshot.digest,
            "manifest_sha256": hashlib.sha256(manifest_bytes).hexdigest(),
            "briefing_sha256": hashlib.sha256(briefing_bytes).hexdigest(),
        }
        if any(binding.get(key) != value for key, value in actual.items()):
            raise ValueError(
                "bound review artifacts changed: "
                + format_differences(
                    {key: binding.get(key) for key in actual},
                    actual,
                )
            )
    except (KeyError, OSError, TypeError, ValueError, json.JSONDecodeError) as exc:
        raise AcceptanceLedgerError(f"invalid review package binding: {exc}") from exc


def _validate_manifest_identity(manifest: Mapping[str, Any], snapshot: AcceptanceSnapshot) -> None:
    manifest_generation = manifest.get("ticket_generation")
    snapshot_generation = snapshot.ticket_identity.get("generation")
    if not isinstance(manifest_generation, str) or manifest_generation != snapshot_generation:
        raise ValueError(
            "review package names a different Ticket generation: "
            + format_differences(
                {"ticket_generation": snapshot_generation},
                {"ticket_generation": manifest_generation},
            )
        )
    heads = {"outer": manifest.get("head_sha")}
    if "project_head_sha" in manifest:
        heads["project"] = manifest.get("project_head_sha")
    if _participant_heads(heads) != snapshot.participant_heads:
        raise ValueError(
            "review package heads disagree with the Criteria Satisfaction Record: "
            + format_differences(snapshot.participant_heads, _participant_heads(heads))
        )


def read_acceptance(log_dir: Path) -> AcceptanceReadResult:
    """Read and integrity-check the Criteria Satisfaction Record for one Ticket."""
    root = Path(log_dir) / "acceptance"
    reference_path = root / "accepted.json"
    if not reference_path.exists():
        return AcceptanceReadResult(
            "unavailable", reason="Criteria Satisfaction Record is unavailable"
        )
    try:
        reference = json.loads(reference_path.read_text(encoding="utf-8"))
        digest = reference["snapshot_digest"]
        if not isinstance(digest, str) or len(digest) != 64:
            raise ValueError("accepted reference has an invalid digest")
        payload = json.loads((root / "snapshots" / f"{digest}.json").read_text(encoding="utf-8"))
        actual = hashlib.sha256(canonical_json(payload)).hexdigest()
        if actual != digest:
            raise ValueError("Criteria Satisfaction Record digest mismatch")
        snapshot = _snapshot_from_payload(payload, digest)
        if not _snapshot_report_effective(Path(log_dir), snapshot):
            return AcceptanceReadResult(
                "unavailable",
                reason="report submission is uncommitted or invalidated; submit a new report",
            )
        return AcceptanceReadResult("accepted", snapshot)
    except (
        AcceptanceLedgerError,
        KeyError,
        OSError,
        TypeError,
        ValueError,
        json.JSONDecodeError,
    ) as exc:
        return AcceptanceReadResult("corrupt", reason=str(exc))


def _snapshot_report_effective(log_dir: Path, snapshot: AcceptanceSnapshot) -> bool:
    from .paths import existing_ticket_runtime_file
    from .report_submission import ID_KEY, KEY, effective_met

    report = snapshot.criteria.get(KEY)
    if report is None or report.get("met") is not True:
        return True
    detail = report.get("detail", {})
    if not effective_met(log_dir, True, detail, identity=snapshot.ticket_identity):
        return False
    state = DevelopmentState.load(existing_ticket_runtime_file(log_dir, "booley_state.json"))
    rows = [
        row
        for row in current_evidence_records(log_dir, state, snapshot.ticket_identity)
        if row["criterion"] == KEY
    ]
    if rows and not rows[-1]["met"]:
        return False
    if detail.get(ID_KEY) and KEY in state.criteria:
        project_report_state(state, log_dir, identity=snapshot.ticket_identity)
        return state.criteria[KEY].met
    return not detail.get(ID_KEY)


def _report_identity(log_dir: Path, state: DevelopmentState) -> dict[str, Any]:
    if log_dir.parent.name != "logs" or not state.strict_criteria:
        return {}
    from .io import TicketIO

    return TicketIO(log_dir.parent.parent).load_basis(log_dir.name).ticket_identity()


def project_report_state(
    state: DevelopmentState,
    log_dir: Path | None = None,
    *,
    identity: Mapping[str, Any] | None = None,
) -> None:
    """Fence the mutable report with the latest validated current observation."""
    from .report_submission import KEY, effective_met, state_log_dir

    root = log_dir or state_log_dir(state)
    entry = state.criteria.get(KEY)
    if root is None or entry is None:
        return
    current = dict(identity) if identity is not None else _report_identity(root, state)
    rows = [
        row for row in current_evidence_records(root, state, current) if row["criterion"] == KEY
    ]
    if rows:
        latest = rows[-1]
        if not latest["met"]:
            entry.met = False
        elif entry.met and any(
            latest[key] != value
            for key, value in {
                "detail": entry.detail,
                "mandatory": entry.mandatory,
                "params": entry.params,
            }.items()
        ):
            raise AcceptanceLedgerError(
                "current report observation differs from mutable projection"
            )
    elif current and entry.detail.get("report_submission_id"):
        entry.met = False
    entry.met = effective_met(root, entry.met, entry.detail, identity=current)


def project_report_mapping(
    state: Mapping[str, Any], log_dir: Path, *, identity: Mapping[str, Any] | None = None
) -> dict[str, Any]:
    """Project report authority without altering unrelated mutable state fields."""
    from booley.core.boundary import require_dict

    # The state module's boundary parser for this field; reused so the report
    # projection accepts exactly what a state load accepts.
    from booley.criteria.state import (
        CriterionEntry,
        _acceptance_transactions,  # pyright: ignore[reportPrivateUsage]
    )

    criteria = dict(require_dict(state.get("criteria", {}), field="criteria"))
    key = "_report_submitted"
    if key not in criteria:
        return dict(state)
    row = dict(require_dict(criteria[key], field=key))
    from booley.core.boundary import require_bool

    require_bool(row, "met", field="report met")
    require_bool(row, "mandatory", default=True, field="report mandatory")
    require_dict(row.get("detail", {}), field="report detail")
    require_dict(row.get("params", {}), field="report params")
    projection = DevelopmentState(
        strict_criteria=require_bool(state, "strict_criteria"),
        criteria={key: CriterionEntry.from_dict(row)},
        acceptance_transactions=_acceptance_transactions(state.get("acceptance_transactions", [])),
    )
    project_report_state(projection, log_dir, identity=identity)
    criteria[key] = {**row, "met": projection.criteria[key].met}
    return {**state, "criteria": criteria}


def submitted_report(log_dir: Path, *, identity: Mapping[str, Any] | None = None) -> Path | None:
    """Expose report bytes only after validated current evidence and completion."""
    from .paths import existing_ticket_runtime_file
    from .report_submission import KEY, read_receipt

    state = DevelopmentState.load(existing_ticket_runtime_file(log_dir, "booley_state.json"))
    project_report_state(state, log_dir, identity=identity)
    entry = state.criteria.get(KEY)
    if entry is None:
        return None if read_receipt(log_dir) else log_dir / "REPORT.md"
    return log_dir / "REPORT.md" if entry.met else None


def effective_state_bytes(
    log_dir: Path, content: bytes, *, identity: Mapping[str, Any] | None = None
) -> bytes:
    from booley.core.boundary import require_dict

    state = require_dict(json.loads(content), field="report state")
    return canonical_json(project_report_mapping(state, log_dir, identity=identity)) + b"\n"
