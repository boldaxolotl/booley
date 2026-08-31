from __future__ import annotations

from pathlib import Path

import pytest

from booley.config.host_config import (
    DEFAULT_IDLE_TIMEOUT_SECONDS,
    DEFAULT_MAX_SESSIONS,
    HostConfigError,
    InteractiveHostPolicy,
    host_config_path,
    load_host_policy,
    retired_project_policy_message,
)


def test_absent_host_config_uses_defaults_without_creating_file(tmp_path: Path) -> None:
    path = tmp_path / "config.toml"
    assert load_host_policy(path) == InteractiveHostPolicy()
    assert not path.exists()


def test_host_config_path_honors_xdg(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    assert host_config_path() == tmp_path / "booley" / "config.toml"


def test_valid_policy_round_trips(tmp_path: Path) -> None:
    path = tmp_path / "config.toml"
    path.write_text(
        "[interactive]\nidle_timeout_seconds = 600\nmax_sessions = 2\n"
        'egress_allowlist = ["Example.COM", "foo.test"]\n',
        encoding="utf-8",
    )
    assert load_host_policy(path) == InteractiveHostPolicy(600, 2, ("example.com", "foo.test"))


def test_malformed_toml_fails_with_actionable_path(tmp_path: Path) -> None:
    path = tmp_path / "config.toml"
    path.write_text("[interactive\nmax_sessions = 2\n", encoding="utf-8")
    with pytest.raises(HostConfigError, match="malformed TOML") as raised:
        load_host_policy(path)
    assert raised.value.path == path


@pytest.mark.parametrize(
    "body, field",
    [
        ("[unknown]\nvalue = 1\n", "root"),
        ("[interactive]\nunknown = 1\n", "interactive"),
        ('[interactive]\nmax_sessions = "four"\n', "interactive.max_sessions"),
        ("[interactive]\nmax_sessions = true\n", "interactive.max_sessions"),
        ("[interactive]\nidle_timeout_seconds = 0\n", "interactive.idle_timeout_seconds"),
        ('[interactive]\negress_allowlist = "example.com"\n', "interactive.egress_allowlist"),
        ("[interactive]\negress_allowlist = [42]\n", "interactive.egress_allowlist[0]"),
    ],
)
def test_invalid_policy_fails_strictly(tmp_path: Path, body: str, field: str) -> None:
    path = tmp_path / "config.toml"
    path.write_text(body, encoding="utf-8")
    with pytest.raises(HostConfigError) as raised:
        load_host_policy(path)
    assert raised.value.path == path
    assert raised.value.field == field


@pytest.mark.parametrize(
    "hostname",
    [
        "https://example.com",
        "example.com/path",
        "example.com:443",
        "127.0.0.1",
        "2001:db8::1",
        "*.example.com",
        "localhost",
        "-bad.example",
        "bad_.example",
    ],
)
def test_invalid_egress_hostname_shapes_are_rejected(tmp_path: Path, hostname: str) -> None:
    path = tmp_path / "config.toml"
    path.write_text(f'[interactive]\negress_allowlist = ["{hostname}"]\n', encoding="utf-8")
    with pytest.raises(HostConfigError, match="egress_allowlist"):
        load_host_policy(path)


def test_defaults_are_canonical() -> None:
    assert InteractiveHostPolicy() == InteractiveHostPolicy(
        DEFAULT_IDLE_TIMEOUT_SECONDS,
        DEFAULT_MAX_SESSIONS,
        (),
    )


def test_retired_project_policy_names_destination_and_concrete_replacement(tmp_path: Path) -> None:
    destination = tmp_path / "config.toml"
    message = retired_project_policy_message(
        {"interactive": {"idle_timeout_seconds": 90, "egress_allowlist": ["foo.test"]}},
        destination=destination,
    )
    assert message is not None
    assert str(destination) in message
    assert "[interactive]\n" in message
    assert "idle_timeout_seconds = 90" in message
    assert 'egress_allowlist = ["foo.test"]' in message
    assert "will not migrate" in message
