"""Contracts for the read-only Agent Readiness Check."""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tomllib
from pathlib import Path

import pytest

from booley.dev_support import agent_readiness as readiness
from booley.runtime.host_probes import ProbeState, probe_docker, probe_github


@pytest.mark.parametrize(
    ("facts", "status"),
    [
        (
            readiness.TopologyFacts(False, "codex/x", "h", "m", "b", 1, False),
            readiness.Status.BLOCKED,
        ),
        (readiness.TopologyFacts(True, "main", "h", "m", "b", 1, False), readiness.Status.BLOCKED),
        (
            readiness.TopologyFacts(True, "codex/x", "h", None, None, None, False),
            readiness.Status.BLOCKED,
        ),
        (
            readiness.TopologyFacts(True, "codex/x", "h", "h", "h", 0, False),
            readiness.Status.READY,
        ),
        (
            readiness.TopologyFacts(True, "codex/x", "h", "h", "h", 0, True),
            readiness.Status.DEGRADED,
        ),
        (readiness.TopologyFacts(True, "codex/x", "h", "m", "b", 2, True), readiness.Status.READY),
        (
            readiness.TopologyFacts(True, "codex/x", "h", "m", "b", 0, False),
            readiness.Status.DEGRADED,
        ),
    ],
)
def test_topology_classifier_is_ordered_and_total(facts, status):
    assert readiness.classify_topology(facts)[0] is status


def test_status_precedence_and_exit_contract():
    checks = [
        readiness.Check("one", readiness.Status.READY, True, "ok"),
        readiness.Check("two", readiness.Status.ESCALATION, True, "approval"),
        readiness.Check("three", readiness.Status.DEGRADED, False, "optional"),
    ]
    assert readiness.aggregate_status(checks) is readiness.Status.ESCALATION
    assert readiness.exit_code(readiness.Status.ESCALATION) == 0
    assert readiness.exit_code(readiness.Status.BLOCKED) == 1


def test_verification_commands_are_stable(tmp_path):
    commands = readiness._verification_commands(tmp_path / "bin/python", tmp_path)
    assert [command.id for command in commands] == [
        "ruff.agent-gate",
        "ruff.ci-check",
        "ruff.ci-format",
        "pytest.broad",
    ]
    assert commands[0].argv == (
        str(tmp_path / "bin/python"),
        "-m",
        "ruff",
        "check",
        "src/",
        "tests/",
    )
    assert commands[0].required_at == "before-commit"
    assert commands[-1].required_at == "optional-broad-verification"


def test_project_runner_pins_match_the_readiness_contract():
    root = Path(__file__).parents[2]
    project = tomllib.loads((root / "pyproject.toml").read_text(encoding="utf-8"))["project"]
    extras = project["optional-dependencies"]
    for extra in ("test", "dev"):
        declared = {item.split("==", 1)[0]: item for item in extras[extra]}
        runners = {
            name: version for name, version in readiness.PINNED_RUNNERS.items() if name != "ruff"
        }
        for name, version in runners.items():
            assert declared.get(name) == f"{name}=={version}"
    quality = {item.split("==", 1)[0]: item for item in extras["quality"]}
    assert quality["ruff"] == "ruff==0.16.7"


def test_bootstrap_dependencies_never_install_booley():
    arguments = readiness.dependency_argv(Path(__file__).parents[2])
    assert not any(
        item.startswith("-e") or item in {".", "booley", "booley-rtl"} for item in arguments
    )


def test_default_docker_check_is_passive(monkeypatch):
    calls = []
    monkeypatch.setattr(readiness.shutil, "which", lambda name: "/usr/bin/docker")
    monkeypatch.setattr(readiness, "_run_process", lambda *args, **kwargs: calls.append(args))
    check = readiness._docker_check(False)
    assert check.status is readiness.Status.READY
    assert not calls


def test_escalation_is_visible_even_when_exit_is_zero():
    check = readiness.Check(
        "git.write-policy", readiness.Status.ESCALATION, False, "approval required"
    )
    result = readiness.Result(
        "prepare",
        readiness.Status.ESCALATION,
        readiness.Repository("/tmp/x", "codex/x", "primary"),
        (check,),
        (),
    )
    human = readiness.render_human(result)
    assert "ESCALATION-REQUIRED" in human
    assert "exit zero is not permission" in human
    assert result.as_dict()["status"] == "escalation-required"


def test_real_launcher_emits_one_versioned_json_document(tmp_path):
    launcher = Path(__file__).parents[2] / ".github/scripts/agent_readiness.py"
    environment = os.environ.copy()
    environment.pop("PYTHONDONTWRITEBYTECODE", None)
    result = subprocess.run(
        [
            sys.executable,
            str(launcher),
            "--phase",
            "prepare",
            "--branch",
            "codex/contract-test",
            "--json",
        ],
        cwd=launcher.parents[2],
        env=environment,
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode in {0, 1}
    document = json.loads(result.stdout)
    assert document["schema_version"] == 1
    assert document["phase"] == "prepare"
    assert not result.stderr


def test_neutral_docker_probe_does_not_disclose_output():
    def run(*args, **kwargs):
        return subprocess.CompletedProcess(args[0], 1, stdout="secret", stderr="permission denied")

    observation = probe_docker(which=lambda _: "/docker", run=run)
    assert observation.state is ProbeState.PERMISSION
    assert not hasattr(observation, "stdout")


def test_neutral_github_probes_auth_and_connectivity_independently():
    seen = []

    def run(args, **kwargs):
        seen.append(tuple(args[1:]))
        return subprocess.CompletedProcess(
            args, 0 if args[1] == "auth" else 1, stdout="", stderr=""
        )

    observation = probe_github(which=lambda _: "/gh", run=run)
    assert observation.authentication is ProbeState.HEALTHY
    assert observation.connectivity is ProbeState.FAILED
    assert seen == [("auth", "status"), ("repo", "view", "--json", "nameWithOwner")]


@pytest.mark.parametrize("value", ["bad", "allowed-now"])
def test_git_write_policy_is_closed(monkeypatch, value):
    monkeypatch.setenv("BOOLEY_AGENT_GIT_WRITE_POLICY", value)
    with pytest.raises(readiness.ReadinessUsageError):
        readiness._prepare(Path.cwd(), "codex/x", None)
