"""``booley review --verify`` and ``--status``: re-check findings and ask if a commit is Reviewed.

Verify re-checks the open findings of the latest discovery Review Run of the commit or of an
ancestor, against the sources of the commit it is given (a fix commit). It adds no finding:
the Reviewer only judges each open finding fixed, waived or still open. The result is stored
as a verify Review Run that links to its discovery run. ``--status`` reads the stored runs.

Exit status: 0 when nothing is open (verify) or the commit is Reviewed (status), 1 when a
finding is open or the commit is not Reviewed, 2 when a Reviewer run failed or the command
was refused (nothing is stored for a refusal).
"""

from __future__ import annotations

import json
import re
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from booley.evidence.review_vocabulary import OPEN_STATUSES, STATUS_WAIVED
from booley.harness.review_command import (
    ReviewRefusedError,
    ReviewRequest,
    exported_commit,
    failure_text,
    key_of,
    load_live_settings,
    load_reviews,
    resolve_base_of,
    resolve_commit,
    run_reviewer,
)
from booley.review.declared_reviews import review_rules_changes
from booley.review.review_report import markdown_report, terminal_report
from booley.review.review_run import (
    DISCOVERY_KIND,
    ERROR,
    OPEN,
    PASS,
    VERIFY_KIND,
    PlannedReview,
    ReviewResult,
    VerifiedReview,
    VerifyFacts,
    exit_code_of,
    load_review_runs,
    new_run_id,
    open_finding_ids,
    plan_reviews,
    review_declarations,
    review_declarations_of,
    reviewed_by,
    store_review_run,
    verify_document,
)
from booley.runtime.pinned_history import FileCommitError, raw_git
from booley.runtime.timefmt import rfc3339_from_datetime
from booley.specialists.review_recheck import RecheckInput
from booley.specialists.reviewer import ReviewerSpecialist

_SHORT = 12  # hex digits of a commit shown in a message
_COMMIT_NAME = re.compile(r"[0-9a-f]{40,64}")


def _is_ancestor(project_root: Path, commit: str, descendant: str) -> bool:
    """Whether *commit* is *descendant* or one of its ancestors."""
    try:
        raw_git(project_root, "merge-base", "--is-ancestor", commit, descendant)
    except FileCommitError:
        return False
    return True


def _runs_of(main: Path, commit: str) -> list[tuple[Path, dict[str, Any]]]:
    try:
        return load_review_runs(main, commit)
    except ValueError as exc:
        raise ReviewRefusedError(str(exc)) from exc


def _history(project_root: Path, main: Path, sha: str) -> list[dict[str, Any]]:
    """Every stored run of *sha* or an ancestor, by commit folder."""
    folder = main / "reviews"
    names = sorted(p.name for p in folder.iterdir()) if folder.is_dir() else []
    return [
        document
        for name in names
        if _COMMIT_NAME.fullmatch(name) and _is_ancestor(project_root, name, sha)
        for _, document in _runs_of(main, name)
    ]


def _latest_discovery(history: list[dict[str, Any]], sha: str, base_commit: str) -> dict[str, Any]:
    """The latest discovery run in *history* against *base_commit*; none is a refusal."""
    found = [
        document
        for document in history
        if document["kind"] == DISCOVERY_KIND and document["base"]["commit"] == base_commit
    ]
    if not found:
        raise ReviewRefusedError(
            f"no discovery Review Run of {sha[:_SHORT]} or an ancestor against Base "
            f"{base_commit[:_SHORT]}; run booley review first"
        )
    latest = max(found, key=lambda document: document["run_id"])
    where = f"discovery Review Run {latest['run_id']} of {latest['commit'][:_SHORT]}"
    if latest["outcome"] == ERROR:
        raise ReviewRefusedError(f"{where} failed; run a new discovery")
    if not open_finding_ids(latest):
        raise ReviewRefusedError(f"{where} has no open finding to verify")
    return latest


