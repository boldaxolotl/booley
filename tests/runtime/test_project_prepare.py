"""Project preparation is shared by authoring, readiness, and execution."""

from __future__ import annotations

import subprocess
from pathlib import Path

from booley.runtime.project_prepare import prepare_project


def _git(repo: Path, *args: str) -> str:
    return subprocess.run(
        ["git", *args], cwd=repo, check=True, capture_output=True, text=True
    ).stdout.strip()


def _project(tmp_path: Path) -> tuple[Path, Path]:
    root = tmp_path / "rtl"
    root.mkdir()
    _git(root, "init", "-b", "main")
    _git(root, "config", "user.name", "Test")
    _git(root, "config", "user.email", "test@example.invalid")
    (root / ".gitignore").write_text("/generated.hex\n", encoding="utf-8")
    project = root / ".booley_project"
    (project / "hooks").mkdir(parents=True)
    (project / "hooks" / "post-setup.sh").write_text(
        "#!/bin/sh\nprintf 'firmware\\n' > generated.hex\n",
        encoding="utf-8",
    )
    _git(root, "add", "-A")
    _git(root, "commit", "-m", "initial")
    return root, project


def test_prepare_project_is_idempotent_and_never_commits_generated_files(tmp_path: Path) -> None:
    root, _project_dir = _project(tmp_path)
    before = _git(root, "rev-parse", "HEAD")

    first = prepare_project(root, root, slug="demo", sim_flow_enabled=True)
    second = prepare_project(root, root, slug="demo", sim_flow_enabled=True)

    assert first.ok and second.ok
    assert (root / "generated.hex").read_text(encoding="utf-8") == "firmware\n"
    assert _git(root, "rev-parse", "HEAD") == before
    assert _git(root, "status", "--porcelain") == ""


def test_prepare_project_returns_hook_failure(tmp_path: Path) -> None:
    root, project = _project(tmp_path)
    (project / "hooks" / "post-setup.sh").write_text(
        "#!/bin/sh\necho broken >&2\nexit 7\n", encoding="utf-8"
    )

    result = prepare_project(root, root, slug="demo", sim_flow_enabled=True)

    assert not result.ok
    assert "rc=7" in result.error
    assert "broken" in result.error
