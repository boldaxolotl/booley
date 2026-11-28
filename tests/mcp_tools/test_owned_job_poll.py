"""Argument-free ``booley_poll`` waits on the Jobs the calling agent started (#2538)."""

from __future__ import annotations

import asyncio
import logging
import socket

import pytest

try:
    from booley.mcp import server as mcp_server

    # Private seam: ``_poll`` below serves importers that drive their own ``_JobManager``.
    from booley.mcp.server import _dispatch_poll
except ImportError:
    pytest.skip("mcp package not installed", allow_module_level=True)

from booley.mcp import job_ownership
from booley.mcp.application import McpApplication, McpRequestContext, McpToolPayload
from booley.mcp.job_lifecycle import JobLifecycle
from booley.mcp.session_registry import Attribution
from booley.runtime import job_records as jobrec
from booley.runtime.session_paths import session_jobs_dir
from booley.runtime.timefmt import utc_now_rfc3339
from tests import mcp_runner_fake
from tests.mcp_tools.test_mcp_dispatch import _run_id_from
from tests.time_budget import hang_guard_s
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
    monkeypatch.setenv("BOOLEY_MCP_JOB_INLINE_WAIT_SECONDS", "0")  # detach at once
    # Private seam: no environment knob, and an adopted-Job poll would wait 5 s per tick.
    monkeypatch.setattr(mcp_server.job_lifecycle, "_DISK_POLL_TICK_SECONDS", 0.05)
    monkeypatch.setattr(mcp_server, "is_pid_alive", lambda _pid: True)  # the fake PID


def _caller(thread: str = "t1", *, session_key: str | None = None):
    """The caller of one agent tab: server identity plus its thread key."""
    return job_ownership.Caller(f"{SERVER}|codex:{thread}", session_key or f"codex:{thread}")


def _all_text(result) -> str:
    value = result.value if isinstance(result, mcp_server.McpDispatchResult) else result
    blocks = value[0] if isinstance(value, tuple) else value
    return "\n".join(block.text for block in blocks)


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


def _app(monkeypatch, tmp_path) -> McpApplication:
    """The real application; Job records land in its ``session_jobs_dir()``."""
    return mcp_runner_fake.application(monkeypatch, tmp_path)


def _context(
    key: str = "codex:t1", kind: str = "thread", worktree_key: str = ""
) -> McpRequestContext:
    return McpRequestContext(attribution=Attribution(key, kind, "/w", worktree_key))


def _owner(context: McpRequestContext) -> str:
    """The owner the application stamps for *context* (it derives the same identity)."""
    return job_ownership.caller_for(job_ownership.server_identity(), context).owner


def _text(payload: McpToolPayload) -> str:
    return "\n".join(block.text for block in payload.content)


def _gated(
    monkeypatch, gates: dict[str, asyncio.Event], spawned: asyncio.Event | None = None
) -> None:
    """Fake Job child: finishes once the gate named by its ``--target`` is set.

    ``spawned``, when given, is set right after the child reports its PID, so a
    test can wait for the PID to be persisted instead of relying on loop order.
    """

    async def run(cmd, *, on_spawn, **_kwargs):
        on_spawn(_FAKE_CHILD_PID)
        if spawned is not None:
            spawned.set()
        target = cmd[cmd.index("--target") + 1]
        await gates[target].wait()
        return (0, f"OUT-{target}", "", False)

    mcp_runner_fake.install(monkeypatch, run)


async def _submit(app: McpApplication, tag: str, context: McpRequestContext) -> str:
    out = await app.call_tool("sim", {"target": tag}, request_context=context)
    return _run_id_from(_text(out))


async def _booley_poll(
    app: McpApplication, context: McpRequestContext, wait: float = _POLL_WAIT_S, **arguments
) -> McpToolPayload:
    return await asyncio.wait_for(
        app.call_tool("booley_poll", {"wait_seconds": wait, **arguments}, request_context=context),
        timeout=2 * hang_guard_s(),
    )


async def _poll(jobs, caller, wait: float = _POLL_WAIT_S, **arguments):
    """Poll *jobs*, an importer's own ``_JobManager``: no public call renders that."""
    return await asyncio.wait_for(
        _dispatch_poll({"wait_seconds": wait, **arguments}, jobs, caller),
        timeout=2 * hang_guard_s(),
    )


