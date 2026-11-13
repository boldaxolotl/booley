"""Immutable acceptance facts the acceptance coordinator builds from campaign records."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import cast

from booley.evidence.fields import ACCEPTANCE_FACTS_SCHEMA_V2
from booley.flows.sim.record_files import document_digest

from .codec import canonical_json_bytes
from .model import _freeze_json

# v2 names the Test Run and its fingerprint of every observation and of every
# prerequisite's baseline ``cycle_observation`` (ADR 0072). It is the only
# version written.
ACCEPTANCE_FACTS_SCHEMA = ACCEPTANCE_FACTS_SCHEMA_V2


@dataclass(frozen=True, slots=True)
class AcceptanceFacts:
    """Facts built from records checked at their read boundaries and use sites.

    The document is deep-frozen once; nothing re-validates or decodes it.
    """

    document: Mapping[str, object]

    def __post_init__(self) -> None:
        object.__setattr__(
            self, "document", cast(Mapping[str, object], _freeze_json(self.document))
        )

    @property
    def sha256(self) -> str:
        """``sha256:`` digest of the canonical bytes without their trailing newline."""
        return document_digest(self.document)

    def canonical_bytes(self) -> bytes:
        """Sorted, compact UTF-8 JSON with one trailing newline."""
        return canonical_json_bytes(self.document)


__all__ = ["AcceptanceFacts"]