def _chain(
    history: list[dict[str, Any]], discovery: dict[str, Any], document: dict[str, Any]
) -> list[dict[str, Any]]:
    """The discovery run, its earlier verify runs on this line of history, then *document*.

    A verify run on a sibling branch is not in *history*, so it never enters.
    """
    earlier = sorted(
        (
            run
            for run in history
            if run["kind"] == VERIFY_KIND and run["discovery"]["run_id"] == discovery["run_id"]
        ),
        key=lambda run: run["run_id"],
    )
    return [discovery, *earlier, document]


def _proposals(discovery: dict[str, Any], waive: tuple[tuple[str, str], ...]) -> dict[str, str]:
    """The waiver proposals as ``{finding ID: justification}``; refuse one that cannot be judged."""
    open_ids = frozenset().union(*open_finding_ids(discovery).values())
    proposals: dict[str, str] = {}
    for finding_id, justification in waive:
        if finding_id not in open_ids:
            problem = f"not an open finding of discovery Review Run {discovery['run_id']}"
        elif not justification.strip():
            problem = "the justification is empty"
        elif finding_id in proposals:
            problem = "proposed more than once"
        else:
            proposals[finding_id] = justification.strip()
            continue
        raise ReviewRefusedError(f"--waive {finding_id}: {problem}")
    return proposals


def _recheck(review: dict[str, Any], run_path: str, checkout: Path) -> RecheckInput:
    """The re-check input of one stored review: its contract and its introduced findings."""
    targets = [t for t in review["targets"] if t["needed"]]
    scope = tuple(sorted({file for target in targets for file in target["files"]}))
    contract: dict[str, Any] = {
        "category": review["role"],
        "focus": review["focus"],
        "scope": list(scope),
    }
    if review["spec"]:
        contract["spec_source"] = str(checkout / review["spec"])
    return RecheckInput(
        Path(run_path),
        contract,
        review["role"],
        review["focus"],
        scope,
        list(review["introduced"]),
    )


def _verify_one(
    review: dict[str, Any], run_path: str, checkout: Path, proposals: dict[str, str]
) -> VerifiedReview:
    """Re-check one stored review's open findings in the export; a failed run is ``error``."""
    open_ids = frozenset(
        row["finding_id"] for row in review["introduced"] if row["status"] in OPEN_STATUSES
    )
    mine = {key: value for key, value in proposals.items() if key in open_ids}
    argv = [
        *("--project", str(checkout), "--fail-on", review["resolve"]),
        *(word for pair in mine.items() for word in ("--waive", *pair)),
    ]
    reviewer = ReviewerSpecialist(recheck=_recheck(review, run_path, checkout))
    unchanged = ReviewResult(ERROR, tuple(review["introduced"]))
    try:
        outcome = run_reviewer(reviewer, argv)
    except (OSError, ValueError) as exc:
        failed = ReviewResult(ERROR, unchanged.introduced, error=" ".join(str(exc).split()))
        return VerifiedReview(review, failed, _waivers(mine, None, ()))
    if outcome.exit_code not in (0, 1):
        failed = ReviewResult(ERROR, unchanged.introduced, error=failure_text(outcome))
        return VerifiedReview(review, failed, _waivers(mine, None, ()))
    rows = tuple(outcome.detail["issue_list"])
    verdict = OPEN if outcome.exit_code == 1 else PASS
    refused = tuple(outcome.detail["refused_waivers"])
    return VerifiedReview(review, ReviewResult(verdict, rows), _waivers(mine, rows, refused))


def _waivers(
    mine: dict[str, str],
    rows: tuple[dict[str, Any], ...] | None,
    refused: tuple[dict[str, Any], ...],
) -> tuple[dict[str, Any], ...]:
    """Every proposal routed to a review, with whether the Reviewer accepted it (null on error)."""
    by_id = {row["finding_id"]: row for row in rows or ()}
    refusals = {row["finding_id"]: row for row in refused}
    waivers: list[dict[str, Any]] = []
    for finding_id, justification in mine.items():
        accepted = None if rows is None else by_id[finding_id]["status"] == STATUS_WAIVED
        given = by_id.get(finding_id, {}).get("waiver_ruling") or refusals.get(finding_id, {}).get(
            "ruling"
        )
        waivers.append(
            {
                "finding_id": finding_id,
                "justification": justification,
                "accepted": accepted,
                "ruling": given or None,
            }
        )
    return tuple(waivers)


