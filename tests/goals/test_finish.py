"""Production lifecycle services over real Git, real evidence and committed input views."""

from __future__ import annotations

import json
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4

import pytest

from booley.criteria.evidence_ledger import AcceptanceLedgerError
from booley.goals.abandon import abandon_goal
from booley.goals.apply import ChangeEnvironment
from booley.goals.entry import EntryEnvironment, EntryRequest, enter_goal_mode
from booley.goals.finish import FinishEnvironment, finish_goal
from booley.goals.lifecycle import LifecycleError, LifecycleRequest
from booley.goals.model import GoalState, parse_goal_args
from booley.goals.paths import record_paths
from booley.goals.store import GoalStore
from booley.runtime.project_gitignore import PROJECT_GITIGNORE
from tests.goals.conftest import bind, enter_goals, git
from tests.goals.test_status import publish

# These multi-step Git/Reviewer cases retain the established Goal lifecycle budget.
# The measured Windows Reviewer baseline is 27.768s (windows-test-timings.json);
# its 3x/30s-rounded minimum is 90s, above CI's unannotated 60s default.
pytestmark = pytest.mark.timeout(120)


class Crash(BaseException):
    """Model process death after a durable boundary, without rollback."""


def environment(layout: SimpleNamespace, checkpoint=None) -> FinishEnvironment:
    return FinishEnvironment(
        ChangeEnvironment(EntryEnvironment(layout.control)), on_boundary=checkpoint
    )


def request(layout: SimpleNamespace, **options) -> LifecycleRequest:
    return LifecycleRequest(
        layout.worktree,
        layout.record.id,
        str(uuid4()),
        summary="Verified lint for the final committed design.",
        **options,
    )


@pytest.fixture
def complete(layout: SimpleNamespace) -> SimpleNamespace:
    layout.record = enter_goals(layout, [{"family": "lint", "target": "top"}])
    publish(layout)
    git(layout.main, "config", "user.name", "Goal Test")
    git(layout.main, "config", "user.email", "goal@test.invalid")
    return layout


def publication_layout(layout: SimpleNamespace) -> SimpleNamespace:
    (layout.main / ".git/info/exclude").write_bytes(b"")
    git(layout.worktree, "config", "core.excludesfile", str(layout.main / "no-global-excludes"))
    project = layout.worktree / ".booley_project"
    (project / "booley.toml").write_bytes(b"[stealth]\nenabled = false\n")
    (layout.control / "booley.toml").write_bytes(b"[stealth]\nenabled = false\n")
    (project / ".gitignore").write_bytes(PROJECT_GITIGNORE.encode())
    (project / "tests.toml").write_bytes(b'[top]\ntests = ["smoke"]\n')
    git(layout.worktree, "add", ".booley_project")
    git(layout.worktree, "commit", "-qm", "versioned project inputs")
    layout.record = enter_goals(layout, [{"family": "lint", "target": "top"}])
    publish(layout)
    git(layout.main, "config", "user.name", "Goal Test")
    git(layout.main, "config", "user.email", "goal@test.invalid")
    return layout


def test_stealth_completion_is_frozen_and_makes_zero_git_writes(complete):
    call = request(complete, explain_html=True)
    before = git(complete.worktree, "rev-parse", "HEAD")
    object_count = git(complete.worktree, "count-objects", "-v")
    result = finish_goal(call, environment(complete))
    assert result["status"] == "finished"
    assert "Stealth" in result["publication_note"]
    assert git(complete.worktree, "rev-parse", "HEAD") == before
    assert git(complete.worktree, "count-objects", "-v") == object_count
    package = Path(result["package"]).read_bytes()
    assert json.loads(package)["goals"][0]["selected_observation"]["producer"] == "lint"
    assert Path(result["html"]).is_file()
    assert finish_goal(call, environment(complete)) == result
    assert Path(result["package"]).read_bytes() == package
    with pytest.raises(LifecycleError, match="immutable payload"):
        finish_goal(replace(call, summary="Changed summary"), environment(complete))


def test_committed_summary_is_on_worktree_branch_and_main_does_not_move(layout):
    complete = publication_layout(layout)
    main = git(complete.main, "rev-parse", "HEAD")
    before = git(complete.worktree, "rev-parse", "HEAD")
    result = finish_goal(request(complete), environment(complete))
    assert result["status"] == "finished"
    assert git(complete.main, "rev-parse", "HEAD") == main
    assert git(complete.worktree, "rev-parse", "HEAD^1") == before
    assert git(complete.worktree, "status", "--porcelain") == ""
    assert (
        git(complete.worktree, "show", f"HEAD:{result['history_path']}")
        == Path(result["summary"]).read_text().strip()
    )


@pytest.mark.parametrize(
    "boundary",
    [
        "attempt-frozen",
        "finishing",
        "local-artifacts",
        "publication-intent",
        "destination-materialized",
        "commit-journaled",
        "ref-published",
        "index-synchronized",
        "finished",
    ],
)
def test_every_publication_crash_boundary_recovers_exactly_once(layout, boundary):
    complete = publication_layout(layout)
    call = request(complete)
    before = git(complete.worktree, "rev-parse", "HEAD")

    def stop(name):
        if name == boundary:
            raise Crash(name)

    with pytest.raises(Crash):
        finish_goal(call, environment(complete, stop))
    result = finish_goal(call, environment(complete))
    assert result["status"] == "finished"
    assert git(complete.worktree, "rev-parse", "HEAD^1") == before
    assert finish_goal(call, environment(complete)) == result


