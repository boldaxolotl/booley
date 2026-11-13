"""``booley session refresh`` re-applies Project Initialization without rewriting tracked files."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

import pytest

from booley.fusesoc.core_projection import PROJECTED_CORE_GLOB
from booley.harness import init_cmd
from booley.harness import session_refresh as harness_refresh
from booley.runtime import session_refresh as runtime_refresh
from booley.runtime.lifecycle_lock import LifecycleLockError
from booley.runtime.session_runtime import SessionError
from tests.harness import test_init_rerun as init_fixtures
from tests.subprocess_budget import run_bounded_child

# Reuse the init suite's isolated scratch repo (no Docker, isolated HOME) and its guard.
repo = init_fixtures.repo
_no_docker_cli = init_fixtures._no_docker_cli

CORE_TEXT = "CAPI=2:\nname: example:lib:cpu:1.0\n"


def _git(root: Path, *args: str) -> str:
    return run_bounded_child(["git", "-C", str(root), *args], check=True).stdout


def _refresh(root: Path) -> runtime_refresh.RefreshResult:
    """Run Session refresh with the runtime (Docker) half replaced by a fixed outcome."""
    image = runtime_refresh.RefreshImage("sha256:fresh", "payload", None, False)
    outcome = runtime_refresh.RefreshResult(image, "kept", None, "container-1")
    with patch.object(harness_refresh.runtime_refresh, "refresh", return_value=outcome):
        return harness_refresh.refresh(root)


@pytest.fixture
def initialized(repo: Path) -> Path:
    """A fully initialized Stealth Project with one authored core."""
    booley_dir = repo / ".booley"
    (booley_dir / "cores").mkdir(parents=True)
    (booley_dir / "AGENTS.md").write_text("# Project guide\n", encoding="utf-8")
    (booley_dir / "booley.toml").write_text("[stealth]\nenabled = true\n", encoding="utf-8")
    (booley_dir / "cores" / "cpu.core").write_text(CORE_TEXT, encoding="utf-8")
    assert init_fixtures._run_full_init(repo) == 0
    return repo


def test_refresh_restores_project_file_drift(initialized: Path) -> None:
    root = initialized
    (root / "AGENTS.md").unlink()
    (root / ".git" / "hooks" / "commit-msg").unlink()
    _git(root, "config", "--unset", "gc.worktreePruneExpire")
    for projection in root.glob(PROJECTED_CORE_GLOB):
        projection.unlink()
    exclude = root / ".git" / "info" / "exclude"
    assert PROJECTED_CORE_GLOB in exclude.read_text(encoding="utf-8")
    exclude.write_text(
        "".join(
            line + "\n"
            for line in exclude.read_text(encoding="utf-8").splitlines()
            if PROJECTED_CORE_GLOB not in line
        ),
        encoding="utf-8",
    )
    assert PROJECTED_CORE_GLOB not in exclude.read_text(encoding="utf-8")

    _refresh(root)

    assert (root / "AGENTS.md").exists()
    assert (root / ".git" / "hooks" / "commit-msg").is_file()
    assert _git(root, "config", "--get", "gc.worktreePruneExpire").strip() == "never"
    assert len(list(root.glob(PROJECTED_CORE_GLOB))) == 1
    assert PROJECTED_CORE_GLOB in exclude.read_text(encoding="utf-8")


def _commit_crlf_checkout(root: Path, name: str, content: bytes) -> None:
    """Commit *name* with LF line endings, then leave a CRLF copy in the working tree."""
    (root / name).write_bytes(content.replace(b"\r\n", b"\n"))
    _git(root, "add", name)
    _git(
        root,
        "-c",
        "user.name=Test",
        "-c",
        "user.email=test@example.invalid",
        "commit",
        "--message",
        "add file",
    )
    (root / name).write_bytes(content)


def test_refresh_reports_crlf_without_rewriting_tracked_file(
    initialized: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    root = initialized
    crlf_bytes = b"line one\r\nline two\r\n"
    _commit_crlf_checkout(root, "notes.txt", crlf_bytes)
    _git(root, "config", "--unset", "core.autocrlf")
    inner_attributes = (root / ".booley" / ".gitattributes").read_bytes()
    capsys.readouterr()

    _refresh(root)

    assert (root / "notes.txt").read_bytes() == crlf_bytes
    assert not (root / ".gitattributes").exists()
    assert (root / ".booley" / ".gitattributes").read_bytes() == inner_attributes
    assert "1 tracked file(s) are checked out with CRLF" in capsys.readouterr().out
    # Pinning autocrlf=false over a CRLF checkout would only add phantom diffs.
    unpinned = run_bounded_child(
        ["git", "-C", str(root), "config", "--local", "--get", "core.autocrlf"]
    )
    assert unpinned.returncode == 1


def test_refresh_fails_with_container_outcome_when_a_step_errs(initialized: Path) -> None:
    (initialized / ".booley" / "booley.toml").write_text("[agent\n", encoding="utf-8")

    with pytest.raises(SessionError) as excinfo:
        _refresh(initialized)

    message = str(excinfo.value)
    assert message.startswith("Sandbox refreshed (kept: already current), but Project Init")
    assert "agent_config: invalid booley.toml" in message
    assert "run `booley init` on the host" in message


def test_refresh_converts_an_escaped_filesystem_error(
    initialized: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def disk_full(_root: Path, _facts: init_cmd.ProjectFacts) -> None:
        raise OSError("disk full")

    monkeypatch.setattr(init_cmd, "reinitialize_project", disk_full)

    with pytest.raises(SessionError, match=r"kept: already current\), .* failed: disk full"):
        _refresh(initialized)


def test_refresh_converts_a_lifecycle_lock_timeout(
    initialized: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def lock_timeout(_root: Path, _facts: init_cmd.ProjectFacts) -> None:
        raise LifecycleLockError("host Docker lifecycle is busy")

    monkeypatch.setattr(init_cmd, "reinitialize_project", lock_timeout)

    with pytest.raises(SessionError, match=r"kept: already current\), .* failed: host Docker"):
        _refresh(initialized)


def test_refresh_leaves_legacy_hooks_and_a_tracked_bundle_alone(initialized: Path) -> None:
    root = initialized
    booley_dir = root / ".booley"
    hooks = booley_dir / "hooks"
    hooks.mkdir()
    legacy = hooks / "commit_msg_hook.py"
    legacy.write_bytes(b"# diverged legacy hook\n")
    bundle = booley_dir / ".managed" / "project-git-hooks.pyz"
    bundle.write_bytes(b"stale tracked bundle")
    _git(booley_dir, "add", "-f", "hooks", ".managed/project-git-hooks.pyz")

    _refresh(root)

    assert legacy.read_bytes() == b"# diverged legacy hook\n"
    assert not legacy.with_name("commit_msg_hook.py.pre-booley").exists()
    assert bundle.read_bytes() == b"stale tracked bundle"
    assert _git(booley_dir, "status", "--porcelain", "hooks").startswith("A")
