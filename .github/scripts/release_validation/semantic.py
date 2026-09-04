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
    "src/booley/flows/sim/flow.py": {"release_sensitive", "standard_image"},
    "src/booley/harness/doctor.py": {"release_sensitive", "standard_image"},
    ".github/contracts/image-size-limits.toml": {
        "release_sensitive",
        "standard_image",
        "riscv_image",
    },
    "src/booley/flows/synth/flow.py": {"release_sensitive", "standard_image"},
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


def validate_release_topology(workflow: dict[str, Any]) -> tuple[str, ...]:
    jobs = _mapping(workflow.get("jobs"), "release workflow jobs")
    if "promote" not in jobs:
        return ("release workflow must define promote",)
    return ()


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