def test_first_finished_owned_job_returned_with_other_listed(monkeypatch, tmp_path):
    gates = {"a": asyncio.Event(), "b": asyncio.Event()}
    _gated(monkeypatch, gates)
    app, me = _app(monkeypatch, tmp_path), _context()

    async def scenario():
        run_a, run_b = await _submit(app, "a", me), await _submit(app, "b", me)
        waiting = asyncio.create_task(_booley_poll(app, me))
        await asyncio.sleep(0.1)
        gates["b"].set()
        first = _text(await waiting)
        gates["a"].set()
        second = _text(await _booley_poll(app, me))
        return run_a, run_b, first, second

    run_a, run_b, first, second = asyncio.run(scenario())
    assert f"Result of job {run_b}" in first
    assert "EXIT_CODE: 0" in first
    assert "OUT-b" in first
    assert "Still running" in first
    assert run_a in first
    assert "OUT-a" not in first
    assert "OUT-a" in second


def test_owned_poll_times_out_with_running_list(monkeypatch, tmp_path):
    gates = {"a": asyncio.Event()}
    _gated(monkeypatch, gates)
    app, me = _app(monkeypatch, tmp_path), _context()

    async def scenario():
        run_a = await _submit(app, "a", me)
        text = _text(await _booley_poll(app, me, wait=1))  # the schema takes whole seconds
        gates["a"].set()
        return run_a, text

    run_a, text = asyncio.run(scenario())
    assert "RUNNING" in text
    assert run_a in text
    assert "no run_id" in text


def test_unread_finished_job_returns_immediately(monkeypatch, tmp_path):
    app, me = _app(monkeypatch, tmp_path), _context()
    _write("done-1", owner=_owner(me), ended_at="2026-01-01T00:01:00Z")

    first = _text(asyncio.run(_booley_poll(app, me, wait=270)))
    stamped = _read("done-1").read_at
    again = asyncio.run(_booley_poll(app, me, wait=270))

    assert "Result of job done-1" in first
    assert stamped is not None
    assert "NOTHING TO WAIT FOR" in _text(again)
    assert not again.is_error
    assert _read("done-1").read_at == stamped


def test_inline_completion_counts_as_read(monkeypatch, tmp_path):
    async def run(cmd, *, on_spawn, **_kwargs):
        on_spawn(_FAKE_CHILD_PID)
        return (0, "FAST", "", False)

    mcp_runner_fake.install(monkeypatch, run)
    monkeypatch.setenv("BOOLEY_MCP_JOB_INLINE_WAIT_SECONDS", str(hang_guard_s()))
    app, me = _app(monkeypatch, tmp_path), _context()

    async def scenario():
        inline = _text(await app.call_tool("sim", {"target": "x"}, request_context=me))
        return inline, _text(await _booley_poll(app, me, wait=0))

    inline, text = asyncio.run(scenario())
    assert "FAST" in inline
    assert "NOTHING TO WAIT FOR" in text
    (rec,) = jobrec.list_records(session_jobs_dir())
    assert rec.read_at is not None


def test_job_owner_separates_tabs_and_scopes(monkeypatch):
    from booley.mcp import job_ownership
    from booley.mcp.session_registry import Attribution

    def attribution(key: str, kind: str) -> Attribution:
        return Attribution(key=key, kind=kind, work_dir="/w", worktree_key="wk")

    tab_1 = job_ownership.job_owner(SERVER, attribution("codex:t1", "thread"))
    tab_2 = job_ownership.job_owner(SERVER, attribution("codex:t2", "thread"))
    claude = job_ownership.job_owner(SERVER, attribution("pid:ns:7:9", "process"))
    assert len({tab_1, tab_2, claude, SERVER}) == 4
    assert job_ownership.job_owner(SERVER, attribution("wk", "worktree")) == SERVER
    assert job_ownership.job_owner(SERVER, None) == SERVER

    developer = job_ownership.server_identity()
    monkeypatch.setenv("BOOLEY_NESTED_AGENT", "1")
    assert job_ownership.server_identity() != developer
    monkeypatch.setattr(job_ownership.os, "getppid", lambda: 0)
    assert ":self:" in job_ownership.server_identity()


