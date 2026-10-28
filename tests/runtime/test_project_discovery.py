"""Project discovery distinguishes the checkout from its data repositories."""

import os
import subprocess
from pathlib import Path

import pytest
from tests.conftest import symlink_or_skip


@pytest.mark.parametrize("name", [".booley", "custom-data"])
def test_all_checkout_local_data_selections(tmp_path, monkeypatch, name):
    from booley.runtime.project_discovery import discover_project_root

    outer = tmp_path / "checkout"
    data = outer / name
    data.mkdir(parents=True)
    for root in (outer, data):
        subprocess.run(["git", "init", str(root)], check=True, capture_output=True, timeout=30)
    if name == "custom-data":
        (outer / "booley.toml").write_text('[project]\ndir = "custom-data"\n')
    child = data / "child"
    child.mkdir()
    monkeypatch.delenv("RTL_PROJECT_ROOT", raising=False)
    monkeypatch.delenv("BOOLEY_DIR", raising=False)
    assert discover_project_root(child) == outer
    monkeypatch.chdir(data)
    assert discover_project_root() == outer


def test_unrelated_nested_checkout_keeps_identity(tmp_path, monkeypatch):
    from booley.runtime.project_discovery import discover_project_root

    nested = tmp_path / "nested"
    nested.mkdir()
    for root in (tmp_path, nested):
        subprocess.run(["git", "init", str(root)], check=True, capture_output=True, timeout=30)
    monkeypatch.delenv("RTL_PROJECT_ROOT", raising=False)
    monkeypatch.delenv("BOOLEY_DIR", raising=False)
    assert discover_project_root(nested) == nested


@pytest.mark.parametrize("explicit", [False, True])
def test_ticket_design_worktree_inside_data_keeps_checkout(tmp_path, monkeypatch, explicit):
    from booley.runtime.project_discovery import discover_project_root

    outer = tmp_path / "checkout"
    data = outer / ".booley"
    data.mkdir(parents=True)
    subprocess.run(["git", "init", str(outer)], check=True, capture_output=True, timeout=30)
    subprocess.run(
        [
            "git",
            "-C",
            str(outer),
            "-c",
            "user.name=Test",
            "-c",
            "user.email=test@example.com",
            "commit",
            "--allow-empty",
            "-m",
            "initial",
        ],
        check=True,
        capture_output=True,
        timeout=30,
    )
    worktree = data / "worktrees/ticket"
    subprocess.run(
        ["git", "-C", str(outer), "worktree", "add", "--detach", str(worktree)],
        check=True,
        capture_output=True,
        timeout=30,
    )
    snapshot = worktree / ".booley"
    snapshot.mkdir()
    subprocess.run(["git", "init", str(snapshot)], check=True, capture_output=True, timeout=30)
    monkeypatch.setenv("BOOLEY_DIR", str(data))
    if explicit:
        monkeypatch.setenv("RTL_PROJECT_ROOT", str(worktree))
    else:
        monkeypatch.delenv("RTL_PROJECT_ROOT", raising=False)
    assert discover_project_root(worktree) == worktree
    assert discover_project_root(snapshot) == worktree
    if explicit:
        assert discover_project_root(data) == worktree


def test_detached_data_refuses_and_explicit_outer_disambiguates(tmp_path, monkeypatch):
    from booley.runtime.project_discovery import ProjectRootDiscoveryError, discover_project_root

    data = tmp_path / "data"
    outer = tmp_path / "checkout"
    data.mkdir()
    outer.mkdir()
    monkeypatch.setenv("BOOLEY_DIR", str(data))
    monkeypatch.delenv("RTL_PROJECT_ROOT", raising=False)
    with pytest.raises(ProjectRootDiscoveryError, match="run from the Project Root"):
        discover_project_root(data)
    monkeypatch.setenv("RTL_PROJECT_ROOT", str(data))
    with pytest.raises(ProjectRootDiscoveryError):
        discover_project_root(data)
    monkeypatch.setenv("RTL_PROJECT_ROOT", str(outer))
    assert discover_project_root(data) == outer


