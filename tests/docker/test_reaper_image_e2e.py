"""Opt-in end-to-end proof for the packaged idle-reaper image."""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import subprocess
import time
import uuid

import pytest


def _docker(*args: str, timeout: int = 60) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["docker", *args],
        capture_output=True,
        text=True,
        timeout=timeout,
        check=False,
    )


def _assert_ok(result: subprocess.CompletedProcess[str]) -> None:
    assert result.returncode == 0, result.stdout + result.stderr


def _candidate_image() -> str:
    image = os.environ.get("BOOLEY_REAPER_IMAGE")
    if image is None:
        pytest.skip("set BOOLEY_REAPER_IMAGE to run the packaged reaper proof")
    if shutil.which("docker") is None:
        pytest.skip("docker is unavailable")
    if _docker("image", "inspect", image).returncode != 0:
        pytest.skip(f"{image} is not built")
    return image


def _exists(kind: str, name: str) -> bool:
    return _docker(kind, "inspect", name).returncode == 0


@pytest.mark.slow()
def test_candidate_image_contract() -> None:
    image = _candidate_image()
    version = _docker("run", "--rm", "--entrypoint", "python3", image, "--version")
    _assert_ok(version)
    assert (version.stdout + version.stderr).strip() == "Python 3.14.7"

    inspected = _docker("image", "inspect", image)
    _assert_ok(inspected)
    config = json.loads(inspected.stdout)[0]["Config"]
    assert config["Entrypoint"] == ["python3", "-u", "reaper.py"]


@pytest.mark.slow()
def test_candidate_entrypoint_reaps_owned_topology() -> None:
    """Run the image entrypoint against a real mounted Docker socket."""
    image = _candidate_image()
    unique = uuid.uuid4().hex[:12]
    project_id = hashlib.sha256(f"reaper-image-e2e-{unique}".encode()).hexdigest()
    short_id = project_id[:16]
    session = f"booley-reaper-e2e-session-{unique}"
    relay = f"booley-license-relay-{short_id}"
    private = f"booley-license-private-{short_id}"
    outbound = f"booley-license-outbound-{short_id}"
    reaper = f"booley-reaper-e2e-{unique}"

    try:
        _assert_ok(_docker("network", "create", private))
        _assert_ok(_docker("network", "create", outbound))
        _assert_ok(
            _docker(
                "run",
                "-d",
                "--name",
                relay,
                "--label",
                "booley.role=license-relay",
                "--label",
                f"booley.project-id={project_id}",
                "--network",
                outbound,
                "--entrypoint",
                "sleep",
                image,
                "60",
            )
        )
        _assert_ok(_docker("network", "connect", private, relay))
        _assert_ok(
            _docker(
                "run",
                "-d",
                "--name",
                session,
                "--label",
                "booley.role=interactive",
                "--label",
                f"booley.project-id={project_id}",
                "--label",
                "booley.license-profile=e2e",
                "--network",
                private,
                "--entrypoint",
                "sleep",
                image,
                "60",
            )
        )
        _assert_ok(
            _docker(
                "run",
                "-d",
                "--name",
                reaper,
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

        deadline = time.monotonic() + 15
        while time.monotonic() < deadline:
            if not any(
                _exists(kind, name)
                for kind, name in (
                    ("container", session),
                    ("container", relay),
                    ("network", private),
                    ("network", outbound),
                )
            ):
                break
            time.sleep(0.25)
        else:
            pytest.fail("candidate reaper did not remove its owned topology")

        logs = _docker("logs", reaper)
        _assert_ok(logs)
        assert "reaped 1 session container" in logs.stdout + logs.stderr
    finally:
        _docker("rm", "-f", reaper, session, relay)
        _docker("network", "rm", private, outbound)


@pytest.mark.slow()
def test_candidate_waits_between_passes_when_daemon_is_unreachable() -> None:
    image = _candidate_image()
    unavailable = f"booley-reaper-unavailable-e2e-{uuid.uuid4().hex[:12]}"
    try:
        _assert_ok(
            _docker(
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
        running = _docker("inspect", unavailable, "--format", "{{.State.Running}}")
        _assert_ok(running)
        assert running.stdout.strip() == "true"
        failed_logs = _docker("logs", unavailable)
        _assert_ok(failed_logs)
        failures = (failed_logs.stdout + failed_logs.stderr).count("docker ps failed")
        assert 2 <= failures <= 4
    finally:
        _docker("rm", "-f", unavailable)
