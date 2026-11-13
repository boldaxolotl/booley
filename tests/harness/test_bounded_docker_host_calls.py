"""Every timed, captured Docker CLI call in host-side code outside the runtime is bounded.

``subprocess.run(..., timeout=...)`` kills only the direct child and then waits for the
output pipes with no limit (on Windows a surviving descendant holds them), so a hung
Docker CLI could hang Doctor, init and similar commands past their timeout.
``platform_paths.run_captured_bounded`` kills the whole tree and abandons held pipes.
These tests pin that each audited call site uses it (directly, or through the runner
its composer injects where the package cannot import the runtime), keeps its
caller-visible results, and that a hung self-test really returns promptly with no
descendant left running.
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
import sys
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from types import ModuleType, SimpleNamespace
from typing import Any
from unittest import mock

import pytest

from booley.audit import eda_environment
from booley.audit.diagnostic_results import Findings
from booley.dev_support import agent_readiness
from booley.eda.provisioning.licensing.flexnet_docker import resources_for_session
from booley.flows import runtime_reality
from booley.flows.fpga import vivado_startup
from booley.harness import bootstrap, doctor, sandbox, sandbox_stop_warning
from booley.harness.setup import docker_image, readiness
from booley.mcp import server_inspection
from booley.runtime import interactive_docker, session_runtime
from booley.runtime.session_admission import Sandbox
from tests.pipe_holding_tree import heartbeat_tree, parent_argv, wait_until_heartbeat_stops

_IMAGE_ID = "sha256:" + "a" * 64
_OUTCOMES = ("ok", "nonzero", "missing", "timeout")
_MARKER_PATTERN = re.compile(r"BOOLEY_VIVADO_STARTUP_[0-9a-f]{32}")
_MCP_OK_PAYLOAD = (
    '{"tools": ["bwave", "sim", "lint", "synth", "reviewer"], "errors": [], "logs_dir_ok": true}'
)


@dataclass(frozen=True)
class _Site:
    """One audited call site and what its caller shows for each command outcome."""

    name: str
    #: Module whose ``run_captured_bounded`` the site looks up.
    module: ModuleType
    #: Leading argv words of the command under test.
    target: list[str]
    #: The timeout the site has always passed.
    timeout: float
    #: Calls the command under test (given the tmp dir) and returns what the caller shows.
    drive: Callable[[Path], Any]
    #: Stdout the command prints on success (a function of its argv when it must echo one).
    ok_stdout: str | Callable[[list[str]], str]
    #: Observation per outcome: ``returned:<value>`` or the raised exception's type name.
    expected: dict[str, str]
    #: Stdout of earlier commands in the same call (the command under test is the last).
    prior_stdout: str = ""
    #: Keyword arguments the bounded call must carry (``stdin``, ``text``).
    kwargs: dict[str, Any] = field(default_factory=dict)


def _same(shown: str) -> dict[str, str]:
    """Expected observations when every failure outcome surfaces identically."""
    return {"nonzero": shown, "missing": shown, "timeout": shown}


class _Events:
    """Check callbacks (pass, warn, skip, fail) that record what they were told."""

    def __init__(self) -> None:
        self.log: list[tuple[str, str]] = []

    def _record(self, kind: str) -> Callable[..., None]:
        def record(message: object, *_rest: object) -> None:
            self.log.append((kind, str(getattr(message, "message", message))))

        return record

    @property
    def pass_(self) -> Callable[..., None]:
        return self._record("pass")

    @property
    def warn(self) -> Callable[..., None]:
        return self._record("warn")

    @property
    def skip(self) -> Callable[..., None]:
        return self._record("skip")

    @property
    def fail(self) -> Callable[..., None]:
        return self._record("fail")


def _bootstrap_state(name: str, flag: str) -> str:
    return bootstrap._tool_finding(name, flag).state.name


def _sandbox_probe_returncode() -> int:
    box = Sandbox("cid", "n", "/p", None, datetime.now(UTC), None, True, False)
    return sandbox_stop_warning._run_probe(box).returncode


def _build_cache_messages() -> list[str]:
    messages: list[str] = []
    with mock.patch.object(docker_image, "info", messages.append):
        docker_image._report_build_cache()
    return messages


def _marker_stdout(argv: list[str]) -> str:
    found = _MARKER_PATTERN.search(" ".join(argv))
    return (found.group(0) if found else "") + "\n"


def _first_finding(report: Any) -> tuple[str, str]:
    finding = report.findings[0]
    return finding.severity.value, finding.message.partition(" (")[0].partition(": ")[0]


def _mcp_request(root: Path) -> server_inspection.McpServerInspectionRequest:
    return server_inspection.McpServerInspectionRequest(
        project_root=root,
        booley_dir=root,
        docker_exe="docker",
        image="img",
        booley_toml={},
        fpga_covered=False,
    )


def _runtime_request(root: Path, flow: str) -> runtime_reality.FlowRuntimeInspectionRequest:
    return runtime_reality.FlowRuntimeInspectionRequest(
        flow_name=flow,
        targets=(),
        project_root=root,
        runtime_available=True,
        inside_sandbox=False,
        sandbox_command=lambda inner: ["docker", "exec", "c", *inner],
    )


def _session_binary_finding(root: Path) -> tuple[str, str]:
    findings = Findings()
    runtime_reality._check_session_binary(_runtime_request(root, "sim"), "verilator", findings)
    return _first_finding(findings.report())


def _container_uid_events(root: Path) -> list[tuple[str, str]]:
    events = _Events()
    project = SimpleNamespace(booley_dir=root)
    doctor._check_container_uid(
        project,  # type: ignore[arg-type]
        "docker",
        "img",
        events.pass_,
        events.warn,
        events.fail,
    )
    return events.log


def _runtime_marker_events() -> list[tuple[str, str]]:
    events = _Events()
    doctor._check_image_bakes_runtime_marker(
        "docker", "img", events.pass_, events.warn, events.skip
    )
    return events.log


def _container_check_events() -> list[tuple[str, str]]:
    events = _Events()
    check = doctor._container_command_check("docker", "img", False, events.pass_, events.fail)
    check("tool", ["tool"], "fix")
    return events.log


def _riscv_first_event(root: Path) -> tuple[str, str]:
    events = _Events()
    doctor._check_riscv_toolchain("docker", "img", events.pass_, events.skip, events.fail)
    return events.log[0]


def _selftest_result(root: Path) -> tuple[int | None, list[tuple[str, str]]]:
    events = _Events()
    result = doctor._execute_selftest(
        SimpleNamespace(project_root=root),  # type: ignore[arg-type]
        ["docker", "exec", "c", "selftest"],
        {"PATH": os.environ.get("PATH", "")},
        7,
        "label",
        "sim",
        _fail=events.fail,
    )
    return (None if result is None else result.returncode), events.log


def _flow_check_result(root: Path) -> tuple[int | None, list[str]]:
    findings = Findings()
    result = doctor._run_flow_check_subprocess(
        SimpleNamespace(project_root=root, booley_dir=root),  # type: ignore[arg-type]
        ["docker", "exec", "c", "flow-check"],
        timeout_s=7,
        label="L",
        report_dir=root,
        findings=findings,
    )
    messages = [finding.message for finding in findings.report().findings]
    return (None if result is None else result.returncode), messages


def _sites() -> list[_Site]:
    riscv_command = list(eda_environment.riscv_probe_specs()[0].command)
    riscv_description = eda_environment.riscv_probe_specs()[0].description
    relay = resources_for_session("project")
    sites = [
        _Site(
            "bootstrap docker --version",
            bootstrap,
            ["docker", "--version"],
            5,
            lambda _root: _bootstrap_state("docker", "--version"),
            "Docker version 27\n",
            {"ok": "returned:'CURRENT'"} | _same("returned:'ERROR'"),
        ),
        _Site(
            "bootstrap docker info",
            bootstrap,
            ["docker", "info"],
            10,
            lambda _root: bootstrap._docker_daemon_error(),
            "",
            {
                "ok": "returned:None",
                "nonzero": "returned:'Docker daemon is not running or accessible: boom'",
                "missing": "returned:'cannot contact Docker daemon: docker'",
                "timeout": "returned:'Docker daemon did not respond within 10 seconds'",
            },
        ),
        _Site(
            "sandbox verify docker info",
            sandbox,
            ["docker", "info"],
            15,
            lambda _root: sandbox.DockerSandboxConfig().verify(),
            "",
            {
                "ok": "returned:None",
                "nonzero": "returned:'Docker daemon not running (docker info failed) — boom'",
                "missing": "returned:'docker not found on PATH'",
                "timeout": "returned:'Docker daemon not responding (docker info timed out)'",
            },
        ),
        _Site(
            "sandbox verify image inspect",
            sandbox,
            ["docker", "image", "inspect"],
            15,
            lambda _root: sandbox.DockerSandboxConfig().verify(),
            "",
            {
                "ok": "returned:None",
                "nonzero": (
                    "returned:\"Docker image 'booley-sandbox' not found. "
                    'Build it with: booley init (or docker build)"'
                ),
                "missing": "returned:\"Could not verify Docker image 'booley-sandbox'\"",
                "timeout": "returned:\"Could not verify Docker image 'booley-sandbox'\"",
            },
        ),
        _Site(
            "sandbox_stop_warning probe",
            sandbox_stop_warning,
            ["docker", "exec"],
            15,
            lambda _root: _sandbox_probe_returncode(),
            "",
            {"ok": "returned:0", "nonzero": "returned:1"}
            | {"missing": "FileNotFoundError", "timeout": "TimeoutExpired"},
            kwargs={"stdin": subprocess.DEVNULL},
        ),
        _Site(
            "docker_image image label",
            docker_image,
            ["docker", "image", "inspect", "-f"],
            15,
            lambda _root: docker_image._image_label("img", "label"),
            "v1\n",
            {"ok": "returned:'v1'"} | _same("returned:None"),
        ),
        _Site(
            "docker_image tag pulled image",
            docker_image,
            ["docker", "tag"],
            30,
            lambda _root: docker_image._tag_pulled_image("src", "dst"),
            "",
            {"ok": "returned:True"} | _same("returned:False"),
            kwargs={"text": False},
        ),
        _Site(
            "docker_image build cache",
            docker_image,
            ["docker", "system", "df"],
            15,
            lambda _root: _build_cache_messages(),
            "Build Cache\t1.5GB\t1GB\n",
            {"ok": "returned:['docker build cache: 1.5GB (1GB reclaimable)']"}
            | _same("returned:[]"),
        ),
        _Site(
            "doctor sandbox identity",
            doctor,
            ["docker", "exec"],
            15,
            lambda root: doctor._observe_sandbox_identity(root, "cid"),
            f"{_IMAGE_ID}\n1.2.3\n",
            {"ok": f"returned:('{_IMAGE_ID}', '1.2.3')"} | _same("returned:(None, None)"),
            kwargs={"stdin": subprocess.DEVNULL},
        ),
        _Site(
            "doctor image env value",
            doctor,
            ["docker", "image", "inspect", "img"],
            10,
            lambda _root: doctor._image_env_value("docker", "img", "KEY"),
            '["KEY=value"]',
            {"ok": "returned:'value'"} | _same("returned:None"),
        ),
        _Site(
            "doctor runtime marker",
            doctor,
            ["docker", "image", "inspect", "img"],
            10,
            lambda _root: _runtime_marker_events(),
            '["BOOLEY_CONTAINER=1"]',
            {
                "ok": "returned:[('pass', 'img bakes BOOLEY_CONTAINER=1 (runtime marker)')]",
                "nonzero": (
                    "returned:[('skip', 'sandbox image runtime marker check skipped - "
                    "image img not present')]"
                ),
                "missing": (
                    "returned:[('skip', 'sandbox image runtime marker check skipped - "
                    "image inspect failed')]"
                ),
                "timeout": (
                    "returned:[('skip', 'sandbox image runtime marker check skipped - "
                    "image inspect failed')]"
                ),
            },
        ),
        _Site(
            "doctor image created epoch",
            doctor,
            ["docker", "image", "inspect", "--format"],
            10,
            lambda _root: doctor._image_created_epoch("docker", "img"),
            "2024-01-01T00:00:00Z\n",
            {"ok": "returned:1704067200.0"} | _same("returned:None"),
        ),
        _Site(
            "doctor container uid",
            doctor,
            ["docker", "run", "--rm", "img", "id", "-u"],
            30,
            _container_uid_events,
            f"{os.getuid() if hasattr(os, 'getuid') else 0}\n",
            {
                "ok": (
                    f"returned:[('pass', 'container agent uid "
                    f"{os.getuid() if hasattr(os, 'getuid') else 0} matches .booley owner')]"
                ),
                "nonzero": "returned:[('warn', 'could not probe container uid')]",
                "missing": "returned:[('warn', 'could not probe container uid: docker')]",
                "timeout": (
                    "returned:[('warn', \"could not probe container uid: Command "
                    "'['docker', 'run', '--rm', 'img', 'id', '-u']' timed out after 30 seconds\")]"
                ),
            },
        ),
        _Site(
            "doctor container command check",
            doctor,
            ["docker", "run", "--rm", "img", "tool"],
            30,
            lambda _root: _container_check_events(),
            "",
            {
                "ok": "returned:[('pass', 'tool')]",
                "nonzero": "returned:[('fail', 'tool')]",
                "missing": "returned:[('fail', 'tool (timeout/error)')]",
                "timeout": "returned:[('fail', 'tool (timeout/error)')]",
            },
        ),
        _Site(
            "doctor self-test",
            doctor,
            ["docker", "exec", "c", "selftest"],
            7,
            _selftest_result,
            "",
            {
                "ok": "returned:(0, [])",
                "nonzero": "returned:(1, [])",
                "missing": "returned:(None, [('fail', 'label failed to start: docker')])",
                "timeout": "returned:(None, [('fail', 'label timed out after 7s')])",
            },
        ),
        _Site(
            "doctor flow check",
            doctor,
            ["docker", "exec", "c", "flow-check"],
            7,
            _flow_check_result,
            "",
            {
                "ok": "returned:(0, [])",
                "nonzero": "returned:(1, [])",
                "missing": "returned:(None, ['L failed to start: docker'])",
                "timeout": "returned:(None, ['L timed out after 7s'])",
            },
        ),
        _Site(
            "doctor riscv toolchain probe",
            doctor,
            ["docker", "run", "--rm", "img", riscv_command[0]],
            30,
            _riscv_first_event,
            "",
            {
                "ok": f"returned:('pass', {riscv_description!r})",
                "nonzero": f"returned:('fail', {riscv_description!r})",
                "missing": f"returned:('fail', {riscv_description + ' (timeout/error)'!r})",
                "timeout": f"returned:('fail', {riscv_description + ' (timeout/error)'!r})",
            },
            prior_stdout='["BOOLEY_SANDBOX_FLAVOR=riscv"]',
        ),
        _Site(
            "readiness sandbox image git",
            readiness,
            ["docker", "run", "--rm", "img", "git", "--version"],
            30,
            lambda _root: readiness._git_version_at(
                "docker", "run", "--rm", "img", "git", "--version"
            ),
            "git version 2.40.1\n",
            {"ok": "returned:(2, 40, 1)"} | _same("returned:None"),
        ),
        _Site(
            "mcp server inspection",
            server_inspection,
            ["docker", "run", "--init", "--rm"],
            60,
            lambda root: _first_finding(server_inspection.inspect_mcp_server(_mcp_request(root))),
            _MCP_OK_PAYLOAD,
            {
                "ok": "returned:('pass', 'MCP server exposes 5 MCP tool(s)')",
                "nonzero": "returned:('fail', 'MCP server probe failed with exit 1')",
                "missing": "returned:('fail', 'MCP server probe failed to start')",
                "timeout": "returned:('fail', 'MCP server probe failed to start')",
            },
        ),
        _Site(
            "vivado startup",
            vivado_startup,
            ["docker", "exec", "c", "sh", "-c"],
            30,
            lambda root: _first_finding(
                vivado_startup.inspect_vivado_startup(_runtime_request(root, "fpga"))
            ),
            _marker_stdout,
            {
                "ok": "returned:('pass', 'fpga')",
                "nonzero": "returned:('fail', 'fpga')",
                "missing": "returned:('fail', 'fpga')",
                "timeout": "returned:('fail', 'fpga')",
            },
        ),
        _Site(
            "runtime reality session binary",
            runtime_reality,
            ["docker", "exec", "c", "sh", "-c"],
            30,
            _session_binary_finding,
            "/usr/bin/verilator\n",
            {
                "ok": "returned:('pass', 'sim')",
                "nonzero": "returned:('fail', 'sim')",
                "missing": "returned:('fail', 'sim')",
                "timeout": "returned:('fail', 'sim')",
            },
        ),
        _Site(
            "agent_readiness docker daemon",
            agent_readiness,
            ["docker", "info"],
            10,
            lambda _root: _readiness_docker_observation(),
            "",
            {
                "ok": "returned:('docker.daemon', 'ready')",
                "nonzero": "returned:('docker.daemon', 'blocked')",
                "missing": "returned:('docker.daemon', 'blocked')",
                "timeout": "returned:('docker.daemon', 'blocked')",
            },
            kwargs={"cwd": Path.cwd()},
        ),
        _Site(
            "flexnet relay container rm",
            interactive_docker,
            ["docker", "container", "rm"],
            30,
            lambda _root: _remove_relay_for_site(relay),
            "",
            {"ok": "returned:None"} | _same("SessionError"),
        ),
    ]
    return sites


def _readiness_docker_observation() -> tuple[str, str]:
    check = agent_readiness._docker_check(True)
    return check.id, check.status.value


def _remove_relay_for_site(relay: Any) -> None:
    session_runtime._remove_license_relay(relay)


def _check_site(
    site: _Site, outcome: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> list[str]:
    """Drive *site* with the scripted *outcome*; return what differs from the contract."""
    problems: list[str] = []
    calls: list[tuple[list[str], float, dict[str, Any]]] = []

    def bounded(
        argv: list[str], *, timeout: float, **kwargs: Any
    ) -> subprocess.CompletedProcess[str]:
        calls.append((list(argv), timeout, kwargs))
        if list(argv[: len(site.target)]) != site.target:
            return subprocess.CompletedProcess(argv, 0, site.prior_stdout, "")
        if outcome == "missing":
            raise FileNotFoundError(argv[0])
        if outcome == "timeout":
            raise subprocess.TimeoutExpired(argv, timeout)
        if outcome == "nonzero":
            return subprocess.CompletedProcess(argv, 1, "", "boom")
        stdout = site.ok_stdout(argv) if callable(site.ok_stdout) else site.ok_stdout
        return subprocess.CompletedProcess(argv, 0, stdout, "")

    def unbounded(*args: object, **kwargs: object) -> object:
        problems.append(f"{site.name}: unbounded subprocess.run used")
        raise AssertionError("unbounded subprocess.run used")

    with monkeypatch.context() as patch:
        patch.setattr(site.module, "run_captured_bounded", bounded, raising=False)
        patch.setattr(subprocess, "run", unbounded)
        patch.setattr(shutil, "which", lambda name: name)
        shown = _observe(lambda: site.drive(tmp_path))
    if shown != site.expected[outcome]:
        problems.append(
            f"{site.name} [{outcome}]: shows {shown!r}, expected {site.expected[outcome]!r}"
        )
    made = [call for call in calls if call[0][: len(site.target)] == site.target]
    if len(made) != 1 or made[0][1] != site.timeout:
        problems.append(
            f"{site.name} [{outcome}]: bounded calls {[c[:2] for c in calls]!r}, expected one "
            f"{site.target!r} with timeout {site.timeout}"
        )
    elif any(made[0][2].get(key) != value for key, value in site.kwargs.items()):
        problems.append(
            f"{site.name} [{outcome}]: call kwargs {made[0][2]!r} lack {site.kwargs!r}"
        )
    return problems


def _observe(drive: Callable[[], Any]) -> str:
    """What the caller shows: ``returned:<value>`` or the raised exception's type name."""
    try:
        value = drive()
    except Exception as exc:  # noqa: BLE001 - the type name is the observation
        return type(exc).__name__
    return f"returned:{value!r}"


