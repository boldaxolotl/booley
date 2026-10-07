"""Committed participant selection, waiver anchors and frozen nonversioned recovery."""

import json
import os
import shutil
from pathlib import Path
from types import SimpleNamespace

import pytest

from booley.goals.finish import finish_goal
from booley.goals.lifecycle import LifecycleError
from booley.goals.model import GoalState
from booley.goals.store import GoalStore
from tests.goals.conftest import enter_goals, git, install_paired_project
from tests.goals.test_finish import Crash, environment, request
from tests.goals.test_status import publish

# These multi-step Git/Reviewer cases retain the established Goal lifecycle budget.
# The measured Windows Reviewer baseline is 27.768s (windows-test-timings.json);
# its 3x/30s-rounded minimum is 90s, above CI's unannotated 60s default.
pytestmark = pytest.mark.timeout(120)


def paired_complete(layout, tmp_path):
    project = install_paired_project(layout, tmp_path)
    (project / "tests.toml").write_bytes(b'[top]\ntests = ["smoke"]\n')
    git(project, "add", "tests.toml")
    git(project, "commit", "-qm", "entry suite")
    layout.record = enter_goals(layout, [{"family": "lint", "target": "top"}])
    publish(layout)
    return layout, project


def test_paired_dirty_participant_refuses_without_git_writes(layout, tmp_path):
    complete, project = paired_complete(layout, tmp_path)
    (project / "note.txt").write_bytes(b"not committed")
    before = git(complete.worktree, "count-objects", "-v")
    with pytest.raises(LifecycleError, match="commit changes"):
        finish_goal(request(complete), environment(complete))
    assert git(complete.worktree, "count-objects", "-v") == before
    assert GoalStore(complete.control).load(complete.record.id).state is GoalState.ACTIVE


def test_shared_nonversioned_control_project_uses_explicit_view_without_main_git_mutation(layout):
    from booley.goals.entry import EntryEnvironment, EntryRequest, enter_goal_mode
    from booley.goals.model import parse_goal_args
    from tests.goals.conftest import TOP_CORE

    shutil.rmtree(layout.worktree / ".booley_project")
    (layout.worktree / "top.core").write_bytes(TOP_CORE.encode())
    git(layout.worktree, "add", "top.core")
    git(layout.worktree, "commit", "-qm", "runtime Target")
    (layout.control / "booley.toml").write_bytes(b"[stealth]\nenabled=false\n")
    (layout.control / "tests.toml").write_bytes(b'[top]\ntests=["shared"]\n')
    layout.record = enter_goal_mode(
        EntryRequest(
            layout.worktree, "shared", parse_goal_args([{"family": "lint", "target": "top"}])
        ),
        EntryEnvironment(layout.control),
    ).record
    publish(layout)
    head = git(layout.main, "rev-parse", "HEAD")
    index = (layout.main / ".git/index").read_bytes()
    result = finish_goal(request(layout), environment(layout))
    facts = json.loads(Path(result["package"]).read_bytes())
    assert facts["input_proof"]["project"] == str(layout.control.resolve())
    assert facts["input_proof"]["project_pin"] is None
    assert any(
        row["classification"] == "ProjectSnapshot"
        for row in facts["input_proof"]["nonversioned_observations"]
    )
    assert "git-excluded" in result["publication_note"]
    assert git(layout.main, "rev-parse", "HEAD") == head
    assert (layout.main / ".git/index").read_bytes() == index


def test_authored_absolute_source_cannot_make_committed_freshness_read_live_checkout(layout):
    layout.record = enter_goals(layout, [{"family": "lint", "target": "top"}])
    core = layout.worktree / "top.core"
    core.write_bytes(
        core.read_bytes().replace(
            b"[rtl.v]", json.dumps([str(layout.worktree / "rtl.v")]).encode()
        )
    )
    git(layout.worktree, "add", "top.core")
    git(layout.worktree, "commit", "-qm", "absolute declared source")
    publish(layout)
    before = git(layout.worktree, "count-objects", "-v")
    with pytest.raises(LifecycleError, match="escapes committed scratch"):
        finish_goal(request(layout), environment(layout))
    assert git(layout.worktree, "count-objects", "-v") == before


