"""Tests for the Sandbox Docker probes (ADR 0018).

The Docker CLI is the shared `SandboxDocker` fake, so these run without Docker installed.
The last two tests run real processes through `_run_docker`, the CLI boundary itself.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest

from booley.runtime import host_docker as idk
from booley.runtime import image_identity, project_image
from tests.harness.session_docker_fake import SandboxDocker, completed, serve_sandbox_docker
from tests.pipe_holding_tree import (
    heartbeat_tree,
    parent_argv,
    start_deadline_after_tree_start,
    wait_until_heartbeat_stops,
)
from tests.time_budget import hang_guard_s, scaled

_DEADLINE_S = scaled(2)
_ID_A = "sha256:" + "a" * 64
_DAEMON_DOWN = completed(125, stderr="Cannot connect to the Docker daemon")


@pytest.fixture
def docker(monkeypatch: pytest.MonkeyPatch) -> SandboxDocker:
    return serve_sandbox_docker(monkeypatch)


def _record(image_id: str, **labels: str) -> dict[str, Any]:
    return {"Id": image_id, "RootFS": {"Layers": ["sha256:l"]}, "Config": {"Labels": labels}}


def test_image_lookups_read_the_stored_image(docker: SandboxDocker) -> None:
    large = "x" * 1_048_577
    docker.images["keeper"] = _record(_ID_A, large=large)

    assert idk.image_id("keeper") == _ID_A
    metadata = idk.inspect_image_metadata("keeper")

    assert metadata is not None
    assert (metadata.layers, metadata.labels) == (("sha256:l",), {"large": large})
    assert docker.calls == [
        ["docker", "image", "inspect", "keeper", "--format", "{{.Id}}"],
        ["docker", "image", "inspect", "keeper"],
    ]
    assert idk.image_id("gone") is None
    docker.script(("image", "inspect"), completed(1, stderr="Error: no such object: gone"))
    assert idk.inspect_image_metadata("keeper") is None


_LOOKUP_FAILURES = {
    "missing-cli": (FileNotFoundError("docker"), "docker", "image_id"),
    "timeout": (subprocess.TimeoutExpired("docker", 30), "timed out", "image_id"),
    "not-executable": (PermissionError("not executable"), "not executable", "image_id"),
    "daemon-down": (_DAEMON_DOWN, "Cannot connect", "image_id"),
    "exit-without-text": (completed(1), "exited 1", "image_id"),
    "no-id": (completed(0), "no image ID", "image_id"),
    # Only Docker's own no-such-image answer is absence.
    "no-such-container": (
        completed(1, stderr="Error: No such container: keeper:tag"),
        "No such container",
        "inspect_image_metadata",
    ),
    "not-json": (completed(0, stdout="not json"), "invalid metadata", "inspect_image_metadata"),
    "no-rootfs": (
        completed(0, stdout=json.dumps([{"Id": "sha256:x", "RootFS": None}])),
        "invalid metadata",
        "inspect_image_metadata",
    ),
}


@pytest.mark.parametrize("failure", _LOOKUP_FAILURES)
def test_image_lookups_refuse_to_answer_when_docker_cannot(
    failure: str, docker: SandboxDocker
) -> None:
    reply, cause, lookup = _LOOKUP_FAILURES[failure]
    docker.script((), reply)

    with pytest.raises(idk.ImageInspectError, match=cause) as caught:
        getattr(idk, lookup)("keeper:tag")

    assert "'keeper:tag'" in str(caught.value)


_KEEPER = "booley-issued-" + "a" * 64 + ":session"
_KEEPER_FAILURES = {
    # A daemon-failure remove must surface, while an already-missing keeper is fine (acfa2af08).
    "remove-missing": (("image", "rm"), completed(1, stderr="No such image: keeper"), None),
    "remove-daemon-down": (("image", "rm"), _DAEMON_DOWN, "Cannot connect"),
    "remove-missing-cli": (("image", "rm"), FileNotFoundError("docker"), "cannot remove"),
    "tag-no-space": (("image", "tag"), completed(1, stderr="no space"), "no space"),
    "tag-missing-cli": (("image", "tag"), FileNotFoundError("docker"), "cannot retain"),
}


@pytest.mark.parametrize("change", _KEEPER_FAILURES)
def test_keeper_tag_changes_surface_docker_failures(change: str, docker: SandboxDocker) -> None:
    prefix, reply, cause = _KEEPER_FAILURES[change]
    docker.script(prefix, reply)
    action = idk.remove_image_tag if prefix[1] == "rm" else lambda t: idk.tag_image("sha256:a", t)

    if cause is None:
        action(_KEEPER)
        return
    with pytest.raises(RuntimeError, match=cause):
        action(_KEEPER)


def test_lists_only_well_formed_keeper_tags(docker: SandboxDocker) -> None:
    listing = f"{_KEEPER}\nubuntu:latest\nbooley-issued-bad:session\n"
    docker.script(("image", "ls"), completed(stdout=listing))

    assert idk.issued_image_tags() == (_KEEPER,)


def _container_listing(inspect_output: str) -> list[tuple[tuple[str, ...], Any]]:
    return [
        (("container", "ls"), completed(stdout="c" * 64)),
        (("container", "inspect"), completed(stdout=inspect_output)),
    ]


def _daemon_failure(operation: str) -> tuple[Any, str, str]:
    return ([((), completed(1, stderr="daemon unavailable"))], "daemon unavailable", operation)


_INVENTORY_FAILURES = {
    "empty-image-id": (_container_listing(""), "invalid immutable", "container_image_ids_strict"),
    "short-image-id": (
        _container_listing("short"),
        "invalid immutable",
        "container_image_ids_strict",
    ),
    "uppercase-image-id": (
        _container_listing("sha256:" + "A" * 64),
        "invalid immutable",
        "container_image_ids_strict",
    ),
    "tags-daemon-failure": _daemon_failure("issued_image_tags"),
    "containers-daemon-failure": _daemon_failure("container_image_ids_strict"),
    "tags-missing-cli": (
        [((), FileNotFoundError("docker"))],
        "cannot observe",
        "issued_image_tags",
    ),
    "containers-timeout": (
        [((), subprocess.TimeoutExpired("docker", 30))],
        "cannot observe",
        "container_image_ids_strict",
    ),
    # The container listed by `container ls` is gone when it is inspected.
    "disappearing-container": (
        [(("container", "ls"), completed(stdout="c" * 64))],
        "No such container",
        "container_image_ids_strict",
    ),
}


@pytest.mark.parametrize("failure", _INVENTORY_FAILURES)
def test_keeper_inventory_fails_closed(failure: str, docker: SandboxDocker) -> None:
    rules, cause, operation = _INVENTORY_FAILURES[failure]
    for prefix, reply in rules:
        docker.script(prefix, reply)

    with pytest.raises(RuntimeError, match=cause):
        getattr(idk, operation)()


def test_ensure_unproxied_network_creates_only_a_missing_routed_network(
    docker: SandboxDocker,
) -> None:
    assert idk.ensure_unproxied_network() is True
    assert idk.UNPROXIED_NETWORK in docker.networks
    assert idk.ensure_unproxied_network() is False
    assert [c[1:3] for c in docker.calls if c[2:3] == ["create"]] == [["network", "create"]]


def test_ensure_unproxied_network_refuses_an_internal_network(docker: SandboxDocker) -> None:
    docker.script(("network", "inspect"), completed(stdout="true\n"))

    with pytest.raises(RuntimeError, match=f"docker network rm {idk.UNPROXIED_NETWORK}"):
        idk.ensure_unproxied_network()

    assert not any(call[1:3] == ["network", "create"] for call in docker.calls)


@pytest.mark.parametrize(
    "reply",
    [
        completed(
            stdout="booley-claude-state-a\nbooley-codex-state-b\nbooley-t3-state-c\n"
            "booley-pip-local\nunrelated\n"
        ),
        completed(1, stderr="boom"),
    ],
)
def test_state_volumes_lists_state_volumes_only(reply: Any, docker: SandboxDocker) -> None:
    docker.script(("volume", "ls"), reply)

    listed = ["booley-claude-state-a", "booley-codex-state-b", "booley-t3-state-c"]
    expected = [] if reply.returncode else listed
    assert idk.state_volumes() == expected


def test_issued_image_comparison_follows_provenance(docker: SandboxDocker, tmp_path: Path) -> None:
    managed = project_image.project_image_name(tmp_path)
    same, rebuilt = _record(_ID_A, role="same"), _record("sha256:" + "b" * 64, role="same")
    docker.images.update({_ID_A: same, "external": same, managed: same, "rebuilt": rebuilt})

    def compare(configured: str) -> image_identity.Status:
        return idk.compare_issued_image(
            _ID_A, configured, project_root=tmp_path, executable="custom-docker"
        ).status

    assert compare("external") is image_identity.Status.MATCH
    # Inherited labels do not make a rebuilt image the issued one (7b576678a).
    assert compare("rebuilt") is image_identity.Status.MISMATCH
    assert compare("missing") is image_identity.Status.UNKNOWN
    assert compare(managed) is image_identity.Status.UNKNOWN
    assert {call[0] for call in docker.calls} == {"custom-docker"}
    docker.calls.clear()
    old = idk.compare_issued_image("old-tag", "external", project_root=tmp_path)
    assert old.status is image_identity.Status.MISMATCH
    assert docker.calls == []


def test_issued_comparisons_report_inspect_failure_cause(docker: SandboxDocker) -> None:
    docker.script((), _DAEMON_DOWN)
    comparisons = {
        "build": lambda: idk.compare_issued_build("issued"),
        "reference": lambda: idk.compare_issued_reference("issued", "configured"),
        "selection": lambda: idk.compare_issued_selection("issued", "configured"),
    }

    for name, compare in comparisons.items():
        result = compare()

        assert result.status is image_identity.Status.UNKNOWN, name
        assert "Cannot connect to the Docker daemon" in result.detail, name
        assert "'issued'" in result.detail, name


# `_run_docker` is the Docker CLI boundary itself. Its public callers fix the argv to Docker
# verbs and swallow or wrap the timeout, so only a direct call runs a stand-in process tree.


@pytest.mark.parametrize(
    "mode",
    [
        "live",
        "exit",
        pytest.param(
            "escape",
            marks=pytest.mark.skipif(os.name == "nt", reason="start_new_session is POSIX-only"),
        ),
    ],
)
def test_run_docker_timeout_stops_or_abandons_the_tree(
    mode: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The call raises while the grandchild holding the pipe is still running (2f5537dd3, e990c2e5c).

    The grandchild writes `done` only when it ends by itself, so a runner that waits
    for the pipe to close would return only after `done` exists.
    """
    with heartbeat_tree(tmp_path) as paths:
        # `executable` is the interpreter; the "docker args" are the script.
        python, *args = parent_argv(tmp_path, sys.executable, paths, mode)
        start_deadline_after_tree_start(monkeypatch, paths)

        with pytest.raises(subprocess.TimeoutExpired):
            idk._run_docker(args, timeout=_DEADLINE_S, executable=python)

        assert not paths["done"].exists()
        if mode == "live":
            assert wait_until_heartbeat_stops(paths["heartbeat"]), "grandchild outlived the kill"


def test_run_docker_returns_output_and_raises_for_a_missing_cli(tmp_path: Path) -> None:
    code = "import sys; print('out'); print('err', file=sys.stderr); sys.exit(3)"

    result = idk._run_docker(["-c", code], timeout=hang_guard_s(), executable=sys.executable)

    assert (result.returncode, result.stdout.strip(), result.stderr.strip()) == (3, "out", "err")
    with pytest.raises(FileNotFoundError):
        idk._run_docker(
            ["version"], timeout=hang_guard_s(), executable=str(tmp_path / "no-such-docker")
        )
