"""Plan the reviews of a commit and write its Review Run (issue #3272).

A Review Run is the JSON document ``booley review`` stores for one discovery run of a
commit (or, with ``--verify``, one verify run of a fix commit): every Declared Review that
had a changed file, what it found, and the Base the change was taken from. This module
holds the pure parts: which reviews a change needs (one per distinct review, however many
Targets declare it), the documents, their storage, and which commits are Reviewed. It runs
no Reviewer and prints nothing.
"""

from __future__ import annotations

import hashlib
import json
import secrets
from collections.abc import Collection, Mapping, Sequence
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

from booley.core.atomic_files import atomic_replace_bytes, atomic_write_json
from booley.evidence.review_vocabulary import OPEN_STATUSES
from booley.measurement.base import Base
from booley.review.review_resolution import DeclaredReview
from booley.runtime.timefmt import compact_utc_now
from booley.specialists.review_contract import reviewable

SCHEMA_VERSION = 1
DISCOVERY_KIND = "discovery"
VERIFY_KIND = "verify"
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
    spec_digest: str | None = None  # "sha256:<hex>" of the spec file's bytes, null without one

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
    message: str  # the commit message
    rules_changed: tuple[str, ...]  # review rules changed since the Base, as declared


def new_run_id() -> str:
    """A run id that sorts by start time, to the microsecond, and does not repeat."""
    return f"{compact_utc_now(microseconds=True)}-{secrets.token_hex(3)}"


def spec_digest(checkout: Path, spec: str | None) -> str | None:
    """``sha256:<hex>`` of the bytes of *spec* in *checkout*; null without a spec."""
    if spec is None:
        return None
    return "sha256:" + hashlib.sha256((checkout / spec).read_bytes()).hexdigest()


