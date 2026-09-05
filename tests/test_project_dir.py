"""Tests for project_dir: 4-step project directory discovery."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from booley.runtime.checkout_role import SourceCheckoutProjectError
from booley.runtime.project_dir import (
    checkout_project_dir_relative_to,
    project_dir_for_init,
    reset_cache,
    resolve_checkout_project_dir,
    resolve_project_dir,
)
from booley.ticket_board.helpers import detect_project_root


@pytest.fixture(autouse=True)
def _clear_cache(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """Reset the module-level cache before and after each test."""
    reset_cache()
    monkeypatch.chdir(tmp_path)
    yield
    reset_cache()


class TestProjectDirForInit:
    def test_uses_selected_checkout_despite_ancestor_env_and_cache(self, tmp_path, monkeypatch):
        ancestor = tmp_path / ".booley_project"
        ancestor.mkdir()
        child = tmp_path / "child"
        child.mkdir()
        monkeypatch.setenv("BOOLEY_PROJECT_DIR", str(ancestor))
        assert resolve_project_dir() == ancestor.resolve()

        assert project_dir_for_init(child) == child.resolve() / ".booley_project"

    def test_rejects_booley_source_checkout(self, tmp_path):
        root = tmp_path / "booley-source"
        (root / "src" / "booley").mkdir(parents=True)
        (root / "src" / "booley" / "__init__.py").write_text("", encoding="utf-8")
        (root / "pyproject.toml").write_text(
            "[tool.booley]\nsource_checkout = true\n",
            encoding="utf-8",
        )

        with pytest.raises(SourceCheckoutProjectError, match="cannot be initialized"):
            project_dir_for_init(root)


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

    def test_detect_project_root_prefers_ticket_control_plane(self, tmp_path, monkeypatch):
        control_root = tmp_path / "control"
        monkeypatch.setenv("BOOLEY_CONTROL_PROJECT_ROOT", str(control_root))
        monkeypatch.setenv("PROJECT_ROOT", str(tmp_path / "test-override"))
        monkeypatch.setenv("BOOLEY_PROJECT_DIR", str(tmp_path / "authored-project"))

        assert detect_project_root() == control_root

    def test_checkout_local_snapshot_overrides_session_global_dir(self, tmp_path, monkeypatch):
        session_dir = tmp_path / "session-project"
        session_dir.mkdir()
        checkout = tmp_path / "ticket-checkout"
        local = checkout / ".booley_project"
        local.mkdir(parents=True)
        monkeypatch.setenv("BOOLEY_PROJECT_DIR", str(session_dir))
        assert resolve_project_dir() == session_dir.resolve()  # pre-warm global cache

        assert resolve_checkout_project_dir(checkout) == local.resolve()

    def test_checkout_local_config_overrides_warmed_global_cache(self, tmp_path, monkeypatch):
        session_dir = tmp_path / "session-project"
        session_dir.mkdir()
        checkout = tmp_path / "ticket-checkout"
        custom = checkout / "control"
        custom.mkdir(parents=True)
        (checkout / "booley.toml").write_text('[project]\ndir = "control"\n')
        monkeypatch.setenv("BOOLEY_PROJECT_DIR", str(session_dir))
        assert resolve_project_dir() == session_dir.resolve()

        assert resolve_checkout_project_dir(checkout) == custom.resolve()
        assert checkout_project_dir_relative_to(checkout) == Path("control")

    def test_checkout_relative_project_dir_rejects_external_path(self, tmp_path, monkeypatch):
        checkout = tmp_path / "ticket-checkout"
        checkout.mkdir()
        external = tmp_path / "external"
        external.mkdir()
        (checkout / "booley.toml").write_text(
            f'[project]\ndir = "{external.as_posix()}"\n',
            encoding="utf-8",
        )
        monkeypatch.delenv("BOOLEY_PROJECT_DIR", raising=False)

        with pytest.raises(ValueError, match="outside checkout"):
            checkout_project_dir_relative_to(checkout)


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


class TestSourceCheckoutRefusal:
    def _source(self, tmp_path: Path) -> Path:
        root = tmp_path / "booley-source"
        root.mkdir()
        (root / "pyproject.toml").write_text(
            "[tool.booley]\nsource_checkout = true\n",
            encoding="utf-8",
        )
        return root

    def test_stale_state_does_not_turn_source_into_project(self, tmp_path, monkeypatch):
        root = self._source(tmp_path)
        (root / ".booley_project").mkdir()
        external = tmp_path / "external-state"
        external.mkdir()
        monkeypatch.setenv("BOOLEY_PROJECT_DIR", str(external))

        with pytest.raises(SourceCheckoutProjectError, match="cannot be initialized"):
            resolve_project_dir(start=root)
        with pytest.raises(SourceCheckoutProjectError, match="cannot be initialized"):
            resolve_checkout_project_dir(root)

    def test_implicit_cwd_can_select_an_external_project(self, tmp_path, monkeypatch):
        root = self._source(tmp_path)
        external = tmp_path / "external-state"
        external.mkdir()
        monkeypatch.chdir(root)
        monkeypatch.setenv("BOOLEY_PROJECT_DIR", str(external))

        assert resolve_project_dir() == external.resolve()

    def test_env_cannot_select_state_inside_source(self, tmp_path, monkeypatch):
        root = self._source(tmp_path)
        stale = root / ".booley_project"
        stale.mkdir()
        monkeypatch.chdir(root)
        monkeypatch.setenv("BOOLEY_PROJECT_DIR", str(stale))

        with pytest.raises(SourceCheckoutProjectError, match="cannot be initialized"):
            resolve_project_dir()


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
