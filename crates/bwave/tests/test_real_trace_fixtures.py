"""Self-contained compressed real-world VCD fixture tests for B-wave."""

from __future__ import annotations

import gzip
import hashlib
import json
import shutil
import subprocess
import sys
import tomllib
from pathlib import Path

import pytest

THIS_DIR = Path(__file__).resolve().parent
BWAVE_SRC = THIS_DIR.parent
FIXTURE_DIR = THIS_DIR / "fixtures" / "real_trace"
MANIFEST = FIXTURE_DIR / "MANIFEST.toml"
EXE_SUFFIX = ".exe" if sys.platform == "win32" else ""
BWAVE_BIN = BWAVE_SRC / "target" / "debug" / f"bwave{EXE_SUFFIX}"


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as file:
        for chunk in iter(lambda: file.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def run_command(
    args: list[str], *, cwd: Path = BWAVE_SRC, timeout: int = 60
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        args,
        cwd=cwd,
        capture_output=True,
        text=True,
        timeout=timeout,
        check=False,
    )


def ensure_bwave_built() -> None:
    if BWAVE_BIN.exists():
        return
    result = run_command(["cargo", "build"], timeout=180)
    assert result.returncode == 0, (
        f"cargo build failed\nstdout:\n{result.stdout}\nstderr:\n{result.stderr}"
    )


def bwave(args: list[str], *, timeout: int = 60) -> subprocess.CompletedProcess[str]:
    ensure_bwave_built()
    result = run_command([str(BWAVE_BIN), *args], timeout=timeout)
    assert result.returncode == 0, (
        f"bwave {' '.join(args)} failed with {result.returncode}\n"
        f"stdout:\n{result.stdout}\nstderr:\n{result.stderr}"
    )
    return result


def decompress_fixture(manifest: dict, tmp_path: Path) -> Path:
    fixture = FIXTURE_DIR / manifest["compressed"]
    raw_vcd = tmp_path / manifest["raw_vcd"]
    with gzip.open(fixture, "rb") as source, raw_vcd.open("wb") as dest:
        shutil.copyfileobj(source, dest)
    return raw_vcd


def load_fixture_manifests() -> list[dict]:
    data = tomllib.loads(MANIFEST.read_text(encoding="utf-8"))
    return data["fixture"]


def load_ibex_excerpt_manifests() -> list[dict]:
    return [
        json.loads(path.read_text(encoding="utf-8"))
        for path in sorted(FIXTURE_DIR.glob("ibex-*.vcd.gz.manifest.json"))
    ]


@pytest.mark.parametrize("manifest", load_fixture_manifests(), ids=lambda entry: entry["name"])
def test_real_trace_fixture_manifest_hashes(manifest: dict) -> None:
    fixture = FIXTURE_DIR / manifest["compressed"]

    assert fixture.exists(), f"missing compressed fixture: {fixture}"
    assert manifest["compressed_bytes"] == fixture.stat().st_size
    assert manifest["compressed_sha256"] == sha256(fixture)
    assert manifest["raw_bytes"] > manifest["compressed_bytes"]
    assert manifest["signal_count"] >= 50
    assert manifest["transition_count"] >= 300_000


@pytest.mark.parametrize("manifest", load_fixture_manifests(), ids=lambda entry: entry["name"])
def test_real_trace_fixture_builds_and_queries(manifest: dict, tmp_path: Path) -> None:
    raw_vcd = decompress_fixture(manifest, tmp_path)
    bwave_path = tmp_path / f"{manifest['name']}.fst"

    assert raw_vcd.stat().st_size == manifest["raw_bytes"]
    assert sha256(raw_vcd) == manifest["raw_sha256"]

    bwave(["build", str(raw_vcd), "-o", str(bwave_path)], timeout=120)
    assert bwave_path.exists()
    assert bwave_path.stat().st_size > 0

    listed = bwave(["list", str(bwave_path), "-s", "*dut*"], timeout=120)
    assert "cache_controller_real_trace_tb.dut" in listed.stderr
    assert "mem_ready" in listed.stdout

    stats = bwave(["stats", str(bwave_path), "-s", "*read*", "--with-reset"], timeout=120)
    assert "transitions" in stats.stdout
    assert "unique values" in stats.stdout

    value = bwave(["value", str(bwave_path), "--at", "20", "-s", "*address*", "--with-reset"])
    assert "Snapshot at cycle 20" in value.stdout
    assert "address" in value.stdout

    first_read = bwave(["find", str(bwave_path), "*read*", "rising", "--first", "--with-reset"])
    assert "cycle" in first_read.stdout

    wave = bwave(
        [
            "wave",
            str(bwave_path),
            "-s",
            "*hit*",
            "-s",
            "*miss*",
            "-t",
            "10:30",
            "--with-reset",
        ]
    )
    assert "hit" in wave.stdout
    assert "miss" in wave.stdout

    diff = bwave(["diff", str(bwave_path), "20", "40", "-s", "*address*", "--with-reset"])
    assert "diff cycle 20 vs 40" in diff.stdout
    assert "address" in diff.stdout

    distance = bwave(
        [
            "distance",
            str(bwave_path),
            "*read*",
            "rising",
            "--to",
            "*mem_ready*",
            "rising",
            "--stats",
            "--with-reset",
        ]
    )
    assert "count" in distance.stdout.lower()


@pytest.mark.parametrize(
    "manifest", load_ibex_excerpt_manifests(), ids=lambda entry: entry["profile"]
)
def test_ibex_excerpt_serial_parallel_equivalence(manifest: dict, tmp_path: Path) -> None:
    excerpt = manifest["excerpt"]
    serialized_manifest = json.dumps(manifest)
    assert "/home/" not in serialized_manifest
    assert "/work/" not in serialized_manifest
    compressed = FIXTURE_DIR / excerpt["file"]
    raw_vcd = tmp_path / compressed.name.removesuffix(".gz")
    serial = tmp_path / f"{manifest['profile']}-serial.fst"
    parallel = tmp_path / f"{manifest['profile']}-parallel.fst"

    assert compressed.stat().st_size == excerpt["compressed_bytes"]
    assert sha256(compressed) == excerpt["compressed_sha256"]
    with gzip.open(compressed, "rb") as source, raw_vcd.open("wb") as destination:
        shutil.copyfileobj(source, destination)
    assert raw_vcd.stat().st_size == excerpt["raw_bytes"]
    assert sha256(raw_vcd) == excerpt["raw_sha256"]

    bwave(["build", "--engine", "serial", str(raw_vcd), "-o", str(serial)], timeout=180)
    bwave(["build", "--engine", "parallel", str(raw_vcd), "-o", str(parallel)], timeout=180)
    for command in (
        ["list", "--format", "json", "--limit", "20000"],
        ["stats", "--async", "--format", "json", "--limit", "20000"],
    ):
        serial_result = bwave([command[0], str(serial), *command[1:]], timeout=180)
        parallel_result = bwave([command[0], str(parallel), *command[1:]], timeout=180)
        assert json.loads(serial_result.stdout) == json.loads(parallel_result.stdout)
