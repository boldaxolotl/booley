"""Exercise Goal entry, lint, status and Finish on the candidate image."""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from pathlib import Path

import goal_mode_driver


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
        goals=({"family": "lint", "target": "lint_core"},),
    )
    if result.get("state") != "finished":
        raise RuntimeError("demo Goal surface did not finish")
    return {
        "schema": 1,
        "candidate": {"sha": candidate_sha, "image_digest": image_digest},
        "identity": {"uid": os.getuid(), "gid": os.getgid()},
        "goal_roundtrip": result,
        "checks": [
            {"id": name, "status": "pass"}
            for name in (
                "demo.version",
                "demo.registration",
                "demo.goal-enter",
                "demo.goal-lint",
                "demo.goal-status",
                "demo.goal-finish",
            )
        ],
    }


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
    args.evidence.parent.mkdir(parents=True, exist_ok=True)
    args.evidence.write_text(json.dumps(evidence, indent=2) + "\n", encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
