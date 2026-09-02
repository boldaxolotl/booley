#!/usr/bin/env python3
"""Collect exact-byte Docker image storage evidence."""

from __future__ import annotations

import argparse
import json
import re
import subprocess
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from booley.core.boundary import (
    as_dict,
    as_int,
    require_dict,
    require_int,
    require_list,
    require_str,
)


@dataclass(frozen=True)
class LayerMeasurement:
    digest: str
    bytes: int


@dataclass(frozen=True)
class RegistryMeasurement:
    platform_manifest_digest: str
    layers: tuple[LayerMeasurement, ...]


@dataclass(frozen=True)
class DirectoryMeasurement:
    path: str
    bytes: int


@dataclass(frozen=True)
class ImageMeasurement:
    reference: str
    image_id: str
    os: str
    architecture: str
    runtime_user: str
    repository_digests: tuple[str, ...]
    docker_local_size_bytes: int
    rootfs_diff_ids: tuple[str, ...]
    history_layer_bytes: tuple[int, ...]
    merged_visible_filesystem_bytes: int | None
    largest_directories: tuple[DirectoryMeasurement, ...]
    registry: RegistryMeasurement | None


def _manifest_entry(document: object, os_name: str, architecture: str) -> dict:
    mapping = as_dict(document)
    entries = [mapping] if mapping is not None else require_list(document, field="manifest")
    for raw in entries:
        entry = require_dict(raw, field="manifest entry")
        descriptor = require_dict(entry.get("Descriptor", {}), field="manifest descriptor")
        platform = require_dict(descriptor.get("platform", {}), field="manifest platform")
        if not platform or (
            platform.get("os") == os_name and platform.get("architecture") == architecture
        ):
            return entry
    raise ValueError(f"manifest has no {os_name}/{architecture} entry")


def compressed_layers(
    document: object, *, os_name: str = "linux", architecture: str = "amd64"
) -> dict[str, int]:
    """Return unique compressed layer sizes from verbose manifest JSON."""
    result: dict[str, int] = {}
    for layer in _compressed_layer_rows(document, os_name=os_name, architecture=architecture):
        if layer.digest in result and result[layer.digest] != layer.bytes:
            raise ValueError(f"layer {layer.digest} has conflicting sizes")
        result[layer.digest] = layer.bytes
    return result


def _compressed_layer_rows(
    document: object, *, os_name: str, architecture: str
) -> tuple[LayerMeasurement, ...]:
    entry = _manifest_entry(document, os_name, architecture)
    manifest = entry.get("SchemaV2Manifest", entry.get("OCIManifest", entry))
    layers = require_list(
        require_dict(manifest, field="image manifest").get("layers"),
        field="image manifest layers",
    )
    rows: list[LayerMeasurement] = []
    for raw in layers:
        layer = require_dict(raw, field="image layer")
        digest = require_str(layer, "digest")
        size = require_int(layer.get("size"), field=f"image layer {digest} size")
        rows.append(LayerMeasurement(digest, size))
    return tuple(rows)


def _index_child_reference(
    reference: str, document: object, *, os_name: str, architecture: str
) -> str | None:
    mapping = as_dict(document)
    if mapping is None or "manifests" not in mapping:
        return None
    descriptors = require_list(mapping.get("manifests"), field="image index manifests")
    for raw in descriptors:
        descriptor = require_dict(raw, field="image index manifest descriptor")
        platform = require_dict(
            descriptor.get("platform", {}), field="image index manifest platform"
        )
        if platform.get("os") == os_name and platform.get("architecture") == architecture:
            digest = require_str(descriptor, "digest")
            repository, separator, _ = reference.rpartition("@")
            return f"{repository if separator else reference}@{digest}"
    raise ValueError(f"image index has no {os_name}/{architecture} manifest")


def _docker_output(argv: list[str], *, timeout: int = 120) -> str:
    result = subprocess.run(
        ["docker", *argv], capture_output=True, text=True, check=False, timeout=timeout
    )
    if result.returncode != 0:
        detail = result.stderr.strip() or result.stdout.strip() or "docker command failed"
        raise RuntimeError(detail)
    return result.stdout


