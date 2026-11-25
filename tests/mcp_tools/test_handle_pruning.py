"""Expired Job and Session handles are pruned at MCP server start (#1550)."""

import importlib
import os
import re
import threading
from pathlib import Path
from types import SimpleNamespace

import pytest

from booley.mcp import server
from booley.mcp.session_registry import Attribution, SessionRegistry
from booley.runtime import handles, job_records
from booley.runtime.pid import (
    ProcessIdentity,
    ProcessObservation,
    ProcessState,
    capture_process_identity,
)
from booley.runtime.session_activity import process_session_key
from booley.runtime.timefmt import rfc3339_from_epoch
from tests.time_budget import hang_guard_s
from tests.uvicorn_support import capture_serving

NOW = 1_800_000_000.0
DAY = 24 * 3600.0
EPSILON = 60.0


def pruning():
    """The pruner module, imported when a test runs rather than at collection."""
    return importlib.import_module("booley.mcp.handle_pruning")


def jobs_root(booley_dir: Path) -> Path:
    return booley_dir / "job_logs/old/.runtime/jobs"


def add_job(
    booley_dir: Path,
    run_id: str,
    *,
    status: str = job_records.STATUS_DONE,
    ended_ago: float | None = 0.0,
    session_key: str | None = None,
    write: bool = True,
) -> None:
    """Register a Job handle and (optionally) its record, ended *ended_ago* seconds before NOW."""
    handles.register_job(booley_dir, run_id, "sim", session_key)
    if not write:
        return
    ended = None if ended_ago is None else rfc3339_from_epoch(NOW - ended_ago)
    record = job_records.JobRecord(
        run_id,
        "sim",
        rfc3339_from_epoch(NOW - (ended_ago or 0.0) - 10),
        60,
        status=status,
        ended_at=ended,
        session_key=session_key,
    )
    job_records.write_record(record, jobs_root(booley_dir))


def prune(booley_dir: Path, now: float = NOW) -> None:
    pruning().prune_project_handles(booley_dir, now=now)


def job_map(booley_dir: Path) -> handles.HandleMap:
    return handles.HandleStore(booley_dir, jobs=True).load()


def session_map(booley_dir: Path) -> handles.HandleMap:
    return handles.HandleStore(booley_dir).load()


@pytest.mark.parametrize(
    ("ended_ago", "kept"),
    [(DAY - EPSILON, True), (DAY + EPSILON, False)],
    ids=["just-inside", "just-outside"],
)
def test_terminal_job_entry_expires_only_after_the_grace_period(tmp_path, ended_ago, kept):
    add_job(tmp_path, "sim-one", ended_ago=ended_ago)
    prune(tmp_path)
    assert ("sim-one" in job_map(tmp_path).entries) is kept


def test_non_terminal_job_entry_is_never_pruned(tmp_path):
    add_job(tmp_path, "sim-one", status=job_records.STATUS_RUNNING, ended_ago=100 * DAY)
    prune(tmp_path)
    assert job_map(tmp_path).entries == {"sim-one": "J-SIM1"}


def test_reconciled_terminal_job_without_ended_at_ages_from_started_at(tmp_path):
    add_job(tmp_path, "sim-one", status=job_records.STATUS_FAILED, ended_ago=None)
    add_job(tmp_path, "sim-two", status=job_records.STATUS_FAILED, ended_ago=None)
    old = job_records.read_record("sim-two", jobs_root(tmp_path))
    old.started_at = rfc3339_from_epoch(NOW - 2 * DAY)
    job_records.write_record(old, jobs_root(tmp_path))
    prune(tmp_path)
    assert set(job_map(tmp_path).entries) == {"sim-one"}


def test_rowless_session_lives_while_its_job_lives_then_goes_with_aliases(tmp_path):
    add_job(tmp_path, "sim-one", ended_ago=0.0, session_key="codex:one")
    handles.HandleStore(tmp_path).reserve("worktree:new", "S", predecessors=("codex:one",))
    prune(tmp_path)
    kept = session_map(tmp_path)
    assert kept.entries == {"codex:one": "S1"}
    assert kept.aliases == {"worktree:new": "codex:one"}
    prune(tmp_path, now=NOW + DAY + EPSILON)
    assert session_map(tmp_path).entries == {}
    assert session_map(tmp_path).aliases == {}