def test_paired_original_base_and_final_suite_changes_share_one_view(layout, tmp_path):
    complete, project = paired_complete(layout, tmp_path)
    original = complete.record.paired_project_base_sha
    (project / "tests.toml").write_bytes(b'[top]\ntests = ["smoke", "additional"]\n')
    git(project, "add", "tests.toml")
    git(project, "commit", "-qm", "final suite")
    publish(complete)
    result = finish_goal(request(complete), environment(complete))
    facts = json.loads(Path(result["package"]).read_bytes())
    assert facts["input_proof"]["project_base"] == original
    assert facts["input_proof"]["project_pin"] == git(project, "rev-parse", "HEAD").strip()
    changed = facts["target_changes"][0]
    assert changed["before"]["tests"]["tests"] == ["smoke"]
    assert changed["after"]["tests"]["tests"] == ["smoke", "additional"]
    assert facts["goals"][0]["modified_target"]


def test_stealth_rechecks_paired_pin_after_local_artifacts(layout, tmp_path):
    complete, project = paired_complete(layout, tmp_path)

    def move(name):
        if name == "local-artifacts":
            (project / "note.txt").write_bytes(b"new committed work")
            git(project, "add", "note.txt")
            git(project, "commit", "-qm", "concurrent participant move")

    call = request(complete)
    result = finish_goal(call, environment(complete, move))
    assert result["status"] == "revalidation_required"
    assert finish_goal(call, environment(complete)) == result
    assert GoalStore(complete.control).load(complete.record.id).state is GoalState.ACTIVE


def test_topology_replacement_after_fence_retains_attempt_and_allows_safe_abandon(
    layout, tmp_path
):
    complete, project = paired_complete(layout, tmp_path)
    call = request(complete)

    def stop(name):
        if name == "finishing":
            raise Crash()

    with pytest.raises(Crash):
        finish_goal(call, environment(complete, stop))
    old = project.with_name("retained-project")
    project.rename(old)
    shutil.copytree(old, project)
    result = finish_goal(call, environment(complete))
    assert result["status"] == "revalidation_required" and "identity" in result["reason"]
    assert GoalStore(complete.control).load(complete.record.id).state is GoalState.ACTIVE
    with pytest.raises(LifecycleError, match=r"topology|identity"):
        finish_goal(request(complete), environment(complete))
    from booley.goals.abandon import abandon_goal

    assert (
        abandon_goal(GoalStore(complete.control), complete.worktree, environment(complete))[
            "status"
        ]
        == "abandoned"
    )
    assert old.exists() and project.exists()


def test_nonversioned_original_suite_snapshot_is_not_replaced_by_final(layout):
    layout.record = enter_goals(layout, [{"family": "lint", "target": "top"}])
    (layout.worktree / ".booley_project/tests.toml").write_bytes(b'[top]\ntests=["changed"]\n')
    publish(layout)
    result = finish_goal(request(layout), environment(layout))
    changed = json.loads(Path(result["package"]).read_bytes())["target_changes"][0]
    assert changed["before"]["tests"]["tests"] == ["smoke"]
    assert changed["after"]["tests"]["tests"] == ["changed"]


@pytest.mark.parametrize("platform", ["win32", "linux"], ids=["win32-True", "linux-True"])
def test_recovery_filesystem_mode_uses_windows_readonly_flag_and_posix_nonexecutable_projection(
    layout, monkeypatch, platform
):
    import booley.goals.publication_destination as module

    layout.record = enter_goals(layout, [{"family": "lint", "target": "top"}])
    publish(layout)
    call = request(layout)

    def stop(name):
        if name == "finishing":
            raise Crash()

    with pytest.raises(Crash):
        finish_goal(call, environment(layout, stop))
    directory = layout.control / "goals" / layout.record.id / "operations" / call.operation_id
    frozen = json.loads((directory / "attempt.json").read_bytes())
    destination = layout.worktree / frozen["path"]
    destination.parent.mkdir(parents=True)
    destination.write_bytes(bytes.fromhex(frozen["summary_hex"]))
    destination.chmod(0o666)
    monkeypatch.setattr(module, "sys", SimpleNamespace(platform=platform))
    assert finish_goal(call, environment(layout))["status"] == "finished"


