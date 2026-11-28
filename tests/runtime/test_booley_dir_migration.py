"""The init-only hard cut preserves Git identities, ignored state and immutable records."""

from __future__ import annotations

import argparse
import hashlib
import json
import uuid
from pathlib import Path

import pytest
from tests.git_support import git
from tests.harness.session_docker_fake import SandboxDocker, completed, serve_migration_docker
from tests.owned_scratch_support import exited_process_payload
from tests.subprocess_budget import run_bounded_child

from booley.core.booley_dir import reset_cache, resolve_booley_dir
from booley.core.booley_layout import BooleyLayoutError, ensure_layout_metadata
from booley.core.project_identity import project_label_id
from booley.harness.init_cmd import run_init
from booley.harness.setup.directory_migration import inspect_migration
from booley.runtime import booley_dir_migration as migration
from booley.runtime.session_issuance import stamp_path
from booley.runtime.worktree_identity import resolve_worktree_identity

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
def docker(monkeypatch) -> SandboxDocker:
    """A quiescent host: Docker answers, and no Sandbox runs unless a test scripts one."""
    return serve_migration_docker(monkeypatch)


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


def _leftover_record(directory: Path) -> Path:
    """Write a leftover ``goals/<id>/record.json`` (a removed feature's data) and return it."""
    target = directory / "goals" / "uart-20261006T101500Z" / "record.json"
    target.parent.mkdir(parents=True)
    target.write_text(json.dumps({"id": target.parent.name, "state": "finished"}))
    return target


@pytest.mark.parametrize("value", ["", "somewhere"])
def test_obsolete_environment_refuses_even_with_cached_selection(tmp_path, monkeypatch, value):
    (tmp_path / NEW).mkdir()
    (tmp_path / NEW / "booley.toml").write_text("# Project\n")
    resolve_booley_dir(tmp_path)
    monkeypatch.setenv("BOOLEY_PROJECT_DIR", value)
    monkeypatch.setenv("BOOLEY_DIR", str(tmp_path / NEW))
    with pytest.raises(BooleyLayoutError, match="BOOLEY_DIR"):
        resolve_booley_dir(tmp_path)


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
    leftover = _leftover_record(old)
    frozen = leftover.read_bytes()
    plan = inspect_migration(tmp_path)
    assert plan is not None
    migration.apply_migration(plan)
    moved = tmp_path / NEW / "worktrees" / "débug tree"
    assert not old.exists()
    assert (moved / NEW / "booley.toml").is_file()
    assert (moved / "untracked.v").read_text() == "precious RTL\n"
    assert resolve_worktree_identity(moved) == identity
    assert "prunable" not in git(tmp_path, "worktree", "list", "--porcelain")
    assert "locked preserve" in git(tmp_path, "worktree", "list", "--porcelain")
    assert (tmp_path / NEW / "goals" / leftover.parent.name / "record.json").read_bytes() == frozen
    if paired:
        assert "prunable" not in git(tmp_path / NEW, "worktree", "list", "--porcelain")
        assert Path(git(moved / NEW, "rev-parse", "--show-toplevel")) == moved / NEW
    assert (tmp_path / ".git/info/exclude").read_text() == f"# personal\n/{NEW}\nkeep-me\n"
    after = _snapshot(tmp_path)
    assert inspect_migration(tmp_path) is None
    assert _snapshot(tmp_path) == after


@pytest.mark.parametrize("git_backed", [True, False], ids=["git", "non-git"])
def test_power_loss_after_rename_blocks_until_init_recovers(tmp_path, monkeypatch, git_backed):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "settings"))
    root = tmp_path / "rtl"
    (root / OLD).mkdir(parents=True)
    (root / ".gitignore").write_bytes(f"/{OLD}\n".encode())
    if git_backed:
        _repository(root)
        git(root, "worktree", "add", "--detach", str(root / OLD / "worktrees/one"))
    plan = inspect_migration(root)

    def power_loss(_row):
        raise OSError("power loss")

    with monkeypatch.context() as crash:
        # `_apply_edit` is patched because no external fault lands between the rename
        # and the edits: preflight already proved every edited file writable.
        crash.setattr(migration, "_apply_edit", power_loss)
        with pytest.raises(OSError, match="power loss"):
            migration.apply_migration(plan)
    assert (root / NEW).is_dir()
    with pytest.raises(BooleyLayoutError, match="interrupted"):
        resolve_booley_dir(root)
    migration.apply_migration(inspect_migration(root))
    assert not plan.journal.exists()
    assert (root / ".gitignore").read_bytes() == f"/{NEW}\n".encode()
    if git_backed:
        assert "prunable" not in git(root, "worktree", "list", "--porcelain")


