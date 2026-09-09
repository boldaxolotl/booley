"""Post-stop fault controls preserve restoration bytes and reject escaped paths."""

import importlib.util
from pathlib import Path

import pytest


def test_missing_bind_fault_preserves_exact_bytes_and_rejects_escape(tmp_path):
    path = (
        Path(__file__).resolve().parents[2]
        / "qa/scenarios/uart/probes/legacy-client/docker_fixture.py"
    )
    spec = importlib.util.spec_from_file_location("legacy_fixture", path)
    fixture = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(fixture)
    root = tmp_path / "owned"
    root.mkdir()
    original = root / "bind"
    original.write_bytes(b"controlled bind contents")
    fixture.inject(root, "bind")
    assert not original.exists()
    assert (root / "bind.qa-restoration").read_bytes() == b"controlled bind contents"
    (root / "bind.qa-restoration").rename(original)
    assert original.read_bytes() == b"controlled bind contents"
    outside = tmp_path / "borrowed"
    outside.write_bytes(b"unchanged")
    with pytest.raises(ValueError):
        fixture.inject(root, "../borrowed")
    assert outside.read_bytes() == b"unchanged"
