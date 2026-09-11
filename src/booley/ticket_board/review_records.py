"""Durable inspection identity, evidence selection and review-operation fencing."""

from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path
from typing import Any, Literal, TypedDict, cast

from booley.core.boundary import require_bool, require_dict, require_int, require_list, require_str
from booley.runtime.pid import is_pid_alive
from booley.ticket_board.acceptance_basis import AcceptanceBasis
from booley.ticket_board.acceptance_ledger import read_acceptance
from booley.ticket_board.paths import ticket_runtime_dir


class ReviewEntryError(ValueError):
    """Review identity or an in-progress operation prevents this action."""


class ReviewInspection(TypedDict):
    """Captured review identity; parsed once when durable records are read."""

    schema: int
    generation: str
    basis_id: str
    basis_receipt: dict[str, Any]
    execution_id: str
    source_status: Literal["blocked", "review"]
    heads: dict[str, str]
    disposition: Literal["unaccepted", "accepted"]
    reason: str
    blocked_reason: str
    state: dict[str, Any]
    created_at: str
    capture_sha: str


def _validate_state(raw: Any) -> None:
    state = require_dict(raw, field="review state")
    criteria = require_dict(state.get("criteria"), field="review criteria")
    for key, value in criteria.items():
        row = require_dict(value, field=f"criterion {key}")
        for flag in ("met", "mandatory"):
            if flag not in row:
                raise ReviewEntryError(f"missing criterion {key}.{flag}")
            require_bool(row, flag, field=f"criterion {key}.{flag}")
        for flag in ("ever_met", "ever_failed", "locked", "stale"):
            if flag in row:
                require_bool(row, flag, field=f"criterion {key}.{flag}")
        for field in ("detail", "params"):
            if field in row:
                require_dict(row[field], field=f"criterion {key}.{field}")
        if row.get("availability", "available") not in {"available", "unavailable"}:
            raise ReviewEntryError(f"invalid criterion {key}.availability")
        if "transition_evidence" in row:
            for evidence in require_list(row["transition_evidence"], field="transition evidence"):
                require_dict(evidence, field="transition evidence entry")


def parse_inspection(value: Any) -> ReviewInspection:
    """Validate persisted inspection fields before any reader projects them."""
    try:
        row = require_dict(value, field="review entry")
        if require_int(row.get("schema"), field="review schema") != 1:
            raise ReviewEntryError("unsupported review schema")
        for key in ("generation", "basis_id", "reason", "created_at", "capture_sha"):
            require_str(row, key)
        for key in ("execution_id", "blocked_reason"):
            if not isinstance(row.get(key), str):
                raise ReviewEntryError(f"review {key} must be a string")
        if row.get("disposition") not in {"unaccepted", "accepted"}:
            raise ReviewEntryError("unknown review acceptance disposition")
        if row.get("source_status") not in {"blocked", "review"}:
            raise ReviewEntryError("invalid review source status")
        if re.fullmatch(r"[0-9a-f]{32}", row["generation"]) is None:
            raise ReviewEntryError("invalid review package generation")
        if re.fullmatch(r"[0-9a-f]{64}", row["capture_sha"]) is None:
            raise ReviewEntryError("invalid review capture digest")
        heads = require_dict(row.get("heads"), field="review heads")
        if "outer" not in heads or set(heads) - {"outer", "project"}:
            raise ReviewEntryError("invalid review participant roles")
        for role in heads:
            require_str(heads, role)
        _validate_receipt(row.get("basis_receipt"), row["basis_id"])
        _validate_state(row.get("state"))
        return cast(ReviewInspection, row)
    except (ValueError, TypeError) as exc:
        raise ReviewEntryError(f"invalid review entry: {exc}") from exc


def _validate_receipt(value: Any, basis_id: str) -> None:
    receipt = require_dict(value, field="review Basis receipt")
    if require_int(receipt.get("schema"), field="receipt schema") != 1:
        raise ReviewEntryError("invalid Basis receipt schema")
    if require_str(receipt, "basis_id") != basis_id:
        raise ReviewEntryError("review receipt names another Basis")
    for key in ("source_sha256", "operation_id"):
        require_str(receipt, key)
    record = require_dict(receipt.get("record"), field="receipt record")
    for key in ("role", "locator", "sha256"):
        require_str(record, key)
    basis = AcceptanceBasis.from_mapping(
        {"schema": receipt["schema"], "participants": receipt.get("participants")}
    )
    if basis.basis_id != basis_id:
        raise ReviewEntryError("review receipt participant identity mismatch")


