"""In-Sandbox registrar: interactive Codex uses the gateway under ``gateway`` auth (#2270)."""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tomllib
from pathlib import Path

import pytest
from tests.gateway_command_support import count_runs, write_token_command

from booley.runtime import incontainer_setup as reg

_SRC = Path(__file__).resolve().parents[2] / "src"
_URL = "https://gateway.example/v1"
_MODEL = "synthetic-gateway-model"
_PROVIDER = "booley_gateway"
_TOKEN_VARIABLE = "BOOLEY_GATEWAY_TOKEN"
_TOKEN = "SYNTHETIC_GATEWAY_TOKEN_VALUE"
_NATIVE_KEY = "sk-synthetic-native-key"


@pytest.fixture
def gateway_env(tmp_path, monkeypatch):
    """A gateway Sandbox environment with a fake token command; returns its command."""
    for name in (
        "OPENAI_API_KEY",
        "ANTHROPIC_API_KEY",
        "CLAUDE_CODE_OAUTH_TOKEN",
        "BOOLEY_GATEWAY_TOKEN",
    ):
        monkeypatch.delenv(name, raising=False)
    command_dir = tmp_path / "command"
    command_dir.mkdir()
    command, counter = write_token_command(command_dir, ["old", "new"])
    monkeypatch.setenv("BOOLEY_PRIMARY_AUTH", "gateway")
    monkeypatch.setenv("BOOLEY_GATEWAY_URL", _URL)
    monkeypatch.setenv("BOOLEY_GATEWAY_TOKEN_COMMAND", command)
    monkeypatch.setenv("BOOLEY_GATEWAY_MODEL", _MODEL)
    return command, counter


def _home(tmp_path) -> Path:
    home = tmp_path / "home"
    home.mkdir()
    return home


