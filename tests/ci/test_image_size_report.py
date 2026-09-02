from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest

SCRIPT = Path(__file__).parents[2] / ".github/scripts/image_size_report.py"
SPEC = importlib.util.spec_from_file_location("image_size_report", SCRIPT)
assert SPEC is not None and SPEC.loader is not None
image_size_report = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = image_size_report
SPEC.loader.exec_module(image_size_report)


def _entry(architecture: str, layers: list[tuple[str, int]]) -> dict:
    return {
        "Descriptor": {"platform": {"os": "linux", "architecture": architecture}},
        "SchemaV2Manifest": {
            "layers": [{"digest": digest, "size": size} for digest, size in layers]
        },
    }


def test_compressed_layers_selects_linux_amd64_and_deduplicates() -> None:
    document = [
        _entry("arm64", [("sha256:arm", 30)]),
        _entry("amd64", [("sha256:base", 10), ("sha256:base", 10), ("sha256:app", 20)]),
    ]

    assert image_size_report.compressed_layers(document) == {
        "sha256:base": 10,
        "sha256:app": 20,
    }


def test_compressed_layers_rejects_conflicting_duplicate_digest() -> None:
    with pytest.raises(ValueError, match="conflicting sizes"):
        image_size_report.compressed_layers(
            _entry("amd64", [("sha256:same", 10), ("sha256:same", 11)])
        )


def test_report_deduplicates_shared_registry_layers_and_keeps_sidecars_separate() -> None:
    measurement = image_size_report.ImageMeasurement
    payload = image_size_report.report(
        {
            "sandbox": measurement("base", 100, {"sha256:shared": 10, "sha256:base": 20}),
            "riscv": measurement("riscv", 180, {"sha256:shared": 10, "sha256:riscv": 30}),
            "proxy": measurement("proxy", 5, None),
        }
    )

    assert payload["registry_set"] == {
        "unique_compressed_layer_bytes": 60,
        "unique_compressed_layer_count": 3,
        "riscv_incremental_compressed_layer_bytes": 30,
    }
    assert payload["images"]["proxy"]["compressed_layer_bytes"] is None
    rendered = image_size_report.markdown(payload)
    assert "Compressed layers" in rendered
    assert "Docker metadata" in rendered
