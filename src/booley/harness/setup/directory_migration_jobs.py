"""Read-only inventory of legacy Jobs and execution owners before relocation."""

from __future__ import annotations

import json
from pathlib import Path

from booley.core.booley_layout import BooleyLayoutError
from booley.core.process_identity import ProcessIdentity
from booley.runtime.job_records import STATUS_RUNNING, JobRecordError, strict_records
from booley.runtime.job_slots import JOB_CLASSES, SlotStore
from booley.runtime.pid import DEAD, REUSED, ZOMBIE, is_pid_alive, observe_process
from booley.runtime.safe_storage import refuse_symlinks
from booley.runtime.session_paths import project_job_roots


def _require_dead(identity: ProcessIdentity | None, pid: int | None, label: str) -> None:
    if identity is not None:
        inactive = observe_process(identity).state in {DEAD, REUSED, ZOMBIE}
    else:
        inactive = pid is not None and not is_pid_alive(pid)
    if not inactive:
        raise BooleyLayoutError(f"stop live/unknown Job or execution {label} before migration")


def _slots(directory: Path) -> None:
    root = directory / "runtime/jobs/slots"
    refuse_symlinks(root)
    store = SlotStore(root)
    for name in JOB_CLASSES:
        refuse_symlinks(root / name)
        entries = list((root / name).glob("[hw]-*"))
        holders, waiters = store.observe(name)
        tokens = [*holders, *waiters]
        if {token.path for token in tokens} != set(entries):
            raise BooleyLayoutError(f"cannot prove Job slot inactivity: {root / name}")
        for token in tokens:
            _require_dead(token.owner_identity, token.pid, str(token.path))


def _jobs(directory: Path) -> None:
    try:
        for root in project_job_roots(directory):
            refuse_symlinks(root)
            for record in strict_records(root):
                identity = ProcessIdentity.from_payload(record.process_identity)
                if record.status == STATUS_RUNNING:
                    _require_dead(identity, record.pid, record.run_id)
    except JobRecordError as exc:
        raise BooleyLayoutError(f"cannot prove Job inactivity: {exc}") from exc


def _executions(directory: Path) -> None:
    for path in (directory / ".runtime/executions").glob("*/record.json"):
        refuse_symlinks(path)
        try:
            data = json.loads(path.read_bytes())
            identity = ProcessIdentity.from_payload(data.get("supervisor"))
            if data.get("state") == "terminal" and data.get("tree_terminal") is True:
                continue
            _require_dead(identity, None, str(path))
        except (OSError, ValueError, TypeError, AttributeError) as exc:
            raise BooleyLayoutError(f"cannot prove execution inactivity: {path}") from exc


def observe_jobs(directory: Path) -> None:
    """No reaping, timeout-based exemption, cancellation or metadata publication."""
    _slots(directory)
    _jobs(directory)
    _executions(directory)
