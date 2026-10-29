"""Codex Specialists under ``BOOLEY_PRIMARY_AUTH=gateway`` (#2266).

A fake ``codex`` executable stands in for the real CLI: it reports what it was
launched with (selected provider, the provider's token command and that
command's output, credential variables, ``auth.json``), so the tests observe
the process boundary rather than Booley's internals.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest
from tests.gateway_command_support import count_runs, write_token_command

from booley.config import agent as agent_config
from booley.core.models import AgentCallParams
from booley.runtime import _codex_backend as cb
from booley.runtime import agent_errors, gateway_token
from booley.runtime.agent_errors import AgentProviderError

_URL = "https://gateway.example/v1"
_TOKEN = "SYNTHETIC_GATEWAY_TOKEN"
_NATIVE_CREDENTIALS = {
    "OPENAI_API_KEY": "SYNTHETIC_OPENAI_KEY",
    "ANTHROPIC_API_KEY": "SYNTHETIC_ANTHROPIC_KEY",
    "ANTHROPIC_AUTH_TOKEN": "SYNTHETIC_ANTHROPIC_BEARER",
    "CLAUDE_CODE_OAUTH_TOKEN": "SYNTHETIC_CLAUDE_OAUTH",
}
_STALE_TOKEN = "SYNTHETIC_STALE_TOKEN"
_FRESH_TOKEN = "SYNTHETIC_FRESH_TOKEN"
_SRC = Path(__file__).resolve().parents[2] / "src"

_FAKE_CODEX = """\
import json, os, subprocess, sys, tomllib

home = os.environ["CODEX_HOME"]
with open(os.path.join(home, "config.toml"), "rb") as handle:
    config = tomllib.load(handle)
provider_id = config.get("model_provider")
provider = config.get("model_providers", {}).get(provider_id, {})
auth = provider.get("auth", {})
token_run = None
if auth:
    done = subprocess.run(
        [auth["command"], *auth.get("args", [])],
        cwd=auth.get("cwd"),
        capture_output=True,
        check=False,
        text=True,
    )
    token_run = {"stdout": done.stdout, "stderr": done.stderr, "code": done.returncode}
if os.environ.get("FAKE_CODEX_LAUNCHES"):
    with open(os.environ["FAKE_CODEX_LAUNCHES"], "a", encoding="utf-8") as launches:
        launches.write("launch\\n")
if token_run and token_run["stdout"] == "SYNTHETIC_STALE_TOKEN":
    # The shape of Codex 0.164 when the gateway answers 401 (after its own retries).
    failure = (
        "unexpected status 401 Unauthorized: invalid token, "
        "url: https://gateway.example/v1/responses"
    )
    print(json.dumps({"type": "error", "message": failure}))
    print(json.dumps({"type": "turn.failed", "error": {"message": failure}}))
    sys.exit(1)
catalog = None
for index, argument in enumerate(sys.argv):
    if argument == "-c" and sys.argv[index + 1].startswith("model_catalog_json="):
        catalog_path = json.loads(sys.argv[index + 1].split("=", 1)[1])
        with open(catalog_path, encoding="utf-8") as handle:
            catalog = json.load(handle)
