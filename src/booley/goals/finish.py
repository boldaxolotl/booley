"""Preview Goal completion: frozen proof, publication fence and recoverable pinned history."""

from __future__ import annotations

import hashlib
import os
from collections.abc import Callable
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any

from booley.commit_policy import stealth_enabled
from booley.core.boundary import BoundaryError, require_str_value
from booley.criteria.state import DevelopmentState
from booley.goals.apply import ChangeEnvironment, WaiverPort, recover_locked
from booley.goals.checkout import CheckoutError, GoalCheckout, branch_ref
from booley.goals.completion_capture import legacy_materialization_buffers, public_capture
from booley.goals.completion_history import (
    associate_completion,
    earlier_attempt_facts,
    retained_outputs,
    validate_canonical_ownership,
)
from booley.goals.finish_presentation import freeze_presentation, frozen_response, html_bytes
from booley.goals.freshness import DEFAULT_RESOLVERS, RESOLVER_ERRORS, GoalFreshnessResolvers
from booley.goals.generated_artifacts import (
    artifact_epoch,
    presentation_exclusions,
    retained_presentation_inputs,
)
from booley.goals.input_identity import InputIdentityError, directory_identity, require_bindings
from booley.goals.input_view import (
    CommittedView,
    GeneratedBuildInput,
    InputSelection,
    InputSelectionError,
    alias_resolvers,
    capture_path_roots,
    committed_view,
    observations_unchanged,
    require_clean,
    select_inputs,
    topology_digest,
    validate_committed,
)
from booley.goals.lifecycle import (
    LifecycleError,
    LifecycleOperation,
    LifecycleRequest,
    bind_operation,
    read_bound_request,
)
from booley.goals.model import GoalRecord, GoalState
from booley.goals.paths import record_paths
from booley.goals.proposals import digest, encode
from booley.goals.protected_inputs import ProtectedInputRoots, protected_input_violations
from booley.goals.publication_destination import require_destination as _require_destination
from booley.goals.review_package import (
    build_goal_package,
    completion_authority_digest,
    presentation_observations,
)
from booley.goals.state_store import load_goal_state
from booley.goals.status import build_status
from booley.goals.target_changes import (
    project_target_changes,
    resolved_goal_targets,
    resolved_surfaces,
)
from booley.review.goal_package import (
    GoalCompletionPackage,
    GoalReviewContext,
    render_goal_briefing,
)
from booley.runtime.atomic_files import atomic_write_once
from booley.runtime.history_commit import FileCommitError
from booley.runtime.job_records import JobRecordError
from booley.runtime.job_wait import active_jobs
from booley.runtime.pinned_history import (
    PinnedPublication,
    proves_publication,
    publish_pinned,
    raw_git,
    require_branch,
    restore_publication_blob,
    restore_publication_objects,
    synchronize_index,
    tree_rows,
    validate_pin,
)
from booley.runtime.project_repositories import RepositoryCheckoutError
from booley.runtime.publication_ownership import PublicationOwnership
from booley.runtime.timefmt import utc_now_rfc3339
from booley.targets.goal_diff import semantic_target_changes


@dataclass(frozen=True)
class FinishEnvironment:
    """Domain callers supply shared apply authority and current freshness composition."""

    changes: ChangeEnvironment
    resolvers: GoalFreshnessResolvers = DEFAULT_RESOLVERS
    on_boundary: Callable[[str], None] | None = None
    waiver_factory: Callable[[GoalRecord], WaiverPort] | None = None
    generated_inputs: (
        Callable[[GoalRecord, DevelopmentState, Path], tuple[GeneratedBuildInput, ...]] | None
    ) = None

    def change_environment(self, record: GoalRecord) -> ChangeEnvironment:
        """Compose record-specific waiver authority only after operation binding."""
        return (
            self.changes
            if self.waiver_factory is None
            else replace(self.changes, waivers=self.waiver_factory(record))
        )

    def checkpoint(self, name: str) -> None:
        """Every externally visible boundary has deterministic crash injection."""
        if self.on_boundary is not None:
            self.on_boundary(name)


def finish_goal(request: LifecycleRequest, env: FinishEnvironment) -> dict[str, Any]:
    """Bind first, then resume the exact operation or freeze a new validated attempt."""
    store = env.changes.store
    with bind_operation(store, request) as operation:
        saved = operation.saved_result()
        if saved is not None:
            return saved
        try:
            recovered = recover_linked_operation(operation, env)
            if recovered is not None:
                return recovered
            if request.abandon:
                from booley.goals.abandon import abandon_locked

                return abandon_locked(operation, env)
            return finish_locked(operation, env)
        except (
            FileCommitError,
            CheckoutError,
            RepositoryCheckoutError,
            BoundaryError,
            JobRecordError,
        ) as exc:
            raise LifecycleError(str(exc)) from exc


