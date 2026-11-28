"""Tests for deterministic real-trace excerpt and replay tooling."""

from __future__ import annotations

import gzip
import hashlib
import json
import subprocess
import sys
from pathlib import Path

TESTS_DIR = Path(__file__).resolve().parent
TOOL = TESTS_DIR / "vcd_corpus.py"

SOURCE_VCD = b"""\
$date fixed $end
$comment text mentioning $var is not a declaration $end
$timescale 1ns $end
$scope module tb $end
$var wire 1 ! clk $end
$var wire 1 ! clk_alias $end
$var wire 4 @ data [3:0] $end
$var real 64 R analog $end
$upscope $end
$enddefinitions $end
$dumpvars
0!
b0011 @
r1.5 R
$end
#0
#5
1!
b1010 @
#10
0!
b1111 @
#15
1!
#20
b0001 @
#25
0!"""


def _run(*args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, str(TOOL), *args],
        cwd=TESTS_DIR,
        capture_output=True,
        text=True,
        check=False,
        timeout=30,
    )


def _capture(source: Path, output: Path, *, start: int = 10, end: int = 20):
    return _run(
        "capture",
        str(source),
        "--output",
        str(output),
        "--start",
        str(start),
        "--end",
        str(end),
        "--profile",
        "ordinary",
        "--source-label",
        "synthetic-fixture",
    )


def _manifest(path: Path) -> dict:
    sidecar = path.with_name(path.name + ".manifest.json")
    return json.loads(sidecar.read_text(encoding="utf-8"))


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def test_capture_reconstructs_incoming_state_and_preserves_window(tmp_path: Path) -> None:
    source = tmp_path / "source.vcd"
    excerpt = tmp_path / "ordinary.vcd.gz"
    source.write_bytes(SOURCE_VCD)

    result = _capture(source, excerpt)

    assert result.returncode == 0, result.stderr
    with gzip.open(excerpt, "rb") as stream:
        captured = stream.read()
    header = SOURCE_VCD[: SOURCE_VCD.index(b"$dumpvars")]
    assert captured.startswith(header)
    expected_body = b"""#10
$dumpvars
1!
b1010 @
r1.5 R
$end
0!
b1111 @
#15
1!
#20
b0001 @
"""
    assert captured[len(header) :] == expected_body

    manifest = _manifest(excerpt)
    assert manifest["selection"]["actual_start"] == 10
    assert manifest["selection"]["actual_end"] == 20
    assert manifest["signals"] == {
        "declarations": 4,
        "unique_ids": 3,
        "width_distribution": {"1": 1, "4": 1, "64": 1},
    }
    assert str(source) not in json.dumps(manifest)


def test_capture_is_deterministic_and_handles_zero_start(tmp_path: Path) -> None:
    source = tmp_path / "source.vcd.gz"
    first = tmp_path / "first.vcd.gz"
    second = tmp_path / "second.vcd.gz"
    source.write_bytes(gzip.compress(SOURCE_VCD, mtime=0))

    first_result = _capture(source, first, start=0, end=15)
    second_result = _capture(source, second, start=0, end=15)

    assert first_result.returncode == 0, first_result.stderr
    assert second_result.returncode == 0, second_result.stderr
    assert first.read_bytes() == second.read_bytes()
    first_manifest = _manifest(first)
    assert first_manifest["excerpt"]["raw_sha256"] == _manifest(second)["excerpt"]["raw_sha256"]
    assert first_manifest["source"]["compression"] == "gzip"
    assert first_manifest["source"]["raw_bytes"] == len(SOURCE_VCD)
    assert first_manifest["source"]["raw_sha256"] == hashlib.sha256(SOURCE_VCD).hexdigest()


def test_trusted_verilator_tail_matches_full_source_statistics(tmp_path: Path) -> None:
    source = tmp_path / "source.vcd"
    full = tmp_path / "full.vcd.gz"
    fast = tmp_path / "fast.vcd.gz"
    source.write_bytes(SOURCE_VCD)
    assert _capture(source, full, start=0, end=10).returncode == 0

    result = _run(
        "capture",
        str(source),
        "--output",
        str(fast),
        "--start",
        "0",
        "--end",
        "10",
        "--profile",
        "ordinary",
        "--source-label",
        "synthetic-fixture",
        "--trusted-verilator-tail",
    )

    assert result.returncode == 0, result.stderr
    assert full.read_bytes() == fast.read_bytes()
    full_manifest = _manifest(full)
    fast_manifest = _manifest(fast)
    assert fast_manifest["source"] == full_manifest["source"]
    assert fast_manifest["activity"] == {
        **full_manifest["activity"],
        "tail_scan": "trusted_verilator_aggregate",
    }


def test_replay_offsets_complete_windows_and_records_hash(tmp_path: Path) -> None:
    source = tmp_path / "source.vcd"
    excerpt = tmp_path / "ordinary.vcd.gz"
    output = tmp_path / "ordinary-1k.vcd"
    source.write_bytes(SOURCE_VCD)
    assert _capture(source, excerpt).returncode == 0

    result = _run(
        "replay",
        str(excerpt),
        "--output",
        str(output),
        "--target-bytes",
        "1024",
    )

    assert result.returncode == 0, result.stderr
    data = output.read_bytes()
    timestamps = [int(line[1:]) for line in data.splitlines() if line.startswith(b"#")]
    assert output.stat().st_size >= 1024
    assert timestamps == sorted(timestamps)
    assert timestamps[:6] == [10, 15, 20, 21, 26, 31]
    assert data.count(b"$dumpvars") == 1
    manifest = _manifest(output)
    assert manifest["profile"] == "ordinary"
    assert manifest["output"]["sha256"] == _sha256(output)
    assert manifest["output"]["repetitions"] > 1


def test_capture_rejects_decreasing_timestamps_without_output(tmp_path: Path) -> None:
    source = tmp_path / "decreasing.vcd"
    output = tmp_path / "bad.vcd.gz"
    source.write_bytes(SOURCE_VCD + b"\n#24\n1!\n")

    result = _capture(source, output)

    assert result.returncode == 1
    assert "decreasing timestamp 24 after 25" in result.stderr
    assert not output.exists()
    assert not output.with_name(output.name + ".manifest.json").exists()


def test_capture_reconstructs_incoming_dumpoff_state(tmp_path: Path) -> None:
    source = tmp_path / "dumpoff.vcd"
    output = tmp_path / "dumpoff.vcd.gz"
    source.write_bytes(SOURCE_VCD + b"\n#30\n$dumpoff\n$end\n#35\n1!\n#40\n$dumpon\n$end\n1!\n")

    result = _capture(source, output, start=35, end=40)

    assert result.returncode == 0, result.stderr
    with gzip.open(output, "rb") as stream:
        captured = stream.read()
    assert b"$dumpvars\n0!\n" in captured
    assert b"$end\n$dumpoff\n$end\n1!\n#40\n$dumpon" in captured
