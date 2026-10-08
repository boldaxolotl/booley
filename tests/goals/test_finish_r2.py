"""Final-review counterexamples exercised through real Git and lifecycle services."""

from __future__ import annotations

import errno
import json
import os
import shutil
import subprocess
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

import pytest

from booley.goals.committed_bytes import attributes
from booley.goals.finish import finish_goal
from booley.goals.lifecycle import LifecycleError
from booley.goals.model import GoalState
from booley.goals.store import GoalStore
from tests.goals.conftest import git
from tests.goals.test_committed_export import repository
from tests.goals.test_finish import Crash, environment, publication_layout, request

pytestmark = pytest.mark.timeout(120)


def test_pinned_attributes_accept_paths_beyond_process_argument_limit(tmp_path):
    root = repository(tmp_path / "source")
    (root / ".gitattributes").write_bytes(b"*.v text eol=lf\n")
    git(root, "add", ".gitattributes")
    git(root, "commit", "-qm", "pinned attributes")
    # Attribute queries do not require filesystem entries. These valid relative
    # names exceed Linux ARG_MAX and Windows's command-line limit without a huge
    # fixture checkout; Git must receive literal NUL-separated input paths.
    names = [(("directory/" * 64) + f"source-{i}.v").encode() for i in range(4000)]
    values = attributes(root, git(root, "rev-parse", "HEAD"), names)
    assert set(values) == set(names)
    assert all(row["text"] == "set" and row["eol"] == "lf" for row in values.values())


def test_summary_stage_is_local_to_destination_even_when_devices_match(layout, monkeypatch):
    complete = publication_layout(layout)
    link = os.link

    def distinct_mount_link(source, destination, **kwargs):
        source, destination = Path(source), Path(destination)
        if "operations" in source.parts and destination.parent.name == "history":
            assert source.stat().st_dev == destination.parent.stat().st_dev
            raise OSError(errno.EXDEV, "different mounts of the same filesystem")
        return link(source, destination, **kwargs)

    # Models the kernel's cross-mount rejection; this is not a native bind-mount
    # execution claim. All publication bytes, refs and index effects use real Git.
    monkeypatch.setattr(os, "link", distinct_mount_link)
    call = request(complete)
    result = finish_goal(call, environment(complete))
    assert result["status"] == "finished"
    assert git(complete.worktree, "status", "--porcelain") == ""
    assert finish_goal(call, environment(complete)) == result


def test_control_identity_change_during_finishing_revalidates_and_can_abandon(layout):
    complete = publication_layout(layout)
    call = request(complete)

    def stop(boundary):
        if boundary == "finishing":
            raise Crash()

    with pytest.raises(Crash):
        finish_goal(call, environment(complete, stop))
    # Only the control root is recreated. Keep the actual linked worktree and its
    # administration unchanged by moving it out and back around the directory copy.
    worktrees = complete.control / "worktrees"
    held_worktrees = complete.main / "held-worktrees"
    worktrees.rename(held_worktrees)
    old = complete.control.with_name("old-control")
    complete.control.rename(old)
    shutil.copytree(old, complete.control)
    held_worktrees.rename(worktrees)
    result = finish_goal(call, environment(complete))
    assert result["status"] == "revalidation_required"
    assert GoalStore(complete.control).load(call.record_id).state is GoalState.ACTIVE
    abandon = replace(request(complete), abandon=True, instruction_quote="stop")
    assert finish_goal(abandon, environment(complete))["status"] == "abandoned"


def test_missing_nonversioned_project_during_finishing_is_typed_revalidation(layout):
    complete = layout
    from tests.goals.conftest import enter_goals
    from tests.goals.test_status import publish

    complete.record = enter_goals(complete, [{"family": "lint", "target": "top"}])
    publish(complete)
    call = request(complete)

    def stop(boundary):
        if boundary == "finishing":
            raise Crash()

    with pytest.raises(Crash):
        finish_goal(call, environment(complete, stop))
    project = complete.worktree / ".booley_project"
    project.rename(complete.worktree / "missing-project")
    result = finish_goal(call, environment(complete))
    assert result["status"] == "revalidation_required"


