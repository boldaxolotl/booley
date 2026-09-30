"""Actual running artifact identity crosses every Sandbox attachment path."""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import Mock

import pytest

from booley.runtime import sandbox_artifact as artifact
from booley.runtime import session_runtime as sr

IMAGE = "sha256:" + "a" * 64
CONTAINER = "b" * 64


def _inspection(workspace: Path, *, image: str = IMAGE) -> str:
    return json.dumps(
        [
            {
                "Id": CONTAINER,
                "Image": image,
                "Config": {
                    "Hostname": CONTAINER[:12],
                    "Labels": {
                        "booley.role": "interactive",
                        "booley.project-id": "project",
                        "booley.spec-digest": "issued",
                        "devcontainer.local_folder": str(workspace),
                    },
                },
                "State": {"Running": True},
            }
        ]
    )


def test_host_observes_active_image_without_resolving_selected_tag(tmp_path, monkeypatch):
    monkeypatch.setattr(sr, "session_container_name", lambda _: "sandbox")
    monkeypatch.setattr(sr.dc, "canonical_project_id", lambda _: "project")
    probe = Mock(return_value=_inspection(tmp_path))
    monkeypatch.setattr(artifact, "_docker_stdout", probe)
    observed = artifact.observe(tmp_path, container="sandbox")
    assert observed.image_id == IMAGE
    assert observed.container_id == CONTAINER
    assert all("image" not in call.args[0] for call in probe.call_args_list)


@pytest.mark.parametrize("raw", [None, "[]", "{}", "garbage"])
def test_unavailable_inspection_is_unknown(tmp_path, monkeypatch, raw):
    monkeypatch.setattr(artifact, "_docker_stdout", lambda _: raw)
    assert artifact.observe(tmp_path, container="sandbox").image_id is None


@pytest.mark.parametrize("change", ["image", "owner", "stopped"])
def test_malformed_unowned_or_stopped_container_is_unknown(tmp_path, monkeypatch, change):
    data = json.loads(_inspection(tmp_path))[0]
    if change == "image":
        data["Image"] = "tag:latest"
    elif change == "owner":
        data["Config"]["Labels"]["booley.role"] = "other"
    else:
        data["State"]["Running"] = False
    monkeypatch.setattr(artifact, "_docker_stdout", lambda _: json.dumps([data]))
    assert artifact.observe(tmp_path, container="sandbox").image_id is None


def _receipt(path: Path, *, namespace: str = "uts:[123]") -> None:
    path.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "image_id": IMAGE,
                "container_id": CONTAINER,
                "hostname": CONTAINER[:12],
                "uts_namespace": namespace,
                "booley_version": "1.0",
            }
        )
    )
    path.chmod(0o444)


@pytest.mark.parametrize("attachment", ["bare", "vscode", "automatic", "supervised"])
def test_inside_receipt_needs_no_docker_or_attachment_metadata(tmp_path, monkeypatch, attachment):
    path = tmp_path / "receipt.json"
    _receipt(path)
    monkeypatch.setattr(artifact, "RECEIPT_PATH", path)
    monkeypatch.setattr(artifact, "_root_owned_readonly", lambda _: True)
    monkeypatch.setattr(artifact, "_incarnation", lambda: (CONTAINER[:12], "uts:[123]"))
    monkeypatch.setattr(artifact.runtime_context, "inside_session_runtime", lambda: True)
    monkeypatch.setattr(artifact, "_docker_stdout", Mock(side_effect=AssertionError("Docker")))
    assert artifact.observe(tmp_path).image_id == IMAGE


def test_stale_receipt_cannot_certify_recreated_container(tmp_path, monkeypatch):
    path = tmp_path / "receipt.json"
    _receipt(path)
    monkeypatch.setattr(artifact, "RECEIPT_PATH", path)
    monkeypatch.setattr(artifact, "_root_owned_readonly", lambda _: True)
    monkeypatch.setattr(artifact, "_incarnation", lambda: ("replacement", "uts:[456]"))
    assert artifact.observe_inside().image_id is None


def test_missing_or_writable_receipt_is_unknown(tmp_path, monkeypatch):
    path = tmp_path / "receipt.json"
    monkeypatch.setattr(artifact, "RECEIPT_PATH", path)
    assert artifact.observe_inside().image_id is None
    _receipt(path)
    path.chmod(0o666)
    assert artifact.observe_inside().image_id is None


def test_vscode_observer_covers_replacement_after_prepare(tmp_path, monkeypatch):
    old = json.loads(_inspection(tmp_path))[0]
    new = {**old, "Id": "c" * 64, "Image": "sha256:" + "d" * 64}
    monkeypatch.setattr(artifact, "_prepared_vscode_state", Mock(side_effect=[old, new]))
    monkeypatch.setattr(artifact.time, "sleep", lambda _: None)
    publish = Mock(return_value=True)
    monkeypatch.setattr(artifact, "publish_receipt", publish)
    assert artifact.watch_vscode_start(tmp_path, "issued")
    assert [call.args[1] for call in publish.call_args_list] == [old["Id"], new["Id"]]


