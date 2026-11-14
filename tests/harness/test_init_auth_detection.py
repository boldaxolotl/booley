"""`booley init` records the auth policy each Agent CLI's credentials imply (issue 4077)."""

from __future__ import annotations

import argparse
import json
import tomllib
from pathlib import Path
from typing import Any

import pytest

from booley.harness import init_cmd
from booley.harness.setup.common import InitContext
from booley.runtime import auth_token

_CLAUDE_LOGIN = {
    "claudeAiOauth": {"accessToken": "a", "refreshToken": "r", "expiresAt": 4102444800000}
}
_CODEX_LOGIN = {"tokens": {"access_token": "a", "refresh_token": "r"}}


@pytest.fixture
def home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """An empty host: no credential in the environment or on disk."""
    home = tmp_path / "home"
    home.mkdir()
    for name in ("HOME", "USERPROFILE"):
        monkeypatch.setenv(name, str(home))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(home / ".config"))
    for name in (
        *auth_token.CREDENTIAL_ENV_KEYS,
        "CODEX_HOME",
        "BOOLEY_DIR",
        "BOOLEY_PRIMARY_PROVIDER",
        "BOOLEY_PRIMARY_AUTH",
        "BOOLEY_AGENT_APP",
    ):
        monkeypatch.delenv(name, raising=False)
    init_cmd.reset_cache()
    return home


def _login(home: Path, cli: str) -> None:
    """Leave *cli*'s refreshing login file on the host."""
    path, content = {
        "claude": (home / ".claude" / ".credentials.json", _CLAUDE_LOGIN),
        "codex": (home / ".codex" / "auth.json", _CODEX_LOGIN),
    }[cli]
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(json.dumps(content).encode())


def _api_key(monkeypatch: pytest.MonkeyPatch, cli: str) -> None:
    monkeypatch.setenv(auth_token.API_KEY_ENV[cli], "sk-test-key")


def _args(**overrides: Any) -> argparse.Namespace:
    values: dict[str, Any] = {"seed": False, "provider": None, "auth": None}
    values.update(overrides)
    return argparse.Namespace(**values)


def _config(tmp_path: Path, body: bytes | None = None) -> Path:
    booley_dir = tmp_path / ".booley"
    booley_dir.mkdir(parents=True, exist_ok=True)
    config = booley_dir / "booley.toml"
    if body is not None:
        config.write_bytes(body)
    return config


def _run_init(
    tmp_path: Path, config: Path, *, interactive: bool = False, **flags: Any
) -> tuple[init_cmd.AgentSelection, dict[str, Any]]:
    """Resolve and persist the agent selection; return it and the parsed ``[agent]``."""
    ctx = InitContext(project_root=tmp_path, interactive=interactive)
    selection = init_cmd._resolve_agent_selection(ctx, _args(**flags), config)
    assert selection is not None
    assert init_cmd._step_agent_config(ctx, selection, config)
    return selection, tomllib.loads(config.read_text(encoding="utf-8"))["agent"]


def _credential(home: Path, monkeypatch: pytest.MonkeyPatch, cli: str, state: str) -> str | None:
    """Arrange *state* for *cli*; return the policy a fresh init must record."""
    if state in {"login", "both"}:
        _login(home, cli)
    if state in {"key", "both"}:
        _api_key(monkeypatch, cli)
    if state == "oauth-token":
        auth_token.store_token("sk-ant-oat-test", cli)
    if state == "booley-auth-token":
        auth_token.store_token("sk-test-key", cli)
    return {
        "login": "subscription",
        "key": "api_key",
        "both": "subscription",
        "neither": "subscription",
        "oauth-token": "subscription",
        "booley-auth-token": "api_key",
    }[state]


@pytest.mark.parametrize(
    ("cli", "state"),
    [
        pytest.param("claude", "login", id="claude-login"),
        pytest.param("claude", "key", id="claude-key"),
        pytest.param("claude", "both", id="claude-both"),
        pytest.param("claude", "neither", id="claude-neither"),
        pytest.param("claude", "oauth-token", id="claude-oauth-token"),
        pytest.param("codex", "login", id="codex-login"),
        pytest.param("codex", "key", id="codex-key"),
        pytest.param("codex", "both", id="codex-both"),
        pytest.param("codex", "neither", id="codex-neither"),
        pytest.param("codex", "booley-auth-token", id="codex-booley-auth-token"),
    ],
)
def test_fresh_init_records_detected_auth(
    tmp_path: Path,
    home: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    cli: str,
    state: str,
) -> None:
    expected = _credential(home, monkeypatch, cli, state)
    config = _config(tmp_path)

    selection, agent = _run_init(tmp_path, config, provider=cli)

    assert agent == {"default": cli, "auth": expected}
    assert selection.auth == expected
    output = capsys.readouterr().out
    if state == "both":
        assert f"{cli}: found a subscription login and an API key" in output
        assert f'{cli} = "api_key" under [agent.auth]' in output
        assert "--auth" not in output
    if state == "neither":
        assert f"{cli}: no credential found" in output
        assert auth_token.missing_credential_fix(cli, "subscription") in output


