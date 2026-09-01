"""Opt-in end-to-end proof for the packaged idle-reaper image."""

from __future__ import annotations

import hashlib
import json
import time
import uuid
from dataclasses import dataclass

import pytest
from tests.sidecar_image_helpers import (
    assert_ok,
    assert_python_version,
    candidate_image,
    docker,
)


def _exists(kind: str, name: str) -> bool:
    return docker(kind, "inspect", name).returncode == 0


@dataclass(frozen=True)
class _Topology:
    project_id: str
    session: str
    relay: str
    private: str
    outbound: str
    reaper: str

    @classmethod
    def create(cls) -> _Topology:
        unique = uuid.uuid4().hex[:12]
        project_id = hashlib.sha256(f"reaper-image-e2e-{unique}".encode()).hexdigest()
        short_id = project_id[:16]
        return cls(
            project_id=project_id,
            session=f"booley-reaper-e2e-session-{unique}",
            relay=f"booley-license-relay-{short_id}",
            private=f"booley-license-private-{short_id}",
            outbound=f"booley-license-outbound-{short_id}",
            reaper=f"booley-reaper-e2e-{unique}",
        )


def _start_relay(image: str, topology: _Topology) -> None:
    assert_ok(docker("network", "create", topology.private))
    assert_ok(docker("network", "create", topology.outbound))
    assert_ok(
        docker(
            "run",
            "-d",
            "--name",
            topology.relay,
            "--label",
            "booley.role=license-relay",
            "--label",
            f"booley.project-id={topology.project_id}",
            "--network",
            topology.outbound,
            "--entrypoint",
            "sleep",
            image,
            "60",
        )
    )
    assert_ok(docker("network", "connect", topology.private, topology.relay))


def _start_session(image: str, topology: _Topology) -> None:
    assert_ok(
        docker(
            "run",
            "-d",
            "--name",
            topology.session,
            "--label",
            "booley.role=interactive",
            "--label",
            f"booley.project-id={topology.project_id}",
            "--label",
            "booley.license-profile=e2e",
            "--network",
            topology.private,
            "--entrypoint",
            "sleep",
            image,
            "60",
        )
    )


def _start_reaper(image: str, topology: _Topology) -> None:
    assert_ok(
        docker(
            "run",
            "-d",
            "--name",
            topology.reaper,
            "--mount",
            "type=bind,src=/var/run/docker.sock,dst=/var/run/docker.sock",
            "-e",
            "BOOLEY_IDLE_TIMEOUT_SECONDS=1",
            "-e",
            "BOOLEY_REAP_INTERVAL_SECONDS=1",
            "-e",
            "BOOLEY_MAX_SESSIONS=4",
            image,
        )
    )


def _wait_for_cleanup(topology: _Topology) -> None:
    owned = (
        ("container", topology.session),
        ("container", topology.relay),
        ("network", topology.private),
        ("network", topology.outbound),
    )
    deadline = time.monotonic() + 15
    while time.monotonic() < deadline:
        if not any(_exists(kind, name) for kind, name in owned):
            return
        time.sleep(0.25)
    pytest.fail("candidate reaper did not remove its owned topology")


def _wait_for_log(container: str, expected: str) -> None:
    deadline = time.monotonic() + 3
    captured = ""
    while time.monotonic() < deadline:
        logs = docker("logs", container)
        assert_ok(logs)
        captured = logs.stdout + logs.stderr
        if expected in captured:
            return
        time.sleep(0.1)
    pytest.fail(f"candidate reaper did not log {expected!r}; captured logs:\n{captured}")


def _cleanup(topology: _Topology) -> None:
    docker("rm", "-f", topology.reaper, topology.session, topology.relay)
    docker("network", "rm", topology.private, topology.outbound)


@pytest.mark.slow()
def test_candidate_image_contract() -> None:
    image = candidate_image("BOOLEY_REAPER_IMAGE", "packaged reaper proof")
    assert_python_version(image)

    inspected = docker("image", "inspect", image)
    assert_ok(inspected)
    config = json.loads(inspected.stdout)[0]["Config"]
    assert config["Entrypoint"] == ["python3", "-u", "reaper.py"]


@pytest.mark.slow()
def test_candidate_entrypoint_reaps_owned_topology() -> None:
    """Run the image entrypoint against a real mounted Docker socket."""
    image = candidate_image("BOOLEY_REAPER_IMAGE", "packaged reaper proof")
    topology = _Topology.create()
    try:
        _start_relay(image, topology)
        _start_session(image, topology)
        _start_reaper(image, topology)
        _wait_for_cleanup(topology)
        _wait_for_log(topology.reaper, "reaped 1 session container")
    finally:
        _cleanup(topology)


@pytest.mark.slow()
def test_candidate_waits_between_passes_when_daemon_is_unreachable() -> None:
    image = candidate_image("BOOLEY_REAPER_IMAGE", "packaged reaper proof")
    unavailable = f"booley-reaper-unavailable-e2e-{uuid.uuid4().hex[:12]}"
    try:
        assert_ok(
            docker(
                "run",
                "-d",
                "--name",
                unavailable,
                "-e",
                "DOCKER_HOST=unix:///tmp/booley-missing-docker.sock",
                "-e",
                "BOOLEY_REAP_INTERVAL_SECONDS=2",
                image,
            )
        )
        time.sleep(4.5)
        running = docker("inspect", unavailable, "--format", "{{.State.Running}}")
        assert_ok(running)
        assert running.stdout.strip() == "true"
        failed_logs = docker("logs", unavailable)
        assert_ok(failed_logs)
        failures = (failed_logs.stdout + failed_logs.stderr).count("docker ps failed")
        assert 2 <= failures <= 4
    finally:
        docker("rm", "-f", unavailable)
