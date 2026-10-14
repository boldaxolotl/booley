"""The init-only hard cut preserves Git identities, ignored state and immutable records."""

from __future__ import annotations

import argparse
import hashlib
import json
import subprocess
import uuid
from pathlib import Path

import pytest
from tests.goals.conftest import git

from booley.core.booley_dir import reset_cache, resolve_booley_dir
from booley.core.booley_layout import BooleyLayoutError
from booley.goals.model import GoalRecord, GoalState, WorktreeIdentity
from booley.goals.store import GoalStore, resolve_worktree_identity
from booley.harness.init_cmd import run_init
from booley.runtime import booley_dir_migration as migration

OLD = ".booley_project"
NEW = ".booley"


@pytest.fixture(autouse=True)
def selection(monkeypatch):
    monkeypatch.delenv("BOOLEY_DIR", raising=False)
    monkeypatch.delenv("BOOLEY_PROJECT_DIR", raising=False)
    reset_cache()
    yield
    reset_cache()


def _inspect(root: Path):
    from booley.harness.setup.directory_migration import inspect_migration

    return inspect_migration(root)


def _repository(path: Path) -> None:
    path.mkdir(parents=True, exist_ok=True)
    git(path, "init", "-q", "-b", "main")
    git(path, "commit", "-q", "--allow-empty", "-m", "base")


def _snapshot(root: Path) -> dict[str, str]:
    return {
        str(path.relative_to(root)): (
            str(path.readlink())
            if path.is_symlink()
            else "directory"
            if path.is_dir()
            else hashlib.sha256(path.read_bytes()).hexdigest()
        )
        for path in root.rglob("*")
    }


def _record(directory: Path, state: GoalState, worktree: Path) -> GoalRecord:
    identity = resolve_worktree_identity(worktree, create=True)
    if identity is None:
        identity = WorktreeIdentity(str(uuid.uuid4()), "main")
    record = GoalRecord(
        id="uart-20261006T101500Z",
        state=state,
        worktree=identity,
        worktree_path=str(worktree),
        branch="goal/uart",
        original_ref="refs/heads/main",
        base_sha="a" * 40,
        entered_at="2026-10-06T10:15:00Z",
        revision=1,
    )
    target = directory / "goals" / record.id / "record.json"
    target.parent.mkdir(parents=True)
    target.write_text(json.dumps(record.to_json()))
    return record


def test_hard_cut_diagnoses_old_instead_of_ancestor(tmp_path):
    (tmp_path / NEW).mkdir()
    child = tmp_path / "child"
    (child / OLD).mkdir(parents=True)
    with pytest.raises(BooleyLayoutError, match="booley init"):
        resolve_booley_dir(child)


@pytest.mark.parametrize("value", ["", "somewhere"])
def test_obsolete_environment_refuses_even_with_cached_selection(tmp_path, monkeypatch, value):
    (tmp_path / NEW).mkdir()
    (tmp_path / NEW / "booley.toml").write_text("# Project\n")
    resolve_booley_dir(tmp_path)
    monkeypatch.setenv("BOOLEY_PROJECT_DIR", value)
    monkeypatch.setenv("BOOLEY_DIR", str(tmp_path / NEW))
    with pytest.raises(BooleyLayoutError, match="BOOLEY_DIR"):
        resolve_booley_dir(tmp_path)


@pytest.mark.parametrize("state", [GoalState.ENTERING, GoalState.ACTIVE, GoalState.FINISHING])
def test_init_occupancy_refusal_precedes_all_writes(tmp_path, state, capsys):
    _repository(tmp_path)
    (tmp_path / OLD).mkdir()
    record = _record(tmp_path / OLD, state, tmp_path)
    before = _snapshot(tmp_path)
    assert run_init(argparse.Namespace(check_only=False), tmp_path) == 2
    assert record.id in capsys.readouterr().out
    assert _snapshot(tmp_path) == before