def test_every_timed_docker_call_uses_the_bounded_helper(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Each audited call goes through the bounded helper and keeps its caller-visible results."""
    sites = _sites()
    if not (sys.platform.startswith("linux") and hasattr(os, "getuid")):
        # ``_check_container_uid`` returns before any Docker call outside Linux hosts.
        sites = [site for site in sites if site.name != "doctor container uid"]
    failures = [
        problem
        for site in sites
        for outcome in _OUTCOMES
        for problem in _check_site(site, outcome, tmp_path, monkeypatch)
    ]
    assert not failures, "\n".join(failures)


def test_hung_doctor_selftest_kills_the_tree_and_returns_promptly(tmp_path: Path) -> None:
    """A self-test whose command hangs ends soon as a timeout and leaves no descendant running."""
    events = _Events()
    with heartbeat_tree(tmp_path) as paths:
        cmd = parent_argv(tmp_path, sys.executable, paths, "live")
        # The helper's documented worst case after the 2 s timeout: +12 s POSIX, +32 s Windows.
        limit = 40 if os.name == "nt" else 25
        start = time.monotonic()
        result = doctor._execute_selftest(
            SimpleNamespace(project_root=tmp_path),  # type: ignore[arg-type]
            cmd,
            dict(os.environ),
            2,
            "selftest",
            "sim",
            _fail=events.fail,
        )
        elapsed = time.monotonic() - start
        assert result is None
        assert [message for kind, message in events.log if kind == "fail"] == [
            "selftest timed out after 2s"
        ]
        assert elapsed < limit, f"self-test hung {elapsed:.1f}s on a pipe held by a grandchild"
        assert paths["heartbeat"].exists(), "grandchild never started"
        assert wait_until_heartbeat_stops(paths["heartbeat"], window=5), (
            "grandchild still running after the timeout"
        )
