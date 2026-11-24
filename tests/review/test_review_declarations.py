"""Declared Reviews comparison used by ``booley review --verify`` (issue #3273)."""

from __future__ import annotations

from dataclasses import asdict
from typing import Any

from booley.review.review_run import (
    PlannedReview,
    TargetFiles,
    review_declarations,
    review_declarations_of,
)


def test_declarations_ignore_the_order_of_a_reviews_targets() -> None:
    """A Target declaring one review over two file sets lists them in hash order."""
    targets = (TargetFiles("p#t", ("rtl/a.sv",), True), TargetFiles("p#t", ("rtl/b.sv",), True))
    planned = [PlannedReview("bugs", "rtl", None, "major", targets)]
    reordered = [PlannedReview("bugs", "rtl", None, "major", targets[::-1])]
    document: dict[str, Any] = {"reviews": [asdict(r) for r in reordered]}

    assert review_declarations(planned) == review_declarations_of(document)
