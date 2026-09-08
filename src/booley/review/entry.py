"""Durable inspection identity, evidence selection and review-operation fencing."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

from booley.core.boundary import require_dict
from booley.runtime.pid import is_pid_alive
from booley.ticket_board.acceptance_ledger import read_acceptance
from booley.ticket_board.paths import ticket_runtime_dir


class ReviewEntryError(ValueError):
    """Review identity or an in-progress operation prevents this action."""


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


def read_entry(log_dir: Path) -> dict[str, Any] | None:
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
    if row.get("disposition") not in {"unaccepted", "accepted"}:
        raise ReviewEntryError("unknown review acceptance disposition")
    generation = row.get("generation")
    if not isinstance(generation, str) or len(generation) != 32 or not generation.isalnum():
        raise ReviewEntryError("invalid review package generation")
    return row


def package_dir(log_dir: Path, row: dict[str, Any]) -> Path:
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
        from booley.harness.job_fence import active_ticket_jobs

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
