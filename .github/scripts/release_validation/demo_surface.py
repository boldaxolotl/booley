"""Exercise Goal entry, lint, status and Finish on the candidate image."""

from __future__ import annotations

import argparse
import os
import subprocess
import sys
from pathlib import Path

import goal_mode_driver

from booley.core.boundary import require_dict
from booley.goals.model import parse_goal_arg


def _run(command: list[str], *, project: Path, env: dict[str, str]) -> str:
    result = subprocess.run(
        command,
        cwd=project,
        env=env,
        capture_output=True,
        text=True,
        check=False,
        timeout=300,
    )
    if result.returncode != 0:
        output = result.stdout + result.stderr
        raise RuntimeError(f"command failed ({result.returncode}): {command!r}\n{output}")
    return result.stdout.strip()


def validate(
    *,
    project: Path,
    project_state: Path,
    expected_version: str,
    python: Path,
    candidate_sha: str,
    image_digest: str,
) -> dict[str, object]:
    project = project.resolve()
    state = project_state.resolve()
    _require_image_version(python, project=project, state=state, expected=expected_version)
    env = os.environ | {"BOOLEY_PROJECT_DIR": str(state), "BOOLEY_IN_SANDBOX": "1"}
    _run(
        [str(python), "-I", "-m", "booley.runtime.incontainer_register"], project=project, env=env
    )
    result = goal_mode_driver.validate(
        project=project,
        project_state=state,
        python=python,
        goals=(parse_goal_arg({"family": "lint", "target": "lint_core"}),),
    )
    if result.get("state") != "finished":
        raise RuntimeError("demo Goal surface did not finish")
    goal_checks = _goal_checks(result)
    return {
        "schema": 1,
        "candidate": {"sha": candidate_sha, "image_digest": image_digest},
        "identity": {"uid": os.getuid(), "gid": os.getgid()},
        "goal_roundtrip": result,
        "checks": [
            {"id": "demo.version", "status": "pass"},
            {"id": "demo.registration", "status": "pass"},
            *goal_checks,
        ],
    }


def _goal_checks(result: dict[str, object]) -> list[dict[str, str]]:
    """Every passing surface check has its own recorded operation proof."""
    steps = require_dict(result.get("steps"), field="Goal steps")
    checks = []
    for name in ("goal_enter", "lint", "goal_status", "goal_finish"):
        step = require_dict(steps.get(name), field=name)
        response = step.get("response")
        if step.get("status") != "pass" or not isinstance(response, str) or not response.strip():
            raise RuntimeError(f"demo Goal surface has no successful {name} proof")
        label = name.removeprefix("goal_")
        checks.append({"id": f"demo.goal-{label.replace('_', '-')}", "status": "pass"})
    return checks


def _require_image_version(python: Path, *, project: Path, state: Path, expected: str) -> None:
    """Refuse an image whose installed Booley is not the release candidate's version."""
    env = os.environ | {"BOOLEY_PROJECT_DIR": str(state)}
    version_code = "import booley; print(booley.__version__)"
    version = _run([str(python), "-I", "-c", version_code], project=project, env=env)
    if version != expected:
        raise RuntimeError(f"image version differs: {version!r} != {expected!r}")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--project", type=Path, required=True)
    parser.add_argument("--project-state", type=Path, required=True)
    parser.add_argument("--expected-version", required=True)
    parser.add_argument("--python", type=Path, default=Path(sys.executable))
    parser.add_argument("--candidate-sha", default=os.environ.get("GITHUB_SHA", "unknown"))
    parser.add_argument("--image-digest", required=True)
    parser.add_argument("--evidence", type=Path, required=True)
    args = parser.parse_args()
    evidence = validate(
        project=args.project,
        project_state=args.project_state,
        expected_version=args.expected_version,
        python=args.python,
        candidate_sha=args.candidate_sha,
        image_digest=args.image_digest,
    )
    goal_mode_driver.write_evidence(args.evidence, evidence)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
