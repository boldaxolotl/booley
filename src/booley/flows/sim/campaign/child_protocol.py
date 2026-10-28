"""Per-lane execution identity for parallel Campaign lanes.

Also recovers the child records Booley 0.3.0 wrote.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import stat
import threading
import time
from dataclasses import dataclass
from pathlib import Path

from booley.core.boundary import BoundaryError, require_int
from booley.flows.sim.campaign_durability import durable_create
from booley.runtime.booley_dir import resolve_checkout_booley_dir
from booley.runtime.execution_records import (
    PROTOCOL_VERSION,
    ExecutionId,
    atomic_write_json,
    execution_paths,
    read_json,
    read_json_object,
    request_cancellation,
)
from booley.runtime.execution_recovery import recover_execution
from booley.runtime.regular_file import open_regular_nofollow
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
_PROTOCOL_RECORD_LIMIT = 1024 * 1024
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
    """One parallel lane's execution identity: its ID and context sidecar."""

    execution_id: ExecutionId
    context_path: Path


@dataclass(frozen=True, slots=True)
class _V030Child:
    """A child execution recorded by a Booley 0.3.0 entry record."""

    execution_id: ExecutionId
    entry_sha256: str
    context_path: Path


@dataclass(frozen=True, slots=True)
class _ChildIdentity:
    execution_id: ExecutionId
    parent_execution_id: ExecutionId
    work_item_id: str
    attempt_id: str
    attempt_ordinal: int
    attempt_directory: Path


