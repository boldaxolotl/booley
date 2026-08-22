"""Tests for the machine-readable VCD benchmark runner."""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

TESTS_DIR = Path(__file__).resolve().parent
RUNNER = TESTS_DIR / "benchmark_vcd.py"

FAKE_TIME = """#!/usr/bin/env python3
import subprocess
import sys

output = sys.argv[sys.argv.index("-o") + 1]
command_start = sys.argv.index("-o") + 2
result = subprocess.run(sys.argv[command_start:], check=False)
with open(output, "w", encoding="utf-8") as stream:
    stream.write("user_seconds=0.01\\nsystem_seconds=0.02\\ncpu_percent=90%\\npeak_rss_kb=1234\\n")
raise SystemExit(result.returncode)
"""

FAKE_BWAVE = """#!/usr/bin/env python3
import pathlib
import sys

if sys.argv[1:] == ["--version"]:
    print("bwave 0.test")
    raise SystemExit(0)
if sys.argv[1] == "build":
    output = pathlib.Path(sys.argv[sys.argv.index("-o") + 1])
    header = bytearray(73)
    header[1:9] = (329).to_bytes(8, "big")
    header[65:73] = (3).to_bytes(8, "big")
    output.write_bytes(header + b"payload")
raise SystemExit(0)
"""


def _executable(path: Path, text: str) -> Path:
    path.write_text(text, encoding="utf-8")
    path.chmod(path.stat().st_mode | 0o111)
    return path


def _run(
    tmp_path: Path, *, threshold: str, extra: list[str] | None = None
) -> subprocess.CompletedProcess[str]:
    corpus = tmp_path / "input.vcd"
    corpus.write_text("$enddefinitions $end\n#0\n", encoding="utf-8")
    fake_time = _executable(tmp_path / "time", FAKE_TIME)
    fake_bwave = _executable(tmp_path / "bwave", FAKE_BWAVE)
    command = [
            sys.executable,
            str(RUNNER),
            "--bwave",
            str(fake_bwave),
            "--time-bin",
            str(fake_time),
            "--corpus",
            f"ordinary={corpus}",
            "--output",
            str(tmp_path / "result.json"),
            "--warmups",
            "1",
            "--trials",
            "2",
            "--query-trials",
            "1",
            "--min-bytes-per-second",
            threshold,
        ]
    if extra:
        command.extend(extra)
    return subprocess.run(
        command,
        capture_output=True,
        text=True,
        check=False,
        timeout=30,
    )


@pytest.mark.skipif(os.name == "nt", reason="executable test stubs use POSIX shebangs")
def test_benchmark_writes_complete_machine_readable_record(tmp_path: Path) -> None:
    result = _run(tmp_path, threshold="1")

    assert result.returncode == 0, result.stderr
    report = json.loads((tmp_path / "result.json").read_text(encoding="utf-8"))
    assert report["gate"] == {"passed": True, "violations": []}
    assert report["bwave_version"] == "bwave 0.test"
    assert report["settings"]["engine"] == "parallel"
    corpus = report["corpora"][0]
    assert corpus["profile"] == "ordinary"
    assert len(corpus["trials"]) == 2
    assert corpus["trials"][0]["fst_section_count"] == 3
    assert corpus["trials"][0]["peak_rss_kb"] == 1234
    assert corpus["query"]["trials"][0]["cpu_percent"] == 90.0


@pytest.mark.skipif(os.name == "nt", reason="executable test stubs use POSIX shebangs")
def test_benchmark_gate_fails_after_writing_evidence(tmp_path: Path) -> None:
    result = _run(tmp_path, threshold="1000000000000")

    assert result.returncode == 2
    assert "acceptance gate missed" in result.stderr
    report = json.loads((tmp_path / "result.json").read_text(encoding="utf-8"))
    assert report["gate"]["passed"] is False
    assert "ordinary: median rate" in report["gate"]["violations"][0]


@pytest.mark.skipif(os.name == "nt", reason="executable test stubs use POSIX shebangs")
def test_benchmark_gates_rss_and_serial_output_ratio(tmp_path: Path) -> None:
    baseline = tmp_path / "baseline.json"
    baseline.write_text(
        json.dumps(
            {
                "corpora": [
                    {
                        "profile": "ordinary",
                        "trials": [{"output_bytes": 70}],
                        "query": {"trials": [{"wall_seconds": 1000.0}]},
                    }
                ]
            }
        ),
        encoding="utf-8",
    )
    result = _run(
        tmp_path,
        threshold="1",
        extra=[
            "--max-peak-rss-kb",
            "1000",
            "--baseline",
            str(baseline),
            "--max-output-ratio",
            "1.1",
        ],
    )

    assert result.returncode == 2
    report = json.loads((tmp_path / "result.json").read_text(encoding="utf-8"))
    violations = "\n".join(report["gate"]["violations"])
    assert "peak RSS" in violations
    assert "output-size ratio" in violations
