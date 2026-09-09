"""Pure acceptance authority supplied to deterministic Flow execution."""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum


@dataclass(frozen=True, order=True)
class AcceptanceTargetBinding:
    """Canonical directed Target identities and their callable selectors."""

    flow: str
    criterion: str
    baseline: str
    candidate: str
    baseline_selector: str = ""
    candidate_selector: str = ""

    def validate_persisted(self) -> AcceptanceTargetBinding:
        """Reject incomplete or non-canonical values at persistence seams."""
        values = {
            "flow": self.flow,
            "criterion": self.criterion,
            "baseline": self.baseline,
            "candidate": self.candidate,
            "baseline_selector": self.baseline_selector,
            "candidate_selector": self.candidate_selector,
        }
        invalid = [
            name
            for name, value in values.items()
            if not isinstance(value, str) or not value.strip() or value != value.strip()
        ]
        if invalid:
            raise ValueError(
                "Acceptance Target binding requires canonical non-empty "
                + ", ".join(invalid)
            )
        _ = self.criterion_key
        return self

    @property
    def criterion_key(self) -> str:
        """Return the criterion name from its persisted frontmatter path."""
        for prefix in ("criteria.mandatory.", "criteria.optional."):
            if self.criterion.startswith(prefix) and self.criterion != prefix:
                return self.criterion.removeprefix(prefix)
        raise ValueError(
            "Acceptance Target binding criterion must be a full "
            "criteria.<mandatory|optional>.<criterion> path"
        )

    def as_dict(self) -> dict[str, str]:
        return {
            "flow": self.flow,
            "criterion": self.criterion,
            "baseline": self.baseline,
            "candidate": self.candidate,
            "baseline_selector": self.baseline_selector,
            "candidate_selector": self.candidate_selector,
        }


class PairedBaselineMode(Enum):
    """Authority for selecting an installed paired Project baseline."""

    STANDALONE = "standalone"
    ABSENT = "absent"
    TICKET_PINNED = "ticket-pinned"


@dataclass(frozen=True)
class PairedProjectBaseline:
    """Explicit paired-Project baseline policy; ticket pins never fall back."""

    mode: PairedBaselineMode
    sha: str = ""

    @classmethod
    def standalone(cls) -> PairedProjectBaseline:
        return cls(PairedBaselineMode.STANDALONE)

    @classmethod
    def absent(cls) -> PairedProjectBaseline:
        return cls(PairedBaselineMode.ABSENT)

    @classmethod
    def ticket_pinned(cls, sha: str) -> PairedProjectBaseline:
        if not sha:
            raise ValueError("ticket-pinned paired Project baseline requires a commit")
        return cls(PairedBaselineMode.TICKET_PINNED, sha)


@dataclass(frozen=True)
class ResolvedFlowAcceptance:
    """Storage-independent acceptance authority consumed by one Flow run."""

    bindings: tuple[AcceptanceTargetBinding, ...] = ()
    paired_project: PairedProjectBaseline = PairedProjectBaseline(
        PairedBaselineMode.STANDALONE
    )
    ticket_backed: bool = False

    @property
    def basis_bound(self) -> bool:
        return self.ticket_backed
