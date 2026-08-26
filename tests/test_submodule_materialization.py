"""Offline submodule materialization through the public runtime interface."""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from booley.runtime.submodule_materialization import (
    SubmoduleMaterializationError,
    materialize_submodules,
)


def _git(repo: Path, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", *args],
        cwd=repo,
        check=True,
        capture_output=True,
        text=True,
    )


def _init_repo(repo: Path) -> None:
    repo.mkdir()
    _git(repo, "init", "-q")
    _git(repo, "config", "user.email", "test@example.com")
    _git(repo, "config", "user.name", "Test")


def _commit_file(repo: Path, text: str, message: str) -> str:
    (repo / "source.sv").write_text(text, encoding="utf-8")
    _git(repo, "add", "source.sv")
    _git(repo, "commit", "-qm", message)
    return _git(repo, "rev-parse", "HEAD").stdout.strip()


def _add_submodule(parent: Path, dependency: Path, relative: str) -> None:
    _git(
        parent,
        "-c",
        "protocol.file.allow=always",
        "submodule",
        "add",
        str(dependency),
        relative,
    )


def _add_worktree(source: Path, destination: Path, ref: str | None = None) -> None:
    args = [
        "-c",
        "submodule.recurse=false",
        "worktree",
        "add",
        "--detach",
        str(destination),
    ]
    if ref is not None:
        args.append(ref)
    _git(source, *args)


def _set_submodule_url(parent: Path, name: str) -> None:
    _git(
        parent,
        "config",
        "--file",
        ".gitmodules",
        f"submodule.{name}.url",
        f"git@example.invalid:private/{name}.git",
    )


def _nested_source_repositories(tmp_path: Path) -> tuple[Path, str, str, str]:
    leaf = tmp_path / "leaf"
    _init_repo(leaf)
    old_leaf = _commit_file(leaf, "leaf-old\n", "leaf old")
    new_leaf = _commit_file(leaf, "leaf-new\n", "leaf new")
    middle = tmp_path / "middle"
    _init_repo(middle)
    _add_submodule(middle, leaf, "deps/leaf")
    _git(middle / "deps/leaf", "checkout", "--detach", old_leaf)
    _set_submodule_url(middle, "deps/leaf")
    _git(middle, "add", ".gitmodules", "deps/leaf")
    _git(middle, "commit", "-m", "middle old")
    old_middle = _git(middle, "rev-parse", "HEAD").stdout.strip()
    _git(middle / "deps/leaf", "checkout", "--detach", new_leaf)
    _git(middle, "add", "deps/leaf")
    _git(middle, "commit", "-m", "middle new")
    new_middle = _git(middle, "rev-parse", "HEAD").stdout.strip()
    source = tmp_path / "source"
    _init_repo(source)
    _add_submodule(source, middle, "vendor/middle")
    _set_submodule_url(source, "vendor/middle")
    nested_source = source / "vendor/middle"
    _git(nested_source, "config", "submodule.deps/leaf.url", str(leaf))
    _git(nested_source, "-c", "protocol.file.allow=always", "submodule", "update", "--init")
    _git(nested_source, "checkout", "--detach", old_middle)
    _git(nested_source / "deps/leaf", "checkout", "--detach", old_leaf)
    _git(source, "add", ".gitmodules", "vendor/middle")
    _git(source, "commit", "-m", "source old")
    baseline = _git(source, "rev-parse", "HEAD").stdout.strip()
    _git(nested_source, "checkout", "--detach", new_middle)
    nested_leaf = nested_source / "deps/leaf"
    _git(nested_leaf, "checkout", "--detach", new_leaf)
    (nested_leaf / "source.sv").write_bytes(b"leaf-new\r\n")
    _git(source, "add", "vendor/middle")
    _git(source, "commit", "-m", "source new")
    return source, baseline, old_middle, old_leaf


