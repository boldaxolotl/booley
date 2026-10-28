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

from booley.config import agent as agent_config
from booley.core.models import AgentCallParams
from booley.runtime import _codex_backend as cb

_URL = "https://gateway.example/v1"
_TOKEN = "SYNTHETIC_GATEWAY_TOKEN"
_NATIVE_CREDENTIALS = {
    "OPENAI_API_KEY": "SYNTHETIC_OPENAI_KEY",
    "ANTHROPIC_API_KEY": "SYNTHETIC_ANTHROPIC_KEY",
    "ANTHROPIC_AUTH_TOKEN": "SYNTHETIC_ANTHROPIC_BEARER",
    "CLAUDE_CODE_OAUTH_TOKEN": "SYNTHETIC_CLAUDE_OAUTH",
}
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
report = {
    "model_provider": provider_id,
    "base_url": provider.get("base_url"),
    "fast_mode": config.get("features", {}).get("fast_mode"),
    "auth": auth,
    "token_run": token_run,
    "auth_json_exists": os.path.exists(os.path.join(home, "auth.json")),
    "credentials_present": sorted(
        name
        for name in ("OPENAI_API_KEY", "ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN",
                     "CLAUDE_CODE_OAUTH_TOKEN")
        if os.environ.get(name)
    ),
    "home": home,
}
message = {"type": "item.completed", "item": {"type": "agent_message", "text": json.dumps(report)}}
print(json.dumps(message))
"""


@pytest.fixture
def gateway_sandbox(tmp_path, monkeypatch):
    """A container-like process with every native credential set and a fake ``codex``."""
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
        json.dumps({"tokens": {"access_token": "SYNTHETIC_NATIVE_LOGIN"}, "OPENAI_API_KEY": None})
    )
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    fake = fake_bin / "codex"
    fake.write_text(f"#!{sys.executable}\n{_FAKE_CODEX}")
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
        path.read_text(errors="replace")
        for path in Path(report["home"]).parent.rglob("*")
        if path.is_file()
    ]
    assert written, "the private Codex home must contain the generated config"
    assert not any(_TOKEN in text for text in written)
    assert "BOOLEY_GATEWAY_URL" in "".join(written)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "missing,fragment",
    [
        ("BOOLEY_GATEWAY_URL", "BOOLEY_GATEWAY_URL"),
        ("BOOLEY_GATEWAY_TOKEN", "BOOLEY_GATEWAY_TOKEN"),
    ],
)
async def test_gateway_spawn_with_incomplete_config_fails_naming_the_missing_variable(
    gateway_sandbox, tmp_path, monkeypatch, missing, fragment
):
    monkeypatch.delenv(missing)

    with pytest.raises(ValueError, match=fragment):
        await cb._codex_spawn([str(gateway_sandbox)], _params(tmp_path), "gateway")


def test_gateway_token_entry_point_prints_token_and_fails_loud_when_missing(tmp_path):
    env = {**os.environ, "PYTHONPATH": str(_SRC), "BOOLEY_GATEWAY_URL": _URL}
    command = [sys.executable, "-P", "-m", "booley.runtime.gateway_token"]

    ok = subprocess.run(
        command,
        env={**env, "BOOLEY_GATEWAY_TOKEN": _TOKEN},
        capture_output=True,
        check=False,
        text=True,
    )
    missing = subprocess.run(
        command,
        env={k: v for k, v in env.items() if k != "BOOLEY_GATEWAY_TOKEN"},
        capture_output=True,
        check=False,
        text=True,
    )

    assert (ok.returncode, ok.stdout) == (0, _TOKEN)
    assert missing.returncode == 1
    assert "BOOLEY_GATEWAY_TOKEN" in missing.stderr
    assert missing.stdout == ""


@pytest.mark.asyncio
async def test_gateway_token_command_ignores_shadowing_booley_package_in_cwd(
    gateway_sandbox, tmp_path
):
    """The token is in the environment; a Project ``booley/`` directory must not run."""
    report = await _run_fake_codex(tmp_path, gateway_sandbox)
    shadow = tmp_path / "shadow"
    (shadow / "booley").mkdir(parents=True)
    (shadow / "booley" / "__init__.py").write_text("raise SystemExit('shadowed')\n")

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
    from booley.runtime import auth_token, incontainer_setup

    monkeypatch.setenv("BOOLEY_PRIMARY_AUTH", "gateway")
    container = tmp_path / "container-home"
    (container / ".codex").mkdir(parents=True)
    managed = container / ".codex" / "auth.json"
    managed.write_text('{"OPENAI_API_KEY": "SYNTHETIC_MANAGED_COPY"}')

    assert incontainer_setup.apply_stored_credential("codex", container) == "none"
    assert incontainer_setup.apply_stored_credential("claude", container) == "none"
    assert managed.read_text() == '{"OPENAI_API_KEY": "SYNTHETIC_MANAGED_COPY"}'
    assert auth_token.allowed_credential_keys("codex", "gateway") == frozenset()
