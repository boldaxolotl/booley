#!/usr/bin/env python3
"""Measure release-image transfer layers and local Docker virtual sizes."""

from __future__ import annotations

import argparse
import json
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Any


@dataclass(frozen=True)
class ImageMeasurement:
    reference: str
    virtual_bytes: int
    layers: dict[str, int] | None


def _require_mapping(value: object, label: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise ValueError(f"{label} must be a JSON object")
    return value


def _manifest_entry(document: object, os_name: str, architecture: str) -> dict[str, Any]:
    entries = document if isinstance(document, list) else [document]
    for raw in entries:
        entry = _require_mapping(raw, "manifest entry")
        descriptor = _require_mapping(entry.get("Descriptor", {}), "manifest descriptor")
        platform = _require_mapping(descriptor.get("platform", {}), "manifest platform")
        if not platform or (
            platform.get("os") == os_name and platform.get("architecture") == architecture
        ):
            return entry
    raise ValueError(f"manifest has no {os_name}/{architecture} entry")


def compressed_layers(
    document: object, *, os_name: str = "linux", architecture: str = "amd64"
) -> dict[str, int]:
    """Return unique compressed layer sizes from verbose manifest JSON."""
    entry = _manifest_entry(document, os_name, architecture)
    manifest = entry.get("SchemaV2Manifest", entry)
    layers = _require_mapping(manifest, "image manifest").get("layers")
    if not isinstance(layers, list):
        raise ValueError("image manifest layers must be a list")
    result: dict[str, int] = {}
    for raw in layers:
        layer = _require_mapping(raw, "image layer")
        digest, size = layer.get("digest"), layer.get("size")
        if (
            not isinstance(digest, str)
            or not digest
            or not isinstance(size, int)
            or isinstance(size, bool)
        ):
            raise ValueError("image layer needs a digest and integer byte size")
        if digest in result and result[digest] != size:
            raise ValueError(f"layer {digest} has conflicting sizes")
        result[digest] = size
    return result


def _docker_output(argv: list[str]) -> str:
    result = subprocess.run(
        ["docker", *argv], capture_output=True, text=True, check=False, timeout=120
    )
    if result.returncode != 0:
        detail = result.stderr.strip() or result.stdout.strip() or "docker command failed"
        raise RuntimeError(detail)
    return result.stdout


def measure(reference: str, *, registry_manifest: bool) -> ImageMeasurement:
    virtual = int(_docker_output(["image", "inspect", "--format", "{{.Size}}", reference]))
    layers = None
    if registry_manifest:
        raw = _docker_output(["manifest", "inspect", "--verbose", reference])
        layers = compressed_layers(json.loads(raw))
    return ImageMeasurement(reference, virtual, layers)


def _image_payload(measurement: ImageMeasurement) -> dict[str, object]:
    layers = measurement.layers
    return {
        "reference": measurement.reference,
        "virtual_bytes": measurement.virtual_bytes,
        "compressed_layer_bytes": sum(layers.values()) if layers is not None else None,
        "compressed_layer_count": len(layers) if layers is not None else None,
    }


def _merged_layers(layer_sets: list[dict[str, int]]) -> dict[str, int]:
    merged: dict[str, int] = {}
    for layers in layer_sets:
        for digest, size in layers.items():
            if digest in merged and merged[digest] != size:
                raise ValueError(f"layer {digest} has conflicting cross-image sizes")
            merged[digest] = size
    return merged


def report(measurements: dict[str, ImageMeasurement]) -> dict[str, object]:
    registry_layers = [row.layers for row in measurements.values() if row.layers is not None]
    unique = _merged_layers(registry_layers)
    base = measurements.get("sandbox")
    riscv = measurements.get("riscv")
    incremental = None
    if (
        base is not None
        and riscv is not None
        and base.layers is not None
        and riscv.layers is not None
    ):
        incremental = sum(
            size for digest, size in riscv.layers.items() if digest not in base.layers
        )
    return {
        "units": {"GB": 1_000_000_000, "GiB": 1_073_741_824},
        "images": {name: _image_payload(row) for name, row in measurements.items()},
        "registry_set": {
            "unique_compressed_layer_bytes": sum(unique.values()),
            "unique_compressed_layer_count": len(unique),
            "riscv_incremental_compressed_layer_bytes": incremental,
        },
    }


def _size_text(size: int | None) -> str:
    if size is None:
        return "n/a"
    return f"{size / 1_000_000_000:.2f} GB / {size / 1_073_741_824:.2f} GiB"


def markdown(payload: dict[str, object]) -> str:
    images = _require_mapping(payload.get("images"), "report images")
    lines = [
        "## Release image sizes",
        "",
        "| Image | Compressed layers | Virtual size |",
        "| --- | ---: | ---: |",
    ]
    for name, raw in images.items():
        image = _require_mapping(raw, f"image {name}")
        lines.append(
            f"| {name} | {_size_text(image.get('compressed_layer_bytes'))} | "
            f"{_size_text(image.get('virtual_bytes'))} |"
        )
    registry = _require_mapping(payload.get("registry_set"), "registry set")
    lines.extend(
        [
            "",
            "Unique compressed layers across registry images: "
            f"{_size_text(registry.get('unique_compressed_layer_bytes'))}.",
            "RISC-V compressed layers beyond the standard sandbox: "
            f"{_size_text(registry.get('riscv_incremental_compressed_layer_bytes'))}.",
            "",
            "Virtual sizes exclude Docker metadata, build cache, container writable layers, and project artifacts.",
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
    parser.add_argument("--registry-image", action="append", default=[], type=_named_reference)
    parser.add_argument("--local-image", action="append", default=[], type=_named_reference)
    parser.add_argument("--json", required=True, type=Path)
    parser.add_argument("--markdown", required=True, type=Path)
    args = parser.parse_args()
    measurements = {
        name: measure(reference, registry_manifest=True) for name, reference in args.registry_image
    }
    measurements.update(
        {name: measure(reference, registry_manifest=False) for name, reference in args.local_image}
    )
    payload = report(measurements)
    args.json.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
    args.markdown.write_text(markdown(payload), encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
