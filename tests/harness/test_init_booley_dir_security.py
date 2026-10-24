"""Booley Directory permissions required by Session issuance."""

from pathlib import Path

import pytest

from booley.harness import init_cmd
from booley.harness.setup.common import InitContext
from tests.conftest import symlink_or_skip


@pytest.mark.skipif(init_cmd.os.name == "nt", reason="POSIX permission contract")
def test_existing_project_data_directory_is_tightened(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    target = tmp_path / ".booley"
    target.mkdir(mode=0o775)
    target.chmod(0o775)
    monkeypatch.setattr(init_cmd, "_backfill_config_skeletons", lambda *_args: None)
    monkeypatch.setattr(init_cmd, "_backfill_project_gitignore", lambda *_args: None)
    monkeypatch.setattr(init_cmd, "_backfill_fusesoc_ignore", lambda *_args: None)
    monkeypatch.setattr(init_cmd, "_init_project_git_repo", lambda *_args: None)
    init_cmd._step_booley_dir(InitContext(project_root=tmp_path))
    assert target.stat().st_mode & 0o777 == 0o700


@pytest.mark.skipif(init_cmd.os.name == "nt", reason="POSIX permission contract")
def test_new_project_data_directory_is_private(tmp_path: Path) -> None:
    init_cmd._step_booley_dir(InitContext(project_root=tmp_path))
    assert (tmp_path / ".booley").stat().st_mode & 0o777 == 0o700


def test_project_data_scaffold_is_written_with_lf(tmp_path: Path) -> None:
    """Windows text mode must not turn Booley Directory scaffolds into CRLF (#609)."""
    ctx = InitContext(project_root=tmp_path)
    init_cmd._step_booley_dir(ctx)
    booley_dir = tmp_path / ".booley"
    gitignore = booley_dir / ".gitignore"
    gitignore.write_bytes(b"# user rules\n")
    init_cmd._backfill_project_gitignore(booley_dir, ctx)

    written = [path for path in booley_dir.iterdir() if path.is_file()]
    assert {".gitignore", "booley.toml", "FUSESOC_IGNORE"} <= {path.name for path in written}
    for path in written:
        assert b"\r\n" not in path.read_bytes(), path.name


def test_symlinked_project_gitignore_is_refused(tmp_path: Path) -> None:
    booley_dir = tmp_path / ".booley"
    booley_dir.mkdir()
    destination = tmp_path / "outside-ignore"
    destination.write_bytes(b"# user rules\n")
    symlink_or_skip(booley_dir / ".gitignore", destination)
    ctx = InitContext(project_root=tmp_path)

    init_cmd._backfill_project_gitignore(booley_dir, ctx)

    assert destination.read_bytes() == b"# user rules\n"
    assert (booley_dir / ".gitignore").readlink().resolve() == destination.resolve()
    assert (ctx.results[-1].name, ctx.results[-1].status) == ("booley_dir", "warn")