def test_empty_git_marker_and_fallback_priority(tmp_path, monkeypatch):
    from booley.runtime.shared_infra import resolve_project_root

    (tmp_path / ".git").mkdir()
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("RTL_PROJECT_ROOT", raising=False)
    monkeypatch.delenv("BOOLEY_DIR", raising=False)
    fallback = tmp_path / "fallback"
    assert resolve_project_root(fallback) == fallback
    monkeypatch.setenv("RTL_PROJECT_ROOT", str(tmp_path / "explicit"))
    assert resolve_project_root(fallback) == tmp_path / "explicit"


def test_linked_git_marker_and_symlink_data(tmp_path, monkeypatch):
    from booley.runtime.project_discovery import discover_project_root

    outer = tmp_path / "checkout"
    data = outer / ".booley"
    data.mkdir(parents=True)
    (outer / ".git").write_text("gitdir: /unused/metadata\n")
    alias = tmp_path / "alias"
    alias.symlink_to(data, target_is_directory=True)
    monkeypatch.delenv("RTL_PROJECT_ROOT", raising=False)
    monkeypatch.delenv("BOOLEY_DIR", raising=False)
    assert discover_project_root(alias) == outer


@pytest.mark.parametrize("retired", [False, True])
@pytest.mark.parametrize("stale_data", [False, True])
def test_source_stale_data_refuses_goal_but_feedback_routes(
    tmp_path, monkeypatch, stale_data, retired
):
    from booley.harness.booley import _command_project_root
    from booley.runtime.project_discovery import ProjectRootDiscoveryError

    data = tmp_path / ".booley"
    data.mkdir()
    (tmp_path / "pyproject.toml").write_text("[tool.booley]\nsource_checkout = true\n")
    subprocess.run(["git", "init", str(tmp_path)], check=True, capture_output=True, timeout=30)
    monkeypatch.chdir(data if stale_data else tmp_path)
    for name in ("RTL_PROJECT_ROOT", "BOOLEY_DIR", "BOOLEY_CONTAINER", "BOOLEY_PROJECT_DIR"):
        monkeypatch.delenv(name, raising=False)
    if retired:
        monkeypatch.setenv("BOOLEY_PROJECT_DIR", "")
    if stale_data:
        with pytest.raises(ProjectRootDiscoveryError):
            _command_project_root("goal")
    else:
        assert _command_project_root("goal") == tmp_path
    assert _command_project_root("feedback") == tmp_path


@pytest.mark.parametrize("use_start", [False, True])
def test_checkout_owned_external_symlink_data_resolves_logical_parent(
    tmp_path, monkeypatch, use_start
):
    from booley.runtime.project_discovery import discover_project_root

    outer = tmp_path / "checkout"
    data = tmp_path / "external-data"
    outer.mkdir()
    data.mkdir()
    symlink_or_skip(outer / ".booley", data, target_is_directory=True)
    for root in (outer, data):
        subprocess.run(["git", "init", str(root)], check=True, capture_output=True, timeout=30)
    monkeypatch.delenv("RTL_PROJECT_ROOT", raising=False)
    monkeypatch.delenv("BOOLEY_DIR", raising=False)
    monkeypatch.chdir(data)
    monkeypatch.setenv("PWD", str(outer / ".booley"))
    assert discover_project_root(outer / ".booley" if use_start else None) == outer


@pytest.mark.parametrize("config", ['project = "invalid"', "[project]\ndir = 12"])
def test_malformed_project_selection_does_not_crash_discovery(tmp_path, monkeypatch, config):
    from booley.runtime.project_discovery import discover_project_root

    subprocess.run(["git", "init", str(tmp_path)], check=True, capture_output=True, timeout=30)
    (tmp_path / "booley.toml").write_text(config)
    monkeypatch.delenv("RTL_PROJECT_ROOT", raising=False)
    monkeypatch.delenv("BOOLEY_DIR", raising=False)
    assert discover_project_root(tmp_path) == tmp_path


