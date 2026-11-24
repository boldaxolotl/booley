"""Codex Specialists under ``BOOLEY_PRIMARY_AUTH=gateway`` (#2266).

A fake ``codex`` executable (``tests/codex_cli_fake.py``) stands in for the real
CLI: it reports what it was launched with (selected provider, the provider's
token command and that command's output, credential variables, ``auth.json``),
so the tests observe the process boundary rather than Booley's internals.
"""

from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path

import pytest
from tests.codex_cli_fake import STALE_TOKEN, codex_report, install_fake_codex, write_codex_login
from tests.gateway_command_support import count_runs, write_token_command

from booley.config import agent as agent_config
from booley.runtime import _codex_backend as cb
from booley.runtime import agent_errors, gateway_token

_URL = "https://gateway.example/v1"
_TOKEN = "SYNTHETIC_GATEWAY_TOKEN"
_NATIVE_CREDENTIALS = {
    "OPENAI_API_KEY": "SYNTHETIC_OPENAI_KEY",
    "ANTHROPIC_API_KEY": "SYNTHETIC_ANTHROPIC_KEY",
    "ANTHROPIC_AUTH_TOKEN": "SYNTHETIC_ANTHROPIC_BEARER",
    "CLAUDE_CODE_OAUTH_TOKEN": "SYNTHETIC_CLAUDE_OAUTH",
}
_FRESH_TOKEN = "SYNTHETIC_FRESH_TOKEN"
# Larger than any OS pipe buffer: a Codex that exits without reading stdin breaks the
# pipe mid-write, and a Codex that reads it must read all of it (#3886).
_LARGE_PROMPT = "x" * (4 * 1024 * 1024)


@pytest.fixture
def gateway_sandbox(tmp_path, monkeypatch):
    """A container-like process with every native credential set and a fake ``codex``."""
    install_fake_codex(tmp_path, monkeypatch)
    write_codex_login(
        tmp_path,
        json.dumps({"tokens": {"access_token": "SYNTHETIC_NATIVE_LOGIN"}, "OPENAI_API_KEY": None}),
    )
    monkeypatch.setenv("BOOLEY_GATEWAY_URL", _URL)
    monkeypatch.setenv("BOOLEY_GATEWAY_TOKEN", _TOKEN)
    for name, value in _NATIVE_CREDENTIALS.items():
        monkeypatch.setenv(name, value)
    monkeypatch.delenv("BOOLEY_PRIMARY_AUTH", raising=False)
    agent_config.set_agent_settings(None)
    yield
    agent_config.set_agent_settings(None)


def _use_token_command(monkeypatch, tmp_path, tokens):
    """Switch the sandbox from a static token to a fake token command.

    Returns ``(command, command_counter, codex_launch_counter)``.
    """
    monkeypatch.delenv("BOOLEY_GATEWAY_TOKEN")
    command, command_counter = write_token_command(tmp_path, tokens)
    launches = tmp_path / "codex-launches.txt"
    monkeypatch.setenv("BOOLEY_GATEWAY_TOKEN_COMMAND", command)
    monkeypatch.setenv("FAKE_CODEX_LAUNCHES", str(launches))
    return command, command_counter, launches


def _written_texts(root):
    return [
        path.read_text(encoding="utf-8", errors="replace")
        for path in root.rglob("*")
        if path.is_file()
    ]


def _assert_no_secret(report, root, *secrets):
    """No secret in the fake's argv or in any file written under *root*."""
    written = _written_texts(root)
    assert written, "the private Codex home must contain the generated config"
    for secret in secrets:
        assert not any(secret in argument for argument in report["argv"])
        assert not any(secret in text for text in written)


