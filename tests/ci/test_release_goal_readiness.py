"""Release readiness checks consume the real driver's evidence contract."""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest
import yaml
from tests.python_command_support import python_path_entry

from booley.dev_support.demo_contract_codec import load_contract
from booley.goals.translate import translate_goals

ROOT = Path(__file__).parents[2]
SCRIPT = ROOT / ".github/scripts/release_validation/goal_readiness.py"
CONTRACT = ROOT / ".github/contracts/picorv32-demo.toml"


def _evidence() -> dict:
    specs = translate_goals(load_contract(CONTRACT).required_goals).goals
    keys = [goal.key for goal in specs]
    return {
        "schema": 1,
        "state": "active",
        "record_id": "ci-demo-123",
        "goals": dict.fromkeys(keys, "unmet"),
        "resolved_targets": {goal.key: f"ci:demo:fixture:0#{goal.target}" for goal in specs},
        "steps": {
            "goal_enter": {
                "status": "pass",
                "record_id": "ci-demo-123",
                "response": "Goal Mode ci-demo-123 entered.",
            },
            "goal_status": {"status": "pass", "response": "All declared Goals listed."},
        },
    }


def _run(
    tmp_path: Path, document: object, contract: Path = CONTRACT
) -> subprocess.CompletedProcess[str]:
    path = tmp_path / "goal-readiness.json"
    if document is not None:
        path.write_text(document if isinstance(document, str) else json.dumps(document))
    return subprocess.run(
        [
            sys.executable,
            "-I",
            "-S",
            str(SCRIPT),
            "--contract",
            str(contract),
            "--readiness",
            str(path),
        ],
        capture_output=True,
        text=True,
        timeout=10,
        check=False,
    )


def test_release_accepts_successful_readiness_with_unmet_goals(tmp_path: Path) -> None:
    result = _run(tmp_path, _evidence())
    assert result.returncode == 0, result.stderr
    assert "Goal readiness evidence passed" in result.stdout


@pytest.mark.parametrize(
    "document,message",
    [(None, "cannot read Goal readiness"), ("{", "malformed JSON"), ([], "must be a mapping")],
)
def test_release_rejects_missing_or_malformed_readiness(
    tmp_path: Path, document: object, message: str
) -> None:
    result = _run(tmp_path, document)
    assert result.returncode == 2
    assert message in result.stderr
    assert not result.stdout


@pytest.mark.parametrize(
    "field,value,message",
    [
        ("state", "finished", "Goal State must be active"),
        ("schema", True, "schema"),
        ("record_id", "", "record_id"),
        ("steps", [], "steps must be a mapping"),
        ("goals", [], "goals must be a mapping"),
        ("resolved_targets", [], "resolved_targets must be a mapping"),
    ],
)
def test_release_rejects_invalid_readiness_boundary(
    tmp_path: Path, field: str, value: object, message: str
) -> None:
    document = _evidence()
    document[field] = value
    result = _run(tmp_path, document)
    assert result.returncode == 2
    assert message in result.stderr


@pytest.mark.parametrize("operation", ["goal_enter", "goal_status"])
@pytest.mark.parametrize("change", ["missing", "failed", "empty-response"])
def test_release_requires_each_successful_operation(
    tmp_path: Path, operation: str, change: str
) -> None:
    document = _evidence()
    if change == "missing":
        del document["steps"][operation]
    elif change == "failed":
        document["steps"][operation]["status"] = "fail"
    else:
        document["steps"][operation]["response"] = " "
    result = _run(tmp_path, document)
    assert result.returncode == 2
    assert operation in result.stderr


@pytest.mark.parametrize("change", ["missing", "invalid-status", "missing-target", "empty-target"])
def test_release_names_goal_without_readiness_proof(tmp_path: Path, change: str) -> None:
    document = _evidence()
    key = next(iter(document["goals"]))
    if change == "missing":
        del document["goals"][key]
    elif change == "invalid-status":
        document["goals"][key] = "passed"
    elif change == "missing-target":
        del document["resolved_targets"][key]
    else:
        document["resolved_targets"][key] = " "
    result = _run(tmp_path, document)
    assert result.returncode == 2
    assert key in result.stderr


def test_release_rejects_mismatched_entry_record(tmp_path: Path) -> None:
    document = _evidence()
    document["steps"]["goal_enter"]["record_id"] = "other"
    result = _run(tmp_path, document)
    assert result.returncode == 2
    assert "record_id" in result.stderr


def test_release_derives_required_goals_from_supplied_contract(tmp_path: Path) -> None:
    contract = tmp_path / "demo.toml"
    contract.write_text(
        CONTRACT.read_text() + '\n[[required_goal]]\nfamily = "SIM"\ntarget = "sim_dhry"\n'
    )
    result = _run(tmp_path, _evidence(), contract)
    assert result.returncode == 2
    assert "sim_pass_sim_dhry" in result.stderr


@pytest.mark.parametrize(
    "identity",
    [
        "garbage",
        "lint_core",
        "fixture#lint_core",
        "ci:demo:fixture:0#sim_core",
        "ci:demo:fixture:0##lint_core",
    ],
)
def test_release_rejects_malformed_or_mismatched_target_identity(
    tmp_path: Path, identity: str
) -> None:
    document = _evidence()
    document["resolved_targets"]["lint_clean_lint_core"] = identity
    result = _run(tmp_path, document)
    assert result.returncode == 2
    assert "lint_clean_lint_core" in result.stderr


@pytest.mark.skipif(sys.platform == "win32", reason="release step runs under bash on Linux")
@pytest.mark.parametrize("valid", [True, False])
def test_release_summary_is_written_only_after_valid_readiness(
    tmp_path: Path, valid: bool
) -> None:
    directory = tmp_path / "picorv32-evidence"
    directory.mkdir()
    document = _evidence()
    if not valid:
        document["steps"]["goal_status"]["status"] = "fail"
    (directory / "goal-readiness.json").write_text(json.dumps(document))
    workflow = yaml.safe_load((ROOT / ".github/workflows/docker-publish.yml").read_text())
    step = next(
        step
        for job in workflow["jobs"].values()
        for step in job["steps"]
        if step.get("name") == "Record PicoRV32 evidence"
    )
    result = subprocess.run(
        ["bash", "-e", "-o", "pipefail", "-c", step["run"]],
        cwd=ROOT,
        env={
            **os.environ,
            "PATH": f"{python_path_entry(tmp_path / 'python-shim')}{os.pathsep}{os.environ['PATH']}",
            "RUNNER_TEMP": str(tmp_path),
            "GITHUB_SHA": "candidate-sha",
            "IMAGE_DIGEST": "sha256:image",
        },
        capture_output=True,
        text=True,
        check=False,
        timeout=10,
    )
    summary = directory / "summary.json"
    assert summary.exists() == valid
    assert result.returncode == (0 if valid else 2), result.stderr
    if valid:
        assert {"id": "picorv32.goal-ready", "status": "pass"} in json.loads(summary.read_text())[
            "checks"
        ]
