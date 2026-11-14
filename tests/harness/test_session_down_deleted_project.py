"""`booley session down --project` still stops a Session whose Project directory is gone."""

from __future__ import annotations

import os
import re
import shlex
import shutil
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path

import pytest

from booley.config.host_config import SandboxHostPolicy
from booley.harness import host_sidecars as sidecars
from booley.runtime import session_admission, session_runtime
from tests.harness.session_docker_fake import (
    SessionDockerFake,
    SidecarPort,
    route_session_commands,
    run_cli,
)


def install_docker_fake(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> SessionDockerFake:
    """Route every Docker call `session down` makes to one stateful fake."""
    fake = SessionDockerFake()
    fake.probed = route_session_commands(monkeypatch, tmp_path, fake.run, fake.containers)
    return fake


def start_deleted_session(fake: SessionDockerFake, root: Path) -> str:
    """Start a fake Session for *root*, then delete the Project directory."""
    root.mkdir()
    name = session_runtime.session_container_name(root)
    fake.add_session(name, root)
    shutil.rmtree(root)
    return name


def test_session_down_removes_session_of_deleted_project(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    fake = install_docker_fake(monkeypatch, tmp_path)
    root = tmp_path / "proj"
    name = start_deleted_session(fake, root)

    for selector in ("--project", "-C"):
        fake.removed.clear()
        fake.add_session(name, root)  # the directory is gone; the identity is its path
        assert run_cli(monkeypatch, ["session", "down", selector, str(root)]) == 0
        assert fake.removed == [name], selector
        assert name not in fake.containers
        assert f"removed {name}" in capsys.readouterr().out
    assert fake.probed, "the Standalone Job warning must see the running Sandbox"


def test_session_down_absolute_path_without_session_reports_nothing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    fake = install_docker_fake(monkeypatch, tmp_path)

    code = run_cli(monkeypatch, ["session", "down", "--project", str(tmp_path / "never-existed")])

    assert code == 0
    assert "no Sandbox container for this Project" in capsys.readouterr().out
    assert fake.removed == []


def test_session_down_still_rejects_missing_relative_path(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    fake = install_docker_fake(monkeypatch, tmp_path)
    monkeypatch.chdir(tmp_path)

    code = run_cli(monkeypatch, ["session", "down", "--project", "gone"])

    assert code == 2
    assert "Project path must be an existing directory: gone" in capsys.readouterr().err
    assert fake.calls == []


def _down_hint(message: str) -> list[str]:
    """Extract the `booley session down ...` argv a hint message recommends."""
    commands = re.findall(r"`([^`]*booley session down[^`]*)`", message)
    assert commands, message
    return shlex.split(commands[0])[1:]


@pytest.mark.skipif(os.name == "nt", reason="hint quoting is PowerShell there")
def test_every_session_down_hint_works_for_deleted_project(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    fake = install_docker_fake(monkeypatch, tmp_path)
    monkeypatch.setattr(sidecars, "_docker_adapter", lambda: SidecarPort(fake))

    def host_sidecars_hint(root: Path) -> str:
        return sidecars._active_session_summary(sidecars._active_sessions(SidecarPort(fake)))

    def sandbox(_root: Path) -> session_admission.Sandbox:
        (item,) = session_admission.running_sandboxes(run=fake.run)
        return item

    builders: dict[str, Callable[[Path], str]] = {
        "bootstrap blocker": host_sidecars_hint,
        "recreate remedy": lambda root: session_admission.recreate_remedy(sandbox(root)),
        "capacity refusal": lambda root: session_admission._refusal(
            SandboxHostPolicy(), (sandbox(root),), (), datetime.now(UTC)
        ),
        "identity recovery": lambda root: session_admission._identity_recovery(
            sandbox(root), str(root.resolve())
        ),
    }
    for label, build in builders.items():
        root = tmp_path / label.replace(" ", "-")
        name = start_deleted_session(fake, root)
        hint = build(root)

        assert run_cli(monkeypatch, _down_hint(hint)) == 0, label
        assert fake.removed[-1:] == [name], label
        assert not fake.containers, label
