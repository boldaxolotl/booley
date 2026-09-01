"""Shared helpers for opt-in sidecar image end-to-end tests."""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
import time
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path

import pytest

from booley.core.boundary import BoundaryError, require_dict, require_list, require_str

DIND_IMAGE = (
    "docker:29.7.2-dind@sha256:3ef33f2e220b79ed3ef3b99d81746f06f306cd6340e2cb7331d17ae996e74cb6"
)
_DOCKER_SOCKET = "unix:///var/run/docker.sock"
_NAME_PREFIX_RE = re.compile(r"^[a-zA-Z0-9][a-zA-Z0-9_.-]*$")


@dataclass(frozen=True)
class DockerClient:
    """Run Docker CLI commands through one fixed daemon route."""

    command_prefix: tuple[str, ...] = ()

    def run(self, *args: str, timeout: int = 60) -> subprocess.CompletedProcess[str]:
        """Run one bounded Docker command without raising."""
        return subprocess.run(
            ["docker", *self.command_prefix, *args],
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
        )


HOST_DOCKER = DockerClient()


@dataclass(frozen=True)
class IsolatedDockerDaemon:
    """A candidate image loaded into a disposable Docker daemon."""

    client: DockerClient
    image_id: str


def docker(*args: str, timeout: int = 60) -> subprocess.CompletedProcess[str]:
    """Run Docker without raising so tests can report stdout and stderr."""
    return HOST_DOCKER.run(*args, timeout=timeout)


def assert_ok(result: subprocess.CompletedProcess[str]) -> None:
    """Assert that a Docker command completed successfully."""
    assert result.returncode == 0, result.stdout + result.stderr


def _candidate_platform(image: str) -> str:
    result = HOST_DOCKER.run(
        "image",
        "inspect",
        image,
        "--format",
        "{{.Id}}\t{{.Os}}/{{.Architecture}}",
    )
    assert_ok(result)
    image_id, separator, platform = result.stdout.strip().partition("\t")
    assert image_id.startswith("sha256:"), f"invalid candidate image ID: {image_id!r}"
    assert separator and "/" in platform, f"invalid candidate platform: {platform!r}"
    return platform


def _start_dind(name: str) -> None:
    result = HOST_DOCKER.run(
        "run",
        "-d",
        "--name",
        name,
        "--label",
        "booley.test-role=reaper-e2e-dind",
        "--privileged",
        "--network",
        "none",
        "--tmpfs",
        "/var/lib/docker:exec",
        "-e",
        "DOCKER_TLS_CERTDIR=",
        DIND_IMAGE,
        # Bypass the image's default argument rewriting, which adds a TCP listener.
        "dockerd",
        f"--host={_DOCKER_SOCKET}",
        "--storage-driver=vfs",
    )
    assert_ok(result)


def _wait_for_dind(client: DockerClient, *, timeout: float = 30.0) -> str:
    deadline = time.monotonic() + timeout
    captured = ""
    while time.monotonic() < deadline:
        result = client.run("info", "--format", "{{.ID}}", timeout=5)
        captured = result.stdout + result.stderr
        daemon_id = result.stdout.strip()
        if result.returncode == 0 and daemon_id:
            return daemon_id
        time.sleep(0.25)
    raise AssertionError(f"isolated Docker daemon did not become ready: {captured}")


def _assert_isolated_dind(name: str, inner_daemon_id: str) -> None:
    inspected = HOST_DOCKER.run("inspect", name)
    assert_ok(inspected)
    field = f"Docker inspect response for {name!r}"
    try:
        records = require_list(json.loads(inspected.stdout), field=field)
        if len(records) != 1:
            raise BoundaryError(f"{field} must contain exactly one container")
        config = require_dict(records[0], field=f"{field}[0]")
        host_config = require_dict(config.get("HostConfig"), field=f"{field}[0].HostConfig")
        network_mode = require_str(host_config, "NetworkMode")
        mounts = require_list(config.get("Mounts", []), field=f"{field}[0].Mounts")
        mount_records = [
            require_dict(mount, field=f"{field}[0].Mounts[{index}]")
            for index, mount in enumerate(mounts)
        ]
        mount_paths = [
            (require_str(mount, "Source"), require_str(mount, "Destination"))
            for mount in mount_records
        ]
        port_bindings = host_config.get("PortBindings")
        if port_bindings is not None:
            require_dict(port_bindings, field=f"{field}[0].HostConfig.PortBindings")
    except (json.JSONDecodeError, BoundaryError) as exc:
        raise AssertionError(f"invalid {field}: {exc}") from exc
    assert network_mode == "none", "DIND must have no outer network"
    assert not port_bindings, "DIND must publish no ports"
    socket_paths = {"/var/run/docker.sock", "/run/docker.sock"}
    assert not any(
        source in socket_paths or destination in socket_paths
        for source, destination in mount_paths
    ), "DIND must not mount an outer Docker socket"
    outer_info = HOST_DOCKER.run("info", "--format", "{{.ID}}")
    assert_ok(outer_info)
    outer_daemon_id = outer_info.stdout.strip()
    assert outer_daemon_id, "outer Docker daemon did not report an ID"
    assert inner_daemon_id != outer_daemon_id, "DIND resolved to the outer Docker daemon"


