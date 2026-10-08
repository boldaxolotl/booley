"""Task ownership, JSONC preservation, rollback and deployment gates without Docker/Git."""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from booley.runtime import dashboard_tasks as tasks
from booley.runtime import devcontainer, session_issuance
from booley.runtime.jsonc import Document
from tests.conftest import symlink_or_skip


@pytest.fixture
def project(tmp_path, monkeypatch):
    monkeypatch.setenv("BOOLEY_GOAL_MODE_PREVIEW", "1")
    root = tmp_path / "project"
    root.mkdir()
    data = root / ".booley_project"
    data.mkdir()
    common = root / "git-admin"
    (common / "info").mkdir(parents=True)
    (common / "info/exclude").write_bytes(b"# user\n/other\n")
    monkeypatch.setattr(tasks, "git_directories", lambda _: SimpleNamespace(common_dir=common))
    monkeypatch.setattr(session_issuance, "stamp_path_for_identity", lambda _: root / "stamp.json")
    monkeypatch.setattr(
        session_issuance, "_legacy_stamp_path", lambda _: root / "legacy-stamp.json"
    )
    return SimpleNamespace(
        root=root, data=data, exclude=common / "info/exclude", file=root / ".vscode/tasks.json"
    )


def test_new_directory_task_preview_env_and_idempotent_disable(project, monkeypatch):
    baseline = project.exclude.read_bytes()
    assert tasks.inspect(project.root, project.data).pending
    assert not project.file.exists()
    transaction = tasks.reconcile(project.root, project.data)
    assert transaction.applied
    task = Document(project.file.read_text()).root.value["tasks"][0]
    assert task["command"] == "booley dashboard"
    assert task["options"]["env"] == {"BOOLEY_GOAL_MODE_PREVIEW": "1"}
    assert task["runOptions"] == {"runOn": "folderOpen", "instanceLimit": 1}
    assert project.exclude.read_bytes().startswith(baseline)
    assert not tasks.inspect(project.root, project.data).pending
    monkeypatch.delenv("BOOLEY_GOAL_MODE_PREVIEW")
    tasks.reconcile(project.root, project.data)
    assert not project.file.exists()
    assert project.exclude.read_bytes() == baseline
    monkeypatch.setenv("BOOLEY_GOAL_MODE_PREVIEW", "1")
    tasks.reconcile(project.root, project.data)
    assert b"/.vscode\n" in project.exclude.read_bytes()


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
    project.file.write_text(source)
    before_exclude = project.exclude.read_bytes()
    tasks.reconcile(project.root, project.data)
    rendered = project.file.read_text()
    assert "Booley Dashboard" in rendered
    assert Document(rendered).root.value["tasks"][-1] == tasks.TASK
    assert project.exclude.read_bytes() == before_exclude
    for token in ("// top", "/*keep*/", "// tail", "// end", "/*c*/", "// last", '"unknown":42'):
        if token in source:
            assert token in rendered
    tasks.reconcile(project.root, project.data)
    assert project.file.read_text() == rendered
    (project.data / "booley.toml").write_text("[sandbox]\ndashboard = false\n")
    tasks.reconcile(project.root, project.data)
    assert all(
        task.get("label") != tasks.LABEL
        for task in Document(project.file.read_text()).root.value.get("tasks", [])
    )
    for token in ("// top", "/*keep*/", "// tail", "// end", "/*c*/", "// last", '"unknown":42'):
        if token in source:
            assert token in project.file.read_text()


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
    project.file.write_text(source)
    before = project.exclude.read_bytes()
    plan = tasks.inspect(project.root, project.data)
    assert not plan.pending
    assert diagnostic in plan.diagnostics[0]
    tasks.reconcile(project.root, project.data)
    assert project.file.read_text() == source
    assert project.exclude.read_bytes() == before


def test_user_edited_owned_task_never_overwritten_or_removed(project, monkeypatch):
    tasks.reconcile(project.root, project.data)
    project.file.write_text(
        project.file.read_text().replace(
            '"command": "booley dashboard"', '"command": "my-command"'
        )
    )
    edited = project.file.read_bytes()
    assert "user-edited" in tasks.inspect(project.root, project.data).diagnostics[0]
    monkeypatch.delenv("BOOLEY_GOAL_MODE_PREVIEW")
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
    (project.file.parent / "settings.json").write_text(
        '{//user\n"task.allowAutomaticTasks":"off"}'
    )
    assert not tasks.inspect(project.root, project.data).pending
    assert not project.file.exists()
    (project.file.parent / "settings.json").unlink()
    (project.data / "booley.toml").write_text('[sandbox]\ndashboard = "false"')
    assert tasks.inspect(project.root, project.data).diagnostics


def test_spec_builder_pure_automatic_tasks_setting(tmp_path):
    before = list(tmp_path.iterdir())
    settings = devcontainer.build_devcontainer_spec(dashboard=True)["customizations"]["vscode"][
        "settings"
    ]
    assert settings["task.allowAutomaticTasks"] == "on"
    assert (
        "task.allowAutomaticTasks"
        not in devcontainer.build_devcontainer_spec()["customizations"]["vscode"]["settings"]
    )
    assert list(tmp_path.iterdir()) == before