def test_paired_and_rtl_file_and_constraint_deltas_are_unambiguous(layout, tmp_path):
    project = install_paired_project(layout, tmp_path)
    (project / "tests.toml").write_bytes(b'[top]\ntests = ["smoke"]\n')
    for root in (layout.worktree, project):
        (root / "same.sdc").write_bytes(b"original constraint")
        git(root, "add", ".")
        git(root, "commit", "-qm", "original constraints")
    layout.record = enter_goals(layout, [{"family": "lint", "target": "top"}])
    for root in (layout.worktree, project):
        (root / "same.sdc").write_bytes(b"final constraint")
        (root / "new.xdc").write_bytes(b"new constraint")
        git(root, "add", ".")
        git(root, "commit", "-qm", "final constraints")
    publish(layout)
    result = finish_goal(request(layout, explain_html=True), environment(layout))
    facts = json.loads(Path(result["package"]).read_bytes())
    paths = {"rtl/same.sdc", "project/same.sdc", "rtl/new.xdc", "project/new.xdc"}
    assert paths == {row["path"] for row in facts["constraint_edits"]}
    assert paths <= {row["path"] for row in facts["diff_summary"]}
    assert {row["repository"] for row in facts["constraint_edits"]} == {"rtl", "project"}
    for path in paths:
        assert path in result["message"]
        assert path in Path(result["html"]).read_text()


@pytest.mark.parametrize("outcome", ["finish", "refusal"])
def test_cleanliness_does_not_refresh_shared_index_for_same_bytes(layout, outcome):
    layout.record = enter_goals(layout, [{"family": "lint", "target": "top"}])
    publish(layout)
    path = layout.worktree / "rtl.v"
    metadata = path.stat()
    os.utime(path, ns=(metadata.st_atime_ns, metadata.st_mtime_ns + 1_000_000_000))
    index = Path(git(layout.worktree, "rev-parse", "--git-path", "index"))
    before = index.read_bytes()
    objects = git(layout.worktree, "count-objects", "-v")
    call = request(layout)
    if outcome == "refusal":
        from dataclasses import replace

        with pytest.raises(LifecycleError, match="Session Summary"):
            finish_goal(replace(call, summary=""), environment(layout))
    else:
        assert finish_goal(call, environment(layout))["status"] == "finished"
    assert index.read_bytes() == before
    assert git(layout.worktree, "count-objects", "-v") == objects


@pytest.mark.skipif(os.name == "nt", reason="POSIX namespace-spelling fixture uses symlinks")
@pytest.mark.parametrize(
    "boundary",
    [None, "attempt-frozen", "finishing", "publication-intent", "ref-published", "finished"],
)
def test_same_physical_mount_spelling_can_finish_and_recover(
    layout, tmp_path, monkeypatch, boundary
):
    from dataclasses import replace

    from tests.goals.test_finish import publication_layout

    layout = publication_layout(layout)
    call = request(layout)
    if boundary is not None:

        def stop(name):
            if name == boundary:
                raise Crash()

        with pytest.raises(Crash):
            finish_goal(call, environment(layout, stop))
    current = mount_spelling(layout, tmp_path, monkeypatch)
    before = git(layout.main, "rev-parse", "HEAD")
    result = finish_goal(replace(call, work_dir=current.worktree), environment(current))
    assert result["status"] == "finished"
    assert git(layout.main, "rev-parse", "HEAD") == before
    assert git(layout.worktree, "rev-parse", "HEAD^1") == layout.record.base_sha
    assert finish_goal(replace(call, work_dir=current.worktree), environment(current)) == result


def test_windows_reverse_target_labels_match_live_posix_labels(monkeypatch):
    from pathlib import PureWindowsPath

    import booley.goals.target_surface as module

    monkeypatch.setattr(module, "Path", PureWindowsPath)
    mapped = module._logical_label(
        "C:/scratch/project/tests.toml",
        {PureWindowsPath("C:/scratch/project"): PureWindowsPath("D:/project")},
    )
    assert mapped == "D:/project/tests.toml"


@pytest.mark.skipif(os.name == "nt", reason="POSIX namespace-spelling fixture uses symlinks")
@pytest.mark.parametrize("boundary", [None, "attempt-frozen", "finishing"])
@pytest.mark.parametrize("drift", [None, "protected", "target"])
def test_coalesced_project_control_spellings_preserve_entry_authority(
    layout, tmp_path, monkeypatch, boundary, drift
):
    control = enter_shared_role_aliases(layout, tmp_path, monkeypatch)
    call = request(layout)
    if boundary is not None:

        def stop(name):
            if name == boundary:
                raise Crash()

        with pytest.raises(Crash):
            finish_goal(call, environment(layout, stop))
    layout.control = control.readlink()
    monkeypatch.setenv("BOOLEY_PROJECT_DIR", str(layout.control))
    if drift == "protected":
        (layout.control / "booley.toml").write_text("[stealth]\nenabled=false\n# changed\n")
    elif drift == "target":
        (layout.control / "tests.toml").write_text('[top]\ntests=["changed"]\n')
    if drift is not None and boundary is None:
        with pytest.raises(LifecycleError, match=r"protected input|met and fresh"):
            finish_goal(call, environment(layout))
        return
    result = finish_goal(call, environment(layout))
    assert result["status"] == ("finished" if drift is None else "revalidation_required")
    assert finish_goal(call, environment(layout)) == result


