"""Durable parent linkage for supervised Simulation Campaign child claims."""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from pathlib import Path

from booley.flows.sim.campaign_durability import durable_create
from booley.runtime.execution_records import (
    PROTOCOL_VERSION,
    ExecutionId,
    atomic_write_json,
    execution_paths,
    read_json,
    request_cancellation,
)
from booley.runtime.execution_recovery import recover_execution
from booley.runtime.project_dir import resolve_checkout_project_dir
from booley.runtime.timefmt import utc_now_rfc3339

from .codec import (
    SimulationCampaignIntegrityError,
    canonical_json_bytes,
    decode_simulation_attempt,
)
from .model import SimulationCampaignManifest
from .planning import manifest_digest
from .store import CampaignStore

_ENTRY_SCHEMA = "booley.simulation-campaign-child-entry/v1"
_CONTEXT_SCHEMA = "booley.supervised-child-context/v1"
_RETIREMENT_SCHEMA = "booley.simulation-campaign-child-retirement/v1"
_DIGEST_RE = re.compile(r"sha256:[0-9a-f]{64}")
_ENTRY_FIELDS = {
    "$schema",
    "child_execution_id",
    "parent_execution_id",
    "campaign_id",
    "manifest_path",
    "manifest_sha256",
    "work_item_id",
    "attempt_id",
    "attempt_ordinal",
    "attempt_relative_path",
    "runtime_context_sha256",
}
_RETIREMENT_FIELDS = {
    "$schema",
    "child_execution_id",
    "entry_sha256",
    "execution_terminal_sha256",
    "lease_id",
    "token_absent",
    "terminal_cause",
}


@dataclass(frozen=True, slots=True)
class PreparedChild:
    execution_id: ExecutionId
    entry_sha256: str
    project_entry: Path
    campaign_entry: Path
    context_path: Path


@dataclass(frozen=True, slots=True)
class _ChildIdentity:
    execution_id: ExecutionId
    parent_execution_id: str
    work_item_id: str
    attempt_id: str
    attempt_ordinal: int
    attempt_directory: Path


