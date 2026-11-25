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


@pytest.fixture
def path(tmp_path: Path) -> Path:
    return tmp_path / "config.toml"


def test_absent_host_config_uses_defaults_without_creating_file(path: Path) -> None:
    assert load_host_policy(path) == SandboxHostPolicy()
    assert not path.exists()


def test_host_config_path_honors_xdg(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    assert host_config_path() == tmp_path / "booley" / "config.toml"


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        (
            "idle_timeout_seconds = 600\nmax_sessions = 2\n"
            'egress_allowlist = ["Example.COM", "foo.test"]\n',
            SandboxHostPolicy(600, 2, ("example.com", "foo.test")),
        ),
        ("", SandboxHostPolicy()),
        ('egress = "proxy"\n', SandboxHostPolicy()),
        ('egress = "unproxied"\n', SandboxHostPolicy(egress="unproxied")),
    ],
    ids=["limits-and-allowlist", "egress-absent", "egress-proxy", "egress-unproxied"],
)
@pytest.mark.parametrize("table", ["sandbox", "interactive"])
def test_valid_policy_round_trips(
    path: Path, table: str, text: str, expected: SandboxHostPolicy
) -> None:
    path.write_text(f"[{table}]\n{text}", encoding="utf-8")
    assert load_host_policy(path) == expected


def test_malformed_toml_fails_with_actionable_path(path: Path) -> None:
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
def test_invalid_policy_fails_strictly(path: Path, body: str, field: str, table: str) -> None:
    body = body.replace("interactive", table)
    field = field.replace("interactive", table)
    path.write_text(body, encoding="utf-8")
    with pytest.raises(HostConfigError) as raised:
        load_host_policy(path)
    assert raised.value.path == path
    assert raised.value.field == field


@pytest.mark.parametrize(
    "body",
    ['egress = "direct"\n', 'egress = "unproxied"\negress_allowlist = ["example.com"]\n'],
    ids=["unknown-value", "unproxied-with-allowlist"],
)
def test_invalid_egress_mode_fails_strictly(path: Path, body: str) -> None:
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
def test_invalid_egress_hostname_shapes_are_rejected(path: Path, hostname: str) -> None:
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
    path: Path, legacy: str
) -> None:
    text = legacy + '[sandbox]\negress_allowlist = ["foo.test"]\n'
    path.write_text(text)
    messages: list[str] = []
    assert load_host_policy(path, on_deprecation=messages.append) == SandboxHostPolicy(
        egress_allowlist=("foo.test",)
    )
    assert len(messages) == 1
    assert "[interactive] is deprecated and ignored" in messages[0]
    assert path.read_text() == text


def test_legacy_warning_contains_round_trippable_exact_replacement(path: Path) -> None:
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


def test_invalid_sandbox_does_not_fall_back_to_legacy(path: Path) -> None:
    path.write_text("[sandbox]\nmax_sessions = false\n[interactive]\nmax_sessions = 2\n")
    with pytest.raises(HostConfigError) as raised:
        load_host_policy(path)
    assert raised.value.field == "sandbox.max_sessions"


@pytest.mark.parametrize(
    ("before", "requested", "content", "previous"),
    [
        (None, "unproxied", b'[sandbox]\negress = "unproxied"\n', "proxy"),
        (None, "proxy", None, "proxy"),
        (b"[sandbox]\nmax_sessions = 2\n", "proxy", None, "proxy"),
        (
            b"# keep\n[sandbox]\nmax_sessions = 2  # cap\n",
            "unproxied",
            b'# keep\n[sandbox]\nmax_sessions = 2  # cap\negress = "unproxied"\n',
            "proxy",
        ),
        (
            b"[sandbox]\r\nmax_sessions = 2\r\n",
            "unproxied",
            b'[sandbox]\r\nmax_sessions = 2\r\negress = "unproxied"\r\n',
            "proxy",
        ),
        (
            b'[sandbox]\negress = "unproxyed"\n',
            "proxy",
            b'[sandbox]\negress = "proxy"\n',
            "unproxyed",
        ),
    ],
    ids=["create", "missing-is-proxy", "absent-key-is-proxy", "add-lf", "add-crlf", "repair"],
)
def test_egress_edit_plans_the_new_file_content(
    path: Path, before: bytes | None, requested: str, content: bytes | None, previous: str
) -> None:
    if before is not None:
        path.write_bytes(before)

    change = host_config.plan_host_egress(requested, path)

    assert (change.content, change.previous, change.requested) == (content, previous, requested)
    assert change.path == path
    assert (path.read_bytes() if before is not None else path.exists()) == (
        before or False
    )  # no write


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


@pytest.mark.parametrize(
    ("body", "match"),
    [
        (b"[sandbox\n", "malformed TOML"),
        (b"\xff\xfe", "cannot read file"),
        (b"sandbox = 1\n", "must be a TOML table"),
        (b"[sandbox]\nmax_sessions = 0\n", "must be positive"),
        (b'[sandbox]\negress_allowlist = ["example.com"]\n', "unproxied cannot be combined"),
        (
            b"[interactive]\nmax_sessions = -1\n",
            r"legacy \[interactive\], rename it to \[sandbox\] first",
        ),
    ],
)
def test_egress_edit_refuses_unreadable_or_invalid_file(
    path: Path, body: bytes, match: str
) -> None:
    path.write_bytes(body)

    with pytest.raises(HostConfigError, match=match):
        host_config.plan_host_egress("unproxied", path)
