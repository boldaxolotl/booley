"""Durable storage boundary for Acceptance Journal checkpoints."""

from __future__ import annotations

import json
import os
import tempfile
from enum import StrEnum
from pathlib import Path

from booley.runtime.project_dir import runtime_dir

from ._model import AcceptanceJournal


class AcceptanceCheckpoint(StrEnum):
    """Semantic durability boundaries used by recovery tests and adapters."""

    NORMALIZED = "normalized"
    SOURCES_PINNED = "sources-pinned"
    CANDIDATES_PREPARED = "candidates-prepared"
    PREPARATION_COMPLETE = "preparation-complete"
    CANDIDATES_FINALIZED = "candidates-finalized"
    LEGACY_PREPARED_RECOVERED = "legacy-prepared-recovered"
    PROJECT_PUBLISHED = "project-published"
    OUTER_PUBLISHED = "outer-published"
    ACCEPTED = "accepted"
    PROJECT_CLEANED = "project-cleaned"
    OUTER_CLEANED = "outer-cleaned"
    DONE = "done"


def journal_path(root: Path, slug: str) -> Path:
    """Resolve and create the runtime path for one Ticket's journal."""
    directory = runtime_dir(root) / "acceptance"
    directory.mkdir(parents=True, exist_ok=True)
    return directory / f"{slug}.json"


def write_journal(
    path: Path,
    journal: AcceptanceJournal,
    checkpoint: AcceptanceCheckpoint,
) -> None:
    """Atomically persist and fsync one semantic recovery checkpoint."""
    del checkpoint
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    temporary_path = Path(temporary)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(journal, handle, indent=2, sort_keys=True)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        temporary_path.replace(path)
        if os.name != "nt":
            directory_fd = os.open(path.parent, os.O_RDONLY)
            try:
                os.fsync(directory_fd)
            finally:
                os.close(directory_fd)
    finally:
        if temporary_path.exists():
            temporary_path.unlink()