class ChildExecutionRegistry:
    """Publish and recover exact child execution linkage for one campaign."""

    def __init__(
        self,
        store: CampaignStore,
        manifest: SimulationCampaignManifest,
        project_root: Path,
    ) -> None:
        self._store = store
        self._manifest = manifest
        self._checkout_root = project_root
        self._campaign_root = store.root / "child-executions"

    @property
    def _project_data(self) -> Path:
        return resolve_checkout_project_dir(self._checkout_root)

    @property
    def project_data(self) -> Path:
        return self._project_data

    @property
    def _project_root(self) -> Path:
        return self._project_data / ".runtime" / "campaign-child-executions"

    def prepare(
        self,
        execution_id: ExecutionId,
        *,
        work_item_id: str,
        attempt_id: str,
        attempt_ordinal: int,
        attempt_directory: Path,
        parent_execution_id: str,
    ) -> PreparedChild:
        identity = _ChildIdentity(
            execution_id,
            parent_execution_id,
            work_item_id,
            attempt_id,
            attempt_ordinal,
            attempt_directory,
        )
        context = self._context(identity)
        context_raw = canonical_json_bytes(context)
        entry = self._entry(identity, _sha(context_raw))
        entry_raw = canonical_json_bytes(entry)
        project_entry = self._project_root / "entries" / f"{execution_id}.json"
        campaign_entry = self._campaign_root / "entries" / f"{execution_id}.json"
        _publish_or_verify(project_entry, entry_raw)
        _publish_or_verify(campaign_entry, entry_raw)
        paths = execution_paths(execution_id, project_dir=self._project_data)
        _publish_or_verify(paths.context, context_raw)
        if read_json(paths.record) is None:
            atomic_write_json(paths.record, _waiting_record())
        return PreparedChild(
            execution_id,
            _sha(entry_raw),
            project_entry,
            campaign_entry,
            paths.context,
        )

    def mark_terminal(
        self,
        prepared: PreparedChild,
        terminal_cause: str,
    ) -> None:
        paths = execution_paths(prepared.execution_id, project_dir=self._project_data)
        terminal = read_json(paths.record)
        if terminal is None:
            raise SimulationCampaignIntegrityError("child execution record disappeared")
        if terminal.get("state") == "waiting" and terminal.get("leader") is None:
            atomic_write_json(paths.record, _terminal_record(terminal_cause))
            return
        if terminal.get("state") != "terminal" or terminal.get("tree_terminal") is not True:
            raise SimulationCampaignIntegrityError(
                "child execution lacks process-tree terminal proof"
            )

    def retire(
        self,
        prepared: PreparedChild,
        *,
        lease_id: str | None,
        terminal_cause: str,
        token_absent: bool,
    ) -> None:
        if not token_absent:
            raise SimulationCampaignIntegrityError(
                "cannot retire a child execution while its token remains"
            )
        paths = execution_paths(prepared.execution_id, project_dir=self._project_data)
        terminal = read_json(paths.record)
        if terminal is None or terminal.get("tree_terminal") is not True:
            raise SimulationCampaignIntegrityError(
                "cannot retire a child execution before tree-terminal proof"
            )
        assert terminal is not None
        terminal_raw = canonical_json_bytes(terminal)
        retirement = {
            "$schema": _RETIREMENT_SCHEMA,
            "child_execution_id": str(prepared.execution_id),
            "entry_sha256": prepared.entry_sha256,
            "execution_terminal_sha256": _sha(terminal_raw),
            "lease_id": lease_id,
            "token_absent": token_absent,
            "terminal_cause": terminal_cause,
        }
        raw = canonical_json_bytes(retirement)
        project = self._project_root / "retired" / f"{prepared.execution_id}.json"
        campaign = self._campaign_root / "retired" / f"{prepared.execution_id}.json"
        _publish_or_verify(project, raw)
        _publish_or_verify(campaign, raw)

    def is_terminal(self, execution_id: ExecutionId) -> bool:
        paths = execution_paths(execution_id, project_dir=self._project_data)
        record = read_json(paths.record)
        return bool(
            record is not None
            and record.get("state") == "terminal"
            and record.get("tree_terminal") is True
        )

    def cancel(self, execution_id: ExecutionId) -> bool:
        paths = execution_paths(execution_id, project_dir=self._project_data)
        request_cancellation(paths, reason="campaign_cancelled")
        return recover_execution(execution_id, project_dir=self._project_data)

    def recover_unretired(self, slot_store: object | None) -> None:
        """Finish exact child cleanup before work-item attempt recovery."""
        entries = self._project_root / "entries"
        expected_manifest = str(self._store.manifest_path.resolve(strict=True))
        expected_digest = manifest_digest(self._manifest)
        recovered_ids: set[str] = set()
        paths = sorted(entries.glob("*.json")) if entries.is_dir() else []
        for path in paths:
            raw = path.read_bytes()
            entry = read_json(path)
            if entry is None:
                raise SimulationCampaignIntegrityError(f"invalid child entry: {path}")
            _validate_entry(path, entry)
            if (
                entry.get("manifest_path") != expected_manifest
                or entry.get("manifest_sha256") != expected_digest
            ):
                continue
            self._validate_entry_identity(entry, _sha(raw))
            self._recover_entry(path, raw, entry, slot_store)
            recovered_ids.add(path.name)
        self._verify_campaign_entries(recovered_ids, expected_manifest, expected_digest)

    def _verify_campaign_entries(
        self, recovered: set[str], manifest_path: str, manifest_sha256: str
    ) -> None:
        entries = self._campaign_root / "entries"
        if not entries.is_dir():
            return
        for path in sorted(entries.glob("*.json")):
            document = read_json(path)
            if document is None:
                raise SimulationCampaignIntegrityError(f"invalid campaign child entry: {path}")
            if (
                document.get("manifest_path") == manifest_path
                and document.get("manifest_sha256") == manifest_sha256
                and path.name not in recovered
            ):
                raise SimulationCampaignIntegrityError(
                    "campaign child entry has no Project-local authority"
                )

    def _validate_entry_identity(self, entry: dict, entry_sha256: str) -> None:
        if entry["campaign_id"] != self._manifest.document["campaign_id"]:
            raise SimulationCampaignIntegrityError("child entry campaign identity is invalid")
        try:
            ExecutionId(entry["parent_execution_id"])
        except ValueError as exc:
            raise SimulationCampaignIntegrityError(
                "child entry parent execution identity is invalid"
            ) from exc
        relative = _safe_relative_path(entry["attempt_relative_path"])
        work_item_id = entry["work_item_id"]
        expected = (
            self._store.work_item_directory(work_item_id)
            / "attempts"
            / f"{entry['attempt_ordinal']:04d}-{entry['attempt_id']}"
        )
        if self._store.root / relative != expected:
            raise SimulationCampaignIntegrityError(
                "child entry attempt path disagrees with its work-item identity"
            )
        attempt_path = expected / "attempt.json"
        if attempt_path.exists() or attempt_path.is_symlink():
            self._validate_bound_attempt(attempt_path, entry, entry_sha256)

    @staticmethod
    def _validate_bound_attempt(
        path: Path, entry: dict, entry_sha256: str
    ) -> None:
        if path.is_symlink() or not path.is_file():
            raise SimulationCampaignIntegrityError("child-bound attempt is not regular")
        try:
            raw = path.read_bytes()
        except OSError as exc:
            raise SimulationCampaignIntegrityError("child-bound attempt is unreadable") from exc
        attempt = decode_simulation_attempt(raw).document
        expected = {
            "child_execution_id": entry["child_execution_id"],
            "child_entry_sha256": entry_sha256,
            "campaign_id": entry["campaign_id"],
            "manifest_sha256": entry["manifest_sha256"],
            "work_item_id": entry["work_item_id"],
            "attempt_id": entry["attempt_id"],
            "attempt_ordinal": entry["attempt_ordinal"],
        }
        if any(attempt.get(field) != value for field, value in expected.items()):
            raise SimulationCampaignIntegrityError(
                "child entry disagrees with its exact Simulation Attempt"
            )

    def _recover_entry(self, path, raw, entry, slot_store) -> None:
        execution_id = ExecutionId(entry.get("child_execution_id"))
        campaign_entry = self._campaign_root / "entries" / path.name
        _publish_or_verify(campaign_entry, raw)
        project_retired = self._project_root / "retired" / path.name
        campaign_retired = self._campaign_root / "retired" / path.name
        if project_retired.exists():
            retired_raw = project_retired.read_bytes()
            terminal = read_json(
                execution_paths(execution_id, project_dir=self._project_data).record
            )
            if terminal is None or terminal.get("tree_terminal") is not True:
                raise SimulationCampaignIntegrityError(
                    "child retirement lacks its terminal execution record"
                )
            _validate_retirement(
                retired_raw,
                execution_id,
                _sha(raw),
                _sha(canonical_json_bytes(terminal)),
            )
            self._assert_token_absent(slot_store, execution_id)
            _publish_or_verify(campaign_retired, retired_raw)
            return
        prepared = PreparedChild(
            execution_id,
            _sha(raw),
            path,
            campaign_entry,
            execution_paths(execution_id, project_dir=self._project_data).context,
        )
        self._recover_execution(prepared, entry)
        lease_id = self._release_matching_token(slot_store, execution_id)
        if slot_store is None:
            self._assert_no_unmanaged_token(execution_id)
        record = read_json(execution_paths(execution_id, project_dir=self._project_data).record)
        cause = str(record.get("terminal_cause", "recovered")) if record else "recovered"
        self.retire(
            prepared, lease_id=lease_id, terminal_cause=cause, token_absent=True
        )

    def _recover_execution(self, prepared: PreparedChild, entry: dict) -> None:
        identity = _ChildIdentity(
            prepared.execution_id,
            entry["parent_execution_id"],
            entry["work_item_id"],
            entry["attempt_id"],
            entry["attempt_ordinal"],
            self._store.root / entry["attempt_relative_path"],
        )
        context = self._context(identity)
        raw = canonical_json_bytes(context)
        if _sha(raw) != entry.get("runtime_context_sha256"):
            raise SimulationCampaignIntegrityError("child context digest disagrees with entry")
        _publish_or_verify(prepared.context_path, raw)
        paths = execution_paths(prepared.execution_id, project_dir=self._project_data)
        if read_json(paths.record) is None:
            atomic_write_json(paths.record, _waiting_record())
        if not self.is_terminal(prepared.execution_id) and not self.cancel(
            prepared.execution_id
        ):
            raise SimulationCampaignIntegrityError(
                f"child execution recovery is incomplete: {prepared.execution_id}"
            )

    @staticmethod
    def _release_matching_token(slot_store, execution_id: ExecutionId) -> str | None:
        if slot_store is None:
            return None
        from booley.runtime.job_slots import CLASS_HEAVY

        holders, waiters = slot_store.snapshot(CLASS_HEAVY)
        matches = [token for token in (*holders, *waiters) if token.execution_id == execution_id]
        if len(matches) > 1:
            raise SimulationCampaignIntegrityError("duplicate child slot claims")
        if not matches:
            return None
        token = matches[0]
        slot_store.release(token)
        if token.path.exists():
            raise SimulationCampaignIntegrityError("recovered child token remains present")
        return token.lease_id

    def _assert_token_absent(self, slot_store, execution_id: ExecutionId) -> None:
        if slot_store is None:
            self._assert_no_unmanaged_token(execution_id)
            return
        from booley.runtime.job_slots import CLASS_HEAVY

        holders, waiters = slot_store.snapshot(CLASS_HEAVY)
        if any(token.execution_id == execution_id for token in (*holders, *waiters)):
            raise SimulationCampaignIntegrityError(
                "child retirement claims token absence while its slot claim remains"
            )

    def _assert_no_unmanaged_token(self, execution_id: ExecutionId) -> None:
        slots = self._project_data / "runtime" / "jobs" / "slots"
        for path in slots.glob("*/*.json"):
            payload = read_json(path)
            if payload is not None and payload.get("execution_id") == execution_id:
                raise SimulationCampaignIntegrityError(
                    "child token presence cannot be reconciled without managed admission"
                )

    def _context(self, identity: _ChildIdentity):
        return {
            "$schema": _CONTEXT_SCHEMA,
            "child_execution_id": str(identity.execution_id),
            "parent_execution_id": identity.parent_execution_id,
            "campaign_id": self._manifest.document["campaign_id"],
            "manifest_sha256": manifest_digest(self._manifest),
            "work_item_id": identity.work_item_id,
            "attempt_id": identity.attempt_id,
            "attempt_ordinal": identity.attempt_ordinal,
        }

    def _entry(self, identity: _ChildIdentity, context_digest: str):
        relative = identity.attempt_directory.relative_to(self._store.root).as_posix()
        return {
            "$schema": _ENTRY_SCHEMA,
            "child_execution_id": str(identity.execution_id),
            "parent_execution_id": identity.parent_execution_id,
            "campaign_id": self._manifest.document["campaign_id"],
            "manifest_path": str(self._store.manifest_path.resolve(strict=True)),
            "manifest_sha256": manifest_digest(self._manifest),
            "work_item_id": identity.work_item_id,
            "attempt_id": identity.attempt_id,
            "attempt_ordinal": identity.attempt_ordinal,
            "attempt_relative_path": relative,
            "runtime_context_sha256": context_digest,
        }


