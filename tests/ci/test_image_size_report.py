from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path

import pytest

SCRIPT = Path(__file__).parents[2] / ".github/scripts/image_size_report.py"
BASELINE = Path(__file__).parents[2] / ".github/evidence/docker-image-baseline-0.2.10-amd64.json"
LIMITS = Path(__file__).parents[2] / ".github/contracts/image-size-limits.toml"
SPEC = importlib.util.spec_from_file_location("image_size_report", SCRIPT)
assert SPEC is not None and SPEC.loader is not None
image_size_report = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = image_size_report
SPEC.loader.exec_module(image_size_report)


def _entry(architecture: str, layers: list[tuple[str, int]]) -> dict:
    return {
        "Descriptor": {
            "digest": f"sha256:{architecture}",
            "platform": {"os": "linux", "architecture": architecture},
        },
        "SchemaV2Manifest": {
            "layers": [{"digest": digest, "size": size} for digest, size in layers]
        },
    }


def _measurement(
    reference: str,
    local_size: int,
    registry_layers: dict[str, int] | None,
) -> object:
    registry = (
        image_size_report.RegistryMeasurement(
            "sha256:manifest",
            tuple(
                image_size_report.LayerMeasurement(digest, size)
                for digest, size in registry_layers.items()
            ),
        )
        if registry_layers is not None
        else None
    )
    return image_size_report.ImageMeasurement(
        reference=reference,
        image_id="sha256:image",
        os="linux",
        architecture="amd64",
        runtime_user="agent",
        repository_digests=(),
        docker_local_size_bytes=local_size,
        rootfs_diff_ids=("sha256:diff",),
        history_layer_bytes=(40, 60),
        merged_visible_filesystem_bytes=90,
        largest_directories=(image_size_report.DirectoryMeasurement("/usr", 50),),
        registry=registry,
    )


def test_compressed_layers_selects_linux_amd64_and_deduplicates() -> None:
    document = [
        _entry("arm64", [("sha256:arm", 30)]),
        _entry("amd64", [("sha256:base", 10), ("sha256:base", 10), ("sha256:app", 20)]),
    ]

    assert image_size_report.compressed_layers(document) == {
        "sha256:base": 10,
        "sha256:app": 20,
    }
    rows = image_size_report._compressed_layer_rows(
        document, os_name="linux", architecture="amd64"
    )
    assert [(row.digest, row.bytes) for row in rows] == [
        ("sha256:base", 10),
        ("sha256:base", 10),
        ("sha256:app", 20),
    ]


def test_registry_payload_distinguishes_manifest_descriptors_from_unique_blobs() -> None:
    registry = image_size_report.RegistryMeasurement(
        "sha256:manifest",
        (
            image_size_report.LayerMeasurement("sha256:empty", 32),
            image_size_report.LayerMeasurement("sha256:empty", 32),
            image_size_report.LayerMeasurement("sha256:payload", 100),
        ),
    )

    payload = image_size_report._registry_payload(registry)

    assert payload["compressed_layer_bytes"] == 164
    assert payload["compressed_layer_count"] == 3
    assert payload["unique_compressed_blob_bytes"] == 132
    assert payload["unique_compressed_blob_count"] == 2
    assert len(payload["layers"]) == 3


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