def finish_locked(operation: LifecycleOperation, env: FinishEnvironment) -> dict[str, Any]:
    """Finish under an already bound worktree/record operation."""
    record = operation.store.load(operation.request.record_id)
    attempt = operation.directory / "attempt.json"
    if (
        record.state is GoalState.FINISHED
        and record.finish_operation == operation.request.operation_id
    ):
        return _save_finished_result(operation, _read_attempt(operation))
    if record.state is GoalState.FINISHING:
        if record.finish_operation != operation.request.operation_id:
            prior = _prior_operation(operation, record)
            operation.write_sealed("recovery", {"operation_id": prior.request.operation_id})
            recovered = recover_finish(prior, env)
            if recovered["status"] == "finished":
                return operation.save_recovered_result(recovered)
            record = operation.store.load(record.id)
        else:
            return recover_finish(operation, env)
    if record.state is not GoalState.ACTIVE:
        raise LifecycleError(f"Goal Mode {record.id} is {record.state.value}; cannot finish")
    recover_locked(operation.lock, env.change_environment(record))
    record = operation.store.load(record.id)
    if attempt.exists() or (operation.directory / "attempt.authority.json").exists():
        frozen = _read_attempt(operation)
        if not _attempt_authority_current(operation, record, frozen):
            return _revalidation(
                operation,
                record,
                "record changed before finishing fence",
                cutoff=frozen["record_revision"] + 1,
            )
    else:
        frozen = _freeze(operation, record, env)
        operation.write_sealed("attempt", frozen)
        env.checkpoint("attempt-frozen")
    operation.store.save(
        operation.lock,
        replace(
            record,
            state=GoalState.FINISHING,
            validated_head=frozen["inputs"]["rtl_pin"],
            package_digest="sha256:" + frozen["package_digest"],
            finish_attempt_digest="sha256:" + digest(frozen),
            publication_floor=record.revision + 1,
            finish_operation=operation.request.operation_id,
        ),
    )
    env.checkpoint("finishing")
    return recover_finish(operation, env)


def _attempt_authority_current(
    operation: LifecycleOperation, record: GoalRecord, frozen: dict[str, Any]
) -> bool:
    return frozen["record_revision"] == record.revision and frozen[
        "authority_digest"
    ] == completion_authority_digest(
        record, load_goal_state(operation.store, record), operation.store.project_dir
    )


def _freeze(
    operation: LifecycleOperation, record: GoalRecord, env: FinishEnvironment
) -> dict[str, Any]:
    selection = select_inputs(record, operation.root)
    require_clean(selection, retained=retained_outputs(operation))
    require_branch(operation.root, branch_ref(record.branch))
    status = build_status(
        operation.store,
        record,
        work_dir=operation.root,
        resolvers=alias_resolvers(record, selection, operation.store.project_dir, env.resolvers),
    )
    unmet = [row.key for row in status.goals if row.status != "met"]
    if unmet:
        raise LifecycleError("Goals must be met and fresh before finish: " + ", ".join(unmet))
    if status.pending_proposals:
        raise LifecycleError("resolve pending Goal proposals before finish")
    if not operation.request.summary.strip():
        raise LifecycleError("finish requires a nonblank Session Summary")
    package, capture = _build_frozen_package(operation, record, env, selection)
    if select_inputs(record, operation.root) != selection:
        raise LifecycleError("input participants moved while freezing completion")
    require_clean(selection, retained=retained_outputs(operation))
    summary = render_goal_briefing(package).encode("utf-8")
    path = _publication_path(operation, record)
    _require_destination(operation.root / path, summary, allow_owned=False)
    skip = _skip_publication(operation.root, selection.project, path)
    if skip is None:
        _publication_preflight(operation, path, env)
    return _frozen_attempt(operation, record, selection, package, capture, summary, path, skip)


def _publication_preflight(
    operation: LifecycleOperation, path: str, env: FinishEnvironment
) -> None:
    """Deterministic publication prerequisites precede the permanent Job fence."""
    raw_git(operation.root, "var", "GIT_AUTHOR_IDENT")
    raw_git(operation.root, "var", "GIT_COMMITTER_IDENT")
    PublicationOwnership(
        operation.directory, operation.write_sealed, operation.read_sealed, env.checkpoint
    ).validate_summary_anchor(operation.root / path)


