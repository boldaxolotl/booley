"""Tests for Specialist — model and Agent CLI resolution, argparse, failure reports."""

from __future__ import annotations

import argparse
import contextlib
import dataclasses
import os
import subprocess
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
from claude_agent_sdk import ClaudeSDKError

from booley.core.models import AgentCallParams
from booley.mcp.base import EXIT_FAILURE, EXIT_SUCCESS, McpToolResult
from booley.specialists.specialist import Specialist
from tests.subprocess_budget import SUBPROCESS_TIMEOUT_S


def _env_with_state(state_file: Path, slug: str = "test") -> dict[str, str]:
    env = os.environ.copy()
    env["BOOLEY_SLUG"] = slug
    env["BOOLEY_STATE_FILE"] = str(state_file)
    return env


class ReviewSpecialist(Specialist):
    """Concrete Specialist for testing."""

    name = "review_test"
    description = "Test review endpoint"

    def _add_agent_args(self, parser: argparse.ArgumentParser) -> None:
        parser.add_argument("--diff-ref", default="HEAD~1")

    def _build_prompt(self) -> str:
        return f"Review changes since {self.args.diff_ref}"

    def _interpret_output(self, output: str, structured: dict | None) -> McpToolResult:
        if structured and structured.get("approved"):
            return McpToolResult(
                exit_code=EXIT_SUCCESS,
                criterion_key="review_rtl_bugs_done",
                criterion_met=True,
            )
        return McpToolResult(exit_code=EXIT_FAILURE, criterion_met=False)


class TestSpecialistArgparse:
    def test_common_agent_args(self, tmp_path: Path):
        state_file = tmp_path / "state.json"
        env = _env_with_state(state_file)
        endpoint = ReviewSpecialist()
        with patch.dict(os.environ, env):
            args = endpoint.parse_args(
                ["--instruction", "focus on timing", "--diff-ref", "HEAD~3"]
            )
        assert args.instruction == "focus on timing"
        assert args.diff_ref == "HEAD~3"

    def test_defaults(self, tmp_path: Path):
        state_file = tmp_path / "state.json"
        env = _env_with_state(state_file)
        endpoint = ReviewSpecialist()
        with patch.dict(os.environ, env):
            args = endpoint.parse_args([])
        assert args.instruction == ""
        assert args.diff_ref == "HEAD~1"

    @pytest.mark.parametrize("value", ["0", "-1", "1.5", "words"])
    def test_max_turns_must_be_a_positive_integer(self, value: str):
        with pytest.raises(SystemExit):
            ReviewSpecialist().parse_args(["--max-turns", value])

    def test_hidden_options_do_not_accept_abbreviations(self):
        with pytest.raises(SystemExit):
            ReviewSpecialist().parse_args(["--transcript", "injected"])


class TestTimeoutClamping:
    def test_timeout_below_min_is_clamped(self, tmp_path: Path):
        """Developer Agent passing too-low --timeout-ms gets clamped to min_timeout."""
        state_file = tmp_path / "state.json"
        state_file.write_text("{}")
        env = _env_with_state(state_file)

        class StrictSpecialist(Specialist):
            name = "strict"
            description = "test"
            min_timeout = 1200

            def _add_agent_args(self, parser):
                pass

            def _build_prompt(self):
                return "test"

            def _interpret_output(self, output, structured):
                return McpToolResult(exit_code=EXIT_SUCCESS)

        endpoint = StrictSpecialist()
        with patch.dict(os.environ, env):
            endpoint.parse_args(["--timeout-ms", "600000"])
        assert endpoint.args.timeout_ms == 600000  # not clamped at parse time
        # Clamping happens in _run() — test via mock
        with patch.object(endpoint, "_invoke_agent", side_effect=RuntimeError("skip")):
            endpoint.read_state()
            endpoint._start_time = 0
            with contextlib.suppress(RuntimeError):
                endpoint._run()
        assert endpoint.args.timeout_ms == 1200000

    def test_timeout_above_min_unchanged(self, tmp_path: Path):
        state_file = tmp_path / "state.json"
        state_file.write_text("{}")
        env = _env_with_state(state_file)
        endpoint = ReviewSpecialist()
        with patch.dict(os.environ, env):
            endpoint.parse_args(["--timeout-ms", "3600000"])
        # Run the clamping step in _run(); an above-minimum timeout must survive it.
        with patch.object(endpoint, "_invoke_agent", side_effect=RuntimeError("skip")):
            endpoint.read_state()
            endpoint._start_time = 0
            with contextlib.suppress(RuntimeError):
                endpoint._run()
        assert endpoint.args.timeout_ms == 3600000