def _waiting_record() -> dict[str, object]:
    return {
        "schema_version": PROTOCOL_VERSION,
        "state": "waiting",
        "runtime_identity": None,
        "supervisor": None,
        "leader": None,
        "exit_code": None,
        "tree_terminal": False,
        "terminal_cause": None,
        "updated_at": utc_now_rfc3339(),
    }


def _terminal_record(cause: str) -> dict[str, object]:
    return {
        **_waiting_record(),
        "state": "terminal",
        "exit_code": 0 if cause == "completed" else 125,
        "tree_terminal": True,
        "terminal_cause": cause,
        "updated_at": utc_now_rfc3339(),
    }


def _sha(raw: bytes) -> str:
    return "sha256:" + hashlib.sha256(raw).hexdigest()


def _validate_entry(path: Path, entry: dict) -> None:
    if set(entry) != _ENTRY_FIELDS or entry.get("$schema") != _ENTRY_SCHEMA:
        raise SimulationCampaignIntegrityError(f"child entry has an invalid schema: {path}")
    try:
        child_id = ExecutionId(entry.get("child_execution_id"))
    except ValueError as exc:
        raise SimulationCampaignIntegrityError(f"child entry has an invalid id: {path}") from exc
    if path.stem != child_id:
        raise SimulationCampaignIntegrityError("child entry filename disagrees with its identity")
    ordinal = entry.get("attempt_ordinal")
    if not isinstance(ordinal, int) or isinstance(ordinal, bool) or ordinal < 1:
        raise SimulationCampaignIntegrityError("child entry attempt ordinal is invalid")
    for field in ("manifest_sha256", "runtime_context_sha256"):
        if not isinstance(entry.get(field), str) or not _DIGEST_RE.fullmatch(entry[field]):
            raise SimulationCampaignIntegrityError(f"child entry {field} is invalid")
    for field in (
        "parent_execution_id",
        "campaign_id",
        "manifest_path",
        "work_item_id",
        "attempt_id",
        "attempt_relative_path",
    ):
        value = entry.get(field)
        if not isinstance(value, str) or not value or len(value) > 4096:
            raise SimulationCampaignIntegrityError(f"child entry {field} is invalid")


