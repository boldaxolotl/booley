"""`booley init` inside a hand-written Sandbox runs Project Initialization alone (issue 2751)."""

from __future__ import annotations

import argparse
import sys
from collections.abc import Iterator
from pathlib import Path

import pytest

from booley.harness import booley as cli
from booley.harness import init_cmd
from booley.harness.setup import git_hooks
from booley.harness.setup.common import InitContext
from booley.runtime import checkout_role, dashboard_tasks, runtime_context
from booley.runtime.booley_dir import init_booley_dir_scope, reset_cache
from booley.runtime.checkout_role import SourceCheckoutProjectError
from tests.docker_cli_guard import assert_docker_cli_unused, forbid_docker_cli
from tests.harness.test_project_initialization import HOST_PRIVATE_ENVIRONMENT, _repository_state
from tests.subprocess_budget import run_bounded_child

# A Sandbox is always Linux; the fixtures rely on symlink guidance links and POSIX Git checkouts.
pytestmark = pytest.mark.skipif(sys.platform == "win32", reason="a Sandbox is always Linux")

CORE_TEXT = "CAPI=2:\nname: example:lib:cpu:1.0\n"
GIT_IDENTITY = ("-c", "user.name=Publisher", "-c", "user.email=publisher@example.invalid")
ISSUED_MOUNTINFO = "1 0 0:1 / /work/.devcontainer ro - ext4 /dev/x ro\n"
PLAIN_MOUNTINFO = "1 0 0:1 / / rw - overlay overlay rw\n2 1 0:2 / /work rw - ext4 /dev/x rw\n"
TASKS_JSON = b'{"version": "2.0.0", "tasks": [{"label": "build", "command": "make"}]}\n'


def _git(repository: Path, *args: str) -> str:
    """Run git in *repository* and return stdout."""
    completed = run_bounded_child(
        ["git", *GIT_IDENTITY, "-C", str(repository), *args],
        check=True,
    )
    return completed.stdout


def _commit_all(repository: Path, *paths: str) -> None:
    """Commit *paths* (default: everything) without running any hook."""
    _git(repository, "add", *(paths or ("-A",)))
    if _git(repository, "status", "--porcelain", "--untracked-files=no"):  # else nothing to commit
        _git(repository, "commit", "--no-verify", "-m", "snapshot")


def _publish(publisher: Path) -> None:
    """Build the repositories a published demo ships: a Booley Directory and an outer repo."""
    publisher.mkdir()
    _git(publisher, "init", "-b", "main")
    booley_dir = publisher / ".booley"
    (booley_dir / "cores").mkdir(parents=True)
    (booley_dir / "AGENTS.md").write_text("# Project guide\n", encoding="utf-8", newline="\n")
    # Stealth Mode is opt-in: a published demo records it on so the Booley Directory is its own repository.
    (booley_dir / "booley.toml").write_bytes(
        b'[agent]\nprovider = "claude"\n\n[stealth]\nenabled = true\n'
    )
    (booley_dir / "cores" / "cpu.core").write_text(CORE_TEXT, encoding="utf-8", newline="\n")
    (publisher / "README.md").write_bytes(b"# Demo\n")
    ctx = InitContext(project_root=publisher, interactive=False, show_step_banners=False)
    config_path = booley_dir / "booley.toml"
    selection = init_cmd._resolve_agent_selection(ctx, init_cmd.argparse.Namespace(), config_path)
    request = init_cmd.ProjectInitialization(
        selection,
        config_path,
        lambda: init_cmd.ProjectFacts((2, 50, 0)),
        reconcile_interactive_files=False,
    )
    with init_booley_dir_scope(publisher):
        assert init_cmd.initialize_project(ctx, request)
    _commit_all(booley_dir)
    _commit_all(publisher, "README.md")