def test_provider_exception_preserves_report_and_persists_traceback(
    tmp_path: Path, capsys, caplog, monkeypatch
) -> None:
    state_file = tmp_path / "state.json"
    state_file.write_text("{}")
    endpoint = ReviewSpecialist()
    monkeypatch.setattr(
        "booley.runtime.runtime_context.container_only_error", lambda _command: None
    )
    message = "provider failed\nsecret detail " + "x" * 2_000
    caplog.set_level("DEBUG")

    with (
        patch.dict(os.environ, _env_with_state(state_file)),
        patch.object(endpoint, "_invoke_agent", side_effect=RuntimeError(message)),
    ):
        result = endpoint.execute_cli(
            [
                "--work-dir",
                str(tmp_path),
                "--report-dir",
                str(tmp_path / "reports"),
                "--transcript-dir",
                str(tmp_path / "transcripts"),
            ]
        )

    assert result.exit_code == 2
    diagnosis = result.outcome.report_text.split(". Diagnostic: ", 1)[0]
    assert diagnosis.startswith("review_test failed: RuntimeError: provider failed secret detail")
    assert "\n" not in diagnosis
    assert len(diagnosis) < 1_100
    diagnostic = Path(result.outcome.report_text.rsplit(". Diagnostic: ", 1)[1])
    assert diagnostic.parent == tmp_path / "transcripts"
    assert diagnostic.name.startswith("review_test.")
    assert diagnostic.name.endswith(".error.log")
    assert message in diagnostic.read_text(encoding="utf-8")
    assert "Traceback" not in capsys.readouterr().err
    assert any(record.exc_info for record in caplog.records if record.levelname == "DEBUG")


def _named(name: str) -> ReviewSpecialist:
    """A test Specialist that resolves its model and Agent CLI under *name*."""
    endpoint = ReviewSpecialist()
    endpoint.name = name
    return endpoint


@pytest.fixture
def per_cli_backends(monkeypatch):
    """Install settings with one recording fake backend per Agent CLI.

    ``install(settings, respond=None)`` returns ``(built, calls)``: ``built`` lists each
    ``(cli, auth)`` backend construction and ``calls`` each ``(cli, params)`` call.
    ``respond(params)`` replaces the default approving reply (it may raise).
    """
    from booley.core.models import AgentResult
    from booley.runtime import agent_config

    built: list[tuple[str, str]] = []
    calls: list[tuple[str, AgentCallParams]] = []

    def approve(_params: AgentCallParams) -> AgentResult:
        return AgentResult(structured={"approved": True})

    def install(settings, respond=approve):
        class FakeBackend:
            def __init__(self, cli: str) -> None:
                self.cli = cli

            async def call(self, params, *, on_event=None, on_start=None):
                calls.append((self.cli, params))
                return respond(params)

        def make_backend(cli: str, auth: str) -> FakeBackend:
            built.append((cli, auth))
            return FakeBackend(cli)

        monkeypatch.setattr(agent_config, "_make_backend", make_backend)
        agent_config.set_backend_config(agent_config.configure_backend(settings))
        return built, calls

    monkeypatch.setattr(
        "booley.runtime.runtime_context.container_only_error", lambda _command: None
    )
    monkeypatch.setattr(
        "booley.specialists.specialist.parent_death_watchdog", lambda *_: contextlib.nullcontext()
    )
    yield install
    agent_config.set_backend_config(None)


def _launch(endpoint: Specialist, tmp_path: Path) -> McpToolResult:
    """Run *endpoint*'s agent step: model and CLI resolution, invocation, failure report."""
    state_file = tmp_path / "state.json"
    state_file.write_text("{}")
    with patch.dict(os.environ, _env_with_state(state_file)):
        endpoint.parse_args(
            [
                "--work-dir",
                str(tmp_path),
                "--report-dir",
                str(tmp_path / "reports"),
                "--transcript-dir",
                str(tmp_path / "transcripts"),
            ]
        )
        endpoint.read_state()
        return endpoint._run()


