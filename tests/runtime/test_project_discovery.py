"""Project discovery distinguishes the checkout from its data repositories."""

import subprocess

import pytest

from booley.harness.booley import find_project_root


def test_nested_data_repository_selects_checkout(tmp_path, monkeypatch):
    outer = tmp_path / "checkout"
    data = outer / ".booley_project"
    data.mkdir(parents=True)
    for root in (outer, data):
        subprocess.run(["git", "init", str(root)], check=True, capture_output=True, timeout=30)
    monkeypatch.delenv("RTL_PROJECT_ROOT", raising=False)
    monkeypatch.delenv("BOOLEY_PROJECT_DIR", raising=False)
    monkeypatch.chdir(data)
    assert find_project_root() == outer


@pytest.mark.parametrize("name", [".booley_project", ".booley/project", "custom-data"])
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
    monkeypatch.delenv("BOOLEY_PROJECT_DIR", raising=False)
    assert discover_project_root(child) == outer


def test_unrelated_nested_checkout_keeps_identity(tmp_path, monkeypatch):
    from booley.runtime.project_discovery import discover_project_root

    nested = tmp_path / "nested"
    nested.mkdir()
    for root in (tmp_path, nested):
        subprocess.run(["git", "init", str(root)], check=True, capture_output=True, timeout=30)
    monkeypatch.delenv("RTL_PROJECT_ROOT", raising=False)
    monkeypatch.delenv("BOOLEY_PROJECT_DIR", raising=False)
    assert discover_project_root(nested) == nested


@pytest.mark.parametrize("explicit", [False, True])
def test_ticket_design_worktree_inside_data_keeps_checkout(tmp_path, monkeypatch, explicit):
    from booley.runtime.project_discovery import discover_project_root

    outer = tmp_path / "checkout"
    data = outer / ".booley_project"
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
    snapshot = worktree / ".booley_project"
    snapshot.mkdir()
    subprocess.run(["git", "init", str(snapshot)], check=True, capture_output=True, timeout=30)
    monkeypatch.setenv("BOOLEY_PROJECT_DIR", str(data))
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
    monkeypatch.setenv("BOOLEY_PROJECT_DIR", str(data))
    monkeypatch.delenv("RTL_PROJECT_ROOT", raising=False)
    with pytest.raises(ProjectRootDiscoveryError, match="run from the Project checkout"):
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
    monkeypatch.delenv("BOOLEY_PROJECT_DIR", raising=False)
    fallback = tmp_path / "fallback"
    assert resolve_project_root(fallback) == fallback
    monkeypatch.setenv("RTL_PROJECT_ROOT", str(tmp_path / "explicit"))
    assert resolve_project_root(fallback) == tmp_path / "explicit"


def test_linked_git_marker_and_symlink_data(tmp_path, monkeypatch):
    from booley.runtime.project_discovery import discover_project_root

    outer = tmp_path / "checkout"
    data = outer / ".booley_project"
    data.mkdir(parents=True)
    (outer / ".git").write_text("gitdir: /unused/metadata\n")
    alias = tmp_path / "alias"
    alias.symlink_to(data, target_is_directory=True)
    monkeypatch.delenv("RTL_PROJECT_ROOT", raising=False)
    monkeypatch.delenv("BOOLEY_PROJECT_DIR", raising=False)
    assert discover_project_root(alias) == outer


def test_source_stale_data_refuses_goal_but_feedback_routes(tmp_path, monkeypatch):
    from booley.harness.booley import _command_project_root
    from booley.runtime.project_discovery import ProjectRootDiscoveryError

    data = tmp_path / ".booley_project"
    data.mkdir()
    (tmp_path / "pyproject.toml").write_text("[tool.booley]\nsource_checkout = true\n")
    subprocess.run(["git", "init", str(tmp_path)], check=True, capture_output=True, timeout=30)
    monkeypatch.chdir(data)
    monkeypatch.delenv("RTL_PROJECT_ROOT", raising=False)
    monkeypatch.delenv("BOOLEY_PROJECT_DIR", raising=False)
    with pytest.raises(ProjectRootDiscoveryError):
        _command_project_root("goal")
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
    (outer / ".booley_project").symlink_to(data, target_is_directory=True)
    for root in (outer, data):
        subprocess.run(["git", "init", str(root)], check=True, capture_output=True, timeout=30)
    monkeypatch.delenv("RTL_PROJECT_ROOT", raising=False)
    monkeypatch.delenv("BOOLEY_PROJECT_DIR", raising=False)
    monkeypatch.chdir(data)
    monkeypatch.setenv("PWD", str(outer / ".booley_project"))
    assert discover_project_root(outer / ".booley_project" if use_start else None) == outer


