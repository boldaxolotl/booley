"""The per-call context resolves exactly what the server's scattered reads did."""

from __future__ import annotations

import asyncio
import collections
import dataclasses
import json
import os
from pathlib import Path
from types import MappingProxyType

import pytest

from booley.mcp.call_context import CallContext, resolve_call_context, resolve_work_dir
from booley.runtime import job_records as jobrec
from booley.ticket_board.paths import session_jobs_dir

try:
    from booley.mcp import server as mcp_server
except ImportError:
    pytest.skip("mcp package not installed", allow_module_level=True)

_CALL_ENV = ("BOOLEY_LOGS_DIR", "BOOLEY_RUNTIME_DIR", "BOOLEY_STATE_FILE")


@pytest.fixture
def bare_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Server environment with none of the call variables set, cwd at *tmp_path*."""
    for name in _CALL_ENV:
        monkeypatch.delenv(name, raising=False)
    monkeypatch.chdir(tmp_path)
    return tmp_path


def _deleted_cwd() -> Path:
    raise FileNotFoundError("server cwd was deleted")


class _Lifetime:
    """Minimal stand-in for the server lifetime a job manager holds busy."""

    def mark_mcp_endpoint_start(self) -> None:
        pass

    def mark_mcp_endpoint_end(self) -> None:
        pass


def _old_state_path() -> Path | None:
    """The read ``_ticket_baseline_required`` made before the call context."""
    raw = os.environ.get("BOOLEY_STATE_FILE")
    return Path(raw) if raw else None


class TestWorkDir:
    def test_omitted_work_dir_is_the_server_cwd(self, bare_env: Path) -> None:
        arguments: dict[str, object] = {}

        context = resolve_call_context(arguments)

        assert context.work_dir == Path(str(arguments.get("work_dir") or Path.cwd()))
        assert context.work_dir == bare_env
        assert resolve_work_dir(arguments) == context.work_dir

    @pytest.mark.parametrize("value", [None, ""])
    def test_empty_work_dir_falls_back_to_cwd(self, bare_env: Path, value: object) -> None:
        assert resolve_call_context({"work_dir": value}).work_dir == bare_env

    def test_explicit_work_dir_is_used_verbatim(self, bare_env: Path, tmp_path: Path) -> None:
        raw = str(tmp_path / "wt")
        arguments = {"work_dir": raw}

        context = resolve_call_context(arguments)

        work_dir_raw = arguments.get("work_dir")
        assert context.work_dir == (Path(work_dir_raw) if work_dir_raw else Path.cwd())
        assert context.work_dir == Path(raw)

    def test_relative_work_dir_stays_relative(self, bare_env: Path) -> None:
        # The old reads passed the raw string to Path; resolution was the reader's job.
        assert resolve_call_context({"work_dir": "wt"}).work_dir == Path("wt")

    def test_cwd_default_is_read_only_when_work_dir_is_read(
        self, bare_env: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(Path, "cwd", staticmethod(_deleted_cwd))

        context = resolve_call_context({})

        assert context.explicit_work_dir is None
        with pytest.raises(FileNotFoundError):
            _ = context.work_dir
        explicit = resolve_call_context({"work_dir": str(bare_env)})
        assert explicit.work_dir == explicit.explicit_work_dir == bare_env

    @pytest.mark.parametrize("flow", ["lint", "synth"])
    def test_requested_timeout_budget_never_reads_cwd(
        self, bare_env: Path, monkeypatch: pytest.MonkeyPatch, flow: str
    ) -> None:
        # resolve_timeout_ms applies the cwd default itself, and only for config lookup.
        monkeypatch.setattr(Path, "cwd", staticmethod(_deleted_cwd))

        timeout = mcp_server._mcp_tool_timeout_seconds(
            flow, {"timeout_ms": 1_000}, {"default_timeout": 60}
        )

        assert timeout >= 60


class TestEnvironmentFacts:
    def test_unset_environment_resolves_to_none(self, bare_env: Path) -> None:
        context = resolve_call_context({})

        assert context.jobs_root is None
        assert context.jobs_root == session_jobs_dir()
        assert context.state_path is None
        assert context.state_path == _old_state_path()
        assert context.logs_dir is None
        assert context.runtime_dir is None

    def test_empty_variables_resolve_like_unset(
        self, bare_env: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        for name in _CALL_ENV:
            monkeypatch.setenv(name, "")

        context = resolve_call_context({})

        assert (context.jobs_root, context.state_path) == (None, None)
        assert (context.logs_dir, context.runtime_dir) == (None, None)
        assert context.jobs_root == session_jobs_dir()
        assert context.state_path == _old_state_path()

    def test_logs_dir_alone_derives_the_jobs_root(
        self, bare_env: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        logs = bare_env / "logs"
        monkeypatch.setenv("BOOLEY_LOGS_DIR", str(logs))

        context = resolve_call_context({})

        assert context.jobs_root == session_jobs_dir() == logs / ".runtime" / "jobs"
        assert context.logs_dir == logs
        # runtime_dir mirrors the variable itself, not the logs-derived fallback.
        assert context.runtime_dir is None

    def test_every_variable_set(self, bare_env: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        logs, runtime, state = bare_env / "logs", bare_env / "rt", bare_env / "state.json"
        monkeypatch.setenv("BOOLEY_LOGS_DIR", str(logs))
        monkeypatch.setenv("BOOLEY_RUNTIME_DIR", str(runtime))
        monkeypatch.setenv("BOOLEY_STATE_FILE", str(state))

        context = resolve_call_context({"work_dir": str(bare_env)})

        assert context.jobs_root == session_jobs_dir() == runtime / "jobs"
        assert context.state_path == _old_state_path() == state
        assert (context.logs_dir, context.runtime_dir) == (logs, runtime)
        assert context.work_dir == bare_env

    def test_runtime_dir_without_logs_dir_has_no_jobs_root(
        self, bare_env: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("BOOLEY_RUNTIME_DIR", str(bare_env / "rt"))

        context = resolve_call_context({})

        assert context.jobs_root is None
        assert context.jobs_root == session_jobs_dir()
        assert context.runtime_dir == bare_env / "rt"

    def test_context_is_frozen_with_empty_immutable_overrides(self, bare_env: Path) -> None:
        context = resolve_call_context({})

        assert dict(context.subprocess_env_overrides) == {}
        with pytest.raises(TypeError):
            context.subprocess_env_overrides["X"] = "1"  # type: ignore[index]
        with pytest.raises(dataclasses.FrozenInstanceError):
            context.explicit_work_dir = Path("/elsewhere")  # type: ignore[misc]


class TestLocateJob:
    def test_returns_the_record_the_direct_read_returned(
        self, bare_env: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("BOOLEY_LOGS_DIR", str(bare_env / "logs"))
        rec = jobrec.JobRecord(
            run_id="sim-20261005T000000Z-1",
            endpoint="sim",
            started_at="2026-10-05T00:00:00Z",
            timeout_s=60,
            argv=["python", "-m", "booley.flows.sim"],
        )
        jobrec.write_record(rec, root=session_jobs_dir())

        located = mcp_server._locate_job(rec.run_id)

        assert located is not None
        assert located == jobrec.read_record(rec.run_id, root=session_jobs_dir())
        assert located.to_dict() == rec.to_dict()

    def test_unknown_run_id_is_none(self, bare_env: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("BOOLEY_LOGS_DIR", str(bare_env / "logs"))

        assert mcp_server._locate_job("nope-1") is None

    def test_unconfigured_root_is_none(self, bare_env: Path) -> None:
        assert jobrec.read_record("sim-1", root=session_jobs_dir()) is None
        assert mcp_server._locate_job("sim-1") is None


class TestServerConsumers:
    def test_baseline_check_reads_the_context_state_path(
        self, bare_env: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        state = bare_env / "state.json"
        state.write_text(
            json.dumps(
                {"criteria": {"synthesis_ok_core": {"params": {"_baseline_ref": "a" * 40}}}}
            ),
            encoding="utf-8",
        )
        monkeypatch.setenv("BOOLEY_STATE_FILE", str(state))
        state_path = resolve_call_context({}).state_path

        assert mcp_server._ticket_baseline_required("synthesis_ok_", state_path) is True
        assert mcp_server._ticket_baseline_required("fpga_impl_ok_", state_path) is False
        assert mcp_server._ticket_baseline_required("synthesis_ok_", None) is False

    def test_subprocess_env_layers_context_then_per_run_overrides(
        self, bare_env: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("BOOLEY_R1_PROBE", "server")
        base = resolve_call_context({})
        context = CallContext(
            explicit_work_dir=base.explicit_work_dir,
            jobs_root=base.jobs_root,
            state_path=base.state_path,
            logs_dir=base.logs_dir,
            runtime_dir=base.runtime_dir,
            subprocess_env_overrides=MappingProxyType(
                {"BOOLEY_R1_PROBE": "context", "BOOLEY_R1_ONLY": "context"}
            ),
            ticket_file=base.ticket_file,
        )

        plain = mcp_server._endpoint_subprocess_env(base)
        layered = mcp_server._endpoint_subprocess_env(context, BOOLEY_R1_ONLY="run")

        assert plain["BOOLEY_R1_PROBE"] == "server"
        assert "BOOLEY_R1_ONLY" not in plain
        assert layered["BOOLEY_R1_PROBE"] == "context"
        assert layered["BOOLEY_R1_ONLY"] == "run"
        from booley.flows.cli_selection import INVOCATION_ORIGIN_ENV

        assert plain[INVOCATION_ORIGIN_ENV] == "transport"
        assert layered[INVOCATION_ORIGIN_ENV] == "transport"


@pytest.fixture
def job_env(bare_env: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Server environment with a configured jobs root and a pinned slot store."""
    monkeypatch.setenv("BOOLEY_LOGS_DIR", str(bare_env / "logs"))
    monkeypatch.setenv("BOOLEY_RUNTIME_DIR", str(bare_env / "rt"))
    monkeypatch.setenv("BOOLEY_SLOTS_DIR", str(bare_env / "slots"))
    monkeypatch.delenv("BOOLEY_TICKET_FILE", raising=False)
    return bare_env