def test_specialist_launch_runs_on_its_models_cli(tmp_path: Path, per_cli_backends) -> None:
    from booley.config.agent import AgentSettings

    built, calls = per_cli_backends(
        AgentSettings(
            default_cli="claude",
            clis=("claude", "codex"),
            auth={"claude": "subscription", "codex": "api_key"},
            specialist_models={"reviewer": "gpt-6-luna"},
        )
    )

    assert _launch(_named("reviewer"), tmp_path).exit_code == EXIT_SUCCESS
    assert _launch(_named("mutation_tester"), tmp_path).exit_code == EXIT_SUCCESS

    (reviewer_cli, reviewer), (mutation_cli, mutation) = calls
    assert (reviewer_cli, reviewer.model, reviewer.reasoning_effort) == (
        "codex",
        "gpt-6-luna",
        "high",
    )
    assert (mutation_cli, mutation.model, mutation.reasoning_effort) == (
        "claude",
        "claude-opus-5-5",
        None,
    )
    # The default CLI's backend is built up front; codex's once, on first use, with its auth.
    assert built == [("claude", "subscription"), ("codex", "api_key")]


def test_refused_model_raises_refusal_and_tries_no_other_model(
    tmp_path: Path, per_cli_backends
) -> None:
    from booley.config.agent import AgentSettings
    from booley.runtime.agent_errors import model_unavailable_error

    def refuse(params: AgentCallParams):
        # Built as the Codex backend builds it: inside the call, from the live settings.
        raise model_unavailable_error(
            params.model, "codex", '{"status":400,"error":{"code":"model_not_found"}}'
        )

    _built, calls = per_cli_backends(
        AgentSettings(
            default_cli="codex", clis=("codex",), specialist_models={"reviewer": "gpt-6-luna"}
        ),
        respond=refuse,
    )

    result = _launch(_named("reviewer"), tmp_path)

    assert [(cli, params.model) for cli, params in calls] == [("codex", "gpt-6-luna")]
    assert result.exit_code == 2
    report = result.report_text
    assert report.startswith("reviewer failed: ModelUnavailableError: ")
    assert "'gpt-6-luna'" in report
    assert "`[models] reviewer`" in report
    assert "Protected Input" in report
    assert "model_not_found" not in report


# --- hidden [models] fallback list (#2836, ADR 0084 amendment) ---------------------


def _launch_finalized(endpoint: Specialist, tmp_path: Path) -> McpToolResult:
    """``_launch`` plus the finalization every executed Specialist result goes through."""
    result = _launch(endpoint, tmp_path)
    endpoint._finalize_result(result)
    return result


_OPUS_THEN_LUNA = ("claude-opus-5-5", "gpt-6-luna")


def _refusal_memory() -> dict:
    """The process refusal memory of the model fallback list.

    Empty when the module is absent, so the regression tests fail in their bodies, not in
    fixture setup, on a base that predates the list form.
    """
    try:
        from booley.runtime import model_fallback
    except ImportError:
        return {}
    return model_fallback._REFUSED


@pytest.fixture
def logins(monkeypatch):
    """Set which Agent CLIs have a login; the process refusal memory starts empty."""
    from booley.runtime import auth_token

    logged_in: set[str] = set()
    monkeypatch.setattr(
        auth_token, "cli_logged_in", lambda cli, *_a, **_kw: cli in logged_in, raising=False
    )
    refused = _refusal_memory()
    refused.clear()
    yield logged_in
    refused.clear()


def _list_settings(models=_OPUS_THEN_LUNA, *, clis=("claude", "codex"), default="claude"):
    from booley.config.agent import AgentSettings

    return AgentSettings(
        default_cli=default,
        clis=clis,
        auth={"claude": "subscription", "codex": "api_key"},
        specialist_models={"reviewer": tuple(models)},
    )


def _refuse_model(refused: str):
    """A backend reply that refuses *refused* as the real backends do, else approves."""
    from booley.core.models import AgentResult
    from booley.runtime.agent_errors import model_unavailable_error

    def respond(params: AgentCallParams):
        if params.model == refused:
            cli = "claude" if refused.startswith("claude-") else "codex"
            raise model_unavailable_error(params.model, cli, "model_not_found")
        return AgentResult(structured={"approved": True})

    return respond


def test_model_list_skips_entry_whose_cli_cannot_run(
    tmp_path: Path, per_cli_backends, logins
) -> None:
    cases = [
        ({"codex"}, ("claude", "codex"), "claude"),  # Claude has no login
        ({"claude", "codex"}, ("codex",), "codex"),  # Claude is not in [agent] clis
    ]
    for logged_in, clis, default in cases:
        logins.clear()
        logins.update(logged_in)
        _built, calls = per_cli_backends(_list_settings(clis=clis, default=default))
        earlier = len(calls)  # the fixture's call log is shared across installs

        result = _launch_finalized(_named("reviewer"), tmp_path)

        assert result.exit_code == EXIT_SUCCESS
        # No model call for the skipped Claude entry; Luna runs with Codex's effort.
        assert [(cli, p.model, p.reasoning_effort) for cli, p in calls[earlier:]] == [
            ("codex", "gpt-6-luna", "high")
        ]
        note = result.detail["specialist_model"]
        assert note["model"] == "gpt-6-luna"
        assert note["fallback"]["first_choice"] == "claude-opus-5-5"