def _load_candidate(name: str, client: DockerClient, archive: Path) -> str:
    # The DIND runtime shadows /tmp, so copy the archive into its root filesystem.
    copied = HOST_DOCKER.run("cp", str(archive), f"{name}:/candidate.tar")
    assert_ok(copied)
    present = HOST_DOCKER.run("exec", name, "test", "-f", "/candidate.tar")
    assert_ok(present)
    loaded = client.run("image", "load", "--input", "/candidate.tar")
    assert_ok(loaded)
    listed = client.run("image", "ls", "--quiet", "--no-trunc")
    assert_ok(listed)
    image_ids = {line.strip() for line in listed.stdout.splitlines() if line.strip()}
    assert len(image_ids) == 1, f"candidate archive loaded unexpected images: {sorted(image_ids)}"
    image_id = image_ids.pop()
    assert image_id.startswith("sha256:"), f"invalid inner candidate image ID: {image_id!r}"
    return image_id


def _report_dind_failure(name: str) -> None:
    try:
        logs = HOST_DOCKER.run("logs", name, timeout=30)
        captured = logs.stdout + logs.stderr
    except (OSError, subprocess.SubprocessError) as exc:
        captured = f"could not read logs: {exc}"
    if captured:
        print(f"isolated Docker daemon logs:\n{captured}", file=sys.stderr)


def _cleanup_dind(name: str, primary_error: BaseException | None) -> None:
    cause: BaseException | None = None
    try:
        cleanup = HOST_DOCKER.run("rm", "-f", "-v", name, timeout=30)
        detail = cleanup.stderr.strip()
        if cleanup.returncode == 0:
            return
    except (OSError, subprocess.SubprocessError) as exc:
        cause = exc
        detail = str(exc)
    message = f"failed to remove isolated Docker daemon {name}: {detail}"
    if primary_error is not None:
        primary_error.add_note(message)
        return
    raise AssertionError(message) from cause


@contextmanager
def isolated_docker_daemon(
    candidate: str,
    archive_dir: Path,
    *,
    name_prefix: str | None = None,
    readiness_timeout: float = 30.0,
) -> Iterator[IsolatedDockerDaemon]:
    """Load *candidate* into a networkless disposable Docker daemon."""
    if readiness_timeout <= 0:
        raise ValueError("readiness_timeout must be positive")
    prefix = name_prefix or os.environ.get("BOOLEY_DOCKER_NAME_PREFIX", "booley-reaper-e2e")
    if _NAME_PREFIX_RE.fullmatch(prefix) is None:
        raise ValueError(f"invalid Docker container name prefix: {prefix!r}")
    platform = _candidate_platform(candidate)
    archive = archive_dir / f"candidate-{uuid.uuid4().hex}.tar"
    assert_ok(
        HOST_DOCKER.run(
            "image",
            "save",
            # BuildKit image IDs can name multi-platform OCI indexes. Exporting the
            # inspected platform gives the inner daemon one runnable image.
            "--platform",
            platform,
            "--output",
            str(archive),
            candidate,
        )
    )
    name = f"{prefix}-dind-{uuid.uuid4().hex[:12]}"
    primary_error: BaseException | None = None
    try:
        _start_dind(name)
        client = DockerClient(("exec", name, "docker", "--host", _DOCKER_SOCKET))
        inner_daemon_id = _wait_for_dind(client, timeout=readiness_timeout)
        _assert_isolated_dind(name, inner_daemon_id)
        image_id = _load_candidate(name, client, archive)
        yield IsolatedDockerDaemon(client=client, image_id=image_id)
    except BaseException as exc:
        primary_error = exc
        _report_dind_failure(name)
        raise
    finally:
        _cleanup_dind(name, primary_error)


def candidate_image(environment_variable: str, proof_name: str) -> str:
    """Return an available opt-in image or skip the proof."""
    image = os.environ.get(environment_variable)
    if image is None:
        pytest.skip(f"set {environment_variable} to run the {proof_name}")
    if shutil.which("docker") is None:
        pytest.skip("docker is unavailable")
    if docker("image", "inspect", image).returncode != 0:
        pytest.skip(f"{image} is not built")
    return image


def assert_python_version(image: str, expected: str = "Python 3.14.7") -> None:
    """Assert the exact Python patch packaged in a sidecar image."""
    version = docker("run", "--rm", "--entrypoint", "python3", image, "--version")
    assert_ok(version)
    assert (version.stdout + version.stderr).strip() == expected
