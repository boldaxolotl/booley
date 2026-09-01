"""Human and machine-readable host EDA CLI contracts."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

import pytest

from booley.eda import cli
from booley.eda.provisioning import authority


def _parse(*argv: str) -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    commands = parser.add_subparsers(dest="command", required=True)
    cli.add_subparser(commands)
    return parser.parse_args(("eda", *argv))


def test_grant_add_prints_informative_success(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    project = tmp_path / "project"
    project.mkdir()
    grant = authority.ProjectGrant(str(project), "vivado", "vivado_2025_2", None)
    monkeypatch.setattr(authority, "add_grant", lambda *_args, **_kwargs: grant)

    args = _parse(
        "grant",
        "add",
        "--kind",
        "vivado",
        "--installation",
        "vivado_2025_2",
        str(project),
    )
    assert cli.run(args, project) == 0

    output = capsys.readouterr().out
    assert "Granted vivado EDA access" in output
    assert str(project) in output
    assert "vivado_2025_2" in output
    assert not output.lstrip().startswith("{")


def test_grant_add_json_preserves_record_shape(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    grant = authority.ProjectGrant(str(tmp_path), "vivado", "vivado_2025_2", None)
    monkeypatch.setattr(authority, "add_grant", lambda *_args, **_kwargs: grant)

    args = _parse("grant", "add", str(tmp_path), "--kind", "vivado", "--json")
    assert cli.run(args, tmp_path) == 0

    assert json.loads(capsys.readouterr().out) == {
        "installation": "vivado_2025_2",
        "kind": "vivado",
        "license_profile": None,
        "project_root": str(tmp_path),
    }


@pytest.mark.parametrize(
    "group,action,value,expected",
    [
        (
            "installation",
            "register",
            {"name": "vivado", "kind": "vivado", "source": "/opt/Vivado"},
            "Registered vivado EDA installation 'vivado' from /opt/Vivado.",
        ),
        ("installation", "remove", {"removed": "vivado"}, "Removed EDA installation 'vivado'."),
        (
            "installation",
            "show",
            {"name": "vivado", "kind": "vivado"},
            "EDA installation:\n  vivado: kind=vivado",
        ),
        ("installation", "list", [], "EDA installations:\n  none"),
        (
            "license",
            "register",
            {"name": "site", "server_ipv4": "10.0.0.1", "lmgrd_port": 2100},
            "Registered License Profile 'site' for 10.0.0.1:2100.",
        ),
        ("license", "remove", {"removed": "site"}, "Removed License Profile 'site'."),
        (
            "license",
            "show",
            {"name": "site", "server_ipv4": "10.0.0.1"},
            "License Profile:\n  site: server_ipv4=10.0.0.1",
        ),
        ("license", "list", [], "License Profiles:\n  none"),
    ],
)
def test_human_renderers_cover_each_authority_operation(
    group: str,
    action: str,
    value: cli._Result,
    expected: str,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    args = argparse.Namespace(eda_group=group, eda_action=action, json=False)
    monkeypatch.setattr(cli, "_dispatch", lambda _args: value)

    assert cli.run(args, Path("/project")) == 0

    assert expected in capsys.readouterr().out


def test_grant_human_output_describes_both_authority_sources(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    grant = authority.ProjectGrant(str(tmp_path), "vivado", "vivado_2025_2", "site")
    monkeypatch.setattr(authority, "add_grant", lambda *_args, **_kwargs: grant)

    assert (
        cli.run(
            _parse(
                "grant",
                "add",
                str(tmp_path),
                "--kind",
                "vivado",
                "--installation",
                "vivado_2025_2",
                "--license-profile",
                "site",
            ),
            tmp_path,
        )
        == 0
    )

    output = capsys.readouterr().out
    assert "installation 'vivado_2025_2' and License Profile 'site'" in output


def test_revoke_does_not_repeat_authority_cleanup(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    grant = authority.ProjectGrant(str(tmp_path), "vivado")
    calls: list[tuple[Path, str]] = []
    monkeypatch.setattr(
        authority,
        "revoke_grant",
        lambda project, kind: (calls.append((project, kind)), grant)[1],
    )

    args = _parse("grant", "revoke", str(tmp_path), "--kind", "vivado")
    assert cli.run(args, tmp_path) == 0

    assert calls == [(tmp_path, "vivado")]
    assert "Revoked vivado EDA access" in capsys.readouterr().out


def test_legacy_grant_list_is_hidden_and_warns(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    with pytest.raises(SystemExit) as help_exit:
        _parse("grant", "--help")
    assert help_exit.value.code == 0
    assert "list" not in capsys.readouterr().out

    grant = authority.ProjectGrant("/project", "vivado")
    monkeypatch.setattr(
        authority,
        "load_state",
        lambda: authority.AuthorityState({}, {}, (grant,)),
    )

    assert cli.run(_parse("grant", "list"), Path("/project")) == 0

    streams = capsys.readouterr()
    assert json.loads(streams.out) == [
        {
            "installation": None,
            "kind": "vivado",
            "license_profile": None,
            "project_root": "/project",
        }
    ]
    assert "deprecated" in streams.err.lower()
    assert "booley projects" in streams.err
