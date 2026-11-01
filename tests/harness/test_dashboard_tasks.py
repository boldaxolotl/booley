"""Task ownership, JSONC preservation, rollback and deployment gates without Docker/Git."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest

from booley.runtime import dashboard_tasks as tasks
from booley.runtime import devcontainer, session_issuance
from booley.runtime.jsonc import Document
from tests.conftest import symlink_or_skip

_REAL_TASK_FILE_TRACKED = tasks._task_file_tracked


@pytest.fixture
def project(tmp_path, monkeypatch):
    root = tmp_path / "project"
    root.mkdir()
    data = root / ".booley"
    data.mkdir()
    common = root / "git-admin"
    (common / "info").mkdir(parents=True)
    (common / "info/exclude").write_bytes(b"# user\n/other\n")
    monkeypatch.setattr(tasks, "git_directories", lambda _: SimpleNamespace(common_dir=common))
    monkeypatch.setattr(tasks, "_task_file_tracked", lambda _: False)
    monkeypatch.setattr(session_issuance, "stamp_path_for_identity", lambda _: root / "stamp.json")
    monkeypatch.setattr(
        session_issuance, "_legacy_stamp_path", lambda _: root / "legacy-stamp.json"
    )
    return SimpleNamespace(
        root=root, data=data, exclude=common / "info/exclude", file=root / ".vscode/tasks.json"
    )


def test_new_directory_task_and_idempotent_disable(project, monkeypatch):
    baseline = project.exclude.read_bytes()
    assert tasks.inspect(project.root, project.data).pending
    assert not project.file.exists()
    transaction = tasks.reconcile(project.root, project.data)
    assert transaction.applied
    task = Document(project.file.read_bytes().decode()).root.value["tasks"][0]
    assert task["command"] == "booley dashboard"
    assert "options" not in task
    assert task["runOptions"] == {"instanceLimit": 1}
    assert project.exclude.read_bytes().startswith(baseline)
    assert not tasks.inspect(project.root, project.data).pending
    (project.data / "booley.toml").write_text("[sandbox]\ndashboard = false\n")
    tasks.reconcile(project.root, project.data)
    assert not project.file.exists()
    assert project.exclude.read_bytes() == baseline
    (project.data / "booley.toml").write_text("[sandbox]\ndashboard = true\n")
    tasks.reconcile(project.root, project.data)
    assert b"/.vscode\n" in project.exclude.read_bytes()


def test_attach_task_does_not_require_global_automatic_task_permission(project):
    tasks.reconcile(project.root, project.data)
    task = Document(project.file.read_text()).root.value["tasks"][0]
    assert "runOn" not in task["runOptions"]


def test_old_owned_automatic_task_migrates_without_losing_user_content(project, monkeypatch):
    old = {**tasks.TASK, "runOptions": {"runOn": "folderOpen", "instanceLimit": 1}}
    with monkeypatch.context() as previous:
        previous.setattr(tasks, "TASK", old)
        tasks.reconcile(project.root, project.data)
    original = project.file.read_bytes()
    project.file.write_bytes(
        original.replace(b'"version": "2.0.0"', b'"version": "2.0.0", "user": 42')
    )
    tasks.reconcile(project.root, project.data)
    document = Document(project.file.read_text()).root.value
    assert document["user"] == 42
    assert "runOn" not in document["tasks"][0]["runOptions"]


def test_runtime_attach_accepts_host_ownership_at_a_different_mount(project):
    import json

    tasks.reconcile(project.root, project.data)
    owner_path = project.data / "runtime/dashboard-task.json"
    owner = json.loads(owner_path.read_bytes())
    owner["root"] = "/host-side/project"
    owner_path.write_text(json.dumps(owner))
    before = {path: path.read_bytes() for path in (project.file, owner_path, project.exclude)}
    assert tasks.attach_blocker(project.root, project.data) == ""
    assert before == {path: path.read_bytes() for path in before}


@pytest.mark.parametrize("conflict", ["removed", "edited", "unowned", "old", "disabled", "off"])
def test_runtime_attach_preserves_opt_out_and_migration_conflicts(project, monkeypatch, conflict):
    if conflict == "old":
        with monkeypatch.context() as previous:
            previous.setattr(tasks, "TASK", {**tasks.TASK, "runOptions": {"runOn": "folderOpen"}})
            tasks.reconcile(project.root, project.data)
    else:
        tasks.reconcile(project.root, project.data)
    if conflict == "removed":
        project.file.unlink()
    elif conflict == "edited":
        project.file.write_text(project.file.read_text().replace("booley dashboard", "custom"))
    elif conflict == "unowned":
        (project.data / "runtime/dashboard-task.json").unlink()
    elif conflict == "disabled":
        (project.data / "booley.toml").write_text("[sandbox]\ndashboard = false\n")
    elif conflict == "off":
        (project.file.parent / "settings.json").write_text('{"task.allowAutomaticTasks":"off"}')
    before = project.file.read_bytes() if project.file.exists() else None
    assert tasks.attach_blocker(project.root, project.data)
    assert (project.file.read_bytes() if project.file.exists() else None) == before


def test_runtime_attach_does_not_require_editing_tracked_unrelated_tasks(project, monkeypatch):
    project.file.parent.mkdir()
    project.file.write_text('{"version":"2.0.0","tasks":[]}')
    monkeypatch.setattr(tasks, "_task_file_tracked", lambda _: True)
    assert tasks.attach_blocker(project.root, project.data) == ""
    project.file.write_text('{"tasks":[{"label":"Booley Dashboard","command":"user"}]}')
    assert "User-owned" in tasks.attach_blocker(project.root, project.data)


def test_tracking_a_task_file_does_not_erase_a_recorded_removal(project, monkeypatch):
    tasks.reconcile(project.root, project.data)
    project.file.write_text('{"tasks":[]}')
    monkeypatch.setattr(tasks, "_task_file_tracked", lambda _: True)
    assert "User-removed" in tasks.attach_blocker(project.root, project.data)
    (project.data / "booley.toml").write_text("[sandbox]\ndashboard = false\n")
    transaction = tasks.reconcile(project.root, project.data)
    assert len(transaction.applied) == 1
    assert project.file.read_text() == '{"tasks":[]}'
    (project.data / "booley.toml").write_text("[sandbox]\ndashboard = true\n")
    assert tasks.attach_blocker(project.root, project.data) == ""


@pytest.mark.parametrize(
    "source,blocked",
    [
        (r'{"tasks":[{"label":"\u0042ooley Dashboard"}]}', True),
        ('{// "Booley Dashboard" is only a comment\n"tasks":[]}', False),
    ],
)
def test_tracked_dashboard_detection_uses_decoded_task_labels(
    project, monkeypatch, source, blocked
):
    project.file.parent.mkdir()
    project.file.write_text(source)
    monkeypatch.setattr(tasks, "_task_file_tracked", lambda _: True)
    assert bool(tasks.attach_blocker(project.root, project.data)) is blocked


def test_invalid_folder_settings_produce_a_boundary_error(project):
    tasks.reconcile(project.root, project.data)
    (project.file.parent / "settings.json").write_text("[]")
    with pytest.raises(ValueError, match="object"):
        tasks.attach_blocker(project.root, project.data)


def test_disable_retains_exclude_ownership_until_user_tail_allows_removal(project, monkeypatch):
    import json

    baseline = project.exclude.read_bytes()
    tasks.reconcile(project.root, project.data)
    owned = project.exclude.read_bytes()
    project.exclude.write_bytes(owned + b"# user appended\n/extra\n")
    (project.data / "booley.toml").write_text("[sandbox]\ndashboard = false\n")
    tasks.reconcile(project.root, project.data)
    owner_path = project.data / "runtime/dashboard-task.json"
    owner = json.loads(owner_path.read_bytes())
    assert not owner["enabled"]
    assert owner["exclude_suffix"]
    assert project.exclude.read_bytes() == owned + b"# user appended\n/extra\n"
    project.exclude.write_bytes(owned)
    tasks.reconcile(project.root, project.data)
    assert project.exclude.read_bytes() == baseline
    assert json.loads(owner_path.read_bytes())["exclude_suffix"] == ""
    assert not tasks.inspect(project.root, project.data).pending


@pytest.mark.parametrize(
    "source",
    [
        '{ // top\n "version": "2.0.0", "other": {"x": 1}, "tasks": [/*keep*/ {"label":"user","unknown":42}, // tail\n], } // end\n',
        '{"unknown":1 /*c*/} // last',
        '{"tasks": []}',
        '{"tasks": [{"label":"a"}]}',
    ],
)
def test_jsonc_preserves_unrelated_bytes_and_existing_directory_excludes(project, source):
    project.file.parent.mkdir()
    project.file.write_bytes(source.encode())
    before_exclude = project.exclude.read_bytes()
    tasks.reconcile(project.root, project.data)
    rendered = project.file.read_bytes().decode()
    assert "Booley Dashboard" in rendered
    assert Document(rendered).root.value["tasks"][-1] == tasks.TASK
    assert project.exclude.read_bytes() == before_exclude
    for token in ("// top", "/*keep*/", "// tail", "// end", "/*c*/", "// last", '"unknown":42'):
        if token in source:
            assert token in rendered
    tasks.reconcile(project.root, project.data)
    assert project.file.read_bytes().decode() == rendered
    (project.data / "booley.toml").write_bytes(b"[sandbox]\ndashboard = false\n")
    tasks.reconcile(project.root, project.data)
    assert all(
        task.get("label") != tasks.LABEL
        for task in Document(project.file.read_bytes().decode()).root.value.get("tasks", [])
    )
    for token in ("// top", "/*keep*/", "// tail", "// end", "/*c*/", "// last", '"unknown":42'):
        if token in source:
            assert token in project.file.read_bytes().decode()


@pytest.mark.parametrize(
    "source,diagnostic",
    [
        ('{"tasks":[{"label":"Booley Dashboard","command":"user"}]}', "unowned"),
        ('{"tasks":[{"label":"Booley Dashboard"},{"label":"Booley Dashboard"}]}', "duplicate"),
        ("{broken", "unavailable"),
        ('{"tasks":{}}', "array"),
        ('{"tasks":[],"tasks":[]}', "duplicate"),
    ],
)
def test_conflicts_preserved_with_diagnostic(project, source, diagnostic):
    project.file.parent.mkdir()
    project.file.write_bytes(source.encode())
    before = project.exclude.read_bytes()
    plan = tasks.inspect(project.root, project.data)
    assert not plan.pending
    assert diagnostic in plan.diagnostics[0]
    tasks.reconcile(project.root, project.data)
    assert project.file.read_bytes().decode() == source
    assert project.exclude.read_bytes() == before


@pytest.mark.parametrize(
    "edit",
    [(b'"command": "booley dashboard"', b'"command": "my-command"'), (b"\n", b"\r\n")],
    ids=["command", "line-endings"],
)
def test_user_edited_owned_task_never_overwritten_or_removed(project, monkeypatch, edit):
    tasks.reconcile(project.root, project.data)
    project.file.write_bytes(project.file.read_bytes().replace(*edit))
    edited = project.file.read_bytes()
    assert "user-edited" in tasks.inspect(project.root, project.data).diagnostics[0]
    (project.data / "booley.toml").write_text("[sandbox]\ndashboard = false\n")
    tasks.reconcile(project.root, project.data)
    assert project.file.read_bytes() == edited


@pytest.mark.parametrize("conflict", ["task-link", "directory-link", "ancestor-file"])
def test_symlink_and_ancestor_conflicts(project, tmp_path, conflict):
    target = tmp_path / "external"
    target.mkdir()
    (target / "tasks.json").write_bytes(b'{"tasks":[]}')
    if conflict == "directory-link":
        symlink_or_skip(project.file.parent, target, target_is_directory=True)
    elif conflict == "task-link":
        project.file.parent.mkdir()
        symlink_or_skip(project.file, target / "tasks.json")
    else:
        project.file.parent.write_bytes(b"ancestor conflict")
    assert tasks.inspect(project.root, project.data).diagnostics
    assert (target / "tasks.json").read_bytes() == b'{"tasks":[]}'


def test_task_reconciles_under_a_symlinked_booley_directory(project, tmp_path):
    store = tmp_path / "store"
    project.data.rmdir()
    store.mkdir()
    symlink_or_skip(project.data, store, target_is_directory=True)

    assert tasks.inspect(project.root, project.data).pending
    assert tasks.reconcile(project.root, project.data).applied
    assert (store / "runtime/dashboard-task.json").is_file()
    assert not tasks.inspect(project.root, project.data).pending

    # A link below the Booley Directory is still refused.
    (store / "runtime").rename(store / "moved")
    symlink_or_skip(store / "runtime", store / "moved", target_is_directory=True)
    assert tasks.inspect(project.root, project.data).diagnostics


def test_compare_and_swap_preserves_intervening_user_edit(project):
    transaction = tasks.TaskTransaction(tasks.inspect(project.root, project.data))
    project.file.parent.mkdir()
    project.file.write_bytes(b'{"tasks":[],"user":1}')
    with pytest.raises(OSError, match="changed since inspection"):
        transaction.apply()
    assert project.file.read_bytes() == b'{"tasks":[],"user":1}'
    assert project.exclude.read_bytes() == b"# user\n/other\n"


def test_rollback_restores_exact_bytes_and_keeps_concurrent_edits(project):
    before = project.exclude.read_bytes()
    transaction = tasks.reconcile(project.root, project.data)
    transaction.rollback()
    assert not project.file.exists()
    assert project.exclude.read_bytes() == before
    transaction = tasks.reconcile(project.root, project.data)
    project.file.write_bytes(b"user edit after publication")
    transaction.rollback()
    assert project.file.read_bytes() == b"user edit after publication"


def test_explicit_automatic_task_opt_out_and_knob(project):
    project.file.parent.mkdir()
    (project.file.parent / "settings.json").write_bytes(
        b'{//user\n"task.allowAutomaticTasks":"off"}'
    )
    assert not tasks.inspect(project.root, project.data).pending
    assert not project.file.exists()
    (project.file.parent / "settings.json").unlink()
    (project.data / "booley.toml").write_bytes(b'[sandbox]\ndashboard = "false"')
    assert tasks.inspect(project.root, project.data).diagnostics


def test_spec_builder_pure_automatic_tasks_setting(tmp_path):
    before = list(tmp_path.iterdir())
    settings = devcontainer.build_devcontainer_spec(dashboard=True)["customizations"]["vscode"][
        "settings"
    ]
    assert settings["booley.dashboard.attachProtocol"] == 1
    assert "task.allowAutomaticTasks" not in settings
    assert (
        devcontainer.build_devcontainer_spec()["customizations"]["vscode"]["settings"][
            "booley.dashboard.attachProtocol"
        ]
        == 0
    )
    assert (
        "task.allowAutomaticTasks"
        not in devcontainer.build_devcontainer_spec()["customizations"]["vscode"]["settings"]
    )
    assert list(tmp_path.iterdir()) == before


def test_issuance_success_path_reconciles_dashboard_tasks(project, monkeypatch):
    prepared = session_issuance.PreparedSessionSpec(
        {"image": "sha256:fixture"},
        "digest",
        session_issuance.SessionSpecInputs(project.data, (), (), None, None),
        None,
    )
    monkeypatch.setattr(session_issuance, "_current_keeper_id", lambda _: None)
    monkeypatch.setattr(session_issuance, "_restore_keeper", lambda *_: None)
    monkeypatch.setattr(session_issuance, "authorized_project_data_source", lambda _: project.data)

    def issued(*_args):
        assert project.file.exists() == tasks.enabled(project.data)
        return SimpleNamespace()

    monkeypatch.setattr(session_issuance, "_issue_document_with_requirements", issued)
    requirements = SimpleNamespace()
    session_issuance._persist_prepared(project.root, prepared, requirements=requirements)
    assert project.file.exists()
    (project.data / "booley.toml").write_text("[sandbox]\ndashboard = false\n")
    session_issuance._persist_prepared(project.root, prepared, requirements=requirements)
    assert not project.file.exists()


def test_symlinked_devcontainer_spec_is_refused_before_issuance(project, tmp_path):
    prepared = session_issuance.PreparedSessionSpec(
        {"image": "sha256:fixture"},
        "digest",
        session_issuance.SessionSpecInputs(project.data, (), (), None, None),
        None,
    )
    destination = tmp_path / "outside-devcontainer.json"
    destination.write_bytes(b"{}\n")
    spec_path = devcontainer.devcontainer_path(project.root)
    spec_path.parent.mkdir()
    symlink_or_skip(spec_path, destination)
    planted_target = spec_path.readlink()

    with pytest.raises(session_issuance.RuntimeSpecError, match="symlinked"):
        session_issuance._persist_prepared(project.root, prepared, requirements=SimpleNamespace())

    assert destination.read_bytes() == b"{}\n"
    assert spec_path.readlink() == planted_target
    assert not project.file.exists()


@pytest.mark.parametrize("legacy", [False, True])
def test_issuance_failure_after_task_publication_restores_previous_files(
    project, monkeypatch, legacy
):
    if legacy:
        with monkeypatch.context() as previous:
            previous.setattr(tasks, "TASK", {**tasks.TASK, "runOptions": {"runOn": "folderOpen"}})
            tasks.reconcile(project.root, project.data)
    prepared = session_issuance.PreparedSessionSpec(
        {"image": "sha256:fixture"},
        "digest",
        session_issuance.SessionSpecInputs(project.data, (), (), None, None),
        None,
    )
    paths = (project.file, project.exclude, project.data / "runtime/dashboard-task.json")
    before = {path: path.read_bytes() if path.exists() else None for path in paths}
    monkeypatch.setattr(session_issuance, "_current_keeper_id", lambda _: None)
    monkeypatch.setattr(session_issuance, "_restore_keeper", lambda *_: None)
    monkeypatch.setattr(session_issuance, "authorized_project_data_source", lambda _: project.data)

    def fail(*_args):
        assert project.file.exists()
        assert (
            "runOn" not in Document(project.file.read_text()).root.value["tasks"][0]["runOptions"]
        )
        raise session_issuance.RuntimeSpecError("fixture issuance failure")

    monkeypatch.setattr(session_issuance, "_issue_document_with_requirements", fail)
    with pytest.raises(session_issuance.RuntimeSpecError, match="fixture issuance failure"):
        session_issuance._persist_prepared(project.root, prepared, requirements=SimpleNamespace())
    assert {path: path.read_bytes() if path.exists() else None for path in paths} == before


def test_dashboard_disabled_leaves_unowned_malformed_tasks_unobserved(project, monkeypatch):
    project.file.parent.mkdir()
    project.file.write_bytes(b"user unsupported document")
    (project.data / "booley.toml").write_text("[sandbox]\ndashboard = false\n")
    assert tasks.inspect(project.root, project.data) == tasks.TaskPlan()


@pytest.mark.parametrize("baseline", [b"# user no trailing newline", b"# user\n"])
def test_disable_preserves_exact_exclude_separator_and_deleted_task(
    project, monkeypatch, baseline
):
    project.exclude.write_bytes(baseline)
    tasks.reconcile(project.root, project.data)
    project.file.unlink()  # User removal stays an opt-out while enabled.
    assert tasks.inspect(project.root, project.data).diagnostics
    (project.data / "booley.toml").write_text("[sandbox]\ndashboard = false\n")
    tasks.reconcile(project.root, project.data)
    assert not project.file.exists()
    assert project.exclude.read_bytes() == baseline


def test_owned_previous_task_revision_is_updated_without_user_content_loss(project):
    tasks.reconcile(project.root, project.data)
    owner_path = project.data / "runtime/dashboard-task.json"
    import json

    owner = json.loads(owner_path.read_bytes())
    previous = owner["task"].replace('"clear": false', '"clear": true')
    project.file.write_bytes(
        project.file.read_bytes().replace(owner["task"].encode(), previous.encode())
    )
    owner["task"] = previous
    owner_path.write_bytes(json.dumps(owner).encode())
    tasks.reconcile(project.root, project.data)
    assert Document(project.file.read_bytes().decode()).root.value["tasks"][0] == tasks.TASK


def test_disable_restores_absent_exclude_file(project, monkeypatch):
    project.exclude.unlink()
    tasks.reconcile(project.root, project.data)
    assert project.exclude.exists()
    (project.data / "booley.toml").write_text("[sandbox]\ndashboard = false\n")
    tasks.reconcile(project.root, project.data)
    assert not project.exclude.exists()


@pytest.mark.parametrize(
    "source",
    [
        '{\n    "version": "2.0.0",\n    "tasks": [\n        {"label":"user"},\n\n    ],\n}\n',
        '{"tasks":[{"label":"user"}]}',
        '{"tasks": []}\n\n',
        '{"unrelated": true,}\n',
    ],
)
def test_enable_disable_cycles_restore_exact_jsonc_bytes(project, source):
    project.file.parent.mkdir()
    original = source.encode()
    for _ in range(3):
        project.file.write_bytes(original)
        (project.data / "booley.toml").write_bytes(b"[sandbox]\ndashboard = true\n")
        tasks.reconcile(project.root, project.data)
        rendered = project.file.read_bytes().decode()
        assert Document(rendered).root.value["tasks"][-1] == tasks.TASK
        if '    "tasks"' in source:
            assert '\n        {\n            "label": "Booley Dashboard"' in rendered
        (project.data / "booley.toml").write_bytes(b"[sandbox]\ndashboard = false\n")
        tasks.reconcile(project.root, project.data)
        assert project.file.read_bytes() == original


def test_disable_removes_only_booley_created_empty_vscode(project):
    tasks.reconcile(project.root, project.data)
    (project.data / "booley.toml").write_bytes(b"[sandbox]\ndashboard = false\n")
    tasks.reconcile(project.root, project.data)
    assert not project.file.parent.exists()


def test_disable_preserves_user_edits_outside_owned_insertion(project):
    project.file.parent.mkdir()
    source = b'{"tasks": [{"label":"user"},], "value": 1}\n'
    project.file.write_bytes(source)
    tasks.reconcile(project.root, project.data)
    project.file.write_bytes(project.file.read_bytes().replace(b'"value": 1', b'"value": 2'))
    (project.data / "booley.toml").write_bytes(b"[sandbox]\ndashboard = false\n")
    tasks.reconcile(project.root, project.data)
    assert project.file.read_bytes() == source.replace(b'"value": 1', b'"value": 2')


def test_busy_task_lock_skips_with_diagnostic_and_preserves_files(project):
    from booley.core.file_lock import nonblocking_file_lock

    lock = project.data / "runtime/dashboard-task.lock"
    lock.parent.mkdir()
    with lock.open("a+") as handle, nonblocking_file_lock(handle):
        transaction = tasks.reconcile(project.root, project.data)
    assert not transaction.applied
    assert "busy" in transaction.plan.diagnostics[0]
    assert not project.file.exists()


def test_disable_keeps_user_created_empty_vscode(project):
    project.file.parent.mkdir()
    tasks.reconcile(project.root, project.data)
    (project.data / "booley.toml").write_bytes(b"[sandbox]\ndashboard = false\n")
    tasks.reconcile(project.root, project.data)
    assert project.file.parent.is_dir()


def test_reenable_does_not_claim_user_recreated_vscode(project, monkeypatch):
    tasks.reconcile(project.root, project.data)
    (project.data / "booley.toml").write_text("[sandbox]\ndashboard = false\n")
    tasks.reconcile(project.root, project.data)
    assert not project.file.parent.exists()
    project.file.parent.mkdir()
    user_file = project.file.parent / "user.txt"
    user_file.write_bytes(b"keep")
    baseline = project.exclude.read_bytes()
    (project.data / "booley.toml").write_text("[sandbox]\ndashboard = true\n")
    tasks.reconcile(project.root, project.data)
    assert project.exclude.read_bytes() == baseline
    (project.data / "booley.toml").write_text("[sandbox]\ndashboard = false\n")
    tasks.reconcile(project.root, project.data)
    assert user_file.read_bytes() == b"keep"
    assert project.file.parent.exists()


@pytest.mark.parametrize(
    "source",
    [
        '{ // keep café\r\n  "tasks": [/*user*/ {"label":"user"},],\r\n  "other": 42,\r\n}\r\n',
        '{\r\n  "version": "2.0.0", // keep\r\n  "other": 42,\r\n}\r\n',
    ],
)
def test_crlf_task_enable_update_disable_restores_exact_bytes(project, monkeypatch, source):
    original = source.encode()
    project.file.parent.mkdir()
    project.file.write_bytes(original)
    exclude = project.exclude.read_bytes()
    assert tasks.reconcile(project.root, project.data).applied
    enabled = project.file.read_bytes()
    assert b"\n" not in enabled.replace(b"\r\n", b"")
    assert not tasks.inspect(project.root, project.data).pending
    upgraded = {**tasks.TASK, "presentation": {**tasks.TASK["presentation"], "clear": True}}
    monkeypatch.setattr(tasks, "TASK", upgraded)
    assert tasks.reconcile(project.root, project.data).applied
    updated = project.file.read_bytes()
    assert b"\n" not in updated.replace(b"\r\n", b"")
    assert updated != enabled and updated.count(tasks.LABEL.encode()) == 1
    assert Document(updated.decode()).root.value["tasks"][-1] == upgraded
    assert not tasks.inspect(project.root, project.data).pending
    (project.data / "booley.toml").write_bytes(b"[sandbox]\r\ndashboard = false\r\n")
    tasks.reconcile(project.root, project.data)
    assert project.file.read_bytes() == original
    assert project.exclude.read_bytes() == exclude


def test_owned_task_update_removes_environment_and_keeps_disable_reversible(project, monkeypatch):
    obsolete = {
        **tasks.TASK,
        "options": {"env": {"BOOLEY_" + "GOAL_MODE" + "_PREVIEW": "1"}},
    }
    baseline = project.exclude.read_bytes()
    with monkeypatch.context() as prior:
        prior.setattr(tasks, "TASK", obsolete)
        tasks.reconcile(project.root, project.data)
    assert "options" in Document(project.file.read_text()).root.value["tasks"][0]
    assert tasks.reconcile(project.root, project.data).applied
    assert Document(project.file.read_text()).root.value["tasks"] == [tasks.TASK]
    assert not tasks.inspect(project.root, project.data).pending
    (project.data / "booley.toml").write_text("[sandbox]\ndashboard = false\n")
    tasks.reconcile(project.root, project.data)
    assert not project.file.exists()
    assert project.exclude.read_bytes() == baseline


def test_new_non_git_directory_silently_has_no_dashboard_plan(tmp_path, caplog):
    root = tmp_path / "new"
    root.mkdir()
    booley_dir = root / ".booley"
    assert tasks.inspect(root, booley_dir) == tasks.TaskPlan()
    assert not tasks.reconcile(root, booley_dir).applied
    assert caplog.records == []
    assert list(root.iterdir()) == []


@pytest.mark.parametrize("unit,newline", [("  ", "\n"), ("    ", "\r\n"), ("\t", "\n")])
def test_owned_task_upgrade_preserves_indent_and_unrelated_text(
    project, monkeypatch, unit, newline
):
    import json

    old_task = {**tasks.TASK, "options": {"env": {"old-preview": "1"}}}
    source = (
        '{"version": "2.0.0", "tasks": [\n'
        + unit * 2
        + json.dumps(old_task, indent=unit).replace("\n", "\n" + unit * 2)
        + "\n]}\n"
    )
    source = source.replace("\n", newline)
    project.file.parent.mkdir()
    project.file.write_bytes(source.encode())
    owner = project.data / "runtime/dashboard-task.json"
    owner.parent.mkdir()
    raw = Document(source).root.members["tasks"].children[0]
    owner.write_text(
        json.dumps(
            {
                "schema": 1,
                "root": str(project.root.resolve()),
                "enabled": True,
                "task": source[raw.start : raw.end],
            }
        )
    )
    tasks.reconcile(project.root, project.data)
    after = project.file.read_bytes().decode()
    assert newline + unit * 3 + '"label":' in after
    assert newline + unit * 2 + "}" in after
    assert "options" not in after
    assert after.startswith('{"version": "2.0.0", "tasks": [')
    assert after.endswith(newline + "]}" + newline)
    assert not tasks.inspect(project.root, project.data).pending


def test_task_inspection_resolves_git_directories_once(project, monkeypatch):
    original = tasks.git_directories
    calls = []

    def directories(root):
        calls.append(root)
        return original(root)

    monkeypatch.setattr(tasks, "git_directories", directories)
    assert tasks.inspect(project.root, project.data).pending
    assert calls == [project.root]


@pytest.mark.parametrize("owned", [False, True])
@pytest.mark.parametrize("disabled", [False, True])
def test_tracked_task_file_is_preserved_with_informational_notice(
    tmp_path, monkeypatch, owned, disabled
):
    def git(*args):
        result = subprocess.run(
            ["git", *args], cwd=tmp_path, capture_output=True, text=True, timeout=30, check=True
        )
        return result.stdout

    git("init", "-q")
    data = tmp_path / ".booley"
    data.mkdir()
    task_file = tmp_path / ".vscode/tasks.json"
    if owned:
        tasks.reconcile(tmp_path, data)
    else:
        task_file.parent.mkdir()
        task_file.write_text('{"version":"2.0.0","tasks":[]}\n')
    git("add", "-f", ".vscode/tasks.json")
    if disabled:
        (data / "booley.toml").write_text("[sandbox]\ndashboard = false\n")
    before = {
        path.relative_to(tmp_path): path.read_bytes()
        for path in tmp_path.rglob("*")
        if path.is_file() and ".git" not in path.parts
    }
    with monkeypatch.context() as changed_task:
        changed_task.setattr(tasks, "TASK", {**tasks.TASK, "command": "booley dashboard --new"})
        plan = tasks.inspect(tmp_path, data)
        assert plan.pending == (owned and disabled)
        assert not plan.diagnostics
        # An owned task already carries the Dashboard label: nothing to tell the user.
        assert len(plan.notices) == (0 if disabled or owned else 1)
        if not disabled and not owned:
            assert plan.notices == (
                "Dashboard task not installed: .vscode/tasks.json is tracked by Git; add the task yourself or set [sandbox].dashboard = false",
            )
        assert bool(tasks.reconcile(tmp_path, data).applied) == (owned and disabled)
    after = {
        path.relative_to(tmp_path): path.read_bytes()
        for path in tmp_path.rglob("*")
        if path.is_file() and ".git" not in path.parts
    }
    if owned and disabled:
        owner = Path(".booley/runtime/dashboard-task.json")
        assert json.loads(after.pop(owner))["enabled"] is False
        before.pop(owner)
    assert after == before


@pytest.mark.parametrize("check_only", [False, True])
def test_init_tracked_task_notice_is_one_informational_line(
    tmp_path, monkeypatch, capsys, check_only
):
    from booley.harness import init_cmd
    from booley.harness.setup.common import InitContext

    notice = (
        "Dashboard task not installed: .vscode/tasks.json is tracked by Git; "
        "add the task yourself or set [sandbox].dashboard = false"
    )
    plan = SimpleNamespace(dashboard_tasks=tasks.TaskPlan(notices=(notice,)), pending_details=())
    monkeypatch.setattr(init_cmd, "_interactive_precondition_failed", lambda *a, **kw: False)
    monkeypatch.setattr(
        init_cmd,
        "_interactive_spec_sources",
        lambda *a, **kw: SimpleNamespace(build=lambda: None, mask_paths=()),
    )
    monkeypatch.setattr(init_cmd, "_inspect_interactive_plan", lambda *a: plan)
    monkeypatch.setattr(
        init_cmd,
        "_apply_interactive_plan",
        lambda *a: pytest.fail("informational plan should not need changes"),
    )
    ctx = InitContext(project_root=tmp_path, check_only=check_only)
    init_cmd._step_interactive(ctx)
    output = capsys.readouterr().out
    assert output.count(notice) == 1
    assert "  !  " not in output
    assert ctx.results[-1].status == "current"


@pytest.mark.parametrize(
    "failure",
    [
        subprocess.CompletedProcess([], 128, "", "Git index unavailable"),
        subprocess.TimeoutExpired(cmd="git", timeout=30),
        OSError("git missing"),
    ],
    ids=["exit-128", "timeout", "start-failure"],
)
def test_git_tracking_failure_refuses_task_mutation(project, monkeypatch, failure):
    def ls_files(*_args, **_kwargs):
        if isinstance(failure, BaseException):
            raise failure
        return failure

    monkeypatch.setattr(tasks, "_task_file_tracked", _REAL_TASK_FILE_TRACKED)
    monkeypatch.setattr(tasks.subprocess, "run", ls_files)
    plan = tasks.inspect(project.root, project.data)
    assert not plan.pending
    assert "could not inspect Dashboard task Git tracking" in plan.diagnostics[0]
    assert not project.file.exists()


_NEWLINE_CASES = [
    pytest.param("/* café\n\n\n\r\n */", "\n", id="lf-majority"),
    pytest.param("/* café\r\n\r\n\r\n\n */", "\r\n", id="crlf-majority"),
    pytest.param("/* café\n\n */", "\n", id="lf"),
    pytest.param("/* café\r\n\r\n */", "\r\n", id="crlf"),
    pytest.param("/* café\n\r\n */", "\n", id="tie"),
    pytest.param("/* café */", "\n", id="no-breaks"),
]


@pytest.mark.parametrize(
    "comment,newline",
    [
        *_NEWLINE_CASES,
        pytest.param("/* café\r\r */", "\n", id="lone-cr"),
        pytest.param("/* café\r\r\n\n */", "\n", id="lone-cr-tie"),
    ],
)
def test_jsonc_document_uses_dominant_newline(comment, newline):
    source = comment + '{"escaped":"\\n", "tasks":[]}'
    document = Document(source)
    assert document.newline == newline
    assert document.source == source


@pytest.mark.parametrize("comment,newline", _NEWLINE_CASES)
@pytest.mark.parametrize("layout", ["empty", "user", "missing"])
def test_dominant_newline_task_enable_disable_restores_exact_bytes(
    project, comment, newline, layout
):
    prefix = (comment + '{"escaped":"\\n",').encode()
    if layout == "missing":
        prefix += b'"other":42'
        suffix = b"}"
    else:
        prefix += b'"tasks":['
        if layout == "user":
            prefix += b'{"label":"user"}'
        suffix = b"]}"
    original = prefix + suffix
    project.file.parent.mkdir()
    project.file.write_bytes(original)
    exclude = project.exclude.read_bytes()
    assert tasks.reconcile(project.root, project.data).applied
    rendered = project.file.read_bytes()
    assert rendered.startswith(prefix) and rendered.endswith(suffix)
    insertion = rendered[len(prefix) : -len(suffix)]
    assert rendered == prefix + insertion + suffix
    _assert_inserted_newlines(insertion, newline)
    assert Document(rendered.decode()).root.value["tasks"][-1] == tasks.TASK
    assert not tasks.reconcile(project.root, project.data).applied
    (project.data / "booley.toml").write_bytes(b"[sandbox]\ndashboard = false\n")
    tasks.reconcile(project.root, project.data)
    assert project.file.read_bytes() == original
    assert project.exclude.read_bytes() == exclude


def _assert_inserted_newlines(insertion, newline):
    assert b"\n" in insertion
    if newline == "\r\n":
        assert b"\n" not in insertion.replace(b"\r\n", b"")
    else:
        assert b"\r\n" not in insertion


@pytest.mark.parametrize("layout", ["user", "missing"])
def test_dominant_newline_legacy_task_update_restores_exact_bytes(project, monkeypatch, layout):
    comment = "/* café" + "\n" * 40 + "\r\n */"
    content = '{"tasks":[{"label":"user"}]}' if layout == "user" else '{"other":42}'
    original = (comment + content).encode()
    project.file.parent.mkdir()
    project.file.write_bytes(original)
    exclude = project.exclude.read_bytes()

    class LegacyDocument(Document):
        def __init__(self, source):
            super().__init__(source)
            self.newline = "\r\n"

    with monkeypatch.context() as legacy:
        legacy.setattr(tasks, "Document", LegacyDocument)
        assert tasks.reconcile(project.root, project.data).applied
    enabled = project.file.read_bytes()
    old = Document(enabled.decode()).root.members["tasks"].children[-1]
    # UTF-8 spans are character offsets; slice before encoding the original body.
    old_body = enabled.decode()[old.start : old.end].encode()
    assert b"\r\n" in old_body
    upgraded = {**tasks.TASK, "presentation": {**tasks.TASK["presentation"], "clear": True}}
    monkeypatch.setattr(tasks, "TASK", upgraded)
    assert tasks.reconcile(project.root, project.data).applied
    updated = project.file.read_bytes()
    parsed = Document(updated.decode())
    node = parsed.root.members["tasks"].children[-1]
    new_body = updated.decode()[node.start : node.end].encode()
    _assert_inserted_newlines(new_body, "\n")
    assert updated == enabled.replace(old_body, new_body, 1)
    assert parsed.root.value["tasks"][-1] == upgraded
    assert not tasks.reconcile(project.root, project.data).applied
    (project.data / "booley.toml").write_bytes(b"[sandbox]\ndashboard = false\n")
    tasks.reconcile(project.root, project.data)
    assert project.file.read_bytes() == original
    assert project.exclude.read_bytes() == exclude