def test_new_external_versioned_project_during_finishing_revalidates(layout):
    from tests.goals.conftest import enter_goals
    from tests.goals.test_status import publish

    layout.record = enter_goals(layout, [{"family": "lint", "target": "top"}])
    publish(layout)
    call = request(layout)

    def stop(boundary):
        if boundary == "finishing":
            raise Crash()

    with pytest.raises(Crash):
        finish_goal(call, environment(layout, stop))
    project = layout.worktree / ".booley_project"
    # A separate repository in the literal Project directory changes participants
    # without introducing a publication-path symlink that must independently refuse.
    git(project, "init", "-q", "-b", "main")
    git(project, "add", ".")
    git(project, "commit", "-qm", "external versioned Project")
    result = finish_goal(call, environment(layout))
    assert result["status"] == "revalidation_required"
    assert "outside the pinned" in result["reason"]
    assert GoalStore(layout.control).load(call.record_id).state is GoalState.ACTIVE


@pytest.mark.parametrize("prerequisite", ["identity", "ignore"])
def test_publication_prerequisites_refuse_before_the_finishing_fence(
    layout, monkeypatch, prerequisite
):
    complete = publication_layout(layout)
    if prerequisite == "identity":
        config = complete.main / "no-global-config"
        config.write_bytes(b"")
        monkeypatch.setenv("GIT_CONFIG_GLOBAL", str(config))
        for key in (
            "GIT_AUTHOR_NAME",
            "GIT_AUTHOR_EMAIL",
            "GIT_COMMITTER_NAME",
            "GIT_COMMITTER_EMAIL",
            "EMAIL",
        ):
            monkeypatch.delenv(key, raising=False)
        git(complete.main, "config", "--unset", "user.name")
        git(complete.main, "config", "--unset", "user.email")
        git(complete.main, "config", "user.useConfigOnly", "true")
    else:
        from tests.goals.test_status import publish

        policy = complete.worktree / ".booley_project/.gitignore"
        policy.write_bytes(policy.read_bytes().replace(b"goals/*/\n", b""))
        git(complete.worktree, "commit", "-qam", "missing transient ignore")
        publish(complete)
    call = request(complete)
    with pytest.raises(LifecycleError, match=r"identity|managed goals"):
        finish_goal(call, environment(complete))
    current = GoalStore(complete.control).load(call.record_id)
    assert current.state is GoalState.ACTIVE and current.publication_floor == 0


def test_commit_tree_does_not_inherit_commit_gpgsign(layout):
    complete = publication_layout(layout)
    git(complete.main, "config", "commit.gpgsign", "true")
    git(complete.main, "config", "gpg.program", str(complete.main / "absent-signing-program"))
    assert finish_goal(request(complete), environment(complete))["status"] == "finished"


@pytest.mark.parametrize("selector", ["top#top", "::top:0#top"])
def test_completion_marks_canonical_bound_target_from_qualified_selector(
    layout, monkeypatch, selector
):
    from booley.criteria.state import DevelopmentState
    from booley.goals import entry
    from booley.goals.paths import record_paths
    from booley.goals.recorder import GoalEvidenceRecorder
    from booley.targets.catalog import TargetCatalog
    from tests.goals.conftest import bind, enter_goals

    monkeypatch.setattr(entry, "TargetCatalog", TargetCatalog)
    layout.record = enter_goals(layout, [{"family": "lint", "target": selector}])
    core = layout.worktree / "top.core"
    core.write_bytes(
        core.read_bytes().replace(b"toplevel: top}", b"toplevel: top, parameters: [FEATURE]}")
        + b"parameters:\n  FEATURE: {datatype: int, paramtype: vlogdefine, default: 1}\n"
    )
    git(layout.worktree, "commit", "-qam", "modified bound Target")
    recorder = GoalEvidenceRecorder(bind(layout))
    state = DevelopmentState.load(
        record_paths(layout.control, layout.record.id).state_file, recorder.state_persistence()
    )
    changes = state.set_criterion(layout.record.goals[0].spec.key, True, detail={"warnings": 0})
    recorder.record_changes(state, changes, invocation_id="qualified", producer="lint")
    state.save()
    result = finish_goal(request(layout), environment(layout))
    assert json.loads(Path(result["package"]).read_bytes())["goals"][0]["modified_target"]


