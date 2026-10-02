"""Tests for project_dir: 4-step project directory discovery."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from booley.runtime.checkout_role import SourceCheckoutProjectError
from booley.runtime.project_dir import (
    checkout_project_dir_relative_to,
    checkout_runtime_dir,
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

    def test_detect_project_root_recovers_control_plane_from_runtime_ticket(
        self, tmp_path, monkeypatch
    ):
        control_root = tmp_path / "control"
        runtime_ticket = (
            control_root / ".booley_project" / "tickets" / "logs" / "ticket-a" / "ticket.md"
        )
        misleading_worktree = control_root / ".booley_project" / "worktrees" / "ticket-a"
        monkeypatch.delenv("BOOLEY_CONTROL_PROJECT_ROOT", raising=False)
        monkeypatch.setenv("BOOLEY_TICKET_FILE", str(runtime_ticket))
        monkeypatch.setenv("PROJECT_ROOT", str(misleading_worktree))
        monkeypatch.setenv("BOOLEY_PROJECT_DIR", str(misleading_worktree / ".booley_project"))

        assert detect_project_root() == control_root

    def test_detect_project_root_rejects_unrelated_tickets_path(self, tmp_path, monkeypatch):
        fallback = tmp_path / "fallback"
        unrelated = tmp_path / "example" / "tickets" / "logs" / "ticket-a" / "ticket.md"
        monkeypatch.delenv("BOOLEY_CONTROL_PROJECT_ROOT", raising=False)
        monkeypatch.setenv("BOOLEY_TICKET_FILE", str(unrelated))
        monkeypatch.setenv("PROJECT_ROOT", str(fallback))

        assert detect_project_root() == fallback

    def test_checkout_local_snapshot_overrides_session_global_dir(self, tmp_path, monkeypatch):
        session_dir = tmp_path / "session-project"
        session_dir.mkdir()
        checkout = tmp_path / "ticket-checkout"
        local = checkout / ".booley_project"
        local.mkdir(parents=True)
        monkeypatch.setenv("BOOLEY_PROJECT_DIR", str(session_dir))
        assert resolve_project_dir() == session_dir.resolve()  # pre-warm global cache

        assert resolve_checkout_project_dir(checkout) == local.resolve()

    def test_checkout_runtime_dir_ignores_session_global_dir(self, tmp_path, monkeypatch):
        selected = tmp_path / "selected"
        selected_project = selected / ".booley_project"
        selected_project.mkdir(parents=True)
        global_project = tmp_path / "global" / ".booley_project"
        global_project.mkdir(parents=True)
        monkeypatch.setenv("BOOLEY_PROJECT_DIR", str(global_project))

        assert checkout_runtime_dir(selected) == selected_project / ".runtime"

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


class TestContainment:
    def test_nonlexical_directory_identity(self, tmp_path, monkeypatch):
        from booley.runtime import project_dir

        root, alias = tmp_path / "selected", tmp_path / "alias"
        for directory in (root, alias):
            (directory / "inputs").mkdir(parents=True)
            (directory / "inputs" / "file").write_text("data")
        original = Path.samefile
        calls = []

        def samefile(path, other):
            if {path, Path(other)} == {root, alias}:
                calls.append(path)
                return True
            return original(path, other)

        monkeypatch.setattr(Path, "samefile", samefile)
        candidate = alias / "inputs" / "file"
        assert not candidate.resolve().is_relative_to(root.resolve())
        assert project_dir.contains(candidate, project_dir=root) == root / "inputs" / "file"
        assert calls
        assert (
            project_dir.contains(root / "inputs" / "file", project_dir=alias)
            == alias / "inputs" / "file"
        )

    def test_rebased_escape_is_denied(self, tmp_path, monkeypatch):
        from booley.runtime import project_dir

        root, alias, outside = (tmp_path / name for name in ("root", "alias", "outside"))
        for directory in (root, alias, outside):
            directory.mkdir()
        (root / "escape").symlink_to(outside, target_is_directory=True)
        (alias / "escape").mkdir()
        original = Path.samefile
        monkeypatch.setattr(
            Path, "samefile", lambda p, q: {p, Path(q)} == {root, alias} or original(p, q)
        )
        assert project_dir.contains(alias / "escape" / "new", project_dir=root) is None

    def test_contract_matrix(self, tmp_path, monkeypatch):
        from booley.runtime.project_dir import contains

        root = tmp_path / "root"
        root.mkdir()
        inside = root / "directory" / "file"
        inside.parent.mkdir()
        inside.write_text("data")
        outside = tmp_path / "outside"
        outside.mkdir()
        alias = tmp_path / "alias"
        alias.symlink_to(root, target_is_directory=True)
        (root / "escape").symlink_to(outside, target_is_directory=True)
        (root / "internal").symlink_to(inside.parent, target_is_directory=True)
        hardlink = outside / "hardlink"
        hardlink.hardlink_to(inside)
        for candidate, expected in (
            (root, root),
            (inside.parent, inside.parent),
            (inside, inside),
            (alias / "directory" / "file", inside),
            (root / "internal" / "file", inside),
            (root / "missing" / "child", root / "missing" / "child"),
            (outside, None),
            (hardlink, None),
            (root / "escape" / "new", None),
            (alias / "escape" / "new", None),
            (root / ".." / "outside", None),
            (tmp_path / "root-extra" / "file", None),
        ):
            assert contains(candidate, project_dir=root) == expected
        assert contains(alias / "directory" / "file", project_dir=alias).samefile(inside)
        monkeypatch.chdir(root)
        assert contains("directory/file", project_dir=root) == inside
        monkeypatch.setenv("BOOLEY_PROJECT_DIR", str(root))
        assert contains(inside) == inside
        assert contains(inside, project_dir=tmp_path / "absent") is None

    def test_errors(self, tmp_path, monkeypatch):
        from booley.runtime import project_dir

        root = tmp_path / "root"
        root.mkdir()
        with pytest.raises(ValueError):
            project_dir.contains("bad\0path", project_dir=root)

        def resolver():
            raise ValueError("selection failed")

        monkeypatch.setattr(project_dir, "resolve_project_dir", resolver)
        with pytest.raises(ValueError, match="selection failed"):
            project_dir.contains(root)
        original = Path.resolve

        def resolve(path, *args, **kwargs):
            if path == root / "denied":
                raise PermissionError("denied")
            return original(path, *args, **kwargs)

        monkeypatch.setattr(Path, "resolve", resolve)
        assert project_dir.contains(root / "denied", project_dir=root) is None
        (root / "loop").symlink_to(root / "loop")
        assert project_dir.contains(root / "loop", project_dir=root) is None
        monkeypatch.setattr(
            Path, "samefile", lambda *args: (_ for _ in ()).throw(PermissionError("denied"))
        )
        assert project_dir.contains(root, project_dir=root) is None

    def test_inaccessible_candidate_stat(self, tmp_path, monkeypatch):
        from booley.runtime.project_dir import contains

        root = tmp_path / "root"
        root.mkdir()
        candidate = root / "denied"
        original = Path.stat

        def stat(path, *args, **kwargs):
            if path == candidate:
                raise PermissionError("denied")
            return original(path, *args, **kwargs)

        monkeypatch.setattr(Path, "stat", stat)
        assert contains(candidate, project_dir=root) is None

    def test_rebased_alias_child_symlink(self, tmp_path, monkeypatch):
        from booley.runtime.project_dir import contains

        root, alias = tmp_path / "root", tmp_path / "alias"
        for directory in (root, alias):
            directory.mkdir()
            (directory / "target").mkdir()
        (alias / "input").mkdir()
        (root / "input").symlink_to(alias / "target", target_is_directory=True)
        original = Path.samefile
        monkeypatch.setattr(
            Path, "samefile", lambda p, q: {p, Path(q)} == {root, alias} or original(p, q)
        )
        result = contains(alias / "input", project_dir=root)
        assert result == root / "target"
        assert result.relative_to(root) == Path("target")

    def test_final_authority_rebase_escape_is_denied(self, tmp_path, monkeypatch):
        from booley.runtime.project_dir import contains

        root, alias, outside = (tmp_path / name for name in ("root", "alias", "outside"))
        for directory in (root, alias, outside):
            directory.mkdir()
        (alias / "input").mkdir()
        (alias / "target").mkdir()
        (root / "input").symlink_to(alias / "target", target_is_directory=True)
        (root / "target").symlink_to(outside, target_is_directory=True)
        original = Path.samefile
        monkeypatch.setattr(
            Path, "samefile", lambda p, q: {p, Path(q)} == {root, alias} or original(p, q)
        )
        assert contains(alias / "input", project_dir=root) is None