def test_both_credentials_prompt_and_enter_picks_subscription(
    tmp_path: Path, home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _login(home, "claude")
    _api_key(monkeypatch, "claude")
    prompts: list[str] = []
    answers = iter(("", "api-key"))

    def fake_input(prompt: str) -> str:
        prompts.append(prompt)
        return next(answers)

    monkeypatch.setattr("builtins.input", fake_input)

    for index, expected in enumerate(("subscription", "api_key")):
        config = _config(tmp_path / str(index))
        _, agent = _run_init(tmp_path, config, interactive=True, provider="claude")
        assert agent["auth"] == expected

    assert len(prompts) == 2
    assert all("auth for claude" in prompt for prompt in prompts)


def test_single_credential_never_prompts(
    tmp_path: Path, home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _api_key(monkeypatch, "claude")
    monkeypatch.setattr(
        "builtins.input",
        lambda _prompt: (_ for _ in ()).throw(AssertionError("must not prompt")),
    )

    _, agent = _run_init(tmp_path, _config(tmp_path), interactive=True, provider="claude")

    assert agent["auth"] == "api_key"


def test_aborted_both_credentials_prompt_records_nothing(
    tmp_path: Path, home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _login(home, "claude")
    _api_key(monkeypatch, "claude")

    def abort(_prompt: str) -> str:
        raise EOFError

    monkeypatch.setattr("builtins.input", abort)
    ctx = InitContext(project_root=tmp_path, interactive=True)

    selection = init_cmd._resolve_agent_selection(ctx, _args(provider="claude"), _config(tmp_path))

    assert selection is None


def test_mixed_detection_writes_agent_auth_table(
    tmp_path: Path, home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _login(home, "claude")
    _api_key(monkeypatch, "codex")
    config = _config(tmp_path, b'[agent]\nclis = ["claude", "codex"]\ndefault = "claude"\n')

    selection, agent = _run_init(tmp_path, config)

    assert agent["auth"] == {"claude": "subscription", "codex": "api_key"}
    assert selection.auth == "subscription"
    assert "[agent.auth]" in config.read_text(encoding="utf-8")


def test_matching_detection_for_every_cli_writes_one_string(tmp_path: Path, home: Path) -> None:
    config = _config(tmp_path, b'[agent]\nclis = ["claude", "codex"]\ndefault = "claude"\n')

    _, agent = _run_init(tmp_path, config)

    assert agent["auth"] == "subscription"


_RERUN_BASE = b'[agent]\nclis = ["claude", "codex"]\ndefault = "claude"\n'


@pytest.mark.parametrize(
    ("body", "logins", "keys", "expected"),
    [
        pytest.param(
            b'[agent]\ndefault = "claude"\nauth = "auto"\n',
            (),
            ("claude",),
            "api_key",
            id="string",
        ),
        pytest.param(
            _RERUN_BASE + b'\n[agent.auth]\nclaude = "api_key"\ncodex = "auto"\n',
            ("codex",),
            (),
            {"claude": "api_key", "codex": "subscription"},
            id="table-entry",
        ),
        pytest.param(
            _RERUN_BASE + b'\n[agent.auth]\nclaude = "auto"\ncodex = "auto"\n',
            ("claude", "codex"),
            (),
            "subscription",
            id="all-auto-table",
        ),
        pytest.param(
            _RERUN_BASE + b'auth = "auto"\n',
            ("claude",),
            ("codex",),
            {"claude": "subscription", "codex": "api_key"},
            id="auto-string-to-table",
        ),
    ],
)
def test_rerun_replaces_recorded_auto(
    tmp_path: Path,
    home: Path,
    monkeypatch: pytest.MonkeyPatch,
    body: bytes,
    logins: tuple[str, ...],
    keys: tuple[str, ...],
    expected: str | dict[str, str],
) -> None:
    for cli in logins:
        _login(home, cli)
    for cli in keys:
        _api_key(monkeypatch, cli)
    config = _config(tmp_path, body)

    _, agent = _run_init(tmp_path, config)

    assert agent["auth"] == expected
    text = config.read_text(encoding="utf-8")
    assert "auto" not in text


def test_rerun_keeps_table_comments_and_other_lines(tmp_path: Path, home: Path) -> None:
    _login(home, "codex")
    body = (
        b'[agent]\nclis = ["claude", "codex"]\ndefault = "claude"\n\n'
        b'[agent.auth]\n# billing\nclaude = "api_key"  # keep\ncodex = "auto"\n\n[flows]\nx = 1\n'
    )
    config = _config(tmp_path, body)

    _run_init(tmp_path, config)

    assert config.read_bytes() == body.replace(b'codex = "auto"', b'codex = "subscription"')


def test_rerun_replaces_auto_string_in_place_keeping_its_comment(
    tmp_path: Path, home: Path
) -> None:
    _login(home, "claude")
    body = b'[agent]\nauth = "auto"  # billing\ndefault = "claude"\n'
    config = _config(tmp_path, body)

    _run_init(tmp_path, config)

    assert (
        config.read_bytes() == b'[agent]\nauth = "subscription"  # billing\ndefault = "claude"\n'
    )


def test_rerun_keeps_explicit_auth(
    tmp_path: Path, home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An explicit entry survives although its CLI now has a key; the ``auto`` entry is filled."""
    _api_key(monkeypatch, "claude")
    _api_key(monkeypatch, "codex")
    config = _config(
        tmp_path, _RERUN_BASE + b'\n[agent.auth]\nclaude = "subscription"\ncodex = "auto"\n'
    )

    _, agent = _run_init(tmp_path, config)

    assert agent["auth"] == {"claude": "subscription", "codex": "api_key"}


def test_rerun_leaves_a_fully_explicit_record_untouched(
    tmp_path: Path, home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _api_key(monkeypatch, "claude")
    body = b'[agent]\ndefault = "claude"\nauth = "subscription"\n'
    config = _config(tmp_path, body)
    monkeypatch.setattr(
        "builtins.input",
        lambda _prompt: (_ for _ in ()).throw(AssertionError("must not prompt")),
    )

    selection, _ = _run_init(tmp_path, config, interactive=True)

    assert selection.auth == "subscription"
    assert config.read_bytes() == body


def test_auth_flag_sets_the_value_without_detection(
    tmp_path: Path, home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _login(home, "claude")

    _, agent = _run_init(tmp_path, _config(tmp_path), provider="claude", auth="api_key")

    assert agent["auth"] == "api_key"


def test_auth_auto_flag_still_records_auto(tmp_path: Path, home: Path) -> None:
    config = _config(tmp_path, b'[agent]\ndefault = "claude"\nauth = "auto"\n')

    _, agent = _run_init(tmp_path, config, auth="auto")

    assert agent["auth"] == "auto"
    assert config.read_bytes() == b'[agent]\ndefault = "claude"\nauth = "auto"\n'


def test_auth_flag_conflicting_with_a_recorded_value_is_refused(
    tmp_path: Path, home: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    config = _config(
        tmp_path,
        _RERUN_BASE + b'\n[agent.auth]\nclaude = "api_key"\ncodex = "auto"\n',
    )
    ctx = InitContext(project_root=tmp_path, interactive=False)

    assert init_cmd._resolve_agent_selection(ctx, _args(auth="subscription"), config) is None
    assert "conflicts with booley.toml" in capsys.readouterr().out


def test_dict_value_without_agent_table_appends_agent_auth_table() -> None:
    updated = init_cmd._insert_agent_fields(
        "", {"default": "claude", "auth": {"claude": "subscription", "codex": "api_key"}}
    )

    assert tomllib.loads(updated) == {
        "agent": {
            "default": "claude",
            "auth": {"claude": "subscription", "codex": "api_key"},
        }
    }


def test_preserved_tracked_config_reports_the_pending_table(
    tmp_path: Path, home: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    _login(home, "claude")
    _api_key(monkeypatch, "codex")
    body = _RERUN_BASE
    config = _config(tmp_path, body)
    ctx = InitContext(project_root=tmp_path, interactive=False, preserve_tracked_content=True)

    selection = init_cmd._resolve_agent_selection(ctx, _args(), config)
    assert selection is not None
    assert init_cmd._step_agent_config(ctx, selection, config)

    assert "claude=subscription,codex=api_key" in capsys.readouterr().out
    assert config.read_bytes() == body