@pytest.mark.parametrize("earlier", ["stale-index", "committed-edit"])
def test_later_finish_does_not_wedge_on_earlier_published_attempt(layout, earlier):
    complete = publication_layout(layout)
    first = request(complete)

    def stop(boundary):
        if boundary == "ref-published":
            raise Crash()

    with pytest.raises(Crash):
        finish_goal(first, environment(complete, stop))
    hook = complete.control / "mcp_tools/tool.py"
    original = hook.read_bytes()
    hook.write_bytes(original + b"# changed after publication\n")
    assert finish_goal(first, environment(complete))["status"] == "revalidation_required"
    hook.write_bytes(original)
    path = complete.worktree / f".booley_project/goals/history/{first.record_id}.md"
    before = path.read_bytes()
    if earlier == "committed-edit":
        path.write_bytes(before + b"\nUser-committed historical annotation.\n")
        git(complete.worktree, "add", str(path.relative_to(complete.worktree)))
        git(complete.worktree, "commit", "-qm", "annotated historical summary")
    else:
        assert "D  .booley_project/goals/history/" not in git(
            complete.worktree, "status", "--porcelain"
        )
    retained = path.read_bytes()
    result = finish_goal(request(complete), environment(complete))
    assert result["status"] == "finished" and path.read_bytes() == retained
    assert (
        git(complete.worktree, "show", f"HEAD:{path.relative_to(complete.worktree).as_posix()}")
        == retained.decode().strip()
    )
    user_file = complete.worktree / "ordinary-user-file.txt"
    user_file.write_bytes(b"ordinary user change\n")
    git(complete.worktree, "add", user_file.name)
    git(complete.worktree, "commit", "-qm", "ordinary next user commit")
    assert (
        git(complete.worktree, "show", f"HEAD:{path.relative_to(complete.worktree).as_posix()}")
        == retained.decode().strip()
    )


def snapshot_submodules(tmp_path):
    from booley.goals.input_view import (
        capture_path_roots,
        select_inputs,
        snapshot_project,
        topology_digest,
    )
    from tests.goals.test_committed_export import nested_modules

    root = nested_modules(tmp_path)
    project = root / ".booley_project"
    project.mkdir()
    (project / "booley.toml").write_bytes(b"[submodules]\npaths=[]\n")
    (root / ".git/info/exclude").write_bytes(b"/.booley_project\n")
    record = SimpleNamespace(
        input_paths=capture_path_roots(root, project),
        protected_paths=(),
        project_snapshot=None,
        base_sha=git(root, "rev-parse", "HEAD"),
        paired_project_base_sha=None,
        input_topology_digest="sha256:" + topology_digest(root, project, None),
    )
    select_inputs(record, root)
    record.project_snapshot = snapshot_project(root)
    git(root, "submodule", "deinit", "-q", "-f", "module")
    return root, project, record


def test_nonversioned_project_snapshot_controls_original_and_final_submodule_selection(tmp_path):
    from booley.goals.input_view import committed_view, select_inputs

    root, project, record = snapshot_submodules(tmp_path)
    for baseline in (True, False):
        with committed_view(
            select_inputs(record, root), record, control_project=project, baseline=baseline
        ) as view:
            assert not (view.root / "module").exists()
    (project / "booley.toml").write_bytes(b'[submodules]\npaths=["module"]\n')
    with committed_view(
        select_inputs(record, root), record, control_project=project, baseline=True
    ) as view:
        assert not (view.root / "module").exists()
    with (
        pytest.raises(LifecycleError, match="pinned local submodule unavailable"),
        committed_view(select_inputs(record, root), record, control_project=project),
    ):
        pytest.fail("selected missing submodule was accepted")


def test_nonversioned_selection_change_during_export_cannot_change_the_frozen_view(
    tmp_path, monkeypatch
):
    from booley.goals import input_view

    root, project, record = snapshot_submodules(tmp_path)
    export = input_view.export_tree

    def changing_export(repository, *args, **kwargs):
        result = export(repository, *args, **kwargs)
        if repository == root:
            (project / "booley.toml").write_bytes(b'[submodules]\npaths=["module"]\n')
        return result

    monkeypatch.setattr(input_view, "export_tree", changing_export)
    with (
        pytest.raises(LifecycleError, match="snapshot changed"),
        input_view.committed_view(
            input_view.select_inputs(record, root), record, control_project=project
        ),
    ):
        pytest.fail("mixed Project selection was accepted")


