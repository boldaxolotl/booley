"""Runtime inspection fails closed on real policy inputs at filesystem/process seams."""

from __future__ import annotations

import os
import subprocess
from dataclasses import replace
from pathlib import Path

import pytest
from tests.diagnostic_helpers import _isolate_runtime_files
from tests.harness import test_session_runtime as resolution_fixtures

from booley.audit.diagnostic_results import Severity
from booley.config.eda import EdaConfig
from booley.eda.provisioning.policies import vivado
from booley.runtime import inspection, runtime_context

project_sandbox_resolution = resolution_fixtures.project_sandbox_resolution
workspace = resolution_fixtures.workspace


def _runtime_files(monkeypatch, state):
    is_dir, is_file = Path.is_dir, Path.is_file
    read_bytes, read_text = Path.read_bytes, Path.read_text
    compatibility = {
        Path("/usr/lib/x86_64-linux-gnu/libudev.so.1"),
        Path("/usr/lib/x86_64-linux-gnu/libpixman-1.so.0"),
        Path("/usr/lib/locale/locale-archive"),
    }

    def contents(path, *args, **kwargs):
        if path != Path("/proc/self/mountinfo"):
            return read_text(path, *args, **kwargs)
        if isinstance(state["mountinfo"], Exception):
            raise state["mountinfo"]
        return state["mountinfo"]

    def wrapper(path):
        if path != Path(vivado.WRAPPER_TARGET):
            return read_bytes(path)
        if isinstance(state["wrapper"], Exception):
            raise state["wrapper"]
        return state["wrapper"]

    monkeypatch.setattr(
        Path,
        "is_dir",
        lambda p: state["mounted"] if p == Path(vivado.CONTAINER_TARGET) else is_dir(p),
    )
    monkeypatch.setattr(
        Path, "is_file", lambda p: state["compatible"] if p in compatibility else is_file(p)
    )
    monkeypatch.setattr(Path, "read_bytes", wrapper)
    monkeypatch.setattr(Path, "read_text", contents)
    monkeypatch.setattr(
        os,
        "access",
        lambda p, _mode: (
            state["executable"]
            if p == Path(vivado.CONTAINER_TARGET) / "Vivado/bin/vivado"
            else False
        ),
    )


@pytest.fixture
def mounted_runtime(tmp_path, monkeypatch):
    state = {
        "wrapper": vivado.wrapper_path().read_bytes(),
        "mounted": True,
        "compatible": True,
        "executable": True,
        "mountinfo": "36 25 0:32 / /opt/booley-eda/vivado ro,relatime - ext4 /dev/root ro\n",
        "version": f"Vivado v{vivado.SUPPORTED_VERSION}",
        "returncode": 0,
    }
    calls = []
    monkeypatch.setattr(runtime_context, "inside_session_runtime", lambda: True)
    monkeypatch.delenv("XILINXD_LICENSE_FILE", raising=False)
    _isolate_runtime_files(monkeypatch)
    _runtime_files(monkeypatch, state)

    def run(argv, **kwargs):
        if argv[0] == "git":
            return subprocess.CompletedProcess(argv, 1, "", "not a repository")
        assert Path(argv[0]) == Path(vivado.WRAPPER_TARGET)
        calls.append((argv, kwargs))
        if isinstance(state["version"], Exception):
            raise state["version"]
        return subprocess.CompletedProcess(argv, state["returncode"], state["version"], "")

    monkeypatch.setattr(subprocess, "run", run)
    request = inspection.RuntimeInspectionRequest(
        tmp_path, "image", None, eda={"vivado": EdaConfig("vivado", "host")}, fpga_enabled=True
    )
    return request, state, calls


