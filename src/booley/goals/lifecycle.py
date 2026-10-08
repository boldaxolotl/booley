"""Worktree-bound immutable lifecycle requests and saved retry results."""

from __future__ import annotations

import json
from collections.abc import Generator
from contextlib import contextmanager, suppress
from dataclasses import dataclass
from hashlib import sha256
from pathlib import Path
from typing import Any

from booley.core.boundary import (
    require_bool_value,
    require_dict,
    require_list,
    require_str_value,
    require_uuid4,
)
from booley.goals.checkout import GoalCheckout
from booley.goals.model import GoalRecord, WorktreeIdentity
from booley.goals.paths import validate_goal_id
from booley.goals.proposals import digest, encode
from booley.goals.store import GoalStore, RecordLock
from booley.runtime.atomic_files import WriteOnceConflictError, atomic_write_once


class LifecycleError(ValueError):
    """The requested lifecycle action cannot safely proceed."""


@dataclass(frozen=True)
class LifecycleRequest:
    """Stable public IDs guard retries; worktree identity remains authority."""

    work_dir: Path
    record_id: str
    operation_id: str
    summary: str = ""
    abandon: bool = False
    instruction_quote: str = ""
    explain_html: bool = False
    session_key: str | None = None

    def payload(self, record: GoalRecord) -> dict[str, Any]:
        """Canonical worktree identity admits mount aliases without session ownership."""
        return {
            "record_id": self.record_id,
            "operation_id": self.operation_id,
            "worktree": record.worktree.to_json(),
            "summary": self.summary,
            "abandon": self.abandon,
            "instruction_quote": self.instruction_quote,
            "explain_html": self.explain_html,
        }


@dataclass(frozen=True)
class LifecycleOperation:
    """A bound, owned operation whose payload never changes."""

    store: GoalStore
    lock: RecordLock
    request: LifecycleRequest
    root: Path

    @property
    def directory(self) -> Path:
        """Local immutable artifacts remain attached to the expected record."""
        directory = self.lock.record_dir / "operations" / self.request.operation_id
        if not directory.resolve().is_relative_to(self.lock.record_dir.resolve()) or any(
            path.is_symlink() or getattr(path, "is_junction", lambda: False)()
            for path in (directory, directory.parent)
        ):
            raise LifecycleError("lifecycle operation storage is linked/outside its record")
        return directory

    def saved_result(self) -> dict[str, Any] | None:
        """Return an already-durable result, even after replacement entry."""
        path = self.directory / "result.json"
        if not path.exists() and not self.directory.joinpath("result.authority.json").exists():
            return None
        try:
            result = self.read_sealed("result")
            _validate_result(result, self)
            if result["status"] == "finished":
                _validate_finished_artifacts(result, self)
            return result
        except (OSError, ValueError, KeyError, TypeError) as exc:
            raise LifecycleError(f"invalid saved lifecycle result: {exc}") from exc

    def save_result(self, result: dict[str, Any]) -> dict[str, Any]:
        """The same result bytes are retained across response loss."""
        _validate_result(result, self)
        self.write_sealed("result", result)
        return result

    def write_sealed(self, name: str, value: dict[str, Any]) -> None:
        """Publish complete immutable authority first, then its recoverable artifact."""
        payload = self.request.payload(self.store.load(self.request.record_id))
        authority = {
            "request_digest": digest(payload),
            "digest": digest(value),
            "value": value,
            "record_directory": {
                "path": str(self.lock.record_dir),
                "identity": {"record_id": self.request.record_id, "worktree": payload["worktree"]},
            },
        }
        try:
            atomic_write_once(self.directory / (name + ".authority.json"), encode(authority))
            atomic_write_once(self.directory / (name + ".json"), encode(value))
        except WriteOnceConflictError as exc:
            raise LifecycleError(
                f"immutable {name} conflicts with retained authority; use a fresh operation_id"
            ) from exc

    def read_sealed(self, name: str) -> dict[str, Any]:
        """Verify all persisted fields against the independently durable authority."""
        try:
            authority = require_dict(
                json.loads((self.directory / (name + ".authority.json")).read_bytes()),
                field=name + " authority",
            )
            value = require_dict(authority.get("value"), field=name)
            payload = self.request.payload(self.store.load(self.request.record_id))
            if (
                set(authority) != {"request_digest", "digest", "value", "record_directory"}
                or authority["request_digest"] != digest(payload)
                or authority["digest"] != digest(value)
            ):
                raise ValueError("immutable authority differs from the operation")
            _record_directory(
                authority, self.lock.record_dir, self.store.load(self.request.record_id)
            )
            path = self.directory / (name + ".json")
            if path.exists() and json.loads(path.read_bytes()) != value:
                raise ValueError("persisted artifact differs from durable authority")
            return value
        except (OSError, ValueError, TypeError) as exc:
            raise LifecycleError(f"invalid frozen {name}: {exc}") from exc

    def sealed_directory(self, name: str) -> Path:
        """The original logical storage prefix, for byte-identical restored responses."""
        authority = require_dict(
            json.loads((self.directory / (name + ".authority.json")).read_bytes()),
            field=name + " authority",
        )
        original = _record_directory(
            authority, self.lock.record_dir, self.store.load(self.request.record_id)
        )
        return original / "operations" / self.request.operation_id

    def save_recovered_result(self, result: dict[str, Any]) -> dict[str, Any]:
        """Attribute another operation's proven completion to this bound recovery request."""
        return self.save_result(
            {
                **result,
                "operation_id": self.request.operation_id,
                "recovered_operation_id": result["operation_id"],
            }
        )