def _frozen_attempt(
    operation: LifecycleOperation,
    record: GoalRecord,
    selection: InputSelection,
    package: GoalCompletionPackage,
    capture: dict[str, Any],
    summary: bytes,
    path: str,
    skip: str | None,
) -> dict[str, Any]:
    frozen = {
        "schema": "booley.goal-finish-attempt/v2",
        "private_capture": capture,
        "operation_id": operation.request.operation_id,
        "record_revision": record.revision,
        "authority_digest": completion_authority_digest(
            record, load_goal_state(operation.store, record), operation.store.project_dir
        ),
        "inputs": selection.to_json(),
        "package": package.to_json(),
        "package_digest": digest(package.to_json()),
        "summary_hex": summary.hex(),
        "summary_digest": hashlib.sha256(summary).hexdigest(),
        "path": path,
        "destination_before": "absent",
        "mode": "100644",
        "skip_publication": skip,
        "worktree": record.worktree.to_json(),
        "branch": branch_ref(record.branch),
        "running_jobs": _running_jobs(
            record_paths(operation.store.project_dir, record.id).jobs_dir
        ),
    }
    freeze_presentation(operation, frozen)
    validate_canonical_ownership(operation, frozen)
    return frozen


def _build_frozen_package(
    operation: LifecycleOperation,
    record: GoalRecord,
    env: FinishEnvironment,
    selection: InputSelection,
) -> tuple[GoalCompletionPackage, dict[str, Any]]:
    """Keep classification and resolver failures at the Finish lifecycle boundary."""
    try:
        return _build_frozen_package_checked(operation, record, env, selection)
    except RESOLVER_ERRORS as exc:
        raise LifecycleError(f"completion inputs cannot be resolved: {exc}") from exc


def _build_frozen_package_checked(
    operation: LifecycleOperation,
    record: GoalRecord,
    env: FinishEnvironment,
    selection: InputSelection,
) -> tuple[GoalCompletionPackage, dict[str, Any]]:
    state = load_goal_state(operation.store, record)
    generated = (
        () if env.generated_inputs is None else env.generated_inputs(record, state, operation.root)
    )
    policy = any(goal.spec.target is None for goal in record.goals)
    resolvers = alias_resolvers(record, selection, operation.store.project_dir, env.resolvers)
    excluded = resolvers.select_artifacts(selection.rtl, None) if policy else frozenset()
    resolvers = replace(resolvers, artifact_paths=lambda root: excluded)
    with committed_view(
        selection, record, control_project=operation.store.project_dir, baseline=True
    ) as base:
        before = resolved_surfaces(base.root)
        before_epoch = artifact_epoch(base, record, baseline=True) if policy else None
    with committed_view(selection, record, control_project=operation.store.project_dir) as final:
        validate_committed(
            final,
            record,
            state.criteria,
            resolvers,
            generated,
        )
        target_changes = semantic_target_changes(before, resolved_surfaces(final.root))
        target_changes, omitted = _project_completion_changes(
            operation, record, state, final, before_epoch, target_changes
        )
        proof = _input_proof(selection, record, final)
        context = GoalReviewContext(
            record.to_json(),
            record.base_sha,
            selection.rtl_pin,
            record.branch,
            str(record_paths(operation.store.project_dir, record.id).logs_dir),
            operation.request.summary,
            public_capture(proof),
            target_changes,
            _participant_diff(selection),
            resolved_goal_targets(final.root, record),
        )
        package = build_goal_package(
            context, record, state, operation.store.project_dir, omitted_inputs=omitted
        )
    return GoalCompletionPackage.from_json(
        {**package.to_json(), "earlier_attempts": earlier_attempt_facts(operation)}
    ), proof


def _project_completion_changes(operation, record, state, final, before_epoch, changes):
    """Apply Goal presentation policy only after resolving the raw semantic delta."""
    if before_epoch is None:
        return changes, frozenset()
    after_epoch = artifact_epoch(final, record, baseline=False)
    observations = presentation_observations(record, state, operation.store.project_dir)
    retained = retained_presentation_inputs(final, observations)
    labels, omitted = presentation_exclusions(
        final.selection.rtl, (before_epoch, after_epoch), retained
    )
    return project_target_changes(changes, *labels), omitted