def test_failed_issuance_withdrawal_blocks_until_init_recovers(tmp_path):
    root = tmp_path / "rtl"
    (root / OLD).mkdir(parents=True)
    stamp = stamp_path(root)
    stamp.mkdir(parents=True)  # a directory in the stamp's place cannot be unlinked
    with pytest.raises(OSError):
        migration.apply_migration(inspect_migration(root))
    with pytest.raises(BooleyLayoutError, match="interrupted"):
        resolve_booley_dir(root)
    stamp.rmdir()
    stamp.write_bytes(b"{}")
    migration.apply_migration(inspect_migration(root))
    assert not stamp.exists()


def test_pull_of_tracked_rename_refuses_both_and_preserves_local_state(tmp_path):
    source, clone = tmp_path / "source", tmp_path / "clone"
    _repository(source)
    (source / OLD).mkdir()
    (source / OLD / "booley.toml").write_text("# input\n")
    (source / OLD / ".gitignore").write_text("goals/*/\nworktrees/\n")
    git(source, "add", "-f", OLD)
    git(source, "commit", "-qm", "Project")
    run_bounded_child(["git", "clone", str(source), str(clone)], check=True)
    leftover = _leftover_record(clone / OLD)
    migration.apply_migration(inspect_migration(source))
    git(source, "add", "-A", "--", OLD)
    git(source, "add", "-f", NEW)
    git(source, "commit", "-qm", "tracked rename")
    git(clone, "pull", "--ff-only")
    before = _snapshot(clone)
    with pytest.raises(BooleyLayoutError, match="both"):
        inspect_migration(clone)
    assert _snapshot(clone) == before
    assert leftover.is_file()


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


def _prepared_journal(root: Path):
    """Leave the journal `apply_migration` saves before it renames anything."""
    plan = inspect_migration(root)
    ensure_layout_metadata(root)
    plan.journal.write_text(json.dumps(plan.document))
    return plan


@pytest.mark.parametrize(
    ("fault", "message"),
    [
        ("unbound-edit", "unbound migration edit"),
        ("link-edit", "invalid managed link"),
        ("inventory", "cannot inventory"),
    ],
)
def test_interrupted_journal_refuses_without_writes(tmp_path, docker, fault, message):
    _repository(tmp_path)
    (tmp_path / OLD).mkdir()
    plan = _prepared_journal(tmp_path)
    data = json.loads(plan.journal.read_bytes())
    if fault == "unbound-edit":
        data["edits"].append({"path": str(tmp_path.parent / "outside"), "before": "", "after": ""})
    elif fault == "link-edit":
        link = str(tmp_path / "AGENTS.md")
        data["edits"].append({"path": link, "link_before": f"{OLD}/AGENTS.md"})
    else:
        docker.missing = True
    plan.journal.write_text(json.dumps(data))
    before = _snapshot(tmp_path)
    with pytest.raises(BooleyLayoutError, match=message):
        inspect_migration(tmp_path)
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
    migration.apply_migration(inspect_migration(root))
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
    # Only path components and the exact retired variable are rewritten.
    text = (
        f"CAPI=2:\n# Booley stealth core projection: {OLD}/cores/top.core\n"
        f"/tmp/x{OLD}s/{OLD}/inputs {OLD}_suffix BOOLEY_PROJECT_DIR BOOLEY_PROJECT_DIRTY\n"
    )
    projection.write_bytes(text.encode())
    migration.apply_migration(inspect_migration(tmp_path))
    assert (tmp_path / "AGENTS.md").readlink() == Path(NEW) / "AGENTS.md"
    assert (tmp_path / NEW / "runtime/doctor_stamp.json").read_text() == "{}\n"
    assert projection.read_bytes().decode() == (
        f"CAPI=2:\n# Booley stealth core projection: {NEW}/cores/top.core\n"
        f"/tmp/x{OLD}s/{NEW}/inputs {OLD}_suffix BOOLEY_DIR BOOLEY_PROJECT_DIRTY\n"
    )


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
        inspect_migration(tmp_path)
    assert _snapshot(tmp_path) == before