@contextmanager
def bind_operation(store: GoalStore, request: LifecycleRequest) -> Generator[LifecycleOperation]:
    """Bind before recovery/validation under worktree then record locks."""
    if not request.work_dir.is_absolute():
        raise LifecycleError("work_dir must be an absolute Git worktree path")
    validate_goal_id(request.record_id)
    require_uuid4(request.operation_id, field="operation_id")
    found = GoalCheckout(request.work_dir).containing_repository()
    if found is None:
        raise LifecycleError("work_dir must identify a Git worktree")
    root = found[0]
    identity = store.identify_worktree(root)
    if identity is None:
        raise LifecycleError("worktree has no recorded identity")
    with store.worktree_lock(identity), store.record_lock(request.record_id) as lock:
        yield bind_locked(store, lock, request, root, identity)


def bind_locked(
    store: GoalStore,
    lock: RecordLock,
    request: LifecycleRequest,
    root: Path,
    identity: WorktreeIdentity,
) -> LifecycleOperation:
    """Bind a request when a direct human invocation already holds both locks."""
    lock.require_owned()
    require_uuid4(request.operation_id, field="operation_id")
    record = store.load(request.record_id)
    if record.worktree != identity:
        raise LifecycleError("expected record belongs to a different worktree")
    operation = LifecycleOperation(store, lock, request, root)
    payload = request.payload(record)
    content = {**payload, "payload_digest": digest(payload)}
    path = operation.directory / "request.json"
    if path.exists():
        if json.loads(path.read_bytes()) != content:
            raise LifecycleError("operation_id conflicts with a different immutable payload")
    else:
        occupant = store.active_for_worktree(root)
        if occupant is None or occupant.id != request.record_id:
            raise LifecycleError("expected record is no longer this worktree's occupant")
        atomic_write_once(path, encode(content))
    _audit_attribution(operation)
    return operation


def _audit_attribution(operation: LifecycleOperation) -> None:
    key = operation.request.session_key
    if key is None:
        return
    # Advisory audit is separate from immutable retry authority and never gates work.
    with suppress(OSError, UnicodeError, ValueError):
        name = sha256(key.encode()).hexdigest() + ".json"
        directory = operation.directory / "attribution"
        if not directory.is_symlink() and not (directory / name).is_symlink():
            with suppress(WriteOnceConflictError):
                atomic_write_once(directory / name, encode({"session_key": key}))


def _validate_result(value: dict[str, Any], operation: LifecycleOperation) -> None:
    """A saved response must have a complete known shape and this operation's IDs."""
    request = operation.request
    common = {"status", "record_id", "operation_id", "message"}
    fields = {
        "finished": {
            "package",
            "summary",
            "history_path",
            "publication_note",
            "running_jobs",
            "html",
        },
        "abandoned": {"instruction_quote", "notes"},
        "revalidation_required": {"reason", "attempt"},
    }
    status = value.get("status")
    if not isinstance(status, str) or status not in fields:
        raise LifecycleError("saved lifecycle result has an invalid status")
    expected = common | fields[status]
    if "recovered_operation_id" in value:
        expected.add("recovered_operation_id")
        require_uuid4(value["recovered_operation_id"], field="recovered_operation_id")
    if (
        set(value) != expected
        or value["record_id"] != request.record_id
        or value["operation_id"] != request.operation_id
    ):
        raise LifecycleError("saved lifecycle result differs from its bound operation")
    for key in expected - {"running_jobs", "notes", "history_path", "publication_note", "html"}:
        if not isinstance(value[key], str):
            raise LifecycleError(f"saved lifecycle result {key} must be text")
    for key in ("history_path", "publication_note", "html"):
        if key in value and value[key] is not None and not isinstance(value[key], str):
            raise LifecycleError(f"saved lifecycle result {key} must be text or null")
    _validate_result_paths(value, operation)
    if status == "abandoned" and (
        value["instruction_quote"] != request.instruction_quote
        or not isinstance(value["notes"], list)
        or not all(isinstance(item, str) for item in require_list(value["notes"], field="notes"))
    ):
        raise LifecycleError("saved abandonment result has invalid instruction/notes")
    if status == "finished" and (
        not isinstance(value["running_jobs"], list)
        or not all(
            isinstance(item, dict)
            for item in require_list(value["running_jobs"], field="running_jobs")
        )
    ):
        raise LifecycleError("saved finish result has invalid Jobs")


