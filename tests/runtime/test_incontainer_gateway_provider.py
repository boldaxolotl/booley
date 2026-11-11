"""In-Sandbox registrar: interactive Codex uses the gateway under ``gateway`` auth (#2270)."""

from __future__ import annotations

import os
import sys
import tomllib
from contextlib import contextmanager
from pathlib import Path

import pytest
import tomli
from tests.gateway_command_support import count_runs, write_token_command
from tests.subprocess_budget import run_bounded_child

from booley.runtime import auth_token
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


@contextmanager
def _count_parses(monkeypatch, source):
    """Count every parse of exactly *source* by tomlkit (any path) or tomli."""
    import tomlkit.parser

    seen: list[str] = []
    real_init = tomlkit.parser.Parser.__init__
    real_loads = tomli.loads

    def counted_init(self, string):
        seen.append(string)
        real_init(self, string)

    def counted_loads(text, *args, **kwargs):
        seen.append(text)
        return real_loads(text, *args, **kwargs)

    monkeypatch.setattr(tomlkit.parser.Parser, "__init__", counted_init)
    monkeypatch.setattr(tomli, "loads", counted_loads)
    yield lambda: seen.count(source)


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
    # A stored credential seed (the host sidecar) would normally become auth.json.
    seed = home / auth_token.TOKEN_SEED_BASENAME[auth_token.APP_CODEX]
    seed.write_text(_NATIVE_KEY, encoding="utf-8")

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
        result = run_bounded_child(
            [auth["command"], *auth["args"]],
            cwd=auth["cwd"],
            env=dict(os.environ),
            check=True,
        )
        return result.stdout

    assert [run_helper(), run_helper()] == ["old", "new"]
    assert count_runs(counter) == 2


_SEEDED_OTHER_PROVIDER = (
    'model_provider = "mine"\n\n[model_providers.mine]\nbase_url = "https://m.example/v1"\n'
)


@pytest.mark.parametrize("policy", ["auto", "subscription", "api_key"])
@pytest.mark.parametrize("seeded", [True, False])
def test_other_policies_leave_providers_untouched(
    tmp_path, gateway_env, monkeypatch, policy, seeded
):
    home = _home(tmp_path)
    if seeded:
        _seed(home, _SEEDED_OTHER_PROVIDER)
    monkeypatch.setenv("BOOLEY_PRIMARY_AUTH", policy)
    monkeypatch.setenv("OPENAI_API_KEY", _NATIVE_KEY)

    reg.register("codex", home=home)

    config = _parsed(home)
    assert _PROVIDER not in config.get("model_providers", {})
    assert config.get("model_provider") == ("mine" if seeded else None)
    if seeded:
        assert config["model_providers"]["mine"] == {"base_url": "https://m.example/v1"}


_USER_SEED = (
    'model_provider = "mine"\nmodel = "user-model"\n[model_providers.mine]\nbase_url = "x"\n'
    '[model_providers.booley_gateway]\nbase_url = "https://old.example/v1"\n'
)


@pytest.mark.parametrize("writer", ["add", "remove"])
def test_gateway_writers_parse_config_once(tmp_path, gateway_env, monkeypatch, writer):
    home = _home(tmp_path)
    if writer == "add":
        source = _USER_SEED
        _seed(home, source)
    else:
        reg.register("codex", home=home)
        source = _read(reg.codex_config_path(home))
    with _count_parses(monkeypatch, source) as parses:
        if writer == "add":
            assert reg._apply_codex_gateway(home) == "written"
        else:
            assert reg._drop_codex_gateway(home) == "removed"
    assert parses() == 1


@pytest.mark.parametrize(
    ("kind", "seeded", "status"),
    [
        ("invalid", None, "invalid"),
        (
            "inline-providers",
            'model_providers = { m = { base_url = "https://m.example" } }\n',
            "skipped",
        ),
        ("non-list-exclude", 'shell_environment_policy.exclude = "SECRET_*"\n', "skipped"),
    ],
)
def test_gateway_skips_leave_the_file_unchanged(
    tmp_path, gateway_env, monkeypatch, capsys, kind, seeded, status
):
    home = _home(tmp_path)
    path = reg.codex_config_path(home)
    if seeded:
        _seed(home, seeded)
    if kind == "invalid":
        monkeypatch.delenv("BOOLEY_GATEWAY_URL")

    assert reg._apply_codex_gateway(home) == status

    warning = capsys.readouterr().err
    assert ("BOOLEY_GATEWAY_URL" in warning) is (kind == "invalid")
    assert ("cannot take it" in warning) is (kind != "invalid")
    assert (_read(path) == seeded) if seeded else not path.exists()


def test_gateway_registration_keeps_no_final_newline_valid(tmp_path, gateway_env):
    home = _home(tmp_path)
    _seed(home, 'shell_environment_policy.exclude = ["A"]\nmodel_providers.mine.base_url = "x"')

    assert reg._apply_codex_gateway(home) == "written"

    config = _parsed(home)
    assert config["model_provider"] == _PROVIDER
    assert config["features"]["fast_mode"] is False
    assert config["shell_environment_policy"]["exclude"] == ["A", _TOKEN_VARIABLE]
    assert config["model_providers"]["mine"] == {"base_url": "x"}


def test_gateway_removal_drops_only_owned_entries(tmp_path, gateway_env):
    home = _home(tmp_path)
    path = _seed(home, '[model_providers.mine]\nbase_url = "https://m.example/v1"\n')
    reg.register("codex", home=home)
    assert _parsed(home)["model_provider"] == _PROVIDER

    assert reg._drop_codex_gateway(home) == "removed"

    config = _parsed(home)
    assert set(config["model_providers"]) == {"mine"}
    assert "model_provider" not in config and "model" not in config
    assert config["features"]["fast_mode"] is False
    assert _TOKEN_VARIABLE in config["shell_environment_policy"]["exclude"]
    before = (path.read_bytes(), path.stat().st_mtime_ns)
    assert reg._drop_codex_gateway(home) == "off"
    assert (path.read_bytes(), path.stat().st_mtime_ns) == before


def test_gateway_removal_keeps_users_own_selection(tmp_path, gateway_env):
    home = _home(tmp_path)
    path = _seed(home, f'model_provider = "mine"\nmodel = "{_MODEL}"\n')
    before = path.read_bytes()

    assert reg._drop_codex_gateway(home) == "off"

    assert path.read_bytes() == before


def test_gateway_off_policy_writes_nothing(tmp_path, gateway_env, monkeypatch):
    home = _home(tmp_path)
    monkeypatch.setenv("BOOLEY_PRIMARY_AUTH", "subscription")

    assert reg._apply_codex_gateway(home) == "off"

    assert not reg.codex_config_path(home).exists()
