"""Tests for booley_dir: 4-step Booley Directory discovery."""

from __future__ import annotations

from pathlib import Path

import pytest

from booley.core import booley_layout
from booley.runtime.booley_dir import (
    BooleyDirOutsideCheckoutError,
    booley_dir_for_init,
    checkout_booley_dir_relative_to,
    checkout_runtime_dir,
    contains,
    crossed_booley_dir_entry,
    own_booley_dir,
    own_booley_dir_relative,
    own_booley_dir_repo_path,
    reset_cache,
    resolve_booley_dir,
    resolve_checkout_booley_dir,
    resolve_common_booley_dir,
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
from tests.project_runtime_git_support import git


@pytest.fixture(autouse=True)
def _clear_cache(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """Reset the module-level cache before and after each test."""
    reset_cache()
    monkeypatch.chdir(tmp_path)
    yield
    reset_cache()


def write_config(root: Path, text: str) -> None:
    """Write ``root/booley.toml``, creating *root*."""
    root.mkdir(parents=True, exist_ok=True)
    (root / "booley.toml").write_bytes(text.encode("utf-8"))


@pytest.fixture
def source_checkout(tmp_path: Path) -> Path:
    """A Booley source checkout, which can never be a Project."""
    root = tmp_path / "booley-source"
    root.mkdir()
    (root / "pyproject.toml").write_text("[tool.booley]\nsource_checkout = true\n")
    return root


class TestProjectDirForInit:
    def test_uses_selected_checkout_despite_ancestor_env_and_cache(self, tmp_path, monkeypatch):
        ancestor = tmp_path / ".booley"
        ancestor.mkdir()
        child = tmp_path / "child"
        child.mkdir()
        monkeypatch.setenv("BOOLEY_DIR", str(ancestor))
        assert resolve_booley_dir() == ancestor.resolve()

        assert booley_dir_for_init(child) == child.resolve() / ".booley"

    def test_rejects_booley_source_checkout(self, source_checkout):
        with pytest.raises(SourceCheckoutProjectError, match="cannot be initialized"):
            booley_dir_for_init(source_checkout)


class TestEnvVarOverride:
    def test_env_var_nonexistent_warns(self, tmp_path: Path, monkeypatch):
        fake = tmp_path / "does_not_exist"
        monkeypatch.setenv("BOOLEY_DIR", str(fake))
        with pytest.warns(UserWarning, match="does not exist"):
            result = resolve_booley_dir()
        assert result == fake.resolve()

    def test_env_var_takes_precedence_over_sibling(self, tmp_path: Path, monkeypatch):
        env_dir = tmp_path / "env_project"
        env_dir.mkdir()
        monkeypatch.setenv("BOOLEY_DIR", str(env_dir))
        write_config(tmp_path / ".booley", "# Project\n")
        assert resolve_booley_dir() == env_dir.resolve()

    @pytest.mark.parametrize("configured", [False, True], ids=["default", "configured"])
    def test_checkout_local_selection_overrides_warmed_global_cache(
        self, tmp_path, monkeypatch, configured
    ):
        session_dir = tmp_path / "session-project"
        session_dir.mkdir()
        checkout = tmp_path / "ticket-checkout"
        local = checkout / ("control" if configured else ".booley")
        local.mkdir(parents=True)
        if configured:
            write_config(checkout, '[project]\ndir = "control"\n')
        monkeypatch.setenv("BOOLEY_DIR", str(session_dir))
        assert resolve_booley_dir() == session_dir.resolve()

        assert resolve_checkout_booley_dir(checkout) == local.resolve()
        assert checkout_booley_dir_relative_to(checkout) == local.relative_to(checkout)

    def test_checkout_runtime_dir_ignores_session_global_dir(self, tmp_path, monkeypatch):
        selected = tmp_path / "selected"
        selected_project = selected / ".booley"
        selected_project.mkdir(parents=True)
        global_project = tmp_path / "global" / ".booley"
        global_project.mkdir(parents=True)
        monkeypatch.setenv("BOOLEY_DIR", str(global_project))

        assert checkout_runtime_dir(selected) == selected_project / ".runtime"

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
        external = tmp_path / "external"
        external.mkdir()
        write_config(checkout, f'[project]\ndir = "{external.as_posix()}"\n')
        monkeypatch.delenv("BOOLEY_DIR", raising=False)

        with pytest.raises(ValueError, match="outside checkout"):
            checkout_booley_dir_relative_to(checkout)


class TestBooleyToml:
    @pytest.mark.parametrize("spelling", ["absolute", "relative"])
    def test_dir_in_walked_up_toml(self, tmp_path: Path, monkeypatch, spelling):
        monkeypatch.delenv("BOOLEY_DIR", raising=False)
        project_d = tmp_path / "custom_project"
        project_d.mkdir()
        start = tmp_path / "sub"
        start.mkdir()
        value = project_d.as_posix() if spelling == "absolute" else "custom_project"
        write_config(tmp_path, f'[project]\ndir = "{value}"')

        assert resolve_booley_dir(start=start) == project_d.resolve()

    def test_malformed_toml_falls_through(self, tmp_path: Path, monkeypatch):
        """Broken booley.toml should fall through to step 3."""
        monkeypatch.delenv("BOOLEY_DIR", raising=False)
        write_config(tmp_path, "INVALID{{{")
        sibling = tmp_path / ".booley"
        write_config(sibling, "# Project\n")

        assert resolve_booley_dir(start=tmp_path) == sibling


def test_walk_up_finds_booley_directory(tmp_path: Path, monkeypatch):
    monkeypatch.delenv("BOOLEY_DIR", raising=False)
    booley_dir = tmp_path / ".booley"
    write_config(booley_dir, "# Project\n")
    subdir = tmp_path / "sub" / "deep"
    subdir.mkdir(parents=True)

    assert resolve_booley_dir(start=subdir) == booley_dir


def test_raises_when_nothing_found(tmp_path: Path, monkeypatch):
    monkeypatch.delenv("BOOLEY_DIR", raising=False)
    with pytest.raises(FileNotFoundError, match="booley init"):
        resolve_booley_dir(start=tmp_path)


@pytest.mark.parametrize("leftover", ["ancestor-cache", "environment", "selection", "journal"])
def test_a_0_3_leftover_refuses_resolution(tmp_path, monkeypatch, leftover):
    monkeypatch.delenv("BOOLEY_DIR", raising=False)
    root = tmp_path / "rtl"
    (root / ".booley").mkdir(parents=True)
    start = root
    if leftover in {"ancestor-cache", "environment"}:
        assert resolve_booley_dir(root) == root.resolve() / ".booley"
    if leftover == "ancestor-cache":
        start = root / "child"
        (start / ".booley_project").mkdir(parents=True)
    if leftover == "environment":
        monkeypatch.setenv("BOOLEY_PROJECT_DIR", "")
    if leftover == "selection":
        monkeypatch.setenv("BOOLEY_DIR", str(root / ".booley_project"))
    if leftover == "journal":
        journal = booley_layout.layout_metadata(root) / "directory-migration.json"
        journal.parent.mkdir(parents=True)
        journal.write_bytes(b"{}")

    with pytest.raises(booley_layout.BooleyLayoutError, match=r"upgrade through Booley 0\.4\.x"):
        resolve_booley_dir(start)


class TestSourceCheckoutRefusal:
    def test_source_checkout_guard_precedes_unrelated_ancestor_layout(
        self, tmp_path, source_checkout
    ):
        (tmp_path / ".booley_project").mkdir()

        with pytest.raises(SourceCheckoutProjectError):
            resolve_booley_dir(source_checkout)

    def test_stale_state_does_not_turn_source_into_project(
        self, tmp_path, monkeypatch, source_checkout
    ):
        (source_checkout / ".booley").mkdir()
        external = tmp_path / "external-state"
        external.mkdir()
        monkeypatch.setenv("BOOLEY_DIR", str(external))

        with pytest.raises(SourceCheckoutProjectError, match="cannot be initialized"):
            resolve_booley_dir(start=source_checkout)
        with pytest.raises(SourceCheckoutProjectError, match="cannot be initialized"):
            resolve_checkout_booley_dir(source_checkout)

    def test_implicit_cwd_can_select_an_external_project(
        self, tmp_path, monkeypatch, source_checkout
    ):
        external = tmp_path / "external-state"
        external.mkdir()
        monkeypatch.chdir(source_checkout)
        monkeypatch.setenv("BOOLEY_DIR", str(external))

        assert resolve_booley_dir() == external.resolve()

    def test_env_cannot_select_state_inside_source(self, monkeypatch, source_checkout):
        stale = source_checkout / ".booley"
        stale.mkdir()
        monkeypatch.chdir(source_checkout)
        monkeypatch.setenv("BOOLEY_DIR", str(stale))

        with pytest.raises(SourceCheckoutProjectError, match="cannot be initialized"):
            resolve_booley_dir()


def alias_roots(monkeypatch: pytest.MonkeyPatch, root: Path, alias: Path) -> None:
    """Make *root* and *alias* one directory under two spellings, like two mounts of it."""
    original = Path.samefile
    monkeypatch.setattr(
        Path, "samefile", lambda p, q: {p, Path(q)} == {root, alias} or original(p, q)
    )


class TestContainment:
    def test_nonlexical_directory_identity(self, tmp_path, monkeypatch):
        root, alias = tmp_path / "selected", tmp_path / "alias"
        for directory in (root, alias):
            (directory / "inputs").mkdir(parents=True)
            (directory / "inputs" / "file").write_text("data")
        alias_roots(monkeypatch, root, alias)
        candidate = alias / "inputs" / "file"
        assert not candidate.resolve().is_relative_to(root.resolve())
        assert contains(candidate, booley_dir=root) == root / "inputs" / "file"
        assert contains(root / "inputs" / "file", booley_dir=alias) == alias / "inputs" / "file"

    def test_rebased_escape_is_denied(self, tmp_path, monkeypatch):
        root, alias, outside = (tmp_path / name for name in ("root", "alias", "outside"))
        for directory in (root, alias, outside):
            directory.mkdir()
        symlink_or_skip(root / "escape", outside, target_is_directory=True)
        (alias / "escape").mkdir()
        alias_roots(monkeypatch, root, alias)
        assert contains(alias / "escape" / "new", booley_dir=root) is None

    @pytest.mark.parametrize("target", ["directory", "escape", "internal"])
    def test_rebased_alias_child_symlink_takes_the_root_target(
        self, tmp_path, monkeypatch, target
    ):
        """``root/input`` links into the alias; the root's own ``target`` has the final say."""
        root, alias, outside = (tmp_path / name for name in ("root", "alias", "outside"))
        for directory in (root, alias / "input", alias / "target", outside, root / "real"):
            directory.mkdir(parents=True)
        symlink_or_skip(root / "input", alias / "target", target_is_directory=True)
        expected = {"directory": root / "target", "escape": None, "internal": root / "real"}
        if target == "directory":
            (root / "target").mkdir()
        else:
            link = outside if target == "escape" else root / "real"
            symlink_or_skip(root / "target", link, target_is_directory=True)
        alias_roots(monkeypatch, root, alias)

        assert contains(alias / "input", booley_dir=root) == expected[target]

    def test_contract_matrix(self, tmp_path, monkeypatch):
        root = tmp_path / "root"
        root.mkdir()
        inside = root / "directory" / "file"
        inside.parent.mkdir()
        inside.write_text("data")
        outside = tmp_path / "outside"
        outside.mkdir()
        alias = tmp_path / "alias"
        symlink_or_skip(alias, root, target_is_directory=True)
        symlink_or_skip(root / "escape", outside, target_is_directory=True)
        symlink_or_skip(root / "internal", inside.parent, target_is_directory=True)
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
        root = tmp_path / "root"
        root.mkdir()
        with pytest.raises(ValueError):
            contains("bad\0path", booley_dir=root)
        monkeypatch.delenv("BOOLEY_DIR", raising=False)
        with pytest.raises(FileNotFoundError, match="booley init"):
            contains(root)  # a failed selection propagates
        original = Path.resolve

        def resolve(path, *args, **kwargs):
            if path == root / "denied":
                raise PermissionError("denied")
            return original(path, *args, **kwargs)

        monkeypatch.setattr(Path, "resolve", resolve)
        assert contains(root / "denied", booley_dir=root) is None
        symlink_or_skip(root / "loop", root / "loop")
        assert contains(root / "loop", booley_dir=root) is None
        monkeypatch.setattr(
            Path, "samefile", lambda *args: (_ for _ in ()).throw(PermissionError("denied"))
        )
        assert contains(root, booley_dir=root) is None

    def test_inaccessible_candidate_stat(self, tmp_path, monkeypatch):
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


@pytest.mark.parametrize("selection", ["environment", "toml", "directory"])
def test_observational_resolution_does_not_warm_cache(tmp_path, monkeypatch, selection):
    """A lookup with ``cache_result=False`` leaves the next selection free (#1549)."""
    monkeypatch.delenv("BOOLEY_DIR", raising=False)
    selected = tmp_path / "selected"
    directory = selected / ("custom" if selection == "toml" else ".booley")
    directory.mkdir(parents=True)
    other = tmp_path / "other"
    (other / ".booley").mkdir(parents=True)
    if selection == "environment":
        monkeypatch.setenv("BOOLEY_DIR", str(directory))
    elif selection == "toml":
        write_config(selected, "[project]\ndir='custom'\n")
    assert resolve_booley_dir(selected, cache_result=False) == directory
    monkeypatch.delenv("BOOLEY_DIR", raising=False)

    assert resolve_booley_dir(other) == other / ".booley"
    assert resolve_booley_dir(selected) == other / ".booley"  # the caching lookup warmed it
    reset_cache()
    assert resolve_booley_dir(selected) == directory


def test_observational_resolution_preserves_existing_selection(tmp_path, monkeypatch):
    monkeypatch.delenv("BOOLEY_DIR", raising=False)
    first = tmp_path / "first"
    second = tmp_path / "second"
    for root in (first, second):
        (root / ".booley").mkdir(parents=True)
    expected = resolve_booley_dir(first)
    assert resolve_booley_dir(second, cache_result=False) == expected
    assert resolve_booley_dir(second) == expected


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
    monkeypatch.delenv("BOOLEY_DIR", raising=False)
    path, expected = _crossing_case(tmp_path, case)

    assert crossed_booley_dir_entry(path) == expected


def _mapped_checkout(tmp_path: Path, layout: str) -> tuple[Path, Path, Path]:
    """Return (start, the checkout's own Booley Directory, the Main Booley Directory)."""
    if layout == "relocated":
        main = main_project(tmp_path, relative="hw/booley", toml='[project]\ndir = "hw/booley"\n')
        worktree = snapshot(main, relative="hw/booley")
        return worktree, worktree / "hw" / "booley", main / "hw" / "booley"
    main = main_project(tmp_path)
    if layout == "snapshot":
        checkout = snapshot(main)
        (checkout / "sub").mkdir()
        return checkout / "sub", checkout / ".booley", main / ".booley"
    checkout = paired_checkout(main) if layout == "paired" else main
    return checkout, checkout / ".booley", main / ".booley"


@pytest.mark.parametrize("layout", ["main", "snapshot", "paired", "relocated"])
def test_common_booley_dir_maps_every_checkout_to_main(tmp_path, monkeypatch, layout):
    """The Common resolver reaches the Main Booley Directory from any checkout (#2600)."""
    isolate_git(tmp_path, monkeypatch)
    start, own, common = _mapped_checkout(tmp_path, layout)

    assert resolve_booley_dir(start) == own
    reset_cache()
    assert resolve_common_booley_dir(start) == common
    for selected in (own, common):
        reset_cache()
        monkeypatch.setenv("BOOLEY_DIR", str(selected))
        assert resolve_common_booley_dir() == common


def _unmapped_selection(tmp_path: Path, layout: str) -> tuple[Path, Path]:
    """Return (start, selection) for a layout with no Main Booley Directory to map to."""
    if layout == "stray":
        worktree = snapshot(main_project(tmp_path))
        stray = worktree / "elsewhere"
        write_config(stray, "")
        return worktree, stray
    if layout == "outside-git":
        start = tmp_path / "plain"
    elif layout == "unreadable-git":
        start = tmp_path / "stale"
        start.mkdir()
        (start / ".git").write_text(f"gitdir: {tmp_path / 'gone' / 'worktrees' / 'x'}\n")
    elif layout == "main-without-booley-dir":
        main = tmp_path / "main"
        init_repo(main)
        (main / "README.md").write_text("fixture\n", encoding="utf-8")
        commit_all(main)
        start = tmp_path / "linked"
        git(main, "worktree", "add", "-q", "--detach", str(start))
    else:
        bare = tmp_path / "bare.git"
        git(tmp_path, "clone", "-q", "--bare", str(main_project(tmp_path)), str(bare))
        start = tmp_path / "bare-wt"
        git(bare, "worktree", "add", "-q", "--detach", str(start))
    write_config(start / ".booley", "")
    return start, start / ".booley"


@pytest.mark.parametrize(
    "layout",
    ["outside-git", "unreadable-git", "stray", "main-without-booley-dir", "bare-repository"],
)
def test_common_booley_dir_keeps_a_selection_it_cannot_map(tmp_path, monkeypatch, layout):
    isolate_git(tmp_path, monkeypatch)
    start, selection = _unmapped_selection(tmp_path, layout)
    if layout == "stray":
        monkeypatch.setenv("BOOLEY_DIR", str(selection))

    assert resolve_common_booley_dir(start) == selection
    if layout == "outside-git":
        reset_cache()
        monkeypatch.setenv("BOOLEY_DIR", str(selection))
        assert resolve_common_booley_dir() == selection


class TestCheckoutOwnBooleyDir:
    """``own_booley_dir`` reads one checkout alone, lexically, and never the ambient selection."""

    def test_relative_dir_stays_lexical(self, tmp_path: Path) -> None:
        link = tmp_path / "link"
        real = tmp_path / "real"
        real.mkdir()
        symlink_or_skip(link, real, target_is_directory=True)
        write_config(real, '[project]\ndir = "a/../state/sub"\n')

        assert own_booley_dir(link) == link / "state" / "sub"
        assert own_booley_dir_relative(link).as_posix() == "state/sub"

    def test_ignores_environment_cache_and_ancestors(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        root = tmp_path / "root"
        write_config(tmp_path, '[project]\ndir = "ancestor"\n')
        root.mkdir()
        elsewhere = tmp_path / "elsewhere"
        elsewhere.mkdir()
        monkeypatch.setenv("BOOLEY_DIR", str(elsewhere))
        (tmp_path / ".booley").mkdir()
        resolve_booley_dir(tmp_path)  # warms the process cache

        assert own_booley_dir(root) == root / ".booley"
        write_config(root, '[project]\ndir = "state"\n')
        assert own_booley_dir(root) == root / "state"

    @pytest.mark.parametrize("text", ["INVALID{{{", '[project]\ndir = ""\n'])
    def test_unusable_configuration_means_the_default(self, tmp_path: Path, text: str) -> None:
        write_config(tmp_path, text)

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
        write_config(root, f'[project]\ndir = "{value}"\n')

        with pytest.raises(BooleyDirOutsideCheckoutError, match=r"relative \[project\]\.dir"):
            own_booley_dir_relative(root)


def test_own_booley_dir_repo_path_relocated_default_and_outside(tmp_path: Path) -> None:
    """Repository path: relocated spelling, default ``.booley``, absolute falls back."""
    relocated = tmp_path / "relocated"
    write_config(relocated, '[project]\ndir = "tools/booley"\n')
    default = tmp_path / "default"
    default.mkdir()
    outside = tmp_path / "outside"
    write_config(outside, f'[project]\ndir = "{(tmp_path / "external").as_posix()}"\n')

    assert own_booley_dir_repo_path(relocated).as_posix() == "tools/booley"
    assert own_booley_dir_repo_path(default).as_posix() == ".booley"
    assert own_booley_dir_repo_path(outside).as_posix() == ".booley"
