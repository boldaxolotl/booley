"""Argument-free ``booley_poll`` waits on the Jobs the calling agent started (#2538)."""

from __future__ import annotations

import asyncio
import json
import logging
import socket
from types import SimpleNamespace

import pytest

try:
    from booley.mcp import server as mcp_server
    from booley.mcp.server import _dispatch_async_job, _dispatch_poll, _JobManager
except ImportError:
    pytest.skip("mcp package not installed", allow_module_level=True)

from booley.mcp.agent_thread import AgentThread
from booley.mcp.application import McpRequestContext
from booley.mcp.call_context import resolve_call_context
from booley.runtime import job_records as jobrec
from booley.runtime.session_paths import session_jobs_dir
from booley.runtime.timefmt import utc_now_rfc3339
from tests.mcp_tools.test_mcp_dispatch import _FakeLifetime, _run_id_from
from tests.time_budget import hang_guard_s, scaled
from tests.uvicorn_support import capture_serving

SERVER = "mcp-stdio:developer:pid:test:1:1"

# PID the fake Job child reports. One above Linux PID_MAX_LIMIT (2**22), so no
# live process owns it: the argv-identity check in ``jobrec.derive_status`` reads
# the real /proc/<pid>/cmdline and finds nothing, instead of judging the fake
# child "recycled" because an unrelated host process holds the PID (#3295).
_FAKE_CHILD_PID = 4_194_305

# Poll window for tests that only need the poll to return once a gate opens.
_POLL_WAIT_S = hang_guard_s()


@pytest.fixture(autouse=True)
def _env(tmp_path, monkeypatch):
    monkeypatch.setenv("BOOLEY_LOGS_DIR", str(tmp_path / "logs"))
    monkeypatch.setenv("BOOLEY_RUNTIME_DIR", str(tmp_path))
    monkeypatch.setenv("BOOLEY_SLOTS_DIR", str(tmp_path / "jobs" / "slots"))
    monkeypatch.setattr(mcp_server, "_job_inline_wait_seconds", lambda: 0.0)
    monkeypatch.setattr(mcp_server.job_lifecycle, "_DISK_POLL_TICK_SECONDS", 0.05)
    monkeypatch.setattr(mcp_server, "is_pid_alive", lambda _pid: True)


def _caller(thread: str = "t1"):
    """The caller of one agent tab: server identity plus its thread key."""
    from booley.mcp.job_ownership import Caller

    return Caller(f"{SERVER}|codex:{thread}")


def _all_text(result) -> str:
    value = result.value if isinstance(result, mcp_server.McpDispatchResult) else result
    blocks = value[0] if isinstance(value, tuple) else value
    return "\n".join(block.text for block in blocks)


def _is_error(result) -> bool:
    return isinstance(result, mcp_server.McpDispatchResult) and result.is_error


def _write(run_id: str, *, owner: str | None, status: str = jobrec.STATUS_DONE, **fields) -> str:
    """Persist one Job record in the container jobs root."""
    defaults = {
        "endpoint": "sim",
        "started_at": utc_now_rfc3339(),
        "timeout_s": 60,
        "pid": _FAKE_CHILD_PID,
        "exit_code": 0,
    }
    jobrec.write_record(
        jobrec.JobRecord(run_id=run_id, owner=owner, status=status, **{**defaults, **fields}),
        root=session_jobs_dir(),
    )
    return run_id


def _read(run_id: str) -> jobrec.JobRecord:
    rec = jobrec.read_record(run_id, session_jobs_dir())
    assert rec is not None
    return rec


def _gated(
    monkeypatch, gates: dict[str, asyncio.Event], spawned: asyncio.Event | None = None
) -> None:
    """Fake Job child: finishes once the gate named by its first argv word is set.

    ``spawned``, when given, is set right after the child reports its PID, so a
    test can wait for the PID to be persisted instead of relying on loop order.
    """

    async def run(cmd, *, timeout, on_spawn=None, **_kwargs):
        if on_spawn:
            on_spawn(_FAKE_CHILD_PID)
        if spawned is not None:
            spawned.set()
        await gates[cmd[0]].wait()
        return (0, f"OUT-{cmd[0]}", "", False)

    monkeypatch.setattr(mcp_server, "_run_subprocess", run)


async def _submit(jobs, tag: str, caller) -> str:
    out = await _dispatch_async_job(
        "sim", [tag], 60, jobs, context=resolve_call_context({}), caller=caller
    )
    return _run_id_from(_all_text(out))


