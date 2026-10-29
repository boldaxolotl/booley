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

    digest: str | None  # None when no candidate is eligible: nothing to confirm
    candidates: tuple[Candidate, ...]
    released: list[str] = field(default_factory=list)
    retained: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)
    schema: int = 1


@dataclass(frozen=True, slots=True)
class Leftover:
    """One Docker resource a forgotten Project still leaves on the host."""

    kind: Literal["image", "volume", "container", "network"]
    name: str
    reason: Literal["unused", "in-use", "shared", "unrecorded", "labelled"]
    size: str | None = None
    tags: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class PurgeResult:
    """What one purge removed, and what it kept with the reason."""

    removed: tuple[str, ...] = ()
    kept: tuple[str, ...] = ()