def test_model_list_fallback_keeps_the_session_of_the_model_that_ran(
    tmp_path: Path, per_cli_backends, logins
) -> None:
    """Retry rounds resume the session the fallback model made, not a fresh one."""
    logins.add("codex")
    _built, calls = per_cli_backends(_list_settings())
    endpoint = _named("reviewer")
    _launch_finalized(endpoint, tmp_path)  # round 1 runs on Luna (Claude has no login)

    resumed = dataclasses.replace(
        endpoint._agent_call_params(), session_id="codex-session", resume_session=True
    )
    endpoint._invoke_agent(resumed)

    cli, params = calls[-1]
    assert (cli, params.model) == ("codex", "gpt-6-luna")
    assert (params.session_id, params.resume_session) == ("codex-session", True)


def test_model_list_runs_first_entry_when_both_clis_are_logged_in(
    tmp_path: Path, per_cli_backends, logins
) -> None:
    logins.update({"claude", "codex"})
    _built, calls = per_cli_backends(_list_settings())

    result = _launch_finalized(_named("reviewer"), tmp_path)

    assert result.exit_code == EXIT_SUCCESS
    assert [(cli, params.model) for cli, params in calls] == [("claude", "claude-opus-5-5")]
    assert result.detail["specialist_model"] == {"model": "claude-opus-5-5", "agent_cli": "claude"}
    assert "Ran on" not in result.report_text


def test_model_list_refusal_moves_on_and_is_remembered(
    tmp_path: Path, per_cli_backends, logins
) -> None:
    logins.update({"claude", "codex"})
    _built, calls = per_cli_backends(_list_settings(), respond=_refuse_model("claude-opus-5-5"))

    first = _launch_finalized(_named("reviewer"), tmp_path)
    second = _launch_finalized(_named("reviewer"), tmp_path)

    assert first.exit_code == EXIT_SUCCESS and second.exit_code == EXIT_SUCCESS
    # The refused entry is called once; the second launch goes straight to Luna.
    assert [(cli, params.model) for cli, params in calls] == [
        ("claude", "claude-opus-5-5"),
        ("codex", "gpt-6-luna"),
        ("codex", "gpt-6-luna"),
    ]
    fallback = second.detail["specialist_model"]["fallback"]
    assert fallback["skipped"] == [
        {
            "model": "claude-opus-5-5",
            "reason": "refused by the provider earlier in this process",
        }
    ]


def _non_refusal_errors():
    from booley.runtime.agent_errors import (
        AgentAuthError,
        AgentTimeoutError,
        TransientAPIError,
        UsageLimitError,
    )

    return [
        AgentAuthError("401 unauthorized", "claude"),
        UsageLimitError("usage limit reached", "claude"),
        TransientAPIError("529 overloaded"),
        AgentTimeoutError("deadline reached"),
    ]


def test_model_list_non_refusal_errors_do_not_try_next_entry(
    tmp_path: Path, per_cli_backends, logins
) -> None:
    logins.update({"claude", "codex"})
    for error in _non_refusal_errors():

        def fail(_params: AgentCallParams, error=error):
            raise error

        _built, calls = per_cli_backends(_list_settings(), respond=fail)
        earlier = len(calls)  # the fixture's call log is shared across installs

        result = _launch_finalized(_named("reviewer"), tmp_path)

        assert result.exit_code == 2
        assert [(cli, p.model) for cli, p in calls[earlier:]] == [("claude", "claude-opus-5-5")]
        assert type(error).__name__ in result.report_text


def test_model_list_without_usable_entry_names_each_entry_and_reason(
    tmp_path: Path, per_cli_backends, logins
) -> None:
    logins.update({"codex"})  # Claude has no login; Luna is refused
    _built, calls = per_cli_backends(_list_settings(), respond=_refuse_model("gpt-6-luna"))

    result = _launch_finalized(_named("reviewer"), tmp_path)

    assert result.exit_code == 2
    assert [(cli, params.model) for cli, params in calls] == [("codex", "gpt-6-luna")]
    report = result.report_text
    assert report.startswith("reviewer failed: ModelUnavailableError: ")
    assert "'claude-opus-5-5' (Agent CLI claude is not logged in)" in report
    assert "'gpt-6-luna' (refused by the provider)" in report
    assert "`[models] reviewer`" in report
    assert "Protected Input" in report


