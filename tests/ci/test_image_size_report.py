from __future__ import annotations

import importlib.util
import json
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


def test_compressed_layers_reads_verbose_oci_manifest() -> None:
    document = [
        {
            "Descriptor": {"platform": {"os": "linux", "architecture": "amd64"}},
            "OCIManifest": {"layers": [{"digest": "sha256:runtime", "size": 42}]},
        },
        {
            "Descriptor": {"platform": {"os": "unknown", "architecture": "unknown"}},
            "OCIManifest": {"layers": [{"digest": "sha256:attestation", "size": 7}]},
        },
    ]

    assert image_size_report.compressed_layers(document) == {"sha256:runtime": 42}


def test_measure_resolves_linux_amd64_child_from_oci_index(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    index_reference = "ghcr.io/example/image@sha256:index"
    child_reference = "ghcr.io/example/image@sha256:runtime"
    documents = {
        index_reference: {
            "schemaVersion": 2,
            "mediaType": "application/vnd.oci.image.index.v1+json",
            "manifests": [
                {
                    "digest": "sha256:runtime",
                    "platform": {"os": "linux", "architecture": "amd64"},
                },
                {
                    "digest": "sha256:attestation",
                    "platform": {"os": "unknown", "architecture": "unknown"},
                },
            ],
        },
        child_reference: {
            "schemaVersion": 2,
            "mediaType": "application/vnd.oci.image.manifest.v1+json",
            "layers": [{"digest": "sha256:layer", "size": 42}],
        },
    }
    calls: list[list[str]] = []

    def docker_output(argv: list[str]) -> str:
        calls.append(argv)
        if argv[:2] == ["image", "inspect"]:
            return "123\n"
        return json.dumps(documents[argv[-1]])

    monkeypatch.setattr(image_size_report, "_docker_output", docker_output)

    assert image_size_report.measure(index_reference, registry_manifest=True) == (
        image_size_report.ImageMeasurement(index_reference, 123, {"sha256:layer": 42})
    )
    assert calls == [
        ["image", "inspect", "--format", "{{.Size}}", index_reference],
        ["manifest", "inspect", "--verbose", index_reference],
        ["manifest", "inspect", "--verbose", child_reference],
    ]


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