def _refuse_changed_declarations(
    discovery: dict[str, Any], planned: list[PlannedReview], sha: str
) -> None:
    """The Declared Reviews must cover what the discovery run reviewed (ADR 0074, ADR 0091)."""
    if review_declarations(planned) != review_declarations_of(discovery):
        raise ReviewRefusedError(
            f"the Declared Reviews changed since discovery Review Run {discovery['run_id']}; "
            f"run a new discovery: booley review {sha[:_SHORT]}"
        )


def verify_review(project_root: Path, request: ReviewRequest) -> int:
    """Re-check the open findings of the latest discovery run against ``<sha>``; store the run."""
    from booley.core.booley_dir import main_booley_dir_of

    load_live_settings(project_root)
    started_at = datetime.now(UTC)
    sha = resolve_commit(project_root, request.revision)
    base, base_commit = resolve_base_of(project_root, sha, request.base)
    key_of(project_root, sha)
    main = main_booley_dir_of(project_root)
    history = _history(project_root, main, sha)
    discovery = _latest_discovery(history, sha, base_commit)
    proposals = _proposals(discovery, request.waive)
    run_id = new_run_id()
    with exported_commit(project_root, sha, main, run_id) as (checkout, export):
        keyed = key_of(project_root, sha, export.paired_commit)
        declared = load_reviews(checkout, None)
        planned = plan_reviews(declared, frozenset(), checkout)
        _refuse_changed_declarations(discovery, planned, sha)
        rules_changed = review_rules_changes(project_root, keyed, base_commit, checkout)
        path = str(main / "reviews" / discovery["commit"] / f"{discovery['run_id']}.json")
        reviews = tuple(
            _verify_one(discovery["reviews"][index], path, checkout, proposals)
            for index in open_finding_ids(discovery)
        )
    between = raw_git(project_root, "rev-list", "--reverse", f"{discovery['commit']}..{sha}")
    facts = VerifyFacts(
        run_id,
        rfc3339_from_datetime(started_at),
        rfc3339_from_datetime(datetime.now(UTC)),
        sha,
        keyed.booley_commit,
        base,
        discovery,
        tuple(between.decode("ascii").split()),
        reviews,
        keyed.message,
        rules_changed,
    )
    document = verify_document(facts)
    chain = _chain(history, discovery, document)
    stored = store_review_run(main, document, markdown_report(chain))
    _print_report(chain, stored, as_json=request.as_json)
    return exit_code_of(document["outcome"])


def _print_report(chain: list[dict[str, Any]], stored: Path, *, as_json: bool) -> None:
    document = chain[-1]
    where = f"Review Run {stored.name}"
    if as_json:
        print(json.dumps(document, indent=2))
        print(where, file=sys.stderr)
    else:
        print(terminal_report(chain), end="")
        print(f"\n{where}")
    for review in document["reviews"]:
        if review["error"] is not None:
            print(
                f"ERROR: {review['role']} {review['focus']} re-check failed: {review['error']}",
                file=sys.stderr,
            )


def review_status(project_root: Path, request: ReviewRequest) -> int:
    """Print whether ``<sha>`` is a Reviewed Commit; exit 0 when it is, 1 when it is not."""
    from booley.core.booley_dir import main_booley_dir_of

    sha = resolve_commit(project_root, request.revision)
    runs = [document for _, document in _runs_of(main_booley_dir_of(project_root), sha)]
    reviewer = reviewed_by(runs)
    short = sha[:_SHORT]
    if reviewer is not None:
        print(f"{short}: Reviewed ({reviewer['kind']} Review Run {reviewer['run_id']})")
        return 0
    if not runs:
        reason = "no Review Run"
    elif latest := [run for run in runs if run["kind"] != DISCOVERY_KIND]:
        reason = f"latest verify Review Run {latest[-1]['run_id']} is {latest[-1]['outcome']}"
    else:
        reason = "no Review Run leaves nothing open"
    print(f"{short}: not Reviewed ({reason})")
    return 1
