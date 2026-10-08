"""Cheap advisory attribution/storage tests with injected time and fake procfs."""

from __future__ import annotations

import asyncio
import json
from concurrent.futures import ThreadPoolExecutor
from contextlib import suppress
from pathlib import Path
from types import SimpleNamespace

import pytest

from booley.goals.model import WorktreeIdentity
from booley.mcp import session_peer, session_registry
from booley.mcp.session_observer import SessionObserver
from booley.mcp.session_registry import Attribution, SessionRegistry
from booley.runtime.pid import ProcessIdentity, ProcessObservation, ProcessState
from tests.conftest import symlink_or_skip

SCOPE = "pid:[17]"
IDENTITY = ProcessIdentity(98765, SCOPE, 101)


def facts(key: str = "codex:one", kind: str = "thread") -> Attribution:
    return Attribution(key, kind, "/fixture/work", "repo:work", process=IDENTITY, namespace=SCOPE)


@pytest.fixture
def registry(tmp_path):
    return SessionRegistry(tmp_path, quiet_after=60, proc_root=tmp_path / "proc")


@pytest.mark.parametrize(
    "key", ["/absolute", "../../escape", "a" * 10000, "\\windows\\path", "codex:one"]
)
def test_opaque_contained_keys(registry, key):
    registry.upsert(facts(key), "tool", "completed", now=100)
    assert registry.path(key).parent == registry.root
    assert len(registry.path(key).name) == 69
    assert registry.snapshot().rows[0].attribution.key == key


def test_monotonic_times_started_and_bounded_metadata(registry):
    registry.upsert(facts(), "tool", "completed", now=100)
    registry.upsert(facts(), "tool", "completed", now=90)
    for number in range(30):
        registry.upsert(facts(), "tool", "completed", now=101 + number)
    row = registry.snapshot().rows[0]
    assert row.started_at == "1970-01-01T00:01:40Z"
    assert row.last_call_at == "1970-01-01T00:02:10Z"
    assert len(row.calls) == 20
    assert "arguments" not in json.loads(row.payload())


@pytest.mark.parametrize(
    "state,removed",
    [
        (ProcessState.RUNNING, 0),
        (ProcessState.UNKNOWN, 0),
        (ProcessState.DEAD, 1),
        (ProcessState.REUSED, 1),
        (ProcessState.ZOMBIE, 1),
    ],
)
def test_process_prune_never_treats_unknown_as_death(registry, monkeypatch, state, removed):
    registry.upsert(facts("pid:one", "process"), "tool", "completed", now=100)
    monkeypatch.setattr(
        session_registry, "observe_process", lambda *_a, **_k: ProcessObservation(state)
    )
    assert registry.prune(now=999, namespace=SCOPE) == removed


@pytest.mark.parametrize("kind", ["thread", "worktree"])
def test_quiet_expiry_and_foreign_namespace(registry, kind):
    registry.upsert(facts(kind=kind), "tool", "completed", now=100)
    assert registry.prune(now=200, namespace="foreign") == 0
    assert registry.prune(now=159, namespace=SCOPE) == 0
    assert registry.prune(now=160, namespace=SCOPE) == 1


def test_pruning_touches_registry_only(registry, tmp_path):
    sentinel = tmp_path / "goals" / "still-running"
    sentinel.parent.mkdir()
    sentinel.write_bytes(b"Goal/Job/slot bytes")
    registry.upsert(facts(), "tool", "completed", now=100)
    assert registry.prune(now=100, namespace=SCOPE, worktree_exists=lambda _: False) == 1
    assert sentinel.read_bytes() == b"Goal/Job/slot bytes"


