"""Opt-in production Sandbox proof for host-provisioned Vivado.

Set ``BOOLEY_VIVADO_ROOT`` to the Xilinx 2025.2 release root. The test uses
isolated host authority, a host-issued immutable spec, the real ``booley
session`` lifecycle, the image-owned wrapper, and the ordinary FPGA Flow.
"""

from __future__ import annotations

import hashlib
import ipaddress
import json
import os
import re
import shlex
import shutil
import socket
import subprocess
from pathlib import Path

import pytest

from booley.eda.provisioning import authority
from booley.eda.provisioning.policies.vivado import CONTAINER_TARGET, wrapper_sha256
from booley.runtime import devcontainer as dc
from booley.runtime import interactive_docker as idk
from booley.runtime import session_issuance as runtime_spec
from booley.runtime import session_runtime

_IMAGE = os.environ.get("BOOLEY_VIVADO_E2E_IMAGE", "booley-sandbox")
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
    authority._add_grant(workspace, "vivado", installation="vivado_2025_2")
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
{_expect_denied_script()}
expect_denied touch {CONTAINER_TARGET}/.booley-write-probe
expect_denied sh -c ': > /work/.devcontainer/devcontainer.json'
expect_denied sh -c 'printf drift > /tmp/new-spec && mv -f /tmp/new-spec /work/.devcontainer/devcontainer.json'
expect_denied chmod 600 /work/.devcontainer/devcontainer.json
expect_denied mv /work/.devcontainer/devcontainer.json /work/.devcontainer/changed
expect_denied rm /work/.devcontainer/devcontainer.json
expect_denied mv /work/.devcontainer /work/.devcontainer-old
expect_denied ln -s /tmp/owned /work/.devcontainer/owned-link
"""
    result = _exec(docker, container, "sh", "-c", script)
    assert result.returncode == 0, result.stdout + result.stderr


def _expect_denied_script() -> str:
    return r"""
