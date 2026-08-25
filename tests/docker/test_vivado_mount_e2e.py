"""Opt-in production Session Runtime proof for host-provisioned Vivado.

Set ``BOOLEY_VIVADO_ROOT`` to the Xilinx 2025.2 release root. The test uses
isolated host authority, a host-issued immutable spec, the real ``booley
session`` lifecycle, the image-owned wrapper, and the ordinary FPGA Flow.
"""

from __future__ import annotations

import hashlib
import ipaddress
import json
import os
import shlex
import shutil
import socket
import subprocess
from pathlib import Path

import pytest

from booley.eda import authority, runtime_spec
from booley.eda.vivado import CONTAINER_TARGET, wrapper_sha256
from booley.harness import devcontainer as dc
from booley.harness import interactive_docker as idk
from booley.harness import session_runtime

_IMAGE = "booley-sandbox"
_VIVADO_ENV = "BOOLEY_VIVADO_ROOT"
_FIXTURE = Path(__file__).resolve().parents[1] / "fixtures" / "vivado_mount_poc"
_DEVCONTAINER_ENV = "BOOLEY_DEVCONTAINER_E2E"


def _require_prerequisites() -> tuple[str, Path]:
    docker = shutil.which("docker")
    if docker is None:
        pytest.skip("docker not available")
    value = os.environ.get(_VIVADO_ENV, "").strip()
    if not value:
        pytest.skip(f"{_VIVADO_ENV} is not set")
    root = Path(value).resolve()
    if not os.access(root / "Vivado" / "bin" / "vivado", os.X_OK):
        pytest.fail(f"{_VIVADO_ENV} must contain executable Vivado/bin/vivado: {root}")
    if not (root / "tps").is_dir():
        pytest.fail(f"{_VIVADO_ENV} must be the release root containing tps/: {root}")
    for kind, name in (("image", _IMAGE), ("network", dc.EGRESS_NETWORK)):
        probe = subprocess.run(
            [docker, kind, "inspect", name],
            capture_output=True,
            text=True,
            timeout=60,
            check=False,
        )
        if probe.returncode != 0:
            pytest.skip(f"Docker {kind} {name!r} is absent; run `booley init --force`")
    if not idk.network_is_internal() or not idk.network_is_host_isolated():
        pytest.fail(
            f"Docker network {dc.EGRESS_NETWORK!r} is not host-isolated; "
            "stop Sessions, remove the stale network and booley-proxy, then run "
            "`booley init --force`"
        )
    return docker, root


def _exec(
    docker: str, container: str, *command: str, timeout: int = 120
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [docker, "exec", container, *command],
        capture_output=True,
        text=True,
        timeout=timeout,
        check=False,
    )


def _issue_runtime(workspace: Path, vivado_root: Path) -> None:
    authority.register_installation("vivado_2025_2", "vivado", vivado_root)
    authority.add_grant(workspace, "vivado", installation="vivado_2025_2")
    spec = dc.build_devcontainer_spec(
        dc.APP_NONE,
        image=_IMAGE,
        project_dir_source=str((workspace / ".booley_project").resolve()),
        mcp_start_command=dc.mcp_post_start_command(),
        trusted_eda_mounts=((str(vivado_root), CONTAINER_TARGET),),
        protected_devcontainer_source=str((workspace / ".devcontainer").resolve()),
    )
    runtime_spec.pin_image(spec)
    runtime_spec.seal(workspace, spec)
    path = dc.write_devcontainer(workspace, spec)
    runtime_spec.issue(workspace, spec, path)


def _assert_runtime_boundary(docker: str, container: str) -> None:
    host_home = shlex.quote(str(Path.home()))
    script = f"""
set -eu
test ! -e {host_home}
test ! -e /root/.ssh
test ! -S /var/run/docker.sock
test "$(command -v vivado)" = /usr/local/bin/vivado
test "$(sha256sum /usr/local/bin/vivado | cut -d' ' -f1)" = {wrapper_sha256()}
! touch {CONTAINER_TARGET}/.booley-write-probe
! sh -c ': > /work/.devcontainer/devcontainer.json'
! sh -c 'printf drift > /tmp/new-spec && mv -f /tmp/new-spec /work/.devcontainer/devcontainer.json'
! chmod 600 /work/.devcontainer/devcontainer.json
! mv /work/.devcontainer/devcontainer.json /work/.devcontainer/changed
! rm /work/.devcontainer/devcontainer.json
! mv /work/.devcontainer /work/.devcontainer-old
! ln -s /tmp/owned /work/.devcontainer/owned-link
"""
    result = _exec(docker, container, "sh", "-c", script)
    assert result.returncode == 0, result.stdout + result.stderr


