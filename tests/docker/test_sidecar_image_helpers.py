"""Tests for sidecar image Docker-daemon isolation helpers."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest
from tests.sidecar_image_helpers import DockerClient, isolated_docker_daemon


class FakeDockerBoundary:
    def __init__(self, *, inner_daemon_id: str = "inner-daemon") -> None:
        self.calls: list[list[str]] = []
        self.inner_daemon_id = inner_daemon_id

    @staticmethod
    def completed(args, returncode=0, stdout="", stderr=""):
        return subprocess.CompletedProcess(
            args=args,
            returncode=returncode,
            stdout=stdout,
            stderr=stderr,
        )

    def _inner_command(self, args, inner):
        if inner[:2] == ["info", "--format"]:
            return self.completed(args, stdout=f"{self.inner_daemon_id}\n")
        if inner[:2] == ["image", "load"]:
            return self.completed(args)
        if inner[:2] == ["image", "ls"]:
            return self.completed(args, stdout="sha256:candidate\n")
        if inner == ["ps"]:
            return self.completed(args, stdout="inner-container\n")
        raise AssertionError(f"unexpected inner Docker command: {args}")

    def __call__(self, args, **_kwargs):
        self.calls.append(args)
        command = args[1:]
        if command[:3] == ["image", "inspect", "candidate"]:
            result = self.completed(args, stdout="sha256:candidate\tlinux/amd64\n")
        elif command[:2] == ["image", "save"]:
            result = self.completed(args)
        elif command and command[0] == "run":
            result = self.completed(args, stdout="dind-container-id\n")
        elif command[:2] == ["info", "--format"]:
            result = self.completed(args, stdout="outer-daemon\n")
        elif command and command[0] == "inspect":
            config = [
                {
                    "HostConfig": {"NetworkMode": "none", "PortBindings": {}},
                    "Mounts": [{"Source": "volume", "Destination": "/var/lib/docker"}],
                }
            ]
            result = self.completed(args, stdout=json.dumps(config))
        elif (command and command[0] == "cp") or (
            command and command[0] == "exec" and command[2:4] == ["test", "-f"]
        ):
            result = self.completed(args)
        elif command and command[0] == "exec":
            result = self._inner_command(args, command[5:])
        elif command[:2] == ["rm", "-f"]:
            result = self.completed(args)
        elif command and command[0] == "logs":
            result = self.completed(args, stdout="dind logs\n")
        else:
            raise AssertionError(f"unexpected Docker command: {args}")
        return result


def test_docker_client_routes_commands_through_its_prefix(monkeypatch) -> None:
    captured: dict[str, object] = {}

    def fake_run(args, **kwargs):
        captured["args"] = args
        captured["kwargs"] = kwargs
        return subprocess.CompletedProcess(args=args, returncode=0, stdout="ok", stderr="")

    monkeypatch.setattr(subprocess, "run", fake_run)
    client = DockerClient(
        ("exec", "isolated-daemon", "docker", "--host", "unix:///var/run/docker.sock")
    )

    result = client.run("info", timeout=7)

    assert result.stdout == "ok"
    assert captured == {
        "args": [
            "docker",
            "exec",
            "isolated-daemon",
            "docker",
            "--host",
            "unix:///var/run/docker.sock",
            "info",
        ],
        "kwargs": {
            "capture_output": True,
            "text": True,
            "timeout": 7,
            "check": False,
        },
    }


def test_isolated_daemon_routes_commands_and_cleans_up(monkeypatch, tmp_path: Path) -> None:
    boundary = FakeDockerBoundary()
    monkeypatch.setattr(subprocess, "run", boundary)

    with isolated_docker_daemon("candidate", tmp_path, name_prefix="booley-test") as daemon:
        assert daemon.image_id == "sha256:candidate"
        assert daemon.client.run("ps").stdout == "inner-container\n"

    export_index = next(
        i for i, call in enumerate(boundary.calls) if call[1:3] == ["image", "save"]
    )
    start_index = next(i for i, call in enumerate(boundary.calls) if call[1] == "run")
    assert export_index < start_index
    export = boundary.calls[export_index]
    assert export[export.index("--platform") + 1] == "linux/amd64"
    start = boundary.calls[start_index]
    assert "--network" in start and start[start.index("--network") + 1] == "none"
    assert "--mount" not in start
    assert "-v" not in start
    assert "-p" not in start
    assert "--tmpfs" in start and start[start.index("--tmpfs") + 1] == "/var/lib/docker:exec"
    assert "dockerd" in start
    assert start.count("--host=unix:///var/run/docker.sock") == 1
    assert not any(argument.startswith("--host=tcp") for argument in start)
    assert boundary.calls[-1][1:3] == ["rm", "-f"]


def test_isolated_daemon_rejects_outer_daemon_identity(monkeypatch, tmp_path: Path) -> None:
    boundary = FakeDockerBoundary(inner_daemon_id="outer-daemon")
    monkeypatch.setattr(subprocess, "run", boundary)

    with (
        pytest.raises(AssertionError, match="resolved to the outer Docker daemon"),
        isolated_docker_daemon("candidate", tmp_path, name_prefix="booley-test"),
    ):
        pytest.fail("unsafe daemon was yielded")

    assert boundary.calls[-1][1:3] == ["rm", "-f"]


def test_cleanup_timeout_does_not_mask_body_failure(monkeypatch, tmp_path: Path) -> None:
    class CleanupTimeoutBoundary(FakeDockerBoundary):
        def __call__(self, args, **kwargs):
            if args[1:3] == ["rm", "-f"]:
                raise subprocess.TimeoutExpired(args, timeout=30)
            return super().__call__(args, **kwargs)

    boundary = CleanupTimeoutBoundary()
    monkeypatch.setattr(subprocess, "run", boundary)

    with (
        pytest.raises(ValueError, match="body failed") as caught,
        isolated_docker_daemon("candidate", tmp_path, name_prefix="booley-test"),
    ):
        raise ValueError("body failed")

    assert any(
        "failed to remove isolated Docker daemon" in note for note in caught.value.__notes__
    )


def test_readiness_timeout_cleans_named_daemon(monkeypatch, tmp_path: Path) -> None:
    class NeverReadyBoundary(FakeDockerBoundary):
        def _inner_command(self, args, inner):
            if inner[:2] == ["info", "--format"]:
                return self.completed(args, returncode=1, stderr="not ready")
            return super()._inner_command(args, inner)

    boundary = NeverReadyBoundary()
    monkeypatch.setattr(subprocess, "run", boundary)

    with (
        pytest.raises(AssertionError, match="did not become ready"),
        isolated_docker_daemon(
            "candidate",
            tmp_path,
            name_prefix="booley-test",
            readiness_timeout=0.01,
        ),
    ):
        pytest.fail("unready daemon was yielded")

    assert boundary.calls[-1][1:3] == ["rm", "-f"]


@pytest.mark.parametrize("failure", ["start", "copy", "load"])
def test_setup_failure_cleans_named_daemon(monkeypatch, tmp_path: Path, failure: str) -> None:
    class FailingBoundary(FakeDockerBoundary):
        def __call__(self, args, **kwargs):
            command = args[1:]
            inner = command[5:] if command and command[0] == "exec" else []
            fails = (
                (failure == "start" and command and command[0] == "run")
                or (failure == "copy" and command and command[0] == "cp")
                or (failure == "load" and inner[:2] == ["image", "load"])
            )
            if fails:
                self.calls.append(args)
                return self.completed(args, returncode=1, stderr=f"{failure} failed")
            return super().__call__(args, **kwargs)

    boundary = FailingBoundary()
    monkeypatch.setattr(subprocess, "run", boundary)

    with (
        pytest.raises(AssertionError, match=f"{failure} failed"),
        isolated_docker_daemon("candidate", tmp_path, name_prefix="booley-test"),
    ):
        pytest.fail("failed setup yielded a daemon")

    assert boundary.calls[-1][1:3] == ["rm", "-f"]


@pytest.mark.parametrize(
    ("network_mode", "port_bindings", "mounts", "message"),
    [
        ("bridge", {}, [], "must have no outer network"),
        ("none", {"2375/tcp": [{"HostPort": "12345"}]}, [], "must publish no ports"),
        (
            "none",
            {},
            [{"Source": "/var/run/docker.sock", "Destination": "/var/run/docker.sock"}],
            "must not mount an outer Docker socket",
        ),
    ],
)
def test_unsafe_dind_configuration_fails_closed(
    monkeypatch,
    tmp_path: Path,
    network_mode: str,
    port_bindings: dict,
    mounts: list[dict],
    message: str,
) -> None:
    class UnsafeBoundary(FakeDockerBoundary):
        def __call__(self, args, **kwargs):
            command = args[1:]
            if command and command[0] == "inspect":
                self.calls.append(args)
                config = [
                    {
                        "HostConfig": {
                            "NetworkMode": network_mode,
                            "PortBindings": port_bindings,
                        },
                        "Mounts": mounts,
                    }
                ]
                return self.completed(args, stdout=json.dumps(config))
            return super().__call__(args, **kwargs)

    boundary = UnsafeBoundary()
    monkeypatch.setattr(subprocess, "run", boundary)

    with (
        pytest.raises(AssertionError, match=message),
        isolated_docker_daemon("candidate", tmp_path, name_prefix="booley-test"),
    ):
        pytest.fail("unsafe daemon was yielded")

    assert boundary.calls[-1][1:3] == ["rm", "-f"]
