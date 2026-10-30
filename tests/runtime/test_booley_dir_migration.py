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


@pytest.fixture(autouse=True)
def quiet_sandboxes(monkeypatch):
    """Real-Git relocation cases model a quiescent host independently of other tests."""
    original = migration._running_sandboxes
    monkeypatch.setattr(migration, "_running_sandboxes", lambda: [])
    return original


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
        input_topology_digest="sha256:" + "e" * 64,
        input_paths={
            "rtl": {"path": str(worktree), "identity": [1, 2]},
            "project": {"path": str(directory), "identity": [1, 3]},
        },
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
    if paired:
        assert "prunable" not in git(tmp_path / NEW, "worktree", "list", "--porcelain")
        assert Path(git(moved / NEW, "rev-parse", "--show-toplevel")) == moved / NEW
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
    # Keep the administrative name ASCII while exercising a Unicode checkout
    # path: Git for Windows can sanitize its initial admin name differently.
    initial = old / "worktrees" / "debug-tree"
    git(tmp_path, *flags, "worktree", "add", "--detach", str(initial))
    git(tmp_path, *flags, "worktree", "move", str(initial), str(outer))
    nested = outer / OLD
    if paired:
        git(old, *flags, "worktree", "add", "--detach", str(nested))
    else:
        nested.mkdir()
        (nested / "booley.toml").write_text("# snapshot\n")
    return old, outer


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
    assert Path(git(external, "rev-parse", "--show-toplevel")) == external
    assert Path(git(external, "config", "core.hooksPath")) == root / NEW / ".managed/hooks"
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


def test_post_migration_record_uses_ordinary_current_paths(tmp_path):
    from booley.goals.status import build_status

    _repository(tmp_path)
    (tmp_path / OLD).mkdir()
    migration.apply_migration(_inspect(tmp_path))
    directory = tmp_path / NEW
    record = _record(directory, GoalState.ACTIVE, tmp_path)
    assert build_status(GoalStore(directory), record).record.id == record.id


@pytest.mark.parametrize("variant", ["symlink", "read-only"])
def test_known_managed_file_conflict_refuses_before_rename(tmp_path, variant):
    _repository(tmp_path)
    (tmp_path / OLD).mkdir()
    path = tmp_path / ".gitignore"
    if variant == "symlink":
        target = tmp_path / "user-ignore"
        target.write_text(f"/{OLD}\n")
        path.symlink_to(target)
    else:
        path.write_text(f"/{OLD}\n")
        path.chmod(0o444)
    before = _snapshot(tmp_path)
    with pytest.raises((BooleyLayoutError, OSError), match=r"symlink|read-only|writable"):
        _inspect(tmp_path)
    assert _snapshot(tmp_path) == before
    path.chmod(0o644)


def test_malformed_link_journal_refuses_before_any_rename(tmp_path, monkeypatch):
    _repository(tmp_path)
    (tmp_path / OLD).mkdir()
    plan = _inspect(tmp_path)
    monkeypatch.setattr(migration, "_apply", lambda plan: (_ for _ in ()).throw(OSError("stop")))
    with pytest.raises(OSError):
        migration.apply_migration(plan)
    data = json.loads(plan.journal.read_bytes())
    data["edits"].append({"path": str(tmp_path / "AGENTS.md"), "link_before": f"{OLD}/AGENTS.md"})
    plan.journal.write_text(json.dumps(data))
    before = _snapshot(tmp_path)
    with pytest.raises(BooleyLayoutError, match="invalid managed link"):
        _inspect(tmp_path)
    assert _snapshot(tmp_path) == before


def test_live_job_without_session_registry_refuses_unchanged(tmp_path):
    import os

    from booley.runtime.job_slots import CLASS_HEAVY, SlotStore

    _repository(tmp_path)
    directory = tmp_path / NEW
    store = SlotStore(directory / "runtime/jobs/slots")
    store.submit(CLASS_HEAVY, pid=os.getpid(), argv=["independent-job"])
    directory.rename(tmp_path / OLD)
    before = _snapshot(tmp_path)
    with pytest.raises(BooleyLayoutError, match="live/unknown Job"):
        _inspect(tmp_path)
    assert _snapshot(tmp_path) == before


def test_host_consumer_admission_is_visible_to_migration(tmp_path, monkeypatch):
    from booley.runtime.layout_consumers import directory_consumer, require_quiescent

    monkeypatch.delenv("BOOLEY_CONTAINER", raising=False)
    _repository(tmp_path)
    (tmp_path / NEW).mkdir()
    with (
        directory_consumer(tmp_path),
        pytest.raises(BooleyLayoutError, match="directory consumer"),
    ):
        require_quiescent(tmp_path)
    require_quiescent(tmp_path)