def test_explicit_external_data_symlink_is_refused(tmp_path, monkeypatch):
    from booley.runtime.project_discovery import ProjectRootDiscoveryError, discover_project_root

    outer = tmp_path / "checkout"
    data = tmp_path / "external-data"
    outer.mkdir()
    data.mkdir()
    alias = outer / ".booley"
    symlink_or_skip(alias, data, target_is_directory=True)
    for root in (outer, data):
        subprocess.run(["git", "init", str(root)], check=True, capture_output=True, timeout=30)
    monkeypatch.delenv("BOOLEY_DIR", raising=False)
    monkeypatch.setenv("RTL_PROJECT_ROOT", str(alias))
    with pytest.raises(ProjectRootDiscoveryError, match="run from the Project Root"):
        discover_project_root(outer)


def test_containment_alias_explicit_authority(tmp_path, monkeypatch):
    from pathlib import Path

    from booley.runtime import project_discovery

    root, alias, unrelated = (tmp_path / name for name in ("selected", "alias", "unrelated"))
    for directory in (root, alias, unrelated):
        directory.mkdir()
    resolve = project_discovery.resolve_authoritative_booley_dir
    original = Path.samefile
    calls = []

    def samefile(path, other):
        if {path, Path(other)} == {root, alias}:
            calls.append(path)
            return True
        return original(path, other)

    monkeypatch.setattr(Path, "samefile", samefile)
    monkeypatch.setenv("BOOLEY_DIR", str(root))
    assert project_discovery._data_root(alias / "new", [], resolve) == root
    assert calls
    nested = alias / "embedded"
    nested.mkdir()
    assert project_discovery._data_root(nested, [nested], resolve) is None
    (nested / ".git").mkdir()
    (nested / ".git" / "HEAD").write_text("ref: refs/heads/main")
    (nested / "booley.toml").write_text(f'[project]\ndir = "{root.as_posix()}"\n')
    owner = tmp_path / "owner"
    owner.mkdir()
    (owner / ".git").mkdir()
    (owner / ".git" / "HEAD").write_text("ref: refs/heads/main")
    (owner / "booley.toml").write_text(f'[project]\ndir = "{root.as_posix()}"\n')
    monkeypatch.setenv("BOOLEY_DIR", str(unrelated))
    from booley.runtime.booley_dir import reset_cache, resolve_booley_dir

    reset_cache()
    assert resolve_booley_dir() == unrelated
    assert project_discovery._data_root(alias / "new", [owner], resolve) == root
    assert project_discovery._owning_checkouts([owner], root, resolve) == {owner}
    reset_cache()
    assert project_discovery._owning_checkouts([nested], root, resolve) == set()


@pytest.mark.parametrize("owned", [True, False])
def test_sandbox_requires_checkout_local_ownership(tmp_path, monkeypatch, owned):
    from booley.runtime import project_discovery as discovery

    outer = tmp_path / "checkout"
    data = tmp_path / "data"
    outer.mkdir()
    data.mkdir()
    subprocess.run(["git", "init", str(outer)], check=True, capture_output=True, timeout=30)
    if owned:
        (outer / "booley.toml").write_text(f'[project]\ndir = "{data.as_posix()}"\n')
    monkeypatch.setattr(discovery, "WORK_DIR", str(outer))
    monkeypatch.setattr(discovery, "BOOLEY_DIR_TARGET", str(data))
    monkeypatch.setenv("BOOLEY_CONTAINER", "1")
    monkeypatch.setenv("BOOLEY_DIR", str(data))
    monkeypatch.delenv("RTL_PROJECT_ROOT", raising=False)
    if owned:
        assert discovery.discover_project_root(data) == outer
    else:
        with pytest.raises(discovery.ProjectRootDiscoveryError):
            discovery.discover_project_root(data)


