"""Structured outcomes for Project image-keeper operations."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Literal


@dataclass(frozen=True, slots=True)
class KeeperRelease:
    """The outcome of releasing one exact remembered root's keeper."""

    status: Literal["released", "absent", "retained-in-use"]
    tag: str
    image_id: str | None = None


@dataclass(frozen=True, slots=True)
class Candidate:
    """One exact keeper observation and its inventory/container eligibility."""

    tag: str
    image_id: str
    eligible: bool
    reason: Literal["inventoried", "in-use", "inventory-absent"]


@dataclass(slots=True)
class PruneResult:
    """A preview and explicit partial-apply accounting."""

    digest: str
    candidates: tuple[Candidate, ...]
    released: list[str] = field(default_factory=list)
    retained: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)
    schema: int = 1