def _validate_result_paths(value: dict[str, Any], operation: LifecycleOperation) -> None:
    from booley.runtime.project_dir import contains

    origin = value.get("recovered_operation_id", operation.request.operation_id)
    directory = operation.lock.record_dir / "operations" / origin
    names = (
        {"package": "review-package.json", "summary": "SUMMARY.md", "html": "explanation.html"}
        if value["status"] == "finished"
        else {"attempt": "."}
        if value["status"] == "revalidation_required"
        else {}
    )
    for key, name in names.items():
        if value[key] is None and key == "html":
            continue
        path = Path(value[key])
        for authority_name in ("result.authority.json", "attempt.authority.json"):
            authority = directory / authority_name
            if authority.exists():
                original = _record_directory(
                    require_dict(json.loads(authority.read_bytes()), field="artifact authority"),
                    operation.lock.record_dir,
                    operation.store.load(operation.request.record_id),
                )
                if path.is_relative_to(original):
                    path = operation.lock.record_dir / path.relative_to(original)
                    break
        mapped = contains(path, project_dir=directory)
        if mapped is None or mapped != (directory / name).resolve():
            raise LifecycleError("saved lifecycle result path differs from its bound operation")


def _record_directory(authority: dict[str, Any], current: Path, record: GoalRecord) -> Path:
    binding = require_dict(authority.get("record_directory"), field="record_directory")
    if (
        set(binding) != {"path", "identity"}
        or not isinstance(binding["path"], str)
        or not Path(binding["path"]).is_absolute()
        or Path(binding["path"]).name != record.id
        or current.name != record.id
    ):
        raise LifecycleError("immutable operation directory identity was substituted")
    identity = binding["identity"]
    if isinstance(identity, list):
        identity = require_list(identity, field="legacy directory identity")
        # Legacy storage inode IDs never supersede the bound logical record/request.
        if len(identity) != 2 or any(type(item) is not int or item < 0 for item in identity):
            raise LifecycleError("invalid legacy operation directory identity")
    elif identity != {"record_id": record.id, "worktree": record.worktree.to_json()}:
        raise LifecycleError("operation directory belongs to a different logical record")
    return Path(binding["path"])


def read_bound_request(operation: LifecycleOperation, operation_id: str) -> LifecycleRequest:
    """Decode prior request authority before using its fields for recovery."""
    record = operation.store.load(operation.request.record_id)
    directory = operation.lock.record_dir / "operations" / operation_id
    saved = require_dict(
        json.loads((directory / "request.json").read_bytes()), field="prior lifecycle request"
    )
    payload = {key: value for key, value in saved.items() if key != "payload_digest"}
    if (
        set(saved)
        != {
            "record_id",
            "operation_id",
            "worktree",
            "summary",
            "abandon",
            "instruction_quote",
            "explain_html",
            "payload_digest",
        }
        or payload["record_id"] != record.id
        or payload["operation_id"] != operation_id
        or payload["worktree"] != record.worktree.to_json()
        or saved["payload_digest"] != digest(payload)
    ):
        raise LifecycleError("prior lifecycle request differs from its bound operation")
    require_uuid4(operation_id, field="operation_id")
    return LifecycleRequest(
        operation.root,
        record.id,
        operation_id,
        require_str_value(saved["summary"], field="summary", allow_empty=True),
        require_bool_value(saved["abandon"], field="abandon"),
        require_str_value(saved["instruction_quote"], field="instruction_quote", allow_empty=True),
        require_bool_value(saved["explain_html"], field="explain_html"),
    )


def _validate_finished_artifacts(result: dict[str, Any], operation: LifecycleOperation) -> None:
    from booley.goals.finish_attempt import validate_attempt, validate_local_artifacts
    from booley.goals.finish_presentation import frozen_response

    origin = result.get("recovered_operation_id", operation.request.operation_id)
    prior = LifecycleOperation(
        operation.store, operation.lock, read_bound_request(operation, origin), operation.root
    )
    frozen = prior.read_sealed("attempt")
    validate_attempt(frozen, prior)
    validate_local_artifacts(frozen, prior, required=True)
    expected = {
        "message": frozen_response(prior, frozen)["message"],
        "history_path": frozen["path"] if frozen["skip_publication"] is None else None,
        "publication_note": frozen["skip_publication"],
        "running_jobs": frozen["running_jobs"],
    }
    if any(result[key] != value for key, value in expected.items()):
        raise LifecycleError("saved finished result differs from its frozen completion")