def test_corrupt_future_schema_and_symlink_are_diagnosed_not_pruned(registry, tmp_path):
    registry.upsert(facts(), "tool", "completed", now=100)
    data = json.loads(registry.path(facts().key).read_bytes())
    data["schema"] = 2
    registry.path(facts().key).write_text(json.dumps(data))
    corrupt = registry.root / "corrupt.json"
    corrupt.write_bytes(b"{broken")
    before = {path.name: path.read_bytes() for path in registry.root.iterdir()}
    assert len(registry.snapshot().diagnostics) == 2
    assert registry.prune(now=999, namespace=SCOPE) == 0
    assert before == {path.name: path.read_bytes() for path in registry.root.iterdir()}
    link = registry.root / "linked.json"
    symlink_or_skip(link, corrupt)
    assert len(registry.snapshot().diagnostics) == 3


def test_concurrent_upsert_prune_serialization(registry):
    registry.upsert(facts(), "tool", "completed", now=100)

    def update(number):
        with suppress(BlockingIOError):
            registry.upsert(facts(), "tool", "completed", now=200 + number)

    with ThreadPoolExecutor(max_workers=4) as pool:
        list(pool.map(update, range(20)))
    assert registry.snapshot().rows[0].last_call_at >= "1970-01-01T00:03:20Z"
    assert registry.prune(now=200, namespace=SCOPE) == 0


@pytest.mark.parametrize(
    "client,meta,peer,kind",
    [
        ("codex-mcp-client", {"threadId": "one"}, IDENTITY, "thread"),
        ("codex-mcp-client", {}, IDENTITY, "worktree"),
        ("codex-mcp-client", {"threadId": 123}, IDENTITY, "worktree"),
        ("codex-mcp-client", {"threadId": "a" * 1025}, IDENTITY, "worktree"),
        ("claude-code", {}, IDENTITY, "process"),
        ("unknown", {}, None, "worktree"),
    ],
)
def test_request_priority_and_worktree_aliases(tmp_path, monkeypatch, client, meta, peer, kind):
    identity = WorktreeIdentity("00000000-0000-4000-8000-000000000001", "main")
    monkeypatch.setattr(
        session_registry, "_worktree_facts", lambda root: (root, identity.key, "main")
    )
    monkeypatch.setattr(session_registry, "namespace", lambda _: SCOPE)
    result = session_registry.resolve_attribution(
        tmp_path, metadata=meta, client_name=client, peer=peer
    )
    assert result.kind == kind
    assert result.worktree_key == identity.key
    assert result.branch == "main"


@pytest.mark.asyncio
async def test_reserved_headers_are_stripped_and_interleaved_requests_isolated():
    observed = []

    async def downstream(scope, _receive, _send):
        observed.append(scope["headers"])
        await asyncio.sleep(0)
        assert dict(scope["headers"])[b"x-booley-peer-port"] == str(scope["client"][1]).encode()

    app = session_peer.PeerBoundary(downstream)
    await asyncio.gather(
        *(
            app(
                {
                    "type": "http",
                    "client": ("127.0.0.1", port),
                    "server": ("127.0.0.1", 4321),
                    "headers": [
                        (b"X-Booley-Peer-Port", b"spoof"),
                        (b"x-booley-peer-port", b"spoof2"),
                        (b"safe", b"keep"),
                    ],
                },
                None,
                None,
            )
            for port in (50001, 50002)
        )
    )
    assert [dict(headers)[b"x-booley-peer-port"] for headers in observed] == [b"50001", b"50002"]
    assert session_peer.observed_peer(None) is None


def _fake_process(root: Path, pid: int, inode: str):
    directory = root / str(pid)
    (directory / "fd").mkdir(parents=True)
    (directory / "ns").mkdir()
    symlink_or_skip(directory / "ns/pid", Path(SCOPE))
    symlink_or_skip(directory / "fd/7", Path(f"socket:[{inode}]"))
    (directory / "stat").write_text(f"{pid} (client) S " + "0 " * 18 + "101 0\n")