def _registry_measurement(
    reference: str, *, os_name: str, architecture: str
) -> RegistryMeasurement:
    document = json.loads(_docker_output(["manifest", "inspect", "--verbose", reference]))
    child_reference = _index_child_reference(
        reference, document, os_name=os_name, architecture=architecture
    )
    if child_reference is not None:
        document = json.loads(
            _docker_output(["manifest", "inspect", "--verbose", child_reference])
        )
        digest = child_reference.rpartition("@")[2]
    else:
        entry = _manifest_entry(document, os_name, architecture)
        descriptor = require_dict(entry.get("Descriptor", {}), field="manifest descriptor")
        digest = str(descriptor.get("digest") or reference.rpartition("@")[2])
    if not digest.startswith("sha256:"):
        raise ValueError(f"manifest did not expose a platform digest for {reference!r}")
    return RegistryMeasurement(
        digest,
        _compressed_layer_rows(document, os_name=os_name, architecture=architecture),
    )


def _image_inspect(reference: str) -> dict:
    rows = require_list(
        json.loads(_docker_output(["image", "inspect", reference])), field="image inspect"
    )
    if len(rows) != 1:
        raise ValueError(f"Docker returned {len(rows)} inspect rows for {reference!r}")
    return require_dict(rows[0], field="image inspect row")


def _history_layer_bytes(reference: str) -> tuple[int, ...]:
    raw = _docker_output(
        ["history", "--no-trunc", "--human=false", "--format", "{{json .}}", reference]
    )
    sizes: list[int] = []
    for line in raw.splitlines():
        row = require_dict(json.loads(line), field="image history row")
        size = as_int(row.get("Size"))
        if size is None or size < 0:
            raise ValueError(f"Docker returned an invalid history size for {reference!r}")
        sizes.append(size)
    if not sizes:
        raise ValueError(f"Docker returned no image history for {reference!r}")
    return tuple(sizes)


def _filesystem_measurement(
    reference: str,
) -> tuple[int, tuple[DirectoryMeasurement, ...]]:
    raw = _docker_output(
        [
            "run",
            "--rm",
            "--network",
            "none",
            "--entrypoint",
            "sh",
            reference,
            "-c",
            'status=0; du -x -B1 -d2 / 2>/dev/null || status=$?; test "$status" -le 1',
        ],
        timeout=300,
    )
    directories: list[DirectoryMeasurement] = []
    for line in raw.splitlines():
        size_text, separator, path = line.partition("\t")
        size = as_int(size_text)
        if not separator or size is None or size < 0 or not path.startswith("/"):
            raise ValueError(f"Docker returned an invalid du row for {reference!r}: {line!r}")
        directories.append(DirectoryMeasurement(path, size))
    roots = [row.bytes for row in directories if row.path == "/"]
    if len(roots) != 1:
        raise ValueError(f"Docker returned {len(roots)} root totals for {reference!r}")
    largest = sorted((row for row in directories if row.path != "/"), key=lambda row: -row.bytes)
    return roots[0], tuple(largest[:25])


def measure(
    reference: str, *, registry_manifest: bool, visible_filesystem: bool = False
) -> ImageMeasurement:
    inspected = _image_inspect(reference)
    architecture = require_str(inspected, "Architecture")
    os_name = require_str(inspected, "Os")
    rootfs = require_dict(inspected.get("RootFS"), field="image RootFS")
    config = require_dict(inspected.get("Config"), field="image Config")
    diff_ids = tuple(
        require_str({"value": value}, "value")
        for value in require_list(rootfs.get("Layers"), field="image RootFS layers")
    )
    visible, directories = _filesystem_measurement(reference) if visible_filesystem else (None, ())
    registry = (
        _registry_measurement(reference, os_name=os_name, architecture=architecture)
        if registry_manifest
        else None
    )
    return ImageMeasurement(
        reference=reference,
        image_id=require_str(inspected, "Id"),
        os=os_name,
        architecture=architecture,
        runtime_user=require_str(config, "User"),
        repository_digests=tuple(
            require_str({"value": value}, "value")
            for value in require_list(
                inspected.get("RepoDigests") or [], field="image repository digests"
            )
        ),
        docker_local_size_bytes=require_int(inspected.get("Size"), field="image Size"),
        rootfs_diff_ids=diff_ids,
        history_layer_bytes=_history_layer_bytes(reference),
        merged_visible_filesystem_bytes=visible,
        largest_directories=directories,
        registry=registry,
    )


