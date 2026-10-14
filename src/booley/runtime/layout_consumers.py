"""Host consumer admission and observational quiescence for directory relocation."""

from __future__ import annotations

import json
import os
import uuid
from collections.abc import Generator
from contextlib import contextmanager
from itertools import islice
from pathlib import Path

from booley.core.booley_layout import BooleyLayoutError, layout_metadata, validate_environment
from booley.core.file_lock import LockTimeoutError
from booley.core.layout_lock import layout_gate
from booley.core.process_identity import ProcessIdentity
from booley.runtime.atomic_files import atomic_replace_bytes
from booley.runtime.pid import DEAD, REUSED, ZOMBIE, capture_process_identity, observe_process
from booley.runtime.safe_storage import refuse_symlinks


def require_quiescent(root: Path) -> None:
    """A malformed, live or unprovable host lease refuses; no stale state is reaped."""
    directory = layout_metadata(root) / "directory-consumers"
    refuse_symlinks(directory)
    if not directory.exists():
        return
    entries = list(islice(directory.iterdir(), 4097))
    if len(entries) > 4096:
        raise BooleyLayoutError("cannot inventory directory consumers: too many leases")
    for path in entries:
        refuse_symlinks(path)
        try:
            if path.stat().st_size > 64 * 1024:
                raise ValueError("consumer lease exceeds size limit")
            data = json.loads(path.read_bytes())
            identity = ProcessIdentity.from_payload(data["process"])
            if (
                identity is None
                or not isinstance(data["root"], str)
                or not Path(data["root"]).is_absolute()
            ):
                raise ValueError("consumer identity differs")
        except (OSError, ValueError, KeyError, TypeError) as exc:
            raise BooleyLayoutError(f"cannot prove directory consumer inactive: {path}") from exc
        if observe_process(identity).state not in {DEAD, REUSED, ZOMBIE}:
            raise BooleyLayoutError(f"stop directory consumer PID {identity.pid} before migration")


@contextmanager
def directory_consumer(root: Path) -> Generator[None]:
    """Register a host writer under the same gate used for migration publication.

    Sandbox consumers are fenced by host lifecycle serialization and Docker
    inventory; cross-VM filesystem locks are never used as that authority.
    """
    validate_environment()
    if os.environ.get("BOOLEY_CONTAINER") == "1":
        yield
        return
    root = root.resolve()
    try:
        with layout_gate(root):
            identity = capture_process_identity(os.getpid())
            if identity is None:
                raise BooleyLayoutError("cannot capture directory consumer process identity")
            path = layout_metadata(root) / "directory-consumers" / f"{uuid.uuid4()}.json"
            refuse_symlinks(path)
            atomic_replace_bytes(
                path, json.dumps({"root": str(root), "process": identity.to_payload()}).encode()
            )
    except LockTimeoutError as exc:
        raise BooleyLayoutError(
            "directory layout admission stayed busy; retry the command"
        ) from exc
    try:
        yield
    finally:
        path.unlink(missing_ok=True)
