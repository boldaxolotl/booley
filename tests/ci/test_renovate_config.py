"""Guards for the Renovate runner and its configuration (#2996, tracker #2551, #4390).

Three properties are checked here, because a regression in any of them is a supply
chain hole (coding principle 14, "Repository pins"): the GitHub App token reaches no
update-branch code, nothing automerges, and post-upgrade commands stay within the
runner's allowlist. Renovate's own validator (the ``renovate-config`` CI job) checks
the rest of the configuration.
"""

from __future__ import annotations

import json
import re
import shlex
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import yaml

_ROOT = Path(__file__).parents[2]
_CONFIG_PATH = _ROOT / ".github/renovate.json"
_WORKFLOW_PATH = _ROOT / ".github/workflows/renovate.yml"
_SCRIPTS = "/opt/booley/.github/scripts"
_CHECKOUT_MOUNT = "${GITHUB_WORKSPACE}:/opt/booley:ro"


def _config() -> dict[str, Any]:
    loaded: dict[str, Any] = json.loads(_CONFIG_PATH.read_text(encoding="utf-8"))
    return loaded


def _workflow() -> dict[Any, Any]:
    loaded: dict[Any, Any] = yaml.safe_load(_WORKFLOW_PATH.read_text(encoding="utf-8"))
    return loaded


def _steps(workflow: dict[Any, Any]) -> list[dict[str, Any]]:
    return [step for job in workflow["jobs"].values() for step in job["steps"]]


def _runner_step(workflow: dict[Any, Any]) -> dict[str, Any]:
    """The one step that runs ``docker run``, found by what it does."""
    [step] = [s for s in _steps(workflow) if str(s.get("run", "")).startswith("docker run")]
    return step


def _docker_arguments(command: str) -> tuple[list[str], list[str]]:
    """Names passed as ``--env NAME`` (no value) and ``-v`` mounts of a ``docker run`` command."""
    tokens = shlex.split(command)
    assert tokens[:3] == ["docker", "run", "--rm"], tokens[:3]
    names: list[str] = []
    mounts: list[str] = []
    rest = tokens[3:]
    while rest and rest[0] in {"--env", "-v"}:
        (names if rest[0] == "--env" else mounts).append(rest[1])
        rest = rest[2:]
    assert rest == ["${IMAGE}"], f"unexpected docker arguments: {rest}"
    return names, mounts


def _values_named(node: object, name: str, path: str = "") -> Iterator[tuple[str, Any]]:
    """Yield the path and value of every key called ``name``, at any depth."""
    if isinstance(node, dict):
        for key, value in node.items():
            here = f"{path}.{key}" if path else str(key)
            if key == name:
                yield here, value
            yield from _values_named(value, name, here)
    elif isinstance(node, list):
        for index, value in enumerate(node):
            yield from _values_named(value, name, f"{path}[{index}]")


def test_app_token_reaches_no_update_branch_code() -> None:
    """The workflow runs on schedule or dispatch from main and hands its token to one step."""
    workflow = _workflow()
    text = _WORKFLOW_PATH.read_text(encoding="utf-8")
    triggers: dict[str, Any] = workflow[True]  # PyYAML (YAML 1.1) parses a bare `on` as True
    assert set(triggers) <= {"schedule", "workflow_dispatch"}
    assert workflow["permissions"] == {}
    for name, job in workflow["jobs"].items():
        assert "write" not in json.dumps(job.get("permissions", {})), name

    steps = _steps(workflow)
    [token_job] = [
        job
        for job in workflow["jobs"].values()
        if any("actions/create-github-app-token" in str(s.get("uses", "")) for s in job["steps"])
    ]
    # `workflow_dispatch` can name any branch: only the main-only Environment keeps the
    # App key from a dispatch on an update branch.
    assert token_job.get("environment") == "renovate"
    for step in steps:
        if "actions/checkout" in str(step.get("uses", "")):
            assert step["with"] == {"persist-credentials": False}

    assert text.count("${{ secrets.") == 1
    [token_step] = [s for s in steps if "actions/create-github-app-token" in str(s.get("uses"))]
    assert "${{ secrets." in token_step["with"]["private-key"]
    assert text.count("steps.app-token.outputs.token") == 1
    run_step = _runner_step(workflow)
    assert run_step["env"]["RENOVATE_TOKEN"] == "${{ steps.app-token.outputs.token }}"
    passed, _ = _docker_arguments(run_step["run"])
    assert "RENOVATE_TOKEN" in passed
    assert "${{" not in run_step["run"]
    assert "github.event" not in text


def test_config_never_automerges() -> None:
    """``automerge`` is false wherever the config sets it, and no preset turns it on."""
    config = _config()
    assert config["automerge"] is False
    for path, value in _values_named(config, "automerge"):
        assert value is False, path
    # A preset such as `:automergeMinor` adds rules that the literal check cannot see.
    for path, presets in _values_named(config, "extends"):
        assert not any("automerge" in preset.lower() for preset in presets), path


def test_post_upgrade_commands_are_allowlisted_default_branch_scripts() -> None:
    """Only anchored, allowlisted commands run, and only default-branch repository code."""
    workflow = _workflow()
    run_step = _runner_step(workflow)
    commands: list[str] = [
        command
        for _, block in _values_named(_config(), "postUpgradeTasks")
        for command in block["commands"]
    ]
    assert commands
    patterns: list[str] = json.loads(run_step["env"]["RENOVATE_ALLOWED_COMMANDS"])
    assert patterns
    for pattern in patterns:
        assert pattern.startswith("^") and pattern.endswith("$"), pattern
    for command in commands:
        # Renovate tests each command against every pattern: exactly one must accept it.
        assert sum(bool(re.search(pattern, command)) for pattern in patterns) == 1, command
        program, script, *_ = shlex.split(command)
        assert program == "python3"
        assert script.startswith(f"{_SCRIPTS}/"), command
        assert (_ROOT / ".github/scripts" / script.removeprefix(f"{_SCRIPTS}/")).is_file()
    # Nothing but the commands is allowed: every pattern accepts one of them.
    assert all(any(re.search(pattern, command) for command in commands) for pattern in patterns)
    # A suffix, a prefix or a branch-relative script is not accepted.
    for command in commands:
        for tampered in (f"{command} && id", f"; {command}", command.replace("/opt/booley/", "")):
            assert not any(re.search(pattern, tampered) for pattern in patterns), tampered
    # The runner mounts its credential-less default-branch checkout read-only and gives
    # Renovate no shell to run commands through.
    assert _docker_arguments(run_step["run"])[1] == [_CHECKOUT_MOUNT]
    assert "SHELL_EXECUTOR" not in _WORKFLOW_PATH.read_text(encoding="utf-8")