def test_peer_endpoint_and_inode_unique_revalidation(tmp_path):
    proc = tmp_path / "proc"
    (proc / "net").mkdir(parents=True)
    (proc / "net/tcp").write_text("header\n0: 0100007F:C351 0100007F:10E1 01 0 0 0 0 0 123\n")
    _fake_process(proc, 98765, "123")
    args = (("127.0.0.1", 50001), ("127.0.0.1", 4321))
    assert session_peer.peer_identity(*args, proc_root=proc) == IDENTITY
    assert session_peer.peer_identity(("127.0.0.1", 50002), args[1], proc_root=proc) is None
    _fake_process(proc, 98766, "123")
    assert session_peer.peer_identity(*args, proc_root=proc) is None


@pytest.mark.asyncio
async def test_observer_broken_registry_cannot_change_attribution_or_dispatch(
    monkeypatch, tmp_path
):
    monkeypatch.setenv("BOOLEY_GOAL_MODE_PREVIEW", "1")
    observer = SessionObserver(budget=1)
    monkeypatch.setattr(
        observer,
        "_registry",
        lambda: SimpleNamespace(
            upsert=lambda *_a, **_k: (_ for _ in ()).throw(OSError("unwritable"))
        ),
    )
    await observer.record(facts(), "goal_finish", "completed")
    assert facts().key == "codex:one"


@pytest.mark.asyncio
async def test_slow_registry_leaves_only_one_worker(monkeypatch):
    observer = SessionObserver(budget=0.001)
    pending = asyncio.get_running_loop().create_future()
    observer._pending = pending
    assert await observer._bounded(lambda: pytest.fail("must not queue more work")) is None
    pending.set_result(None)


def test_registry_missing_snapshot_is_file_write_free(tmp_path):
    assert SessionRegistry(tmp_path).snapshot().rows == ()
    assert list(tmp_path.iterdir()) == []


@pytest.mark.asyncio
async def test_thread_identity_survives_busy_presentation_worker(monkeypatch):
    monkeypatch.setenv("BOOLEY_GOAL_MODE_PREVIEW", "1")
    observer = SessionObserver()
    blocked = asyncio.get_running_loop().create_future()
    observer._pending = blocked
    first, second = await asyncio.gather(
        observer.attribution({}, {"threadId": "one"}, "codex-mcp-client"),
        observer.attribution({}, {"threadId": "two"}, "codex-mcp-client"),
    )
    assert (first.key, second.key) == ("codex:one", "codex:two")
    blocked.set_result(None)


def test_visible_expiry_filters_without_deletion(registry):
    registry.upsert(facts(), "sim", "completed", now=100)
    before = registry.path(facts().key).read_bytes()
    assert registry.visible_snapshot(now=160, scope=SCOPE).rows == ()
    assert registry.path(facts().key).read_bytes() == before


@pytest.mark.asyncio
async def test_maintenance_prunes_during_silence(monkeypatch, registry):
    from booley.mcp import session_observer

    monkeypatch.setenv("BOOLEY_GOAL_MODE_PREVIEW", "1")
    registry.upsert(facts(), "sim", "completed", now=100)
    observer = SessionObserver(now=lambda: 200, budget=1)
    monkeypatch.setattr(observer, "_registry", lambda: registry)
    monkeypatch.setattr(session_observer, "namespace", lambda: SCOPE)
    monkeypatch.setattr(
        session_observer, "registered_worktree_presence", lambda _: lambda _facts: True
    )
    await observer.maintain()
    assert registry.snapshot().rows == ()


def test_prune_refresh_race_uses_latest_row_under_lock(registry):
    registry.upsert(facts(), "sim", "completed", now=100)
    stale = registry.snapshot().rows[0]
    registry.upsert(facts(), "sim", "completed", now=200)
    assert registry.prune(now=201, namespace=SCOPE) == 0
    assert registry.snapshot().rows[0] != stale


