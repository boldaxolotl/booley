"""Prove that Simulation Doctor self-tests isolate their bad runtime overlay."""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
from pathlib import Path

_GOOD = re.compile(r"sim self-test good case .* passes")
_BAD = re.compile(r"sim self-test bad case .* correctly graded a failure")


def _doctor(project: Path, booley: Path) -> str:
    result = subprocess.run(
        [str(booley), "doctor", "--deep", "--skip-agent-checks"],
        cwd=project,
        capture_output=True,
        text=True,
        check=False,
        timeout=900,
    )
    output = result.stdout + result.stderr
    if result.returncode != 0:
        raise RuntimeError(f"deep Doctor failed ({result.returncode})\n{output}")
    return output


def validate(
    *, project: Path, booley: Path, candidate_sha: str, image_digest: str
) -> dict[str, object]:
    output = _doctor(project.resolve(), booley.resolve())
    if _GOOD.search(output) is None:
        raise RuntimeError("deep Doctor did not prove the good Simulation self-test")
    if _BAD.search(output) is None:
        raise RuntimeError("deep Doctor did not reject the bad runtime overlay")
    if "0 failed." not in output:
        raise RuntimeError("deep Doctor summary contains failures")
    return {
        "schema": 1,
        "candidate": {"sha": candidate_sha, "image_digest": image_digest},
        "identity": {"uid": os.getuid(), "gid": os.getgid()},
        "checks": [
            {"id": "simulation-selftest.good", "status": "pass"},
            {"id": "simulation-selftest.bad-overlay", "status": "pass"},
            {"id": "doctor.summary", "status": "pass"},
        ],
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--project", type=Path, default=Path.cwd())
    parser.add_argument("--booley", type=Path, default=Path("/usr/local/bin/booley"))
    parser.add_argument("--candidate-sha", default=os.environ.get("GITHUB_SHA", "unknown"))
    parser.add_argument("--image-digest", required=True)
    parser.add_argument("--evidence", required=True, type=Path)
    args = parser.parse_args()
    evidence = validate(
        project=args.project,
        booley=args.booley,
        candidate_sha=args.candidate_sha,
        image_digest=args.image_digest,
    )
    args.evidence.parent.mkdir(parents=True, exist_ok=True)
    args.evidence.write_text(json.dumps(evidence, indent=2) + "\n", encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
