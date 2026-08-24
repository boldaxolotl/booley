"""Tests for the machine-readable FIFO VCD benchmark runner."""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

TESTS_DIR = Path(__file__).resolve().parent
RUNNER = TESTS_DIR / "benchmark_vcd_fifo.py"

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
    source = pathlib.Path(sys.argv[sys.argv.index("--input") + 1])
    source.read_bytes()
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


def _run(tmp_path: Path, producer_threshold: str) -> subprocess.CompletedProcess[str]:
    corpus = tmp_path / "input.vcd"
    corpus.write_text("$enddefinitions $end\n#0\n", encoding="utf-8")
    fake_time = _executable(tmp_path / "time", FAKE_TIME)
    fake_bwave = _executable(tmp_path / "bwave", FAKE_BWAVE)
    return subprocess.run(
        [
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
            "0",
            "--trials",
            "2",
            "--query-trials",
            "1",
            "--query-range",
            "ordinary=0t:10t",
            "--min-producer-bytes-per-second",
            producer_threshold,
            "--min-converter-bytes-per-second",
            "1",
            "--max-slowest-ratio",
            "100",
        ],
        capture_output=True,
        text=True,
        check=False,
        timeout=30,
    )


@pytest.mark.skipif(os.name != "posix", reason="named pipes require POSIX")
def test_fifo_benchmark_records_independent_and_converter_trials(tmp_path: Path) -> None:
    result = _run(tmp_path, "1")

    assert result.returncode == 0, result.stderr
    report = json.loads((tmp_path / "result.json").read_text(encoding="utf-8"))
    assert report["kind"] == "bwave_fifo_vcd_benchmark"
    assert report["settings"]["engine"] == "parallel"
    assert report["gate"] == {"passed": True, "violations": []}
    corpus = report["corpora"][0]
    assert len(corpus["trivial_reader"]["trials"]) == 2
    assert len(corpus["converter"]["trials"]) == 2
    assert corpus["converter"]["summary"]["fst_section_count"] == 3
    assert corpus["converter"]["trials"][0]["consumer"]["peak_rss_kb"] == 1234


@pytest.mark.skipif(os.name != "posix", reason="named pipes require POSIX")
def test_fifo_benchmark_writes_evidence_when_producer_gate_fails(tmp_path: Path) -> None:
    result = _run(tmp_path, "1000000000000")

    assert result.returncode == 2
    assert "acceptance gate missed" in result.stderr
    report = json.loads((tmp_path / "result.json").read_text(encoding="utf-8"))
    assert report["gate"]["passed"] is False
    assert "trivial-reader producer rate" in report["gate"]["violations"][0]
