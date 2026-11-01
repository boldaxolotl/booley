"""Argument-free ``booley_poll`` waits on the Jobs the calling agent started (#2538)."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest

try:
    from booley.mcp import server as mcp_server
    from booley.mcp.server import _dispatch_async_job, _dispatch_poll, _JobManager
except ImportError:
    pytest.skip("mcp package not installed", allow_module_level=True)

from booley.mcp.application import McpRequestContext
from booley.mcp.call_context import resolve_call_context
from booley.runtime import job_records as jobrec
from booley.runtime.session_paths import session_jobs_dir
from booley.runtime.timefmt import utc_now_rfc3339
from tests.mcp_tools.test_mcp_dispatch import _FakeLifetime, _run_id_from

SERVER = "mcp-stdio:developer:pid:test:1:1"


@pytest.fixture(autouse=True)
def _env(tmp_path, monkeypatch):
    monkeypatch.setenv("BOOLEY_LOGS_DIR", str(tmp_path / "logs"))
    monkeypatch.setenv("BOOLEY_RUNTIME_DIR", str(tmp_path))
    monkeypatch.setenv("BOOLEY_SLOTS_DIR", str(tmp_path / "jobs" / "slots"))
    monkeypatch.setattr(mcp_server, "_job_inline_wait_seconds", lambda: 0.0)
    monkeypatch.setattr(mcp_server.job_lifecycle, "_DISK_POLL_TICK_SECONDS", 0.05)
    monkeypatch.setattr(mcp_server, "is_pid_alive", lambda _pid: True)


def _caller(thread: str = "t1", *, session_key: str | None = None):
    """The caller of one agent tab: server identity plus its thread key."""
    from booley.mcp.job_ownership import Caller

    return Caller(f"{SERVER}|codex:{thread}", session_key or f"codex:{thread}")


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
        "pid": 4242,
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


def _gated(monkeypatch, gates: dict[str, asyncio.Event]) -> None:
    """Fake Job child: finishes once the gate named by its first argv word is set."""

    async def run(cmd, *, timeout, on_spawn=None, **_kwargs):
        if on_spawn:
            on_spawn(4242)
        await gates[cmd[0]].wait()
        return (0, f"OUT-{cmd[0]}", "", False)

    monkeypatch.setattr(mcp_server, "_run_subprocess", run)


async def _submit(jobs, tag: str, caller) -> str:
    out = await _dispatch_async_job(
        "sim", [tag], 60, jobs, context=resolve_call_context({}), caller=caller
    )
    return _run_id_from(_all_text(out))


async def _poll(jobs, caller, wait: float = 30, **arguments):
    return await asyncio.wait_for(
        _dispatch_poll({"wait_seconds": wait, **arguments}, jobs, caller), timeout=5
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


def test_several_unread_returns_earliest_and_lists_rest():
    me = _caller()
    _write("late", owner=me.owner, ended_at="2026-01-01T00:05:00Z")
    _write("early", owner=me.owner, ended_at="2026-01-01T00:01:00Z")

    text = _all_text(asyncio.run(_poll(_JobManager(_FakeLifetime()), me)))

    assert "Result of job early" in text
    assert "Also finished, not yet read" in text
    assert "late" in text.split("Also finished, not yet read", 1)[1]


def test_owned_poll_times_out_with_running_list(monkeypatch):
    gates = {"a": asyncio.Event()}
    _gated(monkeypatch, gates)

    async def scenario():
        jobs, me = _JobManager(_FakeLifetime()), _caller()
        run_a = await _submit(jobs, "a", me)
        text = _all_text(await _poll(jobs, me, wait=0.2))
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
            on_spawn(4242)
        return (0, "FAST", "", False)

    monkeypatch.setattr(mcp_server, "_run_subprocess", run)
    monkeypatch.setattr(mcp_server, "_job_inline_wait_seconds", lambda: 5.0)

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
    me = _caller("t1", session_key="worktree:k")
    other_tab = f"{SERVER}|codex:t2"
    nested = "mcp-stdio:nested:pid:test:2:2"
    _write("other-run", owner=other_tab, status=jobrec.STATUS_RUNNING)
    _write("other-done", owner=other_tab, ended_at="2026-01-01T00:01:00Z")
    _write("nested-run", owner=nested, status=jobrec.STATUS_RUNNING)
    _write("baseline", owner=None, endpoint="sim_baseline", session_key="worktree:k")

    out = _all_text(asyncio.run(_poll(_JobManager(_FakeLifetime()), me)))

    assert "NOTHING TO WAIT FOR" in out
    for run_id in ("other-run", "other-done", "nested-run", "baseline"):
        assert run_id not in out


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
    _gated(monkeypatch, gates)
    attribution = SimpleNamespace(key="codex:t1", kind="thread", worktree_key="")
    context = McpRequestContext(attribution=attribution)

    def caller_after_restart():
        return job_ownership.caller_for(job_ownership.server_identity(), context)

    async def scenario():
        run_id = await _submit(_JobManager(_FakeLifetime()), "a", caller_after_restart())
        restarted = _JobManager(_FakeLifetime())
        listed = _all_text(await _poll(restarted, caller_after_restart(), wait=0))
        monkeypatch.setattr(mcp_server, "is_pid_alive", lambda _pid: False)
        restarted.reconcile_orphaned()
        finished = _all_text(await _poll(restarted, caller_after_restart(), wait=0))
        return run_id, listed, finished, _read(run_id)

    run_id, listed, finished, stored = asyncio.run(scenario())
    assert run_id in listed
    assert f"Result of job {run_id}" in finished
    assert stored.owner == caller_after_restart().owner
    assert stored.read_at is not None


def test_http_server_passes_stable_owner(monkeypatch):
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

    import uvicorn

    monkeypatch.setattr(mcp_server, "_interactive_session_id", lambda: "box")
    monkeypatch.setattr(mcp_server, "_maybe_configure_interactive_logs_dir", lambda: None)
    monkeypatch.setattr(mcp_server, "_load_agent_settings_from_toml", lambda: None)
    monkeypatch.setattr(mcp_server, "_build_server", build)
    monkeypatch.setattr(mcp_server.handle_pruning, "prune_at_start", lambda: None)
    monkeypatch.setattr(mcp_server, "_streamable_http_app", lambda _server: object())
    monkeypatch.setattr(mcp_server, "SessionMaintenanceApp", lambda app, _sessions: app)
    monkeypatch.setattr(uvicorn, "run", lambda *_args, **_kwargs: None)

    mcp_server._run_http(8765)

    assert seen["identity"] == job_ownership.http_server_identity("box", 8765)


def test_legacy_record_falls_back_to_session_key():
    _write("old-running", owner=None, status=jobrec.STATUS_RUNNING, session_key="worktree:k")
    jobs = _JobManager(_FakeLifetime())

    mine = _all_text(asyncio.run(_poll(jobs, _caller(session_key="worktree:k"), wait=0)))
    theirs = _all_text(asyncio.run(_poll(jobs, _caller(session_key="worktree:z"), wait=0)))
    by_id = _all_text(asyncio.run(_poll(jobs, _caller(), run_id="old-running", wait=0)))

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
    done = _all_text(asyncio.run(_poll(jobs, _caller(session_key="worktree:k"), wait=0)))
    assert "Result of job old-running" in done


def test_legacy_finished_before_start_not_returned():
    _write("ancient", owner=None, session_key="worktree:k", ended_at="2020-01-01T00:00:00Z")

    out = _all_text(
        asyncio.run(_poll(_JobManager(_FakeLifetime()), _caller(session_key="worktree:k")))
    )

    assert "NOTHING TO WAIT FOR" in out
    assert "ancient" not in out


def test_run_id_poll_of_unowned_job_unchanged():
    _write("legacy-1", owner=None, ended_at="2026-01-01T00:01:00Z")

    out = _all_text(asyncio.run(_poll(_JobManager(_FakeLifetime()), _caller(), run_id="legacy-1")))

    assert "EXIT_CODE: 0" in out