def enter_shared_role_aliases(layout, tmp_path, monkeypatch):
    from booley.goals.entry import EntryEnvironment, EntryRequest, enter_goal_mode
    from booley.goals.model import parse_goal_args
    from tests.goals.conftest import TOP_CORE

    shutil.rmtree(layout.worktree / ".booley_project")
    (layout.worktree / "top.core").write_text(TOP_CORE)
    git(layout.worktree, "add", "top.core")
    git(layout.worktree, "commit", "-qm", "runtime Target")
    (layout.control / "booley.toml").write_text("[stealth]\nenabled=false\n")
    (layout.control / "tests.toml").write_text('[top]\ntests=["shared"]\n')
    project, control = model_shared_root_spellings(layout.control, tmp_path, monkeypatch)
    monkeypatch.setenv("BOOLEY_PROJECT_DIR", str(project))
    layout.control = control
    layout.record = enter_goal_mode(
        EntryRequest(
            layout.worktree, "coalesced", parse_goal_args([{"family": "lint", "target": "top"}])
        ),
        EntryEnvironment(control),
    ).record
    publish(layout)
    return control


def model_shared_root_spellings(root, tmp_path, monkeypatch):
    """Model two bind spellings with actual shared identities and unchanged file bytes."""
    aliases = [tmp_path / name for name in ("project-mount", "control-mount")]
    for alias in aliases:
        alias.symlink_to(root, target_is_directory=True)
    resolve, is_link = Path.resolve, Path.is_symlink

    def mounted_resolve(path, *args, **kwargs):
        physical = resolve(path, *args, **kwargs)
        for alias in aliases:
            if path.is_relative_to(alias) and physical.is_relative_to(root):
                return alias / physical.relative_to(root)
        return physical

    monkeypatch.setattr(Path, "resolve", mounted_resolve)
    monkeypatch.setattr(
        Path, "is_symlink", lambda path: False if path in aliases else is_link(path)
    )
    return aliases


@pytest.mark.skipif(os.name == "nt", reason="POSIX namespace-spelling fixture uses symlinks")
def test_role_labels_keep_same_physical_rtl_project_tests_and_waiver_distinct(tmp_path):
    from unittest.mock import patch

    from booley.goals.input_identity import root_bindings, tests_label
    from booley.goals.input_view import _role_waiver_label
    from booley.goals.proposals import digest
    from booley.goals.target_surface import target_surface_fingerprint

    rtl = tmp_path / "rtl"
    rtl.mkdir()
    tests = rtl / "tests.toml"
    tests.write_text('[top]\ntests=["same"]\n')
    project = tmp_path / "project-mount"
    project.symlink_to(rtl, target_is_directory=True)
    saved = root_bindings({"rtl": rtl, "project": project})
    with (
        patch("booley.goals.target_surface._cores", return_value=[]),
        patch("booley.goals.target_surface._tests_toml", return_value=tests),
    ):
        surface = target_surface_fingerprint(rtl, None, logical_tests=tests_label(saved))
    assert surface["files"] == [str(project / "tests.toml")]
    for anchor, expected in (
        ("project_data_repository", project),
        ("design_repository", rtl),
    ):
        value = {"root": str(rtl / "policy"), "config": {"anchor": anchor}, "files": []}
        assert _role_waiver_label(value, saved, rtl, rtl)
        assert value["root"] == str(expected / "policy")
        assert value["digest"] == digest(
            {key: val for key, val in value.items() if key != "digest"}
        )


@pytest.mark.skipif(os.name == "nt", reason="POSIX namespace-spelling fixture uses symlinks")
def test_same_physical_rtl_project_root_requires_committed_snapshot(layout, tmp_path, monkeypatch):
    from booley.goals.input_view import snapshot_project

    project, _ = model_shared_root_spellings(layout.worktree, tmp_path, monkeypatch)
    (layout.worktree / "booley.toml").write_text(f"[project]\ndir = {str(project)!r}\n")
    assert snapshot_project(layout.worktree) is None