def _component_version(server: dict, name: str) -> str:
    for raw in require_list(server.get("Components", []), field="Docker server components"):
        component = require_dict(raw, field="Docker server component")
        if component.get("Name") == name:
            return require_str(component, "Version")
    raise ValueError(f"Docker server did not report component {name!r}")


def measurement_environment() -> dict[str, object]:
    version = require_dict(
        json.loads(_docker_output(["version", "--format", "{{json .}}"])),
        field="Docker version",
    )
    client = require_dict(version.get("Client"), field="Docker client version")
    server = require_dict(version.get("Server"), field="Docker server version")
    info = require_dict(
        json.loads(_docker_output(["info", "--format", "{{json .}}"])), field="Docker info"
    )
    buildx_version = _docker_output(["buildx", "version"]).strip()
    builder = _docker_output(["buildx", "inspect", "--bootstrap"]).strip()
    buildkit_versions = sorted(set(re.findall(r"BuildKit version:\s*(\S+)", builder)))
    if not buildkit_versions:
        raise ValueError("docker buildx inspect did not report a BuildKit version")
    return {
        "docker_client_version": require_str(client, "Version"),
        "docker_server_version": require_str(server, "Version"),
        "containerd_version": _component_version(server, "containerd"),
        "buildx_version": buildx_version,
        "buildkit_versions": buildkit_versions,
        "storage_driver": require_str(info, "Driver"),
        "storage_driver_status": require_list(
            info.get("DriverStatus") or [], field="Docker storage driver status"
        ),
    }


def _registry_payload(registry: RegistryMeasurement | None) -> dict[str, object] | None:
    if registry is None:
        return None
    unique = _unique_layers(registry.layers)
    return {
        "platform_manifest_digest": registry.platform_manifest_digest,
        "compressed_layer_bytes": sum(layer.bytes for layer in registry.layers),
        "compressed_layer_count": len(registry.layers),
        "unique_compressed_blob_bytes": sum(unique.values()),
        "unique_compressed_blob_count": len(unique),
        "layers": [layer.__dict__ for layer in registry.layers],
    }


def _image_payload(measurement: ImageMeasurement) -> dict[str, object]:
    return {
        "reference": measurement.reference,
        "image_id": measurement.image_id,
        "os": measurement.os,
        "architecture": measurement.architecture,
        "runtime_user": measurement.runtime_user,
        "repository_digests": list(measurement.repository_digests),
        "docker_local_size_bytes": measurement.docker_local_size_bytes,
        "unpacked_layer_history_bytes": sum(measurement.history_layer_bytes),
        "history_layer_count": len(measurement.history_layer_bytes),
        "history_layer_bytes": list(measurement.history_layer_bytes),
        "rootfs_layer_count": len(measurement.rootfs_diff_ids),
        "rootfs_diff_ids": list(measurement.rootfs_diff_ids),
        "merged_visible_filesystem_bytes": measurement.merged_visible_filesystem_bytes,
        "largest_directories": [row.__dict__ for row in measurement.largest_directories],
        "registry": _registry_payload(measurement.registry),
    }


def _merged_layers(layer_sets: list[dict[str, int]]) -> dict[str, int]:
    merged: dict[str, int] = {}
    for layers in layer_sets:
        for digest, size in layers.items():
            if digest in merged and merged[digest] != size:
                raise ValueError(f"layer {digest} has conflicting cross-image sizes")
            merged[digest] = size
    return merged


def _unique_layers(layers: tuple[LayerMeasurement, ...]) -> dict[str, int]:
    unique: dict[str, int] = {}
    for layer in layers:
        if layer.digest in unique and unique[layer.digest] != layer.bytes:
            raise ValueError(f"layer {layer.digest} has conflicting sizes")
        unique[layer.digest] = layer.bytes
    return unique