async def _poll(jobs, caller, wait: float = _POLL_WAIT_S, **arguments):
    return await asyncio.wait_for(
        _dispatch_poll({"wait_seconds": wait, **arguments}, jobs, caller),
        timeout=2 * hang_guard_s(),
    )


def test_first_finished_owned_job_returned_with_other_listed(monkeypatch):
    gates = {"a": asyncio.Event(), "b": asyncio.Event()}
    _gated(monkeypatch, gates)

    async def scenario():
        jobs, me = _JobManager(_FakeLifetime()), _caller()
        run_a, run_b = await _submit(jobs, "a", me), await _submit(jobs, "b", me)
        waiting = asyncio.create_task(_poll(jobs, me))
        await asyncio.sleep(0.1)
        gates["b"].set()
        first = _all_text(await waiting)
        gates["a"].set()
        second = _all_text(await _poll(jobs, me))
        return run_a, run_b, first, second

    run_a, run_b, first, second = asyncio.run(scenario())
    assert f"Result of job {run_b}" in first
    assert "EXIT_CODE: 0" in first
    assert "OUT-b" in first
    assert "Still running" in first
    assert run_a in first
    assert "OUT-a" not in first
    assert "OUT-a" in second


def test_several_unread_returns_earliest_without_listing_rest():
    me = _caller()
    _write("late", owner=me.owner, ended_at="2026-01-01T00:05:00Z")
    _write("early", owner=me.owner, ended_at="2026-01-01T00:01:00Z")

    text = _all_text(asyncio.run(_poll(_JobManager(_FakeLifetime()), me)))

    assert "Result of job early" in text
    # The unread notice that ends every tool response lists the rest (#2539).
    assert "Also finished, not yet read" not in text
    assert "late" not in text


def test_owned_poll_times_out_with_running_list(monkeypatch):
    gates = {"a": asyncio.Event()}
    _gated(monkeypatch, gates)

    async def scenario():
        jobs, me = _JobManager(_FakeLifetime()), _caller()
        run_a = await _submit(jobs, "a", me)
        text = _all_text(await _poll(jobs, me, wait=scaled(0.2)))
        gates["a"].set()
        return run_a, text

    run_a, text = asyncio.run(scenario())
    assert "RUNNING" in text
    assert run_a in text
    assert "no run_id" in text


def test_unread_finished_job_returns_immediately():
    me = _caller()
    _write("done-1", owner=me.owner, ended_at="2026-01-01T00:01:00Z")
    jobs = _JobManager(_FakeLifetime())

    first = _all_text(asyncio.run(_poll(jobs, me, wait=270)))
    stamped = _read("done-1").read_at
    again = _all_text(asyncio.run(_poll(jobs, me, wait=270)))

    assert "Result of job done-1" in first
    assert stamped is not None
    assert "NOTHING TO WAIT FOR" in again
    assert _read("done-1").read_at == stamped


def test_nothing_to_wait_for_returns_immediately():
    out = asyncio.run(_poll(_JobManager(_FakeLifetime()), _caller(), wait=270))

    assert "NOTHING TO WAIT FOR" in _all_text(out)
    assert not _is_error(out)


def test_inline_completion_counts_as_read(monkeypatch):
    async def run(cmd, *, timeout, on_spawn=None, **_kwargs):
        if on_spawn:
            on_spawn(_FAKE_CHILD_PID)
        return (0, "FAST", "", False)

    monkeypatch.setattr(mcp_server, "_run_subprocess", run)
    monkeypatch.setattr(mcp_server, "_job_inline_wait_seconds", hang_guard_s)

    async def scenario():
        jobs, me = _JobManager(_FakeLifetime()), _caller()
        inline = _all_text(
            await _dispatch_async_job(
                "sim", ["x"], 60, jobs, context=resolve_call_context({}), caller=me
            )
        )
        return inline, _all_text(await _poll(jobs, me, wait=0))

    inline, text = asyncio.run(scenario())
    assert "FAST" in inline
    assert "NOTHING TO WAIT FOR" in text
    (rec,) = jobrec.list_records(session_jobs_dir())
    assert rec.read_at is not None