def test_captured_performance_evidence_is_preserved_as_opaque_state(tmp_path):
    fixture = Path(__file__).parent / "fixtures/directory_migration/compiler-cache-879.json"
    _repository(tmp_path)
    output = tmp_path / OLD / "runtime/performance-report.json"
    output.parent.mkdir(parents=True)
    output.write_bytes(fixture.read_bytes())
    migration.apply_migration(inspect_migration(tmp_path))
    assert (
        tmp_path / NEW / "runtime/performance-report.json"
    ).read_bytes() == fixture.read_bytes()


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
        inspect_migration(tmp_path)
    assert _snapshot(tmp_path) == before
    path.chmod(0o644)


@pytest.mark.parametrize("promote", [True, False], ids=["holder", "waiter"])
@pytest.mark.parametrize("retired", ["heavy", "light"])
def test_live_job_without_session_registry_refuses_unchanged(tmp_path, retired, promote):
    import os

    from booley.runtime.job_slots import CLASS_MEMORY, MemoryClaim, SlotStore

    _repository(tmp_path)
    directory = tmp_path / NEW
    slots = directory / "runtime/jobs/slots"
    store = SlotStore(slots)
    token = store.submit(
        CLASS_MEMORY,
        pid=os.getpid(),
        argv=["independent-job"],
        memory=MemoryClaim(budget_mb=1024, run_alone=True),
    )
    if promote:
        assert store.refresh(token).state == "holding"
    # A holder an older version left in a retired class directory still counts as live.
    (slots / CLASS_MEMORY).rename(slots / retired)
    for entry in (slots / retired).glob("[hw]-*"):
        # Older versions wrote these entries without a memory claim.
        payload = json.loads(entry.read_bytes())
        payload.pop("memory", None)
        entry.write_bytes(json.dumps(payload).encode())
    directory.rename(tmp_path / OLD)
    before = _snapshot(tmp_path)
    with pytest.raises(BooleyLayoutError, match="live/unknown Job"):
        inspect_migration(tmp_path)
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


_INCOMPLETE_INVENTORIES = [
    {},
    {"Config": {}, "Mounts": []},
    {"Config": {"Labels": {}}},
    {"Config": {"Labels": {"bad": 1}}, "Mounts": []},
    {"Config": {"Labels": {}}, "Mounts": [{"Type": "bind"}]},
    {"Config": {"Labels": {}}, "Mounts": [None]},
    {"Config": {"Labels": {}}, "Mounts": [{"Type": "bind", "Source": "../relative"}]},
]


@pytest.mark.parametrize(
    ("fault", "message"),
    [
        ("no-cli", "cannot inventory live Sandboxes"),
        ("daemon-down", "cannot inventory live Sandboxes"),
        ("project-label", "stop live Sandbox"),
        *((document, "invalid inventory") for document in _INCOMPLETE_INVENTORIES),
    ],
)
def test_docker_inventory_refuses_before_any_write(tmp_path, docker, fault, message):
    _repository(tmp_path)
    (tmp_path / OLD).mkdir()
    if fault == "no-cli":
        docker.missing = True
    elif fault == "daemon-down":
        docker.failing.add("ps")
    else:
        # A labelled Sandbox is refused although no local spec or mount names the Project.
        labelled = {
            "Config": {"Labels": {"booley.project-id": project_label_id(str(tmp_path))}},
            "Mounts": [{"Type": "bind", "Source": "/desktop-remapped/source"}],
        }
        document = labelled if fault == "project-label" else fault
        docker.script(("ps",), completed(stdout="live\n"))
        docker.script(("container", "inspect"), completed(stdout=json.dumps(document)))
    before = _snapshot(tmp_path)
    with pytest.raises(BooleyLayoutError, match=message):
        inspect_migration(tmp_path)
    assert _snapshot(tmp_path) == before


