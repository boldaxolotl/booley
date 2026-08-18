"""Waveform-store validation — `_bwave_valid` (SETUP-REPORT F-15).

A traced run was accepted as proof of tracing on the strength of a 443-byte
FST that contained nothing but a header. Ibex's `VerilatorSimCtrl` enables
capture only for getopt `-t`/`--trace[=FILE]`, so Booley's generic
`+trace +tracefile=` pair left the simulator writing an empty-but-well-formed
dump. Validation has to require signal data, not a syntactically valid header.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from tests.conftest import FST_HEADER_BYTES, FST_VCDATA_BYTES, MINIMAL_FST_BYTES

from booley.sim.trace_session import _bwave_valid


def _write(tmp_path: Path, data: bytes, name: str = "trace.fst") -> Path:
    path = tmp_path / name
    path.write_bytes(data)
    return path


def test_minimal_store_with_value_change_data_is_valid(tmp_path):
    assert _bwave_valid(_write(tmp_path, MINIMAL_FST_BYTES)) is True


def test_header_only_store_is_rejected(tmp_path):
    """The exact F-15 false pass: valid header, no data, run reported PASS."""
    assert _bwave_valid(_write(tmp_path, FST_HEADER_BYTES)) is False


def test_header_plus_padding_is_rejected(tmp_path):
    """443 bytes — the size the Ibex port actually saw accepted."""
    padded = FST_HEADER_BYTES + b"\x00" * (443 - len(FST_HEADER_BYTES))
    store = _write(tmp_path, padded)
    assert store.stat().st_size == 443
    assert _bwave_valid(store) is False


def test_non_data_blocks_alone_are_rejected(tmp_path):
    """Geometry/hierarchy without value changes is still an empty waveform."""
    geometry = bytes([3]) + (32).to_bytes(8, "big") + b"\x00" * 24
    hierarchy = bytes([4]) + (32).to_bytes(8, "big") + b"\x00" * 24
    assert _bwave_valid(_write(tmp_path, FST_HEADER_BYTES + geometry + hierarchy)) is False


def test_data_block_after_metadata_blocks_is_valid(tmp_path):
    """Real dumps put geometry/hierarchy first — the walk must reach the data."""
    geometry = bytes([3]) + (32).to_bytes(8, "big") + b"\x00" * 24
    hierarchy = bytes([4]) + (32).to_bytes(8, "big") + b"\x00" * 24
    store = _write(tmp_path, FST_HEADER_BYTES + geometry + hierarchy + FST_VCDATA_BYTES)
    assert _bwave_valid(store) is True


@pytest.mark.parametrize(
    ("label", "data"),
    [
        ("empty", b""),
        ("truncated", b"\x00\x00\x00"),
        ("wrong header length", bytes([0]) + (17).to_bytes(8, "big") + b"\x00" * 321),
        ("wrong block type", bytes([9]) + (329).to_bytes(8, "big") + b"\x00" * 321),
    ],
)
def test_malformed_stores_are_rejected(tmp_path, label, data):
    assert _bwave_valid(_write(tmp_path, data)) is False, label


def test_zero_length_block_does_not_loop_forever(tmp_path):
    """A corrupt chain must terminate, not spin on a self-referencing offset."""
    corrupt = FST_HEADER_BYTES + bytes([3]) + (0).to_bytes(8, "big")
    assert _bwave_valid(_write(tmp_path, corrupt)) is False


def test_missing_file_is_rejected(tmp_path):
    assert _bwave_valid(tmp_path / "absent.fst") is False