def test_ignored_nonstealth_is_explicitly_local(complete):
    # Configuration is protected; create a new Goal after the human changes it.
    abandon_goal(GoalStore(complete.control), complete.worktree, environment(complete))
    for root in (complete.control, complete.worktree / ".booley_project"):
        (root / "booley.toml").write_bytes(b"[stealth]\nenabled=false\n")
    complete.record = enter_goal_mode(
        EntryRequest(
            complete.worktree, "again", parse_goal_args([{"family": "lint", "target": "top"}])
        ),
        EntryEnvironment(complete.control),
    ).record
    publish(complete)
    result = finish_goal(request(complete), environment(complete))
    assert "git-excluded" in result["publication_note"]
    assert result["history_path"] is None


@pytest.mark.parametrize(
    "change, message",
    [
        ("dirty", "commit changes"),
        ("branch", "publication needs"),
        ("unmet", "met and fresh"),
        ("protected", "met and fresh"),
        ("blank", "Session Summary"),
    ],
)
def test_finish_refuses_before_publication(complete, change, message):
    call = request(complete)
    if change == "dirty":
        (complete.worktree / "scratch").write_bytes(b"work")
    elif change == "branch":
        git(complete.worktree, "checkout", "-qb", "sibling")
    elif change == "unmet":
        path = record_paths(complete.control, complete.record.id).state_file
        value = json.loads(path.read_bytes())
        value["criteria"]["lint_clean_top"]["met"] = False
        path.write_text(json.dumps(value))
    elif change == "protected":
        (complete.control / "booley.toml").write_bytes(b"[project]\nname='changed'\n")
    else:
        call = replace(call, summary=" ")
    before = git(complete.worktree, "rev-parse", "HEAD")
    with pytest.raises((LifecycleError, ValueError), match=message):
        finish_goal(call, environment(complete))
    assert git(complete.worktree, "rev-parse", "HEAD") == before
    assert GoalStore(complete.control).load(complete.record.id).state is GoalState.ACTIVE


# Native Windows passed call: 43.475s; 3x rounded to 30s requires 150s.
@pytest.mark.timeout(150)
def test_revalidation_retains_attempt_and_permanently_fences_old_run(layout):
    from booley.goals.publication import EvidenceDiscarded, PublicationGate

    complete = publication_layout(layout)
    old = bind(complete, "old-never-published")
    call = request(complete)

    def stop(name):
        if name == "ref-published":
            raise Crash()

    with pytest.raises(Crash):
        finish_goal(call, environment(complete, stop))
    # Reconcile the summary's own index effect first, then add an ordinary commit.
    git(complete.worktree, "add", ".booley_project/goals/history")
    (complete.worktree / "note.txt").write_bytes(b"new commit\n")
    git(complete.worktree, "add", "note.txt")
    git(complete.worktree, "commit", "-qm", "ordinary work")
    result = finish_goal(call, environment(complete))
    assert result["status"] == "revalidation_required"
    assert finish_goal(call, environment(complete)) == result
    with (
        pytest.raises(EvidenceDiscarded, match="lifecycle publication fence"),
        PublicationGate(old).publishing([]),
    ):
        pytest.fail("old run published")
    with PublicationGate(bind(complete, "new-admission")).publishing([]):
        pass
    assert Path(result["attempt"]).joinpath("attempt.json").is_file()
    second = finish_goal(request(complete), environment(complete))
    assert second["status"] == "finished"
    assert second["history_path"] != f".booley_project/goals/history/{complete.record.id}.md"


def test_replacement_entry_does_not_receive_old_retry_or_abandon(complete):
    call = request(complete)
    result = finish_goal(call, environment(complete))
    next_record = enter_goal_mode(
        EntryRequest(
            complete.worktree, "new", parse_goal_args([{"family": "lint", "target": "top"}])
        ),
        EntryEnvironment(complete.control),
    ).record
    before = record_paths(complete.control, next_record.id).record_file.read_bytes()
    assert finish_goal(call, environment(complete)) == result
    with pytest.raises(LifecycleError, match="occupant"):
        finish_goal(
            replace(call, operation_id=str(uuid4()), abandon=True, instruction_quote="Stop"),
            environment(complete),
        )
    assert record_paths(complete.control, next_record.id).record_file.read_bytes() == before


@pytest.mark.parametrize("effect", ["file", "symlink", "ancestor"])
def test_recovery_never_overwrites_external_destination(layout, effect):
    complete = publication_layout(layout)
    call = request(complete)

    def stop(name):
        if name == "finishing":
            raise Crash()

    with pytest.raises(Crash):
        finish_goal(call, environment(complete, stop))
    destination = complete.worktree / f".booley_project/goals/history/{complete.record.id}.md"
    if effect == "ancestor":
        destination.parent.parent.write_bytes(b"conflict")
    else:
        destination.parent.mkdir(parents=True, exist_ok=True)
        if effect == "file":
            destination.write_bytes(b"other bytes")
        else:
            destination.symlink_to(complete.worktree / "rtl.v")
    with pytest.raises(LifecycleError, match="conflict"):
        finish_goal(call, environment(complete))
    assert GoalStore(complete.control).load(complete.record.id).state is GoalState.FINISHING


