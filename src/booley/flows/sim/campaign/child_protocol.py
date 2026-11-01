"""Per-lane execution identity for parallel Campaign lanes."""

from __future__ import annotations

import os
import stat
import threading
import time
from dataclasses import dataclass
from pathlib import Path

from booley.flows.sim.campaign_durability import durable_create
from booley.runtime.booley_dir import resolve_checkout_booley_dir
from booley.runtime.execution_records import (
    PROTOCOL_VERSION,
    ExecutionId,
    atomic_write_json,
    execution_paths,
    read_json,
    request_cancellation,
)
from booley.runtime.execution_recovery import recover_execution
from booley.runtime.regular_file import open_regular_nofollow
from booley.runtime.timefmt import utc_now_rfc3339

from .codec import SimulationCampaignIntegrityError, canonical_json_bytes
from .model import SimulationCampaignManifest
from .planning import manifest_digest

_CONTEXT_SCHEMA = "booley.supervised-child-context/v1"
_PROTOCOL_RECORD_LIMIT = 1024 * 1024


@dataclass(frozen=True, slots=True)
class PreparedChild:
    """One parallel lane's execution identity: its ID and context sidecar."""

    execution_id: ExecutionId
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
    """Give parallel lanes execution identities and make interrupted ones terminal."""

    def __init__(self, manifest: SimulationCampaignManifest, project_root: Path) -> None:
        project_data = resolve_checkout_booley_dir(project_root)
        self._configure(manifest, project_data, project_data)

    @classmethod
    def from_project_data(
        cls,
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
        registry._configure(manifest, project_data, booley_dir)
        return registry

    def _configure(
        self,
        manifest: SimulationCampaignManifest,
        project_data: Path,
        booley_dir: Path | None,
    ) -> None:
        self._manifest = manifest
        self._project_data_root = project_data
        self._booley_dir = booley_dir
        # A lane's mark_terminal and the cancel path (recover_execution) each
        # read-check-write one execution record. Serialize them per execution so
        # those steps never interleave.
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
        if read_json(paths.record) is None:
            atomic_write_json(paths.record, _waiting_record())
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
