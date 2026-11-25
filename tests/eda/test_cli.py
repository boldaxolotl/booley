"""Human and machine-readable host EDA CLI contracts."""

from __future__ import annotations

import argparse
import json
import shutil
from pathlib import Path

import pytest
from tests import eda_grant_support
from tests.docker_cli_guard import docker_cli_answers
from tests.vivado_release_fake import fake_vivado_release, requires_linux_x86_64

from booley.eda import cli
from booley.eda.provisioning import authority
from booley.harness import eda_grants, eda_view
from booley.runtime import issuance_invalidation

_LICENSE = (
    "--server-ipv4",
    "192.0.2.1",
    "--server-hostid",
    "license-host",
    "--vendor-port",
    "2101",
)


def _eda(
    capsys: pytest.CaptureFixture[str],
    *argv: str,
    mutator: cli.GrantMutator = eda_grants.COORDINATOR,
) -> tuple[int, str, str]:
    parser = argparse.ArgumentParser()
    cli.add_subparser(parser.add_subparsers(dest="command", required=True))
    args = parser.parse_args(("eda", *argv))
    code = cli.run(args, Path.cwd(), grant_mutator=mutator, record_view=eda_view)
    streams = capsys.readouterr()
    return code, streams.out, streams.err


@pytest.fixture(autouse=True)
def docker(monkeypatch: pytest.MonkeyPatch) -> list[list[str]]:
    """Answer the Docker CLI calls that grant revocation cleanup makes."""
    return docker_cli_answers(monkeypatch, "")


def _project(path: Path) -> Path:
    (path / ".git").mkdir(parents=True)
    return path


def test_license_grant_lifecycle_output(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], docker: list[list[str]]
) -> None:
    project = _project(tmp_path / "project")

    code, out, _ = _eda(capsys, "license", "register", "site", *_LICENSE, "--lmgrd-port", "2100")
    assert (code, out) == (0, "Registered License Profile 'site' for 192.0.2.1:2100.\n")

    code, out, _ = _eda(
        capsys, "grant", "add", str(project), "--kind", "vivado", "--license-profile", "site"
    )
    assert (code, out) == (
        0,
        f"Granted vivado EDA access to {project} using License Profile 'site'.\n",
    )

    code, out, _ = _eda(capsys, "grant", "revoke", str(project), "--kind", "vivado", "--json")
    assert code == 0
    assert json.loads(out) == {
        "installation": None,
        "kind": "vivado",
        "license_profile": "site",
        "project_root": str(project.resolve()),
        "residual_resources": [],
    }
    assert [call[:2] for call in docker] == [["container", "ls"], ["network", "ls"]]
    assert issuance_invalidation.pending_invalidations() == ()

    assert _eda(capsys, "license", "remove", "site")[1] == "Removed License Profile 'site'.\n"