@pytest.mark.parametrize(
    "field,value,message",
    [
        ("wrapper", PermissionError("denied"), "wrapper is unreadable"),
        ("wrapper", b"changed wrapper", "differs from built-in policy"),
        ("executable", False, "differs from built-in policy"),
        ("compatible", False, "compatibility libraries"),
        ("mountinfo", OSError("unreadable"), "cannot inspect mounted"),
        (
            "mountinfo",
            "36 25 0:32 / /opt/booley-eda/vivado rw - ext4 /dev/root rw\n",
            "exact read-only",
        ),
        ("mountinfo", "", "exact read-only"),
        ("version", subprocess.TimeoutExpired("vivado", 90), "identity probe failed"),
        ("version", "Vivado v1900.1", "exact version"),
        ("returncode", 1, "exact version"),
        ("mounted", False, "host-provisioned Vivado is absent"),
    ],
)
def test_mounted_runtime_rejects_invalid_evidence(mounted_runtime, field, value, message):
    request, state, calls = mounted_runtime
    state[field] = value
    failures = [
        f
        for f in inspection.inspect_runtime(request).issuance.findings
        if f.severity is Severity.FAIL
    ]
    assert len(failures) == 1 and message in failures[0].message
    assert bool(calls) is (field in {"version", "returncode"})
    if calls:
        assert calls[0][1]["timeout"] == 90


def test_mounted_runtime_rejects_external_license_pointer_before_execution(
    mounted_runtime, monkeypatch
):
    request, _, calls = mounted_runtime
    monkeypatch.setenv("XILINXD_LICENSE_FILE", "27000@outside-host")
    report = inspection.inspect_runtime(request).issuance
    assert any(
        f.severity is Severity.FAIL and "private-relay contract" in f.message
        for f in report.findings
    )
    assert not calls


@pytest.mark.parametrize("failure", ["exposed-authority", "exposed-eda-config"])
def test_runtime_rejects_broken_isolation_before_vivado_probe(
    mounted_runtime, monkeypatch, failure
):
    request, _, calls = mounted_runtime
    exposed = Path(
        "/var/run/docker.sock"
        if failure == "exposed-authority"
        else "/home/agent/.config/booley/eda"
    )
    original = Path.exists
    monkeypatch.setattr(Path, "exists", lambda p: True if p == exposed else original(p))
    failures = _fail_findings(inspection.inspect_runtime(request).issuance)
    assert len(failures) == 1 and "forbidden host-authority surface" in failures[0].message
    assert not calls


def test_agent_inaccessible_authority_paths_do_not_break_runtime_inspection(
    mounted_runtime, monkeypatch
):
    request, _, calls = mounted_runtime
    original_exists = Path.exists
    original_lstat = Path.lstat

    def exists(path):
        if path == Path("/root/.ssh"):
            raise PermissionError("agent cannot traverse root home")
        return original_exists(path)

    def lstat(path, *args, **kwargs):
        if path == Path("/root/.ssh"):
            raise PermissionError("agent cannot traverse root home")
        return original_lstat(path, *args, **kwargs)

    monkeypatch.setattr(Path, "exists", exists)
    monkeypatch.setattr(Path, "lstat", lstat)
    report = inspection.inspect_runtime(request).issuance
    assert all(f.severity is Severity.PASS for f in report.findings)
    assert len(calls) == 1


def _redirect_ssh_directory(monkeypatch, tmp_path, target):
    """Point the runtime check's ``target`` SSH path at a scratch location; return it."""
    scratch = tmp_path / "agent-ssh"
    real_path = Path

    def redirected(value, *args):
        return scratch if str(value) == target else real_path(value, *args)

    monkeypatch.setattr(inspection, "Path", redirected)
    return scratch