def test_abandon_retry_after_new_proposal_retains_capture_and_requires_fresh_operation(
    layout, monkeypatch
):
    from booley.goals.lifecycle import LifecycleOperation
    from booley.goals.proposals import load_proposal
    from tests.goals.test_proposals import proposal, setup_cycle

    changes = setup_cycle(layout)
    first = proposal(layout, changes)
    call = replace(request(layout), abandon=True, instruction_quote="stop")
    write = LifecycleOperation.write_sealed

    def crash(self, name, value):
        write(self, name, value)
        if name == "abandonment":
            raise Crash()

    monkeypatch.setattr(LifecycleOperation, "write_sealed", crash)
    with pytest.raises(Crash):
        finish_goal(call, environment(layout))
    monkeypatch.undo()
    root = layout.control / "goals" / call.record_id
    authority = root / "operations" / call.operation_id / "abandonment.authority.json"
    captured = authority.read_bytes()
    second = proposal(layout, changes, maximum=300)
    result = finish_goal(call, environment(layout))
    assert (
        result["status"] == "revalidation_required" and "fresh operation_id" in result["message"]
    )
    assert authority.read_bytes() == captured
    assert changes.store.load(call.record_id).state is GoalState.ACTIVE
    assert not load_proposal(root, first.proposal.id).closed_by_abandonment
    assert not load_proposal(root, second.proposal.id).closed_by_abandonment
    assert finish_goal(call, environment(layout)) == result
    fresh = replace(request(layout), abandon=True, instruction_quote="stop")
    assert finish_goal(fresh, environment(layout))["status"] == "abandoned"
    assert load_proposal(root, first.proposal.id).closed_by_abandonment
    assert load_proposal(root, second.proposal.id).closed_by_abandonment


def test_authority_only_attempt_reserves_its_history_path(layout, monkeypatch):
    from booley.goals import lifecycle

    complete = publication_layout(layout)
    first = request(complete)
    write = lifecycle.atomic_write_once

    def stop(path, content):
        write(path, content)
        if path.name == "attempt.authority.json":
            raise Crash()

    monkeypatch.setattr(lifecycle, "atomic_write_once", stop)
    with pytest.raises(Crash):
        finish_goal(first, environment(complete))
    monkeypatch.undo()
    operation = complete.control / "goals" / first.record_id / "operations" / first.operation_id
    assert not (operation / "attempt.json").exists()
    captured = (operation / "attempt.authority.json").read_bytes()
    fresh = request(complete)
    result = finish_goal(fresh, environment(complete))
    assert result["status"] == "finished"
    path = (
        complete.worktree
        / f".booley_project/goals/history/{fresh.record_id}-{fresh.operation_id}.md"
    )
    assert path.is_file()
    assert not path.with_name(f"{fresh.record_id}.md").exists()
    assert (operation / "attempt.authority.json").read_bytes() == captured


def test_conflicting_sealed_recovery_link_has_actionable_lifecycle_error(layout):
    from booley.goals.lifecycle import bind_operation

    complete = publication_layout(layout)
    with bind_operation(GoalStore(complete.control), request(complete)) as operation:
        operation.write_sealed("recovery", {"operation_id": "prior-a"})
        captured = (operation.directory / "recovery.authority.json").read_bytes()
        with pytest.raises(LifecycleError, match="fresh operation_id"):
            operation.write_sealed("recovery", {"operation_id": "prior-b"})
        assert (operation.directory / "recovery.authority.json").read_bytes() == captured


@pytest.mark.parametrize("staging", ["ancestor", "unmerged", "intent-to-add"])
def test_published_summary_preserves_conflicting_index_entries_without_wedging(layout, staging):
    from tests.goals.test_committed_export import raw_blob

    complete = publication_layout(layout)
    call = request(complete)
    index = complete.main / ".git/worktrees/wt/index"
    saved = []
    path = f".booley_project/goals/history/{call.record_id}.md"

    def stage(boundary):
        if boundary != "ref-published":
            return
        blob = raw_blob(complete.worktree, b"foreign staged bytes\n")
        if staging == "ancestor":
            git(
                complete.worktree,
                "update-index",
                "--add",
                "--cacheinfo",
                f"100644,{blob},.booley_project/goals/history",
            )
        elif staging == "intent-to-add":
            git(complete.worktree, "add", "-N", "--", path)
        else:
            subprocess.run(
                ["git", "update-index", "--index-info"],
                cwd=complete.worktree,
                input=f"100644 {blob} 1\t{path}\n100644 {blob} 2\t{path}\n".encode(),
                capture_output=True,
                check=True,
                timeout=30,
            )
        saved.append(index.read_bytes())

    result = finish_goal(call, environment(complete, stage))
    assert result["status"] == "finished" and index.read_bytes() == saved[0]
    assert finish_goal(call, environment(complete)) == result
    assert index.read_bytes() == saved[0]