report = {
    "model_provider": provider_id,
    "base_url": provider.get("base_url"),
    "fast_mode": config.get("features", {}).get("fast_mode"),
    "shell_exclude": config.get("shell_environment_policy", {}).get("exclude"),
    "auth": auth,
    "token_run": token_run,
    "auth_json_exists": os.path.exists(os.path.join(home, "auth.json")),
    "credentials_present": sorted(
        name
        for name in ("OPENAI_API_KEY", "ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN",
                     "CLAUDE_CODE_OAUTH_TOKEN")
        if os.environ.get(name)
    ),
    "token_command_var_present": "BOOLEY_GATEWAY_TOKEN_COMMAND" in os.environ,
    "home": home,
    "argv": sys.argv[1:],
    "catalog": catalog,
}
message = {"type": "item.completed", "item": {"type": "agent_message", "text": json.dumps(report)}}
print(json.dumps(message))
"""


@pytest.fixture
def gateway_sandbox(tmp_path, monkeypatch):
    """A container-like process with every native credential set and a fake ``codex``."""
    if sys.platform == "win32":
        pytest.skip("the fake codex is a POSIX shebang script")
    real_path = Path

    def isolated_path(value):
        value = str(value)
        return (
            tmp_path / real_path(value).name
            if value.startswith("/tmp/codex-")
            else real_path(value)
        )

    home = tmp_path / "home"
    (home / ".codex").mkdir(parents=True)
    (home / ".codex" / "auth.json").write_text(
        json.dumps({"tokens": {"access_token": "SYNTHETIC_NATIVE_LOGIN"}, "OPENAI_API_KEY": None}),
        encoding="utf-8",
    )
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    fake = fake_bin / "codex"
    fake.write_text(f"#!{sys.executable}\n{_FAKE_CODEX}", encoding="utf-8")
    fake.chmod(0o755)

    monkeypatch.setattr(cb, "Path", isolated_path)
    monkeypatch.setattr(cb, "_NESTED_HOMES", {})
    monkeypatch.setattr(cb, "_inside_container", lambda: True)
    monkeypatch.setattr(agent_config, "_inside_container", lambda: True)
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.delenv("CODEX_HOME", raising=False)
    monkeypatch.setenv("BOOLEY_SLUG", "gateway-spawn")
    monkeypatch.setenv("PATH", f"{fake_bin}{os.pathsep}{os.environ['PATH']}")
    # In a Sandbox booley is installed; here it comes from the source tree.
    monkeypatch.setenv("PYTHONPATH", str(_SRC))
    monkeypatch.setenv("BOOLEY_GATEWAY_URL", _URL)
    monkeypatch.setenv("BOOLEY_GATEWAY_TOKEN", _TOKEN)
    for name, value in _NATIVE_CREDENTIALS.items():
        monkeypatch.setenv(name, value)
    monkeypatch.delenv("BOOLEY_PRIMARY_AUTH", raising=False)
    agent_config.set_agent_settings(None)
    yield fake
    agent_config.set_agent_settings(None)


def _params(tmp_path, **overrides):
    return AgentCallParams(prompt="p", model="gpt-5.5", cwd=str(tmp_path), **overrides)


async def _run_fake_codex(tmp_path, fake, auth_mode="gateway"):
    """Spawn the fake through the real Codex spawn path and return its report."""
    proc, _ = await cb._codex_spawn([str(fake)], _params(tmp_path), auth_mode)
    stdout, _stderr = await proc.communicate(b"")
    event = json.loads(stdout.decode().splitlines()[0])
    return json.loads(event["item"]["text"])


@pytest.mark.asyncio
async def test_fake_codex_specialist_sees_gateway_provider_url_and_token(
    gateway_sandbox, tmp_path
):
    report = await _run_fake_codex(tmp_path, gateway_sandbox)

    assert report["model_provider"] == "booley_gateway"
    assert report["base_url"] == _URL
    assert report["fast_mode"] is False
    assert report["shell_exclude"] == ["BOOLEY_GATEWAY_TOKEN"]
    assert report["token_run"]["stdout"] == _TOKEN
    assert report["token_run"]["code"] == 0


@pytest.mark.asyncio
async def test_specialist_settings_from_env_drive_codex_backend_to_gateway(
    gateway_sandbox, tmp_path, monkeypatch
):
    monkeypatch.setenv("BOOLEY_PRIMARY_AUTH", "gateway")
    monkeypatch.setenv("BOOLEY_PRIMARY_PROVIDER", "codex")

    settings = agent_config.get_agent_settings()
    backend = cb.CodexBackend(auth_mode=settings.auth)
    result = await backend._call_once(
        _params(tmp_path), full_prompt="p", transcript_path=tmp_path / "transcript.jsonl"
    )
    report = json.loads(result.output)

    assert settings.auth == "gateway"
    assert report["model_provider"] == "booley_gateway"
    assert report["base_url"] == _URL
    assert report["token_run"]["stdout"] == _TOKEN


@pytest.mark.asyncio
async def test_fake_codex_specialist_under_gateway_sees_no_native_credential_or_auth_json(
    gateway_sandbox, tmp_path
):
    report = await _run_fake_codex(tmp_path, gateway_sandbox)

    assert report["credentials_present"] == []
    assert report["auth_json_exists"] is False


@pytest.mark.asyncio
async def test_gateway_token_is_not_written_into_private_codex_home(gateway_sandbox, tmp_path):
    report = await _run_fake_codex(tmp_path, gateway_sandbox)

    written = [
        path.read_text(encoding="utf-8", errors="replace")
        for path in Path(report["home"]).parent.rglob("*")
        if path.is_file()
    ]
    assert written, "the private Codex home must contain the generated config"
    assert not any(_TOKEN in text for text in written)
    assert "BOOLEY_GATEWAY_URL" in "".join(written)


@pytest.mark.asyncio
@pytest.mark.parametrize("missing", ["BOOLEY_GATEWAY_URL", "BOOLEY_GATEWAY_TOKEN"])
async def test_gateway_spawn_with_incomplete_config_fails_naming_the_missing_variable(
    gateway_sandbox, tmp_path, monkeypatch, missing
):
    monkeypatch.delenv(missing)

    with pytest.raises(ValueError, match=missing):
        await cb._codex_spawn([str(gateway_sandbox)], _params(tmp_path), "gateway")


def _coverage_params(tmp_path):
    """Real Coverage Analyst call parameters: text-only, one nested tool, pinned model."""
    from booley.specialists.coverage_analyst import CoverageAnalystSpecialist

    project = tmp_path / "project"
    project.mkdir()
    analyst = CoverageAnalystSpecialist()
    analyst.parse_args(["--project", str(project), "--campaign", str(tmp_path / "campaign.json")])
    analysis_dir = tmp_path / "analysis"
    analysis_dir.mkdir()
    params = analyst._coverage_agent_params(
        "p", analysis_dir, tmp_path / "campaign.json", analysis_dir / "evidence-audit.json"
    )
    params.transcript_path = tmp_path / "transcripts" / "run.jsonl"
    return params


def _assert_no_secret(report, root, *secrets):
    """No secret in the fake's argv or in any file written under *root*."""
    written = [
        path.read_text(encoding="utf-8", errors="replace")
        for path in root.rglob("*")
        if path.is_file()
    ]
    assert written, "the analysis directory must contain the private Codex home"
    for secret in secrets:
        assert not any(secret in argument for argument in report["argv"])
        assert not any(secret in text for text in written)


