"""The source-checkout helper copies exactly what Git lists and names bad roots."""

from __future__ import annotations

import re
import subprocess
from pathlib import Path

import pytest

from tests.booley_checkouts_support import commit_all, init_repo, isolate_git


def _files_under(root: Path) -> set[str]:
    return {path.relative_to(root).as_posix() for path in root.rglob("*") if path.is_file()}


def test_copy_takes_git_listed_working_tree_files(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from tests.source_checkout_support import copy_source_checkout

    isolate_git(tmp_path, monkeypatch)
    source = tmp_path / "source"
    init_repo(source)
    (source / ".gitignore").write_bytes(b"__pycache__/\n")
    (source / "edited.txt").write_bytes(b"old")
    (source / "gone.txt").write_bytes(b"gone")
    (source / "sub").mkdir()
    (source / "sub/kept.txt").write_bytes(b"kept")
    commit_all(source)
    (source / "edited.txt").write_bytes(b"new")  # uncommitted edit
    (source / "gone.txt").unlink()  # tracked file deleted from the working tree
    (source / "new.txt").write_bytes(b"untracked")
    (source / "__pycache__").mkdir()
    (source / "__pycache__/x.pyc.1").write_bytes(b"transient")

    everything = tmp_path / "everything"
    copy_source_checkout(source, everything)
    assert _files_under(everything) == {".gitignore", "edited.txt", "new.txt", "sub/kept.txt"}
    assert (everything / "edited.txt").read_bytes() == b"new"

    limited = tmp_path / "limited"
    copy_source_checkout(source, limited, "sub")
    assert _files_under(limited) == {"sub/kept.txt"}


@pytest.mark.parametrize("case", ["plain", "nested", "no-git"])
def test_listing_fails_naming_a_root_that_is_not_a_git_checkout_top(
    case: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from tests.source_checkout_support import copy_source_checkout

    isolate_git(tmp_path, monkeypatch)
    if case == "nested":
        enclosing = tmp_path / "enclosing"
        init_repo(enclosing)
        root = enclosing / "inner"
    else:
        # Stop Git from climbing into a repository that encloses the temporary folder.
        monkeypatch.setenv("GIT_CEILING_DIRECTORIES", str(tmp_path))
        root = tmp_path / "plain"
    root.mkdir()
    if case == "no-git":
        empty_path = tmp_path / "empty-path"
        empty_path.mkdir()
        monkeypatch.setenv("PATH", str(empty_path))
    # BaseException, so a skip that replaced the failure is caught and rejected below.
    with pytest.raises(BaseException, match=re.escape(str(root))) as raised:
        copy_source_checkout(root, tmp_path / "destination")
    assert raised.type is pytest.fail.Exception


def test_listing_fails_naming_the_root_when_git_hangs(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from tests.source_checkout_support import source_checkout_files

    def hang(argv: list[str], **kwargs: object) -> object:
        raise subprocess.TimeoutExpired(argv, 60, output=None, stderr=None)

    monkeypatch.setattr("tests.subprocess_budget.run_captured_bounded", hang)
    with pytest.raises(BaseException, match=re.escape(str(tmp_path))) as raised:
        source_checkout_files(tmp_path)
    assert raised.type is pytest.fail.Exception
    assert "timed out" in str(raised.value)
