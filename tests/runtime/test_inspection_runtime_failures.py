"""Runtime inspection fails closed on real policy inputs at filesystem/process seams."""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest
from tests.diagnostic_helpers import _isolate_runtime_files

from booley.audit.diagnostic_results import Severity
from booley.config.eda import EdaConfig
from booley.eda.provisioning.policies import vivado
from booley.runtime import inspection, runtime_context


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


@pytest.mark.parametrize("failure", ["identity", "missing-project", "exposed-authority"])
def test_runtime_rejects_broken_isolation_before_vivado_probe(
    mounted_runtime, monkeypatch, failure
):
    request, _, calls = mounted_runtime
    if failure == "identity":
        monkeypatch.setenv("BOOLEY_PROJECT_DIR", "/wrong-project")
    elif failure == "missing-project":
        original = Path.is_dir
        monkeypatch.setattr(
            Path, "is_dir", lambda p: False if p == Path("/booley-project") else original(p)
        )
    else:
        original = Path.exists
        monkeypatch.setattr(
            Path, "exists", lambda p: True if p == Path("/var/run/docker.sock") else original(p)
        )
    report = inspection.inspect_runtime(request).issuance
    assert len(report.findings) == 1 and report.findings[0].severity is Severity.FAIL
    assert not calls


def test_agent_inaccessible_authority_paths_do_not_break_runtime_inspection(
    mounted_runtime, monkeypatch
):
    request, _, calls = mounted_runtime
    original = Path.exists

    def exists(path):
        if path == Path("/root/.ssh"):
            raise PermissionError("agent cannot traverse root home")
        return original(path)

    monkeypatch.setattr(Path, "exists", exists)
    report = inspection.inspect_runtime(request).issuance
    assert all(f.severity is Severity.PASS for f in report.findings)
    assert len(calls) == 1


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
