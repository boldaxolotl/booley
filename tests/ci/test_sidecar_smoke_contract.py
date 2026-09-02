"""Behavioral contract for the independently scheduled sidecar smoke job."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml

_WORKFLOW_PATH = Path(".github/workflows/test.yml")


def _jobs() -> dict[str, Any]:
    workflow = yaml.safe_load(_WORKFLOW_PATH.read_text(encoding="utf-8"))
    return workflow["jobs"]


def _step_commands(job: dict[str, Any]) -> str:
    return "\n".join(str(step.get("run", "")) for step in job["steps"])


def _step(job: dict[str, Any], name: str) -> dict[str, Any]:
    return next(step for step in job["steps"] if step.get("name") == name)


def test_sidecar_smoke_is_independently_scheduled_and_required() -> None:
    jobs = _jobs()
    sidecar = jobs["sidecar-smoke"]

    assert sidecar["needs"] == "changes"
    assert sidecar["if"] == "fromJSON(needs.changes.outputs.jobs)['sidecar-smoke']"
    assert "sidecar-smoke" in jobs["ci-required"]["needs"]
    assert "sidecar-build-evidence.sh" not in _step_commands(jobs["bwave-smoke"])
    assert "test_sidecar_image_helpers.py" not in _step_commands(jobs["bwave-smoke"])


def test_sidecar_smoke_proves_execution_and_always_uploads_evidence() -> None:
    sidecar = _jobs()["sidecar-smoke"]
    commands = _step_commands(sidecar)

    assert "bash .github/scripts/sidecar-build-evidence.sh" in commands
    for test_file in (
        "test_sidecar_image_helpers.py",
        "test_egress_proxy.py",
        "test_proxy_entry.py",
        "test_reaper.py",
        "test_egress_proxy_image_e2e.py",
        "test_reaper_image_e2e.py",
        "test_flexnet_relay_e2e.py",
    ):
        assert test_file in commands
    assert '--junitxml="${RUNNER_TEMP}/sidecar-junit.xml"' in commands

    assertion = _step(sidecar, "Assert sidecar tests executed without skips")
    assert assertion["if"] == "always()"
    assert "--min-tests 86 --max-skips 0" in assertion["run"]

    upload = _step(sidecar, "Upload sidecar execution evidence")
    assert upload["if"] == "always()"
    assert "docker-build-evidence/" in upload["with"]["path"]
    assert "sidecar-junit.xml" in upload["with"]["path"]
    assert upload["with"]["if-no-files-found"] == "error"
