"""Real Git evidence for owner-aware, source-preserving input diagnostics."""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest

from booley.runtime import project_trackability as pt
from booley.runtime.booley_dir import reset_cache


def git(root: Path, *args: str) -> str:
    return subprocess.run(
        ["git", *args], cwd=root, check=True, capture_output=True, text=True, timeout=30
    ).stdout.strip()


@pytest.fixture
def project(tmp_path, monkeypatch):
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("USERPROFILE", str(home))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(home / "xdg"))
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", str(home / "gitconfig"))
    monkeypatch.setenv("GIT_CONFIG_NOSYSTEM", "1")
    monkeypatch.delenv("BOOLEY_DIR", raising=False)
    monkeypatch.delenv("GIT_CONFIG_COUNT", raising=False)
    root = tmp_path / "rtl"
    root.mkdir()
    git(root, "init", "-q", "-b", "main")
    git(root, "config", "user.name", "Trackability Fixture")
    git(root, "config", "user.email", "fixture@example.invalid")
    selected = root / ".booley"
    selected.mkdir()
    (selected / "booley.toml").write_text("[stealth]\nenabled = false\n")
    reset_cache()
    yield root, selected
    reset_cache()


@pytest.mark.parametrize("source_kind", ["root", "nested", "info", "explicit", "xdg", "home"])
def test_effective_exclusion_sources(project, source_kind, monkeypatch):
    root, selected = project
    sources = {
        "root": root / ".gitignore",
        "nested": selected / ".gitignore",
        "info": root / ".git/info/exclude",
        "explicit": root.parent / ("global : ignores" if os.name == "posix" else "global ignores"),
        "xdg": Path(os.environ["XDG_CONFIG_HOME"]) / "git/ignore",
        "home": Path(os.environ["HOME"]) / ".config/git/ignore",
    }
    source = sources[source_kind]
    source.parent.mkdir(parents=True, exist_ok=True)
    source.write_text("# rule\nbooley.toml\n")
    if source_kind == "explicit":
        git(root, "config", "core.excludesFile", str(source))
    if source_kind == "home":
        monkeypatch.delenv("XDG_CONFIG_HOME")
    result = pt.inspect_project_trackability(root, selected)
    assert not result.errors
    assert len(result.ignored) == 1
    group = result.ignored[0]
    assert (group.line, group.pattern, group.paths) == (2, "booley.toml", (".booley/booley.toml",))
    assert Path(group.source) == (
        source
        if source.is_relative_to(root.parent / "home") or source_kind == "explicit"
        else source.relative_to(root)
    )
    assert f"{group.source}:2:booley.toml" in group.message


def test_negations_and_tracked_inputs(project):
    root, selected = project
    git(root, "add", ".booley/booley.toml")
    (root / ".gitignore").write_text("booley.toml\n!booley.toml\n")
    assert not pt.inspect_project_trackability(root, selected).ignored
    (root / ".gitignore").write_text("booley.toml\n")
    result = pt.inspect_project_trackability(root, selected)
    assert result.ignored[0].paths == (".booley/booley.toml",)
    assert "new/untracked" in result.ignored[0].message


def test_ignored_ancestor_grouping_and_bounded_preview(project):
    root, selected = project
    inputs = selected / "cores"
    inputs.mkdir()
    separator = ":" if os.name == "posix" else "-"
    for number in range(10):
        (inputs / f"input {number}{separator}name.core").write_text("input")
    (root / ".gitignore").write_text(".booley/\n")
    result = pt.inspect_project_trackability(root, selected)
    group = next(g for g in result.ignored if not g.history)
    assert len(group.paths) == 14
    assert f".booley/cores/input 0{separator}name.core" in group.paths
    assert "+9 more" in group.message
    assert any(g.history for g in result.ignored)


