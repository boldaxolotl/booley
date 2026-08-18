"""Tests for filesystem_utils: safe_rmtree and copy_booley_tree."""

from __future__ import annotations

import stat
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from booley.runtime.filesystem_utils import copy_booley_tree, safe_rmtree

# ---------------------------------------------------------------------------
# safe_rmtree
# ---------------------------------------------------------------------------


class TestSafeRmtree:
    def test_removes_simple_tree(self, tmp_path: Path):
        d = tmp_path / "target"
        d.mkdir()
        (d / "file.txt").write_text("hello")
        (d / "sub").mkdir()
        (d / "sub" / "nested.txt").write_text("world")

        safe_rmtree(d)
        assert not d.exists()

    def test_noop_when_path_does_not_exist(self, tmp_path: Path):
        """Should not raise when path doesn't exist."""
        safe_rmtree(tmp_path / "nonexistent")

    def test_refuses_git_root_by_default(self, tmp_path: Path):
        """protect_git_root=True should block removal of a real git dir."""
        d = tmp_path / "repo"
        d.mkdir()
        git = d / ".git"
        git.mkdir()
        (git / "HEAD").write_text("ref: refs/heads/main")

        with pytest.raises(ValueError, match="refusing to remove git root"):
            safe_rmtree(d)
        assert d.exists()

    def test_allows_git_root_when_protection_off(self, tmp_path: Path):
        d = tmp_path / "repo"
        d.mkdir()
        git = d / ".git"
        git.mkdir()
        (git / "HEAD").write_text("ref: refs/heads/main")

        safe_rmtree(d, protect_git_root=False)
        assert not d.exists()

    def test_handles_read_only_files(self, tmp_path: Path):
        """Read-only files (e.g. git objects) should still be removed."""
        d = tmp_path / "target"
        d.mkdir()
        f = d / "readonly.txt"
        f.write_text("locked")
        f.chmod(stat.S_IREAD)

        safe_rmtree(d)
        assert not d.exists()

    def test_accepts_string_path(self, tmp_path: Path):
        d = tmp_path / "strpath"
        d.mkdir()
        (d / "x.txt").write_text("data")
        safe_rmtree(str(d))
        assert not d.exists()


# ---------------------------------------------------------------------------
# copy_booley_tree
# ---------------------------------------------------------------------------


class TestCopyBooleyTree:
    def test_copies_files(self, tmp_path: Path):
        src = tmp_path / "src_tree"
        src.mkdir()
        (src / "main.py").write_text("print('hello')")
        (src / "tools").mkdir()
        (src / "tools" / "tool.py").write_text("# tool")

        dst = tmp_path / "dst_tree"
        copy_booley_tree(src, dst)

        assert (dst / "main.py").exists()
        assert (dst / "tools" / "tool.py").exists()

    def test_excludes_standard_dirs(self, tmp_path: Path):
        """Directories in BOOLEY_COPY_EXCLUDES should be skipped."""
        src = tmp_path / "src_tree"
        src.mkdir()
        (src / "keep.py").write_text("ok")
        for excl in [".git", ".venv", "__pycache__", "tmp", "target"]:
            d = src / excl
            d.mkdir()
            (d / "inside.txt").write_text("nope")

        dst = tmp_path / "dst_tree"
        copy_booley_tree(src, dst)

        assert (dst / "keep.py").exists()
        for excl in [".git", ".venv", "__pycache__", "tmp", "target"]:
            assert not (dst / excl).exists(), f"{excl} should be excluded"

    def test_replaces_existing_dst(self, tmp_path: Path):
        """If dst already exists, it should be removed first."""
        src = tmp_path / "src_tree"
        src.mkdir()
        (src / "new.py").write_text("new content")

        dst = tmp_path / "dst_tree"
        dst.mkdir()
        (dst / "old.py").write_text("old content")

        copy_booley_tree(src, dst)

        assert (dst / "new.py").exists()
        assert not (dst / "old.py").exists()

    def test_excludes_pyc_files(self, tmp_path: Path):
        src = tmp_path / "src_tree"
        src.mkdir()
        (src / "code.py").write_text("ok")
        (src / "code.pyc").write_text("bytecode")

        dst = tmp_path / "dst_tree"
        copy_booley_tree(src, dst)

        assert (dst / "code.py").exists()
        assert not (dst / "code.pyc").exists()