@pytest.mark.asyncio
async def test_fake_codex_text_only_specialist_reaches_gateway_with_pinned_model(
    gateway_sandbox, tmp_path, monkeypatch
):
    monkeypatch.setenv("BOOLEY_PRIMARY_AUTH", "gateway")
    monkeypatch.setenv("BOOLEY_PRIMARY_PROVIDER", "codex")
    monkeypatch.setenv("BOOLEY_GATEWAY_MODEL", "gw-text-model")
    monkeypatch.setenv("BOOLEY_GATEWAY_EFFORT", "low")
    settings = agent_config.get_agent_settings()
    params = _coverage_params(tmp_path)

    result = await cb.CodexBackend(auth_mode=settings.auth)._call_once(
        params, full_prompt="p", transcript_path=params.transcript_path
    )
    report = json.loads(result.output)

    assert params.text_only
    assert report["model_provider"] == "booley_gateway"
    assert report["base_url"] == _URL
    assert report["token_run"]["stdout"] == _TOKEN
    assert report["argv"][report["argv"].index("-m") + 1] == "gw-text-model"
    assert 'model_reasoning_effort="low"' in report["argv"]
    (entry,) = report["catalog"]["models"]
    assert entry["slug"] == "gw-text-model"
    assert entry["shell_type"] == "disabled"
    assert "low" in [level["effort"] for level in entry["supported_reasoning_levels"]]
    assert report["credentials_present"] == []
    assert report["auth_json_exists"] is False
    assert Path(report["home"]).is_relative_to(tmp_path / "analysis")
    _assert_no_secret(report, tmp_path / "analysis", _TOKEN)