def test_init_check_reports_rename_without_writes_or_bootstrap(tmp_path, capsys):
    _repository(tmp_path)
    (tmp_path / OLD).mkdir()
    before = _snapshot(tmp_path)
    assert run_init(argparse.Namespace(check_only=True), tmp_path) == 2
    assert "Would rename" in capsys.readouterr().out
    assert _snapshot(tmp_path) == before


@pytest.mark.parametrize("older", [False, True])
def test_init_conflict_and_older_layout_are_observational(tmp_path, older, capsys):
    _repository(tmp_path)
    (tmp_path / NEW).mkdir()
    if older:
        (tmp_path / NEW / "project").mkdir()
    else:
        (tmp_path / OLD).mkdir()
    before = _snapshot(tmp_path)
    assert run_init(argparse.Namespace(check_only=False), tmp_path) == 2
    assert ("older" if older else "both") in capsys.readouterr().out
    assert _snapshot(tmp_path) == before


@pytest.mark.parametrize("paired", [False, True])
@pytest.mark.parametrize("relative", [False, True])
def test_moves_detached_worktree_and_nested_copy_or_pair(tmp_path, paired, relative):
    _repository(tmp_path)
    old, outer = _linked_layout(tmp_path, paired, relative)
    (outer / "untracked.v").write_text("precious RTL\n")
    git(tmp_path, "worktree", "lock", "--reason", "preserve", str(outer))
    identity = resolve_worktree_identity(outer, create=True)
    record = _record(old, GoalState.FINISHED, outer)
    frozen = (old / "goals" / record.id / "record.json").read_bytes()
    plan = _inspect(tmp_path)
    assert plan is not None
    migration.apply_migration(plan)
    moved = tmp_path / NEW / "worktrees" / "débug tree"
    assert not old.exists()
    assert (moved / NEW / "booley.toml").is_file()
    assert (moved / "untracked.v").read_text() == "precious RTL\n"
    assert resolve_worktree_identity(moved) == identity
    assert "prunable" not in git(tmp_path, "worktree", "list", "--porcelain")
    assert "locked preserve" in git(tmp_path, "worktree", "list", "--porcelain")
    assert (tmp_path / NEW / "goals" / record.id / "record.json").read_bytes() == frozen
    assert GoalStore(tmp_path / NEW).load(record.id).worktree == identity
    assert migration.archival_path(tmp_path / NEW, outer, record_id=record.id) == moved
    if paired:
        assert "prunable" not in git(tmp_path / NEW, "worktree", "list", "--porcelain")
        assert git(moved / NEW, "rev-parse", "--show-toplevel") == str(moved / NEW)
    assert (tmp_path / ".git/info/exclude").read_text() == f"# personal\n/{NEW}\nkeep-me\n"
    after = _snapshot(tmp_path)
    assert _inspect(tmp_path) is None
    assert _snapshot(tmp_path) == after


def test_recovery_after_parent_rename(tmp_path, monkeypatch):
    _repository(tmp_path)
    (tmp_path / OLD).mkdir()
    outer = tmp_path / OLD / "worktrees" / "one"
    git(tmp_path, "worktree", "add", "--detach", str(outer))
    plan = _inspect(tmp_path)
    original = migration._apply_edit
    monkeypatch.setattr(
        migration, "_apply_edit", lambda row: (_ for _ in ()).throw(OSError("power loss"))
    )
    with pytest.raises(OSError, match="power loss"):
        migration.apply_migration(plan)
    assert (tmp_path / NEW).is_dir()
    with pytest.raises(BooleyLayoutError, match="interrupted"):
        resolve_booley_dir(tmp_path)
    monkeypatch.setattr(migration, "_apply_edit", original)
    recovered = _inspect(tmp_path)
    migration.apply_migration(recovered)
    assert "prunable" not in git(tmp_path, "worktree", "list", "--porcelain")


