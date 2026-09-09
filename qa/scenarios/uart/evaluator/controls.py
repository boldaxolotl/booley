"""Run positive, one-bit corruption and restored controls through the real adapter."""

import argparse
import time
from datetime import UTC, datetime
from pathlib import Path

from cases import evaluator_identity
from control_cases import VARIANTS, control_cases
from publication import publish_new
from run import HERE, case_result, launch


def controls(output: Path) -> dict:
    """Qualify selected observation paths without claiming full UART conformance."""
    output.mkdir(parents=True, exist_ok=False)
    evaluator_sha256 = evaluator_identity()
    results = {}
    phase_deadline = time.time() + 1800
    deadline = time.monotonic() + 1800
    for family, source_name, mutation, stimulus in control_cases():
        source = (HERE / "controls" / f"{source_name}.sv").read_text()
        for variant, verdict in VARIANTS.items():
            directory = output / f"{family}-{variant}"
            directory.mkdir()
            fixture = write_variant(directory, source, mutation, variant)
            build = directory / "build"
            code = launch(
                ["--build", str(build), "--sources", str(fixture)],
                directory / "build.log",
                min(300, deadline - time.monotonic()),
            )
            if code:
                raise RuntimeError(f"Control fixture build failed: {directory}")
            result = case_result(stimulus, build, directory, deadline)
            results[f"{family}-{variant}"] = result
            if result["status"] != verdict:
                raise RuntimeError(f"Control {family}/{variant} expected {verdict}: {result}")
    if evaluator_identity() != evaluator_sha256:
        raise RuntimeError("Evaluator changed during controls; retain this attempt and restart")
    publish_new(
        output / "controls.json",
        {
            "evaluator_sha256": evaluator_sha256,
            "results": results,
            "phase_deadline": datetime.fromtimestamp(phase_deadline, UTC)
            .isoformat(timespec="seconds")
            .replace("+00:00", "Z"),
        },
    )
    return results


def write_variant(directory: Path, source: str, family: str, variant: str) -> Path:
    # Boundary controls use independent literal source-clock periods. Their
    # corrupt variants move exactly one clock outside the public interval.
    if family in ["timeout_early", "timeout_late"]:
        period = 1920 if family == "timeout_early" else 2176
        source = source.replace("TIMEOUT_CYCLES = 2048", f"TIMEOUT_CYCLES = {period}")
    content = source.replace(
        f"CORRUPT_{family.upper()} = 0",
        f"CORRUPT_{family.upper()} = {int(variant == 'corrupt')}",
    )
    fixture = directory / "transport.sv"
    fixture.write_text(content)
    return fixture


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("output", type=Path)
    args = parser.parse_args()
    controls(args.output.resolve())


if __name__ == "__main__":
    main()
