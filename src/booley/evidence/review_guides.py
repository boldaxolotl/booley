"""Declare the packaged review guides each Reviewer focus runs, and digest them.

Receipt freshness (``booley.criteria``) may not import ``booley.specialists``, so the
declaration lives here, where both the Reviewer and the receipt contract can read it.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from booley.runtime.paths import refs_dir


@dataclass(frozen=True)
class ReviewGuide:
    """One packaged review guide under ``refs/code_review``."""

    # ``checklist``: items to check against the code. ``hunt``: a method for finding defects.
    kind: Literal["checklist", "hunt"]
    # File path relative to ``refs/code_review``; the stem is the guide name shown in prompts.
    path: str
    # Testbench guides only: load the guide when the review scope has this source language.
    language: Literal["hdl", "cocotb", "cpp"] | None = None

    @property
    def name(self) -> str:
        """The guide's name: its file stem, which ends in its kind."""
        return Path(self.path).stem


# The one declaration of which guides each ``(category, focus)`` review loads, in prompt order.
# A review makes one model call per guide kind, in order of first appearance.
REVIEW_GUIDES: dict[tuple[str, str], tuple[ReviewGuide, ...]] = {
    ("rtl", "bugs"): (
        ReviewGuide("checklist", "rtl/bugs-checklist.md"),
        ReviewGuide("hunt", "rtl/bugs-hunt.md"),
    ),
    ("rtl", "protocol"): (ReviewGuide("checklist", "rtl/protocol-cdc-checklist.md"),),
    ("rtl", "security"): (ReviewGuide("checklist", "rtl/security-checklist.md"),),
    ("rtl", "optimization"): (ReviewGuide("checklist", "rtl/optimization-checklist.md"),),
    ("rtl", "code_style"): (ReviewGuide("checklist", "rtl/code_style-checklist.md"),),
    ("rtl", "spec"): (ReviewGuide("hunt", "rtl/spec-hunt.md"),),
    ("tb", "quality"): (
        ReviewGuide("checklist", "testbench/c-cpp-checklist.md", "cpp"),
        ReviewGuide("checklist", "testbench/hdl-checklist.md", "hdl"),
        ReviewGuide("checklist", "testbench/cocotb-checklist.md", "cocotb"),
    ),
}


def review_guides_root() -> Path:
    """Directory holding the packaged review guides (the one patch point for tests)."""
    return refs_dir() / "code_review"


def guide_digest(guide: ReviewGuide) -> str:
    """SHA-256 of the guide file's bytes; a missing or unreadable file digests as empty."""
    try:
        return hashlib.sha256((review_guides_root() / guide.path).read_bytes()).hexdigest()
    except OSError:
        return ""


def declared_guide_names(category: str, focus: str) -> list[str]:
    """Names of the guides the declaration assigns to a focus, in declaration order."""
    return [guide.name for guide in REVIEW_GUIDES.get((category, focus), ())]
