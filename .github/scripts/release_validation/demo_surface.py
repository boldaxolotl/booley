"""Exercise the immutable public demo ticket-authoring surface."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import subprocess
from pathlib import Path


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


def _digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _exercise_commands(
    *, project: Path, state: Path, ticket: Path, slug: str, python: Path, booley: Path
) -> None:
    env = os.environ | {
        "BOOLEY_AGENT_APP": "codex",
        "BOOLEY_IN_SANDBOX": "1",
        "BOOLEY_PROJECT_DIR": str(state),
    }
    _run(
        [str(python), "-I", "-m", "booley.runtime.incontainer_register"], project=project, env=env
    )
    _run(
        [str(python), "-I", "-m", "booley.ticket_board", "validate-ticket", str(ticket)],
        project=project,
        env=env,
    )
    _run([str(python), "-I", "-m", "booley.ticket_board", "show", slug], project=project, env=env)
    preflight = "from pathlib import Path; from booley.harness.preflight import run_preflight; run_preflight(Path.cwd())"
    _run([str(python), "-I", "-c", preflight], project=project, env=env)
    _run([str(booley), "board", "show"], project=project, env=env)


def validate(
    *,
    project: Path,
    project_state: Path,
    ticket_slug: str,
    expected_version: str,
    python: Path,
    booley: Path,
    candidate_sha: str,
    image_digest: str,
) -> dict[str, object]:
    project = project.resolve()
    state = project_state.resolve()
    ticket = state / "tickets" / "board" / "queue" / f"{ticket_slug}.md"
    if not ticket.is_file():
        raise ValueError(f"queued demo ticket is missing: {ticket}")
    before = _digest(ticket)
    env = os.environ | {"BOOLEY_PROJECT_DIR": str(state)}
    version_code = "import booley; print(booley.__version__)"
    version = _run([str(python), "-I", "-c", version_code], project=project, env=env)
    if version != expected_version:
        raise RuntimeError(f"image version differs: {version!r} != {expected_version!r}")
    _exercise_commands(
        project=project,
        state=state,
        ticket=ticket,
        slug=ticket_slug,
        python=python,
        booley=booley,
    )
    active = state / "tickets" / "board" / "active" / ticket.name
    if _digest(ticket) != before or active.exists():
        raise RuntimeError("demo ticket surface mutated the queued ticket")
    return {
        "schema": 1,
        "candidate": {"sha": candidate_sha, "image_digest": image_digest},
        "identity": {"uid": os.getuid(), "gid": os.getgid()},
        "checks": [
            {"id": "demo.version", "status": "pass"},
            {"id": "demo.registration", "status": "pass"},
            {"id": "demo.preflight", "status": "pass"},
            {"id": "demo.ticket-immutable", "status": "pass"},
        ],
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--project", type=Path, required=True)
    parser.add_argument("--project-state", type=Path, required=True)
    parser.add_argument("--ticket-slug", required=True)
    parser.add_argument("--expected-version", required=True)
    parser.add_argument("--python", type=Path, default=Path("/usr/local/bin/python3"))
    parser.add_argument("--booley", type=Path, default=Path("/usr/local/bin/booley"))
    parser.add_argument("--candidate-sha", default=os.environ.get("GITHUB_SHA", "unknown"))
    parser.add_argument("--image-digest", required=True)
    parser.add_argument("--evidence", type=Path, required=True)
    args = parser.parse_args()
    evidence = validate(
        project=args.project,
        project_state=args.project_state,
        ticket_slug=args.ticket_slug,
        expected_version=args.expected_version,
        python=args.python,
        booley=args.booley,
        candidate_sha=args.candidate_sha,
        image_digest=args.image_digest,
    )
    args.evidence.parent.mkdir(parents=True, exist_ok=True)
    args.evidence.write_text(json.dumps(evidence, indent=2) + "\n", encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
