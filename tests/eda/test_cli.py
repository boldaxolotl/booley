"""Human and machine-readable host EDA CLI contracts."""

from __future__ import annotations

import argparse
import json
import shutil
from pathlib import Path

import pytest

from booley.eda import cli
from booley.eda.provisioning import authority


def _parse(*argv: str) -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    commands = parser.add_subparsers(dest="command", required=True)
    cli.add_subparser(commands)
    return parser.parse_args(("eda", *argv))


class _DirectGrantMutator:
    def recovery_pending(self):
        return False

    def add(self, project, kind, *, installation, license_profile):
        return authority._add_grant(
            project,
            kind,
            installation=installation,
            license_profile=license_profile,
        )

    def revoke(self, project, kind):
        return authority._revoke_grant(project, kind)


def _run(args: argparse.Namespace, project: Path) -> int:
    return cli.run(args, project, grant_mutator=_DirectGrantMutator())


def test_grant_add_prints_informative_success(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    project = tmp_path / "project"
    project.mkdir()
    grant = authority.ProjectGrant(str(project), "vivado", "vivado_2025_2", None)
    monkeypatch.setattr(authority, "_add_grant", lambda *_args, **_kwargs: grant)

    args = _parse(
        "grant",
        "add",
        "--kind",
        "vivado",
        "--installation",
        "vivado_2025_2",
        str(project),
    )
    assert _run(args, project) == 0

    output = capsys.readouterr().out
    assert "Granted vivado EDA access" in output
    assert str(project) in output
    assert "vivado_2025_2" in output
    assert not output.lstrip().startswith("{")


def test_grant_add_json_preserves_record_shape(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    grant = authority.ProjectGrant(str(tmp_path), "vivado", "vivado_2025_2", None)
    monkeypatch.setattr(authority, "_add_grant", lambda *_args, **_kwargs: grant)

    args = _parse("grant", "add", str(tmp_path), "--kind", "vivado", "--json")
    assert _run(args, tmp_path) == 0

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
    monkeypatch.setattr(cli, "_dispatch", lambda _args, **_kwargs: value)

    assert _run(args, Path("/project")) == 0

    assert expected in capsys.readouterr().out


def test_grant_human_output_describes_both_authority_sources(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    grant = authority.ProjectGrant(str(tmp_path), "vivado", "vivado_2025_2", "site")
    monkeypatch.setattr(authority, "_add_grant", lambda *_args, **_kwargs: grant)

    assert (
        _run(
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
        "_revoke_grant",
        lambda project, kind: (calls.append((project, kind)), grant)[1],
    )

    args = _parse("grant", "revoke", str(tmp_path), "--kind", "vivado")
    assert _run(args, tmp_path) == 0

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

    assert _run(_parse("grant", "list"), Path("/project")) == 0

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


def test_grant_list_fails_closed_while_runtime_recovery_is_pending(
    capsys: pytest.CaptureFixture[str],
) -> None:
    mutator = _DirectGrantMutator()
    mutator.recovery_pending = lambda: True

    assert (
        cli.run(
            _parse("grant", "list"),
            Path("/project"),
            grant_mutator=mutator,
        )
        == 2
    )
    assert "recovery is pending" in capsys.readouterr().err


def test_pending_recovery_does_not_bypass_coordinated_grant_mutation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    grant = authority.ProjectGrant(str(tmp_path), "vivado")
    mutator = _DirectGrantMutator()
    mutator.recovery_pending = lambda: True
    monkeypatch.setattr(authority, "_revoke_grant", lambda *_args: grant)

    assert (
        cli.run(
            _parse("grant", "revoke", str(tmp_path), "--kind", "vivado"),
            tmp_path,
            grant_mutator=mutator,
        )
        == 0
    )


@pytest.mark.parametrize("action", ["add", "revoke"])
def test_source_project_binding_refuses_explicit_target(tmp_path, monkeypatch, action):
    (tmp_path / "pyproject.toml").write_text("[tool.booley]\nsource_checkout = true\n")
    calls = []
    monkeypatch.setattr(authority, "_add_grant", lambda *a, **k: calls.append(a))
    monkeypatch.setattr(authority, "_revoke_grant", lambda *a, **k: calls.append(a))
    options = ["--installation", "registered"] if action == "add" else []
    assert (
        _run(_parse("grant", action, str(tmp_path), "--kind", "vivado", *options), tmp_path) == 2
    )
    assert calls == []


@pytest.mark.parametrize("action", ["add", "revoke"])
@pytest.mark.parametrize("selection", ["subdirectory", "relative", "symlink"])
def test_source_project_binding_target_aliases(tmp_path, monkeypatch, action, selection):
    calls = []
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    monkeypatch.setattr(authority, "_add_grant", lambda *a, **k: calls.append(a))
    monkeypatch.setattr(authority, "_revoke_grant", lambda *a, **k: calls.append(a))
    source = tmp_path / "source"
    source.mkdir()
    (source / "pyproject.toml").write_text("[tool.booley]\nsource_checkout = true\n")
    nested = source / "nested"
    nested.mkdir()
    monkeypatch.chdir(tmp_path)
    if selection == "symlink":
        target = tmp_path / "alias"
        try:
            target.symlink_to(source, target_is_directory=True)
        except OSError:
            pytest.skip("symlinks unavailable")
    else:
        target = nested if selection == "subdirectory" else Path("source")
    options = ["--installation", "registered"] if action == "add" else []
    assert _run(_parse("grant", action, str(target), "--kind", "vivado", *options), tmp_path) == 2
    assert calls == []


def test_project_binding_missing_revoke_preserves_argument(tmp_path, monkeypatch):
    missing = tmp_path / "missing"
    calls = []

    def revoke(project, kind):
        calls.append(project)
        return authority.ProjectGrant(str(project), kind, None, None)

    monkeypatch.setattr(authority, "_revoke_grant", revoke)
    assert _run(_parse("grant", "revoke", str(missing), "--kind", "vivado"), tmp_path) == 0
    assert calls == [missing]


@pytest.mark.parametrize("deleted_nested_project", [True, False])
@pytest.mark.parametrize("target_form", ["literal", "missing_parent", "file_parent"])
def test_project_binding_revoke_recorded_identity_under_source(
    tmp_path, monkeypatch, deleted_nested_project, target_form
):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    source = tmp_path / "source"
    source.mkdir()
    (source / ".git").mkdir()
    project = source / "nested" if deleted_nested_project else source
    (project / ".booley_project").mkdir(parents=True)
    if deleted_nested_project:
        (project / ".git").mkdir()
    authority.register_license(
        "registered",
        server_ipv4="192.0.2.1",
        server_hostid="license-host",
        lmgrd_port=27000,
        vendor_port=27001,
    )
    grant = authority._add_grant(project, "vivado", license_profile="registered")
    (source / "pyproject.toml").write_text("[tool.booley]\nsource_checkout = true\n")
    if deleted_nested_project:
        shutil.rmtree(project)
    target = project
    if target_form == "missing_parent":
        target = project / "missing" / ".."
    elif target_form == "file_parent":
        target = project / "pyproject.toml" / ".."
    result = _run(_parse("grant", "revoke", str(target), "--kind", "vivado"), tmp_path)
    assert result == (0 if deleted_nested_project else 2)
    assert authority.load_state().grants == (() if deleted_nested_project else (grant,))


@pytest.mark.parametrize("relative", [False, True])
def test_source_project_binding_revoke_symlink_parent(tmp_path, monkeypatch, relative):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    source = tmp_path / "source"
    (source / ".booley_project").mkdir(parents=True)
    (source / ".git").mkdir()
    nested = source / "nested"
    nested.mkdir()
    authority.register_license(
        "registered",
        server_ipv4="192.0.2.1",
        server_hostid="license-host",
        lmgrd_port=27000,
        vendor_port=27001,
    )
    grant = authority._add_grant(source, "vivado", license_profile="registered")
    (source / "pyproject.toml").write_text("[tool.booley]\nsource_checkout = true\n")
    other = tmp_path / "other"
    other.mkdir()
    alias = other / "alias"
    try:
        alias.symlink_to(nested, target_is_directory=True)
    except OSError:
        pytest.skip("symlinks unavailable")
    monkeypatch.chdir(other)
    target = Path("alias") / ".." if relative else alias / ".."
    assert _run(_parse("grant", "revoke", str(target), "--kind", "vivado"), other) == 2
    assert authority.load_state().grants == (grant,)


@pytest.mark.parametrize("action", ["add", "revoke"])
def test_project_binding_permission_error_is_clean(tmp_path, monkeypatch, capsys, action):
    calls = []
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    monkeypatch.setattr(authority, "_add_grant", lambda *a, **k: calls.append(a))
    monkeypatch.setattr(authority, "_revoke_grant", lambda *a, **k: calls.append(a))

    def check(project):
        raise PermissionError(f"Project checkout is unreadable: {project}")

    monkeypatch.setattr(cli, "require_project_checkout", check)
    assert _run(_parse("grant", action, str(tmp_path), "--kind", "vivado"), tmp_path) == 2
    assert "Project checkout is unreadable" in capsys.readouterr().err
    assert calls == []


def test_project_binding_inaccessible_revoke_keeps_source_grant(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    source = tmp_path / "source"
    (source / ".booley_project").mkdir(parents=True)
    (source / ".git").mkdir()
    authority.register_license(
        "registered",
        server_ipv4="192.0.2.1",
        server_hostid="license-host",
        lmgrd_port=27000,
        vendor_port=27001,
    )
    grant = authority._add_grant(source, "vivado", license_profile="registered")
    (source / "pyproject.toml").write_text("[tool.booley]\nsource_checkout = true\n")
    stat = Path.stat
    exists = Path.exists

    def inaccessible(path, **kwargs):
        if path == source:
            raise PermissionError("Project parent is unreadable")
        return stat(path, **kwargs)

    monkeypatch.setattr(Path, "stat", inaccessible)
    monkeypatch.setattr(Path, "exists", lambda path: False if path == source else exists(path))
    assert _run(_parse("grant", "revoke", str(source), "--kind", "vivado"), tmp_path) == 2
    assert "unreadable" in capsys.readouterr().err
    assert authority.load_state().grants == (grant,)


def test_project_binding_cleanup_permission_error_propagates(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    project = tmp_path / "project"
    (project / ".booley_project").mkdir(parents=True)
    (project / ".git").mkdir()
    authority.register_license(
        "registered",
        server_ipv4="192.0.2.1",
        server_hostid="license-host",
        lmgrd_port=27000,
        vendor_port=27001,
    )
    authority._add_grant(project, "vivado", license_profile="registered")
    revoke = authority._revoke_grant

    def cleanup_failure(target, kind):
        revoke(target, kind)
        raise PermissionError("cleanup failed after revocation")

    monkeypatch.setattr(authority, "_revoke_grant", cleanup_failure)
    with pytest.raises(PermissionError, match="cleanup failed after revocation"):
        _run(_parse("grant", "revoke", str(project), "--kind", "vivado"), tmp_path)
    assert authority.load_state().grants == ()
