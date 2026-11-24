"""Plan the reviews of a commit and write its Review Run (issue #3272).

A Review Run is the JSON document ``booley review`` stores for one discovery run of a
commit: every Declared Review that had a changed file, what it found, and the Base the
change was taken from. This module holds the pure parts: which reviews a change needs
(one per distinct review, however many Targets declare it), the document, and its
storage. It runs no Reviewer and prints nothing.
"""

from __future__ import annotations

import secrets
from collections.abc import Collection, Mapping
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

from booley.measurement.base import Base
from booley.review.review_resolution import DeclaredReview
from booley.runtime.execution_records import atomic_write_json
from booley.runtime.timefmt import compact_utc_now
from booley.specialists.review_contract import reviewable

SCHEMA_VERSION = 1
KIND = "discovery"
REVIEWS_DIR = "reviews"
NOT_NEEDED = "not-needed"
PASS = "pass"
OPEN = "open"
ERROR = "error"
NOTHING_TO_REVIEW = "nothing-to-review"


@dataclass(frozen=True)
class TargetFiles:
    """One Target's share of a review: its reviewable files, and whether a change needs it."""

    target: str  # identity (vlnv#name)
    files: tuple[str, ...]
    needed: bool


@dataclass(frozen=True)
class PlannedReview:
    """One distinct review: the Targets that declare it and the files one run covers."""

    role: str
    focus: str
    spec: str | None
    resolve: str
    targets: tuple[TargetFiles, ...]

    @property
    def needed(self) -> bool:
        return any(target.needed for target in self.targets)

    @property
    def files(self) -> tuple[str, ...]:
        """The union of the files of the Targets that need the review."""
        return tuple(sorted({f for t in self.targets if t.needed for f in t.files}))


@dataclass(frozen=True)
class ReviewResult:
    """What one planned review found; ``not-needed`` reviews carry nothing."""

    outcome: str
    introduced: tuple[dict[str, Any], ...] = ()
    pre_existing: tuple[dict[str, Any], ...] = ()
    error: str | None = None


@dataclass(frozen=True)
class RunFacts:
    """What a Review Run document records besides its reviews."""

    run_id: str
    started_at: str
    finished_at: str
    commit: str
    booley_commit: str | None
    base: Base
    changed_files: tuple[str, ...]
    reviews: tuple[tuple[PlannedReview, ReviewResult], ...]


def new_run_id() -> str:
    """A run id that sorts by time and does not repeat within a second."""
    return f"{compact_utc_now()}-{secrets.token_hex(3)}"


def plan_reviews(
    declared: Mapping[str, Collection[DeclaredReview]], changed: Collection[str]
) -> list[PlannedReview]:
    """Group the Targets' reviews by what they ask the Reviewer, and mark the needed ones.

    A Target needs a review when one of the review's reviewable files changed, so two
    Targets that share a changed file share one run of the same review.
    """
    groups: dict[tuple[str, str, str | None, str], list[TargetFiles]] = {}
    for target in sorted(declared):
        for review in declared[target]:
            files = tuple(sorted(f for f in review.files if reviewable(f, review.role)))
            needed = any(f in changed for f in files)
            key = (review.role, review.focus, review.spec, review.resolve)
            groups.setdefault(key, []).append(TargetFiles(target, files, needed))
    return [
        PlannedReview(role, focus, spec, resolve, tuple(targets))
        for (role, focus, spec, resolve), targets in sorted(
            groups.items(), key=lambda item: (item[0][0], item[0][1], item[0][2] or "", item[0][3])
        )
    ]


def run_outcome(results: Collection[ReviewResult]) -> str:
    """The Review Run's outcome: the worst of its reviews'."""
    outcomes = {result.outcome for result in results}
    for worst in (ERROR, OPEN, PASS):
        if worst in outcomes:
            return worst
    return NOTHING_TO_REVIEW


def exit_code_of(outcome: str) -> int:
    """0 when nothing is open, 1 when a finding is open, 2 when a review errored."""
    return {ERROR: 2, OPEN: 1}.get(outcome, 0)


def review_document(facts: RunFacts) -> dict[str, Any]:
    """The Review Run document of *facts*."""
    return {
        "schema_version": SCHEMA_VERSION,
        "kind": KIND,
        "run_id": facts.run_id,
        "started_at": facts.started_at,
        "finished_at": facts.finished_at,
        "commit": facts.commit,
        "booley_commit": facts.booley_commit,
        "base": asdict(facts.base),  # the choice is a StrEnum: JSON writes its wording
        "changed_files": list(facts.changed_files),
        "reviews": [
            {
                "role": planned.role,
                "focus": planned.focus,
                "spec": planned.spec,
                "resolve": planned.resolve,
                "targets": [asdict(target) for target in planned.targets],
                "outcome": result.outcome,
                "introduced": list(result.introduced),
                "pre_existing": list(result.pre_existing),
                "error": result.error,
            }
            for planned, result in facts.reviews
        ],
        "outcome": run_outcome([result for _, result in facts.reviews]),
    }


def store_review_run(main_booley_dir: Path, document: dict[str, Any]) -> Path:
    """Write *document* under ``reviews/<commit>/<run id>.json``; earlier runs stay."""
    path = main_booley_dir / REVIEWS_DIR / document["commit"] / f"{document['run_id']}.json"
    atomic_write_json(path, document)
    return path