@pytest.mark.asyncio
async def test_fake_codex_text_only_specialist_gets_token_command_token(
    gateway_sandbox, tmp_path, monkeypatch
):
    monkeypatch.setenv("BOOLEY_PRIMARY_AUTH", "gateway")
    monkeypatch.setenv("BOOLEY_PRIMARY_PROVIDER", "codex")
    command, _, _ = _use_token_command(monkeypatch, tmp_path, [_FRESH_TOKEN])
    params = _coverage_params(tmp_path)

    result = await cb.CodexBackend(auth_mode="gateway").call(params)
    report = json.loads(result.output)

    assert report["token_run"]["stdout"] == _FRESH_TOKEN
    assert report["token_command_var_present"] is False
    assert report["shell_exclude"] == ["BOOLEY_GATEWAY_TOKEN"]
    _assert_no_secret(report, tmp_path / "analysis", _FRESH_TOKEN, command)


def test_codex_health_check_reports_gateway_misconfiguration(gateway_sandbox, monkeypatch):
    monkeypatch.delenv("BOOLEY_GATEWAY_TOKEN")

    assert "BOOLEY_GATEWAY_TOKEN" in cb.CodexBackend(auth_mode="gateway").health_check()


def test_gateway_token_entry_point_fails_loud_when_token_is_missing(monkeypatch, capsys):
    from booley.runtime import gateway_token

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
    report = await _run_fake_codex(tmp_path, gateway_sandbox)
    shadow = tmp_path / "shadow"
    (shadow / "booley").mkdir(parents=True)
    (shadow / "booley" / "__init__.py").write_text(
        "raise SystemExit('shadowed')\n", encoding="utf-8"
    )

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


def test_registrar_under_gateway_leaves_credential_copies_untouched(
    gateway_sandbox, tmp_path, monkeypatch
):
    from booley.runtime import incontainer_setup

    monkeypatch.setenv("BOOLEY_PRIMARY_AUTH", "gateway")
    container = tmp_path / "container-home"
    (container / ".codex").mkdir(parents=True)
    managed = container / ".codex" / "auth.json"
    managed.write_text('{"OPENAI_API_KEY": "SYNTHETIC_MANAGED_COPY"}', encoding="utf-8")

    assert incontainer_setup.apply_stored_credential("codex", container) == "none"
    assert managed.read_text(encoding="utf-8") == '{"OPENAI_API_KEY": "SYNTHETIC_MANAGED_COPY"}'


# --- #2267: BOOLEY_GATEWAY_TOKEN_COMMAND ------------------------------------


def _use_token_command(monkeypatch, tmp_path, tokens):
    """Switch the sandbox from a static token to a fake token command.

    Returns ``(command, command_counter, codex_launch_counter)`` and gives the
    test a fresh process-wide token cache.
    """
    monkeypatch.delenv("BOOLEY_GATEWAY_TOKEN")
    command, command_counter = write_token_command(tmp_path, tokens)
    launches = tmp_path / "codex-launches.txt"
    monkeypatch.setenv("BOOLEY_GATEWAY_TOKEN_COMMAND", command)
    monkeypatch.setenv("FAKE_CODEX_LAUNCHES", str(launches))
    monkeypatch.setattr(gateway_token, "_CACHE", gateway_token.GatewayTokenCache())
    return command, command_counter, launches