def test_audited_inputs_and_transient_pruning(project, monkeypatch):
    root, selected = project
    for name in (
        "plans/note.md",
        "handoffs/note.md",
        "output/report.txt",
        "cores/worktrees/huge/a",
        "cores/__pycache__/cache.pyc",
        "cores/tmp/scratch",
        ".git/objects/a",
    ):
        path = selected / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("local")
    for name in (
        "cores/a.core",
        "docker/requirements.txt",
        "hooks/check.py",
        "goalsets/g.toml",
        "generators/gen.py",
        "mcp_tools/check.py",
        "selftest/check.py",
        "configs.toml",
        "pipeline.toml",
        "criteria.toml",
        "doctor-waivers.toml",
        "AGENTS.md",
        "SETUP-PLAN.md",
        "goals/history/g.md",
        "tickets/history/t.md",
    ):
        path = selected / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("durable")
    original = pt.os.scandir

    def guarded(path):
        assert Path(path).name not in {"worktrees", "__pycache__", "tmp", ".git"}
        return original(path)

    monkeypatch.setattr(pt.os, "scandir", guarded)
    (root / ".gitignore").write_text(".booley/\n")
    result = pt.inspect_project_trackability(root, selected)
    paths = [p for g in result.ignored for p in g.paths]
    assert not result.errors
    assert ".booley/docker/requirements.txt" in paths
    assert ".booley/cores/a.core" in paths
    assert not any(
        part in p for p in paths for part in ("plans/", "handoffs/", "output/", "huge/")
    )


@pytest.mark.parametrize("linked", [False, True])
def test_independent_owner_inputs_histories_and_attributes(project, linked):
    root, selected = project
    selected.rename(root.parent / "seed")
    owner = root.parent / "data" if linked else selected
    owner.mkdir()
    git(owner, "init", "-q", "-b", "main")
    git(owner, "config", "user.name", "Fixture")
    git(owner, "config", "user.email", "fixture@example.invalid")
    (owner / "booley.toml").write_text("[stealth]\nenabled = false\n")
    git(owner, "add", "booley.toml")
    git(owner, "commit", "-qm", "seed")
    if linked:
        git(owner, "worktree", "add", "-q", "--detach", str(selected))
    (root / ".gitignore").write_text(".booley/\n")
    assert not pt.inspect_project_trackability(root, selected).ignored
    (selected / "goals/history").mkdir(parents=True)
    (selected / "goals/history/a.md").write_text("summary")
    (selected / ".gitattributes").write_text("* text=auto eol=lf\n")
    (selected / ".gitignore").write_text("booley.toml\ngoals/history/\n.gitattributes\n")
    result = pt.inspect_project_trackability(root, selected)
    assert not result.errors
    assert {p for g in result.ignored for p in g.paths} == {
        "booley.toml",
        "goals/history/a.md",
        ".gitattributes",
    }
    assert all(not g.history and g.owner == selected for g in result.ignored)


def test_custom_and_external_selection_and_symlink_root(project, monkeypatch):
    root, selected = project
    custom = root / "control"
    selected.rename(custom)
    monkeypatch.setenv("BOOLEY_DIR", str(custom))
    (root / ".gitignore").write_text("control/booley.toml\n")
    assert pt.inspect_project_trackability(root).ignored[0].paths == ("control/booley.toml",)
    external = root.parent / "external"
    custom.rename(external)
    git(external, "init", "-q")
    (external / ".gitignore").write_text("booley.toml\n")
    custom.symlink_to(external, target_is_directory=True)
    result = pt.inspect_project_trackability(root, custom)
    assert not result.errors
    assert result.ignored[0].owner == external
    assert result.ignored[0].paths == ("booley.toml",)


def test_symlink_leaves_are_not_followed(project):
    root, selected = project
    tree = selected / "cores"
    tree.mkdir()
    outside = root.parent / "outside"
    outside.mkdir()
    (outside / "secret.core").write_text("private")
    (tree / "alias").symlink_to(outside, target_is_directory=True)
    (tree / "leaf").symlink_to(outside / "secret.core")
    (root / ".gitignore").write_text(".booley/cores/*\n")
    result = pt.inspect_project_trackability(root, selected)
    assert {p for g in result.ignored for p in g.paths} == {
        ".booley/cores/alias",
        ".booley/cores/leaf",
    }


def test_inspection_failures_are_location_specific(project, monkeypatch):
    root, selected = project
    (selected / "cores").mkdir()
    original = pt.os.scandir

    def unreadable(path):
        if Path(path).name == "cores":
            raise PermissionError("unreadable tree")
        return original(path)

    monkeypatch.setattr(pt.os, "scandir", unreadable)
    result = pt.inspect_project_trackability(root, selected)
    assert any(str(selected / "cores") in e and "unreadable" in e for e in result.errors)
    monkeypatch.setattr(pt, "_git", lambda *args: (_ for _ in ()).throw(OSError("Git failed")))
    assert any("Git failed" in e for e in pt.inspect_project_trackability(root, selected).errors)