def test_socket_pid_reuse_revalidation(tmp_path, monkeypatch):
    proc = tmp_path / "proc"
    (proc / "net").mkdir(parents=True)
    (proc / "net/tcp").write_text("header\n0: 0100007F:C351 0100007F:10E1 01 0 0 0 0 0 123\n")
    _fake_process(proc, 98765, "123")
    identities = iter((IDENTITY, ProcessIdentity(IDENTITY.pid, SCOPE, 102)))
    monkeypatch.setattr(
        session_peer, "capture_process_identity", lambda *_a, **_k: next(identities)
    )
    assert (
        session_peer.peer_identity(("127.0.0.1", 50001), ("127.0.0.1", 4321), proc_root=proc)
        is None
    )


def test_registry_rejects_noncanonical_and_regressing_history(registry):
    registry.upsert(facts(), "tool", "completed", now=100)
    path = registry.path(facts().key)
    row = json.loads(path.read_bytes())
    row["last_call_at"] = "1970-01-01T00:01:40+00:00"
    path.write_text(json.dumps(row))
    assert "canonical" in registry.snapshot().diagnostics[0]
    row["last_call_at"] = "1970-01-01T00:01:40Z"
    row["calls"][0]["at"] = "1970-01-01T00:00:00Z"
    path.write_text(json.dumps(row))
    assert "regress" in registry.snapshot().diagnostics[0]


def test_registry_maintenance_accepts_moved_identity_and_git_failure(tmp_path, monkeypatch):
    from booley.runtime import worktrees

    identity = WorktreeIdentity("00000000-0000-4000-8000-000000000001", "main")
    monkeypatch.setattr(
        worktrees, "list_worktrees", lambda _: (worktrees.WorktreeEntry(tmp_path),)
    )
    monkeypatch.setattr(session_registry, "resolve_worktree_identity", lambda _: identity)
    observed = Attribution("key", "thread", str(tmp_path / "old"), identity.key)
    assert session_registry.registered_worktree_presence(tmp_path)(observed) is True
    monkeypatch.setattr(
        worktrees, "list_worktrees", lambda _: (_ for _ in ()).throw(OSError("unavailable"))
    )
    assert session_registry.registered_worktree_presence(tmp_path)(observed) is None


def test_visible_registry_is_one_namespace_and_unknown_is_unavailable(registry):
    registry.upsert(facts(), "tool", "completed", now=100)
    registry.upsert(
        Attribution("foreign", "thread", "/fixture", "repo:work", namespace="other"),
        "tool",
        "completed",
        now=100,
    )
    before = {path.name: path.read_bytes() for path in registry.root.iterdir()}
    assert [
        row.attribution.key for row in registry.visible_snapshot(now=101, scope=SCOPE).rows
    ] == [facts().key]
    assert registry.visible_snapshot(now=101, scope="").rows == ()
    assert "namespace unavailable" in registry.visible_snapshot(now=101, scope="").diagnostics[-1]
    assert before == {path.name: path.read_bytes() for path in registry.root.iterdir()}


@pytest.mark.parametrize("thread", ["bad\ud800", "bad\n", "bad\x7f", "", 123])
def test_invalid_thread_metadata_falls_back_without_encoding_failure(tmp_path, thread):
    result = session_registry.resolve_attribution(
        tmp_path, metadata={"threadId": thread}, client_name="codex", peer=IDENTITY
    )
    assert result.kind == "worktree"


def test_stateless_client_without_metadata_does_not_guess_distinct_process(tmp_path):
    result = session_registry.resolve_attribution(
        tmp_path, metadata={}, client_name="", peer=IDENTITY
    )
    assert result.kind == "worktree"


@pytest.mark.asyncio
async def test_invalid_workdir_does_not_discard_a_valid_thread_error_attribution(monkeypatch):
    monkeypatch.setenv("BOOLEY_GOAL_MODE_PREVIEW", "1")
    monkeypatch.setattr(session_registry, "_worktree_facts", lambda root: (root, "cwd", ""))
    result = await SessionObserver().attribution({"work_dir": 42}, {"threadId": "one"}, "codex")
    assert result.key == "codex:one"
    assert result.work_dir == ""