def _input_proof(
    selection: InputSelection, record: GoalRecord, final: CommittedView
) -> dict[str, Any]:
    """Capture the exact private inputs before constructing their public projection."""
    return {
        **selection.to_json(),
        "nonversioned_observations": final.observations,
        "committed_materializations": final.materializations,
        "path_roots": final.current_roots,
        "original_project_snapshot": None
        if record.project_snapshot is None
        else dict(record.project_snapshot),
        "nonversioned_project_baseline": "entry snapshot"
        if record.project_snapshot is not None
        else "versioned bases, or unavailable for legacy nonversioned Project",
    }


def _participant_diff(selection: InputSelection) -> list[dict[str, Any]]:
    rows = _diff_rows(selection.rtl, selection.rtl_base, selection.rtl_pin, "rtl")
    if selection.project_repository is not None:
        assert selection.project_base is not None and selection.project_pin is not None
        rows += _diff_rows(
            selection.project_repository, selection.project_base, selection.project_pin, "project"
        )
    return rows


def _diff_rows(root: Path, base: str, head: str, participant: str) -> list[dict[str, Any]]:
    before, after = tree_rows(root, base), tree_rows(root, head)
    return [
        {
            "path": participant + "/" + os.fsdecode(name),
            "repository": participant,
            "relative_path": os.fsdecode(name),
            "change": "added"
            if name not in before
            else "removed"
            if name not in after
            else "modified",
        }
        for name in sorted(before.keys() | after.keys())
        if before.get(name) != after.get(name)
    ]


def _publication_path(operation: LifecycleOperation, record: GoalRecord) -> str:
    operations = operation.lock.record_dir / "operations"
    attempts = list(operations.glob("*/attempt.authority.json")) or list(
        operations.glob("*/attempt.json")
    )
    suffix = "" if not attempts else "-" + operation.request.operation_id
    return f".booley_project/goals/history/{record.id}{suffix}.md"


def _skip_publication(root: Path, project: Path, path: str) -> str | None:
    if stealth_enabled(root, project_dir=project):
        return "summary kept local; Stealth publication is skipped"
    result = GoalCheckout(root)._git("check-ignore", "-q", "--", path, check=False)  # pyright: ignore[reportPrivateUsage]
    if result.returncode not in {0, 1}:
        raise LifecycleError(
            f"cannot inspect summary exclusion: git check-ignore exited {result.returncode}: {result.stderr.strip()}"
        )
    return (
        "summary kept local; `.booley_project` is git-excluded" if result.returncode == 0 else None
    )


def _read_attempt(operation: LifecycleOperation) -> dict[str, Any]:
    frozen = operation.read_sealed("attempt")
    from booley.goals.finish_attempt import validate_attempt, validate_local_artifacts

    validate_attempt(frozen, operation)
    validate_local_artifacts(frozen, operation)
    _publication_intent(operation, frozen)
    return frozen


def recover_finish(operation: LifecycleOperation, env: FinishEnvironment) -> dict[str, Any]:
    """Recognize exact own effects before generic dirtiness; retain every prior attempt."""
    frozen = _read_attempt(operation)
    record = operation.store.load(operation.request.record_id)
    content = bytes.fromhex(frozen["summary_hex"])
    destination = operation.root / frozen["path"]
    owned = _require_destination(destination, content)
    publication = _publication_intent(operation, frozen)
    head = require_branch(operation.root, frozen["branch"])
    if legacy_materialization_buffers(frozen):
        return _recover_legacy(operation, record, frozen, publication, head, owned, env)
    try:
        current = select_inputs(record, operation.root)
    except InputSelectionError as exc:
        return _recover_identity_mismatch(
            operation, record, frozen, publication, head, owned, env, str(exc)
        )
    expected = frozen["inputs"]
    if current.topology != expected["topology"] or record.worktree.to_json() != frozen["worktree"]:
        raise LifecycleError("finish topology/worktree identity changed; reconcile before retry")
    require_branch(operation.root, frozen["branch"])
    publication = _publication_intent(operation, frozen)
    own_head = _owns_publication_head(operation.root, current.rtl_pin, publication)
    if current.project_pin != expected["project_pin"] or (
        current.rtl_pin != expected["rtl_pin"] and not own_head
    ):
        return _revalidate_publication(
            operation, record, frozen, env, "a participant moved beyond the frozen final pin"
        )
    require_clean(
        current,
        owned=destination if owned or own_head else None,
        retained=retained_outputs(operation),
        preserve_publication_staging=own_head,
    )
    try:
        unchanged = _inputs_unchanged(operation, record, frozen)
    except (InputIdentityError, OSError) as exc:
        return _recover_identity_mismatch(
            operation, record, frozen, publication, head, owned, env, str(exc)
        )
    if not unchanged:
        return _revalidate_publication(
            operation, record, frozen, env, "a frozen nonversioned input changed"
        )
    _write_local_artifacts(operation, frozen, env)
    if frozen["skip_publication"] is None:
        _publish(operation, frozen, env)
    return _finish_terminal(operation, record, frozen, env)


