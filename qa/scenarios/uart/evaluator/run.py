"""Evaluate one frozen case manifest against an exact accepted RTL commit."""

import argparse
import hashlib
import json
import os
import re
import signal
import subprocess
import sys
import time
from pathlib import Path

from cases import evaluator_identity, materialize

HERE = Path(__file__).resolve().parent


def identity(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def candidate_sources(manifest: dict) -> list[Path]:
    """Validate explicit RTL inputs; never discover or import candidate tests."""
    root = Path(manifest["root"]).resolve(strict=True)
    if root.is_relative_to(HERE) or HERE.is_relative_to(root):
        raise ValueError("Candidate Project and operator evaluator must be disjoint")
    result = subprocess.run(
        ["git", "-C", str(root), "rev-parse", "HEAD"],
        check=True,
        capture_output=True,
        text=True,
        timeout=10,
    )
    if result.stdout.strip() != manifest["commit"]:
        raise ValueError("Candidate checkout does not match accepted commit")
    sources = []
    for item in manifest["sources"]:
        path = (root / item["path"]).resolve(strict=True)
        if not path.is_relative_to(root) or path.suffix not in [".v", ".sv"]:
            raise ValueError("RTL source must be a contained .v/.sv file")
        if identity(path) != item["sha256"]:
            raise ValueError(f"Candidate source digest mismatch: {path}")
        committed = subprocess.run(
            ["git", "-C", str(root), "show", f"{manifest['commit']}:{item['path']}"],
            check=True,
            capture_output=True,
            timeout=10,
        ).stdout
        if hashlib.sha256(committed).hexdigest() != item["sha256"]:
            raise ValueError(f"Candidate bytes differ from committed source: {path}")
        sources.append(path)
    if not sources or len(sources) != len(set(sources)):
        raise ValueError("Expected a nonempty unique explicit candidate RTL source list")
    return sources


def launch(args: list[str], log: Path, seconds: float) -> int:
    """Bound simulator work and terminate the entire owned process tree on timeout."""
    if seconds <= 0:
        return 124
    with log.open("w", encoding="utf-8") as stream, subprocess.Popen(
        [sys.executable, str(HERE / "worker.py"), *args],
        stdout=stream,
        stderr=subprocess.STDOUT,
        start_new_session=os.name != "nt",
        env={**os.environ, "PYTHONPATH": str(HERE), "PYTHONSAFEPATH": "1"},
    ) as process:
        try:
            return process.wait(timeout=seconds)
        except subprocess.TimeoutExpired:
            if os.name == "nt":
                subprocess.run(
                    ["taskkill", "/PID", str(process.pid), "/T", "/F"],
                    check=False,
                    capture_output=True,
                    timeout=10,
                )
                process.kill()
            else:
                os.killpg(process.pid, signal.SIGKILL)
            process.wait(timeout=10)
            stream.write("\nOperator execution ceiling exhausted.\n")
            return 124


def verify_controls(controls: dict) -> None:
    if controls.get("evaluator_sha256") != evaluator_identity():
        raise ValueError("Controls must qualify the exact current evaluator")
    expected = {
        f"{family}-{variant}": status
        for family in ["mmio", "serial"]
        for variant, status in [("positive", "pass"), ("corrupt", "fail"), ("restored", "pass")]
    }
    observed = {key: value["status"] for key, value in controls["results"].items()}
    if observed != expected:
        raise ValueError("Complete positive, corruption and restoration controls are required")


def case_result(case: dict, build: Path, output: Path, deadline: float) -> dict:
    directory = output / "cases" / case["id"]
    directory.mkdir(parents=True)
    (directory / "stimulus.json").write_text(json.dumps(case, indent=2, sort_keys=True) + "\n")
    code = launch(
        ["--build", str(build), "--case", str(directory)],
        directory / "execution.log",
        min(30, deadline - time.monotonic()),
    )
    observation = directory / "observations.json"
    if code or not observation.is_file():
        result = {
            "id": case["id"],
            "status": "blocked",
            "reason": f"Evaluator process incomplete, exit {code}",
        }
    else:
        result = json.loads(observation.read_text())
        if result["id"] != case["id"] or result["status"] not in ["pass", "fail", "blocked"]:
            raise ValueError("Malformed independent observation identity/verdict")
    result["artifacts"] = {p.name: identity(p) for p in directory.iterdir() if p.is_file()}
    return result


def evaluate(candidate: dict, manifest: dict, output: Path, seconds: int, controls: dict) -> dict:
    """Retain every attempted and blocked case; never narrow repair selections."""
    if seconds not in [1800, 2700]:
        raise ValueError("Initial/repair evaluator phase ceiling must be 1800/2700 seconds")
    verify_controls(controls)
    if manifest != materialize(manifest["seed"]):
        raise ValueError(
            "Manifest differs from complete frozen cases or evaluator/public identity"
        )
    cases = manifest["cases"]
    ids = [case["id"] for case in cases]
    if (
        not ids
        or ids != sorted(set(ids))
        or any(not re.fullmatch(r"[a-zA-Z0-9_.-]+", name) for name in ids)
    ):
        raise ValueError("Case manifest must have unique sorted safe full IDs")
    sources = candidate_sources(candidate)
    output.mkdir(parents=True, exist_ok=False)
    (output / "candidate.json").write_text(json.dumps(candidate, indent=2) + "\n")
    (output / "manifest.json").write_text(json.dumps(manifest, indent=2, sort_keys=True) + "\n")
    deadline = time.monotonic() + seconds
    build = output / "build"
    code = launch(
        ["--build", str(build), "--sources", *map(str, sources)],
        output / "build.log",
        min(300, deadline - time.monotonic()),
    )
    counts = {"pass": 0, "fail": 0, "blocked": 0}
    with (output / "results.jsonl").open("x", encoding="utf-8") as stream:
        for case in cases:
            result = (
                {
                    "id": case["id"],
                    "status": "blocked",
                    "reason": f"Evaluator build failed: {code}",
                }
                if code
                else case_result(case, build, output, deadline)
            )
            counts[result["status"]] += 1
            stream.write(json.dumps(result, sort_keys=True) + "\n")
            stream.flush()
    (output / "summary.json").write_text(json.dumps(counts, indent=2) + "\n")
    return counts


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--controls", type=Path, required=True)
    parser.add_argument("--candidate", type=Path, required=True)
    parser.add_argument("--manifest", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--phase-seconds", type=int, choices=[1800, 2700], default=1800)
    args = parser.parse_args()
    counts = evaluate(
        json.loads(args.candidate.read_text()),
        json.loads(args.manifest.read_text()),
        args.output.resolve(),
        args.phase_seconds,
        json.loads(args.controls.read_text()),
    )
    return 1 if counts["fail"] or counts["blocked"] else 0


if __name__ == "__main__":
    sys.exit(main())