@requires_linux_x86_64
def test_installation_grant_lifecycle_output(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    release = fake_vivado_release(tmp_path / "Xilinx" / "2025.2")
    project = _project(tmp_path / "project")
    eda_grant_support.register_license("site")

    code, out, _ = _eda(
        capsys, "installation", "register", "vivado", "--kind", "vivado", "--source", str(release)
    )
    assert (code, out) == (
        0,
        f"Registered vivado EDA installation 'vivado' from {release.resolve()}.\n",
    )
    code, out, _ = _eda(
        capsys, *("grant", "add", str(project), "--kind", "vivado", "--installation", "vivado")
    )
    assert (code, out) == (
        0,
        f"Granted vivado EDA access to {project} using installation 'vivado'.\n",
    )
    _eda(capsys, "grant", "revoke", str(project), "--kind", "vivado")
    code, out, _ = _eda(
        capsys,
        *("grant", "add", str(project), "--kind", "vivado"),
        *("--installation", "vivado", "--license-profile", "site"),
    )
    assert (code, out) == (
        0,
        f"Granted vivado EDA access to {project} using installation 'vivado' "
        "and License Profile 'site'.\n",
    )
    out = _eda(capsys, "grant", "revoke", str(project), "--kind", "vivado")[1]
    assert out == f"Revoked vivado EDA access for {project.resolve()}.\n"
    out = _eda(capsys, "installation", "remove", "vivado")[1]
    assert out == "Removed EDA installation 'vivado'.\n"


def test_legacy_grant_list_is_hidden_and_warns(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    with pytest.raises(SystemExit) as help_exit:
        _eda(capsys, "grant", "--help")
    assert help_exit.value.code == 0
    assert "list" not in capsys.readouterr().out

    project = _project(tmp_path / "project")
    eda_grant_support.register_license()
    eda_grant_support.add_grant(project, license_profile="registered")

    code, out, err = _eda(capsys, "grant", "list")

    assert code == 0
    assert json.loads(out) == [
        {
            "installation": None,
            "kind": "vivado",
            "license_profile": "registered",
            "project_root": str(project.resolve()),
        }
    ]
    assert "deprecated" in err.lower()
    assert "booley projects" in err


def test_pending_recovery_blocks_grant_list_and_completes_before_revoke(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    project = _project(tmp_path / "project")
    other = _project(tmp_path / "other")
    eda_grant_support.register_license()
    eda_grant_support.add_grant(project, license_profile="registered")
    issuance_invalidation.prepare(str(other.resolve()))

    code, _, err = _eda(capsys, "grant", "list")
    assert code == 2
    assert "recovery is pending" in err

    assert _eda(capsys, "grant", "revoke", str(project), "--kind", "vivado")[0] == 0
    assert authority.load_state().grants == ()
    assert issuance_invalidation.pending_invalidations() == ()


def _source_checkout(path: Path) -> None:
    (path / "pyproject.toml").write_text("[tool.booley]\nsource_checkout = true\n")


@pytest.mark.parametrize("action", ["add", "revoke"])
@pytest.mark.parametrize("selection", ["root", "subdirectory", "relative", "symlink"])
def test_source_project_binding_refuses_target(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    action: str,
    selection: str,
) -> None:
    source = _project(tmp_path / "source")
    (source / "nested").mkdir()
    eda_grant_support.register_license()
    if action == "revoke":
        eda_grant_support.add_grant(source, license_profile="registered")
    _source_checkout(source)
    monkeypatch.chdir(tmp_path)
    target = {
        "root": source,
        "subdirectory": source / "nested",
        "relative": Path("source"),
        "symlink": tmp_path / "alias",
    }[selection]
    if selection == "symlink":
        try:
            target.symlink_to(source, target_is_directory=True)
        except OSError:
            pytest.skip("symlinks unavailable")
    before = authority.load_state()
    options = ["--license-profile", "registered"] if action == "add" else []

    code, _, err = _eda(capsys, "grant", action, str(target), "--kind", "vivado", *options)

    assert code == 2
    assert "Source Checkout" in err
    assert authority.load_state() == before


@pytest.mark.parametrize("deleted_nested_project", [True, False])
@pytest.mark.parametrize("target_form", ["literal", "missing_parent", "file_parent"])
def test_project_binding_revoke_recorded_identity_under_source(
    tmp_path, capsys, deleted_nested_project, target_form
):
    source = _project(tmp_path / "source")
    project = source / "nested" if deleted_nested_project else source
    (project / ".booley").mkdir(parents=True)
    if deleted_nested_project:
        (project / ".git").mkdir()
    eda_grant_support.register_license()
    grant = eda_grant_support.add_grant(project, "vivado", license_profile="registered")
    _source_checkout(source)
    if deleted_nested_project:
        shutil.rmtree(project)
    target = project
    if target_form == "missing_parent":
        target = project / "missing" / ".."
    elif target_form == "file_parent":
        target = project / "pyproject.toml" / ".."
    code = _eda(capsys, "grant", "revoke", str(target), "--kind", "vivado")[0]
    assert code == (0 if deleted_nested_project else 2)
    assert authority.load_state().grants == (() if deleted_nested_project else (grant,))


@pytest.mark.parametrize("relative", [False, True])
def test_source_project_binding_revoke_symlink_parent(tmp_path, monkeypatch, capsys, relative):
    source = _project(tmp_path / "source")
    (source / ".booley").mkdir()
    nested = source / "nested"
    nested.mkdir()
    eda_grant_support.register_license()
    grant = eda_grant_support.add_grant(source, "vivado", license_profile="registered")
    _source_checkout(source)
    other = tmp_path / "other"
    other.mkdir()
    alias = other / "alias"
    try:
        alias.symlink_to(nested, target_is_directory=True)
    except OSError:
        pytest.skip("symlinks unavailable")
    monkeypatch.chdir(other)
    target = Path("alias") / ".." if relative else alias / ".."
    assert _eda(capsys, "grant", "revoke", str(target), "--kind", "vivado")[0] == 2
    assert authority.load_state().grants == (grant,)


def test_project_binding_inaccessible_revoke_keeps_source_grant(tmp_path, monkeypatch, capsys):
    source = _project(tmp_path / "source")
    (source / ".booley").mkdir()
    eda_grant_support.register_license()
    grant = eda_grant_support.add_grant(source, "vivado", license_profile="registered")
    _source_checkout(source)
    stat = Path.stat
    exists = Path.exists

    def inaccessible(path, **kwargs):
        if path == source:
            raise PermissionError("Project parent is unreadable")
        return stat(path, **kwargs)

    monkeypatch.setattr(Path, "stat", inaccessible)
    monkeypatch.setattr(Path, "exists", lambda path: False if path == source else exists(path))
    code, _, err = _eda(capsys, "grant", "revoke", str(source), "--kind", "vivado")
    assert code == 2
    assert "unreadable" in err
    assert authority.load_state().grants == (grant,)


def test_project_binding_cleanup_permission_error_propagates(tmp_path, capsys):
    project = _project(tmp_path / "project")
    (project / ".booley").mkdir()
    eda_grant_support.register_license()
    eda_grant_support.add_grant(project, "vivado", license_profile="registered")

    class FailingCleanup(eda_grants.GrantCoordinator):
        def revoke(self, project, kind):
            super().revoke(project, kind)
            raise PermissionError("cleanup failed after revocation")

    with pytest.raises(PermissionError, match="cleanup failed after revocation"):
        _eda(capsys, "grant", "revoke", str(project), "--kind", "vivado", mutator=FailingCleanup())
    assert authority.load_state().grants == ()