def test_single_model_value_ignores_list_memory_and_never_falls_back(
    tmp_path: Path, per_cli_backends, logins
) -> None:
    from booley.config.agent import AgentSettings

    logins.update({"claude", "codex"})
    # A list refuses Luna first, so the process remembers Luna as refused ...
    _built, list_calls = per_cli_backends(
        _list_settings(("gpt-6-luna", "claude-opus-5-5")), respond=_refuse_model("gpt-6-luna")
    )
    assert _launch(_named("reviewer"), tmp_path).exit_code == EXIT_SUCCESS
    assert [(cli, params.model) for cli, params in list_calls] == [
        ("codex", "gpt-6-luna"),
        ("claude", "claude-opus-5-5"),
    ]

    # ... yet a string pin for Luna still calls it once, and its refusal tries nothing else.
    earlier = len(list_calls)  # the fixture's call log is shared across installs
    _built, calls = per_cli_backends(
        AgentSettings(
            default_cli="claude",
            clis=("claude", "codex"),
            specialist_models={"reviewer": "gpt-6-luna"},
        ),
        respond=_refuse_model("gpt-6-luna"),
    )
    result = _launch_finalized(_named("reviewer"), tmp_path)

    assert [(cli, params.model) for cli, params in calls[earlier:]] == [("codex", "gpt-6-luna")]
    assert result.exit_code == 2
    assert result.report_text.startswith("reviewer failed: ModelUnavailableError: ")
    assert "specialist_model" not in result.detail


def test_specialist_result_and_evidence_record_model_and_fallback(
    tmp_path: Path, per_cli_backends, logins
) -> None:
    from booley.flows.endpoint_state import EndpointState

    logins.update({"codex"})
    _built, _calls = per_cli_backends(_list_settings())
    endpoint = _named("reviewer")

    result = _launch_finalized(endpoint, tmp_path)

    expected = {
        "model": "gpt-6-luna",
        "agent_cli": "codex",
        "fallback": {
            "first_choice": "claude-opus-5-5",
            "skipped": [
                {"model": "claude-opus-5-5", "reason": "Agent CLI claude is not logged in"}
            ],
        },
    }
    assert result.detail["specialist_model"] == expected
    assert "Ran on gpt-6-luna instead of claude-opus-5-5" in result.report_text
    # The Criterion evidence (what the Review Package copies) carries the same note.
    detail: dict = {"specialist_model": {"model": "stale"}}
    with patch.object(EndpointState, "set_criterion") as base:
        endpoint.set_criterion("review_rtl_bugs_done", True, detail=detail)
    assert base.call_args.kwargs["detail"]["specialist_model"] == expected


class TestTranscriptPath:
    def test_transcript_path_created(self, tmp_path: Path):
        state_file = tmp_path / "state.json"
        transcript_dir = tmp_path / "transcripts"
        env = _env_with_state(state_file)
        endpoint = ReviewSpecialist()
        with patch.dict(os.environ, env):
            endpoint.parse_args(["--transcript-dir", str(transcript_dir)])
        path = endpoint.transcript_path()
        assert path is not None
        assert path.name == "review_test.jsonl"
        assert transcript_dir.exists()

    def test_no_transcript_when_no_dir(self, tmp_path: Path):
        state_file = tmp_path / "state.json"
        env = _env_with_state(state_file)
        endpoint = ReviewSpecialist()
        with patch.dict(os.environ, env):
            endpoint.parse_args([])
        assert endpoint.transcript_path() is None


# ===========================================================================
# Session persistence round-trip
# ===========================================================================