def test_session_owner_from_creator_path_with_external_data(tmp_path, monkeypatch):
    from tests.harness.test_session_runtime import _linked_project

    from booley.runtime.project_discovery import discover_session_context

    root, linked = _linked_project(tmp_path, monkeypatch, sandbox_pointer=True)
    external = tmp_path / "external-data"
    external.mkdir()
    (root / "booley.toml").write_text(f'[project]\ndir = "{external.as_posix()}"\n')
    (root / ".booley" / "FUSESOC_IGNORE").touch()
    context = discover_session_context(linked)
    assert context.project_root == root
    assert context.booley_dir == external
    assert context.checkout == linked
    assert context.cwd == "/work/.booley/worktrees/goal"


def test_session_owner_from_sandbox_created_absolute_pointers(tmp_path, monkeypatch):
    """Host Git cannot read `/work/...` pointers; the creator path alone proves ownership."""
    from tests.harness.test_session_runtime import _linked_project

    from booley.runtime.project_discovery import discover_session_context

    root, linked = _linked_project(tmp_path, monkeypatch, sandbox_pointer=True)
    admin = root / ".git" / "worktrees" / "goal"
    assert (admin / "gitdir").read_text().strip() == "/work/.booley/worktrees/goal/.git"
    assert (linked / ".git").read_text().strip() == "gitdir: /work/.git/worktrees/goal"
    context = discover_session_context(linked)
    assert (context.project_root, context.checkout) == (root, linked)
    assert context.cwd == "/work/.booley/worktrees/goal"


def test_session_owner_keeps_source_checkout_rejection(tmp_path, monkeypatch):
    from tests.harness.test_session_runtime import _linked_project

    from booley.core.checkout_role import SourceCheckoutProjectError
    from booley.runtime.project_discovery import discover_session_context

    root, linked = _linked_project(tmp_path, monkeypatch)
    (root / "pyproject.toml").write_text("[tool.booley]\nsource_checkout = true\n")
    with pytest.raises(SourceCheckoutProjectError):
        discover_session_context(linked)


@pytest.mark.parametrize("admin", ["separate", "relative"])
def test_session_owner_git_administration(tmp_path, monkeypatch, admin):
    from tests.harness.test_session_runtime import _linked_project, _replace_file

    from booley.runtime.project_discovery import (
        ProjectRootDiscoveryError,
        discover_session_context,
    )

    root, linked = _linked_project(tmp_path, monkeypatch, external=True)
    if admin == "separate":
        # A separate Git directory has no owning checkout to adopt.
        subprocess.run(
            ["git", "init", "--separate-git-dir", str(tmp_path / "administration"), str(root)],
            check=True,
            capture_output=True,
            timeout=30,
        )
        subprocess.run(
            ["git", "-C", str(root), "worktree", "repair", str(linked)],
            check=True,
            capture_output=True,
            timeout=30,
        )
        with pytest.raises(ProjectRootDiscoveryError, match="run from the Project Root"):
            discover_session_context(linked)
        return
    pointer = Path((linked / ".git").read_text().removeprefix("gitdir:").strip())
    (pointer / "commondir").write_text("../..\n")
    _replace_file(linked / ".git", "gitdir: " + os.path.relpath(pointer, linked) + "\n")
    context = discover_session_context(linked)
    assert context.project_root == root
    assert context.cwd is None


def test_external_data_attachment_records_use_owner_authority(tmp_path, monkeypatch):
    from types import SimpleNamespace

    from tests.harness.test_session_runtime import _linked_project

    from booley.runtime import runtime_attachment, session_runtime

    root, linked = _linked_project(tmp_path, monkeypatch)
    external = tmp_path / "external-data"
    external.mkdir()
    (root / "booley.toml").write_text(f'[project]\ndir = "{external.as_posix()}"\n')
    (linked / ".booley").mkdir()
    monkeypatch.setenv("BOOLEY_DIR", str(linked / ".booley"))
    monkeypatch.setattr(session_runtime, "_probe_command_cwd", lambda *_: None)
    captured = []

    def run(driver, execution_id, argv):
        captured.append(driver._project_data)
        return SimpleNamespace(exit_code=0)

    monkeypatch.setattr(runtime_attachment.SupervisedExecutionDriver, "run", run)
    runtime_attachment.run_command(
        root, "immutable-id", ["pwd"], tty=False, cwd="/work/.booley/worktrees/goal"
    )
    assert captured == [external]