def test_registry_measurement_resolves_linux_amd64_child_from_oci_index(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    index_reference = "ghcr.io/example/image@sha256:index"
    child_reference = "ghcr.io/example/image@sha256:runtime"
    documents = {
        index_reference: {
            "schemaVersion": 2,
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
            "layers": [{"digest": "sha256:layer", "size": 42}],
        },
    }
    calls: list[list[str]] = []

    def docker_output(argv: list[str], **_kwargs: object) -> str:
        calls.append(argv)
        return json.dumps(documents[argv[-1]])

    monkeypatch.setattr(image_size_report, "_docker_output", docker_output)

    assert image_size_report._registry_measurement(
        index_reference, os_name="linux", architecture="amd64"
    ) == image_size_report.RegistryMeasurement(
        "sha256:runtime", (image_size_report.LayerMeasurement("sha256:layer", 42),)
    )
    assert [call[-1] for call in calls] == [index_reference, child_reference]


def test_measure_records_exact_local_history_and_identity(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    inspect = [
        {
            "Id": "sha256:image",
            "Os": "linux",
            "Architecture": "amd64",
            "Config": {"User": "agent"},
            "Size": 123,
            "RepoDigests": ["example@sha256:manifest"],
            "RootFS": {"Layers": ["sha256:one", "sha256:two"]},
        }
    ]

    def docker_output(argv: list[str], **_kwargs: object) -> str:
        if argv[:2] == ["image", "inspect"]:
            return json.dumps(inspect)
        assert argv[:4] == ["history", "--no-trunc", "--human=false", "--format"]
        return '{"Size":"100"}\n{"Size":"23"}\n'

    monkeypatch.setattr(image_size_report, "_docker_output", docker_output)
    measured = image_size_report.measure("example", registry_manifest=False)

    assert measured.docker_local_size_bytes == 123
    assert measured.history_layer_bytes == (100, 23)
    assert measured.rootfs_diff_ids == ("sha256:one", "sha256:two")
    assert measured.repository_digests == ("example@sha256:manifest",)


@pytest.mark.parametrize("config", ({}, {"User": ""}))
def test_measure_accepts_default_root_runtime_user(
    monkeypatch: pytest.MonkeyPatch,
    config: dict[str, str],
) -> None:
    inspect = [
        {
            "Id": "sha256:image",
            "Os": "linux",
            "Architecture": "amd64",
            "Config": config,
            "Size": 123,
            "RepoDigests": [],
            "RootFS": {"Layers": ["sha256:one"]},
        }
    ]

    def docker_output(argv: list[str], **_kwargs: object) -> str:
        if argv[:2] == ["image", "inspect"]:
            return json.dumps(inspect)
        return '{"Size":"123"}\n'

    monkeypatch.setattr(image_size_report, "_docker_output", docker_output)

    measured = image_size_report.measure("helper", registry_manifest=False)

    assert measured.runtime_user == ""


def test_filesystem_measurement_keeps_exact_root_and_largest_directories(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        image_size_report,
        "_docker_output",
        lambda *_args, **_kwargs: "25\t/usr/bin\n100\t/\n60\t/usr\n30\t/opt\n",
    )

    visible, largest = image_size_report._filesystem_measurement("example")

    assert visible == 100
    assert [(row.path, row.bytes) for row in largest] == [
        ("/usr", 60),
        ("/opt", 30),
        ("/usr/bin", 25),
    ]


def test_measurement_environment_records_engine_components_and_storage(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    responses = {
        "version": json.dumps(
            {
                "Client": {"Version": "29.0"},
                "Server": {
                    "Version": "29.1",
                    "Components": [{"Name": "containerd", "Version": "v2.3"}],
                },
            }
        ),
        "info": json.dumps(
            {"Driver": "overlayfs", "DriverStatus": [["driver-type", "containerd"]]}
        ),
        "buildx-version": "github.com/docker/buildx v0.36 deadbeef\n",
        "buildx-inspect": "Nodes:\nBuildKit version: v0.32.2\n",
    }

    def docker_output(argv: list[str], **_kwargs: object) -> str:
        if argv[0] == "version":
            return responses["version"]
        if argv[0] == "info":
            return responses["info"]
        if argv == ["buildx", "version"]:
            return responses["buildx-version"]
        return responses["buildx-inspect"]

    monkeypatch.setattr(image_size_report, "_docker_output", docker_output)

    assert image_size_report.measurement_environment() == {
        "docker_client_version": "29.0",
        "docker_server_version": "29.1",
        "containerd_version": "v2.3",
        "buildx_version": "github.com/docker/buildx v0.36 deadbeef",
        "buildkit_versions": ["v0.32.2"],
        "storage_driver": "overlayfs",
        "storage_driver_status": [["driver-type", "containerd"]],
    }


def test_report_keeps_metrics_distinct_and_deduplicates_registry_layers() -> None:
    payload = image_size_report.report(
        {
            "sandbox": _measurement("base", 100, {"sha256:shared": 10, "sha256:base": 20}),
            "riscv": _measurement("riscv", 180, {"sha256:shared": 10, "sha256:riscv": 30}),
            "proxy": _measurement("proxy", 5, None),
        },
        environment={"storage_driver": "overlayfs"},
        measured_at="2026-09-02T00:00:00Z",
    )

    assert payload["registry_set"] == {
        "unique_compressed_layer_bytes": 60,
        "unique_compressed_layer_count": 3,
        "riscv_incremental_compressed_layer_bytes": 30,
    }
    assert payload["images"]["proxy"]["registry"] is None
    assert payload["images"]["sandbox"]["docker_local_size_bytes"] == 100
    assert payload["images"]["sandbox"]["unpacked_layer_history_bytes"] == 100
    assert payload["images"]["sandbox"]["merged_visible_filesystem_bytes"] == 90
    rendered = image_size_report.markdown(payload)
    assert "Registry layers" in rendered
    assert "must not be added" in rendered


def test_size_limits_cover_local_and_registry_storage_views() -> None:
    payload = image_size_report.report(
        {
            "sandbox": _measurement("base", 100, {"sha256:base": 20}),
            "riscv": _measurement("riscv", 180, {"sha256:riscv": 30}),
        }
    )
    limits = {
        "sandbox": {
            "docker_local_size_bytes": 100,
            "unpacked_layer_history_bytes": 100,
            "merged_visible_filesystem_bytes": 90,
            "registry_compressed_layer_bytes": 20,
        },
        "riscv": {
            "docker_local_size_bytes": 200,
            "unpacked_layer_history_bytes": 80,
            "merged_visible_filesystem_bytes": 100,
            "registry_compressed_layer_bytes": 40,
        },
    }

    errors = image_size_report.apply_size_limits(payload, limits)

    assert errors == ["riscv unpacked_layer_history_bytes is 20 bytes over its 80-byte ceiling"]
    assert payload["size_limits"]["passed"] is False
    assert payload["size_limits"]["images"]["sandbox"]["merged_visible_filesystem_bytes"] == {
        "actual_bytes": 90,
        "max_bytes": 90,
        "passed": True,
    }


def test_size_limits_fail_closed_when_a_named_image_is_not_measured() -> None:
    payload = image_size_report.report({"sandbox": _measurement("base", 100, None)})

    errors = image_size_report.apply_size_limits(
        payload, {"riscv": {"docker_local_size_bytes": 200}}
    )

    assert errors == ["size-limited image was not measured: riscv"]


def test_runtime_measurement_skips_registry_only_limit() -> None:
    payload = image_size_report.report({"sandbox": _measurement("base", 100, None)})

    errors = image_size_report.apply_size_limits(
        payload,
        {
            "sandbox": {
                "docker_local_size_bytes": 100,
                "registry_compressed_layer_bytes": 1,
            }
        },
    )

    assert errors == []
    assert (
        payload["size_limits"]["images"]["sandbox"]["registry_compressed_layer_bytes"]["status"]
        == "not-measured"
    )


def test_committed_size_limits_bound_both_runtime_images_below_baseline() -> None:
    limits = image_size_report._load_size_limits(LIMITS)
    baseline = json.loads(BASELINE.read_text(encoding="utf-8"))

    assert set(limits) == {"sandbox", "riscv"}
    for name, image_limits in limits.items():
        assert set(image_limits) == {
            "registry_compressed_layer_bytes",
            "docker_local_size_bytes",
            "unpacked_layer_history_bytes",
            "merged_visible_filesystem_bytes",
        }
        baseline_image = baseline["images"][name]
        assert (
            image_limits["registry_compressed_layer_bytes"]
            < baseline_image["registry"]["compressed_layer_bytes"]
        )
        for metric in (
            "docker_local_size_bytes",
            "unpacked_layer_history_bytes",
            "merged_visible_filesystem_bytes",
        ):
            assert image_limits[metric] < baseline_image[metric]


def test_local_size_limits_cover_docker_engine_unpacked_storage_view() -> None:
    limits = image_size_report._load_size_limits(LIMITS)

    assert limits["sandbox"]["docker_local_size_bytes"] == 3_500_000_000
    assert limits["riscv"]["docker_local_size_bytes"] == 5_350_000_000


def test_committed_baseline_preserves_exact_bytes_and_environment() -> None:
    baseline = json.loads(BASELINE.read_text(encoding="utf-8"))
    sandbox = baseline["images"]["sandbox"]
    riscv = baseline["images"]["riscv"]

    assert baseline["schema"] == 1
    assert baseline["measured_at"].endswith("Z")
    assert baseline["environment"]["containerd_version"]
    assert baseline["environment"]["buildkit_versions"]
    assert baseline["environment"]["storage_driver_status"]
    assert sandbox["registry"]["compressed_layer_bytes"] == 4_327_757_023
    assert sandbox["registry"]["unique_compressed_blob_bytes"] == 4_327_756_927
    assert sandbox["docker_local_size_bytes"] == 4_327_809_677
    assert sandbox["unpacked_layer_history_bytes"] == 10_344_845_312
    assert sandbox["merged_visible_filesystem_bytes"] == 9_663_725_568
    assert riscv["registry"]["compressed_layer_bytes"] == 5_533_441_890
    assert riscv["registry"]["unique_compressed_blob_bytes"] == 5_533_441_730
    assert riscv["merged_visible_filesystem_bytes"] == 13_169_491_968
