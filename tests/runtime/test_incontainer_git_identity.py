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


def test_interactive_identity_overrides_copied_global_config(tmp_path: Path) -> None:
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
        [sys.executable, "-m", "booley.runtime.incontainer_git_identity"],
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