def test_local_retired_project_outranks_warmed_ancestor_cache(tmp_path):
    (tmp_path / NEW).mkdir()
    assert resolve_booley_dir(tmp_path) == tmp_path / NEW
    child = tmp_path / "child"
    (child / OLD).mkdir(parents=True)
    with pytest.raises(BooleyLayoutError, match="booley init"):
        resolve_booley_dir(child)


def test_source_checkout_guard_precedes_unrelated_ancestor_layout(tmp_path):
    from booley.core.checkout_role import SourceCheckoutProjectError

    (tmp_path / OLD).mkdir()
    source = tmp_path / "framework"
    _repository(source)
    (source / "pyproject.toml").write_text("[tool.booley]\nsource_checkout = true\n")
    with pytest.raises(SourceCheckoutProjectError):
        resolve_booley_dir(source)


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
    migration.apply_migration(inspect_migration(root))
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
    tmp_path, docker, capsys
):
    _repository(tmp_path)
    (tmp_path / OLD).mkdir()
    docker.missing = True
    before = _snapshot(tmp_path)
    assert run_init(argparse.Namespace(check_only=True), tmp_path) == 2
    output = capsys.readouterr().out
    assert "Would rename" in output and "cannot inventory" in output
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
    migration.apply_migration(inspect_migration(tmp_path))
    assert (tmp_path / NEW / ".runtime/jobs/finished.json").read_bytes() == before


def test_dead_consumer_of_other_registered_root_does_not_block(tmp_path):
    _repository(tmp_path)
    (tmp_path / OLD).mkdir()
    leases = migration.metadata_root(tmp_path) / "directory-consumers"
    leases.mkdir(parents=True)
    (leases / "old-worktree.json").write_text(
        json.dumps({"root": str(tmp_path / "other-worktree"), "process": exited_process_payload()})
    )
    migration.apply_migration(inspect_migration(tmp_path))
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
        inspect_migration(linked)
    assert _snapshot(tmp_path) == before


def test_unmoved_git_config_keeps_custom_formatting(tmp_path):
    _repository(tmp_path)
    (tmp_path / OLD).mkdir()
    config = tmp_path / ".git/config"
    with config.open("a") as handle:
        handle.write("[core]\n    hooksPath    =    custom/hooks\n")
    before = config.read_bytes()
    migration.apply_migration(inspect_migration(tmp_path))
    assert config.read_bytes() == before


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
    assert inspect_migration(linked) is None
    assert _snapshot(tmp_path) == before


@pytest.mark.parametrize("relative", [".gitignore", ".git/info/exclude"])
def test_managed_ignore_rewrite_preserves_unrelated_bytes_and_line_endings(tmp_path, relative):
    _repository(tmp_path)
    (tmp_path / OLD).mkdir()
    path = tmp_path / relative
    before = b"# opaque \xff \xc3\xa9\r\n/" + OLD.encode() + b"/\r\nkeep \xfe\n"
    path.write_bytes(before)
    migration.apply_migration(inspect_migration(tmp_path))
    assert path.read_bytes() == before.replace(
        b"/" + OLD.encode() + b"/", b"/" + NEW.encode() + b"/"
    )


def test_unobserved_admission_entry_refuses_with_job_admission_wording(tmp_path):
    _repository(tmp_path)
    # A holder entry the Job slot store cannot read is not one it can prove inactive.
    entry = tmp_path / OLD / "runtime/jobs/slots/memory/h-unreadable"
    entry.parent.mkdir(parents=True)
    entry.write_text("{}", encoding="utf-8")
    refusal = "cannot prove Job admission inactivity: .*memory"
    with pytest.raises(BooleyLayoutError, match=refusal):
        inspect_migration(tmp_path)
