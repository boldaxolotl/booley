"""The per-call context resolves exactly what the server's scattered reads did."""

from __future__ import annotations

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
            context.work_dir = Path("/elsewhere")  # type: ignore[misc]


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
            work_dir=base.work_dir,
            jobs_root=base.jobs_root,
            state_path=base.state_path,
            logs_dir=base.logs_dir,
            runtime_dir=base.runtime_dir,
            subprocess_env_overrides=MappingProxyType(
                {"BOOLEY_R1_PROBE": "context", "BOOLEY_R1_ONLY": "context"}
            ),
        )

        plain = mcp_server._endpoint_subprocess_env(base)
        layered = mcp_server._endpoint_subprocess_env(context, BOOLEY_R1_ONLY="run")

        assert plain["BOOLEY_R1_PROBE"] == "server"
        assert "BOOLEY_R1_ONLY" not in plain
        assert layered["BOOLEY_R1_PROBE"] == "context"
        assert layered["BOOLEY_R1_ONLY"] == "run"
