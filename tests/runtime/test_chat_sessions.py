"""One stable display session per launched tab, including multiplexed conversations."""

from pathlib import Path

import pytest
from tests.conftest import symlink_or_skip

from booley.mcp import session_observer, session_registry
from booley.mcp.session_observer import SessionObserver
from booley.mcp.session_registry import Attribution, SessionRegistry
from booley.runtime import chat_sessions
from booley.runtime.handles import HandleStore, session_writer
from booley.runtime.pid import ProcessIdentity
from booley.runtime.session_activity import process_session_key

SCOPE = "pid:[17]"


def process(
    proc: Path,
    pid: int,
    parent: int,
    *,
    start: int = 7,
    executable: str = "codex",
    args: tuple[str, ...] = (),
) -> ProcessIdentity:
    directory = proc / str(pid)
    (directory / "ns").mkdir(parents=True, exist_ok=True)
    if not (directory / "ns/pid").is_symlink():
        symlink_or_skip(directory / "ns/pid", Path(SCOPE))
    fields = ["S", str(parent), *["0"] * 17, str(start), "0"]
    (directory / "stat").write_text(f"{pid} (process with spaces) " + " ".join(fields))
    if not (directory / "exe").is_symlink():
        symlink_or_skip(directory / "exe", Path("/bin") / executable)
    (directory / "cmdline").write_bytes(b"\0".join(arg.encode() for arg in (executable, *args)))
    return ProcessIdentity(pid, SCOPE, start)


def test_two_tabs_carry_handles_without_allocating_subagent_ids(tmp_path):
    proc = tmp_path / "proc"
    first, second = (
        process(proc, 100, 1, executable="node"),
        process(proc, 200, 1, executable="node"),
    )
    first_peer, second_peer = process(proc, 101, 100), process(proc, 201, 200)
    assert chat_sessions.reserve_codex_chat(tmp_path, first) == "S1"
    assert chat_sessions.reserve_codex_chat(tmp_path, second) == "S2"
    registry = SessionRegistry(tmp_path, proc_root=proc)
    for key, peer in [("main", first_peer), ("child", first_peer), ("other", second_peer)]:
        facts = Attribution("codex:" + key, "thread", "", "", process=peer, namespace=SCOPE)
        registry.upsert(facts, "sim", "completed", now=100)
        resolved = registry.chat_attribution(facts)
        assert resolved.key == process_session_key(first if peer == first_peer else second)
        assert resolved.kind == "process"
    labels = HandleStore(tmp_path).load().labels()
    assert labels["codex:main"] == labels["codex:child"] == "S1"
    assert labels["codex:other"] == "S2"
    assert HandleStore(tmp_path).load().next_number == 3
    assert len(registry.snapshot().rows) == 2
    registry.backfill_handles()
    assert HandleStore(tmp_path).load().next_number == 3


def test_ordinary_codex_does_not_inherit_an_unmarked_ancestor_handle(tmp_path):
    proc = tmp_path / "proc"
    parent, peer = process(proc, 100, 1, executable="node"), process(proc, 101, 100)
    with session_writer(tmp_path):
        store = HandleStore(tmp_path)
        handles = store.load()
        handles.reserve(process_session_key(parent), "S")
        store.save(handles)
    registry = SessionRegistry(tmp_path, proc_root=proc)
    facts = Attribution("codex:direct", "thread", "", "", process=peer, namespace=SCOPE)
    assert registry.preserve_worktree(facts) == facts
    registry.upsert(facts, "sim", "completed", now=100)
    assert HandleStore(tmp_path).load().labels()[facts.key] == "S2"


def test_pid_reuse_unreadable_procfs_and_walk_bound_do_not_guess(tmp_path, monkeypatch):
    proc = tmp_path / "proc"
    parent, peer = process(proc, 100, 1, executable="node"), process(proc, 101, 100)
    chat_sessions.reserve_codex_chat(tmp_path, parent)
    assert chat_sessions.codex_chat_owner(tmp_path, peer, proc_root=proc) == parent
    monkeypatch.setattr(chat_sessions, "MAX_ANCESTORS", 1)
    assert chat_sessions.codex_chat_owner(tmp_path, peer, proc_root=proc) is None
    monkeypatch.setattr(chat_sessions, "MAX_ANCESTORS", 32)
    process(proc, 100, 1, start=8)
    assert chat_sessions.codex_chat_owner(tmp_path, peer, proc_root=proc) is None
    (proc / "101/stat").unlink()
    assert chat_sessions.codex_chat_owner(tmp_path, peer, proc_root=proc) is None


def test_existing_thread_allocation_is_not_renumbered_when_a_tab_takes_ownership(tmp_path):
    proc = tmp_path / "proc"
    owner = process(proc, 100, 1, executable="node")
    peer = process(proc, 101, 100)
    registry = SessionRegistry(tmp_path, proc_root=proc)
    facts = Attribution("codex:main", "thread", "", "", process=peer, namespace=SCOPE)
    registry.upsert(facts, "sim", "completed", now=100)
    assert chat_sessions.reserve_codex_chat(tmp_path, owner) == "S2"
    registry.upsert(facts, "sim", "completed", now=101)
    # Existing allocations are never renumbered by handle alias creation.
    assert HandleStore(tmp_path).load().labels()[facts.key] == "S1"
    assert [row.attribution.key for row in registry.snapshot().rows] == [
        process_session_key(owner)
    ]