def test_home_watcher_state_is_not_adopted_even_with_dotfiles_repository(tmp_path, monkeypatch):
    ancestor = tmp_path / NEW
    ancestor.mkdir()
    (ancestor / "booley.toml").write_text("# actual Project\n")
    home = tmp_path / "user"
    _repository(home)
    (home / NEW).mkdir()
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: home))
    assert resolve_booley_dir(home) == ancestor


def test_owned_integration_rewrites_only_path_components_and_exact_variable(tmp_path):
    from booley.harness.setup.directory_migration_integrations import _rewrite

    path = tmp_path / "owned.json"
    path.write_text(
        f"/tmp/x{OLD}s/{OLD}/inputs {OLD}_suffix BOOLEY_PROJECT_DIR BOOLEY_PROJECT_DIRTY"
    )
    row = _rewrite(path, [])
    migration._apply_edit(row)
    assert (
        path.read_text()
        == f"/tmp/x{OLD}s/{NEW}/inputs {OLD}_suffix BOOLEY_DIR BOOLEY_PROJECT_DIRTY"
    )


def test_live_sandbox_label_refuses_without_local_spec_or_matching_mount(
    tmp_path, monkeypatch, quiet_sandboxes
):
    from types import SimpleNamespace

    from booley.core.project_identity import project_label_id

    _repository(tmp_path)
    (tmp_path / OLD).mkdir()
    before = _snapshot(tmp_path)
    monkeypatch.setattr(migration, "_running_sandboxes", quiet_sandboxes)
    monkeypatch.setattr(migration.shutil, "which", lambda name: "/fake/docker")
    responses = iter(
        [
            SimpleNamespace(returncode=0, stdout="live-container\n"),
            SimpleNamespace(
                returncode=0,
                stdout=json.dumps(
                    [
                        {
                            "Config": {
                                "Labels": {"booley.project-id": project_label_id(str(tmp_path))}
                            },
                            "Mounts": [{"Type": "bind", "Source": "/desktop-remapped/source"}],
                        }
                    ]
                ),
            ),
        ]
    )
    monkeypatch.setattr(migration.subprocess, "run", lambda *a, **kw: next(responses))
    with pytest.raises(BooleyLayoutError, match="stop live Sandbox"):
        migration._refuse_live_sandboxes(tmp_path, [])
    assert _snapshot(tmp_path) == before


def test_local_retired_project_outranks_warmed_ancestor_cache(tmp_path):
    (tmp_path / NEW).mkdir()
    assert resolve_booley_dir(tmp_path) == tmp_path / NEW
    child = tmp_path / "child"
    (child / OLD).mkdir(parents=True)
    with pytest.raises(BooleyLayoutError, match="booley init"):
        resolve_booley_dir(child)


@pytest.mark.parametrize("unavailable", ["binary", "daemon"])
def test_unavailable_docker_refuses_without_local_spec_before_writes(
    tmp_path, monkeypatch, quiet_sandboxes, unavailable
):
    from types import SimpleNamespace

    _repository(tmp_path)
    (tmp_path / OLD).mkdir()
    before = _snapshot(tmp_path)
    monkeypatch.setattr(migration, "_running_sandboxes", quiet_sandboxes)
    monkeypatch.setattr(
        migration.shutil, "which", lambda name: None if unavailable == "binary" else "/fake/docker"
    )
    monkeypatch.setattr(
        migration.subprocess, "run", lambda *a, **kw: SimpleNamespace(returncode=1, stdout="")
    )
    with pytest.raises(BooleyLayoutError, match="cannot inventory live Sandboxes"):
        migration._refuse_live_sandboxes(tmp_path, [])
    assert _snapshot(tmp_path) == before


def test_source_checkout_guard_precedes_unrelated_ancestor_layout(tmp_path):
    from booley.core.checkout_role import SourceCheckoutProjectError

    (tmp_path / OLD).mkdir()
    source = tmp_path / "framework"
    _repository(source)
    (source / "pyproject.toml").write_text("[tool.booley]\nsource_checkout = true\n")
    with pytest.raises(SourceCheckoutProjectError):
        resolve_booley_dir(source)


