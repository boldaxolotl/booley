"""``booley eda installation|license list|show`` human views and JSON stability."""

from __future__ import annotations

from argparse import ArgumentParser
from pathlib import Path

import pytest

from booley.eda import cli as eda_cli
from booley.eda.provisioning import authority
from booley.harness import booley, colors

_LONG_SOURCE = "/opt/Xilinx/" + "a" * 50

_STATE = authority.AuthorityState(
    installations={
        "vivado_2025_2": authority.Installation(
            "vivado_2025_2", "vivado", _LONG_SOURCE, "2025.2", "x86_64", 3
        )
    },
    licenses={
        "site": authority.LicenseProfile("site", "vivado", "10.0.0.1", "0123456789ab", 2100, 2101)
    },
    grants=(),
)
_EMPTY = authority.AuthorityState(installations={}, licenses={}, grants=())

_INSTALLATION_TABLE = [
    "  Name            Kind     Version   Arch     Source",
    "  ─────────────   ──────   ───────   ──────   " + "─" * 48,
    "  vivado_2025_2   vivado   2025.2    x86_64   /opt/Xilinx/" + "a" * 35 + "…",
]
_LICENSE_TABLE = [
    "  Name   Kind     Server     Host ID        lmgrd port   Vendor port",
    "  ────   ──────   ────────   ────────────   ──────────   ───────────",
    "  site   vivado   10.0.0.1   0123456789ab   2100         2101",
]
_INSTALLATION_SHOW = [
    "EDA installation vivado_2025_2",
    "  kind             vivado",
    "  source           " + _LONG_SOURCE,
    "  version          2025.2",
    "  architecture     x86_64",
    "  policy revision  3",
]
_LICENSE_SHOW = [
    "License Profile site",
    "  kind           vivado",
    "  server ipv4    10.0.0.1",
    "  server hostid  0123456789ab",
    "  lmgrd port     2100",
    "  vendor port    2101",
]
_SHOW_LABELS = {
    "installation": ("kind", "source", "version", "architecture", "policy revision"),
    "license": ("kind", "server ipv4", "server hostid", "lmgrd port", "vendor port"),
}


@pytest.fixture(autouse=True)
def _plain_colors(monkeypatch):
    monkeypatch.setattr(colors, "COLORS_ENABLED", False)


def _eda(monkeypatch, capsys, state, *argv: str) -> str:
    """Run ``booley eda *argv`` through the harness against *state*."""
    monkeypatch.setattr(authority, "load_state", lambda **_kwargs: state)
    parser = ArgumentParser()
    eda_cli.add_subparser(parser.add_subparsers(dest="command"))
    args = parser.parse_args(["eda", *argv])
    capsys.readouterr()
    assert booley._cmd_eda(args, Path("/project")) == 0
    return capsys.readouterr().out


@pytest.mark.parametrize(
    ("group", "state", "expected"),
    [
        pytest.param("installation", _STATE, _INSTALLATION_TABLE, id="installation"),
        pytest.param("license", _STATE, _LICENSE_TABLE, id="license"),
        pytest.param(
            "installation",
            _EMPTY,
            ["No EDA installations registered."],
            id="installation-empty",
        ),
        pytest.param("license", _EMPTY, ["No License Profiles registered."], id="license-empty"),
    ],
)
def test_list_renders_a_table(monkeypatch, capsys, group, state, expected):
    assert _eda(monkeypatch, capsys, state, group, "list").splitlines() == expected


@pytest.mark.parametrize(
    ("group", "name", "expected"),
    [
        pytest.param("installation", "vivado_2025_2", _INSTALLATION_SHOW, id="installation"),
        pytest.param("license", "site", _LICENSE_SHOW, id="license"),
    ],
)
def test_show_aligns_dimmed_labels(monkeypatch, capsys, group, name, expected):
    plain = _eda(monkeypatch, capsys, _STATE, group, "show", name)
    assert plain.splitlines() == expected

    monkeypatch.setattr(colors, "COLORS_ENABLED", True)
    colored = _eda(monkeypatch, capsys, _STATE, group, "show", name)

    for label in _SHOW_LABELS[group]:
        assert f"\x1b[2m{label}\x1b[0m" in colored
    assert colors.strip_ansi(colored) == plain


_INSTALLATION_JSON = f"""{{
  "architecture": "x86_64",
  "kind": "vivado",
  "name": "vivado_2025_2",
  "policy_revision": 3,
  "source": "{_LONG_SOURCE}",
  "version": "2025.2"
}}
"""
_LICENSE_JSON = """{
  "kind": "vivado",
  "lmgrd_port": 2100,
  "name": "site",
  "server_hostid": "0123456789ab",
  "server_ipv4": "10.0.0.1",
  "vendor_port": 2101
}
"""
_INSTALLATION_LIST_JSON = f"""[
  {{
    "architecture": "x86_64",
    "kind": "vivado",
    "name": "vivado_2025_2",
    "policy_revision": 3,
    "source": "{_LONG_SOURCE}",
    "version": "2025.2"
  }}
]
"""
_LICENSE_LIST_JSON = """[
  {
    "kind": "vivado",
    "lmgrd_port": 2100,
    "name": "site",
    "server_hostid": "0123456789ab",
    "server_ipv4": "10.0.0.1",
    "vendor_port": 2101
  }
]
"""


@pytest.mark.parametrize(
    ("argv", "golden", "human_first_line"),
    [
        pytest.param(
            ("installation", "list"),
            _INSTALLATION_LIST_JSON,
            _INSTALLATION_TABLE[0],
            id="installation-list",
        ),
        pytest.param(
            ("installation", "show", "vivado_2025_2"),
            _INSTALLATION_JSON,
            _INSTALLATION_SHOW[0],
            id="installation-show",
        ),
        pytest.param(
            ("license", "list"),
            _LICENSE_LIST_JSON,
            _LICENSE_TABLE[0],
            id="license-list",
        ),
        pytest.param(
            ("license", "show", "site"), _LICENSE_JSON, _LICENSE_SHOW[0], id="license-show"
        ),
    ],
)
def test_json_output_is_byte_identical_while_human_view_changes(
    monkeypatch, capsys, argv, golden, human_first_line
):
    assert _eda(monkeypatch, capsys, _STATE, *argv, "--json") == golden
    assert _eda(monkeypatch, capsys, _STATE, *argv).splitlines()[0] == human_first_line
