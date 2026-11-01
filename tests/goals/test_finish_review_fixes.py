"""Lifecycle regressions from the independent Phase 5 review."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from booley.goals.abandon import abandon_goal
from booley.goals.finish import finish_goal
from booley.goals.lifecycle import LifecycleError
from booley.goals.paths import record_paths
from booley.goals.store import GoalStore
from tests.goals.conftest import enter_goals, git, install_paired_project
from tests.goals.test_finish import Crash, environment, publication_layout, request
from tests.goals.test_r1_lifecycle import recreate, stop_at
from tests.goals.test_status import publish

pytestmark = pytest.mark.timeout(120)


def paired_publication(layout, tmp_path):
    from booley.runtime.project_gitignore import PROJECT_GITIGNORE

    git(layout.worktree, "config", "user.name", "Goal Test")
    git(layout.worktree, "config", "user.email", "goal@test.invalid")
    (layout.main / ".git/info/exclude").write_bytes(b"")
    git(layout.worktree, "config", "core.excludesfile", str(layout.main / "no-global-excludes"))
    (layout.control / "booley.toml").write_bytes(b"[stealth]\nenabled = false\n")
    project = layout.worktree / ".booley"
    contents = {
        "booley.toml": b"[stealth]\nenabled = false\n",
        "tests.toml": b'[top]\ntests = ["smoke"]\n',
        ".gitignore": PROJECT_GITIGNORE.encode(),
        "mcp_tools/tool.py": b"# custom tool\n",
    }
    for name, content in contents.items():
        (project / name).parent.mkdir(parents=True, exist_ok=True)
        (project / name).write_bytes(content)
    git(layout.worktree, "add", ".booley")
    git(layout.worktree, "commit", "-qm", "tracked project inputs")
    project = install_paired_project(layout, tmp_path)
    for name, content in contents.items():
        (project / name).parent.mkdir(parents=True, exist_ok=True)
        (project / name).write_bytes(content)
    git(project, "add", "-A")
    git(project, "commit", "-qm", "publication project")
    layout.record = enter_goals(layout, [{"family": "LINT", "target": "top"}])
    publish(layout)
    return layout, project


@pytest.mark.parametrize(
    "abandon, paired",
    [
        pytest.param(False, False, marks=pytest.mark.timeout(120), id="False-False"),
        pytest.param(False, True, marks=pytest.mark.timeout(150), id="False-True"),
        pytest.param(True, False, marks=pytest.mark.timeout(150), id="True-False"),
        pytest.param(True, True, marks=pytest.mark.timeout(150), id="True-True"),
    ],
)
def test_revalidated_publication_retains_history_through_next_commit(
    layout, tmp_path, paired, abandon, monkeypatch
):
    if paired:
        complete, project = paired_publication(layout, tmp_path)
    else:
        complete = publication_layout(layout)
    call = request(complete)
    # Construct an attempt made under the historical paired-publication policy.
    with monkeypatch.context() as old_policy:
        if paired:
            old_policy.setattr("booley.goals.finish._skip_publication", lambda *_: None)
        with pytest.raises(Crash):
            finish_goal(call, environment(complete, stop_at("ref-published")))
    if paired:
        (project / "note.txt").write_bytes(b"later paired commit\n")
        git(project, "add", "note.txt")
        git(project, "commit", "-qm", "paired participant moved")
    else:
        hook = complete.control / "mcp_tools/tool.py"
        hook.write_bytes(hook.read_bytes() + b"# changed after publication\n")
    if abandon:
        hook = complete.control / "mcp_tools/tool.py"
        hook.write_bytes(hook.read_bytes() + b"# changed before abandonment\n")
        result = abandon_goal(
            GoalStore(complete.control), complete.worktree, environment(complete)
        )
        assert result["status"] == "abandoned"
    else:
        assert finish_goal(call, environment(complete))["status"] == "revalidation_required"
    summary = f".booley/goals/history/{call.record_id}.md"
    retained = git(complete.worktree, "show", "HEAD:" + summary)
    assert "D  " + summary not in git(complete.worktree, "status", "--porcelain")
    (complete.worktree / "user-note.txt").write_bytes(b"next user commit\n")
    git(complete.worktree, "add", "user-note.txt")
    git(complete.worktree, "commit", "-qm", "ordinary next commit")
    assert git(complete.worktree, "show", "HEAD:" + summary) == retained


def test_content_violation_does_not_authorize_index_write_to_recreated_control(
    layout, monkeypatch
):
    complete = publication_layout(layout)
    call = request(complete)
    with pytest.raises(Crash):
        finish_goal(call, environment(complete, stop_at("ref-published")))
    # Preserve the actual worktree while replacing its Main Booley Directory's physical root.
    worktrees = complete.control / "worktrees"
    held = complete.control.parent / "held-worktrees"
    worktrees.rename(held)
    original_rename = Path.rename

    def restore_worktrees(source, target):
        if source == held and Path(target).exists():
            raise FileExistsError("Windows rename requires an absent destination")
        return original_rename(source, target)

    monkeypatch.setattr(Path, "rename", restore_worktrees)
    recreate(complete.control)
    held.rename(worktrees)
    hook = complete.control / "mcp_tools/tool.py"
    hook.write_bytes(hook.read_bytes() + b"# protected content also changed\n")
    before = git(complete.worktree, "diff", "--cached", "--raw")
    assert finish_goal(call, environment(complete))["status"] == "revalidation_required"
    assert git(complete.worktree, "diff", "--cached", "--raw") == before


def test_detached_head_finish_reports_required_branch(layout):
    complete = publication_layout(layout)
    git(complete.worktree, "checkout", "--detach")
    with pytest.raises(LifecycleError, match=r"branch|detached"):
        finish_goal(request(complete), environment(complete))


def test_abandoned_retry_reads_sealed_notes_when_projection_is_missing(layout):
    complete = publication_layout(layout)
    call = request(complete, abandon=True, instruction_quote="Stop this Goal now")
    with pytest.raises(Crash):
        finish_goal(call, environment(complete, stop_at("abandoned")))
    directory = complete.control / "goals" / call.record_id / "operations" / call.operation_id
    authority = json.loads((directory / "abandonment.authority.json").read_bytes())
    (directory / "abandonment.json").unlink()
    result = finish_goal(call, environment(complete))
    assert result["status"] == "abandoned"
    assert result["notes"] == authority["value"]["notes"]
    assert finish_goal(call, environment(complete)) == result


@pytest.mark.timeout(150)
def test_recovering_publication_with_broken_paired_git_is_actionable(
    layout, tmp_path, monkeypatch
):
    complete, project = paired_publication(layout, tmp_path)
    call = request(complete)
    with monkeypatch.context() as old_policy:
        old_policy.setattr("booley.goals.finish._skip_publication", lambda *_: None)
        with pytest.raises(Crash):
            finish_goal(call, environment(complete, stop_at("ref-published")))
    marker = project / ".git"
    marker.chmod(marker.stat().st_mode | 0o200)
    marker.unlink()
    marker.write_text("gitdir: /missing-paired-git-administration\n")
    before = git(complete.worktree, "rev-parse", "HEAD")
    with pytest.raises(LifecycleError, match=r"Paired Checkout.*unavailable"):
        finish_goal(call, environment(complete))
    assert git(complete.worktree, "rev-parse", "HEAD") == before


def _crashed_abandonment(complete):
    call = request(complete, abandon=True, instruction_quote="Stop this Goal now")
    with pytest.raises(Crash):
        finish_goal(call, environment(complete, stop_at("abandoned")))
    directory = complete.control / "goals" / call.record_id / "operations" / call.operation_id
    return call, directory


def test_abandoned_retry_refuses_an_unsealed_abandonment_file(layout):
    complete = publication_layout(layout)
    call, directory = _crashed_abandonment(complete)
    (directory / "abandonment.authority.json").unlink()
    assert (directory / "abandonment.json").is_file()
    with pytest.raises(LifecycleError, match="invalid frozen abandonment"):
        finish_goal(call, environment(complete))


def test_inode_list_operation_directory_identity_is_refused(layout):
    complete = publication_layout(layout)
    call, directory = _crashed_abandonment(complete)
    path = directory / "abandonment.authority.json"
    authority = json.loads(path.read_bytes())
    authority["record_directory"]["identity"] = [1, 2]
    path.write_text(json.dumps(authority))
    with pytest.raises(LifecycleError, match="different logical record"):
        finish_goal(call, environment(complete))


@pytest.mark.parametrize("substitute", ["projection", "notes"])
def test_abandoned_retry_refuses_substituted_modern_notes(layout, substitute):
    complete = publication_layout(layout)
    call = request(complete, abandon=True, instruction_quote="Stop this Goal now")
    with pytest.raises(Crash):
        finish_goal(call, environment(complete, stop_at("abandoned")))
    directory = complete.control / "goals" / call.record_id / "operations" / call.operation_id
    if substitute == "projection":
        projection = directory / "abandonment.json"
        value = json.loads(projection.read_bytes())
        value["notes"] = ["substituted"]
        projection.write_text(json.dumps(value))
    else:
        authority = directory / "abandonment.authority.json"
        value = json.loads(authority.read_bytes())
        value["value"]["notes"] = [42]
        from booley.goals.proposals import digest, encode

        value["digest"] = digest(value["value"])
        authority.write_bytes(encode(value))
        (directory / "abandonment.json").write_bytes(encode(value["value"]))
    before = git(complete.worktree, "rev-parse", "HEAD")
    with pytest.raises(LifecycleError):
        finish_goal(call, environment(complete))
    assert git(complete.worktree, "rev-parse", "HEAD") == before


def test_completion_jobs_use_typed_bounded_read_only_activity(layout, monkeypatch):
    from booley.runtime import job_records, job_wait

    complete = publication_layout(layout)
    jobs = record_paths(complete.control, complete.record.id).jobs_dir
    jobs.mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(job_wait, "is_pid_alive", lambda pid: pid == 123)
    monkeypatch.setattr(job_records.time, "time", lambda: 1_800_000_000.0)
    for run_id, pid in [("live", 123), ("dead", 124), ("spawn", None)]:
        job_records.write_record(
            job_records.JobRecord(
                run_id,
                "lint",
                "2027-01-15T08:00:00Z",
                60,
                pid=pid,
            ),
            jobs,
        )
    before = {path.name: path.read_bytes() for path in jobs.iterdir()}
    result = finish_goal(request(complete), environment(complete))
    assert {job["run_id"] for job in result["running_jobs"]} == {"live", "spawn"}
    assert {path.name: path.read_bytes() for path in jobs.iterdir()} == before
    assert all(set(job) == {"run_id", "endpoint", "status"} for job in result["running_jobs"])


def test_completion_malformed_job_record_has_actionable_lifecycle_error(layout):
    complete = publication_layout(layout)
    jobs = record_paths(complete.control, complete.record.id).jobs_dir
    jobs.mkdir(parents=True, exist_ok=True)
    path = jobs / "broken.json"
    path.write_bytes(b"[]")
    with pytest.raises(LifecycleError, match="malformed Job"):
        finish_goal(request(complete), environment(complete))
    assert path.read_bytes() == b"[]"


def test_prefence_invalid_retry_preserves_job_after_unrelated_approved_change(layout):
    from booley.criteria.state import DevelopmentState
    from booley.flows.execution_persistence import EvidenceDiscarded
    from booley.goals.binding import bind_run
    from booley.goals.recorder import GoalEvidenceRecorder
    from tests.goals.conftest import LINT_KEY
    from tests.goals.test_proposals import approve, proposal, setup_cycle
    from tests.goals.test_proposals import publish as cycle_publish

    changes = setup_cycle(layout)
    cycle_publish(layout)
    initial = proposal(layout, changes)
    assert approve(layout, changes, initial.proposal.id).state == "applied"
    store = GoalStore(layout.control)
    old_binding = bind_run(store, layout.worktree, "before-frozen-attempt")
    call = request(layout)
    with pytest.raises(Crash):
        finish_goal(call, environment(layout, stop_at("attempt-frozen")))
    view = proposal(layout, changes, maximum=300)
    assert approve(layout, changes, view.proposal.id).state == "applied"
    fresh_binding = bind_run(store, layout.worktree, "after-approved-change")
    assert fresh_binding.record_revision > old_binding.record_revision
    assert finish_goal(call, environment(layout))["status"] == "revalidation_required"
    for binding, accepted in [(old_binding, False), (fresh_binding, True)]:
        recorder = GoalEvidenceRecorder(binding)
        state = DevelopmentState.load(
            record_paths(layout.control, layout.record.id).state_file, recorder.state_persistence()
        )
        changes = state.set_criterion(LINT_KEY, True, detail={"warnings": 0})
        if accepted:
            recorder.record_changes(
                state, changes, invocation_id=binding.invocation_id, producer="lint"
            )
            state.save()
        else:
            with pytest.raises(EvidenceDiscarded, match="publication fence"):
                recorder.record_changes(
                    state, changes, invocation_id=binding.invocation_id, producer="lint"
                )


@pytest.mark.parametrize(
    "abandon",
    [
        pytest.param(False, marks=pytest.mark.timeout(150), id="False"),
        pytest.param(True, marks=pytest.mark.timeout(150), id="True"),
    ],
)
def test_pruned_publication_reconstructs_exact_journaled_commit(layout, abandon):
    complete = publication_layout(layout)
    call = request(complete)
    with pytest.raises(Crash):
        finish_goal(call, environment(complete, stop_at("commit-journaled")))
    directory = complete.control / "goals" / call.record_id / "operations" / call.operation_id
    journal = json.loads((directory / "publication.json").read_bytes())
    original = journal["commit"]
    from booley.runtime.pinned_history import raw_git

    raw = raw_git(complete.worktree, "cat-file", "commit", original)
    pin = git(complete.worktree, "rev-parse", "HEAD")
    git(complete.worktree, "prune", "--expire=now")
    from booley.runtime.history_commit import FileCommitError
    from booley.runtime.pinned_history import raw_git

    with pytest.raises(FileCommitError):
        raw_git(complete.worktree, "cat-file", "commit", original)
    if abandon:
        hook = complete.control / "mcp_tools/tool.py"
        hook.write_bytes(hook.read_bytes() + b"# changed before abandonment\n")
        result = abandon_goal(
            GoalStore(complete.control), complete.worktree, environment(complete)
        )
        assert result["status"] == "abandoned"
        assert git(complete.worktree, "rev-parse", "HEAD") == pin
    else:
        assert finish_goal(call, environment(complete))["status"] == "finished"
        assert git(complete.worktree, "rev-parse", "HEAD") == original
    assert raw_git(complete.worktree, "cat-file", "commit", original) == raw


@pytest.mark.parametrize(
    "attack", ["corrupt-pack", "physical-control", "physical-admin", "missing-pack"]
)
def test_pruned_publication_without_exact_authority_refuses_without_git_writes(layout, attack):
    from booley.goals.proposals import digest, encode

    complete = publication_layout(layout)
    call = request(complete)
    with pytest.raises(Crash):
        finish_goal(call, environment(complete, stop_at("commit-journaled")))
    directory = complete.control / "goals" / call.record_id / "operations" / call.operation_id
    if attack not in {"physical-control", "physical-admin"}:
        path = directory / "publication.authority.json"
        authority = json.loads(path.read_bytes())
        if attack == "corrupt-pack":
            authority["value"]["pack_hex"] = b"substituted pack".hex()
        else:
            del authority["value"]["pack_hex"]
        authority["digest"] = digest(authority["value"])
        path.write_bytes(encode(authority))
        (directory / "publication.json").write_bytes(encode(authority["value"]))
    elif attack == "physical-admin":
        recreate(complete.main / ".git")
    else:
        worktrees = complete.control / "worktrees"
        held = complete.control.parent / "held-worktrees"
        worktrees.rename(held)
        recreate(complete.control)
        held.rename(worktrees)
    git(complete.worktree, "prune", "--expire=now")
    before = (
        git(complete.worktree, "rev-parse", "HEAD"),
        git(complete.worktree, "count-objects", "-v"),
        git(complete.worktree, "diff", "--cached", "--raw"),
    )
    with pytest.raises(LifecycleError):
        finish_goal(call, environment(complete))
    assert before == (
        git(complete.worktree, "rev-parse", "HEAD"),
        git(complete.worktree, "count-objects", "-v"),
        git(complete.worktree, "diff", "--cached", "--raw"),
    )