def test_corrupt_handles_preserve_thread_presence_and_report_the_failure(tmp_path, caplog):
    proc = tmp_path / "proc"
    peer = process(proc, 101, 1)
    store = HandleStore(tmp_path)
    store.root.mkdir(parents=True)
    store.path.write_text("broken")
    registry = SessionRegistry(tmp_path, proc_root=proc)
    facts = Attribution("codex:main", "thread", "", "", process=peer, namespace=SCOPE)
    assert registry.chat_attribution(facts) == facts
    registry.upsert(facts, "sim", "completed", now=100)
    assert registry.snapshot().rows[0].attribution == facts
    assert "Session handle unavailable" in caplog.text


@pytest.mark.parametrize(
    "fault", ["invalid-parent", "self-parent", "missing-exe", "missing-parent"]
)
def test_unreadable_or_invalid_ancestry_cannot_confer_a_handle(tmp_path, fault):
    proc = tmp_path / "proc"
    owner = process(proc, 100, 1, executable="node")
    peer = process(proc, 101, 100)
    chat_sessions.reserve_codex_chat(tmp_path, owner)
    if fault in {"invalid-parent", "self-parent"}:
        fields = (proc / "101/stat").read_text().rsplit(")", 1)[1].split()
        fields[1] = "bad" if fault == "invalid-parent" else "101"
        (proc / "101/stat").write_text("101 (codex) " + " ".join(fields))
    elif fault == "missing-exe":
        (proc / "101/exe").unlink()
    else:
        (proc / "100/stat").unlink()
    assert chat_sessions.codex_chat_owner(tmp_path, peer, proc_root=proc) is None


@pytest.mark.parametrize("launcher", ["node", "codex"])
def test_nested_clients_cannot_claim_an_outer_tab_but_backend_helpers_can(tmp_path, launcher):
    proc = tmp_path / "proc"
    owner = process(proc, 100, 1, executable=launcher)
    main = process(proc, 101, 100) if launcher == "node" else owner
    backend = process(proc, 102, main.pid, args=("app-server", "--listen", "unix://"))
    process(proc, 200, backend.pid, executable="sh")
    nested = process(proc, 201, 200, args=("exec", "nested task"))
    chat_sessions.reserve_codex_chat(tmp_path, owner)
    assert chat_sessions.codex_chat_owner(tmp_path, main, proc_root=proc) == owner
    assert chat_sessions.codex_chat_owner(tmp_path, backend, proc_root=proc) == owner
    assert chat_sessions.codex_chat_owner(tmp_path, nested, proc_root=proc) is None


@pytest.mark.asyncio
@pytest.mark.parametrize("initial_peer_unavailable", [False, True])
async def test_observer_migrates_thread_rows_without_a_false_shared_warning(
    tmp_path,
    monkeypatch,
    initial_peer_unavailable,
):
    proc = tmp_path / "proc"
    owner = process(proc, 100, 1, executable="node")
    peer = process(proc, 101, 100)
    registry = SessionRegistry(tmp_path, proc_root=proc)
    observer = SessionObserver(now=lambda: 100, budget=1)
    monkeypatch.setattr(observer, "_registry", lambda: registry)
    monkeypatch.setattr(session_registry, "namespace", lambda *_args: SCOPE)
    monkeypatch.setattr(session_observer, "namespace", lambda: SCOPE)
    monkeypatch.setattr(
        session_observer,
        "observed_peer",
        lambda _request: None if initial_peer_unavailable else peer,
    )
    arguments, metadata = {"work_dir": str(tmp_path)}, {"threadId": "main"}
    if initial_peer_unavailable:
        chat_sessions.reserve_codex_chat(tmp_path, owner)
    await observer.attribution(arguments, metadata, "codex-mcp-client")
    assert registry.path("codex:main").exists()
    if not initial_peer_unavailable:
        chat_sessions.reserve_codex_chat(tmp_path, owner)
    monkeypatch.setattr(session_observer, "observed_peer", lambda _request: peer)
    try:
        facts = await observer.attribution(arguments, metadata, "codex-mcp-client")
        assert facts.key == process_session_key(owner)
        assert not registry.path("codex:main").exists()
        assert [row.attribution.key for row in registry.snapshot().rows] == [facts.key]
        assert len(registry.snapshot().rows[0].calls) == 2
        assert await observer.shared(facts) == ((facts.key,), "")
        # New threads get aliases without allocating their own display IDs.
        await observer.attribution(arguments, {"threadId": "child"}, "codex-mcp-client")
        assert (
            HandleStore(tmp_path).load().labels()["codex:child"]
            == HandleStore(tmp_path).load().labels()[facts.key]
        )
    finally:
        observer.close()
