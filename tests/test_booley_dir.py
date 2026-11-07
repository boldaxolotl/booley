"""Tests for booley_dir: 4-step Booley Directory discovery."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from booley.runtime.booley_dir import (
    BooleyDirOutsideCheckoutError,
    booley_dir_for_init,
    checkout_booley_dir_relative_to,
    checkout_runtime_dir,
    own_booley_dir,
    own_booley_dir_relative,
    reset_cache,
    resolve_booley_dir,
    resolve_checkout_booley_dir,
)
from booley.runtime.checkout_role import SourceCheckoutProjectError
from tests.booley_checkouts_support import (
    commit_all,
    init_repo,
    isolate_git,
    main_project,
    paired_checkout,
    snapshot,
)
from tests.conftest import symlink_or_skip


@pytest.fixture(autouse=True)
def _clear_cache(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """Reset the module-level cache before and after each test."""
    reset_cache()
    monkeypatch.chdir(tmp_path)
    yield
    reset_cache()


class TestProjectDirForInit:
    def test_uses_selected_checkout_despite_ancestor_env_and_cache(self, tmp_path, monkeypatch):
        ancestor = tmp_path / ".booley"
        ancestor.mkdir()
        child = tmp_path / "child"
        child.mkdir()
        monkeypatch.setenv("BOOLEY_DIR", str(ancestor))
        assert resolve_booley_dir() == ancestor.resolve()

        assert booley_dir_for_init(child) == child.resolve() / ".booley"

    def test_rejects_booley_source_checkout(self, tmp_path):
        root = tmp_path / "booley-source"
        (root / "src" / "booley").mkdir(parents=True)
        (root / "src" / "booley" / "__init__.py").write_text("", encoding="utf-8")
        (root / "pyproject.toml").write_text(
            "[tool.booley]\nsource_checkout = true\n",
            encoding="utf-8",
        )

        with pytest.raises(SourceCheckoutProjectError, match="cannot be initialized"):
            booley_dir_for_init(root)


# ---------------------------------------------------------------------------
# Step 1: BOOLEY_DIR env var
# ---------------------------------------------------------------------------


class TestEnvVarOverride:
    def test_env_var_returns_path(self, tmp_path: Path, monkeypatch):
        d = tmp_path / "my_project"
        d.mkdir()
        monkeypatch.setenv("BOOLEY_DIR", str(d))
        result = resolve_booley_dir()
        assert result == d.resolve()

    def test_env_var_nonexistent_warns(self, tmp_path: Path, monkeypatch):
        fake = tmp_path / "does_not_exist"
        monkeypatch.setenv("BOOLEY_DIR", str(fake))
        with pytest.warns(UserWarning, match="does not exist"):
            result = resolve_booley_dir()
        assert result == fake.resolve()

    def test_env_var_takes_precedence_over_sibling(self, tmp_path: Path, monkeypatch):
        """Env var should win even if sibling .booley/ exists."""
        env_dir = tmp_path / "env_project"
        env_dir.mkdir()
        monkeypatch.setenv("BOOLEY_DIR", str(env_dir))
        # Also create a sibling that would match step 3
        sibling = tmp_path / ".booley"
        sibling.mkdir()
        (sibling / "booley.toml").write_text("# Project\n")
        result = resolve_booley_dir()
        assert result == env_dir.resolve()

    def test_checkout_local_snapshot_overrides_session_global_dir(self, tmp_path, monkeypatch):
        session_dir = tmp_path / "session-project"
        session_dir.mkdir()
        checkout = tmp_path / "ticket-checkout"
        local = checkout / ".booley"
        local.mkdir(parents=True)
        monkeypatch.setenv("BOOLEY_DIR", str(session_dir))
        assert resolve_booley_dir() == session_dir.resolve()  # pre-warm global cache

        assert resolve_checkout_booley_dir(checkout) == local.resolve()

    def test_checkout_runtime_dir_ignores_session_global_dir(self, tmp_path, monkeypatch):
        selected = tmp_path / "selected"
        selected_project = selected / ".booley"
        selected_project.mkdir(parents=True)
        global_project = tmp_path / "global" / ".booley"
        global_project.mkdir(parents=True)
        monkeypatch.setenv("BOOLEY_DIR", str(global_project))

        assert checkout_runtime_dir(selected) == selected_project / ".runtime"

    def test_checkout_local_config_overrides_warmed_global_cache(self, tmp_path, monkeypatch):
        session_dir = tmp_path / "session-project"
        session_dir.mkdir()
        checkout = tmp_path / "ticket-checkout"
        custom = checkout / "control"
        custom.mkdir(parents=True)
        (checkout / "booley.toml").write_text('[project]\ndir = "control"\n')
        monkeypatch.setenv("BOOLEY_DIR", str(session_dir))
        assert resolve_booley_dir() == session_dir.resolve()

        assert resolve_checkout_booley_dir(checkout) == custom.resolve()
        assert checkout_booley_dir_relative_to(checkout) == Path("control")

    def test_checkout_relative_booley_dir_names_a_symlinked_entry(self, tmp_path, monkeypatch):
        checkout = tmp_path / "checkout"
        checkout.mkdir()
        external = tmp_path / "external"
        external.mkdir()
        symlink_or_skip(checkout / ".booley", external, target_is_directory=True)
        monkeypatch.delenv("BOOLEY_DIR", raising=False)

        assert checkout_booley_dir_relative_to(checkout) == Path(".booley")

    def test_checkout_relative_booley_dir_rejects_external_path(self, tmp_path, monkeypatch):
        checkout = tmp_path / "ticket-checkout"
        checkout.mkdir()
        external = tmp_path / "external"
        external.mkdir()
        (checkout / "booley.toml").write_text(
            f'[project]\ndir = "{external.as_posix()}"\n',
            encoding="utf-8",
        )
        monkeypatch.delenv("BOOLEY_DIR", raising=False)

        with pytest.raises(ValueError, match="outside checkout"):
            checkout_booley_dir_relative_to(checkout)


# ---------------------------------------------------------------------------
# Step 2: booley.toml [project] dir
# ---------------------------------------------------------------------------


class TestBooleyToml:
    def test_absolute_dir_in_toml(self, tmp_path: Path, monkeypatch):
        monkeypatch.delenv("BOOLEY_DIR", raising=False)
        project_d = tmp_path / "custom_project"
        project_d.mkdir()

        # booley.toml lives in the walk-up path
        toml_path = tmp_path / "booley.toml"
        toml_path.write_text(
            f'[project]\ndir = "{project_d.as_posix()}"',
            encoding="utf-8",
        )

        result = resolve_booley_dir(start=tmp_path)
        assert result == project_d

    def test_relative_dir_in_toml(self, tmp_path: Path, monkeypatch):
        monkeypatch.delenv("BOOLEY_DIR", raising=False)
        subdir = tmp_path / "sub"
        subdir.mkdir()
        project_d = tmp_path / "rel_proj"
        project_d.mkdir()

        toml_path = tmp_path / "booley.toml"
        toml_path.write_text('[project]\ndir = "rel_proj"', encoding="utf-8")

        result = resolve_booley_dir(start=subdir)
        assert result == project_d.resolve()

    def test_malformed_toml_falls_through(self, tmp_path: Path, monkeypatch):
        """Broken booley.toml should fall through to step 3."""
        monkeypatch.delenv("BOOLEY_DIR", raising=False)
        (tmp_path / "booley.toml").write_text("INVALID{{{", encoding="utf-8")
        sibling = tmp_path / ".booley"
        sibling.mkdir()
        (sibling / "booley.toml").write_text("# Project\n")

        result = resolve_booley_dir(start=tmp_path)
        assert result == sibling


# ---------------------------------------------------------------------------
# Step 3: Walk-up .booley/
# ---------------------------------------------------------------------------


class TestWalkUpConvention:
    def test_finds_booley_directory(self, tmp_path: Path, monkeypatch):
        monkeypatch.delenv("BOOLEY_DIR", raising=False)
        booley_dir = tmp_path / ".booley"
        booley_dir.mkdir()
        (booley_dir / "booley.toml").write_text("# Project\n")
        # Start from a subdirectory
        subdir = tmp_path / "sub" / "deep"
        subdir.mkdir(parents=True)

        result = resolve_booley_dir(start=subdir)
        assert result == booley_dir


# ---------------------------------------------------------------------------
# Step 4: Not found
# ---------------------------------------------------------------------------


class TestNotFound:
    def test_raises_when_nothing_found(self, tmp_path: Path, monkeypatch):
        monkeypatch.delenv("BOOLEY_DIR", raising=False)
        # Empty tmp_path — no toml, no .booley/
        with pytest.raises(FileNotFoundError, match="booley init"):
            resolve_booley_dir(start=tmp_path)


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
        (root / ".booley").mkdir()
        external = tmp_path / "external-state"
        external.mkdir()
        monkeypatch.setenv("BOOLEY_DIR", str(external))

        with pytest.raises(SourceCheckoutProjectError, match="cannot be initialized"):
            resolve_booley_dir(start=root)
        with pytest.raises(SourceCheckoutProjectError, match="cannot be initialized"):
            resolve_checkout_booley_dir(root)

    def test_implicit_cwd_can_select_an_external_project(self, tmp_path, monkeypatch):
        root = self._source(tmp_path)
        external = tmp_path / "external-state"
        external.mkdir()
        monkeypatch.chdir(root)
        monkeypatch.setenv("BOOLEY_DIR", str(external))

        assert resolve_booley_dir() == external.resolve()

    def test_env_cannot_select_state_inside_source(self, tmp_path, monkeypatch):
        root = self._source(tmp_path)
        stale = root / ".booley"
        stale.mkdir()
        monkeypatch.chdir(root)
        monkeypatch.setenv("BOOLEY_DIR", str(stale))

        with pytest.raises(SourceCheckoutProjectError, match="cannot be initialized"):
            resolve_booley_dir()


# ---------------------------------------------------------------------------
# Caching
# ---------------------------------------------------------------------------


class TestCaching:
    def test_cache_returns_same_value(self, tmp_path: Path, monkeypatch):
        d = tmp_path / "cached_proj"
        d.mkdir()
        monkeypatch.setenv("BOOLEY_DIR", str(d))
        r1 = resolve_booley_dir()
        r2 = resolve_booley_dir()
        assert r1 == r2
        assert r1 is r2  # same object from cache

    def test_reset_cache_clears(self, tmp_path: Path, monkeypatch):
        d1 = tmp_path / "proj1"
        d1.mkdir()
        monkeypatch.setenv("BOOLEY_DIR", str(d1))
        r1 = resolve_booley_dir()

        d2 = tmp_path / "proj2"
        d2.mkdir()
        reset_cache()
        monkeypatch.setenv("BOOLEY_DIR", str(d2))
        r2 = resolve_booley_dir()

        assert r1 != r2


class TestContainment:
    def test_nonlexical_directory_identity(self, tmp_path, monkeypatch):
        from booley.runtime import booley_dir

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
        assert booley_dir.contains(candidate, booley_dir=root) == root / "inputs" / "file"
        assert calls
        assert (
            booley_dir.contains(root / "inputs" / "file", booley_dir=alias)
            == alias / "inputs" / "file"
        )

    def test_rebased_escape_is_denied(self, tmp_path, monkeypatch):
        from booley.runtime import booley_dir

        root, alias, outside = (tmp_path / name for name in ("root", "alias", "outside"))
        for directory in (root, alias, outside):
            directory.mkdir()
        (root / "escape").symlink_to(outside, target_is_directory=True)
        (alias / "escape").mkdir()
        original = Path.samefile
        monkeypatch.setattr(
            Path, "samefile", lambda p, q: {p, Path(q)} == {root, alias} or original(p, q)
        )
        assert booley_dir.contains(alias / "escape" / "new", booley_dir=root) is None

    def test_contract_matrix(self, tmp_path, monkeypatch):
        from booley.runtime.booley_dir import contains

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
            assert contains(candidate, booley_dir=root) == expected
        assert contains(alias / "directory" / "file", booley_dir=alias).samefile(inside)
        monkeypatch.chdir(root)
        assert contains("directory/file", booley_dir=root) == inside
        monkeypatch.setenv("BOOLEY_DIR", str(root))
        assert contains(inside) == inside
        assert contains(inside, booley_dir=tmp_path / "absent") is None

    def test_errors(self, tmp_path, monkeypatch):
        from booley.runtime import booley_dir

        root = tmp_path / "root"
        root.mkdir()
        with pytest.raises(ValueError):
            booley_dir.contains("bad\0path", booley_dir=root)

        def resolver():
            raise ValueError("selection failed")

        monkeypatch.setattr(booley_dir, "resolve_booley_dir", resolver)
        with pytest.raises(ValueError, match="selection failed"):
            booley_dir.contains(root)
        original = Path.resolve

        def resolve(path, *args, **kwargs):
            if path == root / "denied":
                raise PermissionError("denied")
            return original(path, *args, **kwargs)

        monkeypatch.setattr(Path, "resolve", resolve)
        assert booley_dir.contains(root / "denied", booley_dir=root) is None
        (root / "loop").symlink_to(root / "loop")
        assert booley_dir.contains(root / "loop", booley_dir=root) is None
        monkeypatch.setattr(
            Path, "samefile", lambda *args: (_ for _ in ()).throw(PermissionError("denied"))
        )
        assert booley_dir.contains(root, booley_dir=root) is None

    def test_inaccessible_candidate_stat(self, tmp_path, monkeypatch):
        from booley.runtime.booley_dir import contains

        root = tmp_path / "root"
        root.mkdir()
        candidate = root / "denied"
        original = Path.stat

        def stat(path, *args, **kwargs):
            if path == candidate:
                raise PermissionError("denied")
            return original(path, *args, **kwargs)

        monkeypatch.setattr(Path, "stat", stat)
        assert contains(candidate, booley_dir=root) is None

    def test_rebased_alias_child_symlink(self, tmp_path, monkeypatch):
        from booley.runtime.booley_dir import contains

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
        result = contains(alias / "input", booley_dir=root)
        assert result == root / "target"
        assert result.relative_to(root) == Path("target")

    def test_final_authority_rebase_escape_is_denied(self, tmp_path, monkeypatch):
        from booley.runtime.booley_dir import contains

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
        assert contains(alias / "input", booley_dir=root) is None


def test_containment_final_authority_internal_symlink(tmp_path, monkeypatch):
    from booley.runtime.booley_dir import contains

    root, alias = tmp_path / "root", tmp_path / "alias"
    for directory in (root, alias):
        directory.mkdir()
    (alias / "input").mkdir()
    (alias / "target").mkdir()
    (root / "real").mkdir()
    (root / "input").symlink_to(alias / "target", target_is_directory=True)
    (root / "target").symlink_to(root / "real", target_is_directory=True)
    original = Path.samefile
    monkeypatch.setattr(
        Path, "samefile", lambda p, q: {p, Path(q)} == {root, alias} or original(p, q)
    )
    assert contains(alias / "input", booley_dir=root) == root / "real"


@pytest.mark.parametrize("selection", ["environment", "toml", "directory"])
def test_observational_resolution_does_not_warm_cache(tmp_path, monkeypatch, selection):
    from booley.core import booley_dir

    monkeypatch.delenv("BOOLEY_DIR", raising=False)
    selected = tmp_path / "selected"
    directory = selected / ("custom" if selection == "toml" else ".booley")
    directory.mkdir(parents=True)
    if selection == "environment":
        monkeypatch.setenv("BOOLEY_DIR", str(directory))
    elif selection == "toml":
        (selected / "booley.toml").write_text("[project]\ndir='custom'\n")
    assert resolve_booley_dir(selected, cache_result=False) == directory
    assert booley_dir._cache is None
    assert resolve_booley_dir(selected) == directory
    assert booley_dir._cache == directory


def test_observational_resolution_preserves_existing_selection(tmp_path, monkeypatch):
    from booley.core import booley_dir

    monkeypatch.delenv("BOOLEY_DIR", raising=False)
    first = tmp_path / "first"
    second = tmp_path / "second"
    for root in (first, second):
        (root / ".booley").mkdir(parents=True)
    expected = resolve_booley_dir(first)
    assert resolve_booley_dir(second, cache_result=False) == expected
    assert booley_dir._cache == expected


def _crossing_case(tmp_path: Path, case: str) -> tuple[Path, Path | None]:
    """Build one layout for the crossed-entry helper; return (path, expected entry)."""
    project = tmp_path / "project"
    (project / ".git").mkdir(parents=True)
    external = tmp_path / "external-booley"
    (external / "worktrees" / "goal-x" / ".booley").mkdir(parents=True)
    entry = project / ".booley"
    link = {"misnamed": project / ".booley-data", "nested": project / "sub" / ".booley"}.get(
        case, entry
    )
    if case in {"plain", "below"}:
        entry.mkdir()
    else:
        link.parent.mkdir(exist_ok=True)
        symlink_or_skip(link, external, target_is_directory=True)
    if case == "below":
        symlink_or_skip(entry / "flow-reports", external, target_is_directory=True)
    if case == "above":
        symlink_or_skip(tmp_path / "alias", project, target_is_directory=True)
    return {
        "entry": (entry / "flow-reports/sim/1", entry),
        "worktree": (entry / "worktrees/goal-x/.booley/cw", entry),
        "plain": (entry / "flow-reports", None),
        "below": (entry / "flow-reports/sim", None),
        "above": (tmp_path / "alias/.booley/flow-reports", None),
        "misnamed": (link / "flow-reports", None),
        "nested": (link / "cw", None),
    }[case]


@pytest.mark.parametrize(
    "case", ["entry", "worktree", "plain", "below", "above", "misnamed", "nested"]
)
def test_crossed_booley_dir_entry_names_only_the_entry(tmp_path, monkeypatch, case):
    """Only the exact Booley Directory entry of a Git checkout counts as crossed (#2192)."""
    from booley.runtime.booley_dir import crossed_booley_dir_entry

    monkeypatch.delenv("BOOLEY_DIR", raising=False)
    path, expected = _crossing_case(tmp_path, case)

    assert crossed_booley_dir_entry(path) == expected


# ---------------------------------------------------------------------------
# Common (Main) Booley Directory resolver (#2600, ADR 0082)
# ---------------------------------------------------------------------------


def _common(start: Path | None = None) -> Path:
    """Call the Common resolver, importing it lazily so each test fails in its body."""
    from booley.runtime.booley_dir import resolve_common_booley_dir

    return resolve_common_booley_dir(start)


class TestCommonBooleyDir:
    def test_from_main_checkout(self, tmp_path, monkeypatch):
        isolate_git(tmp_path, monkeypatch)
        main = main_project(tmp_path)

        assert _common(main) == main / ".booley"
        reset_cache()
        monkeypatch.setenv("BOOLEY_DIR", str(main / ".booley"))
        assert _common() == main / ".booley"

    def test_from_linked_worktree_snapshot(self, tmp_path, monkeypatch):
        isolate_git(tmp_path, monkeypatch)
        main = main_project(tmp_path)
        worktree = snapshot(main)
        (worktree / "sub").mkdir()

        assert resolve_booley_dir(worktree / "sub") == worktree / ".booley"
        reset_cache()
        assert _common(worktree / "sub") == main / ".booley"
        reset_cache()
        monkeypatch.setenv("BOOLEY_DIR", str(worktree / ".booley"))
        assert _common() == main / ".booley"
        reset_cache()
        monkeypatch.setenv("BOOLEY_DIR", str(main / ".booley"))
        assert _common() == main / ".booley"

    def test_from_paired_checkout(self, tmp_path, monkeypatch):
        isolate_git(tmp_path, monkeypatch)
        main = main_project(tmp_path)
        worktree = paired_checkout(main)

        assert resolve_booley_dir(worktree) == worktree / ".booley"
        reset_cache()
        assert _common(worktree) == main / ".booley"
        reset_cache()
        monkeypatch.setenv("BOOLEY_DIR", str(worktree / ".booley"))
        assert _common() == main / ".booley"
        reset_cache()
        monkeypatch.setenv("BOOLEY_DIR", str(main / ".booley"))
        assert _common() == main / ".booley"

    def test_honors_relocated_project_dir(self, tmp_path, monkeypatch):
        isolate_git(tmp_path, monkeypatch)
        main = main_project(tmp_path, relative="hw/booley", toml='[project]\ndir = "hw/booley"\n')
        worktree = snapshot(main, relative="hw/booley")

        assert _common(main) == main / "hw" / "booley"
        reset_cache()
        assert _common(worktree) == main / "hw" / "booley"

    def test_outside_git_keeps_selection(self, tmp_path, monkeypatch):
        isolate_git(tmp_path, monkeypatch)
        plain = tmp_path / "plain"
        (plain / ".booley").mkdir(parents=True)
        (plain / ".booley" / "booley.toml").write_text("", encoding="utf-8")

        assert _common(plain) == plain / ".booley"
        reset_cache()
        monkeypatch.setenv("BOOLEY_DIR", str(plain / ".booley"))
        assert _common() == plain / ".booley"

    def test_unreadable_git_common_dir_keeps_selection(self, tmp_path, monkeypatch):
        isolate_git(tmp_path, monkeypatch)
        stale = tmp_path / "stale"
        (stale / ".booley").mkdir(parents=True)
        (stale / ".booley" / "booley.toml").write_text("", encoding="utf-8")
        (stale / ".git").write_text(
            f"gitdir: {tmp_path / 'gone' / 'worktrees' / 'x'}\n", encoding="utf-8"
        )

        assert _common(stale) == stale / ".booley"

    def test_stray_booley_dir_is_not_remapped(self, tmp_path, monkeypatch):
        isolate_git(tmp_path, monkeypatch)
        main = main_project(tmp_path)
        worktree = snapshot(main)
        stray = worktree / "elsewhere"
        stray.mkdir()
        (stray / "booley.toml").write_text("", encoding="utf-8")
        monkeypatch.setenv("BOOLEY_DIR", str(stray))

        assert _common(worktree) == stray

    def test_main_without_booley_dir_keeps_selection(self, tmp_path, monkeypatch):
        isolate_git(tmp_path, monkeypatch)
        from tests.project_runtime_git_support import git

        main = tmp_path / "main"
        init_repo(main)
        (main / "README.md").write_text("fixture\n", encoding="utf-8")
        commit_all(main)
        linked = tmp_path / "linked"
        git(main, "worktree", "add", "-q", "--detach", str(linked))
        (linked / ".booley").mkdir()
        (linked / ".booley" / "booley.toml").write_text("", encoding="utf-8")

        assert _common(linked) == linked / ".booley"

    def test_bare_repository_worktree_keeps_selection(self, tmp_path, monkeypatch):
        isolate_git(tmp_path, monkeypatch)
        from tests.project_runtime_git_support import git

        source = main_project(tmp_path)
        bare = tmp_path / "bare.git"
        git(tmp_path, "clone", "-q", "--bare", str(source), str(bare))
        worktree = tmp_path / "bare-wt"
        git(bare, "worktree", "add", "-q", "--detach", str(worktree))
        (worktree / ".booley").mkdir()
        (worktree / ".booley" / "booley.toml").write_text("", encoding="utf-8")

        assert _common(worktree) == worktree / ".booley"


class TestCheckoutOwnBooleyDir:
    """``own_booley_dir`` reads one checkout alone, lexically, and never the ambient selection."""

    @staticmethod
    def _configure(root: Path, text: str) -> None:
        root.mkdir(parents=True, exist_ok=True)
        (root / "booley.toml").write_bytes(text.encode("utf-8"))

    def test_relative_dir_stays_lexical(self, tmp_path: Path) -> None:
        link = tmp_path / "link"
        real = tmp_path / "real"
        real.mkdir()
        symlink_or_skip(link, real, target_is_directory=True)
        self._configure(real, '[project]\ndir = "a/../state/sub"\n')

        assert own_booley_dir(link) == link / "state" / "sub"
        assert own_booley_dir_relative(link).as_posix() == "state/sub"

    def test_ignores_environment_cache_and_ancestors(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        root = tmp_path / "root"
        self._configure(tmp_path, '[project]\ndir = "ancestor"\n')
        root.mkdir()
        elsewhere = tmp_path / "elsewhere"
        elsewhere.mkdir()
        monkeypatch.setenv("BOOLEY_DIR", str(elsewhere))
        (tmp_path / ".booley").mkdir()
        resolve_booley_dir(tmp_path)  # warms the process cache

        assert own_booley_dir(root) == root / ".booley"
        self._configure(root, '[project]\ndir = "state"\n')
        assert own_booley_dir(root) == root / "state"

    @pytest.mark.parametrize("text", ["INVALID{{{", '[project]\ndir = ""\n'])
    def test_unusable_configuration_means_the_default(self, tmp_path: Path, text: str) -> None:
        self._configure(tmp_path, text)

        assert own_booley_dir(tmp_path) == tmp_path / ".booley"

    @pytest.mark.parametrize("kind", ["absolute", "escaping", "root"])
    def test_relative_form_refuses_a_directory_not_strictly_inside(
        self, tmp_path: Path, kind: str
    ) -> None:
        root = tmp_path / "root"
        value = {
            "absolute": (tmp_path / "external").as_posix(),
            "escaping": "../external",
            "root": ".",
        }[kind]
        self._configure(root, f'[project]\ndir = "{value}"\n')

        with pytest.raises(BooleyDirOutsideCheckoutError, match=r"relative \[project\]\.dir"):
            own_booley_dir_relative(root)


def test_own_booley_dir_repo_path_relocated_default_and_outside(tmp_path: Path) -> None:
    """Repository path: relocated spelling, default ``.booley``, absolute falls back."""
    from booley.runtime.booley_dir import own_booley_dir_repo_path

    relocated = tmp_path / "relocated"
    relocated.mkdir()
    (relocated / "booley.toml").write_bytes(b'[project]\ndir = "tools/booley"\n')
    default = tmp_path / "default"
    default.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "booley.toml").write_bytes(
        f'[project]\ndir = "{(tmp_path / "external").as_posix()}"\n'.encode()
    )

    assert own_booley_dir_repo_path(relocated).as_posix() == "tools/booley"
    assert own_booley_dir_repo_path(default).as_posix() == ".booley"
    assert own_booley_dir_repo_path(outside).as_posix() == ".booley"
