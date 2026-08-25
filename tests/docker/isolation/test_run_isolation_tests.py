"""Unit contracts for the host-side sandbox isolation runner."""

from __future__ import annotations

import subprocess

from tests.docker.isolation import run_isolation_tests


def test_each_probe_has_a_unique_cleanup_addressable_name(monkeypatch) -> None:
    commands: list[list[str]] = []

    def fake_run(command: list[str], **_kwargs: object) -> subprocess.CompletedProcess[str]:
        commands.append(command)
        return subprocess.CompletedProcess(command, 1, "", "expected failure")

    monkeypatch.setenv("BOOLEY_DOCKER_NAME_PREFIX", "booley-ci-123-1-isolation")
    monkeypatch.setattr(run_isolation_tests.subprocess, "run", fake_run)

    run_isolation_tests.run("false")
    run_isolation_tests.run("false")

    names = [command[command.index("--name") + 1] for command in commands]
    assert len(set(names)) == 2
    assert all(name.startswith("booley-ci-123-1-isolation-") for name in names)