def test_pull_of_tracked_rename_refuses_both_and_preserves_local_state(tmp_path):
    source, clone = tmp_path / "source", tmp_path / "clone"
    _repository(source)
    (source / OLD).mkdir()
    (source / OLD / "booley.toml").write_text("# input\n")
    (source / OLD / ".gitignore").write_text("goals/*/\nworktrees/\n")
    git(source, "add", "-f", OLD)
    git(source, "commit", "-qm", "Project")
    subprocess.run(
        ["git", "clone", str(source), str(clone)], check=True, capture_output=True, timeout=30
    )
    record = _record(clone / OLD, GoalState.ABANDONED, clone)
    migration.apply_migration(_inspect(source))
    git(source, "add", "-A", "--", OLD)
    git(source, "add", "-f", NEW)
    git(source, "commit", "-qm", "tracked rename")
    git(clone, "pull", "--ff-only")
    before = _snapshot(clone)
    with pytest.raises(BooleyLayoutError, match="both"):
        _inspect(clone)
    assert _snapshot(clone) == before
    assert (clone / OLD / "goals" / record.id / "record.json").is_file()


def _linked_layout(tmp_path, paired, relative):
    old = tmp_path / OLD
    old.mkdir()
    (old / "booley.toml").write_text("# preserved input\n")
    (tmp_path / ".git/info/exclude").write_text(f"# personal\n/{OLD}\nkeep-me\n")
    if paired:
        _repository(old)
        git(old, "add", "booley.toml")
        git(old, "commit", "-qm", "Project inputs")
    outer = old / "worktrees" / "débug tree"
    flags = ["-c", f"worktree.useRelativePaths={'true' if relative else 'false'}"]
    git(tmp_path, *flags, "worktree", "add", "--detach", str(outer))
    nested = outer / OLD
    if paired:
        git(old, *flags, "worktree", "add", "--detach", str(nested))
    else:
        nested.mkdir()
        (nested / "booley.toml").write_text("# snapshot\n")
    return old, outer


@pytest.mark.parametrize("tamper", [False, True])
def test_captured_v2_finish_replays_without_checkout_or_evidence_rewrite(tmp_path, tamper):
    import shutil

    from booley.goals.apply import ChangeEnvironment
    from booley.goals.entry import EntryEnvironment
    from booley.goals.finish import FinishEnvironment, finish_goal
    from booley.goals.lifecycle import LifecycleError, LifecycleRequest
    from booley.goals.status import build_status

    fixture = Path(__file__).parent / "fixtures/directory_migration/finished-v2"
    metadata = json.loads((fixture / "capture.json").read_bytes())
    _repository(tmp_path)
    shutil.copytree(fixture / "goals", tmp_path / OLD / "goals")
    before = _snapshot(tmp_path / OLD / "goals")
    migration.apply_migration(_inspect(tmp_path))
    directory = tmp_path / NEW
    assert _snapshot(directory / "goals") == before
    store = GoalStore(directory)
    record = store.load(metadata["record_id"])
    operation = directory / "goals" / record.id / "operations" / metadata["operation_id"]
    result = json.loads((operation / "result.json").read_bytes())
    call = LifecycleRequest(
        Path(record.worktree_path),
        record.id,
        metadata["operation_id"],
        summary=metadata["summary"],
        explain_html=True,
    )
    env = FinishEnvironment(ChangeEnvironment(EntryEnvironment(directory)))
    if tamper:
        (operation / "attempt.json").write_text("{}")
        before = _snapshot(tmp_path)
        with pytest.raises(LifecycleError, match="durable authority"):
            finish_goal(call, env)
    else:
        before = _snapshot(tmp_path)
        assert finish_goal(call, env) == result
        assert build_status(store, record).record.worktree == record.worktree
    assert _snapshot(tmp_path) == before


def test_recovery_rejects_unbound_journal_edit_without_writes(tmp_path, monkeypatch):
    _repository(tmp_path)
    (tmp_path / OLD).mkdir()
    plan = _inspect(tmp_path)
    monkeypatch.setattr(migration, "_apply", lambda plan: (_ for _ in ()).throw(OSError("stop")))
    with pytest.raises(OSError):
        migration.apply_migration(plan)
    data = json.loads(plan.journal.read_bytes())
    data["edits"].append({"path": str(tmp_path.parent / "outside"), "before": "", "after": ""})
    plan.journal.write_text(json.dumps(data))
    before = _snapshot(tmp_path)
    with pytest.raises(BooleyLayoutError, match="unbound migration edit"):
        _inspect(tmp_path)
    assert _snapshot(tmp_path) == before


