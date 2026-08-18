"""Administrator-gated paid Vivado proof through production Session Runtime.

This test is inert unless ``BOOLEY_LICENSE_TEST=1``. It consumes only approved
topology metadata, never reads a license file, and accepts checkout evidence
only from a live vendor ``lmutil lmstat`` response observed through the relay.
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path

import pytest
from tests.license_evidence import (
    LicenseEvidenceError,
    parse_flexnet_lmstat,
    require_checkout_then_release,
)

from booley.eda import authority, runtime_spec
from booley.eda.flexnet_docker import resources_for_session
from booley.eda.vivado import CONTAINER_TARGET
from booley.harness import devcontainer as dc
from booley.harness import session_runtime

_FIXTURE = Path(__file__).resolve().parents[1] / "fixtures" / "vivado_mount_poc"
_PART_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{2,79}$")


@dataclass(frozen=True)
class Prerequisites:
    vivado_root: Path
    server_ipv4: str
    server_hostid: str
    lmgrd_port: int
    vendor_port: int
    part: str
    feature: str


def _required(name: str) -> str:
    value = os.environ.get(name, "").strip()
    if not value:
        pytest.fail(f"{name} is required when BOOLEY_LICENSE_TEST=1")
    return value


def _port(name: str) -> int:
    raw = _required(name)
    if not raw.isascii() or not raw.isdecimal() or not 1 <= int(raw) <= 65535:
        pytest.fail(f"{name} must be a decimal TCP port from 1 through 65535")
    return int(raw)


def _prerequisites() -> Prerequisites:
    if os.environ.get("BOOLEY_LICENSE_TEST") != "1":
        pytest.skip("set BOOLEY_LICENSE_TEST=1 only for an approved paid-seat window")
    if shutil.which("docker") is None:
        pytest.skip("Docker is unavailable")
    root = Path(_required("BOOLEY_VIVADO_ROOT")).resolve()
    if not os.access(root / "Vivado" / "bin" / "vivado", os.X_OK):
        pytest.fail("BOOLEY_VIVADO_ROOT is not an executable Vivado release root")
    part = _required("BOOLEY_VIVADO_PAID_PART")
    if _PART_RE.fullmatch(part) is None:
        pytest.fail("BOOLEY_VIVADO_PAID_PART contains unsupported characters")
    return Prerequisites(
        root,
        _required("BOOLEY_LICENSE_SERVER_IP"),
        _required("BOOLEY_LICENSE_SERVER_HOSTID"),
        _port("BOOLEY_LICENSE_LMGRD_PORT"),
        _port("BOOLEY_LICENSE_VENDOR_PORT"),
        part,
        _required("BOOLEY_LICENSE_EXPECTED_FEATURE"),
    )


def _project(tmp_path: Path, part: str) -> Path:
    workspace = tmp_path / "paid-vivado"
    shutil.copytree(_FIXTURE, workspace, ignore=shutil.ignore_patterns("Dockerfile", "README.md"))
    core = workspace / "vivado_mount_poc.core"
    text = core.read_text(encoding="utf-8")
    marker = "part: xc7a35tcpg236-1"
    if text.count(marker) != 1:
        pytest.fail("paid fixture no longer has one replaceable FPGA part")
    core.write_text(text.replace(marker, f"part: {part}"), encoding="utf-8")
    (workspace / "project_data").rename(workspace / ".booley_project")
    (workspace / ".booley_project").chmod(0o700)
    return workspace


def _issue(workspace: Path, p: Prerequisites) -> None:
    authority.register_installation("vivado_2025_2", "vivado", p.vivado_root)
    authority.register_license(
        "approved_site",
        server_ipv4=p.server_ipv4,
        server_hostid=p.server_hostid,
        lmgrd_port=p.lmgrd_port,
        vendor_port=p.vendor_port,
    )
    authority.add_grant(
        workspace,
        "vivado",
        installation="vivado_2025_2",
        license_profile="approved_site",
    )
    spec = dc.build_devcontainer_spec(
        dc.APP_NONE,
        project_dir_source=str((workspace / ".booley_project").resolve()),
        mcp_start_command=dc.mcp_post_start_command(),
        trusted_eda_mounts=((str(p.vivado_root), CONTAINER_TARGET),),
        protected_devcontainer_source=str(workspace / ".devcontainer"),
        fixed_container_env={"XILINXD_LICENSE_FILE": f"{p.lmgrd_port}@booley-license-xilinx"},
    )
    runtime_spec.pin_image(spec)
    runtime_spec.seal(workspace, spec)
    path = dc.write_devcontainer(workspace, spec)
    runtime_spec.issue(workspace, spec, path)


def _flow(container: str, *, timeout_ms: int = 600_000) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        _flow_argv(container, timeout_ms=timeout_ms),
        capture_output=True,
        text=True,
        timeout=(timeout_ms // 1000) + 90,
        check=False,
    )


def _flow_argv(container: str, *, timeout_ms: int) -> list[str]:
    return [
        "docker",
        "exec",
        container,
        "python3",
        "-m",
        "booley.flows.fpga",
        "--target",
        "fpga",
        "--work-dir",
        "/work",
        "--report-dir",
        "/work/report",
        "--timeout",
        str(timeout_ms),
    ]


def _lmstat(container: str, p: Prerequisites) -> subprocess.CompletedProcess[str]:
    lmutil = f"{CONTAINER_TARGET}/Vivado/bin/unwrapped/lnx64.o/lmutil"
    return subprocess.run(
        [
            "docker",
            "exec",
            container,
            "/lib64/ld-linux-x86-64.so.2",
            lmutil,
            "lmstat",
            "-c",
            f"{p.lmgrd_port}@booley-license-xilinx",
            "-f",
            p.feature,
            "-t",
            "3",
        ],
        capture_output=True,
        text=True,
        timeout=15,
        check=False,
    )


def _container_hostname(container: str) -> str:
    result = subprocess.run(
        ["docker", "inspect", container, "--format", "{{.Config.Hostname}}"],
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    if result.returncode != 0 or not result.stdout.strip():
        pytest.fail(f"cannot inspect Session hostname: {result.stderr}")
    return result.stdout.strip()


def _run_paid_flow_with_vendor_evidence(container: str, p: Prerequisites) -> None:
    """Run one Flow while independently sampling the fixed vendor status tool."""
    client_host = _container_hostname(container)
    process = subprocess.Popen(
        _flow_argv(container, timeout_ms=600_000),
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
    )
    active_status = ""
    while process.poll() is None:
        status = _lmstat(container, p)
        if status.returncode == 0:
            try:
                observed = parse_flexnet_lmstat(status.stdout, p.feature, client_host)
            except LicenseEvidenceError:
                pass
            else:
                if observed.client_observed:
                    active_status = status.stdout
                    break
        time.sleep(1)
    output, _ = process.communicate(timeout=690)
    assert process.returncode == 0, output
    assert active_status, "FlexNet never reported this Session's paid checkout"

    released_status = ""
    for _ in range(30):
        status = _lmstat(container, p)
        if status.returncode == 0:
            try:
                observed = parse_flexnet_lmstat(status.stdout, p.feature, client_host)
            except LicenseEvidenceError:
                pass
            else:
                if not observed.client_observed:
                    released_status = status.stdout
                    break
        time.sleep(1)
    assert released_status, "FlexNet still reports this Session after Flow exit"
    require_checkout_then_release(active_status, released_status, p.feature, client_host)


@pytest.mark.slow()
def test_paid_vivado_checkout_uses_production_relay_and_vendor_status_evidence(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    p = _prerequisites()
    workspace = _project(tmp_path, p.part)
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "host-config"))
    monkeypatch.setenv("BOOLEY_PROJECT_DIR", str(workspace / ".booley_project"))
    _issue(workspace, p)

    container = session_runtime.up(workspace)
    relay = resources_for_session(str(workspace.resolve()))
    try:
        _run_paid_flow_with_vendor_evidence(container, p)
        relay_logs = subprocess.run(
            ["docker", "logs", relay.relay_container],
            capture_output=True,
            text=True,
            timeout=30,
            check=False,
        )
        assert f"listener_port={p.lmgrd_port}" in relay_logs.stdout + relay_logs.stderr
        assert f"listener_port={p.vendor_port}" in relay_logs.stdout + relay_logs.stderr
        assert p.server_ipv4 not in relay_logs.stdout + relay_logs.stderr

        subprocess.run(
            ["docker", "stop", relay.relay_container],
            capture_output=True,
            text=True,
            timeout=30,
            check=False,
        )
        failed = _flow(container, timeout_ms=120_000)
        assert failed.returncode != 0, "paid Flow passed with its fixed relay stopped"
    finally:
        session_runtime.down(workspace, remove=True)