def _safe_relative_path(value: str) -> Path:
    if "\\" in value:
        raise SimulationCampaignIntegrityError("child attempt path is not canonical")
    path = Path(value)
    if path.is_absolute() or any(part in {"", ".", ".."} for part in path.parts):
        raise SimulationCampaignIntegrityError("child attempt path is not contained")
    return path


def _validate_retirement(
    raw: bytes,
    execution_id: ExecutionId,
    entry_digest: str,
    terminal_digest: str,
) -> None:
    try:
        document = json.loads(raw)
    except (UnicodeDecodeError, ValueError) as exc:
        raise SimulationCampaignIntegrityError("child retirement is invalid JSON") from exc
    if (
        not isinstance(document, dict)
        or set(document) != _RETIREMENT_FIELDS
        or document.get("$schema") != _RETIREMENT_SCHEMA
        or document.get("child_execution_id") != execution_id
        or document.get("entry_sha256") != entry_digest
        or document.get("execution_terminal_sha256") != terminal_digest
        or document.get("token_absent") is not True
        or not isinstance(document.get("execution_terminal_sha256"), str)
        or not _DIGEST_RE.fullmatch(document["execution_terminal_sha256"])
        or not isinstance(document.get("terminal_cause"), str)
        or not document["terminal_cause"]
        or (
            document.get("lease_id") is not None
            and not isinstance(document.get("lease_id"), str)
        )
    ):
        detail = (
            "child retirement terminal digest is invalid"
            if isinstance(document, dict)
            and document.get("execution_terminal_sha256") != terminal_digest
            else "child retirement identity is invalid"
        )
        raise SimulationCampaignIntegrityError(detail)


def _publish_or_verify(path: Path, raw: bytes) -> None:
    if path.is_symlink():
        raise SimulationCampaignIntegrityError(f"child protocol path is a link: {path}")
    if path.exists():
        try:
            current = path.read_bytes()
        except OSError as exc:
            raise SimulationCampaignIntegrityError(
                f"child protocol record cannot be read: {path}"
            ) from exc
        if current != raw:
            raise SimulationCampaignIntegrityError(
                f"child protocol record disagrees with durable identity: {path}"
            )
        return
    try:
        durable_create(path, raw)
    except FileExistsError:
        if path.read_bytes() != raw:
            raise SimulationCampaignIntegrityError(
                f"child protocol publication raced with different bytes: {path}"
            ) from None


__all__ = ["ChildExecutionRegistry", "PreparedChild"]