def test_ambiguous_ownership_refuses(tmp_path, monkeypatch):
    from booley.runtime import project_discovery as discovery

    owner, other, data = (tmp_path / name for name in ("owner", "other", "data"))
    data.mkdir()
    for root in (owner, other):
        root.mkdir()
        (root / ".git").write_text("gitdir: /unused/metadata\n")
        (root / "booley.toml").write_text(f'[project]\ndir = "{data.as_posix()}"\n')
    (owner / "custom-data").symlink_to(data, target_is_directory=True)
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("RTL_PROJECT_ROOT", raising=False)
    monkeypatch.delenv("BOOLEY_DIR", raising=False)
    monkeypatch.setenv("BOOLEY_CONTAINER", "1")
    monkeypatch.setattr(discovery, "WORK_DIR", str(other))
    monkeypatch.setattr(discovery, "BOOLEY_DIR_TARGET", str(data))
    with pytest.raises(
        discovery.ProjectRootDiscoveryError,
        match="run from the Project Root or set RTL_PROJECT_ROOT",
    ):
        discovery.discover_project_root(owner / "custom-data")


def test_authoritative_directory_resolved_once_per_invocation(tmp_path, monkeypatch):
    from collections import Counter

    from booley.runtime import project_discovery as discovery

    owner, data, replacement = (tmp_path / name for name in ("owner", "data", "replacement"))
    for root in (owner, data, replacement):
        root.mkdir()
        (root / ".git").write_text("gitdir: /unused/metadata\n")
    config = owner / "booley.toml"
    config.write_text(f'[project]\ndir = "{data.as_posix()}"\n')
    alias = owner / "custom-data"
    alias.symlink_to(data, target_is_directory=True)
    for name in ("BOOLEY_DIR", "RTL_PROJECT_ROOT", "BOOLEY_CONTAINER"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.chdir(tmp_path)
    resolve = discovery.resolve_authoritative_booley_dir
    calls = Counter()

    def counted(root):
        calls[root.resolve()] += 1
        return resolve(root)

    monkeypatch.setattr(discovery, "resolve_authoritative_booley_dir", counted)
    assert discovery.discover_project_root(alias) == owner
    assert calls == {data: 1, owner: 1}, "authoritative resolution repeated within invocation"
    calls.clear()
    config.write_text(f'[project]\ndir = "{replacement.as_posix()}"\n')
    assert discovery.discover_project_root(alias) == data
    assert calls == {data: 1, owner: 1}


@pytest.mark.skipif(os.name == "nt", reason="directory symlink requires privilege")
def test_unpinned_linked_ancestor_does_not_block_nested_discovery(tmp_path, monkeypatch):
    from booley.runtime.project_discovery import discover_project_root

    monkeypatch.delenv("RTL_PROJECT_ROOT", raising=False)
    monkeypatch.delenv("BOOLEY_DIR", raising=False)
    monkeypatch.delenv("BOOLEY_CONTAINER", raising=False)
    outer = tmp_path / "outer"
    inner = outer / "inner"
    inner.mkdir(parents=True)
    external = tmp_path / "external"
    external.mkdir()
    (external / "booley.toml").write_text("[project]\n", encoding="utf-8")
    (outer / ".booley").symlink_to(external, target_is_directory=True)
    (inner / ".booley").mkdir()
    for root in (outer, inner):
        subprocess.run(["git", "init", str(root)], check=True, capture_output=True, timeout=30)

    assert discover_project_root(inner) == inner