@pytest.mark.asyncio
@pytest.mark.parametrize("source", ["static", "command"])
async def test_fake_codex_specialist_reaches_gateway_with_the_token_and_no_native_login(
    gateway_sandbox, tmp_path, monkeypatch, source
):
    token, command = _TOKEN, None
    if source == "command":
        token = _FRESH_TOKEN
        command, _, _ = _use_token_command(monkeypatch, tmp_path, [token])

    report = await codex_report(tmp_path, auth_mode="gateway", prompt=_LARGE_PROMPT)

    assert report["model_provider"] == "booley_gateway"
    assert report["base_url"] == _URL
    assert report["fast_mode"] is False
    assert report["shell_exclude"] == ["BOOLEY_GATEWAY_TOKEN"]
    assert report["token_run"]["stdout"] == token
    assert report["token_run"]["code"] == 0
    assert report["token_command_var_present"] is False
    assert report["credentials_present"] == []
    assert report["auth_json_exists"] is False
    # The fake read the whole prompt (#3886).
    assert report["prompt_bytes"] == len(_LARGE_PROMPT)
    home = Path(report["home"]).parent
    _assert_no_secret(report, home, token, *([command] if command else []))
    assert "BOOLEY_GATEWAY_URL" in "".join(_written_texts(home))


@pytest.mark.asyncio
@pytest.mark.parametrize("missing", ["BOOLEY_GATEWAY_URL", "BOOLEY_GATEWAY_TOKEN"])
async def test_gateway_spawn_with_incomplete_config_fails_naming_the_missing_variable(
    gateway_sandbox, tmp_path, monkeypatch, missing
):
    monkeypatch.delenv(missing)

    with pytest.raises(ValueError, match=missing):
        await codex_report(tmp_path, auth_mode="gateway")


@pytest.mark.asyncio
async def test_fake_codex_text_only_specialist_reaches_gateway_with_pinned_model_and_token(
    gateway_sandbox, tmp_path, monkeypatch
):
    monkeypatch.setenv("BOOLEY_PRIMARY_AUTH", "gateway")
    monkeypatch.setenv("BOOLEY_PRIMARY_PROVIDER", "codex")
    monkeypatch.setenv("BOOLEY_GATEWAY_MODEL", "gw-text-model")
    monkeypatch.setenv("BOOLEY_GATEWAY_EFFORT", "low")
    command, _, _ = _use_token_command(monkeypatch, tmp_path, [_FRESH_TOKEN])
    settings = agent_config.get_agent_settings()
    analysis = tmp_path / "analysis"
    analysis.mkdir()

    # A Coverage Analyst call: text-only, one nested tool, the gateway's pinned model.
    report = await codex_report(
        analysis,
        auth_mode=settings.auth_for("codex"),
        model=settings.model_for_specialist("coverage_analyst"),
        reasoning_effort=settings.effort_for("codex"),
        text_only=True,
        nested_mcp_tools=["coverage_evidence"],
        allowed_agent_capabilities=[],
        transcript_path=tmp_path / "transcripts" / "run.jsonl",
    )

    assert settings.auth_for("codex") == "gateway"
    assert report["model_provider"] == "booley_gateway"
    assert report["base_url"] == _URL
    assert report["token_run"]["stdout"] == _FRESH_TOKEN
    assert report["token_command_var_present"] is False
    assert report["shell_exclude"] == ["BOOLEY_GATEWAY_TOKEN"]
    assert report["argv"][report["argv"].index("-m") + 1] == "gw-text-model"
    assert 'model_reasoning_effort="low"' in report["argv"]
    (entry,) = report["catalog"]["models"]
    assert entry["slug"] == "gw-text-model"
    assert entry["shell_type"] == "disabled"
    assert "low" in [level["effort"] for level in entry["supported_reasoning_levels"]]
    assert report["credentials_present"] == []
    assert report["auth_json_exists"] is False
    assert Path(report["home"]).is_relative_to(analysis)
    _assert_no_secret(report, analysis, _FRESH_TOKEN, command)


def test_codex_health_check_reports_gateway_misconfiguration(gateway_sandbox, monkeypatch):
    monkeypatch.delenv("BOOLEY_GATEWAY_TOKEN")

    assert "BOOLEY_GATEWAY_TOKEN" in (cb.CodexBackend(auth_mode="gateway").health_check() or "")