def _owns_publication_head(root: Path, head: str, publication: PinnedPublication | None) -> bool:
    return (
        publication is not None
        and head == publication.intended_commit
        and proves_publication(root, head, publication)
    )


def _recover_identity_mismatch(
    operation: LifecycleOperation,
    record: GoalRecord,
    frozen: dict[str, Any],
    publication: PinnedPublication | None,
    head: str,
    owned: bool,
    env: FinishEnvironment,
    reason: str,
) -> dict[str, Any]:
    """Classify existing effects, never publish a summary/ref against substituted inputs."""
    if publication is not None and head == publication.intended_commit:
        from booley.goals.finish_attempt import validate_local_artifacts
        from booley.runtime.project_repositories import paired_project_repository

        if not owned or not proves_publication(operation.root, head, publication):
            raise LifecycleError("substituted participant has unproved publication effects")
        validate_local_artifacts(frozen, operation, required=True)
        if not _control_binding_unchanged(operation, frozen):
            return _revalidate_publication(operation, record, frozen, env, reason)
        paired = paired_project_repository(operation.root)
        pin = None if paired is None else GoalCheckout(paired.worktree).head_sha()
        if pin == frozen["inputs"]["project_pin"]:
            return _complete_proven_publication(operation, record, frozen, publication, env)
    elif head != frozen["inputs"]["rtl_pin"]:
        raise LifecycleError(
            "substituted participant has foreign/unclassified publication effects"
        )
    return _revalidate_publication(
        operation,
        record,
        frozen,
        env,
        "input participant identity changed; retained attempt permits abandonment: " + reason,
    )


def _complete_proven_publication(
    operation: LifecycleOperation,
    record: GoalRecord,
    frozen: dict[str, Any],
    publication: PinnedPublication,
    env: FinishEnvironment,
) -> dict[str, Any]:
    """Recognize a proven immutable publication without certifying substituted inputs."""
    synchronize_index(
        operation.root,
        publication,
        ownership=PublicationOwnership(
            operation.directory, operation.write_sealed, operation.read_sealed, env.checkpoint
        ),
    )
    operation.store.save(
        operation.lock, replace(record, state=GoalState.FINISHED, ended_at=utc_now_rfc3339())
    )
    env.checkpoint("finished")
    return _save_finished_result(operation, frozen)


def _control_binding_unchanged(operation: LifecycleOperation, frozen: dict[str, Any]) -> bool:
    capture = frozen.get("private_capture", frozen["package"]["input_proof"])
    control = capture.get("path_roots", {}).get("control")
    return control is None or control["identity"] == directory_identity(
        operation.store.project_dir
    )


def _revalidate_publication(
    operation: LifecycleOperation,
    record: GoalRecord,
    frozen: dict[str, Any],
    env: FinishEnvironment,
    reason: str,
) -> dict[str, Any]:
    """Retain a proven publication without leaving its own summary staged for deletion."""
    publication = _publication_intent(operation, frozen)
    if publication is not None and _original_input_bindings(operation, frozen):
        head = require_branch(operation.root, frozen["branch"])
        destination = operation.root / frozen["path"]
        if (
            head == publication.intended_commit
            and _require_destination(destination, bytes.fromhex(frozen["summary_hex"]))
            and proves_publication(operation.root, head, publication)
        ):
            from booley.goals.finish_attempt import validate_local_artifacts

            validate_local_artifacts(frozen, operation, required=True)
            synchronize_index(
                operation.root,
                publication,
                ownership=PublicationOwnership(
                    operation.directory,
                    operation.write_sealed,
                    operation.read_sealed,
                    env.checkpoint,
                ),
            )
    return _revalidation(operation, record, reason)