def test_migration_repairs_external_pair_without_moving_its_checkout(tmp_path):
    root, external = tmp_path / "main", tmp_path / "elsewhere/project"
    _repository(root)
    old = root / OLD
    _repository(old)
    git(old, "worktree", "add", "--detach", str(external))
    git(external, "config", "core.hooksPath", str(old / ".managed/hooks"))
    common = old / ".git/booley"
    common.mkdir()
    (common / "repository-id").write_text(str(uuid.uuid4()) + "\n")
    admin = Path(git(external, "rev-parse", "--absolute-git-dir")) / "booley"
    admin.mkdir()
    (admin / "worktree-id").write_text(str(uuid.uuid4()) + "\n")
    identity = resolve_worktree_identity(external)
    migration.apply_migration(_inspect(root))
    assert resolve_worktree_identity(external) == identity
    assert git(external, "rev-parse", "--show-toplevel") == str(external)
    assert git(external, "config", "core.hooksPath") == str(root / NEW / ".managed/hooks")
    assert "prunable" not in git(root / NEW, "worktree", "list", "--porcelain")


def test_existing_integrations_and_freshness_are_repaired_in_transaction(tmp_path):
    _repository(tmp_path)
    old = tmp_path / OLD
    (old / "runtime").mkdir(parents=True)
    (old / "runtime/doctor_stamp.json").write_text('{"passed_at":"2026-10-14"}')
    (tmp_path / "AGENTS.md").symlink_to(f"{OLD}/AGENTS.md")
    (old / "AGENTS.md").write_text("user guidance\n")
    projection = tmp_path / ".booley-projected-top.core"
    projection.write_text(f"CAPI=2:\n# Booley stealth core projection: {OLD}/cores/top.core\n")
    migration.apply_migration(_inspect(tmp_path))
    assert (tmp_path / "AGENTS.md").readlink() == Path(NEW) / "AGENTS.md"
    assert (tmp_path / NEW / "runtime/doctor_stamp.json").read_text() == "{}\n"
    assert OLD not in projection.read_text()


def test_pending_non_git_migration_blocks_current_resolver(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "settings"))
    root = tmp_path / "rtl"
    (root / OLD).mkdir(parents=True)
    plan = _inspect(root)
    original = migration._invalidate_issuance
    monkeypatch.setattr(
        migration, "_invalidate_issuance", lambda doc: (_ for _ in ()).throw(OSError("power loss"))
    )
    with pytest.raises(OSError):
        migration.apply_migration(plan)
    with pytest.raises(BooleyLayoutError, match="interrupted"):
        resolve_booley_dir(root)
    monkeypatch.setattr(migration, "_invalidate_issuance", original)
    migration.apply_migration(_inspect(root))
    assert not plan.journal.exists()


def test_affected_missing_checkout_refuses_before_mutation(tmp_path):
    import shutil

    _repository(tmp_path)
    old = tmp_path / OLD
    old.mkdir()
    worktree = old / "worktrees/missing"
    git(tmp_path, "worktree", "add", "--detach", str(worktree))
    shutil.rmtree(worktree)
    before = _snapshot(tmp_path)
    with pytest.raises(BooleyLayoutError, match="missing"):
        _inspect(tmp_path)
    assert _snapshot(tmp_path) == before


def test_captured_performance_evidence_is_preserved_as_opaque_state(tmp_path):
    fixture = Path(__file__).parent / "fixtures/directory_migration/compiler-cache-879.json"
    _repository(tmp_path)
    output = tmp_path / OLD / "runtime/performance-report.json"
    output.parent.mkdir(parents=True)
    output.write_bytes(fixture.read_bytes())
    migration.apply_migration(_inspect(tmp_path))
    assert (
        tmp_path / NEW / "runtime/performance-report.json"
    ).read_bytes() == fixture.read_bytes()
