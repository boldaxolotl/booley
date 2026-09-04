"""Durable storage boundary for Acceptance Journal checkpoints."""

from __future__ import annotations

import json
from collections.abc import Iterator
from contextlib import AbstractContextManager, contextmanager
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import Literal, Protocol

from booley.runtime.file_lock import nonblocking_file_lock
from booley.runtime.project_dir import runtime_dir

from ..persistence import atomic_replace_bytes
from ._model import AcceptanceJournal, load_journal, load_persisted_journal


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
    payload = (json.dumps(journal.as_dict(), indent=2, sort_keys=True) + "\n").encode()
    atomic_replace_bytes(path, payload)


class AcceptanceStore(Protocol):
    """Persistence and serialization seam for one Acceptance Journal."""

    def path(self, root: Path, slug: str) -> Path: ...

    def locked(self, path: Path) -> AbstractContextManager[None]: ...

    def load(
        self,
        path: Path,
        slug: str,
        participants: list[dict[str, str]],
        *,
        cleanup: bool,
        removal_targets: tuple[str, ...],
    ) -> AcceptanceJournal: ...

    def load_persisted(self, path: Path) -> AcceptanceJournal: ...

    def journals(self, directory: Path) -> tuple[Path, ...]: ...

    def write(
        self,
        path: Path,
        journal: AcceptanceJournal,
        checkpoint: AcceptanceCheckpoint,
    ) -> None: ...


class FileAcceptanceStore:
    """Production Acceptance Journal store backed by atomic JSON files."""

    def path(self, root: Path, slug: str) -> Path:
        return journal_path(root, slug)

    @contextmanager
    def locked(self, path: Path) -> Iterator[None]:
        lock_path = path.parent / ".lock"
        lock_path.touch(exist_ok=True)
        with lock_path.open("a+", encoding="utf-8") as handle, nonblocking_file_lock(handle):
            yield

    def load(
        self,
        path: Path,
        slug: str,
        participants: list[dict[str, str]],
        *,
        cleanup: bool,
        removal_targets: tuple[str, ...],
    ) -> AcceptanceJournal:
        return load_journal(
            path,
            slug,
            participants,
            cleanup=cleanup,
            removal_targets=removal_targets,
        )

    def load_persisted(self, path: Path) -> AcceptanceJournal:
        return load_persisted_journal(path)

    def journals(self, directory: Path) -> tuple[Path, ...]:
        return tuple(directory.glob("*.json"))

    def write(
        self,
        path: Path,
        journal: AcceptanceJournal,
        checkpoint: AcceptanceCheckpoint,
    ) -> None:
        write_journal(path, journal, checkpoint)


@dataclass
class FaultingAcceptanceStore:
    """Test decorator that interrupts one semantic durability boundary."""

    delegate: AcceptanceStore
    checkpoint: AcceptanceCheckpoint
    timing: Literal["before", "after"]
    triggered: bool = False

    def path(self, root: Path, slug: str) -> Path:
        return self.delegate.path(root, slug)

    def locked(self, path: Path) -> AbstractContextManager[None]:
        return self.delegate.locked(path)

    def load(
        self,
        path: Path,
        slug: str,
        participants: list[dict[str, str]],
        *,
        cleanup: bool,
        removal_targets: tuple[str, ...],
    ) -> AcceptanceJournal:
        return self.delegate.load(
            path,
            slug,
            participants,
            cleanup=cleanup,
            removal_targets=removal_targets,
        )

    def load_persisted(self, path: Path) -> AcceptanceJournal:
        return self.delegate.load_persisted(path)

    def journals(self, directory: Path) -> tuple[Path, ...]:
        return self.delegate.journals(directory)

    def write(
        self,
        path: Path,
        journal: AcceptanceJournal,
        checkpoint: AcceptanceCheckpoint,
    ) -> None:
        should_interrupt = checkpoint is self.checkpoint and not self.triggered
        if should_interrupt and self.timing == "before":
            self.triggered = True
            raise OSError(f"before {checkpoint} checkpoint")
        self.delegate.write(path, journal, checkpoint)
        if should_interrupt and self.timing == "after":
            self.triggered = True
            raise OSError(f"after {checkpoint} checkpoint")