def _build_ssh_layout(scratch, layout):
    """Create the named SSH directory layout at ``scratch``; return the offending path or None."""
    outside = scratch.parent / "outside"
    outside.mkdir()
    (outside / "authorized_keys").write_text("ssh-ed25519 AAAA inbound\n")
    keys = scratch / "authorized_keys"
    if layout == "dangling-ssh-symlink":
        scratch.symlink_to(scratch.parent / "missing")
    elif layout == "ssh-regular-file":
        scratch.write_text("not a directory")
    elif layout == "symlinked-ssh":
        scratch.symlink_to(outside)
    else:
        scratch.mkdir()
    offending = scratch
    if layout == "symlinked-authorized_keys":
        keys.symlink_to(outside / "authorized_keys")
        offending = keys
    elif layout == "authorized_keys-directory":
        keys.mkdir()
        offending = keys
    elif layout in {
        "authorized_keys",
        "id_ed25519",
        "known_hosts",
        "config",
        "root-id_ed25519",
        ".hidden_key",
    }:
        keys.write_text("ssh-ed25519 AAAA inbound\n")
        offending = scratch / layout.removeprefix("root-")
        offending.write_text("outbound authority")
    return None if layout in {"empty", "authorized_keys"} else offending


@pytest.mark.parametrize(
    "target,layout",
    [
        ("/home/agent/.ssh", "empty"),
        ("/home/agent/.ssh", "authorized_keys"),
        ("/home/agent/.ssh", "id_ed25519"),
        ("/home/agent/.ssh", "known_hosts"),
        ("/home/agent/.ssh", "config"),
        # Sorts before authorized_keys: a loop that stops at the first good entry misses it.
        ("/home/agent/.ssh", ".hidden_key"),
        ("/home/agent/.ssh", "symlinked-ssh"),
        ("/home/agent/.ssh", "symlinked-authorized_keys"),
        ("/home/agent/.ssh", "authorized_keys-directory"),
        ("/home/agent/.ssh", "dangling-ssh-symlink"),
        ("/home/agent/.ssh", "ssh-regular-file"),
        ("/root/.ssh", "root-id_ed25519"),
    ],
)
def test_runtime_ssh_directory_admits_only_inbound_authorized_keys(
    mounted_runtime, monkeypatch, tmp_path, target, layout
):
    """Inbound-only SSH state passes; every outbound-capable entry FAILs naming it."""
    request, _, calls = mounted_runtime
    scratch = _redirect_ssh_directory(monkeypatch, tmp_path, target)
    offending = _build_ssh_layout(scratch, layout)
    report = inspection.inspect_runtime(request).issuance
    if offending is None:
        assert [f.message for f in report.findings if f.severity is not Severity.PASS] == []
        assert len(calls) == 1
        return
    failures = _fail_findings(report)
    assert len(failures) == 1 and failures[0].message.endswith(f": {offending}")
    assert not calls


def test_unlistable_ssh_directory_stays_exposed(mounted_runtime, monkeypatch, tmp_path):
    """A visible SSH directory that cannot be listed fails closed, as before."""
    request, _, calls = mounted_runtime
    scratch = _redirect_ssh_directory(monkeypatch, tmp_path, "/home/agent/.ssh")
    scratch.mkdir()
    original_iterdir = Path.iterdir

    def iterdir(path):
        if path == scratch:
            raise PermissionError("cannot list")
        return original_iterdir(path)

    monkeypatch.setattr(Path, "iterdir", iterdir)
    report = inspection.inspect_runtime(request).issuance
    failures = _fail_findings(report)
    assert len(failures) == 1 and failures[0].message.endswith(f": {scratch}")
    assert not calls


