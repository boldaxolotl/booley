#!/usr/bin/env python3
"""Record container startup latency and representative process peak RSS."""

from __future__ import annotations

import argparse
import json
import re
import statistics
import subprocess
import time
from pathlib import Path

_RSS_PATTERN = re.compile(r"^\s*Maximum resident set size \(kbytes\):\s*(\d+)\s*$", re.MULTILINE)
_COMMON_COMMANDS = {
    "python_import": "python3 -c 'import booley, cocotb, fusesoc'",
    "yosys_diagnostics": "yosys -q -p 'help read_slang'",
    "openroad_diagnostics": "openroad -version",
}


def _docker(argv: list[str], *, timeout: int = 300) -> subprocess.CompletedProcess[str]:
    result = subprocess.run(
        ["docker", *argv],
        capture_output=True,
        text=True,
        check=False,
        timeout=timeout,
    )
    if result.returncode != 0:
        detail = result.stderr.strip() or result.stdout.strip() or "docker command failed"
        raise RuntimeError(detail)
    return result


def parse_max_rss_kib(output: str) -> int:
    """Parse the single GNU time maximum-RSS row."""
    values = _RSS_PATTERN.findall(output)
    if len(values) != 1:
        raise ValueError(f"expected exactly one maximum-RSS row, found {len(values)}")
    return int(values[0])


def summarize_startup(samples_ms: list[float]) -> dict[str, object]:
    """Preserve startup samples and deterministic summary statistics."""
    if not samples_ms:
        raise ValueError("at least one startup sample is required")
    return {
        "samples_ms": samples_ms,
        "first_ms": samples_ms[0],
        "median_ms": statistics.median(samples_ms),
        "max_ms": max(samples_ms),
    }


def measure_startup(reference: str, runs: int) -> dict[str, object]:
    if runs < 1:
        raise ValueError("startup runs must be positive")
    samples: list[float] = []
    for _ in range(runs):
        started = time.perf_counter_ns()
        _docker(["run", "--rm", "--network", "none", reference, "true"])
        elapsed_ms = (time.perf_counter_ns() - started) / 1_000_000
        samples.append(round(elapsed_ms, 3))
    return summarize_startup(samples)


def measure_rss(reference: str, command: str) -> int:
    result = _docker(
        [
            "run",
            "--rm",
            "--network",
            "none",
            "--entrypoint",
            "/usr/bin/time",
            reference,
            "-v",
            "sh",
            "-c",
            command,
        ]
    )
    return parse_max_rss_kib(result.stderr)


def measure_image(name: str, reference: str, runs: int) -> dict[str, object]:
    commands = dict(_COMMON_COMMANDS)
    if name == "riscv":
        commands["spike_diagnostics"] = "spike --help >/dev/null 2>&1"
    peak_rss = {command_name: measure_rss(reference, command) for command_name, command in commands.items()}
    return {
        "reference": reference,
        "cold_container_start": measure_startup(reference, runs),
        "peak_rss_kib": peak_rss,
        "max_representative_peak_rss_kib": max(peak_rss.values()),
    }


def markdown(payload: dict[str, object]) -> str:
    lines = [
        "## Runtime resource observations",
        "",
        "| Image | First container start | Median start | Max representative peak RSS |",
        "| --- | ---: | ---: | ---: |",
    ]
    images = payload["images"]
    assert isinstance(images, dict)
    for name, raw in images.items():
        assert isinstance(raw, dict)
        startup = raw["cold_container_start"]
        assert isinstance(startup, dict)
        rss_kib = raw["max_representative_peak_rss_kib"]
        assert isinstance(rss_kib, int)
        lines.append(
            f"| {name} | {startup['first_ms']:.3f} ms | {startup['median_ms']:.3f} ms | "
            f"{rss_kib / 1024:.2f} MiB |"
        )
    lines.extend(
        [
            "",
            "Startup measures local `docker run --rm ... true` latency after the image is loaded; "
            "it is not registry pull time. RSS is GNU time's maximum resident set size for "
            "representative diagnostics.",
        ]
    )
    return "\n".join(lines) + "\n"


def _named_reference(value: str) -> tuple[str, str]:
    name, separator, reference = value.partition("=")
    if not separator or not name or not reference:
        raise argparse.ArgumentTypeError("image must be NAME=REFERENCE")
    return name, reference


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--image", action="append", required=True, type=_named_reference)
    parser.add_argument("--runs", type=int, default=5)
    parser.add_argument("--json", required=True, type=Path)
    parser.add_argument("--markdown", required=True, type=Path)
    args = parser.parse_args()
    payload = {
        "schema": 1,
        "images": {
            name: measure_image(name, reference, args.runs) for name, reference in args.image
        },
    }
    args.json.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
    args.markdown.write_text(markdown(payload), encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