PROCESS = ProcessIdentity(4242, "fixture", 7)


@pytest.mark.parametrize(
    ("state", "kept"),
    [
        (ProcessState.RUNNING, True),
        (ProcessState.UNKNOWN, True),
        (ProcessState.DEAD, False),
        (ProcessState.REUSED, False),
        (ProcessState.ZOMBIE, False),
    ],
)
def test_rowless_process_session_lives_until_its_process_is_proven_dead(
    tmp_path, monkeypatch, state, kept
):
    key = process_session_key(PROCESS)
    handles.reserve_session_handle(tmp_path, key)
    monkeypatch.setattr(pruning(), "observe_process", lambda identity: ProcessObservation(state))
    prune(tmp_path)
    loaded = session_map(tmp_path)
    assert (key in loaded.entries) is kept
    assert not loaded.aliases


def test_own_process_session_key_survives_pruning(tmp_path):
    identity = capture_process_identity(os.getpid())
    key = process_session_key(identity)
    from_key = importlib.import_module(
        "booley.runtime.session_activity"
    ).process_identity_from_session_key
    assert from_key(key) == identity
    assert from_key("codex:thread") is None
    handles.reserve_session_handle(tmp_path, key)
    prune(tmp_path)
    assert session_map(tmp_path).entries == {key: "S1"}


def test_full_expiry_resets_both_counters_and_allocation_restarts_at_one(tmp_path):
    for index in range(3):
        add_job(tmp_path, f"sim-{index}", ended_ago=2 * DAY, session_key=f"codex:{index}")
    prune(tmp_path)
    assert job_map(tmp_path) == handles.HandleMap()
    assert session_map(tmp_path) == handles.HandleMap()
    assert handles.reserve_session_handle(tmp_path, "codex:next") == "S1"
    handles.register_job(tmp_path, "lint-new", "lint", None)
    assert job_map(tmp_path).entries == {"lint-new": "J-LINT1"}


def test_partial_prune_keeps_counter_and_never_reissues_live_numbers(tmp_path):
    add_job(tmp_path, "sim-old", ended_ago=2 * DAY, session_key="codex:old")
    add_job(tmp_path, "sim-live", ended_ago=0.0, session_key="codex:live")
    prune(tmp_path)
    assert job_map(tmp_path).entries == {"sim-live": "J-SIM2"}
    assert session_map(tmp_path).entries == {"codex:live": "S2"}
    assert job_map(tmp_path).next_number == 3
    assert handles.reserve_session_handle(tmp_path, "codex:next") == "S3"
    handles.register_job(tmp_path, "sim-next", "sim", None)
    assert job_map(tmp_path).entries["sim-next"] == "J-SIM3"


@pytest.mark.parametrize("damage", ["malformed", "unreadable"])
def test_unreadable_job_record_keeps_its_entry(tmp_path, damage):
    add_job(tmp_path, "sim-one", ended_ago=2 * DAY)
    path = jobs_root(tmp_path) / "sim-one.json"
    path.unlink()
    if damage == "malformed":
        path.write_text("{not json")
    else:
        path.mkdir()
    prune(tmp_path)
    assert job_map(tmp_path).entries == {"sim-one": "J-SIM1"}


@pytest.mark.parametrize(
    ("submitted_ago", "stamped", "kept"),
    [(DAY - EPSILON, True, True), (DAY + EPSILON, True, False), (0.0, False, False)],
    ids=["fresh-kept", "old-pruned", "unstamped-pruned"],
)
def test_job_entry_without_a_record_is_pruned_only_after_its_submit_stamp_ages(
    tmp_path, submitted_ago, stamped, kept
):
    compact = re.sub(r"[-:]", "", rfc3339_from_epoch(NOW - submitted_ago))
    run_id = job_records.make_run_id("sim", compact, 1) if stamped else "baseline-dir"
    add_job(tmp_path, run_id, write=False)
    prune(tmp_path)
    assert (run_id in job_map(tmp_path).entries) is kept