def test_vscode_observer_waits_for_new_start_and_is_bounded(tmp_path, monkeypatch):
    state = json.loads(_inspection(tmp_path))[0]
    monkeypatch.setattr(artifact, "_prepared_vscode_state", Mock(side_effect=[None, state]))
    monkeypatch.setattr(artifact.time, "sleep", lambda _: None)
    publish = Mock(return_value=True)
    monkeypatch.setattr(artifact, "publish_receipt", publish)
    assert artifact.watch_vscode_start(tmp_path, "issued")
    publish.assert_called_once_with(tmp_path, CONTAINER)
    monkeypatch.setattr(artifact, "_prepared_vscode_state", lambda *_args: None)
    monkeypatch.setattr(artifact.time, "monotonic", Mock(side_effect=[0, 181]))
    assert not artifact.watch_vscode_start(tmp_path, "issued")


def test_receipt_writer_uses_inspected_id_and_no_host_authority(tmp_path, monkeypatch):
    monkeypatch.setattr(sr.dc, "canonical_project_id", lambda _: "project")
    probe = Mock(side_effect=[_inspection(tmp_path), "1.0", ""])
    monkeypatch.setattr(artifact, "_docker_stdout", probe)
    assert artifact.publish_receipt(tmp_path, "editor")
    command = probe.call_args.args[0]
    assert command[:7] == ["docker", "exec", "--user", "0", "--workdir", "/", CONTAINER]
    assert command[7:14] == [
        "/usr/bin/env",
        "-i",
        "PATH=/usr/local/bin:/usr/bin:/bin",
        "python3",
        "-I",
        "-S",
        "-c",
    ]
    assert command[-3:] == [IMAGE, CONTAINER, "1.0"]
    assert "import booley" not in artifact._RECEIPT_WRITER
    assert "docker.sock" not in artifact._RECEIPT_WRITER


def test_headless_start_publishes_receipt_before_hooks(tmp_path, monkeypatch):
    events = []
    request = type(
        "Request",
        (),
        {
            "name": "sandbox",
            "relay": None,
            "profile": None,
            "labels": (),
            "issuance": type("Issuance", (), {"relay_image_id": None})(),
            "spec": {"postCreateCommand": "create", "postStartCommand": "start"},
        },
    )()
    from booley.runtime import session_admission

    monkeypatch.setattr(session_admission, "admit_start", lambda *_args, **_kw: None)
    monkeypatch.setattr(sr, "_prepare_license_relay", lambda *_args: (None, False))
    monkeypatch.setattr(
        sr, "_create_session_container", lambda *_args: events.append("create-container")
    )
    monkeypatch.setattr(artifact, "publish_receipt", lambda *_args: events.append("receipt"))
    monkeypatch.setattr(sr, "_run_hook", lambda _name, _hook, label: events.append(label))
    assert sr._create_or_resume_session(tmp_path, request, exists=False) is False
    assert events == ["create-container", "receipt", "postCreateCommand", "postStartCommand"]


def test_prepare_launches_observer_after_releasing_host_lock(tmp_path, monkeypatch):
    from contextlib import contextmanager

    from booley.runtime import lifecycle_lock

    events = []

    @contextmanager
    def lock(_name):
        events.append("locked")
        yield
        events.append("released")

    monkeypatch.setattr(lifecycle_lock, "host_lifecycle_lock", lock)
    monkeypatch.setattr(sr, "_recover_before_lifecycle", lambda *_args: None)
    monkeypatch.setattr(sr, "_prepare_unlocked", lambda _: "digest")
    monkeypatch.setattr(
        artifact, "launch_vscode_observer", lambda _path, digest: events.append(digest)
    )
    assert sr.prepare(tmp_path) == "digest"
    assert events == ["locked", "released", "digest"]


def test_receipt_writer_runs_without_project_imports_or_startup_hooks(tmp_path):
    import os
    import subprocess
    import sys

    # Execute the production snippet with a test-local admin UID and directory;
    # Docker supplies root and the fixed /run path in production.
    root = tmp_path / "receipt"
    source = artifact._RECEIPT_WRITER.replace(
        "pathlib.Path('/run/booley-artifact')", f"pathlib.Path({str(root)!r})"
    ).replace("s.st_uid != 0", "s.st_uid != os.getuid()")
    for name in ("sitecustomize.py", "json.py", "booley.py"):
        (tmp_path / name).write_text("raise RuntimeError('Project import executed')\n")
    result = subprocess.run(
        [sys.executable, "-I", "-S", "-c", source, IMAGE, CONTAINER, "1.0"],
        cwd=tmp_path,
        env={**os.environ, "PYTHONPATH": str(tmp_path)},
        capture_output=True,
        text=True,
        check=False,
        timeout=10,
    )
    assert result.returncode == 0, result.stderr
    values = json.loads((root / "receipt.json").read_text())
    assert values["image_id"] == IMAGE
    assert values["container_id"] == CONTAINER
    assert values["booley_version"] == "1.0"
    assert (root / "receipt.json").stat().st_mode & 0o222 == 0