def plan_reviews(
    declared: Mapping[str, Collection[DeclaredReview]],
    changed: Collection[str],
    checkout: Path | None = None,
) -> list[PlannedReview]:
    """Group the Targets' reviews by what they ask the Reviewer, and mark the needed ones.

    A Target needs a review when one of the review's reviewable files changed, so two
    Targets that share a changed file share one run of the same review. With *checkout*,
    each review records the digest of its spec file there.
    """
    groups: dict[tuple[str, str, str | None, str], list[TargetFiles]] = {}
    for target in sorted(declared):
        for review in declared[target]:
            files = tuple(sorted(f for f in review.files if reviewable(f, review.role)))
            needed = any(f in changed for f in files)
            key = (review.role, review.focus, review.spec, review.resolve)
            groups.setdefault(key, []).append(TargetFiles(target, files, needed))
    return [
        PlannedReview(
            role,
            focus,
            spec,
            resolve,
            tuple(targets),
            spec_digest(checkout, spec) if checkout is not None else None,
        )
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
        "kind": DISCOVERY_KIND,
        "run_id": facts.run_id,
        "started_at": facts.started_at,
        "finished_at": facts.finished_at,
        "commit": facts.commit,
        "booley_commit": facts.booley_commit,
        "message": facts.message,
        "rules_changed": list(facts.rules_changed),
        "base": asdict(facts.base),  # the choice is a StrEnum: JSON writes its wording
        "changed_files": list(facts.changed_files),
        "reviews": [
            {
                "role": planned.role,
                "focus": planned.focus,
                "spec": planned.spec,
                "resolve": planned.resolve,
                "spec_digest": planned.spec_digest,
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


def store_review_run(main_booley_dir: Path, document: dict[str, Any], markdown: str) -> Path:
    """Write *markdown* and then *document* under ``reviews/<commit>/<run id>``; earlier runs stay.

    The JSON is the source of truth: a failure between the two writes leaves a Markdown file
    no loader reads. Returns the JSON path.
    """
    path = main_booley_dir / REVIEWS_DIR / document["commit"] / f"{document['run_id']}.json"
    atomic_replace_bytes(path.with_suffix(".md"), markdown.encode("utf-8"), mode=0o644)
    atomic_write_json(path, document)
    return path


Declaration = tuple[str, str, str | None, str, str | None, tuple[tuple[str, tuple[str, ...]], ...]]


def review_declarations(planned: Collection[PlannedReview]) -> frozenset[Declaration]:
    """What each planned review covers: role, focus, spec and its digest, resolve, Target files."""
    return frozenset(
        (
            review.role,
            review.focus,
            review.spec,
            review.resolve,
            review.spec_digest,
            tuple(sorted((target.target, target.files) for target in review.targets)),
        )
        for review in planned
    )


def review_declarations_of(document: Mapping[str, Any]) -> frozenset[Declaration]:
    """The declarations of a stored discovery run, comparable with ``review_declarations``."""
    return frozenset(
        (
            review["role"],
            review["focus"],
            review["spec"],
            review["resolve"],
            review.get("spec_digest"),
            tuple(
                sorted((target["target"], tuple(target["files"])) for target in review["targets"])
            ),
        )
        for review in document["reviews"]
    )


def open_finding_ids(document: Mapping[str, Any]) -> dict[int, frozenset[str]]:
    """Per review index of a run, the IDs of its introduced findings that are still open."""
    return {
        index: ids
        for index, review in enumerate(document["reviews"])
        if (
            ids := frozenset(
                r["finding_id"] for r in review["introduced"] if r["status"] in OPEN_STATUSES
            )
        )
    }


@dataclass(frozen=True)
class VerifiedReview:
    """One discovery review re-checked: the stored review, what the Reviewer said, the waivers."""

    review: Mapping[str, Any]
    result: ReviewResult
    waivers: tuple[dict[str, Any], ...]


@dataclass(frozen=True)
class VerifyFacts:
    """What a verify run document records."""

    run_id: str
    started_at: str
    finished_at: str
    commit: str
    booley_commit: str | None
    base: Base
    discovery: Mapping[str, Any]  # the discovery run being verified
    commits: tuple[str, ...]  # after the discovery commit, up to ``commit``, oldest first
    reviews: tuple[VerifiedReview, ...]
    message: str  # the commit message
    rules_changed: tuple[str, ...]  # review rules changed since the Base, as declared


def verify_document(facts: VerifyFacts) -> dict[str, Any]:
    """The verify Review Run document of *facts*: it adds no finding to the discovery run's."""
    return {
        "schema_version": SCHEMA_VERSION,
        "kind": VERIFY_KIND,
        "run_id": facts.run_id,
        "started_at": facts.started_at,
        "finished_at": facts.finished_at,
        "commit": facts.commit,
        "booley_commit": facts.booley_commit,
        "message": facts.message,
        "rules_changed": list(facts.rules_changed),
        "base": asdict(facts.base),
        "discovery": {"commit": facts.discovery["commit"], "run_id": facts.discovery["run_id"]},
        "commits": list(facts.commits),
        "reviews": [
            {
                **{k: item.review[k] for k in ("role", "focus", "spec", "resolve", "targets")},
                "outcome": item.result.outcome,
                "introduced": list(item.result.introduced),
                "waivers": list(item.waivers),
                "error": item.result.error,
            }
            for item in facts.reviews
        ],
        "outcome": run_outcome([item.result for item in facts.reviews]),
    }


def load_review_runs(main_booley_dir: Path, commit: str) -> list[tuple[Path, dict[str, Any]]]:
    """The stored runs of *commit*, oldest first; a malformed file raises ``ValueError``."""
    runs: list[tuple[Path, dict[str, Any]]] = []
    for path in sorted((main_booley_dir / REVIEWS_DIR / commit).glob("*.json")):
        try:
            document: Any = json.loads(path.read_bytes())
        except (OSError, ValueError) as exc:
            raise ValueError(f"Review Run {path.name} cannot be read: {exc}") from exc
        if not isinstance(document, dict) or document.get("schema_version") != SCHEMA_VERSION:
            raise ValueError(
                f"Review Run {path.name} is not a schema version {SCHEMA_VERSION} run"
            )
        runs.append((path, document))  # pyright: ignore[reportUnknownArgumentType]
    return runs


def reviewed_by(runs: Sequence[Mapping[str, Any]]) -> Mapping[str, Any] | None:
    """The run that makes its commit a Reviewed Commit, or ``None``.

    The latest verify run when it passed; else any discovery run that passed or had nothing
    to review. A run with an open or errored outcome never does.
    """
    verified = [run for run in runs if run["kind"] == VERIFY_KIND]
    if verified and verified[-1]["outcome"] == PASS:
        return verified[-1]
    clean = (PASS, NOTHING_TO_REVIEW)
    return next(
        (run for run in runs if run["kind"] == DISCOVERY_KIND and run["outcome"] in clean), None
    )