def test_session_with_a_registry_row_under_an_alias_is_kept(tmp_path):
    handles.reserve_session_handle(tmp_path, "codex:old")
    handles.HandleStore(tmp_path).reserve("codex:canonical", "S", predecessors=("codex:old",))
    attribution = Attribution("codex:canonical", "thread", "/work", "repo:main", namespace="x")
    SessionRegistry(tmp_path).upsert(attribution, "sim", "completed", now=100)
    before = session_map(tmp_path)
    assert before.aliases == {"codex:canonical": "codex:old"}
    prune(tmp_path)
    assert session_map(tmp_path) == before


@pytest.mark.parametrize("which", ["jobs", "sessions"])
def test_corrupt_map_is_left_unchanged_and_still_refused(tmp_path, which):
    add_job(tmp_path, "sim-one", ended_ago=2 * DAY, session_key="codex:one")
    store = handles.HandleStore(tmp_path, jobs=which == "jobs")
    store.path.write_text("broken")
    with pytest.raises(ValueError):
        prune(tmp_path)
    assert store.path.read_text() == "broken"
    with pytest.raises(ValueError):
        store.load()


def test_nested_server_does_not_prune(tmp_path, monkeypatch):
    add_job(tmp_path, "sim-one", ended_ago=2 * DAY, session_key="codex:one")
    before = {p: p.read_bytes() for p in tmp_path.rglob("*.json")}
    monkeypatch.setenv("BOOLEY_MCP_NESTED", "1")
    monkeypatch.setattr(pruning(), "resolve_common_booley_dir", lambda: tmp_path)
    assert pruning().prune_at_start() is None
    assert before == {p: p.read_bytes() for p in tmp_path.rglob("*.json")}


def test_server_start_logs_pruning_failure_and_still_serves(tmp_path, monkeypatch, caplog):
    add_job(tmp_path, "sim-one", ended_ago=2 * DAY)
    handles.HandleStore(tmp_path, jobs=True).path.write_text("broken")
    monkeypatch.delenv("BOOLEY_MCP_NESTED", raising=False)
    monkeypatch.setattr(pruning(), "resolve_common_booley_dir", lambda: tmp_path)
    started: list[threading.Thread | None] = []
    real = pruning().prune_at_start

    def capture():
        started.append(real())
        return started[-1]

    served = capture_serving(monkeypatch)
    monkeypatch.setattr(pruning(), "prune_at_start", capture)
    monkeypatch.setattr(server, "_maybe_configure_job_logs_dir", lambda: None)
    monkeypatch.setattr(server, "_load_agent_settings_from_toml", lambda: None)
    monkeypatch.setattr(
        server._McpLifetime,
        "from_env",
        classmethod(lambda cls, **_: SimpleNamespace(sessions=None)),
    )
    monkeypatch.setattr(server, "_build_server", lambda lifetime, **_: (object(), []))
    monkeypatch.setattr(server, "_streamable_http_app", lambda srv: object())
    monkeypatch.setattr(server, "SessionMaintenanceApp", lambda app, sessions: app)
    with caplog.at_level("WARNING"):
        server._run_http(0)
        assert started[0] is not None
        started[0].join(timeout=hang_guard_s())
    assert len(served) == 1
    assert any("Handle pruning skipped" in r.getMessage() for r in caplog.records)


def test_session_entry_lives_while_its_worktree_exists(tmp_path):
    """A handle named ``S<N>`` stays while ``worktrees/s<N>`` exists, whatever else is gone (#2610)."""
    assert handles.reserve_session_handle(tmp_path, "codex:x") == "S1"
    worktree = tmp_path / "worktrees" / "s1"
    worktree.mkdir(parents=True)
    prune(tmp_path)
    assert session_map(tmp_path).entries == {"codex:x": "S1"}
    worktree.rmdir()
    prune(tmp_path)
    assert session_map(tmp_path).entries == {}


def test_only_session_labels_are_tied_to_worktrees(tmp_path):
    """A label that is not ``S<N>`` has no worktree to keep it."""
    (tmp_path / "worktrees" / "s1").mkdir(parents=True)
    handles.HandleStore(tmp_path).save(handles.HandleMap({"codex:y": "X1"}, next_number=2))
    prune(tmp_path)
    assert session_map(tmp_path).entries == {}
