"""Tests for Specialist — model and Agent CLI resolution, argparse, failure reports."""

from __future__ import annotations

import argparse
import contextlib
import dataclasses
import os
from pathlib import Path
from unittest.mock import patch

import pytest

from booley.core.models import AgentCallParams
from booley.mcp.base import EXIT_FAILURE, EXIT_SUCCESS, McpToolResult
from booley.runtime.agent_errors import (
    AgentAuthError,
    AgentTimeoutError,
    TransientAPIError,
    UsageLimitError,
)
from booley.specialists.specialist import Specialist


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
            return McpToolResult(exit_code=EXIT_SUCCESS)
        return McpToolResult(exit_code=EXIT_FAILURE)


class TestSpecialistArgparse:
    @pytest.mark.parametrize(
        ("argv", "instruction", "diff_ref"),
        [
            (
                ["--instruction", "focus on timing", "--diff-ref", "HEAD~3"],
                "focus on timing",
                "HEAD~3",
            ),
            ([], "", "HEAD~1"),
        ],
    )
    def test_agent_args_and_defaults(self, tmp_path: Path, argv, instruction, diff_ref):
        with patch.dict(os.environ, _env_with_state(tmp_path / "state.json")):
            args = ReviewSpecialist().parse_args(argv)
        assert (args.instruction, args.diff_ref) == (instruction, diff_ref)

    @pytest.mark.parametrize("value", ["0", "-1", "1.5", "words"])
    def test_max_turns_must_be_a_positive_integer(self, value: str):
        with pytest.raises(SystemExit):
            ReviewSpecialist().parse_args(["--max-turns", value])

    def test_hidden_options_do_not_accept_abbreviations(self):
        with pytest.raises(SystemExit):
            ReviewSpecialist().parse_args(["--transcript", "injected"])


def test_reviewer_model_calls_use_the_raised_minimum():
    """The reviewer overrides ``_run`` without the base clamp, so every model call is raised here."""
    from booley.specialists.reviewer import ReviewerSpecialist

    reviewer = ReviewerSpecialist()
    reviewer.parse_args(["--timeout-ms", "300000"])
    assert reviewer.model_call_timeout_ms(300_000) == 600_000
    assert reviewer.timeout_seconds() == 600  # asked for 5m
    reviewer.parse_args(["--timeout-ms", "3600000"])
    assert reviewer.timeout_seconds() == 3600  # above the minimum: unchanged


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
        # A provider failure cannot be provoked through the public path without a backend.
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

        # The one seam that swaps the real Agent CLI backend for the recording fake.
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


def _launch(endpoint: Specialist, tmp_path: Path, *extra: str) -> McpToolResult:
    """Run *endpoint* as the CLI does: model and CLI resolution, invocation, finalization."""
    state_file = tmp_path / "state.json"
    state_file.write_text("{}")
    with patch.dict(os.environ, _env_with_state(state_file)):
        return endpoint.execute_cli(
            [
                "--work-dir",
                str(tmp_path),
                "--report-dir",
                str(tmp_path / "reports"),
                "--transcript-dir",
                str(tmp_path / "transcripts"),
                *extra,
            ]
        ).outcome


@pytest.mark.parametrize(("asked_ms", "expected_s"), [(300_000, 600), (3_600_000, 3600)])
def test_model_call_timeout_is_raised_to_the_minimum(
    tmp_path: Path, per_cli_backends, asked_ms: int, expected_s: int
) -> None:
    """A below-minimum ``--timeout-ms`` is raised to the Specialist's minimum; above it is kept."""
    from booley.config.agent import AgentSettings

    _built, calls = per_cli_backends(AgentSettings(default_cli="claude", clis=("claude",)))

    _launch(_named("reviewer"), tmp_path, "--timeout-ms", str(asked_ms))

    assert [params.timeout_seconds for _cli, params in calls] == [expected_s]


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
    assert _launch(_named("coverage_analyst"), tmp_path).exit_code == EXIT_SUCCESS

    (reviewer_cli, reviewer), (coverage_cli, coverage) = calls
    assert (reviewer_cli, reviewer.model, reviewer.reasoning_effort) == (
        "codex",
        "gpt-6-luna",
        "high",
    )
    assert (coverage_cli, coverage.model, coverage.reasoning_effort) == (
        "claude",
        "claude-opus-5-5",
        None,
    )
    # The default CLI's backend is built up front; codex's once, on first use, with its auth.
    assert built == [("claude", "subscription"), ("codex", "api_key")]


