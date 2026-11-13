"""Stateful Docker fake for headless Sessions whose Project directory may be gone."""

from __future__ import annotations

import json
import subprocess
import sys
from collections.abc import Callable, Container
from pathlib import Path
from typing import Any

import pytest

from booley.core.project_identity import project_label_id
from booley.harness import booley as cli
from booley.runtime import interactive_docker, session_admission, session_runtime


def completed(
    returncode: int = 0, stdout: str = "", stderr: str = ""
) -> subprocess.CompletedProcess[str]:
    return subprocess.CompletedProcess(["docker"], returncode, stdout, stderr)


def headless_session_entry(name: str, root: Path, *, running: bool = True) -> dict[str, Any]:
    """Describe one headless Session the way `docker container inspect` reports it.

    The canonical root is the spelling `session up` recorded: the label digest and the
    `/work` bind source both carry it, and neither needs the directory to exist later.
    """
    canonical = str(root.resolve())
    return {
        "Id": f"id-{name}",
        "Name": f"/{name}",
        "Created": "2026-01-01T00:00:00Z",
        "Image": "sha256:session",
        "State": {"Running": running, "StartedAt": "2026-01-01T00:00:01Z"},
        "Config": {
            "Env": [],
            "Labels": {
                "booley.role": "interactive",
                "booley.project-id": project_label_id(canonical),
            },
        },
        "NetworkSettings": {"Networks": {}},
        "Mounts": [{"Type": "bind", "Destination": "/work", "Source": canonical}],
    }


class SidecarPort:
    """Adapt a docker fake to the `args`-without-`docker` port host_sidecars uses."""

    def __init__(self, fake: SessionDockerFake) -> None:
        self._fake = fake

    def run(self, args: list[str], *, timeout: int = 30) -> subprocess.CompletedProcess[str]:
        del timeout
        return self._fake.run(["docker", *args])


class SessionDockerFake:
    """Serve the `docker` argv `session down` and the hint builders issue."""

    def __init__(self) -> None:
        self.containers: dict[str, dict[str, Any]] = {}
        self.calls: list[list[str]] = []
        self.removed: list[str] = []
        self.stopped: list[str] = []
        self.probed: list[object] = []

    def add_session(self, name: str, root: Path) -> None:
        self.containers[name] = headless_session_entry(name, root)

    def _find(self, key: str) -> str | None:
        for name, entry in self.containers.items():
            if key in {name, entry["Id"]}:
                return name
        return None

    def run(self, argv: list[str], **_kwargs: object) -> subprocess.CompletedProcess[str]:
        self.calls.append(list(argv))
        args = argv[1:]
        if args[0] == "ps":
            return self._ps(args)
        if args[:2] == ["container", "inspect"]:
            name = self._find(args[2])
            if name is None:
                return completed(1, stderr="No such container")
            return completed(stdout=json.dumps(self.containers[name]))
        if args[0] == "stop":
            self.stopped.append(args[1])
            return completed()
        if args[:2] == ["rm", "-f"]:
            name = self._find(args[2])
            if name is not None:
                self.removed.append(name)
                del self.containers[name]
            return completed()
        raise AssertionError(f"unexpected docker call: {argv}")

    def _ps(self, args: list[str]) -> subprocess.CompletedProcess[str]:
        rows = []
        for name, entry in sorted(self.containers.items()):
            labels = entry["Config"]["Labels"]
            if "-a" in args:
                continue  # headless Sessions carry no VS Code folder label
            if "--no-trunc" in args:
                rows.append(
                    f"{entry['Id']}\t{name}\t\t{labels['booley.project-id']}\t{labels['booley.role']}"
                )
            elif entry["State"]["Running"]:
                rows.append(f"{entry['Id']}\t{name}")
        return completed(stdout="".join(f"{row}\n" for row in rows))


def route_session_commands(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    run: Callable[..., subprocess.CompletedProcess[str]],
    existing: Container[str],
) -> list[object]:
    """Send every Docker call `booley session down` makes to *run*.

    Also isolates host state under *tmp_path*, skips the host-only and host-install
    gates, and stubs the in-Sandbox Standalone Job probe; returns the Sandboxes it saw.
    """
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "xdg-config"))
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "xdg-state"))
    monkeypatch.setattr(session_runtime, "_run", run)
    monkeypatch.setattr(session_admission, "_run", run)
    monkeypatch.setattr(interactive_docker, "container_exists", lambda name: name in existing)
    monkeypatch.setattr(interactive_docker, "network_exists", lambda _name: False)
    monkeypatch.setattr(cli, "_enforce_runtime_location", lambda _command: None)
    monkeypatch.setattr(cli, "_host_install_authority_error", lambda _command: None)
    monkeypatch.setattr(cli.runtime_context, "ensure_proxy_env", lambda: False)
    probed: list[object] = []
    monkeypatch.setattr(
        "booley.harness.sandbox_stop_warning._run_probe",
        lambda sandbox: (
            probed.append(sandbox) or completed(stdout='{"running": [], "unreadable": []}')
        ),
    )
    return probed


def run_cli(monkeypatch: pytest.MonkeyPatch, argv: list[str]) -> int:
    """Run `booley <argv>` in-process and return its exit code."""
    monkeypatch.setattr(sys, "argv", ["booley", *argv])
    return cli.main()