def mount_spelling(layout, tmp_path, monkeypatch):
    """Model only the namespace, using actual shared directory/Git/file identities."""
    from booley.goals.checkout import GoalCheckout

    original = layout.main.parent
    alias = tmp_path / "alternate-mount"
    alias.symlink_to(original, target_is_directory=True)
    resolve = Path.resolve
    containing = GoalCheckout.containing_repository
    is_link = Path.is_symlink

    def mounted_resolve(path, *args, **kwargs):
        physical = resolve(path, *args, **kwargs)
        # A bind mount retains its spelling after resolve(). Actual stat/Git/bytes
        # below are shared physical directories; only namespace spelling is modeled.
        if path.is_relative_to(alias) and physical.is_relative_to(original):
            return alias / physical.relative_to(original)
        return physical

    def mounted_repository(checkout):
        found = containing(checkout)
        if (
            found is not None
            and checkout.root.is_relative_to(alias)
            and found[0].is_relative_to(original)
        ):
            return alias / found[0].relative_to(original), found[1]
        return found

    monkeypatch.setattr(Path, "resolve", mounted_resolve)
    monkeypatch.setattr(Path, "is_symlink", lambda path: False if path == alias else is_link(path))
    monkeypatch.setattr(GoalCheckout, "containing_repository", mounted_repository)
    current = SimpleNamespace(**vars(layout))
    current.worktree = alias / layout.worktree.relative_to(original)
    current.control = alias / layout.control.relative_to(original)
    return current


@pytest.mark.skipif(os.name == "nt", reason="POSIX namespace-spelling fixture uses symlinks")
@pytest.mark.parametrize("kind", ["paired", "generated"])
def test_alias_recovery_rechecks_paired_and_exact_generated_input_proof(
    layout, tmp_path, monkeypatch, kind
):
    from dataclasses import replace

    from booley.goals.entry import EntryEnvironment
    from booley.mcp.goal_completion import completion_environment

    if kind == "paired":
        layout, _ = paired_complete(layout, tmp_path)
        env = environment(layout)
    else:
        from tests.goals.test_generated_inputs import classified

        classified(layout)
        env = completion_environment(EntryEnvironment(layout.control))
    call = request(layout)

    def stop(name):
        if name == "finishing":
            raise Crash()

    with pytest.raises(Crash):
        finish_goal(call, replace(env, on_boundary=stop))
    attempt_path = (
        layout.control
        / "goals"
        / layout.record.id
        / "operations"
        / call.operation_id
        / "attempt.json"
    )
    before = json.loads(attempt_path.read_bytes())["package"]
    current = mount_spelling(layout, tmp_path, monkeypatch)
    env = (
        environment(current)
        if kind == "paired"
        else completion_environment(EntryEnvironment(current.control))
    )
    result = finish_goal(replace(call, work_dir=current.worktree), env)
    assert result["status"] == "finished"
    assert json.loads(Path(result["package"]).read_bytes()) == before


@pytest.mark.skipif(os.name == "nt", reason="POSIX namespace-spelling fixture uses symlinks")
def test_entry_through_alternate_spelling_finishes_from_original(layout, tmp_path, monkeypatch):
    from booley.goals.abandon import abandon_goal
    from booley.goals.entry import EntryEnvironment, EntryRequest, enter_goal_mode
    from booley.goals.model import parse_goal_args
    from tests.goals.test_finish import publication_layout

    layout = publication_layout(layout)
    abandon_goal(GoalStore(layout.control), layout.worktree, environment(layout))
    current = mount_spelling(layout, tmp_path, monkeypatch)
    current.record = enter_goal_mode(
        EntryRequest(
            current.worktree,
            "aliased-entry",
            parse_goal_args([{"family": "lint", "target": "top"}]),
        ),
        EntryEnvironment(current.control),
    ).record
    publish(current)
    layout.record = current.record
    result = finish_goal(request(layout), environment(layout))
    assert result["status"] == "finished"


@pytest.mark.skipif(os.name == "nt", reason="requires native POSIX symlinks")
def test_git_admin_alias_keeps_checkout_identity_but_copied_admin_refuses(layout, tmp_path):
    from booley.goals.store import WorktreeIdentityError, _checkout_name
    from booley.runtime.project_repositories import git_directories

    directories = git_directories(layout.worktree)
    alias = tmp_path / "other-admin-name"
    alias.symlink_to(directories.git_dir, target_is_directory=True)
    assert _checkout_name(alias, directories.common_dir) == "worktrees/" + directories.git_dir.name
    copied = tmp_path / "substituted-admin"
    shutil.copytree(directories.git_dir, copied)
    with pytest.raises(WorktreeIdentityError, match="not a worktree"):
        _checkout_name(copied, directories.common_dir)