@pytest.fixture(autouse=True)
def sandbox_environment(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """A container without issued mounts, no Docker and no host-private state."""
    mountinfo = tmp_path / "mountinfo"
    mountinfo.write_text(PLAIN_MOUNTINFO, encoding="utf-8", newline="\n")
    monkeypatch.setattr(runtime_context, "_MOUNTINFO", mountinfo, raising=False)
    monkeypatch.setenv("BOOLEY_CONTAINER", "1")
    monkeypatch.setenv("BOOLEY_EGRESS", "unproxied")
    for variable in ("BOOLEY_DIR", "RTL_PROJECT_ROOT", "BOOLEY_AGENT_APP"):
        monkeypatch.delenv(variable, raising=False)
    forbid_docker_cli(monkeypatch)
    for name in HOST_PRIVATE_ENVIRONMENT:
        monkeypatch.setenv(name, str(tmp_path / "absent" / name))
    reset_cache()
    yield
    assert_docker_cli_unused()


@pytest.fixture
def project(tmp_path: Path, sandbox_environment: None) -> Path:
    """A fresh Codespace checkout: both repositories cloned, none of the local setup."""
    publisher = tmp_path / "publisher"
    _publish(publisher)
    root = tmp_path / "codespace"
    _git(tmp_path, "clone", str(publisher), str(root))
    _git(tmp_path, "clone", str(publisher / ".booley"), str(root / ".booley"))
    reset_cache()
    return root


def _booley_init(root: Path, monkeypatch: pytest.MonkeyPatch, *options: str) -> int:
    """Run the real CLI as `booley init` with the Project Root as the working directory."""
    monkeypatch.chdir(root)
    reset_cache()
    monkeypatch.setattr(sys, "argv", ["booley", "init", *options])
    try:
        return cli.main()
    except SystemExit as refusal:  # the command-location guard exits instead of returning
        return int(refusal.code or 0)


def _set_booley_toml(root: Path, text: str) -> bytes:
    """Replace and commit the clone's ``booley.toml``; return its bytes."""
    path = root / ".booley" / "booley.toml"
    path.write_bytes(text.encode())
    _commit_all(root / ".booley", "booley.toml")
    return path.read_bytes()


def _local_config(root: Path, key: str) -> str:
    return _git(root, "config", "--local", "--get", key).strip()


def test_sandbox_init_sets_up_a_fresh_project(
    project: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    assert not (project / "AGENTS.md").exists()
    assert not (project / ".vscode").exists()

    assert _booley_init(project, monkeypatch) == 0

    capsys.readouterr()
    booley_dir = project / ".booley"
    assert (project / "AGENTS.md").resolve() == (booley_dir / "AGENTS.md").resolve()
    assert (project / "CLAUDE.md").resolve() == (booley_dir / "AGENTS.md").resolve()
    tasks = (project / ".vscode" / "tasks.json").read_text(encoding="utf-8")
    assert "Booley Dashboard" in tasks
    assert dashboard_tasks.attach_blocker(project, booley_dir) == ""
    assert _local_config(project, "gc.worktreePruneExpire") == "never"
    assert _local_config(project, "worktree.useRelativePaths") in {"true", "false"}
    assert _git(project, "status", "--porcelain") == ""
    assert _git(booley_dir, "status", "--porcelain") == ""


def test_second_sandbox_init_changes_nothing(
    project: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    assert _booley_init(project, monkeypatch) == 0
    after_first = _repository_state(project)

    assert _booley_init(project, monkeypatch) == 0

    assert _repository_state(project) == after_first


def test_issued_sandbox_refuses_init_without_writing(
    project: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    before = _repository_state(project)
    # None: an unreadable mountinfo also reads as issued, so the refusal is the safe verdict.
    for mountinfo in (ISSUED_MOUNTINFO, None):
        issued = tmp_path / "issued-mountinfo"
        issued.unlink(missing_ok=True)
        if mountinfo is not None:
            issued.write_text(mountinfo, encoding="utf-8", newline="\n")
        monkeypatch.setattr(runtime_context, "_MOUNTINFO", issued, raising=False)
        capsys.readouterr()

        assert _booley_init(project, monkeypatch) == 2, mountinfo

        assert "`booley init` on the host" in capsys.readouterr().err
        assert _repository_state(project) == before
        assert not (project / ".vscode").exists()
        assert not (project / "AGENTS.md").exists()


def test_sandbox_init_keeps_existing_agent_settings(
    project: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    original = _set_booley_toml(project, '[agent]\ndefault = "codex"\nauth = "subscription"\n')
    monkeypatch.setenv("BOOLEY_AGENT_APP", "claude")

    assert _booley_init(project, monkeypatch) == 0

    assert (project / ".booley" / "booley.toml").read_bytes() == original


def test_sandbox_init_fills_missing_default_cli_from_agent_app(
    project: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    booley_dir = project / ".booley"
    for app in ("claude", "codex"):
        original = _set_booley_toml(project, "[sandbox]\ndashboard = true\n")
        monkeypatch.setenv("BOOLEY_AGENT_APP", app)

        assert _booley_init(project, monkeypatch) == 0, app

        assert booley_dir.joinpath("booley.toml").read_bytes() == (
            original + f'\n[agent]\ndefault = "{app}"\n'.encode()
        )
        assert _git(booley_dir, "status", "--porcelain") == " M booley.toml\n"


def test_sandbox_init_leaves_default_cli_unset_without_a_source(
    project: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    for app in (None, "auto", "none", "gemini"):
        original = _set_booley_toml(project, "[sandbox]\ndashboard = true\n")
        monkeypatch.delenv("BOOLEY_AGENT_APP", raising=False)
        if app is not None:
            monkeypatch.setenv("BOOLEY_AGENT_APP", app)
        capsys.readouterr()

        assert _booley_init(project, monkeypatch) == 0, app

        assert (project / ".booley" / "booley.toml").read_bytes() == original
        assert "left unset" in capsys.readouterr().out


def test_sandbox_init_worktree_policy_follows_local_git(
    project: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    for git_version, expected in (((2, 47, 0), "false"), ((2, 50, 0), "true")):
        # The one patched fact is both the "host" and the "Sandbox" side of the 2.48 rule.
        monkeypatch.setattr(
            git_hooks, "_host_git_version", lambda git_version=git_version: git_version
        )

        assert _booley_init(project, monkeypatch) == 0, git_version

        assert _local_config(project, "worktree.useRelativePaths") == expected


def test_sandbox_init_reports_tracked_tasks_json(
    project: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    tasks = project / ".vscode" / "tasks.json"
    tasks.parent.mkdir()
    tasks.write_bytes(TASKS_JSON)
    _commit_all(project, ".vscode/tasks.json")

    assert _booley_init(project, monkeypatch) == 0

    assert tasks.read_bytes() == TASKS_JSON
    assert ".vscode/tasks.json is tracked by Git" in capsys.readouterr().out
    assert _git(project, "status", "--porcelain") == ""


@pytest.mark.parametrize(
    "options",
    [
        ["--seed"],
        ["--force"],
        ["--repin-booley-dir"],
        ["--scaffold", "uart"],
        ["--provider", "claude"],
        ["--auth", "auto"],
    ],
)
def test_sandbox_init_refuses_host_only_options(
    project: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    options: list[str],
) -> None:
    before = _repository_state(project)

    assert _booley_init(project, monkeypatch, *options) == 2

    assert f"booley init {options[0]}` on the host" in capsys.readouterr().err
    assert _repository_state(project) == before


def test_sandbox_init_check_only_writes_nothing(
    project: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    before = _repository_state(project)

    assert _booley_init(project, monkeypatch, "--check-only") in {0, 1}

    assert _repository_state(project) == before
    assert not (project / ".vscode").exists()


def test_sandbox_init_stops_for_a_pending_directory_migration(
    project: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(init_cmd, "_sandbox_needs_host_migration", lambda _root: True)
    before = _repository_state(project)

    assert _booley_init(project, monkeypatch) == 2

    assert "run `booley init` on the host" in capsys.readouterr().out
    assert _repository_state(project) == before


def test_sandbox_init_reports_unreadable_agent_settings(
    project: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    _set_booley_toml(project, "[agent\n")

    assert _booley_init(project, monkeypatch) == 2

    assert "cannot select an agent" in capsys.readouterr().out
    assert not (project / ".vscode").exists()


def test_sandbox_init_stops_at_a_guidance_conflict(
    project: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    (project / "AGENTS.md").write_bytes(b"# my own guide\n")

    assert _booley_init(project, monkeypatch) == 2

    assert (project / "AGENTS.md").read_bytes() == b"# my own guide\n"
    assert not (project / ".vscode").exists()
    assert "initialization blocked" in capsys.readouterr().out


def test_sandbox_init_reports_filesystem_errors(
    project: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    def broken(_ctx: InitContext) -> None:
        raise OSError("disk on fire")

    monkeypatch.setattr(init_cmd, "_sample_line_ending_baseline", broken)

    assert _booley_init(project, monkeypatch) == 2

    assert "disk on fire" in capsys.readouterr().out


def test_sandbox_init_refuses_booley_source_checkout(
    project: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    def source_checkout(_root: Path) -> Path:
        raise SourceCheckoutProjectError("Booley source is not a Project")

    monkeypatch.setattr(checkout_role, "require_project_checkout", source_checkout)

    # The CLI refuses a source checkout before dispatch; the command guards itself as well.
    assert init_cmd.run_sandbox_init(argparse.Namespace(), project) == 2

    assert "Booley source is not a Project" in capsys.readouterr().out
    assert not (project / ".vscode").exists()


def test_sandbox_init_refuses_host_policy_in_project_config(
    project: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    _set_booley_toml(project, "[sandbox]\nidle_timeout_seconds = 5\n")

    assert _booley_init(project, monkeypatch) == 2

    assert "contains host-only policy" in capsys.readouterr().out
    assert not (project / ".vscode").exists()


def test_sandbox_init_applies_the_line_ending_policy(
    project: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Line-ending normalization runs as on a first host init, in both checkouts."""
    for repository in (project, project / ".booley"):
        _git(repository, "config", "--local", "core.autocrlf", "true")

    assert _booley_init(project, monkeypatch) == 0

    for repository in (project, project / ".booley"):
        assert _local_config(repository, "core.autocrlf") == "false"
    assert "* text=auto eol=lf" in (project / ".git" / "info" / "attributes").read_text(
        encoding="utf-8"
    )