class TestDispatchWithoutCwd:
    def test_specialist_without_work_dir_starts_and_attaches_with_cwd_gone(
        self, job_env: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # On main a work_dir-less Specialist call never read the cwd: the
        # transcript dir, timeout, and attach scan come from the environment.
        release = asyncio.Event()

        async def run(_cmd, *, timeout, on_spawn=None, **_kwargs):
            on_spawn(4242)
            await release.wait()
            return 0, "DONE", "", False

        monkeypatch.setattr(mcp_server, "_run_subprocess", run)
        monkeypatch.setattr(mcp_server, "_job_inline_wait_seconds", lambda: 0.0)
        monkeypatch.setattr(jobrec, "derive_status", lambda rec, _alive, **_kw: rec.status)
        monkeypatch.setattr(jobrec, "is_active", lambda _rec, _alive, **_kw: True)
        monkeypatch.setattr(Path, "cwd", staticmethod(_deleted_cwd))
        definition = {
            "module": "reviewer",
            "module_path": "booley.specialists.reviewer",
            "is_specialist": True,
            "default_timeout": 60,
        }

        async def scenario() -> tuple[str, str]:
            jobs = mcp_server._JobManager(_Lifetime())
            counts: collections.defaultdict[str, int] = collections.defaultdict(int)
            first = await mcp_server._dispatch_booley_mcp_tool(
                "reviewer", {}, definition, counts, jobs
            )
            second = await mcp_server._dispatch_booley_mcp_tool(
                "reviewer", {}, definition, counts, jobs
            )
            release.set()
            return first[0].text, second[0].text

        first, second = asyncio.run(scenario())

        assert "RUNNING: 'reviewer'" in first
        assert "attached to it" in second


class TestManagerReadsPinnedRoot:
    """A run the manager started is read where it is written, whatever the env says now."""

    @staticmethod
    def _spy_reads(monkeypatch: pytest.MonkeyPatch) -> list[Path | None]:
        roots: list[Path | None] = []
        real_read = jobrec.read_record

        def read(run_id: str, root: Path | None) -> jobrec.JobRecord | None:
            roots.append(root)
            return real_read(run_id, root)

        monkeypatch.setattr(jobrec, "read_record", read)
        return roots

    def test_completion_rendering_reads_the_submit_root(
        self, job_env: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        async def run(_cmd, *, timeout, on_spawn=None, **_kwargs):
            on_spawn(4242)
            return 0, "DONE", "", False

        monkeypatch.setattr(mcp_server, "_run_subprocess", run)
        pinned = session_jobs_dir()

        async def scenario() -> tuple[mcp_server._JobManager, str]:
            jobs = mcp_server._JobManager(_Lifetime())
            run_id = jobs.submit("sim", ["c"], 60, context=resolve_call_context({}))
            await jobs._tasks[run_id]
            return jobs, run_id

        jobs, run_id = asyncio.run(scenario())
        monkeypatch.setenv("BOOLEY_RUNTIME_DIR", str(job_env / "other-runtime"))
        roots = self._spy_reads(monkeypatch)

        text = jobs.result_text(run_id)

        assert "EXIT_CODE: 0" in text
        assert roots == [pinned]
        record = jobrec.read_record(run_id, root=pinned)
        assert record is not None and record.status == jobrec.STATUS_DONE

    def test_cancel_finds_the_submit_root_after_the_env_moves(
        self, job_env: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        started = asyncio.Event()

        async def run(_cmd, *, timeout, on_spawn=None, **_kwargs):
            on_spawn(4242)
            started.set()
            await asyncio.Event().wait()

        monkeypatch.setattr(mcp_server, "_run_subprocess", run)
        monkeypatch.setattr(
            jobrec, "derive_status", lambda _rec, _alive, **_kw: jobrec.STATUS_RUNNING
        )
        pinned = session_jobs_dir()

        async def scenario() -> tuple[str | None, str, str]:
            jobs = mcp_server._JobManager(_Lifetime())
            run_id = jobs.submit("sim", ["c"], 60, context=resolve_call_context({}))
            await started.wait()
            monkeypatch.setenv("BOOLEY_RUNTIME_DIR", str(job_env / "other-runtime"))
            phase = await jobs.cancel(run_id)
            return phase, run_id, jobs.result_text(run_id)

        phase, run_id, text = asyncio.run(scenario())

        assert phase == "running"
        assert "CANCELLED" in text
        record = jobrec.read_record(run_id, root=pinned)
        assert record is not None and record.status == jobrec.STATUS_CANCELLED
        assert not (job_env / "other-runtime" / "jobs").exists()
