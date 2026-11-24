"""``booley review [<sha>]``: discover findings in one commit and store the Review Run (#3272).

The command composes existing pieces. It chooses the commit's Base as ``booley measure``
does, exports the commit into a clean checkout, reads that checkout's own Declared Reviews,
and for each distinct review that has a changed file runs the Reviewer in the export, one
run after another and in this process. Two Targets that declare the same review share one
Reviewer run. The result is stored as a Review Run in the Main Booley Directory, unless
``--target`` is given: such a run is only printed.

Exit status: 0 when no finding is open, 1 when a finding the commit introduced is open at
or above its review's ``resolve`` level, 2 when a Reviewer run failed or the command was
refused (nothing is stored for a refusal before any Reviewer run).
"""

from __future__ import annotations

import json
import shutil
import sys
import tempfile
import traceback
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from booley.evidence.review_vocabulary import ORIGIN_PRE_EXISTING
from booley.flows.prepared_invocation import PreparedInvocation
from booley.harness.checkout_flow import run_post_setup_hook
from booley.harness.standalone_job import standalone_log_root
from booley.measurement.base import Base, BaseError, ChangedFiles, changed_files, resolve_base
from booley.measurement.run_key import KeyedCommit, RulesNotVersionedError, resolve_measurement_key
from booley.review.declared_reviews import ReviewsFileError
from booley.review.review_resolution import DeclaredReview, load_declared_reviews
from booley.review.review_run import (
    ERROR,
    NOT_NEEDED,
    OPEN,
    PASS,
    PlannedReview,
    ReviewResult,
    RunFacts,
    exit_code_of,
    new_run_id,
    plan_reviews,
    review_document,
    store_review_run,
)
from booley.runtime.commit_export import CommitExport, CommitExportError, export_commit
from booley.runtime.endpoint_execution import EndpointOutcome
from booley.runtime.pinned_history import FileCommitError, raw_git
from booley.runtime.timefmt import rfc3339_from_datetime
from booley.specialists.review_change import committed_review_change, under_folder
from booley.specialists.reviewer import ReviewerSpecialist
from booley.specialists.specialist_workspace import initialize_snapshot_git
from booley.targets.catalog import TargetCatalog
from booley.targets.domain import FuseSocError

_EXIT_REFUSED = 2
_SHORT = 12  # hex digits of a commit shown in a message
_ERROR_LINE = 300  # characters of a Reviewer error kept in the Review Run


@dataclass(frozen=True)
class ReviewRequest:
    """What the user asked ``booley review`` to do."""

    revision: str  # the user's <sha>; ``HEAD`` by default
    base: str | None  # --base
    target: str | None  # --target
    as_json: bool  # --json
    verify: bool = False  # --verify
    status: bool = False  # --status
    waive: tuple[tuple[str, str], ...] = ()  # --waive <finding-id> <justification>


class ReviewRefusedError(Exception):
    """The review cannot run: exit 2, and nothing is stored."""


def run_review(project_root: Path, request: ReviewRequest) -> int:
    """Review the commit *request* names; print the outcome and return the exit status.

    A refusal or an unexpected failure prints its error and exits 2. A
    ``KeyboardInterrupt`` passes through.
    """
    try:
        return _dispatch(project_root, request)
    except ReviewRefusedError as refusal:
        print(f"ERROR: {refusal}", file=sys.stderr)
    except Exception as failure:  # noqa: BLE001 - the command boundary: infrastructure failures
        print(f"ERROR: review failed: {failure}", file=sys.stderr)
        traceback.print_exc()
    return _EXIT_REFUSED


def _dispatch(project_root: Path, request: ReviewRequest) -> int:
    """Refuse options that do not go together, then run status, verify or discovery."""
    _refuse_conflicts(request)
    if request.status:
        from booley.harness.review_verify import review_status

        return review_status(project_root, request)
    if request.verify:
        from booley.harness.review_verify import verify_review

        return verify_review(project_root, request)
    return _review(project_root, request)


def _refuse_conflicts(request: ReviewRequest) -> None:
    """An option the chosen mode cannot honor is a refusal, before anything runs."""
    if request.waive and not request.verify:
        raise ReviewRefusedError("--waive needs --verify")
    mode = "--status" if request.status else "--verify" if request.verify else None
    if mode and request.target is not None:
        raise ReviewRefusedError(f"--target cannot be used with {mode}")
    if request.status:
        for option, given in (("--base", request.base is not None), ("--json", request.as_json)):
            if given:
                raise ReviewRefusedError(f"{option} cannot be used with --status")


def resolve_commit(project_root: Path, revision: str) -> str:
    """The full sha of *revision*; an option-like or unknown revision is a refusal."""
    try:
        if revision.startswith("-"):
            raise FileCommitError(f"{revision!r} is not a revision")
        raw = raw_git(project_root, "rev-parse", "--verify", f"{revision}^{{commit}}")
    except FileCommitError as exc:
        raise ReviewRefusedError(f"{revision!r} is not a commit of this Project: {exc}") from exc
    return raw.decode("utf-8", "replace").strip()


