"""Validate release-sensitive CI wiring without building a Session Image."""

from __future__ import annotations

import argparse
import json
import os
import subprocess
from pathlib import Path
from typing import Any

import yaml
from ci_changes import classify

_REGRESSION_PATHS = {
    ".github/scripts/image_size_report.py": {"release_sensitive", "standard_image", "riscv_image"},
    ".github/contracts/picorv32-demo.toml": {
        "release_sensitive",
        "standard_image",
        "riscv_image",
    },
    "src/booley/flows/sim/flow.py": {"release_sensitive", "standard_image"},
    "src/booley/harness/doctor.py": {"release_sensitive", "standard_image"},
    ".github/contracts/image-size-limits.toml": {
        "release_sensitive",
        "standard_image",
        "riscv_image",
    },
    "src/booley/flows/synth/flow.py": {"release_sensitive", "standard_image"},
}
_STANDARD_RELEASE_JOBS = {
    "standard-image-contract",
    "openroad-runtime",
    "host-doctor-runtime",
    "simulation-selftest-overlay",
    "helper-image-metadata",
}
_RISCV_RELEASE_JOBS = {
    "riscv-image-contract",
    "demo-ticket-surface",
    "picorv32-demo-flows",
    "ibex-lint-demo",
}


def _mapping(value: object, label: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise ValueError(f"{label} must be a mapping")
    return value


def _needs(job: dict[str, Any]) -> set[str]:
    raw = job.get("needs", [])
    return {raw} if isinstance(raw, str) else set(raw)


def _commands(job: dict[str, Any]) -> tuple[str, ...]:
    steps = job.get("steps", [])
    if not isinstance(steps, list):
        return ()
    return tuple(str(step.get("run", "")) for step in steps if isinstance(step, dict))


def _steps(job: dict[str, Any]) -> tuple[dict[str, Any], ...]:
    raw = job.get("steps", [])
    if not isinstance(raw, list):
        return ()
    return tuple(step for step in raw if isinstance(step, dict))


def _named_step(job: dict[str, Any], name: str) -> dict[str, Any] | None:
    return next((step for step in _steps(job) if step.get("name") == name), None)


def _strings(value: object) -> tuple[str, ...]:
    if isinstance(value, str):
        return (value,)
    if isinstance(value, dict):
        return tuple(text for child in value.values() for text in _strings(child))
    if isinstance(value, list):
        return tuple(text for child in value for text in _strings(child))
    return ()


def validate_pr_topology(workflow: dict[str, Any]) -> tuple[str, ...]:
    jobs = _mapping(workflow.get("jobs"), "test workflow jobs")
    semantic = _mapping(jobs.get("release-semantic"), "release-semantic job")
    aggregate = _mapping(jobs.get("ci-required"), "ci-required job")
    errors: list[str] = []
    if _needs(semantic) != {"changes"}:
        errors.append("release-semantic must depend only on changes")
    if "release-semantic" not in _needs(aggregate):
        errors.append("ci-required must depend on release-semantic")
    if not any("--budget-seconds 60" in command for command in _commands(semantic)):
        errors.append("release-semantic must enforce a 60-second duration budget")
    return tuple(errors)


def _evidence_errors(jobs: dict[str, Any], expected: set[str]) -> list[str]:
    errors: list[str] = []
    for name in expected:
        job = _mapping(jobs[name], name)
        uploads = [
            step
            for step in _steps(job)
            if str(step.get("uses", "")).startswith("actions/upload-artifact@")
        ]
        if len(uploads) != 1:
            errors.append(f"{name} must upload one evidence artifact")
        elif (
            _mapping(uploads[0].get("with"), f"{name} upload inputs").get("if-no-files-found")
            != "error"
        ):
            errors.append(f"{name} must reject missing evidence")
    return errors


def _picorv32_errors(jobs: dict[str, Any]) -> list[str]:
    picorv32 = _mapping(jobs["picorv32-demo-flows"], "picorv32-demo-flows")
    flow_step = _named_step(picorv32, "Run exact reviewed demo flows")
    if flow_step is None:
        return ["picorv32-demo-flows misses its execution step"]
    errors: list[str] = []
    environment = _mapping(flow_step.get("env"), "PicoRV32 flow environment")
    if environment.get("BOOLEY_RUN_PICORV32_FLOWS") != "1":
        errors.append("picorv32-demo-flows must enable lint and simulation")
    if "-e BOOLEY_RUN_PICORV32_FLOWS" not in str(flow_step.get("run", "")):
        errors.append("picorv32-demo-flows must pass its flow flag into the container")
    return errors


def _provenance_errors(jobs: dict[str, Any]) -> list[str]:
    errors: list[str] = []
    for name in ("standard-image-contract", "riscv-image-contract"):
        command = "\n".join(_commands(_mapping(jobs[name], name)))
        if "release_validation/image_provenance.py" not in command:
            errors.append(f"{name} must validate shared provenance and SBOM evidence")
    return errors


def validate_release_topology(workflow: dict[str, Any]) -> tuple[str, ...]:
    jobs = _mapping(workflow.get("jobs"), "release workflow jobs")
    expected = _STANDARD_RELEASE_JOBS | _RISCV_RELEASE_JOBS
    errors: list[str] = []
    missing = sorted(expected - set(jobs))
    if missing:
        errors.append(f"release workflow misses split jobs: {','.join(missing)}")
        return tuple(errors)
    for name in _STANDARD_RELEASE_JOBS:
        job = _mapping(jobs[name], name)
        if "build-and-push-riscv" in _needs(job):
            errors.append(f"{name} must not depend on build-and-push-riscv")
        if any("needs.build-and-push-riscv" in text for text in _strings(job)):
            errors.append(f"standard release job {name} references the RISC-V build output")
    for name in _RISCV_RELEASE_JOBS:
        if "build-and-push-riscv" not in _needs(_mapping(jobs[name], name)):
            errors.append(f"{name} must depend on build-and-push-riscv")
    errors.extend(_evidence_errors(jobs, expected))
    errors.extend(_picorv32_errors(jobs))
    errors.extend(_provenance_errors(jobs))
    promote = _mapping(jobs.get("promote"), "promote job")
    required = expected | {"build-and-push", "build-and-push-riscv"}
    if _needs(promote) != required:
        errors.append("promote must depend on every split release validation")
    return tuple(errors)


def _classifier_errors() -> tuple[str, ...]:
    errors: list[str] = []
    for path, expected in _REGRESSION_PATHS.items():
        missing = expected - classify([path])
        if missing:
            errors.append(f"{path} misses CI categories: {','.join(sorted(missing))}")
    return tuple(errors)


def _load_workflow(path: Path) -> dict[str, Any]:
    return _mapping(yaml.safe_load(path.read_text(encoding="utf-8")), str(path))


def _check(check_id: str, errors: tuple[str, ...]) -> dict[str, object]:
    return {"id": check_id, "status": "fail" if errors else "pass", "errors": list(errors)}


def _candidate_sha(repo: Path) -> str:
    configured = os.environ.get("GITHUB_SHA")
    if configured:
        return configured
    result = subprocess.run(
        ["git", "-C", str(repo), "rev-parse", "HEAD"],
        capture_output=True,
        check=True,
        text=True,
    )
    return result.stdout.strip()


def validate_repository(repo: Path, *, candidate_sha: str | None = None) -> dict[str, object]:
    pr = _load_workflow(repo / ".github/workflows/test.yml")
    release = _load_workflow(repo / ".github/workflows/docker-publish.yml")
    checks = [
        _check("classifier.release-sensitive", _classifier_errors()),
        _check("workflow.pr-topology", validate_pr_topology(pr)),
        _check("workflow.release-topology", validate_release_topology(release)),
    ]
    errors = [error for check in checks for error in check["errors"]]
    return {
        "schema": 1,
        "candidate_sha": candidate_sha or _candidate_sha(repo),
        "checks": checks,
        "errors": errors,
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--repo", type=Path, default=Path.cwd())
    parser.add_argument("--evidence", type=Path, required=True)
    args = parser.parse_args()
    evidence = validate_repository(args.repo)
    args.evidence.parent.mkdir(parents=True, exist_ok=True)
    args.evidence.write_text(json.dumps(evidence, indent=2) + "\n", encoding="utf-8")
    for error in evidence["errors"]:
        print(f"error: {error}")
    return 1 if evidence["errors"] else 0


if __name__ == "__main__":
    raise SystemExit(main())
