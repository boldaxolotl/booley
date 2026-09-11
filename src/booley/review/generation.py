"""Pure review-artifact generation surface.

Ticket lifecycle code resolves and freezes the inputs.  This module is the one
documented composition seam through which Ticket Board asks Review to render
artifacts; it does not admit, move, fence, or recover tickets.
"""

from __future__ import annotations

from .artifact import ReviewPackage
from .evidence import ReviewEvidenceError, ReviewEvidencePackage, build_review_evidence
from .explanation import (
    ExplanationError,
    StructuredExplanation,
    render_explanation_html,
)
from .triage_package import (
    ResolvedReviewEvidence,
    TriagePackageError,
    build_review_facts,
    load_triage_package,
    open_package_diffs,
    render_review_briefing,
    validate_assessment,
    write_triage_package,
)

__all__ = [
    "ExplanationError",
    "ResolvedReviewEvidence",
    "ReviewEvidenceError",
    "ReviewEvidencePackage",
    "ReviewPackage",
    "StructuredExplanation",
    "TriagePackageError",
    "build_review_evidence",
    "build_review_facts",
    "load_triage_package",
    "open_package_diffs",
    "render_explanation_html",
    "render_review_briefing",
    "validate_assessment",
    "write_triage_package",
]