expect_denied() {
    if "$@"; then
        printf 'Unexpected boundary write success: %s\n' "$*" >&2
        exit 1
    fi
}
"""


@pytest.mark.parametrize("command", ["true", "false"])
def test_runtime_boundary_guard_rejects_successful_writes(command: str) -> None:
    if os.name == "nt":
        pytest.skip("POSIX shell guard")
    result = subprocess.run(
        [
            "/bin/sh",
            "-c",
            "set -eu\n" + _expect_denied_script() + f"\nexpect_denied {command}\nprintf finished",
        ],
        capture_output=True,
        text=True,
        timeout=10,
        check=False,
    )
    assert (result.returncode == 0) is (command == "false"), result.stdout + result.stderr
    assert ("finished" in result.stdout) is (command == "false")


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
        "--timeout-ms",
        "600000",
        timeout=660,
    )
    combined = result.stdout + result.stderr
    assert result.returncode == 0, combined
    assert "RESULT: PASS" in combined


def _normalized_metrics(metrics: dict[str, object]) -> dict[str, object]:
    """Remove invocation telemetry while preserving implementation results."""
    transient = {"elapsed_s", "cached", "log_path"}
    normalized = {key: value for key, value in metrics.items() if key not in transient}
    artifacts = normalized.pop("artifacts", None)
    if isinstance(artifacts, dict):
        evidence = {
            key: value for key, value in artifacts.items() if key not in {"log", "live_dirs"}
        }
        if evidence:
            normalized["artifacts"] = evidence
    elif artifacts is not None:
        normalized["artifacts"] = artifacts
    return normalized


def test_normalized_metrics_preserves_implementation_and_artifact_evidence() -> None:
    original = {
        "lut_count": 7,
        "per_clock": {"clk": {"wns_ns": 7.462}},
        "cache_fingerprint": "same-design",
        "failure_output": "",
        "artifacts": {"log": "report-1/run.log", "content_count": 2},
        "cached": False,
        "elapsed_s": 70,
    }
    resumed = {
        **original,
        "artifacts": {"live_dirs": {"build": "scratch"}, "content_count": 2},
        "cached": True,
        "elapsed_s": 0,
    }
    assert _normalized_metrics(original) == _normalized_metrics(resumed)
    assert _normalized_metrics(resumed)["artifacts"] == {"content_count": 2}
    assert _normalized_metrics({**resumed, "lut_count": 8}) != _normalized_metrics(original)
    assert _normalized_metrics(
        {**resumed, "artifacts": {"content_count": 3}}
    ) != _normalized_metrics(original)
    for key, changed in (
        ("cache_fingerprint", "different-design"),
        ("failure_output", "implementation failed"),
        ("per_clock", {"clk": {"wns_ns": -1.0}}),
    ):
        assert _normalized_metrics({**resumed, key: changed}) != _normalized_metrics(original)


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


def _create_stale_vscode_container(
    docker: str, workspace: Path, project_id: str, image: str
) -> str:
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
            image,
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
    return stale_container


def _single_project_container(docker: str, project_id: str) -> str:
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
    return containers[0]


def _remove_containers(docker: str, *containers: str) -> None:
    for container in containers:
        if container:
            subprocess.run(
                [docker, "rm", "-f", container],
                capture_output=True,
                text=True,
                timeout=60,
                check=False,
            )


def _assert_vscode_lifecycle(docker: str, command: list[str], workspace: Path) -> None:
    project_id = hashlib.sha256(str(workspace.resolve()).encode()).hexdigest()
    spec = json.loads(
        (workspace / ".devcontainer" / "devcontainer.json").read_text(encoding="utf-8")
    )
    stale_container = _create_stale_vscode_container(
        docker, workspace, project_id, str(spec["image"])
    )
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
        container = _single_project_container(docker, project_id)
        version = _exec(docker, container, "vivado", "-version")
        assert version.returncode == 0, version.stdout + version.stderr
        assert "vivado v2025.2" in version.stdout.lower()
        _assert_runtime_boundary(docker, container)
        _assert_host_gateway_is_unreachable(docker, container)
        resumed = _vscode_up(command, workspace)
        assert resumed.returncode == 0, resumed.stdout + resumed.stderr
    finally:
        _remove_containers(docker, container, stale_container)


def _assert_headless_lifecycle(docker: str, workspace: Path) -> None:
    container = session_runtime.up(workspace)
    try:
        for arguments in (("doctor",), ("doctor", "--deep")):
            checked = _exec(docker, container, "booley", *arguments, timeout=300)
            output = checked.stdout + checked.stderr
            assert (
                "mounted Vivado 2025.2 wrapper, read-only release, and identity verified" in output
            ), output
            assert "Sandbox Project data and host-authority isolation verified" in output
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

        _assert_vivado_image_libraries(docker, container)
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
            assert metrics["cached"] is (index == 2)
            if index == 1:
                assert metrics["log_path"]
                assert metrics["artifacts"]["log"]
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
    initialized = subprocess.run(
        ["git", "-c", "init.templateDir=", "init", str(workspace)],
        capture_output=True,
        text=True,
        timeout=10,
        check=False,
        env={**os.environ, "GIT_CONFIG_NOSYSTEM": "1", "GIT_CONFIG_GLOBAL": os.devnull},
    )
    assert initialized.returncode == 0, initialized.stdout + initialized.stderr
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
    assert "Sandbox spec has valid host issuance" in host_output, host_output

    devcontainer_command = _devcontainer_command()
    if devcontainer_command is not None:
        _assert_vscode_lifecycle(docker, devcontainer_command, workspace)

    _assert_headless_lifecycle(docker, workspace)

    authority._revoke_grant(workspace, "vivado")
    refusal = f"Project {workspace.resolve()} has no exact vivado grant"
    with pytest.raises(session_runtime.SessionError, match=re.escape(refusal)):
        session_runtime.up(workspace)
    if devcontainer_command is not None:
        denied = _vscode_up(devcontainer_command, workspace)
        assert denied.returncode != 0
        assert refusal in denied.stdout + denied.stderr


def _assert_vivado_image_libraries(docker: str, container: str) -> None:
    expected = os.environ.get("BOOLEY_VIVADO_E2E_EXPECT_OS")
    if not expected:
        return
    script = f"""
set -eu
. /etc/os-release
test "$ID:$VERSION_ID" = {shlex.quote(expected)}
libroot={CONTAINER_TARGET}/Vivado/lib/lnx64.o
test "$("{CONTAINER_TARGET}/Vivado/bin/ldlibpath.sh" "$libroot")" = "$libroot/Ubuntu:$libroot"
{_no_system_vivado_libraries_script()}
sha256sum /usr/local/bin/vivado
cat /etc/os-release
"""
    result = _exec(docker, container, "sh", "-c", script)
    assert result.returncode == 0, result.stdout + result.stderr
    inspected = subprocess.run(
        [docker, "inspect", container, "--format", "{{.Image}}"],
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )
    assert inspected.returncode == 0, inspected.stderr
    print("Vivado acceptance image:", inspected.stdout.strip())
    print(result.stdout)


def _no_system_vivado_libraries_script() -> str:
    return r"""
files=$(find /lib /usr/lib \( -name libncurses.so.5 -o -name libtinfo.so.5 \))
if [ -n "$files" ]; then
    printf 'Unexpected system Vivado compatibility files: %s\n' "$files" >&2
    exit 1
fi
for package in libncurses5 libtinfo5; do
    status=$(dpkg-query -W -f='${db:Status-Status}' "$package" 2>/dev/null || true)
    if [ "$status" = installed ]; then
        printf 'Unexpected system Vivado compatibility package: %s\n' "$package" >&2
        exit 1
    fi
