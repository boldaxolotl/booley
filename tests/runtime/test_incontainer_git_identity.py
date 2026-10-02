"""Interactive Mode Git identity setup."""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest

from booley.runtime import incontainer_git_identity as identity_setup
from booley.runtime.incontainer_git_identity import (
    GitIdentity,
    GitIdentityError,
    apply_git_identity,
    load_git_identity,
)


def _git(path: Path, *args: str, env: dict[str, str] | None = None) -> str:
    result = subprocess.run(
        ["git", "-C", str(path), *args],
        capture_output=True,
        check=True,
        env=env,
        text=True,
        timeout=10,
    )
    return result.stdout.strip()


def test_explicit_ticket_identity_overrides_copied_global_config(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    project_dir = tmp_path / "project-data"
    workspace.mkdir()
    project_dir.mkdir()
    global_config = tmp_path / "global.gitconfig"
    subprocess.run(
        ["git", "config", "--file", str(global_config), "user.name", "Host Alias"],
        check=True,
        timeout=10,
    )
    subprocess.run(
        [
            "git",
            "config",
            "--file",
            str(global_config),
            "user.email",
            "host-identity.invalid",
        ],
        check=True,
        timeout=10,
    )
    (project_dir / "booley.toml").write_text(
        '[agent.git]\nname = "Expected Developer"\nemail = "developer-identity.invalid"\n',
        encoding="utf-8",
    )
    _git(workspace, "init", "-q")
    env = os.environ.copy()
    env["BOOLEY_PROJECT_DIR"] = str(project_dir)
    env["GIT_CONFIG_GLOBAL"] = str(global_config)
    source_root = str(Path(identity_setup.__file__).resolve().parents[2])
    env["PYTHONPATH"] = os.pathsep.join(
        part for part in (source_root, env.get("PYTHONPATH")) if part
    )
    for key in (
        "GIT_AUTHOR_NAME",
        "GIT_AUTHOR_EMAIL",
        "GIT_COMMITTER_NAME",
        "GIT_COMMITTER_EMAIL",
    ):
        env.pop(key, None)

    assert _git(workspace, "config", "user.name", env=env) == "Host Alias"

    subprocess.run(
        [sys.executable, "-m", "booley.runtime.incontainer_git_identity", "--ticket"],
        cwd=workspace,
        env=env,
        check=True,
        timeout=10,
    )
    (workspace / "change.txt").write_text("change\n", encoding="utf-8")
    _git(workspace, "add", "change.txt", env=env)
    _git(workspace, "-c", "commit.gpgSign=false", "commit", "-q", "-m", "change", env=env)

    assert _git(workspace, "show", "-s", "--format=%an <%ae>", "HEAD", env=env) == (
        "Expected Developer <developer-identity.invalid>"
    )
    assert _git(workspace, "show", "-s", "--format=%cn <%ce>", "HEAD", env=env) == (
        "Expected Developer <developer-identity.invalid>"
    )


def test_identity_rejects_non_string_config_before_git_runs(tmp_path: Path) -> None:
    (tmp_path / "booley.toml").write_text(
        '[agent.git]\nname = 42\nemail = "developer-identity.invalid"\n',
        encoding="utf-8",
    )

    with pytest.raises(GitIdentityError, match=r"\[agent\.git\] name must be a string"):
        load_git_identity(tmp_path)


@pytest.mark.parametrize(
    ("contents", "expected"),
    [
        (None, GitIdentity("Dev", "dev@localhost")),
        ("[agent]\nprovider = 'codex'\n", GitIdentity("Dev", "dev@localhost")),
        (
            "[agent.git]\nname = 'Expected Developer'\n",
            GitIdentity("Expected Developer", "dev@localhost"),
        ),
        (
            "[agent.git]\nname = '  '\nemail = 'developer-identity.invalid'\n",
            GitIdentity("Dev", "developer-identity.invalid"),
        ),
    ],
)
def test_identity_defaults_are_resolved_per_field(
    tmp_path: Path, contents: str | None, expected: GitIdentity
) -> None:
    if contents is not None:
        (tmp_path / "booley.toml").write_text(contents, encoding="utf-8")

    assert load_git_identity(tmp_path) == expected


def test_identity_ignores_pipeline_toml(tmp_path: Path) -> None:
    (tmp_path / "pipeline.toml").write_text(
        "[agent.git]\nname = 'Legacy Alias'\nemail = 'legacy-identity.invalid'\n",
        encoding="utf-8",
    )

    assert load_git_identity(tmp_path) == GitIdentity("Dev", "dev@localhost")


@pytest.mark.parametrize(
    ("contents", "match"),
    [
        ("not valid toml {{{", "cannot read Git identity"),
        ("agent = 'codex'\n", r"\[agent\] must be a table"),
        ("[agent]\ngit = 'identity'\n", r"\[agent\.git\] must be a table"),
        (
            '[[agent.git]]\nname = "Expected Developer"\nemail = "developer.invalid"\n',
            r"\[agent\.git\] must be a table",
        ),
        (
            '[agent.git]\nname = """Expected\nDeveloper"""\n',
            "forbidden control character",
        ),
    ],
)
def test_identity_rejects_invalid_configuration(tmp_path: Path, contents: str, match: str) -> None:
    (tmp_path / "booley.toml").write_text(contents, encoding="utf-8")

    with pytest.raises(GitIdentityError, match=match):
        load_git_identity(tmp_path)


def test_identity_rerun_updates_existing_worktree_values(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    _git(workspace, "init", "-q")

    apply_git_identity(workspace, GitIdentity("First Developer", "first-identity.invalid"))
    apply_git_identity(workspace, GitIdentity("Second Developer", "second-identity.invalid"))

    assert _git(workspace, "config", "--worktree", "user.name") == "Second Developer"
    assert _git(workspace, "config", "--worktree", "user.email") == ("second-identity.invalid")


def test_failed_pair_write_restores_prior_worktree_identity(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    _git(workspace, "init", "-q")
    _git(workspace, "config", "extensions.worktreeConfig", "true")
    _git(workspace, "config", "--worktree", "user.name", "Prior Developer")
    _git(workspace, "config", "--worktree", "user.email", "prior-identity.invalid")
    real_run = subprocess.run

    def fail_new_email(*args, **kwargs):
        command = args[0]
        if command[-2:] == ["user.email", "developer-identity.invalid"]:
            raise subprocess.CalledProcessError(1, command, stderr="forced email failure")
        return real_run(*args, **kwargs)

    monkeypatch.setattr(identity_setup.subprocess, "run", fail_new_email)

    with pytest.raises(GitIdentityError, match="forced email failure"):
        apply_git_identity(
            workspace,
            GitIdentity("Expected Developer", "developer-identity.invalid"),
        )

    assert _git(workspace, "config", "--worktree", "user.name") == "Prior Developer"
    assert _git(workspace, "config", "--worktree", "user.email") == "prior-identity.invalid"


def test_failed_publication_does_not_enable_worktree_config(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    _git(workspace, "init", "-q")
    (workspace / ".git" / "config.worktree").mkdir()

    with pytest.raises(GitIdentityError):
        apply_git_identity(
            workspace,
            GitIdentity("Expected Developer", "developer-identity.invalid"),
        )

    result = subprocess.run(
        [
            "git",
            "config",
            "--file",
            str(workspace / ".git" / "config"),
            "--get",
            "extensions.worktreeConfig",
        ],
        capture_output=True,
        check=False,
        text=True,
        timeout=10,
    )
    assert result.returncode == 1


def test_sandbox_identity_preserves_host_and_waiver_guard(tmp_path, monkeypatch):
    from booley.runtime.devcontainer import build_devcontainer_spec, git_identity_command
    from booley.ticket_board.waiver_approval import WaiverDecisionError, approver_identity

    workspace = tmp_path / "workspace"
    workspace.mkdir()
    project = workspace / ".booley_project"
    project.mkdir()
    (project / "booley.toml").write_text('[agent.git]\nname="Dev"\nemail="dev@localhost"\n')
    _git(workspace, "init", "-q")
    _git(workspace, "config", "user.name", "Human")
    _git(workspace, "config", "user.email", "human@example.invalid")
    env = {k: v for k, v in os.environ.items() if not k.startswith("GIT_")}
    env.update(
        BOOLEY_PROJECT_DIR=str(project),
        PYTHONPATH=str(Path(identity_setup.__file__).resolve().parents[2]),
    )
    spec = build_devcontainer_spec(app="codex", image="test", project_dir_source=str(project))
    sandbox = {**env, **spec["containerEnv"]}
    subprocess.run(
        [sys.executable, *git_identity_command().split()[1:]],
        cwd=workspace,
        env=sandbox,
        check=True,
        timeout=10,
    )
    assert _git(workspace, "config", "user.name", env=env) == "Human"
    for commit_env, expected in (
        (env, "Human <human@example.invalid>"),
        (sandbox, "Dev <dev@localhost>"),
    ):
        _git(
            workspace,
            "-c",
            "commit.gpgSign=false",
            "commit",
            "--allow-empty",
            "-qm",
            "identity",
            env=commit_env,
        )
        assert (
            _git(workspace, "show", "-s", "--format=%an <%ae>|%cn <%ce>", "HEAD", env=commit_env)
            == f"{expected}|{expected}"
        )
    assert approver_identity(workspace) == "Human <human@example.invalid>"
    for key, value in sandbox.items():
        monkeypatch.setenv(key, value)
    with pytest.raises(WaiverDecisionError, match="approve waivers as yourself"):
        approver_identity(workspace)


@pytest.mark.parametrize(
    "pair", [("Dev", "dev@localhost"), ("Custom Agent", "custom@example.invalid")]
)
def test_cleanup_recognized_pair_preserves_other_config(tmp_path, pair):
    from booley.runtime.incontainer_git_identity_cleanup import cleanup_git_identity

    _git(tmp_path, "init", "-q")
    _git(tmp_path, "config", "user.name", "Human")
    _git(tmp_path, "config", "user.email", "human@example.invalid")
    apply_git_identity(tmp_path, GitIdentity(*pair))
    target = tmp_path / ".git/config.worktree"
    with target.open("a") as stream:
        stream.write(
            "[user]\n signingkey = abc\n[core]\n hooksPath = ../hooks\n autocrlf = false\n[include]\n path = absent-config\n"
        )
    for _ in range(2):
        cleanup_git_identity(tmp_path, GitIdentity("Custom Agent", "custom@example.invalid"))
    assert _git(tmp_path, "config", "user.name") == "Human"
    assert _git(tmp_path, "config", "user.email") == "human@example.invalid"
    assert _git(tmp_path, "config", "user.signingkey") == "abc"
    assert _git(tmp_path, "config", "core.hooksPath") == "../hooks"
    assert _git(tmp_path, "config", "extensions.worktreeConfig") == "true"
    assert "path = absent-config" in target.read_text()


@pytest.mark.parametrize(
    "contents",
    [
        "[user]\nname=Dev\n",
        "[user]\nname=Human\nemail=dev@localhost\n",
        "[user]\nname=Dev\nemail=human@example.invalid\n",
        "[user]\nname=Dev\nname=Dev\nemail=dev@localhost\n",
        "[user]\nname=Historic Custom\nemail=historic@example.invalid\n",
    ],
)
def test_cleanup_preserves_unrecognized_or_incomplete_pairs(tmp_path, contents):
    from booley.runtime.incontainer_git_identity_cleanup import cleanup_git_identity

    _git(tmp_path, "init", "-q")
    target = tmp_path / ".git/config.worktree"
    target.write_text(contents)
    cleanup_git_identity(tmp_path, GitIdentity("Current", "current@example.invalid"))
    assert target.read_text() == contents


@pytest.mark.parametrize("problem", ["lock", "symlink", "malformed"])
def test_cleanup_refuses_unsafe_config_without_mutation(tmp_path, problem):
    from booley.runtime.incontainer_git_identity_cleanup import cleanup_git_identity

    _git(tmp_path, "init", "-q")
    apply_git_identity(tmp_path, GitIdentity("Dev", "dev@localhost"))
    target = tmp_path / ".git/config.worktree"
    if problem == "lock":
        target.with_name("config.worktree.lock").write_text("someone else's lock")
    elif problem == "symlink":
        source = tmp_path / "source"
        target.replace(source)
        target.symlink_to(source)
    else:
        target.write_text("invalid {{{")
    before = target.read_bytes()
    with pytest.raises(GitIdentityError):
        cleanup_git_identity(tmp_path, GitIdentity("Dev", "dev@localhost"))
    assert target.read_bytes() == before
    if problem == "lock":
        assert target.with_name("config.worktree.lock").read_text() == "someone else's lock"
    else:
        assert not target.with_name("config.worktree.lock").exists()


def test_cleanup_linked_human_project_and_missing_file(tmp_path):
    from booley.runtime.incontainer_git_identity_cleanup import cleanup_git_identity

    _git(tmp_path, "init", "-q")
    _git(
        tmp_path,
        "-c",
        "user.name=Human",
        "-c",
        "user.email=human@example.invalid",
        "commit",
        "--allow-empty",
        "-qm",
        "base",
    )
    checkout = tmp_path / "linked"
    _git(tmp_path, "worktree", "add", "-qb", "linked", str(checkout))
    cleanup_git_identity(checkout, GitIdentity("Dev", "dev@localhost"))
    apply_git_identity(checkout, GitIdentity("Dev", "dev@localhost"))
    cleanup_git_identity(checkout, GitIdentity("Dev", "dev@localhost"))
    target = Path(_git(checkout, "rev-parse", "--git-path", "config.worktree"))
    assert "name" not in target.read_text()
    assert _git(tmp_path, "config", "extensions.worktreeConfig") == "true"


@pytest.mark.parametrize(
    "value", ["${localEnv:GITHUB_TOKEN}", "${containerEnv:SECRET}", "${anything}"]
)
def test_identity_rejects_devcontainer_interpolation(tmp_path, value):
    (tmp_path / "booley.toml").write_text(f"[agent.git]\nname='{value}'\n")
    with pytest.raises(GitIdentityError, match="interpolation"):
        load_git_identity(tmp_path)
    with pytest.raises(GitIdentityError, match="interpolation"):
        identity_setup.git_identity_environment(GitIdentity(value, "dev@localhost"))


def test_failed_cleanup_stage_preserves_original_and_removes_owned_lock(tmp_path, monkeypatch):
    from booley.runtime import incontainer_git_identity_cleanup as cleanup

    _git(tmp_path, "init", "-q")
    apply_git_identity(tmp_path, GitIdentity("Dev", "dev@localhost"))
    target = tmp_path / ".git/config.worktree"
    before = target.read_bytes()

    def fail(*_args):
        raise GitIdentityError("forced stage failure")

    monkeypatch.setattr(cleanup, "_remove_pair", fail)
    with pytest.raises(GitIdentityError, match="forced stage"):
        cleanup.cleanup_git_identity(tmp_path, GitIdentity("Dev", "dev@localhost"))
    assert target.read_bytes() == before
    assert not target.with_name("config.worktree.lock").exists()


def test_cleanup_does_not_trust_inherited_git_config(tmp_path, monkeypatch):
    from booley.runtime.incontainer_git_identity_cleanup import cleanup_git_identity

    _git(tmp_path, "init", "-q")
    apply_git_identity(tmp_path, GitIdentity("Human", "human@example.invalid"))
    target = tmp_path / ".git/config.worktree"
    before = target.read_bytes()
    for key, value in identity_setup.git_identity_environment(
        GitIdentity("Dev", "dev@localhost")
    ).items():
        monkeypatch.setenv(key, value)
    cleanup_git_identity(tmp_path, GitIdentity("Dev", "dev@localhost"))
    assert target.read_bytes() == before


def test_stale_image_missing_cleanup_module_cannot_apply_identity(tmp_path):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    _git(workspace, "init", "-q")
    _git(workspace, "config", "user.name", "Human")
    old_source = tmp_path / "old-source"
    package = old_source / "booley/runtime"
    package.mkdir(parents=True)
    (old_source / "booley/__init__.py").touch()
    (package / "__init__.py").touch()
    (package / "incontainer_git_identity.py").write_text(
        'raise AssertionError("old apply invoked")\n'
    )
    environment = {**os.environ, "PYTHONPATH": str(old_source)}
    result = subprocess.run(
        [sys.executable, "-m", "booley.runtime.incontainer_git_identity_cleanup"],
        cwd=workspace,
        env=environment,
        capture_output=True,
        text=True,
        check=False,
        timeout=10,
    )
    assert result.returncode != 0
    assert "No module named" in result.stderr
    assert "old apply invoked" not in result.stderr
    assert _git(workspace, "config", "user.name") == "Human"


def test_cleanup_preserves_next_writers_lock_after_publication(tmp_path, monkeypatch):
    from booley.runtime.incontainer_git_identity_cleanup import cleanup_git_identity

    _git(tmp_path, "init", "-q")
    apply_git_identity(tmp_path, GitIdentity("Dev", "dev@localhost"))
    target = tmp_path / ".git/config.worktree"
    lock = target.with_name("config.worktree.lock")
    original_replace = Path.replace

    def publish_and_acquire_next_lock(source, destination):
        result = original_replace(source, destination)
        if source == lock and destination == target:
            lock.write_text("next Git writer's lock")
        return result

    monkeypatch.setattr(Path, "replace", publish_and_acquire_next_lock)
    cleanup_git_identity(tmp_path, GitIdentity("Dev", "dev@localhost"))
    assert lock.read_text() == "next Git writer's lock"


@pytest.mark.parametrize("preexisting", [False, True])
def test_cleanup_staging_lock_ownership_on_timeout(tmp_path, monkeypatch, preexisting):
    from booley.runtime import incontainer_git_identity_cleanup as cleanup

    _git(tmp_path, "init", "-q")
    apply_git_identity(tmp_path, GitIdentity("Dev", "dev@localhost"))
    target = tmp_path / ".git/config.worktree"
    lock = target.with_name("config.worktree.lock")
    nested = target.with_name("config.worktree.lock.lock")
    before = target.read_bytes()
    if preexisting:
        nested.write_text("another writer")
    original_config = cleanup._config

    def timed_out_git(checkout, staged, *args):
        if "--unset-all" in args:
            nested.write_text("interrupted staging writer")
            raise GitIdentityError("forced subprocess timeout")
        return original_config(checkout, staged, *args)

    monkeypatch.setattr(cleanup, "_config", timed_out_git)
    with pytest.raises(GitIdentityError, match="existing lock" if preexisting else "timeout"):
        cleanup.cleanup_git_identity(tmp_path, GitIdentity("Dev", "dev@localhost"))
    assert target.read_bytes() == before
    assert not lock.exists()
    if preexisting:
        assert nested.read_text() == "another writer"
    else:
        assert not nested.exists()