class ChildExecutionRegistry:
    """Give parallel lanes execution identities; recover 0.3.0 child records."""

    def __init__(
        self,
        store: CampaignStore,
        manifest: SimulationCampaignManifest,
        project_root: Path,
    ) -> None:
        project_data = resolve_checkout_booley_dir(project_root)
        self._configure(store, manifest, project_data, project_data)

    @classmethod
    def from_project_data(
        cls,
        store: CampaignStore,
        manifest: SimulationCampaignManifest,
        project_data: Path,
        *,
        booley_dir: Path | None = None,
    ) -> ChildExecutionRegistry:
        """Bind recovery to an already resolved Booley Directory root.

        *booley_dir* is the validated entry that may be a symlink (#2191); a caller-supplied
        *project_data* is never followed unless the caller proves it is that entry.
        """
        registry = cls.__new__(cls)
        registry._configure(store, manifest, project_data, booley_dir)
        return registry

    def _configure(
        self,
        store: CampaignStore,
        manifest: SimulationCampaignManifest,
        project_data: Path,
        booley_dir: Path | None,
    ) -> None:
        self._store = store
        self._manifest = manifest
        self._project_data_root = project_data
        self._booley_dir = booley_dir
        self._campaign_root = store.root / "child-executions"
        # A lane's mark_terminal, the cancel path (recover_execution) and a
        # 0.3.0 retirement each read-check-write one execution record. Serialize
        # them per execution so those steps never interleave.
        self._record_locks: dict[ExecutionId, threading.Lock] = {}
        self._record_locks_gate = threading.Lock()

    def _record_lock(self, execution_id: ExecutionId) -> threading.Lock:
        with self._record_locks_gate:
            return self._record_locks.setdefault(execution_id, threading.Lock())

    @property
    def _project_data(self) -> Path:
        return self._project_data_root

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
        parent_execution_id: ExecutionId,
    ) -> PreparedChild:
        identity = _ChildIdentity(
            execution_id,
            parent_execution_id,
            work_item_id,
            attempt_id,
            attempt_ordinal,
            attempt_directory,
        )
        context_raw = canonical_json_bytes(self._context(identity))
        paths = execution_paths(execution_id, booley_dir=self._project_data)
        _publish_or_verify(paths.context, context_raw, booley_dir=self._booley_dir)
        _ensure_child_record(paths.record)
        return PreparedChild(execution_id, paths.context)

    def mark_terminal(
        self,
        prepared: PreparedChild,
        terminal_cause: str,
    ) -> None:
        with self._record_lock(prepared.execution_id):
            paths = execution_paths(prepared.execution_id, booley_dir=self._project_data)
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

    def _retire_v030(
        self,
        child: _V030Child,
        lease_id: str | None,
        terminal_cause: str,
    ) -> None:
        """Publish the retirement record Booley 0.3.0 readers require.

        Every caller has already proven the slot token absent
        (``_release_matching_token`` / ``_assert_token_absent``), and the 0.3.0
        format records that proof as ``token_absent: true``.
        """
        with self._record_lock(child.execution_id):
            paths = execution_paths(child.execution_id, booley_dir=self._project_data)
            terminal = read_json(paths.record)
            if terminal is None or terminal.get("tree_terminal") is not True:
                raise SimulationCampaignIntegrityError(
                    "cannot retire a child execution before tree-terminal proof"
                )
            retirement = {
                "$schema": _RETIREMENT_SCHEMA,
                "child_execution_id": str(child.execution_id),
                "entry_sha256": child.entry_sha256,
                "execution_terminal_sha256": _sha(canonical_json_bytes(terminal)),
                "lease_id": lease_id,
                "token_absent": True,
                "terminal_cause": terminal_cause,
            }
            raw = canonical_json_bytes(retirement)
            name = f"{child.execution_id}.json"
            _publish_or_verify(
                self._project_root / "retired" / name, raw, booley_dir=self._booley_dir
            )
            _publish_or_verify(
                self._campaign_root / "retired" / name, raw, booley_dir=self._booley_dir
            )

    def is_terminal(self, execution_id: ExecutionId) -> bool:
        paths = execution_paths(execution_id, booley_dir=self._project_data)
        record = read_json(paths.record)
        return bool(
            record is not None
            and record.get("state") == "terminal"
            and record.get("tree_terminal") is True
        )

    def cancel(self, execution_id: ExecutionId) -> bool:
        paths = execution_paths(execution_id, booley_dir=self._project_data)
        request_cancellation(paths, reason="campaign_cancelled")
        deadline = time.monotonic() + 5.0
        while True:
            with self._record_lock(execution_id):
                recovered = recover_execution(execution_id, booley_dir=self._project_data)
            if recovered:
                return True
            if time.monotonic() >= deadline:
                return False
            time.sleep(0.05)

    def recover_lane(self, execution_id: ExecutionId) -> None:
        """Make an interrupted lane's execution terminal before its attempt is discarded.

        A lane whose record file is absent never started (or its terminal
        record was garbage-collected). An unreadable record is indeterminate,
        not absent. Anything else must be cancelled and proven tree-terminal;
        failing that, the caller must not delete the attempt.
        """
        record_path = execution_paths(execution_id, booley_dir=self._project_data).record
        if not record_path.exists() or self.is_terminal(execution_id):
            return
        if not self.cancel(execution_id):
            raise SimulationCampaignIntegrityError(
                f"child execution recovery is incomplete: {execution_id}"
            )

    def recover_unretired(self, slot_store: object | None) -> None:
        """Finish exact child cleanup before work-item attempt recovery."""
        entries = self._project_root / "entries"
        expected_manifest = str(self._store.manifest_path.resolve(strict=True))
        expected_digest = manifest_digest(self._manifest)
        recovered_ids: set[str] = set()
        paths = sorted(entries.glob("*.json")) if entries.is_dir() else []
        for path in paths:
            raw, entry = _read_protocol_record(path, "child entry", booley_dir=self._booley_dir)
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

    def recover_selected_unretired(self, slot_store: object | None) -> None:
        """Recover only child IDs named by this Campaign's authenticated mirror."""
        campaign_entries = self._campaign_root / "entries"
        expected_manifest = str(self._store.manifest_path.resolve(strict=True))
        expected_digest = manifest_digest(self._manifest)
        recovered_ids: set[str] = set()
        paths = sorted(campaign_entries.glob("*.json")) if campaign_entries.is_dir() else []
        for campaign_path in paths:
            if (self._campaign_root / "retired" / campaign_path.name).is_file():
                recovered_ids.add(campaign_path.name)
                continue
            campaign_raw, campaign_entry = _read_protocol_record(
                campaign_path, "campaign child entry", booley_dir=self._booley_dir
            )
            _validate_entry(campaign_path, campaign_entry)
            project_path = self._project_root / "entries" / campaign_path.name
            project_raw, entry = _read_protocol_record(
                project_path, "child entry", booley_dir=self._booley_dir
            )
            if project_raw != campaign_raw or entry != campaign_entry:
                raise SimulationCampaignIntegrityError(
                    "campaign child entry disagrees with Project-local authority"
                )
            if (
                entry.get("manifest_path") != expected_manifest
                or entry.get("manifest_sha256") != expected_digest
            ):
                raise SimulationCampaignIntegrityError(
                    "campaign child entry disagrees with its Campaign manifest"
                )
            self._validate_entry_identity(entry, _sha(project_raw))
            self._recover_entry(project_path, project_raw, entry, slot_store)
            recovered_ids.add(project_path.name)
        self._verify_campaign_entries(recovered_ids, expected_manifest, expected_digest)

    def _verify_campaign_entries(
        self, recovered: set[str], manifest_path: str, manifest_sha256: str
    ) -> None:
        entries = self._campaign_root / "entries"
        if not entries.is_dir():
            return
        for path in sorted(entries.glob("*.json")):
            _raw, document = _read_protocol_record(
                path, "campaign child entry", booley_dir=self._booley_dir
            )
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
            self._validate_bound_attempt(
                attempt_path, entry, entry_sha256, booley_dir=self._booley_dir
            )

    @staticmethod
    def _validate_bound_attempt(
        path: Path, entry: dict, entry_sha256: str, *, booley_dir: Path | None = None
    ) -> None:
        if path.is_symlink() or not path.is_file():
            raise SimulationCampaignIntegrityError("child-bound attempt is not regular")
        raw = _read_protocol_bytes(path, "child-bound attempt", booley_dir=booley_dir)
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
        _publish_or_verify(campaign_entry, raw, booley_dir=self._booley_dir)
        project_retired = self._project_root / "retired" / path.name
        campaign_retired = self._campaign_root / "retired" / path.name
        if project_retired.exists():
            retired_raw, _retirement = _read_protocol_record(
                project_retired, "child retirement", booley_dir=self._booley_dir
            )
            terminal = read_json(
                execution_paths(execution_id, booley_dir=self._project_data).record
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
            _publish_or_verify(campaign_retired, retired_raw, booley_dir=self._booley_dir)
            return
        child = _V030Child(
            execution_id,
            _sha(raw),
            execution_paths(execution_id, booley_dir=self._project_data).context,
        )
        self._recover_execution(child, entry)
        lease_id = self._release_matching_token(slot_store, execution_id)
        if slot_store is None:
            self._assert_no_unmanaged_token(execution_id)
        record = read_json(execution_paths(execution_id, booley_dir=self._project_data).record)
        cause = str(record.get("terminal_cause", "recovered")) if record else "recovered"
        self._retire_v030(child, lease_id, cause)

    def _recover_execution(self, child: _V030Child, entry: dict) -> None:
        identity = _ChildIdentity(
            child.execution_id,
            ExecutionId(entry["parent_execution_id"]),
            entry["work_item_id"],
            entry["attempt_id"],
            entry["attempt_ordinal"],
            self._store.root / entry["attempt_relative_path"],
        )
        context = self._context(identity)
        raw = canonical_json_bytes(context)
        if _sha(raw) != entry.get("runtime_context_sha256"):
            raise SimulationCampaignIntegrityError("child context digest disagrees with entry")
        _publish_or_verify(child.context_path, raw, booley_dir=self._booley_dir)
        paths = execution_paths(child.execution_id, booley_dir=self._project_data)
        _ensure_child_record(paths.record)
        if not self.is_terminal(child.execution_id) and not self.cancel(child.execution_id):
            raise SimulationCampaignIntegrityError(
                f"child execution recovery is incomplete: {child.execution_id}"
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
            if not slot_store.execution_token_absent(CLASS_HEAVY, execution_id):
                raise SimulationCampaignIntegrityError(
                    "child slot claim is unreadable; token absence cannot be proven"
                )
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

        if not slot_store.execution_token_absent(CLASS_HEAVY, execution_id):
            raise SimulationCampaignIntegrityError(
                "child retirement claims token absence while its slot claim remains or is unreadable"
            )

    def _assert_no_unmanaged_token(self, execution_id: ExecutionId) -> None:
        slots = self._project_data / "runtime" / "jobs" / "slots"
        for path in slots.glob("*/*.json"):
            try:
                payload = read_json_object(path)
            except OSError as exc:
                raise SimulationCampaignIntegrityError(
                    "child token presence cannot be reconciled: slot entry unreadable"
                ) from exc
            if payload is not None and payload.get("execution_id") == execution_id:
                raise SimulationCampaignIntegrityError(
                    "child token presence cannot be reconciled without managed admission"
                )

    def _context(self, identity: _ChildIdentity):
        return {
            "$schema": _CONTEXT_SCHEMA,
            "child_execution_id": str(identity.execution_id),
            "parent_execution_id": str(identity.parent_execution_id),
            "campaign_id": self._manifest.document["campaign_id"],
            "manifest_sha256": manifest_digest(self._manifest),
            "work_item_id": identity.work_item_id,
            "attempt_id": identity.attempt_id,
            "attempt_ordinal": identity.attempt_ordinal,
        }


def _ensure_child_record(record_path: Path) -> None:
    """Seed a waiting record when none is usable; never replace an unreadable one.

    A missing or corrupt record is replaced. An unreadable record (for example
    a Windows sharing violation that outlasted the read retry) may hold a
    terminal outcome, so overwriting it would lose the real exit code.
    """
    try:
        current = read_json_object(record_path)
    except OSError as exc:
        raise SimulationCampaignIntegrityError(
            f"child execution record is unreadable: {record_path}"
        ) from exc
    if current is None:
        atomic_write_json(record_path, _waiting_record())


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
    try:
        ordinal = require_int(entry.get("attempt_ordinal"), field="child attempt ordinal")
    except BoundaryError as exc:
        raise SimulationCampaignIntegrityError("child entry attempt ordinal is invalid") from exc
    if ordinal < 1:
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
    if (
        value.startswith("/")
        or path.is_absolute()
        or any(part in {"", ".", ".."} for part in path.parts)
    ):
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
        or (document.get("lease_id") is not None and not isinstance(document.get("lease_id"), str))
    ):
        detail = (
            "child retirement terminal digest is invalid"
            if isinstance(document, dict)
            and document.get("execution_terminal_sha256") != terminal_digest
            else "child retirement identity is invalid"
        )
        raise SimulationCampaignIntegrityError(detail)


def _publish_or_verify(path: Path, raw: bytes, *, booley_dir: Path | None = None) -> None:
    if path.exists():
        current = _read_protocol_bytes(path, "child protocol record", booley_dir=booley_dir)
        if current != raw:
            raise SimulationCampaignIntegrityError(
                f"child protocol record disagrees with durable identity: {path}"
            )
        return
    try:
        durable_create(path, raw)
    except FileExistsError:
        if _read_protocol_bytes(path, "child protocol record", booley_dir=booley_dir) != raw:
            raise SimulationCampaignIntegrityError(
                f"child protocol publication raced with different bytes: {path}"
            ) from None


def _read_protocol_record(
    path: Path, label: str, *, booley_dir: Path | None = None
) -> tuple[bytes, dict]:
    raw = _read_protocol_bytes(path, label, booley_dir=booley_dir)
    try:
        document = json.loads(raw)
    except (UnicodeDecodeError, ValueError) as exc:
        raise SimulationCampaignIntegrityError(f"{label} is invalid JSON: {path}") from exc
    if not isinstance(document, dict) or canonical_json_bytes(document) != raw:
        raise SimulationCampaignIntegrityError(f"{label} is not canonical JSON: {path}")
    return raw, document


def _read_protocol_bytes(path: Path, label: str, *, booley_dir: Path | None = None) -> bytes:
    try:
        descriptor = open_regular_nofollow(path, booley_dir=booley_dir)
    except OSError as exc:
        raise SimulationCampaignIntegrityError(
            f"{label} is not a link-free regular file: {path}"
        ) from exc
    try:
        info = os.fstat(descriptor)
        if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
            raise SimulationCampaignIntegrityError(
                f"{label} is not a singly linked regular file: {path}"
            )
        if info.st_size > _PROTOCOL_RECORD_LIMIT:
            raise SimulationCampaignIntegrityError(f"{label} exceeds size limit: {path}")
        raw = bytearray()
        while len(raw) <= _PROTOCOL_RECORD_LIMIT:
            chunk = os.read(
                descriptor,
                min(64 * 1024, _PROTOCOL_RECORD_LIMIT + 1 - len(raw)),
            )
            if not chunk:
                break
            raw.extend(chunk)
        if len(raw) != info.st_size:
            raise SimulationCampaignIntegrityError(f"{label} changed while reading: {path}")
        return bytes(raw)
    finally:
        os.close(descriptor)


__all__ = ["ChildExecutionRegistry", "PreparedChild"]