done
"""


@pytest.mark.parametrize("workaround", ["none", "file", "libncurses5", "libtinfo5", "find-error"])
def test_vivado_library_guard_rejects_system_workarounds(tmp_path: Path, workaround: str) -> None:
    if os.name == "nt":
        pytest.skip("POSIX shell guard")
    tools = tmp_path / "tools"
    tools.mkdir()
    for name, body in (
        (
            "find",
            'case "$WORKAROUND" in file) echo /usr/lib/libncurses.so.5 ;; find-error) exit 2 ;; esac',
        ),
        ("dpkg-query", 'if [ "$3" = "$WORKAROUND" ]; then printf installed; else exit 1; fi'),
    ):
        executable = tools / name
        executable.write_text("#!/bin/sh\n" + body + "\n")
        executable.chmod(0o755)
    result = subprocess.run(
        [
            "/bin/sh",
            "-c",
            "set -eu\n" + _no_system_vivado_libraries_script() + "\nprintf finished",
        ],
        env={
            **os.environ,
            "PATH": str(tools) + os.pathsep + os.environ["PATH"],
            "WORKAROUND": workaround,
        },
        capture_output=True,
        text=True,
        timeout=10,
        check=False,
    )
    assert (result.returncode == 0) is (workaround == "none"), result.stdout + result.stderr
    assert ("finished" in result.stdout) is (workaround == "none")


@pytest.mark.slow()
def test_vivado_wrapper_bundled_library_environment(tmp_path: Path) -> None:
    if os.environ.get("BOOLEY_VIVADO_WRAPPER_DOCKER") != "1":
        pytest.skip("BOOLEY_VIVADO_WRAPPER_DOCKER=1 enables packaged wrapper proof")
    docker = shutil.which("docker")
    assert docker is not None, "Docker is required for opted-in wrapper proof"
    release = _synthetic_vivado_release(tmp_path)
    root = release / "Vivado"
    libroot = f"{CONTAINER_TARGET}/Vivado/lib/lnx64.o"
    for version, newest in (("26.04", "100"), ("24.04", "24"), ("26.04", "24")):
        if newest == "24":
            highest = root / "lib" / "lnx64.o" / "Ubuntu" / "100"
            if highest.exists():
                highest.rmdir()
        (root / "bin" / "os-release").write_text(f"ID=ubuntu\nVERSION_ID={version}\n")
        result = _run_packaged_vivado_wrapper(docker, release)
        assert result.returncode == 0, result.stdout + result.stderr
        expected = (
            f"{libroot}/Ubuntu/{newest}:/caller/libs" if version == "26.04" else "/caller/libs"
        )
        assert result.stdout.splitlines() == [
            f"LIB={expected}",
            f"DOWNSTREAM={libroot}/Ubuntu{'/24' if version == '24.04' else ''}:{libroot}:{expected}",
            "PRELOAD=/lib/x86_64-linux-gnu/libudev.so.1",
            "ARG=argument with spaces",
        ]


def _run_packaged_vivado_wrapper(docker: str, release: Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [
            docker,
            "run",
            "--rm",
            "--network",
            "none",
            "--user",
            "0:0",
            "--mount",
            f"type=bind,source={release},target={CONTAINER_TARGET},readonly",
            "--mount",
            f"type=bind,source={Path(__file__).resolve().parents[2] / 'src/booley/data/docker/vivado-wrapper'},target=/usr/local/bin/vivado,readonly",
            "--env",
            "LD_LIBRARY_PATH=/caller/libs",
            "--entrypoint",
            "/bin/sh",
            _IMAGE,
            "/usr/local/bin/vivado",
            "argument with spaces",
        ],
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )


def _synthetic_vivado_release(tmp_path: Path) -> Path:
    release = tmp_path / "synthetic release"
    root = release / "Vivado"
    (root / "bin").mkdir(parents=True)
    for name in ("9", "024", "24", "100"):
        (root / "lib" / "lnx64.o" / "Ubuntu" / name).mkdir(parents=True)
    launcher = root / "bin" / "vivado"
    launcher.write_text(
        "#!/bin/sh\nroot=${0%/bin/*}\n"
        'selected=$("$root/bin/ldlibpath.sh" "$root/lib/lnx64.o")\n'
        'printf "LIB=%s\\nDOWNSTREAM=%s\\nPRELOAD=%s\\nARG=%s\\n" '
        '"${LD_LIBRARY_PATH-unset}" "$selected${LD_LIBRARY_PATH:+:$LD_LIBRARY_PATH}" "$LD_PRELOAD" "$1"\n'
    )
    launcher.chmod(0o755)
    selector = root / "bin" / "ldlibpath.sh"
    selector.write_text(
        '#!/bin/sh\n. "${0%/*}/os-release"\ncase "$VERSION_ID" in 24*) printf "%s/Ubuntu/24:%s\\n" "$1" "$1" ;; *) printf "%s/Ubuntu:%s\\n" "$1" "$1" ;; esac\n'
    )
    selector.chmod(0o755)
    return release
