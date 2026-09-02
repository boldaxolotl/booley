"""Opt-in real-Docker lifecycle and hardening proof for the production relay."""

from __future__ import annotations

import hashlib
import ipaddress
import json
import os
import shutil
import socket
import subprocess
import time

import pytest

from booley.docker import reaper
from booley.eda.provisioning.licensing.flexnet_docker import (
    RELAY_IMAGE,
    RelayProfile,
    connect_session,
    provision_relay,
    remove_relay,
    validate_relay,
)


def _docker(*args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["docker", *args],
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )


def _assert_ok(result: subprocess.CompletedProcess[str]) -> None:
    assert result.returncode == 0, result.stdout + result.stderr


def _require_docker() -> None:
    if os.environ.get("BOOLEY_FLEXNET_DOCKER_TEST") != "1":
        pytest.skip("set BOOLEY_FLEXNET_DOCKER_TEST=1 for the production relay proof")
    if shutil.which("docker") is None:
        pytest.skip("docker is unavailable")
    if _docker("image", "inspect", RELAY_IMAGE).returncode != 0:
        pytest.skip(f"{RELAY_IMAGE} is not built")


def _wait_for_tcp_listeners(
    container: str,
    ports: tuple[int, ...],
    *,
    timeout: float = 10.0,
) -> None:
    checks = " && ".join(f"netstat -ltn | grep -Eq ':{port}[[:space:]]'" for port in ports)
    deadline = time.monotonic() + timeout
    captured = ""
    while time.monotonic() < deadline:
        result = _docker("exec", container, "sh", "-c", checks)
        captured = result.stdout + result.stderr
        if result.returncode == 0:
            return
        time.sleep(0.1)
    pytest.fail(f"synthetic upstream listeners did not become ready: {captured}")


def test_wait_for_tcp_listeners_retries_until_both_ports_are_bound(monkeypatch) -> None:
    calls: list[tuple[str, ...]] = []
    returncodes = iter((1, 0))

    def fake_docker(*args: str) -> subprocess.CompletedProcess[str]:
        calls.append(args)
        return subprocess.CompletedProcess(args, next(returncodes), "", "listeners not ready")

    monkeypatch.setitem(globals(), "_docker", fake_docker)

    _wait_for_tcp_listeners("upstream", (32110, 32111), timeout=1)

    assert len(calls) == 2
    assert all(call[:3] == ("exec", "upstream", "sh") for call in calls)
    assert all("32110" in call[-1] and "32111" in call[-1] for call in calls)


@pytest.mark.slow()
def test_production_relay_lifecycle_is_healthy_labeled_and_hardened() -> None:
    _require_docker()
    identity = "production-flexnet-relay-e2e"
    labels = ("booley.project-id=e2e-project", "booley.spec-digest=e2e-spec")
    profile = RelayProfile("10.20.30.40", "license-server-01", 32100, 32101)
    session = "booley-flexnet-session-e2e"
    resources = provision_relay(profile, identity, issuance_labels=labels)
    try:
        created = _docker(
            "container",
            "run",
            "-d",
            "--name",
            session,
            "--entrypoint",
            "sleep",
            RELAY_IMAGE,
            "60",
        )
        assert created.returncode == 0, created.stderr
        connect_session(resources, session)
        validate_relay(resources, session, profile, issuance_labels=labels)

        inspected = _docker("container", "inspect", resources.relay_container)
        assert inspected.returncode == 0, inspected.stderr
        state = json.loads(inspected.stdout)[0]
        host = state["HostConfig"]
        assert state["Config"]["User"] == "65532:65532"
        assert host["ReadonlyRootfs"] is True
        assert host["CapDrop"] == ["ALL"]
        assert "no-new-privileges" in host["SecurityOpt"]
        assert host["PortBindings"] == {}
        assert state["Mounts"] == []
        private = _docker("network", "inspect", resources.private_network)
        assert private.returncode == 0, private.stderr
        network = json.loads(private.stdout)[0]
        assert network["Internal"] is True
        assert network["Options"] == {"com.docker.network.bridge.gateway_mode_ipv4": "isolated"}
    finally:
        _docker("container", "rm", "-f", session)
        remove_relay(resources)


