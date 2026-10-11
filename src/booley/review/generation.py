"""Pure review-artifact generation surface.

The lifecycle owner resolves and freezes the inputs. This composition seam
renders review artifacts from those inputs without changing lifecycle state.
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
    accepted_review_presentation,
    build_review_facts,
    load_triage_package,
    open_package_diffs,
    render_review_briefing,
    saved_file_justifications,
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
    "accepted_review_presentation",
    "build_review_evidence",
    "build_review_facts",
    "load_triage_package",
    "open_package_diffs",
    "render_explanation_html",
    "render_review_briefing",
    "saved_file_justifications",
    "validate_assessment",
    "write_triage_package",
]
