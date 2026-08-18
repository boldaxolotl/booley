"""Tests for project_dir: 4-step project directory discovery."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from booley.runtime.project_dir import (
    reset_cache,
    resolve_checkout_project_dir,
    resolve_project_dir,
)
from booley.ticket_board.helpers import detect_project_root


@pytest.fixture(autouse=True)
def _clear_cache():
    """Reset the module-level cache before and after each test."""
    reset_cache()
    yield
    reset_cache()


# ---------------------------------------------------------------------------
# Step 1: BOOLEY_PROJECT_DIR env var
# ---------------------------------------------------------------------------


class TestEnvVarOverride:
    def test_env_var_returns_path(self, tmp_path: Path, monkeypatch):
        d = tmp_path / "my_project"
        d.mkdir()
        monkeypatch.setenv("BOOLEY_PROJECT_DIR", str(d))
        result = resolve_project_dir()
        assert result == d.resolve()

    def test_env_var_nonexistent_warns(self, tmp_path: Path, monkeypatch):
        fake = tmp_path / "does_not_exist"
        monkeypatch.setenv("BOOLEY_PROJECT_DIR", str(fake))
        with pytest.warns(UserWarning, match="does not exist"):
            result = resolve_project_dir()
        assert result == fake.resolve()

    def test_env_var_takes_precedence_over_sibling(self, tmp_path: Path, monkeypatch):
        """Env var should win even if sibling .booley_project/ exists."""
        env_dir = tmp_path / "env_project"
        env_dir.mkdir()
        monkeypatch.setenv("BOOLEY_PROJECT_DIR", str(env_dir))
        # Also create a sibling that would match step 3
        sibling = tmp_path / ".booley_project"
        sibling.mkdir()
        result = resolve_project_dir()
        assert result == env_dir.resolve()

    def test_detect_project_root_uses_project_dir_parent(self, tmp_path: Path, monkeypatch):
        project_dir = tmp_path / ".booley_project"
        project_dir.mkdir()
        monkeypatch.setenv("BOOLEY_PROJECT_DIR", str(project_dir))

        assert detect_project_root() == tmp_path.resolve()

    def test_checkout_local_snapshot_overrides_session_global_dir(self, tmp_path, monkeypatch):
        session_dir = tmp_path / "session-project"
        session_dir.mkdir()
        checkout = tmp_path / "ticket-checkout"
        local = checkout / ".booley_project"
        local.mkdir(parents=True)
        monkeypatch.setenv("BOOLEY_PROJECT_DIR", str(session_dir))
        assert resolve_project_dir() == session_dir.resolve()  # pre-warm global cache

        assert resolve_checkout_project_dir(checkout) == local.resolve()


# ---------------------------------------------------------------------------
# Step 2: booley.toml [project] dir
# ---------------------------------------------------------------------------


class TestBooleyToml:
    def test_absolute_dir_in_toml(self, tmp_path: Path, monkeypatch):
        monkeypatch.delenv("BOOLEY_PROJECT_DIR", raising=False)
        project_d = tmp_path / "custom_project"
        project_d.mkdir()

        # booley.toml lives in the walk-up path
        toml_path = tmp_path / "booley.toml"
        toml_path.write_text(
            f'[project]\ndir = "{project_d.as_posix()}"',
            encoding="utf-8",
        )

        result = resolve_project_dir(start=tmp_path)
        assert result == project_d

    def test_relative_dir_in_toml(self, tmp_path: Path, monkeypatch):
        monkeypatch.delenv("BOOLEY_PROJECT_DIR", raising=False)
        subdir = tmp_path / "sub"
        subdir.mkdir()
        project_d = tmp_path / "rel_proj"
        project_d.mkdir()

        toml_path = tmp_path / "booley.toml"
        toml_path.write_text('[project]\ndir = "rel_proj"', encoding="utf-8")

        result = resolve_project_dir(start=subdir)
        assert result == project_d.resolve()

    def test_malformed_toml_falls_through(self, tmp_path: Path, monkeypatch):
        """Broken booley.toml should fall through to step 3."""
        monkeypatch.delenv("BOOLEY_PROJECT_DIR", raising=False)
        (tmp_path / "booley.toml").write_text("INVALID{{{", encoding="utf-8")
        sibling = tmp_path / ".booley_project"
        sibling.mkdir()

        result = resolve_project_dir(start=tmp_path)
        assert result == sibling


# ---------------------------------------------------------------------------
# Step 3: Walk-up .booley_project/
# ---------------------------------------------------------------------------


class TestWalkUpConvention:
    def test_finds_booley_project(self, tmp_path: Path, monkeypatch):
        monkeypatch.delenv("BOOLEY_PROJECT_DIR", raising=False)
        project_dir = tmp_path / ".booley_project"
        project_dir.mkdir()
        # Start from a subdirectory
        subdir = tmp_path / "sub" / "deep"
        subdir.mkdir(parents=True)

        result = resolve_project_dir(start=subdir)
        assert result == project_dir


# ---------------------------------------------------------------------------
# Step 4: Not found
# ---------------------------------------------------------------------------


class TestNotFound:
    def test_raises_when_nothing_found(self, tmp_path: Path, monkeypatch):
        monkeypatch.delenv("BOOLEY_PROJECT_DIR", raising=False)
        # Empty tmp_path — no toml, no .booley_project/
        with pytest.raises(FileNotFoundError, match="booley init"):
            resolve_project_dir(start=tmp_path)


# ---------------------------------------------------------------------------
# Caching
# ---------------------------------------------------------------------------


class TestCaching:
    def test_cache_returns_same_value(self, tmp_path: Path, monkeypatch):
        d = tmp_path / "cached_proj"
        d.mkdir()
        monkeypatch.setenv("BOOLEY_PROJECT_DIR", str(d))
        r1 = resolve_project_dir()
        r2 = resolve_project_dir()
        assert r1 == r2
        assert r1 is r2  # same object from cache

    def test_reset_cache_clears(self, tmp_path: Path, monkeypatch):
        d1 = tmp_path / "proj1"
        d1.mkdir()
        monkeypatch.setenv("BOOLEY_PROJECT_DIR", str(d1))
        r1 = resolve_project_dir()

        d2 = tmp_path / "proj2"
        d2.mkdir()
        reset_cache()
        monkeypatch.setenv("BOOLEY_PROJECT_DIR", str(d2))
        r2 = resolve_project_dir()

        assert r1 != r2