@pytest.mark.slow()
def test_production_relay_forwards_both_ports_without_direct_or_unrelated_access() -> None:  # noqa: PLR0915 - one atomic real-Docker topology
    """Exercise the actual image over two routes and negative Docker topology."""
    _require_docker()
    identity = "production-flexnet-forwarding-e2e"
    labels = ("booley.project-id=forward-project", "booley.spec-digest=forward-spec")
    upstream_network = "booley-flexnet-upstream-e2e"
    upstream = "booley-flexnet-upstream-e2e"
    session = "booley-flexnet-client-e2e"
    unrelated = "booley-flexnet-unrelated-e2e"
    created_resources = None
    first, second = 32110, 32111
    try:
        _assert_ok(_docker("network", "create", upstream_network))
        upstream_run = _docker(
            "run",
            "-d",
            "--name",
            upstream,
            "--network",
            upstream_network,
            "--entrypoint",
            "sh",
            RELAY_IMAGE,
            "-c",
            (
                # Consume the request before replying so the synthetic server
                # closes cleanly instead of resetting a socket with unread data.
                f"while true; do (nc -l -p {first} -e sh -c "
                "'cat >/dev/null; printf manager-reply') || true; done & "
                f"while true; do (nc -l -p {second} -e sh -c "
                "'cat >/dev/null; printf vendor-reply') || true; done; wait"
            ),
        )
        _assert_ok(upstream_run)
        ip = ""
        for _ in range(30):
            inspected = _docker(
                "inspect",
                upstream,
                "--format",
                f'{{{{(index .NetworkSettings.Networks "{upstream_network}").IPAddress}}}}',
            )
            ip = inspected.stdout.strip()
            if inspected.returncode == 0 and ip:
                break
            time.sleep(0.1)
        assert ip
        _wait_for_tcp_listeners(upstream, (first, second))

        profile = RelayProfile(ip, "license-server-01", first, second)
        created_resources = provision_relay(profile, identity, issuance_labels=labels)
        _assert_ok(
            _docker("network", "connect", upstream_network, created_resources.relay_container)
        )
        # The relay's production outbound network must be the only route named
        # in its policy. The extra synthetic upstream network stands in for the
        # external LAN because this test must not contact a real site service.

        _assert_ok(
            _docker(
                "run",
                "-d",
                "--name",
                session,
                "--network",
                created_resources.private_network,
                "--entrypoint",
                "sleep",
                RELAY_IMAGE,
                "60",
            )
        )
        # validate_relay intentionally rejects the synthetic third upstream
        # network, so the exact topology contract remains covered by the
        # lifecycle test above; this case covers actual bytes and reachability.
        for port, reply in ((first, "manager-reply"), (second, "vendor-reply")):
            flowed = _docker(
                "exec",
                session,
                "sh",
                "-c",
                f"printf request-{port} | nc -w 3 booley-license-xilinx {port}",
            )
            assert flowed.returncode == 0, flowed.stderr
            assert flowed.stdout == reply

        # Session has no direct path to the synthetic license server or public
        # internet; the relay's private DNS alias is the only successful route.
        direct = _docker(
            "exec",
            session,
            "sh",
            "-c",
            f"! nc -z -w 1 {ip} {first} && ! nc -z -w 1 1.1.1.1 443",
        )
        _assert_ok(direct)

        # ``--internal`` alone leaves Docker's host bridge gateway reachable.
        # Bind a real host listener and prove the isolated gateway mode removes
        # that route from the Session/client network.
        private = _docker("network", "inspect", created_resources.private_network)
        _assert_ok(private)
        network = json.loads(private.stdout)[0]
        subnet = ipaddress.ip_network(network["IPAM"]["Config"][0]["Subnet"])
        former_gateway = str(next(subnet.hosts()))
        listener = socket.socket()
        listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        listener.bind(("0.0.0.0", 0))
        listener.listen()
        try:
            host_probe = _docker(
                "exec",
                session,
                "sh",
                "-c",
                f"! nc -z -w 1 {former_gateway} {listener.getsockname()[1]}",
            )
            _assert_ok(host_probe)
        finally:
            listener.close()

        _assert_ok(
            _docker(
                "run",
                "-d",
                "--name",
                unrelated,
                "--entrypoint",
                "sleep",
                RELAY_IMAGE,
                "60",
            )
        )
        isolated = _docker(
            "exec",
            unrelated,
            "sh",
            "-c",
            f"! nc -z -w 1 {ip} {first} && ! nc -z -w 1 {created_resources.relay_container} {first}",
        )
        _assert_ok(isolated)

        logs = _docker("logs", created_resources.relay_container)
        assert "request-" not in logs.stdout + logs.stderr
        assert "listener_port=" in logs.stdout + logs.stderr
        assert ip not in logs.stdout + logs.stderr

        _assert_ok(_docker("stop", created_resources.relay_container))
        failed_closed = _docker(
            "exec",
            session,
            "sh",
            "-c",
            f"! nc -z -w 1 booley-license-xilinx {first}",
        )
        _assert_ok(failed_closed)
    finally:
        _docker("rm", "-f", session, unrelated)
        if created_resources is not None:
            _docker("network", "disconnect", upstream_network, created_resources.relay_container)
            remove_relay(created_resources)
        _docker("rm", "-f", upstream)
        _docker("network", "rm", upstream_network)


@pytest.mark.slow()
def test_idle_reaper_removes_licensed_vscode_container_and_owned_topology() -> None:
    """Exercise the real Docker cleanup path for a VS Code-shaped session."""
    _require_docker()
    identity = "production-flexnet-reaper-e2e"
    project_id = hashlib.sha256(identity.encode()).hexdigest()
    labels = (
        f"booley.project-id={project_id}",
        "booley.spec-digest=reaper-e2e",
        "booley.license-profile=site-e2e",
    )
    profile = RelayProfile("10.20.30.40", "license-server-01", 32120, 32121)
    session = "booley-vscode-licensed-reaper-e2e"
    resources = provision_relay(profile, identity, issuance_labels=labels)
    try:
        created = _docker(
            "container",
            "run",
            "-d",
            "--name",
            session,
            "--label",
            reaper.INTERACTIVE_LABEL,
            "--label",
            f"{reaper.PROJECT_LABEL}={project_id}",
            "--label",
            f"{reaper.LICENSE_LABEL}=site-e2e",
            "--entrypoint",
            "sleep",
            RELAY_IMAGE,
            "60",
        )
        _assert_ok(created)
        connect_session(resources, session)
        container_id = created.stdout.strip()

        def isolated_run(args: list[str], *, timeout: int = 30):
            del timeout
            if args[0] == "ps":
                return subprocess.CompletedProcess(args, 0, f"{container_id}\t{session}\n", "")
            return _docker(*args)

        stopped = reaper.reap_once(
            now=time.time() + 120,
            idle_timeout=1,
            max_sessions=4,
            run=isolated_run,
        )
        assert stopped == [container_id]
        for kind, name in (
            ("container", session),
            ("container", resources.relay_container),
            ("network", resources.private_network),
            ("network", resources.outbound_network),
        ):
            assert _docker(kind, "inspect", name).returncode != 0
    finally:
        _docker("container", "rm", "-f", session, resources.relay_container)
        _docker("network", "rm", resources.private_network, resources.outbound_network)