class TestSessionPersistence:
    def test_roundtrip(self, tmp_path: Path):
        endpoint = ReviewSpecialist()
        endpoint._last_session_id = "sess-abc-123"
        with patch.dict(os.environ, {"BOOLEY_LOGS_DIR": str(tmp_path)}):
            endpoint._persist_session_id("test-key")
            assert endpoint._load_session_id("test-key") == "sess-abc-123"
            endpoint._clear_session_id("test-key")
            assert endpoint._load_session_id("test-key") is None
            assert not (tmp_path / "test-key.session_id").exists()

    def test_standalone_run_persists_under_project_runtime(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ):
        """SETUP-F-42: outside Ticket Mode there is no BOOLEY_LOGS_DIR, and
        returning None there silently disabled resume — every retry round
        cold-started the agent. Standalone runs land in the project runtime."""
        monkeypatch.delenv("BOOLEY_LOGS_DIR", raising=False)
        endpoint = ReviewSpecialist()
        endpoint._last_session_id = "sess-xyz"
        endpoint._persist_session_id("k")

        assert endpoint._load_session_id("k") == "sess-xyz"
        # The autouse _set_booley_dir fixture pins BOOLEY_DIR=tmp_path.
        assert (tmp_path / ".runtime" / "sessions" / "k.session_id").exists()
        endpoint._clear_session_id("k")
        assert endpoint._load_session_id("k") is None

    def test_persist_noop_when_no_project_discoverable(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ):
        """No ticket AND no project on disk — nowhere to persist, so None."""
        from booley.runtime.booley_dir import reset_cache

        monkeypatch.delenv("BOOLEY_LOGS_DIR", raising=False)
        monkeypatch.delenv("BOOLEY_DIR", raising=False)
        empty = tmp_path / "no_project"
        empty.mkdir()
        monkeypatch.chdir(empty)
        reset_cache()

        endpoint = ReviewSpecialist()
        endpoint._last_session_id = "sess-xyz"
        endpoint._persist_session_id("k")
        assert endpoint._session_file_path("k") is None
        assert endpoint._load_session_id("k") is None

    def test_load_returns_none_for_missing_file(self, tmp_path: Path):
        endpoint = ReviewSpecialist()
        with patch.dict(os.environ, {"BOOLEY_LOGS_DIR": str(tmp_path)}):
            assert endpoint._load_session_id("nonexistent") is None

    def test_load_returns_none_for_empty_file(self, tmp_path: Path):
        (tmp_path / "empty.session_id").write_text("", encoding="utf-8")
        endpoint = ReviewSpecialist()
        with patch.dict(os.environ, {"BOOLEY_LOGS_DIR": str(tmp_path)}):
            assert endpoint._load_session_id("empty") is None

    def test_persist_noop_when_no_session_id(self, tmp_path: Path):
        endpoint = ReviewSpecialist()
        endpoint._last_session_id = None
        with patch.dict(os.environ, {"BOOLEY_LOGS_DIR": str(tmp_path)}):
            endpoint._persist_session_id("k")
            assert not (tmp_path / "k.session_id").exists()

    def test_clear_noop_when_no_file(self, tmp_path: Path):
        endpoint = ReviewSpecialist()
        with patch.dict(os.environ, {"BOOLEY_LOGS_DIR": str(tmp_path)}):
            endpoint._clear_session_id("gone")


# ===========================================================================
# _build_resume_params
# ===========================================================================


class TestBuildResumeParams:
    def test_sets_resume_fields_without_mutating_original(self):
        original = AgentCallParams(prompt="p", model="sonnet", cwd="/tmp")
        endpoint = ReviewSpecialist()
        result = endpoint._build_resume_params(original, "sess-123")

        assert result.session_id == "sess-123"
        assert result.resume_session is True
        assert original.session_id is None
        assert original.resume_session is False


# ===========================================================================
# _invoke_agent_with_resume
# ===========================================================================


class TestInvokeAgentWithResume:
    def test_non_resume_calls_directly(self):
        endpoint = ReviewSpecialist()
        mock_result = MagicMock()
        with patch.object(endpoint, "_invoke_agent", return_value=mock_result) as mock_inv:
            params = AgentCallParams(prompt="p", model="sonnet", cwd="/tmp")
            result = endpoint._invoke_agent_with_resume(params)
        assert result is mock_result
        mock_inv.assert_called_once_with(params, on_event=None)

    @pytest.mark.parametrize("error_type", [RuntimeError, ClaudeSDKError])
    def test_falls_back_on_resumable_error(self, error_type):
        endpoint = ReviewSpecialist()
        endpoint.name = "test_endpoint"
        fresh_result = MagicMock()
        calls: list[AgentCallParams] = []

        def _side(p, on_event=None):
            calls.append(p)
            if len(calls) == 1:
                raise error_type("stale session")
            return fresh_result

        with patch.object(endpoint, "_invoke_agent", side_effect=_side):
            params = AgentCallParams(
                prompt="p",
                model="sonnet",
                cwd="/tmp",
                session_id="old",
                resume_session=True,
            )
            result = endpoint._invoke_agent_with_resume(params)

        assert result is fresh_result
        assert len(calls) == 2
        assert calls[1].session_id is None
        assert calls[1].resume_session is False

    def test_model_refusal_is_not_retried(self):
        from booley.runtime.agent_errors import ModelUnavailableError

        endpoint = ReviewSpecialist()
        endpoint.name = "test_endpoint"
        refusal = ModelUnavailableError(
            model="gpt-refused", provider="codex", settings=(), detail="model_not_found"
        )
        with patch.object(endpoint, "_invoke_agent", side_effect=refusal) as invoke:
            params = AgentCallParams(
                prompt="p", model="gpt-refused", cwd="/tmp", session_id="s", resume_session=True
            )
            with pytest.raises(ModelUnavailableError):
                endpoint._invoke_agent_with_resume(params)
        assert invoke.call_count == 1

    def test_timeout_error_propagates(self):
        endpoint = ReviewSpecialist()
        endpoint.name = "test_endpoint"
        with patch.object(endpoint, "_invoke_agent", side_effect=TimeoutError("boom")):
            params = AgentCallParams(
                prompt="p",
                model="sonnet",
                cwd="/tmp",
                session_id="s",
                resume_session=True,
            )
            with pytest.raises(TimeoutError):
                endpoint._invoke_agent_with_resume(params)