@pytest.mark.parametrize(
    "document",
    [
        {},
        {"Config": {}, "Mounts": []},
        {"Config": {"Labels": {}}},
        {"Config": {"Labels": {"bad": 1}}, "Mounts": []},
        {"Config": {"Labels": {}}, "Mounts": [{"Type": "bind"}]},
        {"Config": {"Labels": {}}, "Mounts": [None]},
        {"Config": {"Labels": {}}, "Mounts": [{"Type": "bind", "Source": "../relative"}]},
    ],
)
def test_incomplete_docker_inventory_refuses_before_any_move(
    tmp_path, monkeypatch, quiet_sandboxes, document
):
    from types import SimpleNamespace

    _repository(tmp_path)
    (tmp_path / OLD).mkdir()
    before = _snapshot(tmp_path)
    monkeypatch.setattr(migration, "_running_sandboxes", quiet_sandboxes)
    monkeypatch.setattr(migration.shutil, "which", lambda name: "/fake/docker")
    responses = iter(
        [
            SimpleNamespace(returncode=0, stdout="live\n"),
            SimpleNamespace(returncode=0, stdout=json.dumps([document])),
        ]
    )
    monkeypatch.setattr(migration.subprocess, "run", lambda *a, **kw: next(responses))
    with pytest.raises(BooleyLayoutError, match="invalid inventory"):
        migration._refuse_live_sandboxes(tmp_path, [])
    assert _snapshot(tmp_path) == before


@pytest.mark.parametrize("kind", ["separate", "submodule"])
def test_primary_git_pointer_file_migrates_and_repairs_registered_worktree(tmp_path, kind):
    if kind == "separate":
        root = tmp_path / "rtl"
        root.mkdir()
        git(root, "init", "-q", "-b", "main", "--separate-git-dir", str(tmp_path / "metadata"))
        git(root, "commit", "--allow-empty", "-qm", "base")
    else:
        parent, source = tmp_path / "parent", tmp_path / "source"
        _repository(parent)
        _repository(source)
        git(parent, "-c", "protocol.file.allow=always", "submodule", "add", str(source), "rtl")
        root = parent / "rtl"
    old = root / OLD
    old.mkdir()
    worktree = old / "worktrees/one"
    git(root, "worktree", "add", "--detach", str(worktree))
    common = Path(git(root, "rev-parse", "--path-format=absolute", "--git-common-dir"))
    (common / "info/exclude").write_text(f"/{OLD}\n")
    git(root, "config", "core.hooksPath", str(old / ".managed/hooks"))
    identity = resolve_worktree_identity(worktree, create=True)
    marker = (root / ".git").read_bytes()
    migration.apply_migration(_inspect(root))
    moved = root / NEW / "worktrees/one"
    assert (root / ".git").read_bytes() == marker
    assert resolve_worktree_identity(moved) == identity
    assert Path(git(root, "config", "core.hooksPath")) == root / NEW / ".managed/hooks"
    assert (common / "info/exclude").read_text() == f"/{NEW}\n"
    assert "prunable" not in git(root, "worktree", "list", "--porcelain")
    assert Path(git(moved, "rev-parse", "--show-toplevel")) == moved


def test_seed_on_retired_layout_reports_full_init_without_traceback_or_writes(tmp_path, capsys):
    _repository(tmp_path)
    (tmp_path / OLD).mkdir()
    before = _snapshot(tmp_path)
    assert run_init(argparse.Namespace(check_only=False, seed=True), tmp_path) == 2
    assert "before --seed" in capsys.readouterr().out
    assert _snapshot(tmp_path) == before


def test_check_reports_pending_rename_even_when_docker_inventory_is_unavailable(
    tmp_path, monkeypatch, capsys
):
    _repository(tmp_path)
    (tmp_path / OLD).mkdir()
    monkeypatch.setattr(
        migration,
        "_running_sandboxes",
        lambda: (_ for _ in ()).throw(BooleyLayoutError("cannot inventory")),
    )
    before = _snapshot(tmp_path)
    assert run_init(argparse.Namespace(check_only=True), tmp_path) == 2
    output = capsys.readouterr().out
    assert "Would rename" in output and "cannot inventory" in output
    assert _snapshot(tmp_path) == before


def test_recovery_rechecks_sandbox_inventory_before_any_further_write(tmp_path, monkeypatch):
    _repository(tmp_path)
    (tmp_path / OLD).mkdir()
    plan = _inspect(tmp_path)
    monkeypatch.setattr(migration, "_apply", lambda plan: (_ for _ in ()).throw(OSError("stop")))
    with pytest.raises(OSError):
        migration.apply_migration(plan)
    monkeypatch.setattr(
        migration,
        "_running_sandboxes",
        lambda: (_ for _ in ()).throw(BooleyLayoutError("cannot inventory")),
    )
    before = _snapshot(tmp_path)
    with pytest.raises(BooleyLayoutError, match="cannot inventory"):
        _inspect(tmp_path)
    assert _snapshot(tmp_path) == before


