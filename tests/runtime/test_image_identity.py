"""Sandbox Image selection and embedded Booley identity contracts."""

from __future__ import annotations

import pytest

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

    oversized_environment = [{"Id": "sha256:issued", "Config": {"Env": ["X=" + "x" * 20_000]}}]
    assert identity.decode_image_metadata("issued", oversized_environment) is None


def test_final_selection_fingerprint_survives_pruned_ancestry() -> None:
    fingerprint = "f" * 64
    old = _metadata(
        "sha256:old",
        image_id="sha256:old",
        labels={
            "io.booley.provenance.schema": "3",
            "io.booley.artifact.role": "wheel-overlay",
            "io.booley.build.recipe-fingerprint": "recipe-wheel",
            "io.booley.sandbox.selection-fingerprint": fingerprint,
        },
    )
    moved = _metadata(
        "tag",
        image_id="sha256:new",
        labels=dict(old.labels),
    )

    result = identity.compare_logical_selection(
        "sha256:old", "tag", {"sha256:old": old}.get, {"tag": moved}.get
    )

    assert result.status is identity.Status.MATCH


def test_equal_stamped_selection_fingerprints_ignore_image_ids() -> None:
    fingerprint = "f" * 64
    issued = _metadata(
        "sha256:issued",
        image_id="sha256:old",
        labels={
            "io.booley.provenance.schema": "3",
            "io.booley.artifact.role": "wheel-overlay",
            "io.booley.build.recipe-fingerprint": "recipe-wheel",
            "io.booley.sandbox.selection-fingerprint": fingerprint,
        },
    )
    configured = _metadata(
        "booley-sandbox",
        image_id="sha256:new",
        labels=dict(issued.labels),
    )

    result = identity.compare_logical_selection(
        "sha256:issued",
        "booley-sandbox",
        {"sha256:issued": issued}.get,
        {"booley-sandbox": configured}.get,
    )

    assert result.status is identity.Status.MATCH


def test_malformed_selection_fingerprint_is_unknown() -> None:
    issued = _same_source_graph("a")
    configured = _same_source_graph("b")
    issued["sha256:a2"].labels["io.booley.sandbox.selection-fingerprint"] = "not-a-sha256"
    configured["tag"].labels["io.booley.sandbox.selection-fingerprint"] = "f" * 64

    result = identity.compare_logical_selection("sha256:a2", "tag", issued.get, configured.get)

    assert result.status is identity.Status.UNKNOWN


def test_inherited_fingerprint_does_not_hide_derived_image_recipe() -> None:
    fingerprint = "f" * 64
    base = _node("sha256:base", "wheel-overlay", "sha256:parent")
    base.labels["io.booley.sandbox.selection-fingerprint"] = fingerprint
    derived = _node("sha256:derived", "wheel-overlay", "sha256:base")
    derived.labels["io.booley.sandbox.selection-fingerprint"] = fingerprint
    derived.labels["io.booley.build.recipe-fingerprint"] = "project-recipe"
    graph = {"sha256:base": base, "configured": derived}

    result = identity.compare_logical_selection("sha256:base", "configured", graph.get, graph.get)

    assert result.status is identity.Status.MISMATCH
    assert "recipe_fingerprint" in result.detail


def test_mixed_schema_three_and_legacy_ancestry_is_unknown() -> None:
    final = _node("sha256:final", "wheel-overlay", "sha256:legacy")
    legacy = _metadata(
        "sha256:legacy",
        image_id="sha256:legacy",
        labels={"io.booley.build.recipe-fingerprint": "legacy-recipe"},
    )
    graph = {"sha256:final": final, "tag": final, "sha256:legacy": legacy}

    result = identity.compare_logical_selection("sha256:final", "tag", graph.get, graph.get)

    assert result.status is identity.Status.UNKNOWN


def test_registry_digest_parent_is_a_terminal_selection_fact() -> None:
    left = _same_source_graph("a", registry_parent=True)
    right = _same_source_graph("b", registry_parent=True)
    right["tag"].labels["io.booley.build.parent-artifact"] = (
        "example.test/booley@sha256:" + "d" * 64
    )

    same = identity.compare_logical_selection("sha256:a2", "tag", left.get, left.get)
    changed = identity.compare_logical_selection("sha256:a2", "tag", left.get, right.get)

    assert same.status is identity.Status.MATCH
    assert changed.status is identity.Status.MISMATCH


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