@pytest.mark.asyncio
async def test_calls_without_workdir_refresh_previous_association_not_server_cwd(
    registry, monkeypatch
):
    monkeypatch.setenv("BOOLEY_GOAL_MODE_PREVIEW", "1")
    registry.upsert(facts(), "sim", "completed", now=100)
    observer = SessionObserver(now=lambda: 101, budget=1)
    monkeypatch.setattr(observer, "_registry", lambda: registry)
    observed = await observer.attribution({}, {"threadId": "one"}, "codex")
    await observer.record(observed, "tools/list", "completed")
    row = registry.snapshot().rows[0]
    assert (observed.work_dir, observed.worktree_key) == ("/fixture/work", "repo:work")
    assert row.attribution.work_dir == "/fixture/work"
    assert row.calls[-1].tool == "tools/list"
    assert row.last_call_at == "1970-01-01T00:01:41Z"


@pytest.mark.asyncio
async def test_first_call_without_workdir_never_associates_server_cwd(registry, monkeypatch):
    monkeypatch.setenv("BOOLEY_GOAL_MODE_PREVIEW", "1")
    observer = SessionObserver(budget=1)
    monkeypatch.setattr(observer, "_registry", lambda: registry)
    observed = await observer.attribution({}, {"threadId": "one"}, "codex")
    await observer.record(observed, "tools/list", "completed")
    assert registry.snapshot().rows[0].attribution.work_dir == ""
    assert registry.snapshot().rows[0].attribution.worktree_key == ""


def test_path_rows_follow_repository_identity_after_first_goal_entry(tmp_path):
    from booley.goals.store import REPOSITORY_ID_FILE

    work = tmp_path / "work"
    git = work / ".git"
    git.mkdir(parents=True)
    (git / "HEAD").write_text("ref: refs/heads/goal/fixture\n")
    registry = SessionRegistry(tmp_path / "data")
    before = session_registry.resolve_attribution(work, metadata={}, client_name="")
    registry.upsert(before, "sim", "completed", now=100)
    identity_file = git / REPOSITORY_ID_FILE
    identity_file.parent.mkdir()
    identity_file.write_text("00000000-0000-4000-8000-000000000001\n")
    after = session_registry.resolve_attribution(work, metadata={}, client_name="")
    row = registry.snapshot().rows[0]
    assert after.worktree_key == "00000000-0000-4000-8000-000000000001/main"
    assert row.attribution.worktree_key == after.worktree_key
    assert row.attribution.key == after.key
    assert after.branch == "goal/fixture"


@pytest.mark.asyncio
async def test_busy_fallback_uses_same_worktree_key_and_namespace(tmp_path, monkeypatch):
    from booley.goals.store import REPOSITORY_ID_FILE

    monkeypatch.setenv("BOOLEY_GOAL_MODE_PREVIEW", "1")
    git = tmp_path / ".git"
    (git / REPOSITORY_ID_FILE).parent.mkdir(parents=True)
    (git / REPOSITORY_ID_FILE).write_text("00000000-0000-4000-8000-000000000001\n")
    (git / "HEAD").write_text("ref: refs/heads/goal/fixture\n")
    observer = SessionObserver()
    blocked = asyncio.get_running_loop().create_future()
    observer._pending = blocked
    fallback = await observer.attribution({"work_dir": str(tmp_path)}, {}, "")
    normal = session_registry.resolve_attribution(tmp_path, metadata={}, client_name="")
    blocked.set_result(None)
    assert (fallback.key, fallback.worktree_key, fallback.namespace) == (
        normal.key,
        normal.worktree_key,
        normal.namespace,
    )