def report(
    measurements: dict[str, ImageMeasurement],
    *,
    environment: dict[str, object] | None = None,
    measured_at: str = "1970-01-01T00:00:00Z",
) -> dict[str, object]:
    registry_rows = [row.registry for row in measurements.values() if row.registry is not None]
    unique = _merged_layers([_unique_layers(row.layers) for row in registry_rows])
    sandbox = measurements.get("sandbox")
    riscv = measurements.get("riscv")
    incremental = None
    if sandbox and riscv and sandbox.registry and riscv.registry:
        sandbox_layers = _unique_layers(sandbox.registry.layers)
        incremental = sum(
            size
            for digest, size in _unique_layers(riscv.registry.layers).items()
            if digest not in sandbox_layers
        )
    return {
        "schema": 1,
        "measured_at": measured_at,
        "units": {"GB": 1_000_000_000, "GiB": 1_073_741_824},
        "environment": environment or {},
        "images": {name: _image_payload(row) for name, row in measurements.items()},
        "registry_set": {
            "unique_compressed_layer_bytes": sum(unique.values()) if registry_rows else None,
            "unique_compressed_layer_count": len(unique) if registry_rows else None,
            "riscv_incremental_compressed_layer_bytes": incremental,
        },
    }


def _size_text(size: object) -> str:
    if not isinstance(size, int):
        return "n/a"
    return f"{size / 1_000_000_000:.2f} GB / {size / 1_073_741_824:.2f} GiB"


def markdown(payload: dict[str, object]) -> str:
    images = require_dict(payload.get("images"), field="report images")
    lines = [
        "## Release image sizes",
        "",
        "| Image | Registry layers | Docker `.Size` | Unpacked history | Visible filesystem |",
        "| --- | ---: | ---: | ---: | ---: |",
    ]
    for name, raw in images.items():
        image = require_dict(raw, field=f"image {name}")
        registry = as_dict(image.get("registry")) or {}
        lines.append(
            f"| {name} | {_size_text(registry.get('compressed_layer_bytes'))} | "
            f"{_size_text(image.get('docker_local_size_bytes'))} | "
            f"{_size_text(image.get('unpacked_layer_history_bytes'))} | "
            f"{_size_text(image.get('merged_visible_filesystem_bytes'))} |"
        )
    registry_set = require_dict(payload.get("registry_set"), field="registry set")
    lines.extend(
        [
            "",
            "Unique compressed layers across registry images: "
            f"{_size_text(registry_set.get('unique_compressed_layer_bytes'))}.",
            "RISC-V compressed layers beyond the standard sandbox: "
            f"{_size_text(registry_set.get('riscv_incremental_compressed_layer_bytes'))}.",
            "",
            "These metrics are distinct storage representations and must not be added. "
            "They exclude Docker metadata, build cache, writable layers, and project artifacts.",
        ]
    )
    return "\n".join(lines) + "\n"


def _named_reference(value: str) -> tuple[str, str]:
    name, separator, reference = value.partition("=")
    if not separator or not name or not reference:
        raise argparse.ArgumentTypeError("image must be NAME=REFERENCE")
    return name, reference


def _timestamp() -> str:
    return datetime.now(UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--registry-image", action="append", default=[], type=_named_reference)
    parser.add_argument("--runtime-image", action="append", default=[], type=_named_reference)
    parser.add_argument("--local-image", action="append", default=[], type=_named_reference)
    parser.add_argument("--json", required=True, type=Path)
    parser.add_argument("--markdown", required=True, type=Path)
    args = parser.parse_args()
    measurements = {
        name: measure(reference, registry_manifest=True, visible_filesystem=True)
        for name, reference in args.registry_image
    }
    measurements.update(
        {
            name: measure(reference, registry_manifest=False, visible_filesystem=True)
            for name, reference in args.runtime_image
        }
    )
    measurements.update(
        {name: measure(reference, registry_manifest=False) for name, reference in args.local_image}
    )
    payload = report(
        measurements,
        environment=measurement_environment(),
        measured_at=_timestamp(),
    )
    args.json.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
    args.markdown.write_text(markdown(payload), encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
