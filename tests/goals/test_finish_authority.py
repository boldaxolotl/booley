"""Persisted lifecycle boundaries refuse tampering before any local or Git effect."""

import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from booley.goals.finish import finish_goal
from booley.goals.lifecycle import LifecycleError
from booley.goals.model import GoalState
from booley.goals.proposals import digest
from booley.goals.store import GoalStore
from booley.review.goal_package import GoalCompletionPackage, GoalPackageError
from tests.goals.conftest import git
from tests.goals.test_finish import Crash, environment, publication_layout, request

pytestmark = pytest.mark.timeout(120)


@pytest.fixture
def complete(layout):
    from tests.goals.conftest import enter_goals
    from tests.goals.test_status import publish

    layout.record = enter_goals(layout, [{"family": "lint", "target": "top"}])
    publish(layout)
    return layout


def directory(layout, call):
    return layout.control / "goals" / layout.record.id / "operations" / call.operation_id


def freeze(layout, boundary):
    call = request(layout, explain_html=True)

    def stop(name):
        if name == boundary:
            raise Crash()

    with pytest.raises(Crash):
        finish_goal(call, environment(layout, stop))
    return call, directory(layout, call)


@pytest.mark.parametrize("boundary", ["attempt-frozen", "finishing"])
@pytest.mark.parametrize("publication", ["normal", "stealth", "excluded"])
@pytest.mark.parametrize("spelling", ["absolute", "traversal", "different-literal"])
def test_saved_attempt_path_tamper_refuses_before_any_effect(
    layout, tmp_path, boundary, publication, spelling
):
    if publication == "normal":
        layout = publication_layout(layout)
    else:
        from tests.goals.conftest import enter_goals
        from tests.goals.test_status import publish

        if publication == "excluded":
            for root in (layout.control, layout.worktree / ".booley_project"):
                (root / "booley.toml").write_bytes(b"[stealth]\nenabled=false\n")
        layout.record = enter_goals(layout, [{"family": "lint", "target": "top"}])
        publish(layout)
    call, operation = freeze(layout, boundary)
    outside = tmp_path / "outside-summary.md"
    attempt = json.loads((operation / "attempt.json").read_bytes())
    attempt["path"] = (
        str(outside)
        if spelling == "absolute"
        else "../../../outside-summary.md"
        if spelling == "traversal"
        else ".booley_project/goals/history/foreign.md"
    )
    (operation / "attempt.json").write_text(json.dumps(attempt))
    index = Path(git(layout.worktree, "rev-parse", "--git-path", "index"))
    before = (
        index.read_bytes(),
        git(layout.worktree, "count-objects", "-v"),
        git(layout.worktree, "rev-parse", "HEAD"),
    )
    with pytest.raises(LifecycleError, match="durable authority"):
        finish_goal(call, environment(layout))
    assert not outside.exists()
    assert not (operation / "SUMMARY.md").exists()
    assert not (operation / "review-package.json").exists()
    assert before == (
        index.read_bytes(),
        git(layout.worktree, "count-objects", "-v"),
        git(layout.worktree, "rev-parse", "HEAD"),
    )
    expected = GoalState.ACTIVE if boundary == "attempt-frozen" else GoalState.FINISHING
    assert GoalStore(layout.control).load(layout.record.id).state is expected


@pytest.mark.parametrize(
    "field",
    [
        "branch",
        "inputs",
        "skip_publication",
        "mode",
        "worktree",
        "record_revision",
        "running_jobs",
        "package",
        "summary_hex",
    ],
)
def test_all_persisted_attempt_fields_are_bound_to_independent_authority(complete, field):
    call, operation = freeze(complete, "finishing")
    attempt = json.loads((operation / "attempt.json").read_bytes())
    replacements = {
        "branch": "refs/heads/foreign",
        "inputs": {},
        "skip_publication": None,
        "mode": "100755",
        "worktree": {},
        "record_revision": -1,
        "running_jobs": [{"status": "foreign"}],
        "package": {},
        "summary_hex": b"foreign".hex(),
    }
    attempt[field] = replacements[field]
    if field == "package":
        attempt["package_digest"] = digest(attempt["package"])
    (operation / "attempt.json").write_text(json.dumps(attempt))
    with pytest.raises(LifecycleError, match="durable authority"):
        finish_goal(call, environment(complete))
    assert not (operation / "SUMMARY.md").exists()


def test_durable_finishing_record_also_binds_entire_attempt(complete):
    call, operation = freeze(complete, "finishing")
    attempt = json.loads((operation / "attempt.json").read_bytes())
    attempt["running_jobs"] = [{"status": "foreign"}]
    (operation / "attempt.json").write_text(json.dumps(attempt))
    seal = json.loads((operation / "attempt.authority.json").read_bytes())
    seal.update(value=attempt, digest=digest(attempt))
    (operation / "attempt.authority.json").write_text(json.dumps(seal))
    with pytest.raises(LifecycleError, match="finishing fence"):
        finish_goal(call, environment(complete))
    assert not (operation / "SUMMARY.md").exists()