def test_two_identical_finish_calls_share_one_frozen_result(complete):
    from concurrent.futures import ThreadPoolExecutor

    call = request(complete)
    with ThreadPoolExecutor(max_workers=2) as workers:
        futures = [workers.submit(finish_goal, call, environment(complete)) for _ in range(2)]
        results = [future.result(timeout=30) for future in futures]
    assert results[0] == results[1]


def test_untracked_ignored_consumed_design_source_is_not_copied_to_committed_view(layout):
    layout.record = enter_goals(layout, [{"family": "lint", "target": "top"}])
    # The user's explicit ignore hides the source from clean-tree checks.
    git(layout.worktree, "rm", "--cached", "rtl.v")
    (layout.main / ".git/info/exclude").write_bytes(b"/.booley_project\nrtl.v\n")
    git(layout.worktree, "commit", "-qm", "stop tracking source")
    publish(layout)
    with pytest.raises(LifecycleError, match="no committed representation"):
        finish_goal(request(layout), environment(layout))


def test_selected_missing_and_substituted_evidence_refuses(complete):
    evidence = next(
        record_paths(complete.control, complete.record.id).logs_dir.glob(
            "acceptance/evidence/*/record.json"
        )
    )
    evidence.unlink()
    with pytest.raises(
        (LifecycleError, ValueError, AcceptanceLedgerError),
        match=r"(evidence|sequence|met and fresh)",
    ):
        finish_goal(request(complete), environment(complete))


def test_summary_is_not_overwritten_when_existing_before_first_attempt(complete):
    destination = complete.worktree / f".booley_project/goals/history/{complete.record.id}.md"
    destination.parent.mkdir(parents=True)
    destination.write_bytes(b"preexisting summary")
    with pytest.raises(LifecycleError, match="destination conflicts"):
        finish_goal(request(complete), environment(complete))
    assert destination.read_bytes() == b"preexisting summary"


def test_late_pre_finish_job_cannot_change_terminal_state_or_package(complete):
    from booley.criteria.state import DevelopmentState
    from booley.goals.publication import EvidenceDiscarded
    from booley.goals.recorder import GoalEvidenceRecorder

    recorder = GoalEvidenceRecorder(bind(complete, "late"))
    state = DevelopmentState.load(
        record_paths(complete.control, complete.record.id).state_file, recorder.state_persistence()
    )
    result = finish_goal(request(complete), environment(complete))
    before = Path(result["package"]).read_bytes()
    with pytest.raises(EvidenceDiscarded):
        state.save()
    assert Path(result["package"]).read_bytes() == before


def test_publication_during_pre_fence_crash_requires_revalidation_and_permanent_fence(complete):
    from booley.goals.publication import EvidenceDiscarded, PublicationGate

    old = bind(complete, "before-attempt")
    call = request(complete)

    def stop(name):
        if name == "attempt-frozen":
            raise Crash()

    with pytest.raises(Crash):
        finish_goal(call, environment(complete, stop))
    publish(complete)
    result = finish_goal(call, environment(complete))
    assert result["status"] == "revalidation_required"
    with (
        pytest.raises(EvidenceDiscarded, match="lifecycle publication fence"),
        PublicationGate(old).publishing([]),
    ):
        pytest.fail("pre-fence admission published after revalidation")
    assert finish_goal(request(complete), environment(complete))["status"] == "finished"


@pytest.mark.parametrize("tamper", ["intent", "blob", "package", "summary"])
def test_recovery_refuses_substituted_immutable_publication_proof(layout, tamper):
    complete = publication_layout(layout)
    call = request(complete)

    def stop(name):
        if name == "publication-intent":
            raise Crash()

    with pytest.raises(Crash):
        finish_goal(call, environment(complete, stop))
    directory = (
        record_paths(complete.control, complete.record.id).root / "operations" / call.operation_id
    )
    if tamper in {"intent", "blob"}:
        path = directory / "publication.json"
        value = json.loads(path.read_bytes())
        if tamper == "intent":
            value["path"] = "other.md"
        else:
            from booley.runtime.pinned_history import raw_git

            value["blob"] = raw_git(complete.worktree, "hash-object", "rtl.v").decode().strip()
    else:
        path = directory / "attempt.json"
        value = json.loads(path.read_bytes())
        if tamper == "package":
            value["package"]["session_summary"] = "substituted"
        else:
            value["summary_hex"] = b"substituted".hex()
    path.write_text(json.dumps(value))
    before = git(complete.worktree, "rev-parse", "HEAD")
    with pytest.raises(LifecycleError, match=r"(substituted|differs)"):
        finish_goal(call, environment(complete))
    assert git(complete.worktree, "rev-parse", "HEAD") == before