def _original_input_bindings(operation: LifecycleOperation, frozen: dict[str, Any]) -> bool:
    """Content invalidation never substitutes for independent physical-root authority."""
    capture = frozen.get("private_capture", frozen["package"]["input_proof"])
    saved = capture.get("path_roots")
    if saved is None:
        return False  # Legacy captures without physical authority cannot authorize index writes.
    try:
        current = capture_path_roots(operation.root, operation.store.project_dir)
        require_bindings(saved, current)
        from booley.runtime.project_repositories import paired_project_repository

        paired = paired_project_repository(operation.root)
        if (
            topology_digest(
                operation.root,
                Path(current["project"]["path"]),
                None if paired is None else paired.worktree,
            )
            != frozen["inputs"]["topology"]
        ):
            return False
    except InputIdentityError:
        return False
    return True


def _recover_legacy(
    operation: LifecycleOperation,
    record: GoalRecord,
    frozen: dict[str, Any],
    publication: PinnedPublication | None,
    head: str,
    owned: bool,
    env: FinishEnvironment,
) -> dict[str, Any]:
    """Preserve proven historical publication; require a private-capture recipe before new effects."""
    if publication is not None and head == publication.intended_commit:
        return _recover_identity_mismatch(
            operation,
            record,
            frozen,
            publication,
            head,
            owned,
            env,
            "legacy publication participant requires revalidation",
        )
    if head != frozen["inputs"]["rtl_pin"]:
        raise LifecycleError("legacy participant has foreign/unclassified publication effects")
    return _revalidation(
        operation,
        record,
        "unpublished legacy attempt contains private materialization buffers; "
        "start a fresh finish operation for the hash-only public package",
    )


def _finish_terminal(
    operation: LifecycleOperation,
    record: GoalRecord,
    frozen: dict[str, Any],
    env: FinishEnvironment,
) -> dict[str, Any]:
    """Read pins/topology again after every local or Git effect, including skipped publication."""
    current = select_inputs(record, operation.root)
    require_branch(operation.root, frozen["branch"])
    if current.topology != frozen["inputs"]["topology"]:
        raise LifecycleError("input topology changed before terminal save")
    publication = _publication_intent(operation, frozen)
    expected = frozen["inputs"]["rtl_pin"] if publication is None else publication.intended_commit
    if current.rtl_pin != expected or current.project_pin != frozen["inputs"]["project_pin"]:
        return _revalidation(operation, record, "a participant moved before terminal save")
    destination = operation.root / frozen["path"]
    owned = _require_destination(destination, bytes.fromhex(frozen["summary_hex"]))
    require_clean(
        current,
        owned=destination if owned else None,
        retained=retained_outputs(operation),
        preserve_publication_staging=publication is not None and owned,
    )
    if not _inputs_unchanged(operation, record, frozen):
        return _revalidation(
            operation, record, "a frozen nonversioned input changed before terminal save"
        )
    operation.store.save(
        operation.lock, replace(record, state=GoalState.FINISHED, ended_at=utc_now_rfc3339())
    )
    env.checkpoint("finished")
    return _save_finished_result(operation, frozen)


def _inputs_unchanged(
    operation: LifecycleOperation, record: GoalRecord, frozen: dict[str, Any]
) -> bool:
    violations = protected_input_violations(
        record.protected_paths,
        record.protected_digest or "",
        record.protected_head_digest,
        ProtectedInputRoots(operation.root, operation.store.project_dir).with_input_paths(
            record.input_paths
        ),
    )
    return not violations and observations_unchanged(
        frozen.get("private_capture", frozen["package"]["input_proof"]),
        capture_path_roots(operation.root, operation.store.project_dir),
    )


def _write_local_artifacts(
    operation: LifecycleOperation, frozen: dict[str, Any], env: FinishEnvironment
) -> None:
    atomic_write_once(operation.directory / "SUMMARY.md", bytes.fromhex(frozen["summary_hex"]))
    atomic_write_once(operation.directory / "review-package.json", encode(frozen["package"]))
    if operation.request.explain_html:
        atomic_write_once(operation.directory / "explanation.html", html_bytes(frozen))
    env.checkpoint("local-artifacts")


def _publication_intent(
    operation: LifecycleOperation, frozen: dict[str, Any]
) -> PinnedPublication | None:
    path = operation.directory / "publication.json"
    present = path.exists() or path.with_name("publication.authority.json").exists()
    commit = operation.directory / "intended-commit.json"
    if not present:
        if commit.exists() or commit.with_name("intended-commit.authority.json").exists():
            raise LifecycleError("intended commit has no bound publication intent")
        return None
    if frozen["skip_publication"] is not None:
        raise LifecycleError("skipped publication has an unexpected Git intent")
    value = operation.read_sealed("publication")
    expected = {
        "mode": "100644",
        "repository": frozen["inputs"]["roots"]["rtl"],
        "worktree": frozen["worktree"],
        "branch": frozen["branch"],
        "pin": frozen["inputs"]["rtl_pin"],
        "path": frozen["path"],
        "operation_id": operation.request.operation_id,
    }
    if set(value) != {*expected, "blob"} or {key: value.get(key) for key in expected} != expected:
        raise LifecycleError("publication intent differs from the frozen operation")
    return _validated_publication_objects(
        operation, frozen, require_str_value(value["blob"], field="publication blob")
    )