@pytest.mark.parametrize("config", ['project = "invalid"', "[project]\ndir = 12"])
def test_malformed_project_selection_does_not_crash_discovery(tmp_path, monkeypatch, config):
    from booley.runtime.project_discovery import discover_project_root

    subprocess.run(["git", "init", str(tmp_path)], check=True, capture_output=True, timeout=30)
    (tmp_path / "booley.toml").write_text(config)
    monkeypatch.delenv("RTL_PROJECT_ROOT", raising=False)
    monkeypatch.delenv("BOOLEY_PROJECT_DIR", raising=False)
    assert discover_project_root(tmp_path) == tmp_path


def test_explicit_external_data_symlink_is_refused(tmp_path, monkeypatch):
    from booley.runtime.project_discovery import ProjectRootDiscoveryError, discover_project_root

    outer = tmp_path / "checkout"
    data = tmp_path / "external-data"
    outer.mkdir()
    data.mkdir()
    alias = outer / ".booley_project"
    alias.symlink_to(data, target_is_directory=True)
    for root in (outer, data):
        subprocess.run(["git", "init", str(root)], check=True, capture_output=True, timeout=30)
    monkeypatch.delenv("BOOLEY_PROJECT_DIR", raising=False)
    monkeypatch.setenv("RTL_PROJECT_ROOT", str(alias))
    with pytest.raises(ProjectRootDiscoveryError, match="run from the Project checkout"):
        discover_project_root(outer)


def test_containment_alias_explicit_authority(tmp_path, monkeypatch):
    from pathlib import Path

    from booley.runtime import project_discovery

    root, alias, unrelated = (tmp_path / name for name in ("selected", "alias", "unrelated"))
    for directory in (root, alias, unrelated):
        directory.mkdir()
    original = Path.samefile
    calls = []

    def samefile(path, other):
        if {path, Path(other)} == {root, alias}:
            calls.append(path)
            return True
        return original(path, other)

    monkeypatch.setattr(Path, "samefile", samefile)
    monkeypatch.setenv("BOOLEY_PROJECT_DIR", str(root))
    assert project_discovery._data_root(alias / "new", []) == root
    assert calls
    nested = alias / "embedded"
    nested.mkdir()
    assert project_discovery._data_root(nested, [nested]) is None
    (nested / ".git").mkdir()
    (nested / ".git" / "HEAD").write_text("ref: refs/heads/main")
    (nested / "booley.toml").write_text(f'[project]\ndir = "{root.as_posix()}"\n')
    owner = tmp_path / "owner"
    owner.mkdir()
    (owner / ".git").mkdir()
    (owner / ".git" / "HEAD").write_text("ref: refs/heads/main")
    (owner / "booley.toml").write_text(f'[project]\ndir = "{root.as_posix()}"\n')
    monkeypatch.setenv("BOOLEY_PROJECT_DIR", str(unrelated))
    from booley.runtime.project_dir import reset_cache, resolve_project_dir

    reset_cache()
    assert resolve_project_dir() == unrelated
    assert project_discovery._data_root(alias / "new", [owner]) == root
    assert project_discovery._owning_checkouts([owner], root) == {owner}
    reset_cache()
    assert project_discovery._owning_checkouts([nested], root) == set()


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
    monkeypatch.setattr(discovery, "PROJECT_DIR_TARGET", str(data))
    monkeypatch.setenv("BOOLEY_CONTAINER", "1")
    monkeypatch.setenv("BOOLEY_PROJECT_DIR", str(data))
    monkeypatch.delenv("RTL_PROJECT_ROOT", raising=False)
    if owned:
        assert discovery.discover_project_root(data) == outer
    else:
        with pytest.raises(discovery.ProjectRootDiscoveryError):
            discovery.discover_project_root(data)