def _assert_host_gateway_is_unreachable(docker: str, container: str) -> None:
    """Prove a process listening on every host address is unreachable from Session."""
    inspected = subprocess.run(
        [docker, "network", "inspect", dc.EGRESS_NETWORK],
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )
    assert inspected.returncode == 0, inspected.stderr
    network = json.loads(inspected.stdout)[0]
    subnet = ipaddress.ip_network(network["IPAM"]["Config"][0]["Subnet"])
    former_gateway = str(next(subnet.hosts()))
    listener = socket.socket()
    listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    listener.bind(("0.0.0.0", 0))
    listener.listen()
    try:
        port = listener.getsockname()[1]
        probe = _exec(
            docker,
            container,
            "python3",
            "-c",
            (
                "import socket,sys;"
                "s=socket.socket();s.settimeout(1);"
                f"sys.exit(1 if s.connect_ex(('{former_gateway}',{port})) == 0 else 0)"
            ),
        )
        assert probe.returncode == 0, "Session reached an arbitrary host TCP listener"
    finally:
        listener.close()


def _run_flow(docker: str, container: str, report_dir: str) -> None:
    result = _exec(
        docker,
        container,
        "python3",
        "-m",
        "booley.flows.fpga",
        "--target",
        "fpga",
        "--work-dir",
        "/work",
        "--report-dir",
        report_dir,
        "--timeout",
        "600000",
        timeout=660,
    )
    combined = result.stdout + result.stderr
    assert result.returncode == 0, combined
    assert "RESULT: PASS" in combined


def _normalized_metrics(metrics: dict[str, object]) -> dict[str, object]:
    """Remove wall-clock telemetry while preserving implementation results."""
    return {key: value for key, value in metrics.items() if key != "elapsed_s"}


def _devcontainer_command() -> list[str] | None:
    if os.environ.get(_DEVCONTAINER_ENV) != "1":
        return None
    node = shutil.which("node")
    candidates = sorted(
        (Path.home() / ".vscode" / "extensions").glob(
            "ms-vscode-remote.remote-containers-*/dist/spec-node/devContainersSpecCLI.js"
        )
    )
    if node is None or not candidates:
        pytest.fail(f"{_DEVCONTAINER_ENV}=1 requires node and the VS Code Dev Containers CLI")
    return [node, str(candidates[-1])]


def _vscode_up(command: list[str], workspace: Path) -> subprocess.CompletedProcess[str]:
    environment = os.environ.copy()
    venv_bin = Path(__file__).resolve().parents[2] / ".venv" / "bin"
    environment["PATH"] = f"{venv_bin}{os.pathsep}{environment.get('PATH', '')}"
    return subprocess.run(
        [*command, "up", "--workspace-folder", str(workspace), "--include-configuration"],
        capture_output=True,
        text=True,
        timeout=300,
        check=False,
        env=environment,
    )


def _assert_vscode_lifecycle(docker: str, command: list[str], workspace: Path) -> None:
    project_id = hashlib.sha256(str(workspace.resolve()).encode()).hexdigest()
    spec = json.loads(
        (workspace / ".devcontainer" / "devcontainer.json").read_text(encoding="utf-8")
    )
    stale_source = workspace.parent / f"{workspace.name}-deleted-bind"
    stale_source.mkdir()
    stale = subprocess.run(
        [
            docker,
            "create",
            "--label",
            dc.INTERACTIVE_ROLE_LABEL,
            "--label",
            f"booley.project-id={project_id}",
            "--label",
            "booley.spec-digest=stale",
            "--label",
            f"devcontainer.local_folder={workspace}",
            "--label",
            f"devcontainer.config_file={workspace / '.devcontainer' / 'devcontainer.json'}",
            "--mount",
            f"type=bind,source={stale_source},target=/tmp/deleted-bind",
            str(spec["image"]),
            "sleep",
            "infinity",
        ],
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )
    assert stale.returncode == 0, stale.stdout + stale.stderr
    stale_container = stale.stdout.strip()
    stale_source.rmdir()
    container = ""
    try:
        created = _vscode_up(command, workspace)
        assert created.returncode == 0, created.stdout + created.stderr
        stale_inspect = subprocess.run(
            [docker, "inspect", stale_container],
            capture_output=True,
            text=True,
            timeout=60,
            check=False,
        )
        assert stale_inspect.returncode != 0, "stopped stale container was not reconciled"
        listed = subprocess.run(
            [docker, "ps", "-aq", "--filter", f"label=booley.project-id={project_id}"],
            capture_output=True,
            text=True,
            timeout=60,
            check=False,
        )
        assert listed.returncode == 0, listed.stdout + listed.stderr
        containers = [line for line in listed.stdout.splitlines() if line]
        assert len(containers) == 1, listed.stdout
        container = containers[0]
        version = _exec(docker, container, "vivado", "-version")
        assert version.returncode == 0, version.stdout + version.stderr
        assert "vivado v2025.2" in version.stdout.lower()
        _assert_runtime_boundary(docker, container)
        _assert_host_gateway_is_unreachable(docker, container)
        resumed = _vscode_up(command, workspace)
        assert resumed.returncode == 0, resumed.stdout + resumed.stderr
    finally:
        for candidate in (container, stale_container):
            if candidate:
                subprocess.run(
                    [docker, "rm", "-f", candidate],
                    capture_output=True,
                    text=True,
                    timeout=60,
                    check=False,
                )


