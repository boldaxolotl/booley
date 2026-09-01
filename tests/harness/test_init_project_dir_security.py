"""Project-data directory permissions required by Session issuance."""

from pathlib import Path

import pytest

from booley.harness import init_cmd
from booley.harness.setup.common import InitContext


@pytest.mark.skipif(init_cmd.os.name == "nt", reason="POSIX permission contract")
def test_existing_project_data_directory_is_tightened(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    target = tmp_path / ".booley_project"
    target.mkdir(mode=0o775)
    target.chmod(0o775)
    monkeypatch.setattr(init_cmd, "_backfill_config_skeletons", lambda *_args: None)
    monkeypatch.setattr(init_cmd, "_backfill_project_gitignore", lambda *_args: None)
    monkeypatch.setattr(init_cmd, "_backfill_fusesoc_ignore", lambda *_args: None)
    monkeypatch.setattr(init_cmd, "_init_project_git_repo", lambda *_args: None)
    init_cmd._step_project_dir(InitContext(project_root=tmp_path))
    assert target.stat().st_mode & 0o777 == 0o700


@pytest.mark.skipif(init_cmd.os.name == "nt", reason="POSIX permission contract")
def test_new_project_data_directory_is_private(tmp_path: Path) -> None:
    init_cmd._step_project_dir(InitContext(project_root=tmp_path))
    assert (tmp_path / ".booley_project").stat().st_mode & 0o777 == 0o700