def digest(value: Any) -> str:
    """Identify a canonical captured input or published entry."""
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


def entry_path(log_dir: Path) -> Path:
    """Return the durable published inspection pointer."""
    return log_dir / "review" / "entry.json"


def operation_path(log_dir: Path) -> Path:
    """Return the recoverable operation record, outside disposable runtime outputs."""
    return log_dir / "review" / "operation.json"


def read_json(path: Path) -> dict[str, Any] | None:
    """Read an optional record, rejecting malformed content."""
    if not path.exists():
        return None
    try:
        return require_dict(json.loads(path.read_text()), field=str(path))
    except (ValueError, TypeError) as exc:
        raise ReviewEntryError(f"invalid review record {path.name}: {exc}") from exc


def read_entry(log_dir: Path) -> ReviewInspection | None:
    """Read an integrity-checked inspection; absence is never implicit acceptance."""
    operation = read_json(operation_path(log_dir))
    if operation and operation.get("phase") in {"publishing", "accepting"}:
        raise ReviewEntryError("review publication is pending; retry the recorded board command")
    envelope = read_json(entry_path(log_dir))
    if envelope is None:
        return None
    row = require_dict(envelope.get("entry"), field="review entry")
    if envelope.get("sha256") != digest(row) or row.get("schema") != 1:
        raise ReviewEntryError("review entry integrity or schema is invalid")
    return parse_inspection(row)


def package_dir(log_dir: Path, row: ReviewInspection) -> Path:
    """Resolve an immutable package generation within this ticket."""
    return ticket_runtime_dir(log_dir) / "review-packages" / row["generation"]


def assert_idle(log_dir: Path) -> None:
    """Fence mutations while another process prepares, runs or publishes review."""
    operation = read_json(operation_path(log_dir))
    if operation is None:
        return
    pid = operation.get("pid")
    if isinstance(pid, int) and is_pid_alive(pid):
        raise ReviewEntryError("ticket has an active review operation; wait for it to finish")
    if operation.get("phase") == "interactive":
        from booley.ticket_board.ticket_jobs import active_ticket_jobs

        try:
            require_str(operation, "token")
        except (TypeError, ValueError) as exc:
            raise ReviewEntryError("interactive review operation identity is invalid") from exc
        if active_ticket_jobs(log_dir):
            raise ReviewEntryError("interactive review Jobs are still active")
    if operation.get("phase") in {"publishing", "accepting"}:
        raise ReviewEntryError(
            "review publication was interrupted; retry the recorded board command"
        )


def criteria_projection(log_dir: Path) -> dict[str, Any] | None:
    """Select immutable accepted or explicitly unaccepted inspection criteria."""
    operation = read_json(operation_path(log_dir))
    if operation and operation.get("phase") in {"publishing", "accepting"}:
        return None
    accepted = read_acceptance(log_dir)
    if accepted.kind == "accepted" and accepted.snapshot is not None:
        return {"criteria": accepted.snapshot.criteria}
    if accepted.kind == "corrupt":
        return None
    entry = read_entry(log_dir)
    if entry is not None and entry["disposition"] == "unaccepted":
        return {"criteria": entry["state"]["criteria"]}
    return None


def require_clean(ctx: Any) -> None:
    """Inspect committed source only; leave dirty work untouched."""
    import subprocess

    repositories = [ctx.worktree]
    if ctx.project_repository is not None:
        repositories.append(ctx.project_repository.worktree)
    for repository in repositories:
        result = subprocess.run(
            ["git", "-C", str(repository), "status", "--porcelain", "--untracked-files=all"],
            capture_output=True,
            text=True,
            check=True,
            timeout=30,
        )
        if result.stdout.strip():
            raise ReviewEntryError(f"commit review source changes first: {repository}")