@pytest.mark.parametrize("failure", ["malformed", "returncode", "timeout", "exception"])
def test_git_failures_are_inspection_warnings(project, monkeypatch, failure):
    root, selected = project
    original = pt.subprocess.run

    def broken(args, **kwargs):
        if "check-ignore" not in args:
            return original(args, **kwargs)
        if failure == "timeout":
            raise subprocess.TimeoutExpired(args, 30)
        if failure == "exception":
            raise OSError("unavailable")
        return subprocess.CompletedProcess(
            args, 128 if failure == "returncode" else 0, b"bad", b"bad Git"
        )

    monkeypatch.setattr(pt.subprocess, "run", broken)
    assert pt.inspect_project_trackability(root, selected).errors


def test_no_owner_is_incomplete(project):
    root, _ = project
    external = root.parent / "unversioned"
    external.mkdir()
    result = pt.inspect_project_trackability(root, external)
    assert any(str(external) in e and "no owning Git" in e for e in result.errors)


def test_rule_subject_survives_home_relocation_and_line_insertion(project):
    root, selected = project
    groups = []
    for name, prefix in (("old-home", "# comment\n"), ("new-home", "# shifted\n" * 8)):
        source = root.parent / name / "ignore"
        source.parent.mkdir()
        source.write_text(prefix + "*.toml\n")
        git(root, "config", "core.excludesFile", str(source))
        groups.append(pt.inspect_project_trackability(root, selected).ignored[0])
    first, second = groups
    assert first.subject == second.subject == "RTL repository:global-excludes:*.toml"
    assert first.message != second.message


def test_registered_submodule_is_inspected_in_its_own_repository(project):
    root, selected = project
    source = root.parent / "core-source"
    source.mkdir()
    git(source, "init", "-q")
    git(source, "config", "user.name", "Fixture")
    git(source, "config", "user.email", "fixture@example.invalid")
    (source / "a.core").write_text("input")
    git(source, "add", "a.core")
    git(source, "commit", "-qm", "seed")
    git(
        root,
        "-c",
        "protocol.file.allow=always",
        "submodule",
        "add",
        "-q",
        str(source),
        ".booley/cores/ip",
    )
    nested = selected / "cores/ip"
    (nested / ".gitignore").write_text("a.core\n")
    (root / ".gitignore").write_text("booley.toml\n")
    result = pt.inspect_project_trackability(root, selected)
    assert not result.errors
    assert {(g.owner, p) for g in result.ignored for p in g.paths} == {
        (root, ".booley/booley.toml"),
        (nested, "a.core"),
    }


def test_managed_root_attributes_are_candidates(project):
    root, selected = project
    (root / ".gitattributes").write_text("* text=auto eol=lf\n")
    (root / ".gitignore").write_text(".gitattributes\n")
    assert pt.inspect_project_trackability(root, selected).ignored[0].paths == (".gitattributes",)


def test_unavailable_indexed_input_owner_does_not_hide_other_inputs(project):
    root, selected = project
    git(root, "commit", "--allow-empty", "-qm", "seed")
    commit = git(root, "rev-parse", "HEAD")
    git(root, "update-index", "--add", "--cacheinfo", f"160000,{commit},.booley/cores/ip")
    unavailable = selected / "cores/ip"
    unavailable.mkdir(parents=True)
    (root / ".gitignore").write_text("booley.toml\n")
    result = pt.inspect_project_trackability(root, selected)
    assert any(str(unavailable) in error for error in result.errors)
    assert result.ignored[0].paths == (".booley/booley.toml",)


def test_embedded_symlink_probe_does_not_hide_physical_inputs(project):
    root, selected = project
    physical = root / "control"
    selected.rename(physical)
    selected.symlink_to(physical, target_is_directory=True)
    (root / ".gitignore").write_text("control/booley.toml\n")
    result = pt.inspect_project_trackability(root, selected)
    assert any(group.paths == ("control/booley.toml",) for group in result.ignored)
    # The target is inside the RTL checkout: its inputs are reported there, probe included.
    assert not result.errors


