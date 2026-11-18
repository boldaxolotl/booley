"""`booley init` records the auth policy each Agent CLI's credentials imply (issue 4077)."""

from __future__ import annotations

import tomllib
from pathlib import Path

import pytest

from booley.harness import init_cmd
from booley.runtime import auth_token
from tests.harness.init_agent_support import (
    api_key,
    empty_host,
    login,
    project_config,
    seed_init,
    stub_host_images,
)


@pytest.fixture(autouse=True)
def home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    home = empty_host(tmp_path, monkeypatch)
    stub_host_images(monkeypatch)
    return home


def _agent(config: Path) -> dict[str, object]:
    """The parsed ``[agent]`` table of *config*."""
    return tomllib.loads(config.read_text(encoding="utf-8"))["agent"]


def _credential(home: Path, monkeypatch: pytest.MonkeyPatch, cli: str, state: str) -> str | None:
    """Arrange *state* for *cli*; return the policy a fresh init must record."""
    if state in {"login", "both"}:
        login(home, cli)
    if state in {"key", "both"}:
        api_key(monkeypatch, cli)
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
    config = project_config(tmp_path)

    seed_init(tmp_path, provider=cli)

    assert _agent(config) == {"default": cli, "auth": expected}
    output = capsys.readouterr().out
    assert f"{cli}/{expected}" in output
    if state == "both":
        assert f"{cli}: found a subscription login and an API key" in output
        assert f'{cli} = "api_key" under [agent.auth]' in output
        assert "--auth" not in output
    if state == "neither":
        fix = {
            "claude": "run claude and use /login, or on the host: booley auth --cli claude",
            "codex": "codex login",
        }[cli]
        assert f"{cli}: no credential found; sign in: {fix}" in output


_BOTH = b'[agent]\nclis = ["claude", "codex"]\ndefault = "claude"\n'


@pytest.mark.parametrize(
    ("logins", "keys", "expected"),
    [
        pytest.param(
            ("claude",), ("codex",), {"claude": "subscription", "codex": "api_key"}, id="mixed"
        ),
        pytest.param((), (), "subscription", id="matching"),
    ],
)
def test_multi_cli_detection_record(
    tmp_path: Path,
    home: Path,
    monkeypatch: pytest.MonkeyPatch,
    logins: tuple[str, ...],
    keys: tuple[str, ...],
    expected: str | dict[str, str],
) -> None:
    for cli in logins:
        login(home, cli)
    for cli in keys:
        api_key(monkeypatch, cli)
    config = project_config(tmp_path, _BOTH)

    seed_init(tmp_path)

    assert _agent(config)["auth"] == expected
    assert ("[agent.auth]" in config.read_text(encoding="utf-8")) is isinstance(expected, dict)


_RERUN_BASE = _BOTH


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
        pytest.param(
            _RERUN_BASE + b'\n[agent.auth]\nclaude = "subscription"\ncodex = "auto"\n',
            (),
            ("claude", "codex"),
            {"claude": "subscription", "codex": "api_key"},
            id="explicit-entry-kept",
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
    """An explicit entry survives although its CLI now has a key; ``auto`` is filled."""
    for cli in logins:
        login(home, cli)
    for cli in keys:
        api_key(monkeypatch, cli)
    config = project_config(tmp_path, body)

    seed_init(tmp_path)

    assert _agent(config)["auth"] == expected
    assert "auto" not in config.read_text(encoding="utf-8")


def test_preserved_tracked_config_reports_the_pending_table(
    tmp_path: Path,
    home: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    login(home, "claude")
    api_key(monkeypatch, "codex")
    config = project_config(tmp_path, _RERUN_BASE)

    init_cmd.reinitialize_project(tmp_path, init_cmd.ProjectFacts(None))

    assert "claude=subscription,codex=api_key" in capsys.readouterr().out
    assert config.read_bytes() == _RERUN_BASE