def test_pruned_parent_is_unknown_not_stale() -> None:
    pruned = _same_source_graph("a")
    del pruned["sha256:a1"]

    assert (
        identity.compare_logical_selection("sha256:a2", "tag", pruned.get, pruned.get).status
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


def _layout_pair(monkeypatch, tmp_path):
    from types import SimpleNamespace

    from booley.runtime import project_image
    from booley.runtime.image_provenance import resolve_recipe_fingerprint
    from booley.runtime.paths import docker_data_dir

    parent_id, child_id = "sha256:" + "a" * 64, "sha256:" + "b" * 64
    parent_labels = {
        identity.LABEL_SCHEMA: identity.PROVENANCE_SCHEMA,
        identity.LABEL_ARTIFACT_ROLE: "wheel-overlay",
        identity.LABEL_LOGICAL_SELECTION_FINGERPRINT: "c" * 64,
        identity.LABEL_RECIPE_FINGERPRINT: "d" * 64,
        identity.LABEL_PARENT_ARTIFACT_KIND: identity.PARENT_ARTIFACT_LOCAL_IMAGE_ID,
        identity.LABEL_WHEEL_SOURCE_FINGERPRINT: "wheel",
    }
    root = tmp_path / "project"
    root.mkdir()
    (root / ".git").mkdir()
    external = tmp_path / "external"
    external.mkdir()
    monkeypatch.setenv("BOOLEY_PROJECT_DIR", str(external))
    from booley.core.project_dir import reset_cache

    reset_cache()
    recipe = resolve_recipe_fingerprint((docker_data_dir() / "Dockerfile.project-data-layout",))
    reference, inputs = project_image.layout_identity(root, parent_id, recipe)
    child_labels = dict(parent_labels)
    child_labels.update(
        {
            identity.LABEL_ARTIFACT_ROLE: "project-data-layout",
            identity.LABEL_RECIPE_FINGERPRINT: resolve_recipe_fingerprint(
                (docker_data_dir() / "Dockerfile.project-data-layout",)
            ),
            identity.LABEL_EFFECTIVE_INPUTS: inputs,
            identity.LABEL_PARENT_ARTIFACT: parent_id,
            project_image.LABEL_LAYOUT_REFERENCE: reference,
        }
    )
    records = _layout_inspections(parent_id, child_id, parent_labels, child_labels)
    parent = _metadata(parent_id, image_id=parent_id, labels=parent_labels)
    child = _metadata(child_id, image_id=child_id, labels=child_labels)
    images = {"issued": child, "configured": parent, parent_id: parent}
    project_image.verify_layout_parent_identity.cache_clear()
    monkeypatch.setattr(project_image, "inspect_layout_image", lambda ref, **_kw: records.get(ref))
    monkeypatch.setattr(project_image, "_image_history", _layout_history)
    monkeypatch.setattr(
        project_image.subprocess, "run", lambda *_args, **_kwargs: SimpleNamespace(returncode=0)
    )
    return images, records, parent_labels, child_labels


def test_proven_layout_derivative_preserves_parent_logical_selection(monkeypatch, tmp_path):
    images, _records, _parent, _child = _layout_pair(monkeypatch, tmp_path)
    assert (
        identity.compare_logical_selection(
            "issued", "configured", images.get, project_root=tmp_path / "project"
        ).status
        is identity.Status.MATCH
    )


def test_layout_does_not_hide_changed_parent_selection(monkeypatch, tmp_path):
    images, _records, parent_labels, _child_labels = _layout_pair(monkeypatch, tmp_path)
    configured = dict(parent_labels, **{identity.LABEL_RECIPE_FINGERPRINT: "1" * 64})
    images["configured"] = _metadata(
        "configured", image_id="sha256:" + "2" * 64, labels=configured
    )
    assert (
        identity.compare_logical_selection(
            "issued", "configured", images.get, project_root=tmp_path / "project"
        ).status
        is identity.Status.MISMATCH
    )


def test_layout_does_not_hide_forged_recipe_topology_or_parent_fingerprint(monkeypatch, tmp_path):
    images, _records, _parent, child_labels = _layout_pair(monkeypatch, tmp_path)
    for label in (
        identity.LABEL_RECIPE_FINGERPRINT,
        identity.LABEL_EFFECTIVE_INPUTS,
        identity.LABEL_LOGICAL_SELECTION_FINGERPRINT,
        identity.LABEL_PARENT_ARTIFACT,
    ):
        original = child_labels[label]
        child_labels[label] = "3" * 64
        assert (
            identity.compare_logical_selection(
                "issued", "configured", images.get, project_root=tmp_path / "project"
            ).status
            is identity.Status.UNKNOWN
        )
        child_labels[label] = original


def test_layout_requires_actual_parent_configuration_and_layer_ancestry(monkeypatch, tmp_path):
    from booley.runtime import project_image

    images, records, _parent, _child = _layout_pair(monkeypatch, tmp_path)
    child = records[images["issued"].image_id]
    child["Config"]["User"] = "root"
    assert (
        identity.compare_logical_selection(
            "issued", "configured", images.get, project_root=tmp_path / "project"
        ).status
        is identity.Status.UNKNOWN
    )
    child["Config"]["User"] = "agent"
    child["RootFS"]["Layers"] = ["foreign", "layout"]
    project_image.verify_layout_parent_identity.cache_clear()
    assert (
        identity.compare_logical_selection(
            "issued", "configured", images.get, project_root=tmp_path / "project"
        ).status
        is identity.Status.UNKNOWN
    )


def _layout_inspections(parent_id, child_id, parent_labels, child_labels):
    records = {
        parent_id: {
            "Id": parent_id,
            "Config": {"User": "agent", "Labels": parent_labels},
            "RootFS": {"Layers": ["base"]},
        },
        child_id: {
            "Id": child_id,
            "Config": {"User": "agent", "Labels": child_labels},
            "RootFS": {"Layers": ["base", "layout"]},
        },
    }
    return records


def _layout_history(reference, _executable):
    from booley.runtime.paths import docker_data_dir

    recipe = (docker_data_dir() / "Dockerfile.project-data-layout").read_text()
    command = recipe.split("RUN ", 1)[1].split("\nUSER ", 1)[0].replace("\\\n", " ")
    parent = ["base build"]
    return (
        parent
        if reference.endswith("a" * 64)
        else ["RUN /bin/sh -c " + " ".join(command.split()) + " # buildkit", *parent]
    )


def test_layout_requires_explicit_matching_project_and_topology(monkeypatch, tmp_path):
    from booley.runtime import project_image
    from booley.runtime.image_provenance import resolve_recipe_fingerprint
    from booley.runtime.paths import docker_data_dir

    images, _records, _parent, child_labels = _layout_pair(monkeypatch, tmp_path)
    assert (
        identity.compare_logical_selection("issued", "configured", images.get).status
        is identity.Status.UNKNOWN
    )
    foreign = tmp_path / "foreign"
    foreign.mkdir()
    recipe = resolve_recipe_fingerprint((docker_data_dir() / "Dockerfile.project-data-layout",))
    reference, inputs = project_image.layout_identity(foreign, "sha256:" + "a" * 64, recipe)
    child_labels[project_image.LABEL_LAYOUT_REFERENCE] = reference
    child_labels[identity.LABEL_EFFECTIVE_INPUTS] = inputs
    assert (
        identity.compare_logical_selection(
            "issued", "configured", images.get, project_root=tmp_path / "project"
        ).status
        is identity.Status.UNKNOWN
    )


def test_layout_rejects_inherited_extra_layers_and_forged_recipe_history(monkeypatch, tmp_path):
    from booley.runtime import project_image

    images, records, _parent, _child = _layout_pair(monkeypatch, tmp_path)
    record = records[images["issued"].image_id]
    record["RootFS"]["Layers"].append("modified-payload")

    def compare():
        return identity.compare_logical_selection(
            "issued", "configured", images.get, project_root=tmp_path / "project"
        )

    assert compare().status is identity.Status.UNKNOWN
    record["RootFS"]["Layers"].pop()
    project_image.verify_layout_parent_identity.cache_clear()
    monkeypatch.setattr(
        project_image,
        "_image_history",
        lambda ref, _exe: (
            ["base build"]
            if ref.endswith("a" * 64)
            else ["RUN /bin/sh -c replace wheel", "base build"]
        ),
    )
    assert compare().status is identity.Status.UNKNOWN


def test_layout_verification_uses_configured_docker_executable(monkeypatch, tmp_path):
    from booley.runtime import project_image

    images, _records, _parent, _child = _layout_pair(monkeypatch, tmp_path)
    executables = []

    def history(ref, executable):
        executables.append(executable)
        return _layout_history(ref, executable)

    monkeypatch.setattr(project_image, "_image_history", history)
    result = identity.compare_logical_selection(
        "issued",
        "configured",
        images.get,
        project_root=tmp_path / "project",
        executable="configured-docker",
    )
    assert result.status is identity.Status.MATCH
    assert executables == ["configured-docker", "configured-docker"]


def test_layout_rejects_matching_labels_for_local_data_topology(monkeypatch, tmp_path):
    from booley.core.project_dir import reset_cache

    images, _records, _parent, _child = _layout_pair(monkeypatch, tmp_path)
    root = tmp_path / "project"
    (root / ".booley_project").mkdir()
    monkeypatch.setenv("BOOLEY_PROJECT_DIR", str(root / ".booley_project"))
    reset_cache()
    result = identity.compare_logical_selection(
        "issued", "configured", images.get, project_root=root
    )
    assert result.status is identity.Status.UNKNOWN


@pytest.mark.parametrize("output", ["not json", "{}", "[]", "[{}, {}]", "[null]"])
def test_layout_inspection_rejects_malformed_or_multiple_docker_records(
    monkeypatch: pytest.MonkeyPatch, output: str
) -> None:
    from types import SimpleNamespace

    from booley.runtime import project_image

    monkeypatch.setattr(
        project_image.subprocess, "run", lambda *_args, **_kwargs: SimpleNamespace(stdout=output)
    )
    with pytest.raises(RuntimeError, match="layout image"):
        project_image.inspect_layout_image("candidate")


def test_layout_inspection_preserves_exact_docker_record(monkeypatch: pytest.MonkeyPatch) -> None:
    import json
    from types import SimpleNamespace

    from booley.runtime import project_image

    record = {"Id": "sha256:" + "a" * 64, "Config": {"User": "1000"}}
    commands = []

    def inspect(command, **kwargs):
        commands.append((command, kwargs))
        return SimpleNamespace(stdout=json.dumps([record]))

    monkeypatch.setattr(project_image.subprocess, "run", inspect)
    assert project_image.inspect_layout_image("candidate", executable="docker-fixture") == record
    assert commands[0][0] == ["docker-fixture", "image", "inspect", "candidate"]
    assert commands[0][1]["timeout"] == 30


def test_layout_inspection_transport_failure_is_controlled(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from booley.runtime import project_image

    def unavailable(*_args, **_kwargs):
        raise OSError("Docker unavailable")

    monkeypatch.setattr(project_image.subprocess, "run", unavailable)
    with pytest.raises(RuntimeError, match="cannot inspect"):
        project_image.inspect_layout_image("candidate")


@pytest.mark.parametrize("entry", ["unknown operation", "/bin/sh -c change payload"])
def test_layout_history_rejects_untrusted_derivative_operations(
    monkeypatch: pytest.MonkeyPatch, entry: str
) -> None:
    from booley.runtime import project_image

    monkeypatch.setattr(
        project_image,
        "_image_history",
        lambda image, _executable: ["base"] if image == "parent" else [entry, "base"],
    )
    with pytest.raises(RuntimeError, match="history"):
        project_image._verify_layout_history("parent", "candidate", "docker")


def test_layout_history_refuses_unbounded_docker_output(monkeypatch: pytest.MonkeyPatch) -> None:
    from types import SimpleNamespace

    from booley.runtime import project_image

    monkeypatch.setattr(
        project_image.subprocess,
        "run",
        lambda *_args, **_kwargs: SimpleNamespace(stdout="x" * 1_048_577),
    )
    with pytest.raises(RuntimeError, match="exceeds bounds"):
        project_image._image_history("candidate", "docker")


@pytest.mark.parametrize("labels", [["untrusted"], "untrusted"])
def test_layout_verification_refuses_malformed_parent_labels(
    labels: object,
) -> None:
    from booley.runtime import project_image

    records = _layout_inspections("parent", "candidate", {}, {})
    records["parent"]["Config"]["Labels"] = labels
    with pytest.raises(RuntimeError, match="labels are invalid"):
        project_image.verify_layout_image(records["parent"], records["candidate"])