@pytest.mark.parametrize("pinned", [True, False], ids=["pinned", "built_in"])
def test_refused_model_raises_refusal_and_tries_no_other_model(
    tmp_path: Path, per_cli_backends, pinned: bool
) -> None:
    from booley.config.agent import AgentSettings
    from booley.runtime.agent_errors import model_unavailable_error

    refused = "gpt-6-luna" if pinned else "gpt-built-in"  # no `[models]` key selects the latter

    def refuse(params: AgentCallParams):
        # Built as the Codex backend builds it: inside the call, from the live settings.
        raise model_unavailable_error(
            refused, "codex", '{"status":400,"error":{"code":"model_not_found"}}'
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
    assert f"'{refused}'" in report
    if pinned:
        assert "`[models] reviewer`" in report
        assert "Protected Input" in report
    else:
        assert "`[agent] default`" in report
    assert "model_not_found" not in report


# --- hidden [models] fallback list (#2836, ADR 0084 amendment) ---------------------


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
    # The module-global refusal memory has no public reset; each test must start it empty.
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


@pytest.mark.parametrize(
    ("logged_in", "clis", "default"),
    [
        ({"codex"}, ("claude", "codex"), "claude"),  # Claude has no login
        ({"claude", "codex"}, ("codex",), "codex"),  # Claude is not in [agent] clis
    ],
)
def test_model_list_skips_entry_whose_cli_cannot_run(
    tmp_path: Path, per_cli_backends, logins, logged_in, clis, default
) -> None:
    logins.update(logged_in)
    _built, calls = per_cli_backends(_list_settings(clis=clis, default=default))

    result = _launch(_named("reviewer"), tmp_path)

    assert result.exit_code == EXIT_SUCCESS
    # No model call for the skipped Claude entry; Luna runs with Codex's effort.
    assert [(cli, p.model, p.reasoning_effort) for cli, p in calls] == [
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
    _launch(endpoint, tmp_path)  # round 1 runs on Luna (Claude has no login)

    # A resumed call is only built inside the reviewer's multi-round loop, so build it here.
    params = endpoint._agent_call_params()  # the reviewer builds this inside its round loop
    resumed = dataclasses.replace(params, session_id="codex-session", resume_session=True)
    endpoint._invoke_agent(resumed)  # the call the reviewer makes on a retry round

    cli, params = calls[-1]
    assert (cli, params.model) == ("codex", "gpt-6-luna")
    assert (params.session_id, params.resume_session) == ("codex-session", True)


def test_model_list_runs_first_entry_when_both_clis_are_logged_in(
    tmp_path: Path, per_cli_backends, logins
) -> None:
    logins.update({"claude", "codex"})
    _built, calls = per_cli_backends(_list_settings())

    result = _launch(_named("reviewer"), tmp_path)

    assert result.exit_code == EXIT_SUCCESS
    assert [(cli, params.model) for cli, params in calls] == [("claude", "claude-opus-5-5")]
    assert result.detail["specialist_model"] == {"model": "claude-opus-5-5", "agent_cli": "claude"}
    assert "Ran on" not in result.report_text


def test_model_list_refusal_moves_on_and_is_remembered(
    tmp_path: Path, per_cli_backends, logins
) -> None:
    logins.update({"claude", "codex"})
    _built, calls = per_cli_backends(_list_settings(), respond=_refuse_model("claude-opus-5-5"))

    first = _launch(_named("reviewer"), tmp_path)
    second = _launch(_named("reviewer"), tmp_path)

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


@pytest.mark.parametrize(
    "error",
    [
        AgentAuthError("401 unauthorized", "claude"),
        UsageLimitError("usage limit reached", "claude"),
        TransientAPIError("529 overloaded"),
        AgentTimeoutError("deadline reached"),
    ],
    ids=lambda error: type(error).__name__,
)
def test_model_list_non_refusal_errors_do_not_try_next_entry(
    tmp_path: Path, per_cli_backends, logins, error
) -> None:
    logins.update({"claude", "codex"})

    def fail(_params: AgentCallParams):
        raise error

    _built, calls = per_cli_backends(_list_settings(), respond=fail)

    result = _launch(_named("reviewer"), tmp_path)

    assert result.exit_code == 2
    assert [(cli, p.model) for cli, p in calls] == [("claude", "claude-opus-5-5")]
    assert type(error).__name__ in result.report_text


def test_model_list_without_usable_entry_names_each_entry_and_reason(
    tmp_path: Path, per_cli_backends, logins
) -> None:
    logins.update({"codex"})  # Claude has no login; Luna is refused
    _built, calls = per_cli_backends(_list_settings(), respond=_refuse_model("gpt-6-luna"))

    result = _launch(_named("reviewer"), tmp_path)

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
    result = _launch(_named("reviewer"), tmp_path)

    assert [(cli, params.model) for cli, params in calls[earlier:]] == [("codex", "gpt-6-luna")]
    assert result.exit_code == 2
    assert result.report_text.startswith("reviewer failed: ModelUnavailableError: ")
    assert "specialist_model" not in result.detail


def test_specialist_result_and_evidence_record_model_and_fallback(
    tmp_path: Path, per_cli_backends, logins
) -> None:
    logins.update({"codex"})
    _built, _calls = per_cli_backends(_list_settings())
    endpoint = _named("reviewer")

    result = _launch(endpoint, tmp_path)

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


@pytest.mark.parametrize("with_dir", [True, False])
def test_transcript_path_exists_only_when_a_directory_is_given(tmp_path: Path, with_dir: bool):
    endpoint = ReviewSpecialist()
    argv = ["--transcript-dir", str(tmp_path / "transcripts")] if with_dir else []
    with patch.dict(os.environ, _env_with_state(tmp_path / "state.json")):
        endpoint.parse_args(argv)
    path = endpoint.transcript_path()
    if with_dir:
        assert path is not None and path.name == "review_test.jsonl"
        assert path.parent.exists()
    else:
        assert path is None


@pytest.mark.parametrize("where", ["logs_dir", "project_runtime", "no_project", "no_session"])
def test_session_id_persists_where_resume_can_find_it(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, where: str
):
    """SETUP-F-42: outside Ticket Mode there is no BOOLEY_LOGS_DIR, and returning nothing
    there silently disabled resume, so every retry round cold-started the agent."""
    from booley.runtime.booley_dir import reset_cache

    logs = tmp_path / "logs"
    logs.mkdir()
    if where == "logs_dir":
        monkeypatch.setenv("BOOLEY_LOGS_DIR", str(logs))
    else:
        monkeypatch.delenv("BOOLEY_LOGS_DIR", raising=False)
    if where == "no_project":
        # No ticket and no Project on disk: nowhere to persist.
        monkeypatch.delenv("BOOLEY_DIR", raising=False)
        monkeypatch.chdir(logs)
        reset_cache()
    endpoint = ReviewSpecialist()
    # The session id is set by an agent call; this test only covers where it is written.
    endpoint._last_session_id = None if where == "no_session" else "sess-abc-123"

    # The reviewer's multi-round run is the only public caller of these two.
    endpoint._persist_session_id("k")

    written = {
        "logs_dir": logs / "k.session_id",
        # The autouse _set_booley_dir fixture pins BOOLEY_DIR=tmp_path.
        "project_runtime": tmp_path / ".runtime" / "sessions" / "k.session_id",
    }.get(where)
    if written is None:
        assert not list(logs.iterdir())
        assert not (tmp_path / ".runtime" / "sessions" / "k.session_id").exists()
    else:
        assert written.read_text(encoding="utf-8") == "sess-abc-123"


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
