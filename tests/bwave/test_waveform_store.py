"""Behavioral contract for B-Wave-owned waveform-store mechanics."""

from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path

import pytest

from booley.bwave import waveform_store as stores
from tests.conftest import FST_HEADER_BYTES, MINIMAL_FST_BYTES


@pytest.mark.parametrize(
    ("payload", "expected"),
    [
        (b"", stores.StoreStructure.TOO_SMALL),
        (b"not-an-fst" * 4, stores.StoreStructure.MALFORMED),
        (FST_HEADER_BYTES, stores.StoreStructure.NO_VALUE_CHANGES),
        (MINIMAL_FST_BYTES, stores.StoreStructure.VIABLE),
    ],
)
def test_structural_classification(tmp_path: Path, payload: bytes, expected) -> None:
    path = tmp_path / "trace.fst"
    path.write_bytes(payload)

    assert stores.inspect_store(path, probe_reader=False).structure is expected


def test_missing_store_is_distinct(tmp_path: Path) -> None:
    inspection = stores.inspect_store(tmp_path / "missing.fst", probe_reader=False)

    assert inspection.structure is stores.StoreStructure.MISSING
    assert inspection.structurally_usable is False


def test_structural_viability_does_not_imply_queryability(tmp_path: Path, monkeypatch) -> None:
    path = tmp_path / "trace.fst"
    path.write_bytes(MINIMAL_FST_BYTES)
    monkeypatch.setattr(stores, "native_bwave_binary", lambda: "/bin/bwave")
    monkeypatch.setattr(
        stores.subprocess,
        "run",
        lambda *_args, **_kwargs: subprocess.CompletedProcess([], 2, stdout="", stderr="bad"),
    )

    inspection = stores.inspect_store(path)

    assert inspection.structurally_usable is True
    assert inspection.queryable is False
    assert inspection.failure_kind == "reader_error"


def test_reader_metadata_and_expected_scope(tmp_path: Path, monkeypatch) -> None:
    path = tmp_path / "trace.fst"
    path.write_bytes(MINIMAL_FST_BYTES)
    monkeypatch.setattr(stores, "native_bwave_binary", lambda: "/bin/bwave")
    monkeypatch.setattr(
        stores.subprocess,
        "run",
        lambda *_args, **_kwargs: subprocess.CompletedProcess(
            [],
            0,
            stdout=json.dumps(
                {
                    "data": {
                        "scope_prefix": "tb.dut",
                        "root_scopes": ["tb"],
                        "signal_count": 2,
                        "total_ticks": 10,
                        "signals": [{"name": "clk"}],
                    }
                }
            ),
            stderr="",
        ),
    )

    assert stores.inspect_store(path, expected_scope="tb.dut").queryable is True
    mismatch = stores.inspect_store(path, expected_scope="other")
    assert mismatch.failure_kind == "scope_mismatch"


def test_scoped_conversion_removes_partial_output_before_fallback(
    tmp_path: Path, monkeypatch
) -> None:
    vcd = tmp_path / "trace.vcd"
    vcd.write_text("$date now $end\n$scope module tb $end\n", encoding="utf-8")
    destination = tmp_path / "trace.fst"
    calls: list[list[str]] = []
    monkeypatch.setattr(stores, "native_bwave_binary", lambda: "bwave")

    def run(command, **_kwargs):
        calls.append(list(command))
        if "--scope" in command:
            destination.write_bytes(b"partial")
            return subprocess.CompletedProcess(command, 0, stdout=b"", stderr=b"no match")
        assert not destination.exists(), "invalid scoped output survived into fallback"
        destination.write_bytes(MINIMAL_FST_BYTES)
        return subprocess.CompletedProcess(command, 0, stdout=b"", stderr=b"")

    monkeypatch.setattr(stores.subprocess, "run", run)

    result = stores.convert_vcd(
        vcd,
        destination,
        scope="tb.missing",
        allow_unscoped_fallback=True,
    )

    assert result.success is True
    assert len(result.attempts) == 2
    assert "--scope" in calls[0]
    assert "--scope" not in calls[1]
    assert result.attempts[0].stderr == "no match"


def test_refuse_policy_never_overwrites_even_an_invalid_destination(
    tmp_path: Path, monkeypatch
) -> None:
    vcd = tmp_path / "trace.vcd"
    vcd.write_text("$date now $end\n$scope module tb $end\n", encoding="utf-8")
    destination = tmp_path / "trace.fst"
    destination.write_bytes(b"partial")
    monkeypatch.setattr(stores, "native_bwave_binary", lambda: "bwave")

    result = stores.convert_vcd(
        vcd,
        destination,
        existing_store=stores.ExistingStorePolicy.REFUSE,
    )

    assert result.success is False
    assert result.failure_kind == "existing_store"
    assert destination.read_bytes() == b"partial"


def test_discovery_preserves_simulation_manifest_byte_for_byte(tmp_path: Path) -> None:
    cache = tmp_path / "cache"
    cache.mkdir()
    manifest = tmp_path / "trace_status.json"
    original = b'{"attempt":"real-simulation"}\n'
    manifest.write_bytes(original)
    (tmp_path / "trace.fst").write_bytes(MINIMAL_FST_BYTES)

    result = stores.discover_waveform(tmp_path, cache_dir=cache)

    assert result.selected == tmp_path / "trace.fst"
    assert manifest.read_bytes() == original


def test_invalid_newer_cache_does_not_short_circuit_vcd_conversion(
    tmp_path: Path, monkeypatch
) -> None:
    cache = tmp_path / "cache"
    cache.mkdir()
    invalid = cache / "trace.fst"
    invalid.write_bytes(b"newer but invalid")
    vcd = tmp_path / "trace.vcd"
    vcd.write_text("$date now $end\n$scope module tb $end\n", encoding="utf-8")
    os.utime(invalid, (2_000_000, 2_000_000))
    os.utime(vcd, (1_000_000, 1_000_000))
    monkeypatch.setattr(stores, "native_bwave_binary", lambda: "bwave")

    def run(command, **_kwargs):
        invalid.write_bytes(MINIMAL_FST_BYTES)
        return subprocess.CompletedProcess(command, 0, stdout=b"", stderr=b"")

    monkeypatch.setattr(stores.subprocess, "run", run)

    result = stores.discover_waveform(tmp_path, cache_dir=cache)

    assert result.conversion is not None
    assert result.conversion.success is True
    assert result.selected == tmp_path / "trace.fst"
    assert result.selected.read_bytes() == MINIMAL_FST_BYTES
