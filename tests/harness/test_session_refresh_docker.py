"""Opt-in namespaced primitive proof; spec acceptance uses real-builder port tests.

A full refresh would acquire the production image graph and host authority. This
proof instead exercises real park/restore/inspect/start/removal and owned-ID GC
under the production host lock, without acquiring that production graph.
"""

import json
import os
import subprocess
from pathlib import Path
from uuid import uuid4

import pytest

from booley.core.private_store import PrivateStore
from booley.runtime import image_lifecycle, session_runtime
from booley.runtime.lifecycle_lock import host_lifecycle_lock

pytestmark = pytest.mark.skipif(
    os.environ.get("BOOLEY_RUN_REFRESH_DOCKER_PROOF") != "1",
    reason="opt-in real Docker proof: set BOOLEY_RUN_REFRESH_DOCKER_PROOF=1 and run serially (-n 0)",
)


def _docker(*args: str) -> str:
    result = subprocess.run(
        ["docker", *args], capture_output=True, text=True, timeout=180, check=False
    )
    assert result.returncode == 0, result.stderr
    return result.stdout.strip()


def _inventory() -> tuple[set[str], set[str], set[str]]:
    return (
        set(_docker("image", "ls", "--all", "--quiet", "--no-trunc").splitlines()),
        set(
            _docker(
                "image", "ls", "--no-trunc", "--format", "{{.Repository}}:{{.Tag}} {{.ID}}"
            ).splitlines()
        ),
        set(_docker("container", "ls", "--all", "--quiet", "--no-trunc").splitlines()),
    )


def _start(name: str, image: str, token: str) -> str:
    return _docker(
        "run",
        "-d",
        "--name",
        name,
        "--network",
        "none",
        "--label",
        "booley.role=interactive",
        "--label",
        "booley.project-id=" + token,
        "--entrypoint",
        "sleep",
        image,
        "infinity",
    )


def _fail_and_restore(parked, tag: str, containers: list[str]) -> None:
    session_runtime.park_planned_session(parked)
    candidate = _start(parked.name, tag, parked.project_id)
    containers.append(candidate)
    failed = subprocess.run(
        ["docker", "exec", parked.name, "sh", "-c", "exit 7"], timeout=180, check=False
    )
    assert failed.returncode == 7
    _docker("rm", "-f", candidate)
    session_runtime.restore_refresh_session(parked)
    session_runtime.verify_restored_refresh_session(parked)
    assert json.loads(_docker("inspect", parked.name))[0]["Id"] == parked.container_id


def _cleanup(tags, containers, owned) -> None:
    current = _inventory()
    for identity in reversed(containers):
        if identity in current[2]:
            _docker("rm", "-f", identity)
    for tag in reversed(tags):
        if any(row.startswith(tag + " ") for row in _inventory()[1]):
            _docker("image", "rm", "--no-prune", tag)
    for identity in reversed(owned):
        if identity in _inventory()[0]:
            _docker("image", "rm", "--no-prune", identity)


def _instrument(monkeypatch, name, tags, containers, owned):
    original = subprocess.run
    mutations = []

    def run(argv, *args, **kwargs):
        if argv[0] == "docker":
            allowed = {name, name + "-pre-refresh", *tags, *containers, *owned}
            command = argv[1]
            targets = []
            if command == "run":
                targets = [argv[argv.index("--name") + 1]]
            elif command in {"stop", "start", "rename", "rm"}:
                targets = [item for item in argv[2:] if not item.startswith("-")]
            elif command == "tag":
                targets = [argv[-1]]
            elif command == "commit":
                targets = [argv[2], argv[-1]]
            elif command == "exec":
                targets = [argv[2]]
            elif command == "image" and argv[2] == "rm":
                assert "--no-prune" in argv and "-f" not in argv
                targets = [item for item in argv[3:] if not item.startswith("-")]
            assert set(targets) <= allowed, f"foreign Docker mutation refused: {argv}"
            if targets:
                mutations.append(tuple(argv))
            kwargs["timeout"] = min(kwargs.get("timeout") or 180, 180)
        return original(argv, *args, **kwargs)

    monkeypatch.setattr(subprocess, "run", run)
    return mutations


def _gc_keeps_conflict_then_retries(name, token, containers, project, base, tag) -> None:
    """A stopped container keeps its untagged owned image on conflict; a later pass removes it."""
    image = _docker("image", "inspect", tag, "--format", "{{.Id}}")
    stopped = _start(name + "-pre-refresh", tag, token)
    containers.append(stopped)
    _docker("stop", "--time=0", stopped)
    _docker("tag", base, tag)  # as a refresh moves a managed tag off the superseded image
    (kept,) = image_lifecycle.cleanup_owned_images(project)
    assert kept.startswith(image + ": retained (") and "conflict" in kept
    _docker("rm", stopped)
    assert image_lifecycle.cleanup_owned_images(project) == ()
    assert image not in _inventory()[0]


@pytest.mark.timeout(600)
def test_real_namespaced_rollback_and_owned_image_gc(tmp_path: Path, monkeypatch):
    assert not os.environ.get("PYTEST_XDIST_WORKER"), "run Docker proof with -n 0"
    token = uuid4().hex
    reference = "booley-proof-" + token
    name = reference + "-runtime"
    tags = [reference + ":root", reference + ":candidate"]
    containers: list[str] = []
    owned: list[str] = []
    mutations = _instrument(monkeypatch, name, tags, containers, owned)
    # Isolate only private authority/provenance. Never relocate the host lock.
    anchor = tmp_path / "private"
    store = PrivateStore(
        anchor / "session-refresh-images",
        anchor,
        "proof images",
        image_lifecycle.ImageLifecycleError,
    )
    monkeypatch.setattr(image_lifecycle, "_owned_store", lambda: store)
    with host_lifecycle_lock("issue-1552 isolated Docker primitive proof"):
        before = _inventory()
        base = _docker("image", "inspect", "booley-sandbox", "--format", "{{.Id}}")
        try:
            _docker("tag", base, tags[0])
            old = _start(name, tags[0], token)
            containers.append(old)
            _docker("exec", name, "sh", "-c", "echo fixture > /tmp/" + token)
            new_image = _docker("commit", name, tags[1])
            assert new_image not in before[0]
            owned.append(new_image)
            image_lifecycle.OwnedImages(tmp_path).record({tags[1]: new_image}, (new_image,))
            parked = session_runtime.ParkedSession(
                name,
                name + "-pre-refresh",
                True,
                project_id=token,
                reconnect_egress=False,
                container_id=old,
                image_id=base,
            )
            _fail_and_restore(parked, tags[1], containers)
            _gc_keeps_conflict_then_retries(name, token, containers, tmp_path, base, tags[1])
            _docker("image", "rm", "--no-prune", tags[1])
            assert image_lifecycle.cleanup_owned_images(tmp_path) == ()
            assert new_image not in _inventory()[0]
        finally:
            _cleanup(tags, containers, owned)
        assert mutations
        after = _inventory()
        assert before[0] <= after[0], "preexisting image inventory changed during proof"
        assert before[1] <= after[1], "preexisting image references changed during proof"
        assert before[2] <= after[2], "preexisting container inventory changed during proof"