def test_terminal_job_with_retained_pid_migrates_unchanged(tmp_path):
    import os

    from booley.runtime.job_records import STATUS_DONE, JobRecord, write_record

    _repository(tmp_path)
    jobs = tmp_path / OLD / ".runtime/jobs"
    write_record(
        JobRecord(
            "finished",
            "simulate",
            "2026-10-14T00:00:00Z",
            60,
            pid=os.getpid(),
            status=STATUS_DONE,
            exit_code=0,
        ),
        jobs,
    )
    before = (jobs / "finished.json").read_bytes()
    migration.apply_migration(_inspect(tmp_path))
    assert (tmp_path / NEW / ".runtime/jobs/finished.json").read_bytes() == before


def test_dead_consumer_of_other_registered_root_does_not_block(tmp_path, monkeypatch):
    import os
    from types import SimpleNamespace

    from booley.runtime import layout_consumers
    from booley.runtime.pid import DEAD, capture_process_identity

    _repository(tmp_path)
    (tmp_path / OLD).mkdir()
    leases = migration.metadata_root(tmp_path) / "directory-consumers"
    leases.mkdir(parents=True)
    identity = capture_process_identity(os.getpid())
    (leases / "old-worktree.json").write_text(
        json.dumps({"root": str(tmp_path / "other-worktree"), "process": identity.to_payload()})
    )
    monkeypatch.setattr(
        layout_consumers, "observe_process", lambda identity: SimpleNamespace(state=DEAD)
    )
    migration.apply_migration(_inspect(tmp_path))
    assert (tmp_path / NEW).is_dir()


def test_non_git_layout_gate_keeps_host_config_private(tmp_path, monkeypatch):
    import os
    import stat

    from booley.core.layout_lock import layout_gate

    config = tmp_path / "config"
    monkeypatch.setenv("XDG_CONFIG_HOME", str(config))
    monkeypatch.setattr("booley.core.booley_layout.config_dir", lambda: config / "booley")
    root = tmp_path / "project"
    (root / NEW).mkdir(parents=True)
    with layout_gate(root):
        assert migration.metadata_root(root).is_dir()
    if os.name != "nt":
        assert stat.S_IMODE((config / "booley").stat().st_mode) == 0o700


@pytest.mark.parametrize("path", ["/tmp/previous-project", r"C:\previous-project"])
def test_historical_logical_input_roots_parse_across_platforms(path):
    from booley.goals.input_identity import parse_bindings

    bindings = {name: {"path": path, "identity": [1, 2]} for name in ("rtl", "project")}
    assert parse_bindings(bindings) == bindings


def test_linked_separate_git_root_reports_owner_instead_of_metadata_as_root(tmp_path):
    root, metadata = tmp_path / "rtl", tmp_path / "metadata"
    root.mkdir()
    git(root, "init", "-q", "-b", "main", "--separate-git-dir", str(metadata))
    git(root, "commit", "--allow-empty", "-qm", "base")
    (root / OLD).mkdir()
    linked = root / OLD / "worktrees/one"
    git(root, "worktree", "add", "--detach", str(linked))
    (linked / OLD).mkdir()
    before = _snapshot(tmp_path)
    with pytest.raises(BooleyLayoutError, match="checkout owning Git metadata"):
        _inspect(linked)
    assert _snapshot(tmp_path) == before


def test_unmoved_git_config_keeps_custom_formatting(tmp_path):
    _repository(tmp_path)
    (tmp_path / OLD).mkdir()
    config = tmp_path / ".git/config"
    with config.open("a") as handle:
        handle.write("[core]\n    hooksPath    =    custom/hooks\n")
    before = config.read_bytes()
    migration.apply_migration(_inspect(tmp_path))
    assert config.read_bytes() == before


@pytest.mark.parametrize("prefix", ["/history", r"C:\history"])
def test_sealed_historical_directory_uses_recorded_path_flavor(tmp_path, prefix):
    from booley.goals.lifecycle import LifecycleError, _record_directory

    _repository(tmp_path)
    record = _record(tmp_path / NEW, GoalState.FINISHED, tmp_path)
    separator = "\\" if prefix.startswith("C:") else "/"
    stored = prefix + separator + record.id
    authority = {
        "record_directory": {
            "path": stored,
            "identity": {"record_id": record.id, "worktree": record.worktree.to_json()},
        }
    }
    current = tmp_path / NEW / "goals" / record.id
    assert str(_record_directory(authority, current, record)) == stored
    authority["record_directory"]["path"] = prefix + separator + "other-record"
    with pytest.raises(LifecycleError, match="identity was substituted"):
        _record_directory(authority, current, record)