def _assert_headless_lifecycle(docker: str, workspace: Path) -> None:
    container = session_runtime.up(workspace)
    try:
        for arguments in (("doctor",), ("doctor", "--deep")):
            checked = _exec(docker, container, "booley", *arguments, timeout=300)
            output = checked.stdout + checked.stderr
            assert (
                "mounted Vivado 2025.2 wrapper, read-only release, and identity verified" in output
            ), output
            assert "Session Runtime Project data and host-authority isolation verified" in output
        version = _exec(docker, container, "vivado", "-version")
        assert version.returncode == 0, version.stdout + version.stderr
        assert "vivado v2025.2" in version.stdout.lower()
        _assert_runtime_boundary(docker, container)
        _assert_host_gateway_is_unreachable(docker, container)

        stopped = subprocess.run(
            [docker, "stop", container],
            capture_output=True,
            text=True,
            timeout=60,
            check=False,
        )
        assert stopped.returncode == 0, stopped.stdout + stopped.stderr
        assert session_runtime.up(workspace) == container
        resumed = _exec(docker, container, "vivado", "-version")
        assert resumed.returncode == 0, resumed.stdout + resumed.stderr
        assert "vivado v2025.2" in resumed.stdout.lower()

        normalized: list[dict[str, object]] = []
        for index in (1, 2):
            report_dir = f"/work/report-{index}"
            _run_flow(docker, container, report_dir)
            report = json.loads(
                (workspace / f"report-{index}" / "fpga_fpga.json").read_text(encoding="utf-8")
            )
            assert report["passed"] is True
            metrics = report["metrics"]
            assert metrics["lut_count"] > 0
            assert metrics["ff_count"] > 0
            normalized.append(
                {"passed": report["passed"], "metrics": _normalized_metrics(metrics)}
            )
        assert normalized[0] == normalized[1]
    finally:
        session_runtime.down(workspace, remove=True)


@pytest.mark.slow()
def test_host_provisioned_vivado_completes_issued_session_runtime_flow_twice(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    docker, vivado_root = _require_prerequisites()
    workspace = tmp_path / f"vivado-e2e-{tmp_path.name}"
    shutil.copytree(_FIXTURE, workspace, ignore=shutil.ignore_patterns("Dockerfile", "README.md"))
    (workspace / ".git").mkdir()
    (workspace / "project_data").rename(workspace / ".booley_project")
    (workspace / ".booley_project").chmod(0o700)
    config_home = tmp_path / "host-config"
    monkeypatch.setenv("XDG_CONFIG_HOME", str(config_home))
    monkeypatch.setenv("BOOLEY_PROJECT_DIR", str(workspace / ".booley_project"))
    _issue_runtime(workspace, vivado_root)

    host_doctor = subprocess.run(
        ["booley", "doctor"],
        cwd=workspace,
        capture_output=True,
        text=True,
        timeout=300,
        check=False,
    )
    host_output = host_doctor.stdout + host_doctor.stderr
    assert "Session Runtime spec has valid host issuance" in host_output, host_output

    devcontainer_command = _devcontainer_command()
    if devcontainer_command is not None:
        _assert_vscode_lifecycle(docker, devcontainer_command, workspace)

    _assert_headless_lifecycle(docker, workspace)

    authority.revoke_grant(workspace, "vivado")
    with pytest.raises(session_runtime.SessionError, match="host-issued spec stamp"):
        session_runtime.up(workspace)
    if devcontainer_command is not None:
        denied = _vscode_up(devcontainer_command, workspace)
        assert denied.returncode != 0
        assert "host-issued spec stamp" in denied.stdout + denied.stderr