@pytest.mark.parametrize(
    "case", ["ssh-mount", "authorized_keys-mount", "mountinfo-unreadable", "parent-mount-only"]
)
def test_ssh_mount_point_is_host_authority(mounted_runtime, monkeypatch, tmp_path, case):
    """A mounted ``.ssh`` or ``authorized_keys`` FAILs; an ancestor mount does not."""
    request, state, calls = mounted_runtime
    scratch = _redirect_ssh_directory(monkeypatch, tmp_path, "/home/agent/.ssh")
    _build_ssh_layout(scratch, "authorized_keys")
    extra = {
        "ssh-mount": scratch,
        "authorized_keys-mount": scratch / "authorized_keys",
        "parent-mount-only": tmp_path,
    }.get(case)
    offending = scratch / "authorized_keys" if case == "authorized_keys-mount" else scratch
    if case == "mountinfo-unreadable":
        state["mountinfo"] = OSError("unreadable")
    elif extra is not None:
        state["mountinfo"] += f"37 25 0:33 / {extra} rw,relatime - ext4 /dev/host rw\n"
    report = inspection.inspect_runtime(request).issuance
    if case == "parent-mount-only":
        assert [f.message for f in report.findings if f.severity is not Severity.PASS] == []
        assert len(calls) == 1
        return
    failures = _fail_findings(report)
    assert len(failures) == 1 and failures[0].message.endswith(f": {offending}")
    assert not calls


def test_retained_resources_separate_current_project_from_other_project_keepers(
    tmp_path, monkeypatch
):
    from booley.runtime import interactive_docker, session_issuance

    mine = session_issuance.keeper_image(tmp_path)
    other = session_issuance.keeper_image(tmp_path / "old-project")
    monkeypatch.setattr(interactive_docker, "state_volumes", lambda: [])
    monkeypatch.setattr(interactive_docker, "issued_image_tags", lambda: [mine, other])
    report = inspection.inspect_retained_resources(tmp_path, "docker")
    assert any(
        f.severity is Severity.PASS and "retained for this Project" in f.message
        for f in report.findings
    )
    notes = [f for f in report.findings if f.severity is Severity.NOTE]
    assert len(notes) == 1 and "1 issued image keeper(s)" in notes[0].message
    assert len(report.details) == 1 and report.details[0].message.strip() == other
    assert report.details[0].after_finding == len(report.findings)


def test_host_inspection_reports_unknown_project_inventory(
    workspace, project_sandbox_resolution, monkeypatch
):
    fixture = project_sandbox_resolution
    fixture.state["Config"]["Labels"].pop("booley.project-id")
    monkeypatch.setattr(
        inspection,
        "_list_issued_containers",
        lambda *_: subprocess.CompletedProcess([], 0, "", ""),
    )
    report = inspection.Findings()
    inspection._inspect_live_runtime(
        inspection.RuntimeInspectionRequest(workspace, fixture.spec["image"], "docker"),
        fixture.spec,
        fixture.issuance,
        report,
    )
    assert any("identity disagrees" in item.message for item in report.report().findings)


def _fail_findings(report):
    """Return every FAIL finding (identity and host-authority) from one runtime inspection."""
    return [f for f in report.findings if f.severity is Severity.FAIL]


def _hide_image_alias(monkeypatch):
    """Make the image's ``/booley-project`` alias dangle, as in a Codespace."""
    original = Path.is_dir
    monkeypatch.setattr(
        Path, "is_dir", lambda p: False if p == Path("/booley-project") else original(p)
    )


def _booley_dir_spelling(project_root: Path, spelling: str) -> str:
    """Build one ``BOOLEY_DIR`` value for the named layout under ``project_root``."""
    if spelling == "fixed-alias":
        return "/booley-project"
    entry = project_root / ".booley"
    if spelling == "project-directory":
        entry.mkdir()
    elif spelling == "project-symlink":
        external = project_root.parent / f"{project_root.name}-booley-prj"
        external.mkdir()
        entry.symlink_to(external)
    elif spelling == "project-dangling-symlink":
        entry.symlink_to(project_root.parent / "missing-booley-prj")
    elif spelling == "outside-project-root":
        entry = project_root.parent / f"{project_root.name}-elsewhere"
        entry.mkdir()
    elif spelling == "other-directory-dot-booley":
        entry = project_root / "nested" / ".booley"
        entry.mkdir(parents=True)
    elif spelling == "project-relative":
        (project_root / ".booley").mkdir()
        return ".booley"
    elif spelling == "unrelated-path":
        return "/wrong-project"
    else:
        raise AssertionError(f"unknown spelling {spelling}")
    return str(entry)