def _validated_publication_objects(
    operation: LifecycleOperation, frozen: dict[str, Any], blob: str
) -> PinnedPublication:
    intended, recipe = _read_commit_recipe(operation)
    publication = PinnedPublication(
        frozen["inputs"]["rtl_pin"], frozen["branch"], frozen["path"], blob, intended
    )
    content = bytes.fromhex(frozen["summary_hex"])
    try:
        if raw_git(operation.root, "cat-file", "blob", blob) != content:
            raise LifecycleError("publication blob differs from the frozen summary")
        if intended is not None:
            validate_pin(operation.root, intended)
    except FileCommitError as exc:
        _restore_missing_publication(operation, frozen, publication, content, recipe, exc)
    if intended is not None and not proves_publication(operation.root, intended, publication):
        raise LifecycleError("intended commit differs from its frozen publication")
    return publication


def _read_commit_recipe(operation: LifecycleOperation) -> tuple[str | None, bytes | None]:
    commit = operation.directory / "intended-commit.json"
    if not commit.exists() and not commit.with_name("intended-commit.authority.json").exists():
        return None, None
    saved = operation.read_sealed("intended-commit")
    if set(saved) not in ({"commit"}, {"commit", "raw_commit_hex"}):
        raise LifecycleError("invalid intended commit journal")
    intended = require_str_value(saved["commit"], field="intended publication commit")
    if "raw_commit_hex" not in saved:
        return intended, None
    try:
        recipe = bytes.fromhex(require_str_value(saved["raw_commit_hex"], field="commit recipe"))
    except ValueError as exc:
        raise LifecycleError("invalid exact publication commit recipe") from exc
    if (
        raw_git(operation.root, "hash-object", "-t", "commit", "--stdin", input_bytes=recipe)
        .strip()
        .decode("ascii")
        != intended
    ):
        raise LifecycleError("saved publication commit bytes differ from their original OID")
    return intended, recipe


def _restore_missing_publication(
    operation: LifecycleOperation,
    frozen: dict[str, Any],
    publication: PinnedPublication,
    content: bytes,
    recipe: bytes | None,
    error: FileCommitError,
) -> None:
    if recipe is None and publication.intended_commit is not None:
        raise LifecycleError(
            "publication objects unavailable; legacy hash-only journal cannot "
            "reconstruct the exact commit: " + str(error)
        ) from error
    if not _original_input_bindings(operation, frozen):
        raise LifecycleError(
            "publication object recovery requires original physical input bindings"
        ) from error
    if require_branch(operation.root, frozen["branch"]) not in {
        publication.pin,
        publication.intended_commit,
    }:
        raise LifecycleError(
            "publication object recovery requires its original branch pin"
        ) from error
    if publication.intended_commit is None:
        restore_publication_blob(operation.root, publication.blob, content)
    else:
        assert recipe is not None
        restore_publication_objects(operation.root, publication, content, recipe)


def _publish(
    operation: LifecycleOperation, frozen: dict[str, Any], env: FinishEnvironment
) -> None:
    source = operation.directory / "SUMMARY.md"
    publication = _prepare_publication(operation, frozen, env, source)
    assert publication is not None
    content = bytes.fromhex(frozen["summary_hex"])
    destination = operation.root / frozen["path"]
    _require_destination(destination, content)
    # Materialization precedes CAS; recovery explicitly recognizes only these exact bytes.
    ownership = PublicationOwnership(
        operation.directory, operation.write_sealed, operation.read_sealed, env.checkpoint
    )
    ownership.materialize(destination, content)
    env.checkpoint("destination-materialized")

    def journal(commit: str) -> None:
        operation.write_sealed(
            "intended-commit",
            {
                "commit": commit,
                "raw_commit_hex": raw_git(operation.root, "cat-file", "commit", commit).hex(),
            },
        )

    try:
        publish_pinned(
            operation.root,
            publication,
            f"docs(goals): finish {operation.request.record_id}",
            journal,
            checkpoint=env.checkpoint,
            ownership=ownership,
        )
    except FileCommitError as exc:
        raise LifecycleError(f"finish publication requires recovery: {exc}") from exc