@pytest.mark.parametrize("flavor", ["posix", "windows"])
def test_archival_artifact_confinement_preserves_logical_flavor(tmp_path, flavor):
    from pathlib import PurePosixPath, PureWindowsPath
    from types import SimpleNamespace

    from booley.goals.lifecycle import (
        LifecycleError,
        LifecycleOperation,
        LifecycleRequest,
        _validate_result_paths,
    )

    _repository(tmp_path)
    directory = tmp_path / NEW
    record = _record(directory, GoalState.FINISHED, tmp_path)
    store = GoalStore(directory)
    request = LifecycleRequest(tmp_path, record.id, str(uuid.uuid4()))
    record_dir = directory / "goals" / record.id
    operation = LifecycleOperation(
        store, SimpleNamespace(record_dir=record_dir), request, tmp_path
    )
    operation.directory.mkdir(parents=True)
    logical = (
        PureWindowsPath(r"C:\history") if flavor == "windows" else PurePosixPath("/history")
    ) / record.id
    authority = {
        "record_directory": {
            "path": str(logical),
            "identity": {"record_id": record.id, "worktree": record.worktree.to_json()},
        }
    }
    (operation.directory / "attempt.authority.json").write_text(json.dumps(authority))
    original = logical / "operations" / request.operation_id
    assert str(operation.sealed_directory("attempt")) == str(original)
    saved = {
        "status": "finished",
        "package": str(original / "review-package.json"),
        "summary": str(original / "SUMMARY.md"),
        "html": None,
    }
    _validate_result_paths(saved, operation)
    saved["summary"] = str(original / ".." / "SUMMARY.md")
    with pytest.raises(LifecycleError, match="path differs"):
        _validate_result_paths(saved, operation)


@pytest.mark.parametrize("kind", ["bare", "separate"])
@pytest.mark.parametrize("current", [False, True])
def test_non_retired_linked_checkout_does_not_require_primary_owner(tmp_path, kind, current):
    source, metadata, linked = tmp_path / "source", tmp_path / "metadata", tmp_path / "linked"
    _repository(source)
    if kind == "bare":
        git(tmp_path, "clone", "--bare", str(source), str(metadata))
        repository = metadata
    else:
        git(
            tmp_path,
            "clone",
            "--separate-git-dir",
            str(metadata),
            str(source),
            str(tmp_path / "primary"),
        )
        repository = tmp_path / "primary"
    git(repository, "worktree", "add", "--detach", str(linked))
    if current:
        (linked / NEW).mkdir()
    before = _snapshot(tmp_path)
    assert _inspect(linked) is None
    assert _snapshot(tmp_path) == before


@pytest.mark.parametrize("relative", [".gitignore", ".git/info/exclude"])
def test_managed_ignore_rewrite_preserves_unrelated_bytes_and_line_endings(tmp_path, relative):
    _repository(tmp_path)
    (tmp_path / OLD).mkdir()
    path = tmp_path / relative
    before = b"# opaque \xff \xc3\xa9\r\n/" + OLD.encode() + b"/\r\nkeep \xfe\n"
    path.write_bytes(before)
    migration.apply_migration(_inspect(tmp_path))
    assert path.read_bytes() == before.replace(
        b"/" + OLD.encode() + b"/", b"/" + NEW.encode() + b"/"
    )


def test_goal_store_locks_through_the_windows_adapter(tmp_path, monkeypatch):
    import os
    import sys
    from types import SimpleNamespace

    from booley.core import file_lock
    from booley.goals import store
    from booley.goals.status import build_status

    calls = []
    adapter = SimpleNamespace(
        LK_NBLCK=1, LK_UNLCK=2, locking=lambda fd, mode, count: calls.append((fd, mode, count))
    )
    monkeypatch.setitem(sys.modules, "msvcrt", adapter)
    monkeypatch.setattr(file_lock, "sys", SimpleNamespace(platform="win32"))
    monkeypatch.setattr(store, "os", SimpleNamespace(name="nt", getpid=os.getpid, fstat=os.fstat))
    _repository(tmp_path)
    directory = tmp_path / NEW
    record = _record(directory, GoalState.ACTIVE, tmp_path)
    build_status(GoalStore(directory), record)
    assert any(mode == adapter.LK_NBLCK for _, mode, _ in calls)
