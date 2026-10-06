"""Declared CI diagnostics must reach uploads, including mounted outputs.

Audit explicit JUnit, phase-record, and diagnostic flags plus the OpenROAD
export mount and incremental pytest report. Scratch trees are intentionally
excluded. Unsupported path syntax fails instead of silently skipping evidence.
"""

from __future__ import annotations

import copy
import re
from pathlib import Path

import pytest
import yaml

_ROOT = Path(__file__).parents[2]
_FLAGS = r"junitxml|record|evidence|json|markdown"
_VALUE = r'("[^"]+"|\$\{\{.*?\}\}[^\s]*|[^\s]+)'


def _workflow() -> dict:
    return yaml.safe_load((_ROOT / ".github/workflows/test.yml").read_text())


def _steps(steps: list[dict]) -> list[dict]:
    flattened = []
    for step in steps:
        if "parallel" in step:
            flattened.extend(_steps(step["parallel"]))
        elif step.get("uses", "").startswith("./.github/actions/"):
            action = yaml.safe_load((_ROOT / step["uses"] / "action.yml").read_text())
            children = _steps(action["runs"]["steps"])
            if any(
                child.get("uses", "").startswith("actions/upload-artifact@") for child in children
            ):
                assert step.get("if") == "always()", (
                    "composite evidence publication must run after failures"
                )
            flattened.extend(children)
        else:
            flattened.append(step)
    return flattened


def _expand(value: str, variables: dict[str, str]) -> str:
    value = value.strip('"').replace("${{ runner.temp }}", "/runner")
    for _ in range(len(variables) + 1):
        expanded = re.sub(
            r"\$\{([A-Z_a-z][A-Z_a-z0-9]*)\}",
            lambda match: variables.get(match[1], match[0]),
            value,
        )
        expanded = expanded.replace("${{ runner.temp }}", "/runner")
        if expanded == value:
            return value
        value = expanded
    raise AssertionError(f"cyclic evidence path: {value}")


def _outputs(step: dict) -> list[str]:
    script = step.get("run", "")
    variables = {"RUNNER_TEMP": "/runner", **step.get("env", {})}
    variables.update(dict(re.findall(r'^\s*(\w+)="([^"\n]+)"', script, re.MULTILINE)))
    mounts = {
        target: _expand(source, variables)
        for source, target in re.findall(
            r'--mount type=bind,src=("[^"]+"|[^,]+),dst=([^\s,]+)', script
        )
    }
    values = re.findall(rf"--(?:{_FLAGS})(?:=|\s+){_VALUE}", script)
    if any(
        helper in script
        for helper in (
            "ci_run_metrics.py",
            "base_package_inventory.py",
            "riscv_phase_metrics.py finalize",
        )
    ):
        values.extend(re.findall(rf"--output(?:=|\s+){_VALUE}", script))
    values.extend(re.findall(r"BOOLEY_PYTEST_TIMING_FILE=([^\s]+)", script))
    # The helper's second argument is an explicit export directory, not scratch.
    if "verify_openroad_runtime.sh" in script:
        values.append("/evidence/openroad-gui.log")
    outputs = []
    for value in values:
        path = _expand(value, variables)
        for target, source in mounts.items():
            if path.startswith(target + "/"):
                path = source + path[len(target) :]
                break
        assert path.startswith("/runner/"), f"unresolved diagnostic in {step['name']}: {path}"
        outputs.append(path)
    return outputs


def _covered(path: str, upload: dict, tmp_path: Path) -> bool:
    target = tmp_path / path.removeprefix("/runner/")
    target.parent.mkdir(parents=True, exist_ok=True)
    target.touch()
    for raw_pattern in upload["with"]["path"].splitlines():
        pattern = _expand(raw_pattern, {}).removeprefix("/runner/").rstrip("/")
        if target in tmp_path.glob(pattern) or any(
            parent in tmp_path.glob(pattern) for parent in target.parents if parent != tmp_path
        ):
            return True
    return False


def _assert_uploads(workflow: dict, tmp_path: Path) -> None:
    audited_jobs = set()
    for job_name, job in workflow["jobs"].items():
        steps = _steps(job.get("steps", []))
        uploads = [
            step for step in steps if step.get("uses", "").startswith("actions/upload-artifact@")
        ]
        for step in steps:
            for path in _outputs(step):
                audited_jobs.add(job_name)
                gate = step.get("if", "").removeprefix("always() && ")
                compatible = [
                    upload
                    for upload in uploads
                    if upload.get("if") == "always()"
                    or (gate and upload.get("if") == f"always() && {gate}")
                ]
                assert any(_covered(path, upload, tmp_path) for upload in compatible), (
                    f"{step['name']}: no unconditional or producer-gated upload covers {path}"
                )
    assert audited_jobs == {
        "release-semantic",
        "test",
        "coverage-shards",
        "bwave-integration",
        "sidecar-smoke",
        "bwave-smoke",
        "ci-metrics",
    }, "the workflow diagnostic audit must exercise every diagnostic-producing job"


def test_declared_evidence_reaches_uploads(tmp_path: Path) -> None:
    _assert_uploads(_workflow(), tmp_path)


@pytest.mark.parametrize(
    "gap", ["missing-junit", "narrow-phase-gate", "missing-openroad", "missing-incremental"]
)
def test_evidence_audit_rejects_upload_gaps(tmp_path: Path, gap: str) -> None:
    workflow = copy.deepcopy(_workflow())
    steps = workflow["jobs"]["bwave-smoke"]["steps"]
    if gap in {"missing-junit", "missing-incremental"}:
        upload = next(
            step for step in steps if step.get("name") == "Upload coverage release test timing"
        )
        suffix = "coverage-release.xml" if gap == "missing-junit" else "test-timings.jsonl"
        upload["with"]["path"] = "\n".join(
            path for path in upload["with"]["path"].splitlines() if not path.endswith(suffix)
        )
    elif gap == "narrow-phase-gate":
        upload = next(step for step in steps if step.get("name") == "Upload smoke phase records")
        upload["if"] = "always() && needs.changes.outputs.riscv_image == 'true'"
    else:
        steps[:] = [
            step for step in steps if step.get("name") != "Upload OpenROAD runtime evidence"
        ]
    with pytest.raises(AssertionError, match="no unconditional or producer-gated upload"):
        _assert_uploads(workflow, tmp_path)


def test_composite_publication_gate_cannot_hide_evidence(tmp_path: Path) -> None:
    workflow = _workflow()
    publish = next(
        step
        for step in workflow["jobs"]["test"]["steps"]
        if step.get("uses") == "./.github/actions/publish-pytest-evidence"
    )
    publish["if"] = "success()"
    with pytest.raises(AssertionError, match="composite evidence publication"):
        _assert_uploads(workflow, tmp_path)


def test_smoke_evidence_is_uploaded_after_container_cleanup() -> None:
    steps = _workflow()["jobs"]["bwave-smoke"]["steps"]
    cleanup = next(
        index
        for index, step in enumerate(steps)
        if step.get("name") == "Clean up image validation containers"
    )
    for name in (
        "Upload coverage release test timing",
        "Upload smoke phase records",
        "Upload OpenROAD runtime evidence",
    ):
        index = next(index for index, step in enumerate(steps) if step.get("name") == name)
        assert index > cleanup
