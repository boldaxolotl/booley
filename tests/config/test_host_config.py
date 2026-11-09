from __future__ import annotations

from pathlib import Path

import pytest

from booley.config import host_config
from booley.config.host_config import (
    HostConfigError,
    SandboxHostPolicy,
    host_config_path,
    load_host_policy,
    retired_project_policy_message,
)


def test_absent_host_config_uses_defaults_without_creating_file(tmp_path: Path) -> None:
    path = tmp_path / "config.toml"
    assert load_host_policy(path) == SandboxHostPolicy()
    assert not path.exists()


def test_host_config_path_honors_xdg(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    assert host_config_path() == tmp_path / "booley" / "config.toml"


@pytest.mark.parametrize("table", ["sandbox", "interactive"])
def test_valid_policy_round_trips(tmp_path: Path, table: str) -> None:
    path = tmp_path / "config.toml"
    path.write_text(
        f"[{table}]\nidle_timeout_seconds = 600\nmax_sessions = 2\n"
        'egress_allowlist = ["Example.COM", "foo.test"]\n',
        encoding="utf-8",
    )
    assert load_host_policy(path) == SandboxHostPolicy(600, 2, ("example.com", "foo.test"))


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
@pytest.mark.parametrize("table", ["sandbox", "interactive"])
def test_invalid_policy_fails_strictly(tmp_path: Path, body: str, field: str, table: str) -> None:
    body = body.replace("interactive", table)
    field = field.replace("interactive", table)
    path = tmp_path / "config.toml"
    path.write_text(body, encoding="utf-8")
    with pytest.raises(HostConfigError) as raised:
        load_host_policy(path)
    assert raised.value.path == path
    assert raised.value.field == field


@pytest.mark.parametrize(
    "line, expected",
    [("", "proxy"), ('egress = "proxy"\n', "proxy"), ('egress = "unproxied"\n', "unproxied")],
    ids=["absent", "proxy", "unproxied"],
)
def test_egress_mode_round_trips(tmp_path: Path, line: str, expected: str) -> None:
    path = tmp_path / "config.toml"
    path.write_text(f"[sandbox]\n{line}", encoding="utf-8")
    assert load_host_policy(path).egress == expected


@pytest.mark.parametrize(
    "body",
    ['egress = "direct"\n', 'egress = "unproxied"\negress_allowlist = ["example.com"]\n'],
    ids=["unknown-value", "unproxied-with-allowlist"],
)
def test_invalid_egress_mode_fails_strictly(tmp_path: Path, body: str) -> None:
    path = tmp_path / "config.toml"
    path.write_text(f"[sandbox]\n{body}", encoding="utf-8")
    with pytest.raises(HostConfigError) as raised:
        load_host_policy(path)
    assert raised.value.path == path
    assert raised.value.field == "sandbox.egress"


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


def test_retired_project_policy_names_destination_and_concrete_replacement(tmp_path: Path) -> None:
    destination = tmp_path / "config.toml"
    message = retired_project_policy_message(
        {"interactive": {"idle_timeout_seconds": 90, "egress_allowlist": ["foo.test"]}},
        destination=destination,
    )
    assert message is not None
    assert str(destination) in message
    assert "[sandbox]\n" in message
    assert "idle_timeout_seconds = 90" in message
    assert 'egress_allowlist = ["foo.test"]' in message
    assert "will not migrate" in message


@pytest.mark.parametrize("legacy", ["[interactive]\nmax_sessions = 1\n", "interactive = false\n"])
def test_sandbox_wins_without_merging_or_validating_ignored_legacy(
    tmp_path: Path, legacy: str
) -> None:
    path = tmp_path / "config.toml"
    text = legacy + '[sandbox]\negress_allowlist = ["foo.test"]\n'
    path.write_text(text)
    messages: list[str] = []
    assert load_host_policy(path, on_deprecation=messages.append) == SandboxHostPolicy(
        egress_allowlist=("foo.test",)
    )
    assert len(messages) == 1
    assert "[interactive] is deprecated and ignored" in messages[0]
    assert path.read_text() == text


def test_legacy_warning_contains_round_trippable_exact_replacement(tmp_path: Path) -> None:
    path = tmp_path / "config.toml"
    text = '[interactive]\nmax_sessions = 2\negress_allowlist = ["Example.COM\\n"]\n'
    path.write_text(text)
    messages: list[str] = []
    policy = load_host_policy(path, on_deprecation=messages.append)
    assert messages == [
        f"{path}: [interactive] is deprecated; replace it with:\n"
        '[sandbox]\negress_allowlist = ["Example.COM\\n"]\nmax_sessions = 2\n'
        "Booley will not rewrite the host config automatically."
    ]
    assert path.read_text() == text
    replacement = messages[0].split("replace it with:\n")[1].split("\nBooley")[0]
    path.write_text(replacement)
    messages.clear()
    assert load_host_policy(path, on_deprecation=messages.append) == policy
    assert messages == []


def test_invalid_sandbox_does_not_fall_back_to_legacy(tmp_path: Path) -> None:
    path = tmp_path / "config.toml"
    path.write_text("[sandbox]\nmax_sessions = false\n[interactive]\nmax_sessions = 2\n")
    with pytest.raises(HostConfigError) as raised:
        load_host_policy(path)
    assert raised.value.field == "sandbox.max_sessions"


def test_egress_edit_creates_missing_config_with_only_egress(tmp_path: Path) -> None:
    path = tmp_path / "config.toml"

    change = host_config.plan_host_egress("unproxied", path)

    assert change.content == b'[sandbox]\negress = "unproxied"\n'
    assert (change.path, change.previous, change.requested) == (path, "proxy", "unproxied")
    assert not path.exists()  # planning never writes


_EGRESS_BEFORE = (
    "# leading comment\n"
    "[sandbox]\n"
    "idle_timeout_seconds   =  600 # keep short\n"
    "max_sessions = 2\n"
    "egress_allowlist = []\n"
    'egress   =   "unproxied"  # mode\n'
    "\n"
    "# trailing comment\n"
)


def test_egress_edit_changes_only_the_egress_value(tmp_path: Path) -> None:
    for newline in ("\n", "\r\n"):
        path = tmp_path / f"config{len(newline)}.toml"
        original = _EGRESS_BEFORE.replace("\n", newline).encode()
        path.write_bytes(original)

        change = host_config.plan_host_egress("proxy", path)

        assert change.content == original.replace(b'"unproxied"', b'"proxy"')
        assert change.previous == "unproxied"
        assert path.read_bytes() == original


def test_egress_edit_adds_key_to_existing_sandbox_table(tmp_path: Path) -> None:
    path = tmp_path / "config.toml"
    path.write_bytes(b"# keep\n[sandbox]\nmax_sessions = 2  # cap\n")

    change = host_config.plan_host_egress("unproxied", path)

    assert change.content == b'# keep\n[sandbox]\nmax_sessions = 2  # cap\negress = "unproxied"\n'


def test_egress_edit_makes_no_edit_when_mode_already_recorded(tmp_path: Path) -> None:
    path = tmp_path / "config.toml"
    path.write_text("[sandbox]\nmax_sessions = 2\n", encoding="utf-8")

    assert host_config.plan_host_egress("proxy", path).content is None  # absent key means proxy
    assert host_config.plan_host_egress("proxy", tmp_path / "missing.toml").content is None


def test_egress_edit_refuses_invalid_merged_policy(tmp_path: Path) -> None:
    path = tmp_path / "config.toml"
    path.write_text('[sandbox]\negress_allowlist = ["example.com"]\n', encoding="utf-8")

    with pytest.raises(HostConfigError, match="unproxied cannot be combined"):
        host_config.plan_host_egress("unproxied", path)


def test_egress_edit_repairs_invalid_existing_egress(tmp_path: Path) -> None:
    path = tmp_path / "config.toml"
    path.write_text('[sandbox]\negress = "unproxyed"\n', encoding="utf-8")

    change = host_config.plan_host_egress("proxy", path)

    assert change.content == b'[sandbox]\negress = "proxy"\n'
    assert change.previous == "unproxyed"


@pytest.mark.parametrize(
    ("body", "match"),
    [
        (b"[sandbox\n", "malformed TOML"),
        (b"\xff\xfe", "cannot read file"),
        (b"sandbox = 1\n", "must be a TOML table"),
        (b"[sandbox]\nmax_sessions = 0\n", "must be positive"),
    ],
)
def test_egress_edit_refuses_unreadable_or_invalid_file(
    tmp_path: Path, body: bytes, match: str
) -> None:
    path = tmp_path / "config.toml"
    path.write_bytes(body)

    with pytest.raises(HostConfigError, match=match):
        host_config.plan_host_egress("unproxied", path)


def test_egress_edit_refuses_legacy_interactive_table(tmp_path: Path) -> None:
    path = tmp_path / "config.toml"
    path.write_text("[interactive]\nmax_sessions = -1\n", encoding="utf-8")

    with pytest.raises(HostConfigError, match="legacy \\[interactive\\]") as raised:
        host_config.plan_host_egress("unproxied", path)

    assert "rename it to [sandbox] first" in str(raised.value)


def test_egress_edit_refuses_unreadable_path(tmp_path: Path) -> None:
    with pytest.raises(HostConfigError, match="cannot read file"):
        host_config.plan_host_egress("unproxied", tmp_path)  # a directory