def test_materializes_historical_pin_without_using_ssh(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    dependency = tmp_path / "dependency"
    _init_repo(dependency)
    old_sha = _commit_file(dependency, "old\n", "old")
    new_sha = _commit_file(dependency, "new\n", "new")

    source = tmp_path / "source"
    _init_repo(source)
    monkeypatch.setenv("GIT_ALLOW_PROTOCOL", "file")
    _git(source, "submodule", "add", str(dependency), "vendor/private")
    _git(source / "vendor/private", "checkout", "-q", old_sha)
    _git(
        source,
        "config",
        "--file",
        ".gitmodules",
        "submodule.vendor/private.url",
        "ssh://git@example.invalid/private.git",
    )
    _git(source, "add", ".gitmodules", "vendor/private")
    _git(source, "commit", "-qm", "old pin")
    baseline_sha = _git(source, "rev-parse", "HEAD").stdout.strip()

    _git(source / "vendor/private", "checkout", "-q", new_sha)
    _git(source, "add", "vendor/private")
    _git(source, "commit", "-qm", "new pin")

    destination = tmp_path / "destination"
    _add_worktree(source, destination, baseline_sha)
    monkeypatch.setenv("GIT_SSH", "/definitely/no/ssh")

    materialize_submodules(source, destination)

    assert (destination / "vendor/private/source.sv").read_text(encoding="utf-8") == "old\n"
    materialized = destination / "vendor/private"
    assert _git(materialized, "rev-parse", "HEAD").stdout.strip() == old_sha
    assert (materialized / ".git").is_dir()
    assert _git(materialized, "remote").stdout == ""
    assert not (materialized / ".git/objects/info/alternates").exists()


def test_explicit_empty_configuration_materializes_nothing(tmp_path: Path) -> None:
    source = tmp_path / "source"
    _init_repo(source)
    (source / "tracked.txt").write_text("tracked\n", encoding="utf-8")
    _git(source, "add", "tracked.txt")
    _git(source, "commit", "-qm", "root")
    root_sha = _git(source, "rev-parse", "HEAD").stdout.strip()
    (source / ".gitmodules").write_text(
        '[submodule "missing"]\n\tpath = vendor/missing\n\turl = ssh://example.invalid/missing\n',
        encoding="utf-8",
    )
    _git(source, "add", ".gitmodules")
    _git(source, "update-index", "--add", "--cacheinfo", f"160000,{root_sha},vendor/missing")
    project_dir = source / ".booley_project"
    project_dir.mkdir()
    (project_dir / "booley.toml").write_text("[submodules]\npaths = []\n", encoding="utf-8")
    _git(source, "commit", "-qm", "configured submodule")

    destination = tmp_path / "destination"
    _add_worktree(source, destination, "HEAD")

    materialize_submodules(source, destination)

    placeholder = destination / "vendor/missing"
    assert placeholder.is_dir()
    assert not any(placeholder.iterdir())


def test_rejects_unsafe_configured_path_before_touching_destination(tmp_path: Path) -> None:
    source = tmp_path / "source"
    _init_repo(source)
    _commit_file(source, "root\n", "root")
    project_dir = source / ".booley_project"
    project_dir.mkdir()
    (project_dir / "booley.toml").write_text(
        '[submodules]\npaths = ["../../../victim"]\n', encoding="utf-8"
    )
    destination = tmp_path / "destination"
    _git(source, "worktree", "add", str(destination))
    sentinel = tmp_path / "victim"
    sentinel.write_text("keep\n", encoding="utf-8")

    with pytest.raises(SubmoduleMaterializationError, match="unsafe submodule path"):
        materialize_submodules(source, destination)

    assert sentinel.read_text(encoding="utf-8") == "keep\n"


@pytest.mark.parametrize(
    "configuration",
    ['submodules = "invalid"\n', '[submodules]\npaths = "vendor/ip"\n'],
)
def test_rejects_invalid_submodule_configuration(tmp_path: Path, configuration: str) -> None:
    source = tmp_path / "source"
    _init_repo(source)
    _commit_file(source, "root\n", "root")
    project_dir = source / ".booley_project"
    project_dir.mkdir()
    (project_dir / "booley.toml").write_text(configuration, encoding="utf-8")
    destination = tmp_path / "destination"
    _add_worktree(source, destination)

    with pytest.raises(SubmoduleMaterializationError, match=r"\[submodules\]"):
        materialize_submodules(source, destination)


def test_configuration_selects_only_matching_top_level_gitlinks(tmp_path: Path) -> None:
    first = tmp_path / "first"
    second = tmp_path / "second"
    _init_repo(first)
    _init_repo(second)
    _commit_file(first, "first\n", "first")
    _commit_file(second, "second\n", "second")
    source = tmp_path / "source"
    _init_repo(source)
    _add_submodule(source, first, "vendor/first")
    _add_submodule(source, second, "vendor/second")
    _git(source, "commit", "-am", "two submodules")
    project_dir = source / ".booley_project"
    project_dir.mkdir()
    (project_dir / "booley.toml").write_text(
        '[submodules]\npaths = ["vendor/second", "not/a/gitlink"]\n', encoding="utf-8"
    )
    destination = tmp_path / "destination"
    _add_worktree(source, destination)

    materialize_submodules(source, destination)

    assert not any((destination / "vendor/first").iterdir())
    assert (destination / "vendor/second/source.sv").read_text(encoding="utf-8") == "second\n"


def test_materializes_nested_historical_gitlinks_by_same_path(tmp_path: Path) -> None:
    source, baseline, old_middle, old_leaf = _nested_source_repositories(tmp_path)
    destination = tmp_path / "destination"
    _add_worktree(source, destination, baseline)

    materialize_submodules(source, destination)

    actual_middle = _git(destination / "vendor/middle", "rev-parse", "HEAD").stdout.strip()
    actual_leaf = _git(destination / "vendor/middle/deps/leaf", "rev-parse", "HEAD").stdout.strip()
    assert actual_middle == old_middle
    assert actual_leaf == old_leaf
    assert (destination / "vendor/middle/deps/leaf/source.sv").read_text(
        encoding="utf-8"
    ) == "leaf-old\n"


def test_failure_rolls_back_only_directories_created_by_this_call(tmp_path: Path) -> None:
    dependency = tmp_path / "dependency"
    _init_repo(dependency)
    commit = _commit_file(dependency, "available\n", "available")

    source = tmp_path / "source"
    _init_repo(source)
    (source / ".gitmodules").write_text(
        '[submodule "available"]\n\tpath = a/available\n\turl = ssh://private/available\n'
        '[submodule "missing"]\n\tpath = z/missing\n\turl = ssh://private/missing\n',
        encoding="utf-8",
    )
    (source / "a").mkdir()
    _git(source / "a", "clone", str(dependency), "available")
    _git(source, "add", ".gitmodules", "a/available")
    _git(source, "update-index", "--add", "--cacheinfo", f"160000,{commit},z/missing")
    _git(source, "commit", "-m", "two submodules")
    destination = tmp_path / "destination"
    _add_worktree(source, destination)

    with pytest.raises(SubmoduleMaterializationError, match=r"z/missing.*initialize"):
        materialize_submodules(source, destination)

    for relative in (Path("a/available"), Path("z/missing")):
        placeholder = destination / relative
        assert placeholder.is_dir()
        assert not any(placeholder.iterdir())


def test_rejects_broken_destination_symlink_without_following_it(tmp_path: Path) -> None:
    dependency = tmp_path / "dependency"
    _init_repo(dependency)
    _commit_file(dependency, "safe\n", "safe")
    source = tmp_path / "source"
    _init_repo(source)
    _add_submodule(source, dependency, "vendor/ip")
    _git(source, "commit", "-am", "add submodule")
    destination = tmp_path / "destination"
    _add_worktree(source, destination)
    placeholder = destination / "vendor/ip"
    placeholder.rmdir()
    placeholder.symlink_to(tmp_path / "missing-target", target_is_directory=True)

    with pytest.raises(SubmoduleMaterializationError, match="destination is a link"):
        materialize_submodules(source, destination)

    assert placeholder.is_symlink()


def test_rejects_dirty_source_without_mutating_destination(tmp_path: Path) -> None:
    dependency = tmp_path / "dependency"
    _init_repo(dependency)
    _commit_file(dependency, "clean\n", "clean")
    source = tmp_path / "source"
    _init_repo(source)
    _add_submodule(source, dependency, "vendor/ip")
    _git(source, "commit", "-am", "add submodule")
    (source / "vendor/ip/source.sv").write_text("dirty\n", encoding="utf-8")
    destination = tmp_path / "destination"
    _add_worktree(source, destination)

    with pytest.raises(SubmoduleMaterializationError, match=r"vendor/ip.*dirty"):
        materialize_submodules(source, destination)

    placeholder = destination / "vendor/ip"
    assert placeholder.is_dir()
    assert not any(placeholder.iterdir())


def test_rejects_source_path_that_crosses_a_symlink(tmp_path: Path) -> None:
    dependency = tmp_path / "dependency"
    _init_repo(dependency)
    commit = _commit_file(dependency, "outside\n", "outside")
    source = tmp_path / "source"
    _init_repo(source)
    (source / ".gitmodules").write_text(
        '[submodule "ip"]\n\tpath = vendor/ip\n\turl = ssh://private/ip\n',
        encoding="utf-8",
    )
    outside = tmp_path / "outside"
    outside.mkdir()
    _git(outside, "clone", str(dependency), "ip")
    (source / "vendor").symlink_to(outside, target_is_directory=True)
    _git(source, "add", ".gitmodules")
    _git(source, "update-index", "--add", "--cacheinfo", f"160000,{commit},vendor/ip")
    _git(source, "commit", "-m", "linked source")
    destination = tmp_path / "destination"
    _add_worktree(source, destination)

    with pytest.raises(SubmoduleMaterializationError, match="source crosses a link"):
        materialize_submodules(source, destination)

    assert not any((destination / "vendor/ip").iterdir())


def test_rejects_shallow_source_repository(tmp_path: Path) -> None:
    dependency = tmp_path / "dependency"
    _init_repo(dependency)
    _commit_file(dependency, "old\n", "old")
    commit = _commit_file(dependency, "new\n", "new")
    source = tmp_path / "source"
    _init_repo(source)
    (source / ".gitmodules").write_text(
        '[submodule "ip"]\n\tpath = vendor/ip\n\turl = ssh://private/ip\n',
        encoding="utf-8",
    )
    (source / "vendor").mkdir()
    _git(
        source / "vendor",
        "clone",
        "--depth=1",
        dependency.as_uri(),
        "ip",
    )
    _git(source, "add", ".gitmodules", "vendor/ip")
    _git(source, "commit", "-m", "shallow submodule")
    destination = tmp_path / "destination"
    _add_worktree(source, destination)

    assert _git(source / "vendor/ip", "rev-parse", "HEAD").stdout.strip() == commit
    with pytest.raises(SubmoduleMaterializationError, match="is shallow"):
        materialize_submodules(source, destination)


def test_reports_incomplete_local_objects_without_trying_a_remote(tmp_path: Path) -> None:
    historical = tmp_path / "historical"
    replacement = tmp_path / "replacement"
    _init_repo(historical)
    _init_repo(replacement)
    old_commit = _commit_file(historical, "historical\n", "historical")
    _commit_file(replacement, "replacement\n", "replacement")
    source = tmp_path / "source"
    _init_repo(source)
    (source / ".gitmodules").write_text(
        '[submodule "ip"]\n\tpath = vendor/ip\n\turl = ssh://private/ip\n',
        encoding="utf-8",
    )
    (source / "vendor").mkdir()
    _git(source / "vendor", "clone", str(historical), "ip")
    _git(source, "add", ".gitmodules", "vendor/ip")
    _git(source, "commit", "-m", "historical pin")
    baseline = _git(source, "rev-parse", "HEAD").stdout.strip()
    _git(source, "rm", "-f", "vendor/ip")
    _git(source / "vendor", "clone", str(replacement), "ip")
    _git(source, "add", "vendor/ip")
    _git(source, "commit", "-m", "unrelated replacement")
    destination = tmp_path / "destination"
    _add_worktree(source, destination, baseline)

    with pytest.raises(SubmoduleMaterializationError, match=r"local objects.*incomplete"):
        materialize_submodules(source, destination)

    assert old_commit not in _git(source / "vendor/ip", "rev-list", "--all").stdout


def test_materialization_is_idempotent_for_matching_clean_checkout(tmp_path: Path) -> None:
    dependency = tmp_path / "dependency"
    _init_repo(dependency)
    commit = _commit_file(dependency, "stable\n", "stable")
    source = tmp_path / "source"
    _init_repo(source)
    _add_submodule(source, dependency, "ip core")
    _git(source, "commit", "-am", "add submodule")
    destination = tmp_path / "destination"
    _add_worktree(source, destination)

    materialize_submodules(source, destination)
    materialize_submodules(source, destination)

    assert _git(destination / "ip core", "rev-parse", "HEAD").stdout.strip() == commit


def test_resume_revalidates_source_repository(tmp_path: Path) -> None:
    dependency = tmp_path / "dependency"
    _init_repo(dependency)
    _commit_file(dependency, "stable\n", "stable")
    source = tmp_path / "source"
    _init_repo(source)
    _add_submodule(source, dependency, "vendor/ip")
    _git(source, "commit", "-am", "add submodule")
    destination = tmp_path / "destination"
    _add_worktree(source, destination)
    materialize_submodules(source, destination)
    (source / "vendor/ip/source.sv").write_text("dirty\n", encoding="utf-8")

    with pytest.raises(SubmoduleMaterializationError, match=r"vendor/ip.*dirty"):
        materialize_submodules(source, destination)


def test_resume_rejects_destination_with_shared_git_pointer(tmp_path: Path) -> None:
    dependency = tmp_path / "dependency"
    _init_repo(dependency)
    _commit_file(dependency, "stable\n", "stable")
    source = tmp_path / "source"
    _init_repo(source)
    _add_submodule(source, dependency, "vendor/ip")
    _git(source, "commit", "-am", "add submodule")
    destination = tmp_path / "destination"
    _add_worktree(source, destination)
    materialize_submodules(source, destination)
    materialized = destination / "vendor/ip"
    shared_git = tmp_path / "shared-git"
    (materialized / ".git").rename(shared_git)
    (materialized / ".git").write_text(f"gitdir: {shared_git}\n", encoding="utf-8")

    with pytest.raises(SubmoduleMaterializationError, match="standalone"):
        materialize_submodules(source, destination)


def test_accepts_source_with_only_crlf_checkout_smudge(tmp_path: Path) -> None:
    dependency = tmp_path / "dependency"
    _init_repo(dependency)
    _commit_file(dependency, "first\nsecond\n", "stable")
    source = tmp_path / "source"
    _init_repo(source)
    _add_submodule(source, dependency, "vendor/ip")
    _git(source, "commit", "-am", "add submodule")
    (source / "vendor/ip/source.sv").write_bytes(b"first\r\nsecond\r\n")
    destination = tmp_path / "destination"
    _add_worktree(source, destination)

    materialize_submodules(source, destination)

    assert (destination / "vendor/ip/source.sv").read_bytes() == b"first\nsecond\n"


def test_discovers_index_gitlinks_instead_of_stale_gitmodules(tmp_path: Path) -> None:
    dependency = tmp_path / "dependency"
    _init_repo(dependency)
    commit = _commit_file(dependency, "indexed\n", "indexed")
    source = tmp_path / "source"
    _init_repo(source)
    (source / "vendor").mkdir()
    _git(source / "vendor", "clone", str(dependency), "indexed")
    (source / ".gitmodules").write_text(
        '[submodule "stale"]\n\tpath = vendor/stale\n\turl = ssh://private/stale\n',
        encoding="utf-8",
    )
    _git(source, "add", ".gitmodules")
    _git(source, "update-index", "--add", "--cacheinfo", f"160000,{commit},vendor/indexed")
    _git(source, "commit", "-m", "index is authoritative")
    destination = tmp_path / "destination"
    _add_worktree(source, destination)

    materialize_submodules(source, destination)

    assert (destination / "vendor/indexed/source.sv").read_text() == "indexed\n"