def resolve_base_of(project_root: Path, commit: str, requested: str | None) -> tuple[Base, str]:
    """The Base, chosen as ``booley measure`` chooses it, and its sha; none is a refusal."""
    try:
        base = resolve_base(project_root, commit, requested)
    except (FileCommitError, BaseError) as exc:
        raise ReviewRefusedError(f"--base {requested!r} cannot be used: {exc}") from exc
    if base.commit is None:
        raise ReviewRefusedError(f"no Base: {base.no_base}; pass --base <sha>")
    return base, base.commit


def key_of(project_root: Path, commit: str, paired_commit: str | None = None) -> KeyedCommit:
    """The commit's key: rules that are not versioned cannot be reviewed as of a commit."""
    try:
        return resolve_measurement_key(project_root, commit, booley_commit=paired_commit)
    except (RulesNotVersionedError, ValueError, FileCommitError) as exc:
        raise ReviewRefusedError(str(exc)) from exc


def load_reviews(checkout: Path, target: str | None) -> dict[str, frozenset[DeclaredReview]]:
    """The checkout's Declared Reviews by Target identity, narrowed to *target* when given."""
    try:
        declared = load_declared_reviews(checkout).reviews
        identity = (
            None if target is None else TargetCatalog.build(checkout).select(target).identity
        )
    except ReviewsFileError as exc:
        for error in exc.errors:
            print(error.render(), file=sys.stderr)
        raise ReviewRefusedError(str(exc)) from exc
    except FuseSocError as exc:
        raise ReviewRefusedError(f"the Declared Reviews cannot be read: {exc}") from exc
    return {key: value for key, value in declared.items() if identity in (None, key)}


def _changed_paths(
    changes: ChangedFiles, checkout: Path, declared: dict[str, frozenset[DeclaredReview]]
) -> tuple[frozenset[str], tuple[str, ...]]:
    """What the commit changed that can be reviewed, and the submodules it moved.

    A submodule is one entry in the superproject, so the files changed inside it count. Where
    they are unknown (the Base's pin is not in the initialized submodule) or are themselves a
    nested submodule's pin, every declared file under that folder counts as changed.
    """
    changed = {file.path for file in changes.files if file.status != "D"}
    folders = [sub.path for sub in changes.submodules]
    opaque = [sub.path for sub in changes.submodules if sub.files is None]
    for sub in changes.submodules:
        changed.update(sub.files or ())
    nested = [path for path in changed if (checkout / path).is_dir()]
    opaque += nested
    folders += nested
    for reviews in declared.values():
        for review in reviews:
            changed.update(f for f in review.files if any(under_folder(f, o) for o in opaque))
    return frozenset(changed), tuple(folders)


@dataclass(frozen=True)
class _Commit:
    """The commit under review, in the live repository and in its clean export."""

    project_root: Path
    checkout: Path
    base: str
    commit: str
    changed: frozenset[str]
    submodules: tuple[str, ...]  # paths of the submodules whose pin moved


def _run_reviewer(planned: PlannedReview, under: _Commit) -> ReviewResult:
    """Run one planned review in the export; a failed run is an ``error`` result."""
    if not planned.needed:
        return ReviewResult(NOT_NEEDED)
    try:
        return _call_reviewer(planned, under)
    except (OSError, ValueError) as exc:  # Git, evidence or snapshot failure of this review only
        return ReviewResult(ERROR, error=" ".join(str(exc).split())[:_ERROR_LINE])


def run_reviewer(reviewer: ReviewerSpecialist, argv: list[str]) -> EndpointOutcome:
    """Run *reviewer* with *argv* in this process and return its outcome."""
    reviewer.parse_args(argv)
    prepared = reviewer.prepare_endpoint_invocation()
    return prepared.run(None) if isinstance(prepared, PreparedInvocation) else prepared


def failure_text(outcome: EndpointOutcome) -> str:
    """A failed Reviewer run's report on one line, trimmed for the Review Run."""
    text = " ".join(outcome.report_text.split()) or "the Reviewer failed"
    return text[:_ERROR_LINE]


def _call_reviewer(planned: PlannedReview, under: _Commit) -> ReviewResult:
    files = planned.files
    change = committed_review_change(
        under.project_root,
        under.checkout,
        under.base,
        under.commit,
        files,
        under.changed,
        under.submodules,
    )
    argv = [
        *("--project", str(under.checkout)),
        *("--category", planned.role, "--focus", planned.focus),
        *("--scope", ",".join(files), "--fail-on", planned.resolve),
        *(("--spec", planned.spec) if planned.spec else ()),
    ]
    outcome = run_reviewer(ReviewerSpecialist(change=change), argv)
    if outcome.exit_code not in (0, 1):
        return ReviewResult(ERROR, error=failure_text(outcome))
    rows = outcome.detail.get("issue_list", [])
    pre = tuple(row for row in rows if row.get("origin") == ORIGIN_PRE_EXISTING)
    introduced = tuple(row for row in rows if row.get("origin") != ORIGIN_PRE_EXISTING)
    return ReviewResult(OPEN if outcome.exit_code == 1 else PASS, introduced, pre)


