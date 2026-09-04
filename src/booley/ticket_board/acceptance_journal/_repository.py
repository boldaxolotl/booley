"""Repository-operation boundary for recoverable acceptance."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import Literal, Protocol, TypeVar

from booley.runtime.ticket_repositories import resolve_inner_project_repo

_Result = TypeVar("_Result")


class RepositoryBoundary(StrEnum):
    """Semantic Git boundaries at which a retry may be required."""

    PREPARATION = "candidate-preparation"
    PUBLICATION = "publication"
    RETIREMENT = "retirement"


class AcceptanceRepositories(Protocol):
    """Repository discovery and semantic mutation seam."""

    def project_repository(self, root: Path) -> Path | None: ...

    def perform(
        self,
        boundary: RepositoryBoundary,
        role: str,
        operation: Callable[[], _Result],
    ) -> _Result: ...


class LocalAcceptanceRepositories:
    """Production adapter for local Git repositories."""

    def project_repository(self, root: Path) -> Path | None:
        return resolve_inner_project_repo(root)

    def perform(
        self,
        boundary: RepositoryBoundary,
        role: str,
        operation: Callable[[], _Result],
    ) -> _Result:
        del boundary, role
        return operation()


@dataclass
class FaultingAcceptanceRepositories:
    """Test decorator that interrupts one semantic repository boundary."""

    delegate: AcceptanceRepositories
    boundary: RepositoryBoundary
    role: str
    timing: Literal["before", "after"]
    error: Exception
    triggered: bool = False

    def project_repository(self, root: Path) -> Path | None:
        return self.delegate.project_repository(root)

    def perform(
        self,
        boundary: RepositoryBoundary,
        role: str,
        operation: Callable[[], _Result],
    ) -> _Result:
        should_interrupt = boundary is self.boundary and role == self.role and not self.triggered
        if should_interrupt and self.timing == "before":
            self.triggered = True
            raise self.error
        result = operation()
        if should_interrupt and self.timing == "after":
            self.triggered = True
            raise self.error
        return result