class TestCommitMsgBannedPhrase:
    """_prepare_commit_message rejects a banned phrase instead of rewriting the message.

    The prompt warns the agent of the banned list up front; a message that
    still carries one fails with the validation error, never a substitute.
    """

    def _make_endpoint(self, tmp_path: Path, default_msg: str = "feat(rtl): apply changes"):
        endpoint = ReviewSpecialist()
        # Bypass argparse for the focused unit test.
        mock_args = MagicMock()
        # Use a linked-worktree-shaped path: .git as a *file*, not a directory,
        # so _prepare_commit_message does not refuse on the main-worktree guard.
        worktree = tmp_path / "wt"
        worktree.mkdir()
        (worktree / ".git").write_text("gitdir: /elsewhere\n", encoding="utf-8")
        _stealth_project(worktree)
        mock_args.work_dir = worktree
        endpoint._args = mock_args
        endpoint._default_commit_message = default_msg
        endpoint.modifies_category = "rtl"
        return endpoint

    def test_banned_phrase_subject_raises(self, tmp_path: Path):
        endpoint = self._make_endpoint(tmp_path)
        # 'copilot' is on the default banned-phrase list.
        with pytest.raises(Specialist.GitStatusError) as exc:
            endpoint._prepare_commit_message("feat(rtl): copilot wrote this")
        assert "validation failed" in str(exc.value)
        assert "copilot" in str(exc.value)

    def test_non_banned_failure_still_raises(self, tmp_path: Path):
        endpoint = self._make_endpoint(tmp_path)
        # Mock validate_message: auto-format produces a syntactically valid
        # subject for any input, so the only way to stage a non-banned
        # failure is to inject one. The error must surface.
        from booley.specialists import specialist as specialist_module

        with (
            patch.object(
                specialist_module,
                "validate_message",
                return_value=[
                    "Subject doesn't match '<type>(<scope>): <summary>'",
                ],
            ),
            pytest.raises(Specialist.GitStatusError) as exc,
        ):
            endpoint._prepare_commit_message("anything")
        assert "validation failed" in str(exc.value)

    def test_clean_message_passes_through(self, tmp_path: Path):
        endpoint = self._make_endpoint(tmp_path)
        # No banned phrases, valid conventional commit: kept as written.
        ok = endpoint._prepare_commit_message("feat(rtl): add pipeline stage")
        assert ok == "feat(rtl): add pipeline stage"


