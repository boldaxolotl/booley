"""Sandbox Image selection and embedded Booley identity contracts."""

from __future__ import annotations

from booley.runtime import image_identity as identity


def _metadata(
    reference: str,
    *,
    image_id: str,
    labels: dict[str, str],
    environment: dict[str, str] | None = None,
) -> identity.ImageMetadata:
    return identity.ImageMetadata(reference, image_id, labels, environment or {})


def test_unknown_build_values_are_unavailable() -> None:
    metadata = _metadata(
        "issued",
        image_id="sha256:issued",
        labels={
            "org.opencontainers.image.version": "unknown",
            "org.opencontainers.image.revision": "",
            "io.booley.wheel.source-fingerprint": "unknown",
        },
    )

    assert identity.build_identity(metadata) == identity.BooleyBuildIdentity(
        None, None, None, None
    )


def test_wheel_source_fingerprint_decides_even_when_revision_differs() -> None:
    expected = identity.BooleyBuildIdentity("1.0", "abc123", None, "wheel")
    observed = identity.BooleyBuildIdentity("1.0", "def456", None, "wheel")

    assert identity.compare_build_identity(expected, observed).status is identity.Status.MATCH


def test_typed_payload_fingerprint_compares_only_with_same_kind() -> None:
    expected = identity.BooleyBuildIdentity("1.0", "abc123", "payload", "wheel")
    legacy_same = identity.BooleyBuildIdentity("1.0", "abc123", "payload", None)
    legacy_different = identity.BooleyBuildIdentity("1.0", "def456", "other", None)

    assert identity.compare_build_identity(expected, legacy_same).status is identity.Status.MATCH
    assert (
        identity.compare_build_identity(expected, legacy_different).status
        is identity.Status.MISMATCH
    )


def test_no_same_kind_content_or_clean_revision_is_unknown() -> None:
    expected = identity.BooleyBuildIdentity("1.0", "abc123+dirty", None, "wheel")
    observed = identity.BooleyBuildIdentity("1.0", "abc123", "payload", None)

    assert identity.compare_build_identity(expected, observed).status is identity.Status.UNKNOWN


def test_content_mismatch_names_revision_and_both_values() -> None:
    expected = identity.BooleyBuildIdentity("1.0", "abc123", None, "wheel-new")
    observed = identity.BooleyBuildIdentity("1.0", "def456", None, "wheel-old")

    result = identity.compare_build_identity(expected, observed)

    assert result.status is identity.Status.MISMATCH
    assert "revision abc123 -> def456" in result.detail
    assert "wheel_source_fingerprint wheel-new -> wheel-old" in result.detail


def test_payload_fingerprint_compares_only_with_payload_fingerprint() -> None:
    expected = identity.BooleyBuildIdentity("1.0", None, "payload", None)
    same = identity.BooleyBuildIdentity("1.0", None, "payload", None)
    different_kind = identity.BooleyBuildIdentity("1.0", None, None, "payload")

    assert identity.compare_build_identity(expected, same).status is identity.Status.MATCH
    assert (
        identity.compare_build_identity(expected, different_kind).status is identity.Status.UNKNOWN
    )


def test_revision_prefixes_match_but_dirty_revision_is_not_comparable() -> None:
    short = identity.BooleyBuildIdentity("1.0", "abc123", None, None)
    long = identity.BooleyBuildIdentity("1.0", "abc123def", None, None)
    dirty = identity.BooleyBuildIdentity("1.0", "abc123+dirty", None, None)

    assert identity.compare_build_identity(short, long).status is identity.Status.MATCH
    assert identity.compare_build_identity(short, dirty).status is identity.Status.UNKNOWN


def test_ambiguous_payload_environment_is_not_a_typed_payload() -> None:
    metadata = _metadata(
        "issued",
        image_id="sha256:issued",
        labels={},
        environment={"BOOLEY_PAYLOAD_FINGERPRINT": "wheel-not-payload"},
    )

    assert identity.build_identity(metadata).payload_fingerprint is None


def test_decode_docker_document_preserves_labels_and_environment() -> None:
    metadata = identity.decode_image_metadata(
        "issued",
        [
            {
                "Id": "sha256:issued",
                "Config": {
                    "Labels": {"io.booley.provenance.schema": "3"},
                    "Env": ["BOOLEY_VERSION=1.2.3", "EMPTY=", "MALFORMED"],
                },
            }
        ],
    )

    assert metadata is not None
    assert metadata.labels["io.booley.provenance.schema"] == "3"
    assert metadata.environment == {"BOOLEY_VERSION": "1.2.3", "EMPTY": ""}


def test_decode_malformed_docker_document_is_unknown() -> None:
    assert identity.decode_image_metadata("issued", []) is None
    assert identity.decode_image_metadata("issued", [{"Id": 3}]) is None


def test_decode_rejects_unbounded_metadata() -> None:
    document = [
        {
            "Id": "sha256:issued",
            "Config": {"Labels": {str(index): "value" for index in range(257)}},
        }
    ]

    assert identity.decode_image_metadata("issued", document) is None


def test_final_selection_fingerprint_survives_pruned_ancestry() -> None:
    old = _metadata(
        "sha256:old",
        image_id="sha256:old",
        labels={"io.booley.sandbox.selection-fingerprint": "same"},
    )
    moved = _metadata(
        "tag",
        image_id="sha256:new",
        labels={"io.booley.sandbox.selection-fingerprint": "same"},
    )

    result = identity.compare_logical_selection(
        "sha256:old", "tag", {"sha256:old": old}.get, {"tag": moved}.get
    )

    assert result.status is identity.Status.MATCH
    assert (
        identity.decode_image_metadata(
            "issued",
            [{"Id": "sha256:issued", "Config": {"Env": ["X=" + "x" * 20_000]}}],
        )
        is None
    )