def test_non_owner_read_keeps_owner_unread(monkeypatch, tmp_path):
    app, me, other = _app(monkeypatch, tmp_path), _context(), _context("codex:t2")
    _write("mine", owner=_owner(me), ended_at="2026-01-01T00:01:00Z")

    seen = _text(asyncio.run(_booley_poll(app, other, run_id="mine")))
    unread = _read("mine").read_at
    owned = _text(asyncio.run(_booley_poll(app, me)))

    assert "EXIT_CODE: 0" in seen
    assert unread is None
    assert "Result of job mine" in owned
    assert _read("mine").read_at is not None


def test_adopted_job_keeps_owner_across_restart(monkeypatch, tmp_path):
    gates = {"a": asyncio.Event()}
    spawned = asyncio.Event()
    _gated(monkeypatch, gates, spawned)
    me = _context()
    first_server = _app(monkeypatch, tmp_path)

    # Orderings this test controls explicitly (no event-loop or timing luck):
    #  1. the first server's child has spawned and its PID is on disk before the
    #     "restart" (Python 3.11 and 3.12+ schedule the job task differently);
    #  2. the fake child is alive for the first poll and dead at reconciliation,
    #     which reads the real process table.
    async def scenario():
        run_id = await _submit(first_server, "a", me)
        await asyncio.wait_for(spawned.wait(), timeout=hang_guard_s())
        before_restart = _read(run_id)
        restarted = _app(monkeypatch, tmp_path)
        listed = _text(await _booley_poll(restarted, me, wait=0))
        unread_before_reconcile = _read(run_id).read_at is None
        JobLifecycle().reconcile_orphaned()
        finished = _text(await _booley_poll(restarted, me, wait=0))
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
    assert stored.owner == _owner(me)
    assert stored.read_at is not None


def _stub_http_server(monkeypatch, build=None):
    """Stub everything but the bind and announcement of ``_run_http``; return the served list."""
    # Private seams: ``_run_http`` is the HTTP entry ``main()`` calls only after the
    # container, proxy and scratch guards, so no public call reaches it alone.
    monkeypatch.setattr(mcp_server, "_interactive_session_id", lambda: "box")
    monkeypatch.setattr(mcp_server, "_maybe_configure_job_logs_dir", lambda: None)
    monkeypatch.setattr(mcp_server, "_load_agent_settings_from_toml", lambda: None)
    monkeypatch.setattr(mcp_server, "_build_server", build or (lambda *_a, **_k: (object(), [])))
    monkeypatch.setattr(mcp_server.handle_pruning, "prune_at_start", lambda: None)
    monkeypatch.setattr(mcp_server, "_streamable_http_app", lambda _server: object())
    monkeypatch.setattr(mcp_server, "SessionMaintenanceApp", lambda app, _sessions: app)
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
        mcp_server._run_http(0)  # private: see _stub_http_server

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
            mcp_server._run_http(port)  # private: see _stub_http_server

    assert stop.value.code == 2
    err = capsys.readouterr().err
    assert f"127.0.0.1:{port}" in err
    assert "--port" in err
    assert "Interactive MCP server" not in caplog.text
    assert served == []


def test_legacy_record_falls_back_to_session_key(monkeypatch, tmp_path):
    app = _app(monkeypatch, tmp_path)
    mine_k = _context("worktree:k", "worktree", "k")
    theirs_z = _context("worktree:z", "worktree", "z")
    _write("old-running", owner=None, status=jobrec.STATUS_RUNNING, session_key="worktree:k")

    mine = _text(asyncio.run(_booley_poll(app, mine_k, wait=0)))
    theirs = _text(asyncio.run(_booley_poll(app, theirs_z, wait=0)))
    by_id = _text(asyncio.run(_booley_poll(app, _context(), run_id="old-running", wait=0)))

    assert "old-running" in mine
    assert "NOTHING TO WAIT FOR" in theirs
    assert "old-running" in by_id

    _write(
        "old-running",
        owner=None,
        exit_code=0,
        session_key="worktree:k",
        ended_at=utc_now_rfc3339(),
    )
    done = _text(asyncio.run(_booley_poll(app, mine_k, wait=0)))
    assert "Result of job old-running" in done
    by_id = _text(asyncio.run(_booley_poll(app, _context(), run_id="old-running", wait=0)))
    assert "EXIT_CODE: 0" in by_id