async def _backend_call(tmp_path):
    """One real ``CodexBackend.call()`` through the gateway; returns the fake's report."""
    backend = cb.CodexBackend(auth_mode="gateway")
    result = await backend.call(_params(tmp_path, transcript_path=tmp_path / "transcript.jsonl"))
    return json.loads(result.output)


@pytest.mark.asyncio
async def test_gateway_runs_within_ttl_share_one_token_command_run(
    gateway_sandbox, tmp_path, monkeypatch
):
    _, command_runs, launches = _use_token_command(monkeypatch, tmp_path, [_FRESH_TOKEN, "OTHER"])

    first = await _backend_call(tmp_path)
    second = await _backend_call(tmp_path)

    assert first["token_run"]["stdout"] == second["token_run"]["stdout"] == _FRESH_TOKEN
    assert count_runs(command_runs) == 1
    assert count_runs(launches) == 2


@pytest.mark.asyncio
async def test_gateway_auth_failure_reruns_token_command_once_and_retries_run(
    gateway_sandbox, tmp_path, monkeypatch
):
    _, command_runs, launches = _use_token_command(
        monkeypatch, tmp_path, [_STALE_TOKEN, _FRESH_TOKEN]
    )

    report = await _backend_call(tmp_path)

    assert report["token_run"]["stdout"] == _FRESH_TOKEN
    assert count_runs(command_runs) == 2
    assert count_runs(launches) == 2
    assert (tmp_path / "transcript.jsonl").exists()
    assert (tmp_path / "transcript-retry2.jsonl").exists()


@pytest.mark.asyncio
async def test_gateway_repeated_auth_failure_stops_after_one_refresh(
    gateway_sandbox, tmp_path, monkeypatch
):
    _, command_runs, launches = _use_token_command(monkeypatch, tmp_path, [_STALE_TOKEN])

    with pytest.raises(agent_errors.AgentAuthError):
        await _backend_call(tmp_path)

    assert count_runs(command_runs) == 2
    assert count_runs(launches) == 2


def test_auth_failure_text_is_classified_only_under_a_gateway_token_command(monkeypatch):
    detail = "unexpected status 401 Unauthorized: invalid token"
    monkeypatch.setenv("BOOLEY_GATEWAY_URL", "https://gateway.example/v1")
    monkeypatch.delenv("BOOLEY_GATEWAY_TOKEN_COMMAND", raising=False)
    monkeypatch.setenv("BOOLEY_GATEWAY_TOKEN", "SYNTHETIC_STATIC_TOKEN")

    with pytest.raises(AgentProviderError) as static:
        cb._raise_for_codex_detail(detail, prefix="Codex error")

    monkeypatch.delenv("BOOLEY_GATEWAY_TOKEN")
    monkeypatch.setenv("BOOLEY_GATEWAY_TOKEN_COMMAND", "synthetic-token-command")
    with pytest.raises(agent_errors.AgentAuthError):
        cb._raise_for_codex_detail(detail, prefix="Codex error")

    assert not isinstance(static.value, agent_errors.AgentAuthError)


@pytest.mark.asyncio
async def test_token_command_token_reaches_codex_but_no_file_or_shell_env(
    gateway_sandbox, tmp_path, monkeypatch
):
    command, _, _ = _use_token_command(monkeypatch, tmp_path, [_FRESH_TOKEN])

    report = await _run_fake_codex(tmp_path, gateway_sandbox)

    assert report["token_run"]["stdout"] == _FRESH_TOKEN
    assert report["shell_exclude"] == ["BOOLEY_GATEWAY_TOKEN"]
    assert report["token_command_var_present"] is False
    assert report["credentials_present"] == []
    written = [
        path.read_text(encoding="utf-8", errors="replace")
        for path in Path(report["home"]).parent.rglob("*")
        if path.is_file()
    ]
    assert written, "the private Codex home must contain the generated config"
    assert not any(_FRESH_TOKEN in text or command in text for text in written)