def test_equal_stamped_selection_fingerprints_ignore_image_ids() -> None:
    fingerprint = "f" * 64
    issued = _metadata(
        "sha256:issued",
        image_id="sha256:old",
        labels={"io.booley.sandbox.selection-fingerprint": fingerprint},
    )
    configured = _metadata(
        "booley-sandbox",
        image_id="sha256:new",
        labels={"io.booley.sandbox.selection-fingerprint": fingerprint},
    )

    result = identity.compare_logical_selection(
        "sha256:issued",
        "booley-sandbox",
        {"sha256:issued": issued}.get,
        {"booley-sandbox": configured}.get,
    )

    assert result.status is identity.Status.MATCH


def test_schema_three_graph_ignores_changed_image_and_parent_ids() -> None:
    left = _same_source_graph("a")
    right = _same_source_graph("b")

    result = identity.compare_logical_selection("sha256:a2", "tag", left.get, right.get)

    assert result.status is identity.Status.MATCH


def test_standard_and_riscv_graphs_mismatch() -> None:
    standard = _same_source_graph("a")
    riscv = _same_source_graph("b", riscv=True)

    result = identity.compare_logical_selection("sha256:a2", "tag", standard.get, riscv.get)

    assert result.status is identity.Status.MISMATCH
    assert "sandbox_flavor" in result.detail


def test_changed_project_inputs_mismatch() -> None:
    old = _same_source_graph("a", project_inputs="requirements-old")
    new = _same_source_graph("b", project_inputs="requirements-new")

    result = identity.compare_logical_selection("sha256:a3", "tag", old.get, new.get)

    assert result.status is identity.Status.MISMATCH
    assert "effective_inputs" in result.detail


def test_pruned_parent_and_registry_parent_are_unknown_not_stale() -> None:
    pruned = _same_source_graph("a")
    del pruned["sha256:a1"]
    registry = _same_source_graph("b", registry_parent=True)

    assert (
        identity.compare_logical_selection("sha256:a2", "tag", pruned.get, pruned.get).status
        is identity.Status.UNKNOWN
    )
    assert (
        identity.compare_logical_selection("sha256:b2", "tag", registry.get, registry.get).status
        is identity.Status.UNKNOWN
    )


def test_pruned_standard_and_riscv_ancestry_still_mismatch_by_flavor() -> None:
    standard = _same_source_graph("a")
    riscv = _same_source_graph("b", riscv=True)
    del standard["sha256:a1"]
    del riscv["sha256:br"]

    result = identity.compare_logical_selection("sha256:a2", "tag", standard.get, riscv.get)

    assert result.status is identity.Status.MISMATCH
    assert "sandbox_flavor standard -> riscv" in result.detail


def test_cyclic_ancestry_is_unknown_and_bounded() -> None:
    graph = _same_source_graph("a")
    graph["sha256:a1"] = _node("sha256:a1", "standard-substrate", "sha256:a2")
    calls = 0

    def inspect(reference: str):
        nonlocal calls
        calls += 1
        return graph.get(reference)

    result = identity.compare_logical_selection("sha256:a2", "tag", inspect, inspect)

    assert result.status is identity.Status.UNKNOWN
    assert calls <= 8


def _node(image_id: str, role: str, parent: str | None, *, inputs: str = "same"):
    labels = {
        "io.booley.provenance.schema": "3",
        "io.booley.artifact.role": role,
        "io.booley.artifact.effective-inputs": inputs,
        "io.booley.build.recipe-fingerprint": f"recipe-{role}",
        "io.booley.runtime-base.contract": "runtime-contract",
        "io.booley.standard-substrate.contract": "standard-contract",
    }
    if parent:
        labels["io.booley.build.parent-artifact"] = parent
        labels["io.booley.build.parent-artifact-kind"] = "local-image-id"
    return _metadata(image_id, image_id=image_id, labels=labels)


def _same_source_graph(
    suffix: str,
    *,
    riscv: bool = False,
    project_inputs: str | None = None,
    registry_parent: bool = False,
) -> dict[str, identity.ImageMetadata]:
    root_id = f"sha256:{suffix}0"
    standard_id = f"sha256:{suffix}1"
    final_id = f"sha256:{suffix}{'3' if project_inputs else '2'}"
    graph = {
        root_id: _node(root_id, "runtime-base", None),
        standard_id: _node(standard_id, "standard-substrate", root_id),
    }
    parent = standard_id
    if riscv:
        riscv_id = f"sha256:{suffix}r"
        graph[riscv_id] = _node(riscv_id, "riscv-substrate", standard_id)
        parent = riscv_id
    if project_inputs:
        project_id = f"sha256:{suffix}p"
        graph[project_id] = _node(project_id, "project-substrate", parent, inputs=project_inputs)
        parent = project_id
    final = _node(final_id, "wheel-overlay", parent, inputs="wheel-content")
    if riscv:
        final.environment["BOOLEY_SANDBOX_FLAVOR"] = "riscv"
    if registry_parent:
        final.labels["io.booley.build.parent-artifact"] = "example.test/booley@sha256:" + "c" * 64
        final.labels["io.booley.build.parent-artifact-kind"] = "registry-digest"
    graph[final_id] = final
    graph["tag"] = final
    return graph