def test_complete_authority_recovers_artifact_not_yet_written(complete, monkeypatch):
    import booley.goals.lifecycle as module

    write = module.atomic_write_once

    def stop(path, content, **options):
        if path.name == "attempt.json":
            raise Crash()
        return write(path, content, **options)

    call = request(complete)
    with monkeypatch.context() as patch:
        patch.setattr(module, "atomic_write_once", stop)
        with pytest.raises(Crash):
            finish_goal(call, environment(complete))
    operation = directory(complete, call)
    assert not (operation / "attempt.json").exists()
    assert (operation / "attempt.authority.json").exists()
    assert finish_goal(call, environment(complete))["status"] == "finished"


@pytest.mark.parametrize(
    "field", ["status", "record_id", "operation_id", "package", "running_jobs"]
)
def test_saved_result_substitution_is_not_returned(complete, field):
    call = request(complete)
    finish_goal(call, environment(complete))
    operation = directory(complete, call)
    result = json.loads((operation / "result.json").read_bytes())
    result[field] = {}
    (operation / "result.json").write_text(json.dumps(result))
    with pytest.raises(LifecycleError, match="durable authority"):
        finish_goal(call, environment(complete))


@pytest.mark.parametrize(
    "field",
    [
        "record",
        "diff_summary",
        "input_proof",
        "evidence_transactions",
        "applied_proposal_decisions",
    ],
)
def test_package_reader_refuses_missing_required_facts(complete, field):
    result = finish_goal(request(complete), environment(complete))
    facts = json.loads(Path(result["package"]).read_bytes())
    del facts[field]
    with pytest.raises(GoalPackageError, match="complete"):
        GoalCompletionPackage.from_json(facts)


def test_fatal_ignore_inspection_refuses_before_artifacts_or_git_writes(layout, monkeypatch):
    import booley.goals.finish as module

    layout = publication_layout(layout)
    command = module.GoalCheckout._git

    def fail(self, *args, **options):
        if args[0] == "check-ignore":
            return SimpleNamespace(returncode=128, stderr="fatal: failed ignore inspection")
        return command(self, *args, **options)

    call = request(layout)
    objects = git(layout.worktree, "count-objects", "-v")
    monkeypatch.setattr(module.GoalCheckout, "_git", fail)
    with pytest.raises(LifecycleError, match=r"check-ignore exited 128.*failed ignore"):
        finish_goal(call, environment(layout))
    assert not (directory(layout, call) / "attempt.json").exists()
    assert git(layout.worktree, "count-objects", "-v") == objects


@pytest.mark.parametrize("artifact", ["SUMMARY.md", "review-package.json", "explanation.html"])
def test_substituted_completion_artifact_refuses_before_next_effect(complete, artifact):
    call, operation = freeze(complete, "local-artifacts")
    (operation / artifact).write_bytes(b"substituted artifact")
    with pytest.raises(LifecycleError, match="artifact differs"):
        finish_goal(call, environment(complete))
    assert not (operation / "result.json").exists()


def test_saved_success_also_validates_the_frozen_package(complete):
    call = request(complete)
    result = finish_goal(call, environment(complete))
    Path(result["package"]).write_bytes(b'{"purpose":"booley.goal-completion/v1"}')
    with pytest.raises(LifecycleError, match="artifact differs"):
        finish_goal(call, environment(complete))


def test_legacy_unpaired_record_can_freeze_and_retry_with_current_authority(complete):
    record_file = complete.control / "goals" / complete.record.id / "record.json"
    value = json.loads(record_file.read_bytes())
    del value["input_paths"]
    del value["input_topology_digest"]
    record_file.write_text(json.dumps(value))
    call = request(complete)
    result = finish_goal(call, environment(complete))
    assert result["status"] == "finished"
    assert finish_goal(call, environment(complete)) == result


@pytest.mark.parametrize("journal", ["publication", "intended-commit"])
def test_publication_journal_substitution_refuses_before_next_effect(layout, journal):
    layout = publication_layout(layout)
    call, operation = freeze(
        layout, "publication-intent" if journal == "publication" else "commit-journaled"
    )
    path = operation / (journal + ".json")
    value = json.loads(path.read_bytes())
    value["path" if journal == "publication" else "commit"] = (
        "foreign" if journal == "publication" else layout.record.base_sha
    )
    path.write_text(json.dumps(value))
    head = git(layout.worktree, "rev-parse", "HEAD")
    index = Path(git(layout.worktree, "rev-parse", "--git-path", "index"))
    before = index.read_bytes(), (operation / "SUMMARY.md").stat().st_mtime_ns
    with pytest.raises(LifecycleError, match="durable authority"):
        finish_goal(call, environment(layout))
    assert git(layout.worktree, "rev-parse", "HEAD") == head
    assert before == (index.read_bytes(), (operation / "SUMMARY.md").stat().st_mtime_ns)


def test_skipped_attempt_refuses_unexpected_git_intent_before_artifacts(complete):
    call, operation = freeze(complete, "finishing")
    (operation / "publication.json").write_bytes(b"{}")
    with pytest.raises(LifecycleError, match="skipped publication"):
        finish_goal(call, environment(complete))
    assert not (operation / "SUMMARY.md").exists()