class TestSpecialistGitCommit:
    """Specialist commit helper should share harness git hardening."""

    def _init_repo(self, tmp_path: Path) -> Path:
        repo = tmp_path / "repo"
        repo.mkdir()
        subprocess.run(
            ["git", "init"],
            cwd=repo,
            check=True,
            capture_output=True,
            text=True,
            timeout=SUBPROCESS_TIMEOUT_S,
        )
        subprocess.run(
            ["git", "config", "user.email", "test@example.com"],
            cwd=repo,
            check=True,
            capture_output=True,
            text=True,
            timeout=SUBPROCESS_TIMEOUT_S,
        )
        subprocess.run(
            ["git", "config", "user.name", "Test User"],
            cwd=repo,
            check=True,
            capture_output=True,
            text=True,
            timeout=SUBPROCESS_TIMEOUT_S,
        )
        (repo / "init.txt").write_text("init\n", encoding="utf-8")
        subprocess.run(
            ["git", "add", "init.txt"],
            cwd=repo,
            check=True,
            capture_output=True,
            text=True,
            timeout=SUBPROCESS_TIMEOUT_S,
        )
        subprocess.run(
            ["git", "commit", "-m", "feat: init"],
            cwd=repo,
            check=True,
            capture_output=True,
            text=True,
            timeout=SUBPROCESS_TIMEOUT_S,
        )
        return repo

    def test_stale_index_lock_is_removed_and_commit_retried(self, tmp_path: Path):
        repo = self._init_repo(tmp_path)
        worktree = tmp_path / "wt"
        subprocess.run(
            ["git", "-C", str(repo), "worktree", "add", str(worktree)],
            check=True,
            capture_output=True,
            text=True,
        )
        (worktree / "rtl.sv").write_text("module rtl; endmodule\n", encoding="utf-8")
        git_dir = subprocess.check_output(
            ["git", "-C", str(worktree), "rev-parse", "--git-dir"],
            text=True,
        ).strip()
        git_dir_path = Path(git_dir)
        if not git_dir_path.is_absolute():
            git_dir_path = worktree / git_dir_path
        lock_path = git_dir_path / "index.lock"
        lock_path.write_text("", encoding="utf-8")

        endpoint = ReviewSpecialist()
        endpoint._git_add_and_commit(worktree, ["rtl.sv"], "feat(rtl): recover stale lock")

        assert not lock_path.exists()
        log = subprocess.check_output(
            ["git", "-C", str(worktree), "log", "--oneline", "-1"],
            text=True,
        )
        assert "feat(rtl): recover stale lock" in log


def _stealth_project(root: Path, extra: str = "") -> Path:
    """A Project in Stealth Mode, which must be enabled explicitly."""
    (root / ".booley").mkdir()
    (root / ".booley" / "booley.toml").write_text(f"[stealth]\nenabled = true\n{extra}")
    return root


class TestBannedPhraseNote:
    """commit_msg_banned_phrase_note() exposes the live banned list to agents."""

    def test_note_starts_with_header(self, tmp_path):
        note = Specialist.commit_msg_banned_phrase_note(_stealth_project(tmp_path))
        assert note.startswith("## Banned Words")


def test_specialist_preserves_files_outside_its_selected_paths(monkeypatch):
    specialist = ReviewSpecialist()
    monkeypatch.setattr(
        specialist, "_get_uncommitted_files", lambda: ["selected.sv", "outside.sv"]
    )

    def unexpected_revert(_):
        pytest.fail("Outside-scope work must remain available for Developer review")

    monkeypatch.setattr(specialist, "_revert_out_of_scope", unexpected_revert)
    assert specialist._resolve_stageable_files(["selected.sv"]) == (["selected.sv"], [])


def test_privacy_note_tracks_audited_default_vocabulary(tmp_path) -> None:
    note = Specialist.commit_msg_banned_phrase_note(_stealth_project(tmp_path)).lower()
    for term in (
        "claude",
        "anthropic",
        "copilot",
        "codex",
        "openai",
        "chatgpt",
        "gemini",
        "booley",
        "gpt",
        "llm",
        "co-authored-by",
    ):
        assert term in note
    for term in (
        "ticket",
        "cursor",
        "generated",
        "docker",
        "agent",
        "auto-review",
        "auto-approve",
        "per review findings",
        "as suggested by",
        "automated fix",
        "suggested fix",
    ):
        assert term not in note


def test_specialist_note_describes_both_matching_tiers(tmp_path):
    _stealth_project(tmp_path, 'banned_words = ["agent"]\nbanned_substrings = ["quokka"]\n')
    note = Specialist.commit_msg_banned_phrase_note(tmp_path)
    assert "agent" in note and "quokka" in note
    assert "CamelCase" in note and "banned_substrings match anywhere" in note
    assert "word-boundary" not in note


@pytest.mark.parametrize("target", ["", "sim,other"])
def test_specialist_without_eda_omits_report_tool(tmp_path, target):
    import json

    endpoint = ReviewSpecialist()
    endpoint.parse_args(["--report-dir", str(tmp_path), "--target", target])
    path = endpoint.write_report(McpToolResult(report_text="review complete"))
    report = json.loads(path.read_text())
    assert "eda_tool" not in report
    assert "eda_tools" not in report
    endpoint.record_eda_tool("sim", "verilator")
    endpoint.reset_report_metadata()
    path = endpoint.write_report(McpToolResult(report_text="second review"))
    assert "eda_tool" not in json.loads(path.read_text())