def test_foreign_and_nested_jobs_never_returned_or_listed():
    me = _caller("t1")
    other_tab = f"{SERVER}|codex:t2"
    nested = "mcp-stdio:nested:pid:test:2:2"
    _write("other-run", owner=other_tab, status=jobrec.STATUS_RUNNING)
    _write("other-done", owner=other_tab, ended_at="2026-01-01T00:01:00Z")
    _write("nested-run", owner=nested, status=jobrec.STATUS_RUNNING)
    _write("baseline", owner=None, endpoint="sim_baseline")

    out = _all_text(asyncio.run(_poll(_JobManager(_FakeLifetime()), me)))

    assert "NOTHING TO WAIT FOR" in out
    for run_id in ("other-run", "other-done", "nested-run", "baseline"):
        assert run_id not in out


def test_job_owner_separates_tabs_and_scopes(monkeypatch):
    from booley.mcp import job_ownership

    tab_1 = job_ownership.job_owner(SERVER, AgentThread("codex", "t1"))
    tab_2 = job_ownership.job_owner(SERVER, AgentThread("codex", "t2"))
    claude = job_ownership.job_owner(SERVER, AgentThread("claude", "t1"))
    assert len({tab_1, tab_2, claude, SERVER}) == 4
    assert job_ownership.job_owner(SERVER, AgentThread("claude", None)) == SERVER
    assert job_ownership.job_owner(SERVER, None) == SERVER

    developer = job_ownership.server_identity()
    monkeypatch.setenv("BOOLEY_NESTED_AGENT", "1")
    assert job_ownership.server_identity() != developer
    monkeypatch.setattr(job_ownership.os, "getppid", lambda: 0)
    assert ":self:" in job_ownership.server_identity()


def test_non_owner_read_keeps_owner_unread():
    me, other = _caller("t1"), _caller("t2")
    _write("mine", owner=me.owner, ended_at="2026-01-01T00:01:00Z")
    jobs = _JobManager(_FakeLifetime())

    seen = _all_text(asyncio.run(_poll(jobs, other, run_id="mine")))
    unread = _read("mine").read_at
    owned = _all_text(asyncio.run(_poll(jobs, me)))

    assert "EXIT_CODE: 0" in seen
    assert unread is None
    assert "Result of job mine" in owned
    assert _read("mine").read_at is not None


def test_adopted_job_keeps_owner_across_restart(monkeypatch):
    from booley.mcp import job_ownership

    gates = {"a": asyncio.Event()}
    spawned = asyncio.Event()
    _gated(monkeypatch, gates, spawned)
    context = McpRequestContext(agent=AgentThread("codex", "t1"))

    def caller_after_restart():
        return job_ownership.caller_for(job_ownership.server_identity(), context)

    # Orderings this test controls explicitly (no event-loop or timing luck):
    #  1. the first manager's child has spawned and its PID is on disk before
    #     the "restart" (Python 3.11 and 3.12+ schedule the job task differently);
    #  2. the fake child is alive for the first poll and dead for the second.
    async def scenario():
        run_id = await _submit(_JobManager(_FakeLifetime()), "a", caller_after_restart())
        await asyncio.wait_for(spawned.wait(), timeout=hang_guard_s())
        before_restart = _read(run_id)
        restarted = _JobManager(_FakeLifetime())
        listed = _all_text(await _poll(restarted, caller_after_restart(), wait=0))
        unread_before_reconcile = _read(run_id).read_at is None
        monkeypatch.setattr(mcp_server, "is_pid_alive", lambda _pid: False)
        restarted.reconcile_orphaned()
        finished = _all_text(await _poll(restarted, caller_after_restart(), wait=0))
        return run_id, before_restart, listed, unread_before_reconcile, finished, _read(run_id)

    run_id, before_restart, listed, unread_before_reconcile, finished, stored = asyncio.run(
        scenario()
    )
    assert before_restart.pid == _FAKE_CHILD_PID
    assert before_restart.status == jobrec.STATUS_RUNNING
    # Listed as running, not handed back early as a result of a "recycled" PID.
    assert listed.startswith("RUNNING:")
    assert run_id in listed
    assert unread_before_reconcile
    assert f"Result of job {run_id}" in finished
    assert stored.owner == caller_after_restart().owner
    assert stored.read_at is not None