@pytest.mark.parametrize("spelling", ["fixed-alias", "project-directory", "project-symlink"])
def test_runtime_accepts_supported_booley_dir_spellings(
    mounted_runtime, monkeypatch, tmp_path, spelling
):
    """The fixed alias and the Project Root's ``.booley`` entry both pass identity."""
    request, _, calls = mounted_runtime
    project_root = tmp_path / "workspace"
    project_root.mkdir()
    request = replace(request, project_root=project_root)
    monkeypatch.setenv("BOOLEY_DIR", _booley_dir_spelling(project_root, spelling))
    if spelling != "fixed-alias":
        _hide_image_alias(monkeypatch)
    monkeypatch.delenv("CODESPACES", raising=False)
    report = inspection.inspect_runtime(request).issuance
    assert _fail_findings(report) == []
    messages = [f.message for f in report.findings]
    assert any("Booley Directory identity verified" in m for m in messages)
    assert "Sandbox host-authority isolation verified" in messages
    assert len(calls) == 1


@pytest.mark.parametrize(
    "spelling",
    [
        "fixed-alias",
        "outside-project-root",
        "other-directory-dot-booley",
        "project-dangling-symlink",
        "project-relative",
    ],
)
def test_runtime_rejects_other_booley_dir_spellings_and_keeps_surface_checks(
    mounted_runtime, monkeypatch, tmp_path, spelling
):
    """Unsupported spellings FAIL identity; surface and Vivado checks still run."""
    request, _, calls = mounted_runtime
    project_root = tmp_path / "workspace"
    project_root.mkdir()
    request = replace(request, project_root=project_root)
    monkeypatch.chdir(project_root)
    monkeypatch.setenv("BOOLEY_DIR", _booley_dir_spelling(project_root, spelling))
    _hide_image_alias(monkeypatch)  # a missing alias must not rescue the fixed spelling
    report = inspection.inspect_runtime(request).issuance
    failures = _fail_findings(report)
    assert len(failures) == 1
    assert "Booley Directory identity" in failures[0].message
    fix = failures[0].fix
    assert f"BOOLEY_DIR to {project_root / '.booley'}" in fix
    assert "issued Sandbox" in fix
    assert "on the host" not in fix and "booley doctor" not in fix
    assert "Sandbox host-authority isolation verified" in [f.message for f in report.findings]
    assert len(calls) == 1


def test_identity_failure_does_not_hide_exposed_authority(mounted_runtime, monkeypatch):
    """An identity FAIL and a forbidden surface each report their own FAIL."""
    request, _, calls = mounted_runtime
    monkeypatch.setenv("BOOLEY_DIR", "/wrong-project")
    original = Path.exists
    exposed = Path("/var/run/docker.sock")
    monkeypatch.setattr(Path, "exists", lambda p: True if p == exposed else original(p))
    failures = _fail_findings(inspection.inspect_runtime(request).issuance)
    messages = [f.message for f in failures]
    assert any("Booley Directory identity" in m for m in messages)
    assert any("forbidden host-authority surface" in m for m in messages)
    assert len(failures) == 2
    assert not calls


def test_codespaces_variable_never_widens_booley_dir_identity(
    mounted_runtime, monkeypatch, tmp_path
):
    """CODESPACES is spoofable ambient state; an outside path still FAILs with it set."""
    request, _, _ = mounted_runtime
    project_root = tmp_path / "workspace"
    project_root.mkdir()
    request = replace(request, project_root=project_root)
    monkeypatch.setenv("BOOLEY_DIR", _booley_dir_spelling(project_root, "outside-project-root"))
    monkeypatch.setenv("CODESPACES", "true")
    _hide_image_alias(monkeypatch)
    failures = _fail_findings(inspection.inspect_runtime(request).issuance)
    assert len(failures) == 1 and "Booley Directory identity" in failures[0].message