def _read(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def _parsed(home: Path) -> dict:
    return tomllib.loads(_read(reg.codex_config_path(home)))


def _seed(home: Path, text: str) -> Path:
    path = reg.codex_config_path(home)
    path.parent.mkdir(parents=True)
    path.write_text(text, encoding="utf-8")
    return path


def test_gateway_registration_adds_selected_provider_to_unseeded_home(tmp_path, gateway_env):
    home = _home(tmp_path)

    status = reg.register("codex", home=home)

    path = reg.codex_config_path(home)
    config = _parsed(home)
    provider = config["model_providers"][_PROVIDER]
    assert config["model_provider"] == _PROVIDER
    assert provider["base_url"] == _URL
    assert provider["wire_api"] == "responses"
    assert provider["auth"]["command"] == sys.executable
    assert provider["auth"]["args"] == ["-P", "-m", "booley.runtime.gateway_token"]
    assert "requires_openai_auth" not in provider
    assert "env_key" not in provider
    assert config["model"] == _MODEL
    assert config["features"]["fast_mode"] is False
    assert _TOKEN_VARIABLE in config["shell_environment_policy"]["exclude"]
    assert "booley" in config["mcp_servers"]
    body = _read(path)
    command, _counter = gateway_env
    assert command not in body
    assert "gateway:written" in status


def test_gateway_registration_keeps_user_keys_in_seeded_home(tmp_path, gateway_env, capsys):
    home = _home(tmp_path)
    seeded = (
        "# my own settings\n"
        'model_provider = "mine"\n'
        'model = "user-model"\n'
        "features.fast_mode = true\n"
        'shell_environment_policy.exclude = ["SECRET_*"]\n'
        "\n"
        "[model_providers.mine]\n"
        'base_url = "https://mine.example/v1"\n'
    )
    _seed(home, seeded)

    reg.register("codex", home=home)

    body = _read(reg.codex_config_path(home))
    config = _parsed(home)
    assert "# my own settings\n" in body
    assert config["model_provider"] == "mine"
    assert config["model"] == "user-model"
    assert config["features"]["fast_mode"] is True
    assert config["model_providers"]["mine"] == {"base_url": "https://mine.example/v1"}
    assert config["model_providers"][_PROVIDER]["base_url"] == _URL
    assert config["shell_environment_policy"]["exclude"] == ["SECRET_*", _TOKEN_VARIABLE]
    assert "BOOLEY_GATEWAY_TOKEN_COMMAND" not in body
    warnings = capsys.readouterr().err
    assert "not selected" in warnings
    assert "fast_mode" in warnings


def test_gateway_registration_repeats_as_a_no_op(tmp_path, gateway_env):
    home = _home(tmp_path)
    reg.register("codex", home=home)
    files = [path for path in sorted(home.rglob("*")) if path.is_file()]
    before = {path: (path.read_bytes(), path.stat().st_mtime_ns) for path in files}

    status = reg.register("codex", home=home)

    after = {
        path: (path.read_bytes(), path.stat().st_mtime_ns)
        for path in sorted(home.rglob("*"))
        if path.is_file()
    }
    assert after == before
    assert "gateway:current" in status
    assert status.startswith("codex:current")


def test_gateway_registration_writes_no_auth_json(tmp_path, gateway_env, monkeypatch):
    home = _home(tmp_path)
    monkeypatch.setenv("OPENAI_API_KEY", _NATIVE_KEY)

    reg.register("codex", home=home)

    assert _parsed(home)["model_provider"] == _PROVIDER
    assert not (home / ".codex" / "auth.json").exists()
    assert _NATIVE_KEY not in _read(reg.codex_config_path(home))


def test_gateway_registration_replaces_a_stale_owned_provider(tmp_path, gateway_env):
    home = _home(tmp_path)
    _seed(
        home,
        "[model_providers.booley_gateway]\n"
        'base_url = "https://old.example/v1"\n'
        'stale_key = "x"\n'
        "\n"
        "[model_providers.mine]\n"
        'base_url = "https://mine.example/v1"\n',
    )

    reg.register("codex", home=home)

    providers = _parsed(home)["model_providers"]
    assert providers[_PROVIDER]["base_url"] == _URL
    assert "stale_key" not in providers[_PROVIDER]
    assert providers["mine"] == {"base_url": "https://mine.example/v1"}


def test_gateway_registration_with_incomplete_env_skips_provider(
    tmp_path, gateway_env, monkeypatch, capsys
):
    home = _home(tmp_path)
    monkeypatch.delenv("BOOLEY_GATEWAY_URL")

    status = reg.register("codex", home=home)

    config = _parsed(home)
    assert "gateway:invalid" in status
    assert "BOOLEY_GATEWAY_URL" in capsys.readouterr().err
    assert "model_provider" not in config
    assert "booley" in config["mcp_servers"]


def test_gateway_registration_skips_an_unextendable_layout(tmp_path, gateway_env, capsys):
    home = _home(tmp_path)
    path = _seed(home, 'model_providers = { mine = { base_url = "https://m.example" } }\n')

    status = reg.register("codex", home=home)

    assert "gateway:skipped" in status
    assert "cannot take it" in capsys.readouterr().err
    assert "booley_gateway" not in _read(path)
    assert "model_provider =" not in _read(path)


@pytest.mark.skipif(sys.platform == "win32", reason="gateway mode is container-only")
def test_interactive_auth_command_prints_a_freshly_claimed_token(
    tmp_path, gateway_env, monkeypatch
):
    home = _home(tmp_path)
    _command, counter = gateway_env
    reg.register("codex", home=home)
    auth = _parsed(home)["model_providers"][_PROVIDER]["auth"]
    monkeypatch.setenv("PYTHONPATH", str(_SRC))

    def run_helper() -> str:
        result = subprocess.run(
            [auth["command"], *auth["args"]],
            cwd=auth["cwd"],
            env=dict(os.environ),
            capture_output=True,
            text=True,
            check=True,
            timeout=60,
        )
        return result.stdout

    assert [run_helper(), run_helper()] == ["old", "new"]
    assert count_runs(counter) == 2


def test_other_policies_leave_providers_untouched(tmp_path, gateway_env, monkeypatch):
    seeded = (
        'model_provider = "mine"\n\n[model_providers.mine]\nbase_url = "https://m.example/v1"\n'
    )
    for policy in ("auto", "subscription", "api_key"):
        home = tmp_path / f"home-{policy}"
        home.mkdir()
        path = _seed(home, seeded)
        monkeypatch.setenv("BOOLEY_PRIMARY_AUTH", policy)
        monkeypatch.setenv("OPENAI_API_KEY", _NATIVE_KEY)

        reg.register("codex", home=home)

        config = tomllib.loads(_read(path))
        assert config["model_provider"] == "mine"
        assert set(config["model_providers"]) == {"mine"}
        assert json.dumps(config["model_providers"]["mine"]) == json.dumps(
            {"base_url": "https://m.example/v1"}
        )