@pytest.mark.parametrize("requirements", [None, SimpleNamespace()])
def test_both_issuance_success_paths_and_failure_rollback(project, monkeypatch, requirements):
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

    monkeypatch.setattr(session_issuance, "_issue_document", issued)
    monkeypatch.setattr(session_issuance, "_issue_document_with_requirements", issued)
    session_issuance._persist_prepared(project.root, prepared, requirements=requirements)
    assert project.file.exists()
    monkeypatch.setenv("BOOLEY_GOAL_MODE_PREVIEW", "0")
    session_issuance._persist_prepared(project.root, prepared, requirements=requirements)
    assert not project.file.exists()


def test_issuance_failure_after_task_publication_restores_previous_files(project, monkeypatch):
    prepared = session_issuance.PreparedSessionSpec(
        {"image": "sha256:fixture"},
        "digest",
        session_issuance.SessionSpecInputs(project.data, (), (), None, None),
        None,
    )
    before = project.exclude.read_bytes()
    monkeypatch.setattr(session_issuance, "_current_keeper_id", lambda _: None)
    monkeypatch.setattr(session_issuance, "_restore_keeper", lambda *_: None)

    def fail(*_args):
        assert project.file.exists()
        raise session_issuance.RuntimeSpecError("fixture issuance failure")

    monkeypatch.setattr(session_issuance, "_issue_document", fail)
    with pytest.raises(session_issuance.RuntimeSpecError, match="fixture issuance failure"):
        session_issuance._persist_prepared(project.root, prepared)
    assert not project.file.exists()
    assert project.exclude.read_bytes() == before
    assert not (project.data / "runtime/dashboard-task.json").exists()


def test_preview_off_leaves_existing_unowned_malformed_tasks_unobserved(project, monkeypatch):
    project.file.parent.mkdir()
    project.file.write_bytes(b"user unsupported document")
    monkeypatch.delenv("BOOLEY_GOAL_MODE_PREVIEW")
    assert tasks.inspect(project.root, project.data) == tasks.TaskPlan()


def test_preview_env_is_sealed_into_container_for_mcp_and_task():
    spec = devcontainer.build_devcontainer_spec(goal_preview=True, dashboard=False)
    assert spec["containerEnv"]["BOOLEY_GOAL_MODE_PREVIEW"] == "1"
    assert "BOOLEY_GOAL_MODE_PREVIEW" not in spec["remoteEnv"]
    assert "BOOLEY_GOAL_MODE_PREVIEW" not in devcontainer.build_devcontainer_spec()["containerEnv"]


@pytest.mark.parametrize("baseline", [b"# user no trailing newline", b"# user\n"])
def test_disable_preserves_exact_exclude_separator_and_deleted_task(
    project, monkeypatch, baseline
):
    project.exclude.write_bytes(baseline)
    tasks.reconcile(project.root, project.data)
    project.file.unlink()  # User removal stays an opt-out while enabled.
    assert tasks.inspect(project.root, project.data).diagnostics
    monkeypatch.setenv("BOOLEY_GOAL_MODE_PREVIEW", "0")
    tasks.reconcile(project.root, project.data)
    assert not project.file.exists()
    assert project.exclude.read_bytes() == baseline


def test_owned_previous_task_revision_is_updated_without_user_content_loss(project):
    tasks.reconcile(project.root, project.data)
    owner_path = project.data / "runtime/dashboard-task.json"
    import json

    owner = json.loads(owner_path.read_bytes())
    previous = owner["task"].replace('"clear": false', '"clear": true')
    project.file.write_text(project.file.read_text().replace(owner["task"], previous))
    owner["task"] = previous
    owner_path.write_text(json.dumps(owner))
    tasks.reconcile(project.root, project.data)
    assert Document(project.file.read_text()).root.value["tasks"][0] == tasks.TASK


def test_disable_restores_absent_exclude_file(project, monkeypatch):
    project.exclude.unlink()
    tasks.reconcile(project.root, project.data)
    assert project.exclude.exists()
    monkeypatch.setenv("BOOLEY_GOAL_MODE_PREVIEW", "0")
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
        (project.data / "booley.toml").write_text("[sandbox]\ndashboard = true\n")
        tasks.reconcile(project.root, project.data)
        rendered = project.file.read_text()
        assert Document(rendered).root.value["tasks"][-1] == tasks.TASK
        if '    "tasks"' in source:
            assert '\n        {\n            "label": "Booley Dashboard"' in rendered
        (project.data / "booley.toml").write_text("[sandbox]\ndashboard = false\n")
        tasks.reconcile(project.root, project.data)
        assert project.file.read_bytes() == original


def test_disable_removes_only_booley_created_empty_vscode(project):
    tasks.reconcile(project.root, project.data)
    (project.data / "booley.toml").write_text("[sandbox]\ndashboard = false\n")
    tasks.reconcile(project.root, project.data)
    assert not project.file.parent.exists()


def test_disable_preserves_user_edits_outside_owned_insertion(project):
    project.file.parent.mkdir()
    source = b'{"tasks": [{"label":"user"},], "value": 1}\n'
    project.file.write_bytes(source)
    tasks.reconcile(project.root, project.data)
    project.file.write_bytes(project.file.read_bytes().replace(b'"value": 1', b'"value": 2'))
    (project.data / "booley.toml").write_text("[sandbox]\ndashboard = false\n")
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
    (project.data / "booley.toml").write_text("[sandbox]\ndashboard = false\n")
    tasks.reconcile(project.root, project.data)
    assert project.file.parent.is_dir()