@pytest.mark.parametrize("layout", ["embedded", "separate", "unversioned"])
def test_doctor_inspects_symlinked_booley_dir_in_target_owner(project, tmp_path, layout):
    """Doctor follows the linked entry and reports against the checkout owning its target."""
    from booley.harness import doctor

    root, selected = project
    if layout == "embedded":
        target = root / "control"
        (root / ".gitignore").write_text("control/goals/history/\n")
    else:
        target = tmp_path / "booley-repo"
    selected.rename(target)
    selected.symlink_to(target, target_is_directory=True)
    if layout == "separate":
        git(target, "init", "-q", "-b", "main")
        (target / ".gitignore").write_text("goals/history/\n")
    (target / "goals/history").mkdir(parents=True)
    (target / "goals/history/old.md").write_text("summary")
    reset_cache()

    reporter = doctor._Reporter.create()
    doctor._check_goal_history_ignored(root, selected, reporter.pass_, reporter.warn_)
    doctor._check_project_trackability(root, selected, reporter.warn_)
    findings = reporter.findings or []
    ids = [finding.check_id for finding in findings]

    if layout == "embedded":
        # The outer policy check is the one owner of the Goal-history probe.
        assert ids == ["goals.history-ignored"]
        assert "control/goals/history/doctor-probe.md" in findings[0].message
    elif layout == "separate":
        assert ids == ["project.inputs-ignored"]
        assert "Booley repository" in findings[0].message
    else:
        assert ids == ["project.trackability-inspection"]
        assert "no owning Git checkout" in findings[0].message


def test_embedded_ticket_history_is_not_deduplicated_with_goal_probe(project):
    from booley.harness import doctor

    root, selected = project
    history = selected / "tickets/history/old.md"
    history.parent.mkdir(parents=True)
    history.write_text("summary")
    (root / ".gitignore").write_text("*.md\n")
    reporter = doctor._Reporter.create()
    doctor._check_goal_history_ignored(root, selected, reporter.pass_, reporter.warn_)
    doctor._check_project_trackability(root, selected, reporter.warn_)
    assert any("tickets/history/old.md" in finding.message for finding in reporter.findings)


def test_broken_nested_owner_does_not_hide_sibling_or_root_inputs(project):
    root, selected = project
    broken = selected / "cores/a-broken"
    broken.mkdir(parents=True)
    (broken / ".git").write_text("gitdir: missing-administration\n")
    sibling = selected / "cores/z-good.core"
    sibling.write_text("input")
    (root / ".gitignore").write_text("booley.toml\n*.core\n")
    result = pt.inspect_project_trackability(root, selected)
    assert any(str(broken) in error for error in result.errors)
    assert {path for group in result.ignored for path in group.paths} == {
        ".booley/booley.toml",
        ".booley/cores/z-good.core",
    }


@pytest.mark.parametrize("name", ["tmp", "runtime", "worktrees"])
def test_files_named_like_transient_directories_are_inputs(project, name):
    root, selected = project
    (selected / "hooks").mkdir()
    (selected / "hooks" / name).write_text("authored input")
    (root / ".gitignore").write_text(f".booley/hooks/{name}\n")
    result = pt.inspect_project_trackability(root, selected)
    assert any(group.paths == (f".booley/hooks/{name}",) for group in result.ignored)


def test_linked_worktree_info_exclude_has_distinct_source_identity(project):
    root, _selected = project
    git(root, "add", ".booley/booley.toml")
    git(root, "commit", "-qm", "seed")
    checkout = root.parent / "linked"
    git(root, "worktree", "add", "-q", "--detach", str(checkout))
    (root / ".git/info/exclude").write_text("booley.toml\n")
    group = pt.inspect_project_trackability(checkout, checkout / ".booley").ignored[0]
    assert group.subject == "RTL repository:.git/info/exclude:booley.toml"


def test_broken_input_tree_owner_does_not_hide_other_trees(project):
    root, selected = project
    broken = selected / "cores"
    broken.mkdir()
    (broken / ".git").write_text("gitdir: missing-administration\n")
    (selected / "hooks").mkdir()
    (selected / "hooks/check.py").write_text("input")
    (root / ".gitignore").write_text("booley.toml\n*.py\n")
    result = pt.inspect_project_trackability(root, selected)
    assert any(str(broken) in error for error in result.errors)
    assert {path for group in result.ignored for path in group.paths} == {
        ".booley/booley.toml",
        ".booley/hooks/check.py",
    }