def test_gateway_token_entry_point_fails_loud_when_token_is_missing(monkeypatch, capsys):
    monkeypatch.setenv("BOOLEY_GATEWAY_URL", _URL)
    monkeypatch.delenv("BOOLEY_GATEWAY_TOKEN", raising=False)

    assert gateway_token.main() == 1
    captured = capsys.readouterr()
    assert "BOOLEY_GATEWAY_TOKEN" in captured.err
    assert captured.out == ""


@pytest.mark.asyncio
async def test_gateway_token_command_ignores_shadowing_booley_package_in_cwd(
    gateway_sandbox, tmp_path
):
    """The token is in the environment; a Project ``booley/`` directory must not run."""
    report = await codex_report(tmp_path, auth_mode="gateway")
    shadow = tmp_path / "shadow"
    (shadow / "booley").mkdir(parents=True)
    (shadow / "booley" / "__init__.py").write_bytes(b"raise SystemExit('shadowed')\n")

    command = [report["auth"]["command"], *report["auth"]["args"]]
    done = subprocess.run(
        command,
        cwd=shadow,
        env=dict(os.environ, **{"BOOLEY_GATEWAY_TOKEN": _TOKEN}),
        capture_output=True,
        check=False,
        text=True,
    )

    assert done.stdout == _TOKEN
    assert "shadowed" not in done.stderr


# --- #2267: BOOLEY_GATEWAY_TOKEN_COMMAND ------------------------------------


@pytest.mark.asyncio
async def test_gateway_runs_within_ttl_share_one_token_command_run(
    gateway_sandbox, tmp_path, monkeypatch
):
    _, command_runs, launches = _use_token_command(monkeypatch, tmp_path, [_FRESH_TOKEN, "OTHER"])

    first = await codex_report(tmp_path, auth_mode="gateway")
    second = await codex_report(tmp_path, auth_mode="gateway")

    assert first["token_run"]["stdout"] == second["token_run"]["stdout"] == _FRESH_TOKEN
    assert count_runs(command_runs) == 1
    assert count_runs(launches) == 2


@pytest.mark.asyncio
async def test_gateway_auth_failure_reruns_token_command_once_and_retries_run(
    gateway_sandbox, tmp_path, monkeypatch
):
    _, command_runs, launches = _use_token_command(
        monkeypatch, tmp_path, [STALE_TOKEN, _FRESH_TOKEN]
    )

    report = await codex_report(tmp_path, auth_mode="gateway")

    assert report["token_run"]["stdout"] == _FRESH_TOKEN
    assert count_runs(command_runs) == 2
    assert count_runs(launches) == 2
    assert (tmp_path / "transcript.jsonl").exists()
    assert (tmp_path / "transcript-retry2.jsonl").exists()


@pytest.mark.asyncio
@pytest.mark.parametrize("source", ["static", "command"])
async def test_gateway_stale_token_is_an_auth_failure_only_for_a_token_command(
    gateway_sandbox, tmp_path, monkeypatch, source
):
    """The fake rejects the token without reading stdin (the large prompt breaks the pipe).

    Booley must still classify the 401 from Codex's own output: a token command's 401
    re-runs the command once and launches again; a static token's 401 is final.
    """
    launches = tmp_path / "codex-launches.txt"
    if source == "command":
        _, command_runs, launches = _use_token_command(monkeypatch, tmp_path, [STALE_TOKEN])
    else:
        monkeypatch.setenv("BOOLEY_GATEWAY_TOKEN", STALE_TOKEN)
        monkeypatch.setenv("FAKE_CODEX_LAUNCHES", str(launches))

    with pytest.raises(agent_errors.AgentProviderError) as failure:
        await codex_report(tmp_path, auth_mode="gateway", prompt=_LARGE_PROMPT)

    assert isinstance(failure.value, agent_errors.AgentAuthError) is (source == "command")
    assert count_runs(launches) == (2 if source == "command" else 1)
    if source == "command":
        assert count_runs(command_runs) == 2