def load_live_settings(project_root: Path) -> None:
    """Read the agent settings from the live Project, before any Reviewer run moves the directory.

    Each Reviewer run selects the export with --project, which moves the process's Booley
    Directory there; the model must be the Project's and not the reviewed commit's.
    """
    from booley.config.agent import get_agent_settings
    from booley.core.booley_dir import resolve_booley_dir

    resolve_booley_dir(project_root)
    get_agent_settings()


@contextmanager
def exported_commit(
    project_root: Path, commit: str, main: Path, run_id: str
) -> Iterator[tuple[Path, CommitExport]]:
    """A clean, prepared export of *commit*; it and the cached Booley Directory go on exit."""
    from booley.core.booley_dir import reset_cache

    scratch = Path(tempfile.mkdtemp(prefix="booley-review-"))
    try:
        checkout = scratch / "checkout"
        try:
            export = export_commit(project_root, commit, checkout)
        except CommitExportError as exc:
            raise ReviewRefusedError(f"the commit cannot be exported: {exc}") from exc
        _prepare_checkout(checkout, main, run_id)
        yield checkout, export
    finally:
        reset_cache()  # it names the export, which is deleted next
        shutil.rmtree(scratch, ignore_errors=True)


def _review(project_root: Path, request: ReviewRequest) -> int:
    from booley.core.booley_dir import main_booley_dir_of

    load_live_settings(project_root)
    started_at = datetime.now(UTC)
    commit = resolve_commit(project_root, request.revision)
    base, base_commit = resolve_base_of(project_root, commit, request.base)
    key_of(project_root, commit)  # a refusal now saves the export
    main = main_booley_dir_of(project_root)
    run_id = new_run_id()
    with exported_commit(project_root, commit, main, run_id) as (checkout, export):
        keyed = key_of(project_root, commit, export.paired_commit)
        declared = load_reviews(checkout, request.target)
        changed, folders = _changed_paths(
            changed_files(project_root, base_commit, commit), checkout, declared
        )
        under = _Commit(project_root, checkout, base_commit, commit, changed, folders)
        results = tuple(
            (planned, _run_reviewer(planned, under))
            for planned in plan_reviews(declared, changed, checkout)
        )
    facts = RunFacts(
        run_id,
        rfc3339_from_datetime(started_at),
        rfc3339_from_datetime(datetime.now(UTC)),
        commit,
        keyed.booley_commit,
        base,
        tuple(sorted(changed)),
        results,
    )
    return _report(main, request, review_document(facts), results)


def _prepare_checkout(checkout: Path, main: Path, run_id: str) -> None:
    """Run the post-setup hook, then let Git record the export so the Reviewer can snapshot it."""
    try:
        run_post_setup_hook(
            checkout, main, slug=run_id, log=standalone_log_root(main) / run_id / "post-setup.log"
        )
    except ValueError as exc:  # includes CheckoutFlowError and FlowConfigError
        raise ReviewRefusedError(str(exc)) from exc
    # The export stays the clean checkout; Git only lists its files. Forced, so a tracked
    # file the commit's .gitignore matches and an input the hook built stay visible.
    initialize_snapshot_git(checkout, include_ignored=True)


def _report(
    main: Path,
    request: ReviewRequest,
    document: dict[str, Any],
    results: tuple[tuple[PlannedReview, ReviewResult], ...],
) -> int:
    """Store the run (unless ``--target``), print it, and return the exit status."""
    where = "not stored (--target)"
    if request.target is None:
        where = f"Review Run {store_review_run(main, document)}"
    if request.as_json:
        print(json.dumps(document, indent=2))
        print(where, file=sys.stderr)
    else:
        print(_summary(document, results, where))
    for planned, result in results:
        if result.error is not None:
            print(
                f"ERROR: {planned.role} {planned.focus} review failed: {result.error}",
                file=sys.stderr,
            )
    return exit_code_of(document["outcome"])


def _summary(
    document: dict[str, Any],
    results: tuple[tuple[PlannedReview, ReviewResult], ...],
    where: str,
) -> str:
    """One line: the outcome, how many reviews ran, and where the run is."""
    skipped = sum(result.outcome == NOT_NEEDED for _, result in results)
    ran = len(results) - skipped
    if ran == 0:
        base = document["base"]["commit"][:_SHORT]
        return (
            f"nothing to review: no Declared Review has a changed file since Base {base}; {where}"
        )
    found = sum(len(result.introduced) for _, result in results)
    return (
        f"{document['outcome']}: {ran} reviews run, {skipped} not needed; "
        f"{found} introduced findings; {where}"
    )