@pytest.mark.parametrize("client", ["claude", "codex"])
def test_argument_free_poll_finds_the_threads_jobs(monkeypatch, client):
    from booley.mcp.job_ownership import caller_for

    def caller(thread: str):
        return caller_for(SERVER, McpRequestContext(agent=AgentThread(client, thread)))

    gates = {"a": asyncio.Event()}
    gates["a"].set()
    _gated(monkeypatch, gates)

    async def scenario():
        jobs = _JobManager(_FakeLifetime())
        run_id = await _submit(jobs, "a", caller("th-1"))
        other_thread = _all_text(await _poll(jobs, caller("th-2")))
        return run_id, other_thread, _all_text(await _poll(jobs, caller("th-1")))

    run_id, other_thread, same_thread = asyncio.run(scenario())
    assert "NOTHING TO WAIT FOR" in other_thread and run_id not in other_thread
    assert f"Result of job {run_id}" in same_thread
    assert (_read(run_id).client, _read(run_id).thread) == (client, "th-1")


def _stub_http_server(monkeypatch, build=None):
    """Stub everything but the bind and announcement of ``_run_http``; return the served list."""
    monkeypatch.setattr(mcp_server, "_interactive_session_id", lambda: "box")
    monkeypatch.setattr(mcp_server, "_maybe_configure_job_logs_dir", lambda: None)
    monkeypatch.setattr(mcp_server, "_load_agent_settings_from_toml", lambda: None)
    monkeypatch.setattr(mcp_server, "_build_server", build or (lambda *_a, **_k: (object(), [])))
    monkeypatch.setattr(mcp_server.handle_pruning, "prune_at_start", lambda: None)
    monkeypatch.setattr(mcp_server, "_streamable_http_app", lambda _server: object())
    return capture_serving(monkeypatch)


def test_http_server_passes_stable_owner(monkeypatch, caplog):
    from booley.mcp import job_ownership

    assert job_ownership.http_server_identity("box", 8765) == job_ownership.http_server_identity(
        "box", 8765
    )
    assert job_ownership.http_server_identity("box", 8765) != job_ownership.http_server_identity(
        "box", 8766
    )

    seen: dict[str, object] = {}

    def build(lifetime, *, server_identity=None):
        seen["identity"] = server_identity
        return object(), []

    served = _stub_http_server(monkeypatch, build)

    with caplog.at_level(logging.INFO, logger="booley.mcp.server"):
        mcp_server._run_http(0)

    assert seen["identity"] == job_ownership.http_server_identity("box", 0)
    ((_app, (_host, port)),) = served
    assert port != 0
    assert f"Interactive MCP server (HTTP) on 127.0.0.1:{port}" in caplog.messages


def test_http_server_on_a_held_port_exits_2_without_announcing(monkeypatch, caplog, capsys):
    served = _stub_http_server(monkeypatch)
    with socket.socket() as holder:
        holder.bind(("127.0.0.1", 0))
        holder.listen()
        port = holder.getsockname()[1]

        with (
            caplog.at_level(logging.INFO, logger="booley.mcp.server"),
            pytest.raises(SystemExit) as stop,
        ):
            mcp_server._run_http(port)

    assert stop.value.code == 2
    err = capsys.readouterr().err
    assert f"127.0.0.1:{port}" in err
    assert "--port" in err
    assert "Interactive MCP server" not in caplog.text
    assert served == []


def _write_pre_owner(**fields) -> None:
    """A record as written before owners existed, still carrying the retired caller key."""
    _write("old-job", owner=None, **fields)
    path = session_jobs_dir() / "old-job.json"
    record = json.loads(path.read_bytes())
    path.write_bytes(json.dumps({**record, "session_key": "worktree:k"}).encode())


def test_record_without_owner_is_reached_only_by_run_id():
    from booley.mcp.job_ownership import caller_for

    # The request presents the same retired key as the record; it must not match them.
    request = SimpleNamespace(agent=None, attribution=None, session_key=lambda: "worktree:k")
    me = caller_for(SERVER, request)
    jobs = _JobManager(_FakeLifetime())
    _write_pre_owner(status=jobrec.STATUS_RUNNING)

    running = _all_text(asyncio.run(_poll(jobs, me, wait=0)))
    by_id = _all_text(asyncio.run(_poll(jobs, me, run_id="old-job", wait=0)))
    _write_pre_owner(ended_at=utc_now_rfc3339())
    done = _all_text(asyncio.run(_poll(jobs, me, wait=0)))

    assert "NOTHING TO WAIT FOR" in running
    assert "old-job" in by_id
    assert "NOTHING TO WAIT FOR" in done


def test_run_id_poll_of_unowned_job_unchanged():
    _write("legacy-1", owner=None, ended_at="2026-01-01T00:01:00Z")

    out = _all_text(asyncio.run(_poll(_JobManager(_FakeLifetime()), _caller(), run_id="legacy-1")))

    assert "EXIT_CODE: 0" in out
