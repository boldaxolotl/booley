"""Host removal paths must be portable before new registrations exist."""

import shlex
from pathlib import Path, PurePosixPath

import pytest
from tests.harness.test_worktree_cmd import _git, _issued_removal_spec, _write

from booley.runtime import runtime_context, worktree_paths
from booley.runtime.booley_dir import reset_cache
from booley.runtime.project_worktree_pairing import worktree_removal_instructions


@pytest.mark.parametrize(
    "case",
    [
        "canonical",
        "quoted-source",
        "relative-disabled",
        "old-git",
        "linked-primary",
        "containment-escape",
        "windows-drive",
        "windows-unaddressable",
        "sandbox-nonwork-missing-spec",
        "external-bind",
        "missing-source",
        "duplicate-source",
    ],
)
def test_host_removal_preflight(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, case: str
) -> None:
    root = tmp_path / "sandbox"
    root.mkdir()
    assert _git(root, "init").returncode == 0
    _write(root / ".booley/booley.toml", "")
    host = "/host/project's directory" if case == "quoted-source" else "/host/project"
    if case == "windows-drive":
        host = "/c/Users/Developer/project"
    elif case == "windows-unaddressable":
        host = "C:\\Users\\Developer\\project"
    _issued_removal_spec(root, host=host, problem=case)
    monkeypatch.setattr(runtime_context, "inside_session_runtime", lambda: True)
    monkeypatch.delenv("BOOLEY_DIR", raising=False)
    reset_cache()
    destination = root / ".booley/worktrees/new"
    root, destination = _preflight_case(root, destination, tmp_path, monkeypatch, case, host)
    before = _git(root, "config", "--local", "--list").stdout
    try:
        if case in {"canonical", "quoted-source", "windows-drive"}:
            context = worktree_paths.host_removal_context(root, destination, paired=False)
            assert context.root == PurePosixPath(host)
            assert context.destination == PurePosixPath(host) / ".booley/worktrees/new"
            recipe = worktree_removal_instructions(context, "new")
            command = next(
                line.split(": ", 1)[1]
                for line in recipe.splitlines()
                if "Snapshot worktree: " in line
            )
            assert shlex.split(command) == [
                "git",
                "-C",
                host,
                "worktree",
                "remove",
                host + "/.booley/worktrees/new",
            ]
        else:
            with pytest.raises(worktree_paths.HostRemovalError, match="host removal instructions"):
                worktree_paths.host_removal_context(root, destination, paired=False)
        assert not destination.exists()
        assert _git(root, "config", "--local", "--list").stdout == before
        assert not (root / ".git/worktrees").exists()
    finally:
        reset_cache()


def _preflight_case(
    root: Path,
    destination: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    case: str,
    host: str,
) -> tuple[Path, Path]:
    if case == "relative-disabled":
        assert _git(root, "config", "worktree.useRelativePaths", "false").returncode == 0
    elif case == "old-git":
        monkeypatch.setattr(worktree_paths, "_git_supports_relative_paths", lambda: False)
    elif case == "linked-primary":
        _write(root / "README", "committed")
        assert _git(root, "add", "README").returncode == 0
        assert (
            _git(
                root,
                "-c",
                "user.name=Test",
                "-c",
                "user.email=test@example.invalid",
                "commit",
                "-qm",
                "init",
            ).returncode
            == 0
        )
        linked = tmp_path / "linked"
        assert _git(root, "worktree", "add", "--detach", str(linked)).returncode == 0
        _write(linked / ".booley/booley.toml", "")
        _issued_removal_spec(linked, host=host)
        root = linked
        destination = root / ".booley/worktrees/new"
    elif case == "containment-escape":
        destination = tmp_path / "outside"
    elif case == "sandbox-nonwork-missing-spec":
        (root / ".devcontainer/devcontainer.json").unlink()
    return root, destination