@pytest.mark.asyncio
async def test_slow_peer_finishes_presence_after_response_budget(registry, monkeypatch, tmp_path):
    import threading

    from booley.mcp import session_observer

    monkeypatch.setenv("BOOLEY_GOAL_MODE_PREVIEW", "1")
    monkeypatch.setattr(session_registry, "namespace", lambda *_: SCOPE)
    release = threading.Event()
    entered = asyncio.Event()
    loop = asyncio.get_running_loop()

    def slow_peer(*_a):
        loop.call_soon_threadsafe(entered.set)
        assert release.wait(2)
        return IDENTITY

    monkeypatch.setattr(session_observer, "observed_peer", slow_peer)
    observer = SessionObserver(now=lambda: 100, budget=0.001)
    monkeypatch.setattr(observer, "_registry", lambda: registry)
    fallback = await observer.attribution({"work_dir": str(tmp_path)}, {}, "claude-code")
    await entered.wait()
    await observer.record(fallback, "sim", "completed")
    release.set()
    await asyncio.wait_for(observer._pending, 2)
    rows = registry.snapshot().rows
    assert len(rows) == 1
    assert rows[0].attribution.kind == "process"
    assert rows[0].calls[-1].outcome == "completed"


@pytest.mark.asyncio
async def test_pruning_does_not_block_request_attribution(registry, monkeypatch):
    monkeypatch.setenv("BOOLEY_GOAL_MODE_PREVIEW", "1")
    observer = SessionObserver(budget=1)
    blocked = asyncio.get_running_loop().create_future()
    observer._pending = blocked  # An existing request may continue while maintenance runs.
    monkeypatch.setattr(observer, "_registry", lambda: registry)
    registry.upsert(facts(), "sim", "completed", now=100)
    from booley.mcp import session_observer

    monkeypatch.setattr(session_observer, "namespace", lambda: SCOPE)
    monkeypatch.setattr(
        session_observer, "registered_worktree_presence", lambda _: lambda _f: True
    )
    await observer.maintain()
    blocked.set_result(None)
    assert registry.snapshot().rows == ()


def test_fallback_row_migration_preserves_started_time_and_one_visible_row(tmp_path):
    from booley.goals.store import REPOSITORY_ID_FILE

    work = tmp_path / "work"
    git = work / ".git"
    git.mkdir(parents=True)
    (git / "HEAD").write_text("ref: refs/heads/main\n")
    registry = SessionRegistry(tmp_path / "data")
    before = session_registry.resolve_attribution(work, metadata={}, client_name="")
    registry.upsert(before, "sim", "completed", now=100)
    (git / REPOSITORY_ID_FILE).parent.mkdir()
    (git / REPOSITORY_ID_FILE).write_text("00000000-0000-4000-8000-000000000001\n")
    after = session_registry.resolve_attribution(work, metadata={}, client_name="")
    registry.upsert(after, "sim", "completed", now=101)
    rows = registry.snapshot().rows
    assert len(rows) == 1
    assert rows[0].started_at == "1970-01-01T00:01:40Z"
    assert len(rows[0].calls) == 2
    assert not registry.path(before.key).exists()


@pytest.mark.asyncio
async def test_busy_call_cannot_overwrite_another_calls_deferred_metadata(
    registry, tmp_path, monkeypatch
):
    import threading

    from booley.mcp import session_observer

    monkeypatch.setenv("BOOLEY_GOAL_MODE_PREVIEW", "1")
    release = threading.Event()
    entered = asyncio.Event()
    loop = asyncio.get_running_loop()

    def slow_peer(*_):
        loop.call_soon_threadsafe(entered.set)
        assert release.wait(2)
        return IDENTITY

    monkeypatch.setattr(session_observer, "observed_peer", slow_peer)
    observer = SessionObserver(now=lambda: 100, budget=0.001)
    monkeypatch.setattr(observer, "_registry", lambda: registry)
    arguments = {"work_dir": str(tmp_path)}
    first = await observer.attribution(arguments, {}, "claude-code")
    await entered.wait()
    await observer.record(first, "first", "completed")
    second = await observer.attribution(arguments, {}, "claude-code")
    await observer.record(second, "second", "completed")
    release.set()
    await asyncio.wait_for(observer._pending, 2)
    assert registry.snapshot().rows[0].calls[-1].tool == "first"
