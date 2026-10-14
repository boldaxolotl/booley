"""The control repository is independent of a run's authored data selection."""

import subprocess
from pathlib import Path

from booley.runtime.booley_dir import reset_cache
from booley.runtime.project_repositories import resolve_inner_project_repo


def test_control_project_repo_ignores_authored_booley_dir(tmp_path: Path, monkeypatch) -> None:
    project = tmp_path / "checkout/.booley"
    project.mkdir(parents=True)
    subprocess.run(["git", "init", "-q", str(project)], check=True, capture_output=True)
    subprocess.run(
        [
            "git",
            "-C",
            str(project),
            "-c",
            "user.name=Fixture",
            "-c",
            "user.email=fixture@example.test",
            "-c",
            "commit.gpgSign=false",
            "commit",
            "--allow-empty",
            "-qm",
            "initial",
        ],
        check=True,
        capture_output=True,
    )
    nested = tmp_path / "authored-project-data"
    subprocess.run(
        ["git", "-C", str(project), "worktree", "add", "--detach", str(nested)],
        check=True,
        capture_output=True,
    )
    monkeypatch.setenv("BOOLEY_DIR", str(nested))
    reset_cache()
    try:
        assert resolve_inner_project_repo(project.parent) == project
    finally:
        reset_cache()