def _prepare_publication(
    operation: LifecycleOperation, frozen: dict[str, Any], env: FinishEnvironment, source: Path
) -> PinnedPublication | None:
    publication = _publication_intent(operation, frozen)
    if publication is None:
        blob = (
            raw_git(operation.root, "hash-object", "-w", "--no-filters", "--", str(source))
            .strip()
            .decode("ascii")
        )
        if raw_git(operation.root, "cat-file", "blob", blob) != bytes.fromhex(
            frozen["summary_hex"]
        ):
            raise LifecycleError("literal publication blob differs from frozen summary")
        operation.write_sealed(
            "publication",
            {
                "blob": blob,
                "mode": "100644",
                "repository": frozen["inputs"]["roots"]["rtl"],
                "worktree": frozen["worktree"],
                "branch": frozen["branch"],
                "pin": frozen["inputs"]["rtl_pin"],
                "path": frozen["path"],
                "operation_id": operation.request.operation_id,
            },
        )
        env.checkpoint("publication-intent")
        publication = _publication_intent(operation, frozen)
    return publication


def _revalidation(
    operation: LifecycleOperation, record: GoalRecord, reason: str, *, cutoff: int | None = None
) -> dict[str, Any]:
    operation.store.save(
        operation.lock,
        replace(
            record,
            state=GoalState.ACTIVE,
            publication_floor=max(
                record.publication_floor, record.revision + 1 if cutoff is None else cutoff
            ),
        ),
    )
    return operation.save_result(
        {
            "status": "revalidation_required",
            "record_id": record.id,
            "operation_id": operation.request.operation_id,
            "reason": reason,
            "attempt": str(operation.directory),
            "message": "Prior attempt retained; revalidate with a fresh operation_id and Session Summary.",
        }
    )


def _save_finished_result(operation: LifecycleOperation, frozen: dict[str, Any]) -> dict[str, Any]:
    associate_completion(operation, frozen)
    return operation.save_result(frozen_response(operation, frozen))


def _running_jobs(root: Path) -> list[dict[str, Any]]:
    return [
        {"run_id": job.run_id, "endpoint": job.endpoint, "status": job.status}
        for job in sorted(active_jobs(root), key=lambda item: item.run_id)
    ]


def _prior_operation(operation: LifecycleOperation, record: GoalRecord) -> LifecycleOperation:
    if record.finish_operation is None:
        raise LifecycleError("finishing record has no immutable operation; reconcile explicitly")
    request = read_bound_request(operation, record.finish_operation)
    if request.abandon:
        raise LifecycleError("finishing record cannot reference an abandonment request")
    return LifecycleOperation(operation.store, operation.lock, request, operation.root)


def recover_linked_operation(
    operation: LifecycleOperation, env: FinishEnvironment
) -> dict[str, Any] | None:
    """A recovery caller journals its prior operation before that operation can finish."""
    if not (operation.directory / "recovery.authority.json").exists():
        return None
    link = operation.read_sealed("recovery")
    if set(link) != {"operation_id"} or link["operation_id"] == operation.request.operation_id:
        raise LifecycleError("invalid cross-operation recovery identity")
    request = read_bound_request(operation, link["operation_id"])
    if request.abandon:
        raise LifecycleError("recovery identity must name a finish operation")
    prior = LifecycleOperation(operation.store, operation.lock, request, operation.root)
    result = prior.saved_result()
    if result is None:
        record = operation.store.load(request.record_id)
        if record.finish_operation != request.operation_id:
            raise LifecycleError("recovery identity differs from the durable finishing fence")
        if record.state not in {GoalState.FINISHING, GoalState.FINISHED}:
            return None
        result = (
            _save_finished_result(prior, _read_attempt(prior))
            if record.state is GoalState.FINISHED
            else recover_finish(prior, env)
        )
    return operation.save_recovered_result(result) if result["status"] == "finished" else None


def recover_pending_finish(
    operation: LifecycleOperation, record: GoalRecord, env: FinishEnvironment
) -> dict[str, Any]:
    """Abandonment must resolve finish through its original immutable operation."""
    prior = _prior_operation(operation, record)
    operation.write_sealed("recovery", {"operation_id": prior.request.operation_id})
    return prior.saved_result() or recover_finish(prior, env)
