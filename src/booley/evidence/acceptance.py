"""The Paired Checkout baseline policy of a deterministic Flow run."""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum


class PairedBaselineMode(Enum):
    """Authority for selecting an installed Paired Checkout baseline."""

    STANDALONE = "standalone"
    ABSENT = "absent"
    # The paired revision recorded when a baseline was pinned (ADR 0067).
    ENTRY_PINNED = "entry-pinned"


@dataclass(frozen=True)
class PairedProjectBaseline:
    """Explicit Paired Checkout baseline policy; entry pins never fall back."""

    mode: PairedBaselineMode
    sha: str = ""

    @classmethod
    def standalone(cls) -> PairedProjectBaseline:
        return cls(PairedBaselineMode.STANDALONE)

    @classmethod
    def absent(cls) -> PairedProjectBaseline:
        return cls(PairedBaselineMode.ABSENT)

    @classmethod
    def entry_pinned(cls, sha: str) -> PairedProjectBaseline:
        """The Paired Checkout revision recorded when the baseline was pinned; never falls back."""
        if not sha:
            raise ValueError("entry-pinned Paired Checkout baseline requires a commit")
        return cls(PairedBaselineMode.ENTRY_PINNED, sha)

    @property
    def pinned(self) -> bool:
        """Whether the policy names an exact paired revision (no upstream lookup)."""
        return self.mode is PairedBaselineMode.ENTRY_PINNED
