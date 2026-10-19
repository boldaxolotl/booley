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


_SELECTION_LABELS = {
    "role": identity.LABEL_ARTIFACT_ROLE,
    "inputs": identity.LABEL_EFFECTIVE_INPUTS,
    "recipe": identity.LABEL_RECIPE_FINGERPRINT,
    "parent_kind": identity.LABEL_PARENT_ARTIFACT_KIND,
    "parent": identity.LABEL_PARENT_ARTIFACT,
    "runtime": identity.LABEL_RUNTIME_BASE_CONTRACT,
    "standard": identity.LABEL_STANDARD_SUBSTRATE_CONTRACT,
    "schema": identity.LABEL_SCHEMA,
}


_SELECTION_CASES = {
    "equal-stamps": ({}, ("f", "f"), None, "match"),
    "inherited-recipe": ({"recipe": "changed"}, ("f", "f"), None, "recipe"),
    "project-inputs": ({"inputs": "changed"}, ("f", "f"), None, "inputs"),
    "wheel-inputs": ({"role": "wheel-overlay", "inputs": "changed"}, ("f", "f"), None, "match"),
    "role": ({"role": "wheel-overlay"}, ("f", "f"), None, "role"),
    "parent-kind": ({"parent_kind": "registry-digest"}, ("f", "f"), None, "parent_kind"),
    "runtime-contract": ({"runtime": "changed"}, ("f", "f"), None, "runtime"),
    "standard-contract": ({"standard": "changed"}, ("f", "f"), None, "standard"),
    "different-stamps": ({}, ("f", "e"), None, "selection_fingerprint"),
    "flavor-both": ({}, ("f", "f"), "riscv", "sandbox_flavor"),
    "flavor-one": ({}, ("f", None), "riscv", "sandbox_flavor"),
    "flavor-neither": ({}, (None, None), "riscv", "sandbox_flavor"),
    "absent-standard": ({}, ("f", "f"), "standard", "match"),
    "literal-empty": ({}, ("f", "f"), "", "sandbox_flavor"),
    "literal-unknown": ({}, ("f", "f"), "unknown", "sandbox_flavor"),
    "literal-custom": ({}, ("f", "f"), "custom", "sandbox_flavor"),
    "registry-equal": ({"parent": "registry@sha256:abc"}, (None, None), None, "match"),
    "registry-different": (
        {"parent": "registry@sha256:def"},
        (None, None),
        None,
        "registry_parent",
    ),
    "registry-missing": ({"parent": ""}, (None, None), None, "unknown"),
    "one-stamp": ({}, ("f", None), None, "unknown"),
    "one-stamp-recipe": ({"recipe": "changed"}, ("f", None), None, "recipe"),
    "malformed": ({}, ("malformed", "f"), None, "unknown"),
    "old-schema": ({"schema": "2"}, ("f", "f"), None, "unknown"),
    "nonfinal-stamped": ({"role": "runtime-base"}, ("f", "f"), None, "unknown"),
    "malformed-before-flavor": ({}, ("malformed", "f"), "riscv", "unknown"),
}


@pytest.mark.parametrize(
    "changes,stamps,flavor,expected",
    _SELECTION_CASES.values(),
    ids=_SELECTION_CASES,
)
def test_final_selection_rules(changes, stamps, flavor, expected):
    fields = {
        "role": "wheel-overlay" if "parent" in changes else "project-overlay",
        "inputs": "inputs",
        "recipe": "recipe",
        "parent_kind": "registry-digest" if "parent" in changes else "local-image-id",
        "parent": "registry@sha256:abc",
        "runtime": "runtime",
        "standard": "standard",
        "schema": "3",
    }
    left = dict(fields)
    if "inputs" in changes and changes.get("role") == "wheel-overlay":
        left["role"] = "wheel-overlay"
    if changes.get("role") == "runtime-base":
        left["role"] = "runtime-base"
    images = {}
    for name, values, stamp, environment in zip(
        ("issued", "configured"),
        (left, fields | changes),
        stamps,
        ({}, {} if flavor is None else {identity.ENV_SANDBOX_FLAVOR: flavor}),
        strict=True,
    ):
        labels = {_SELECTION_LABELS[field]: value for field, value in values.items()}
        if stamp is not None:
            labels[identity.LABEL_LOGICAL_SELECTION_FINGERPRINT] = (
                stamp * 64 if len(stamp) == 1 else stamp
            )
        images[name] = _metadata(
            name, image_id="sha256:" + name, labels=labels, environment=environment
        )
    result = identity.compare_logical_selection("issued", "configured", images.get)
    status = expected if expected in {"match", "unknown"} else "mismatch"
    assert result.status == status
    if status == "mismatch":
        detail = {
            "recipe": "recipe_fingerprint",
            "inputs": "effective_inputs",
            "runtime": "runtime_base_contract",
            "standard": "standard_substrate_contract",
        }
        assert detail.get(expected, expected) in result.detail


def test_final_selection_never_walks_parents():
    calls = []

    def inspect(reference):
        calls.append(reference)
        return _metadata(
            reference,
            image_id=reference,
            labels={
                identity.LABEL_SCHEMA: identity.PROVENANCE_SCHEMA,
                identity.LABEL_ARTIFACT_ROLE: "wheel-overlay",
                identity.LABEL_RECIPE_FINGERPRINT: "recipe",
                identity.LABEL_PARENT_ARTIFACT_KIND: "local-image-id",
                identity.LABEL_PARENT_ARTIFACT: "parent",
            },
        )

    result = identity.compare_logical_selection("issued", "configured", inspect)
    assert calls == ["issued", "configured"]
    assert result.status is identity.Status.UNKNOWN


def test_decode_large_valid_metadata():
    values = {str(index): "x" * 20_000 for index in range(257)}
    config = {"Labels": values, "Env": [f"{k}={v}" for k, v in values.items()]}
    result = identity.decode_image_metadata("issued", [{"Id": "issued", "Config": config}])
    assert result is not None
    assert result.labels == result.environment == values


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
    monkeypatch.setenv("BOOLEY_DIR", str(external))
    from booley.core.booley_dir import reset_cache

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
    from booley.core.booley_dir import reset_cache

    images, _records, _parent, _child = _layout_pair(monkeypatch, tmp_path)
    root = tmp_path / "project"
    (root / ".booley").mkdir()
    monkeypatch.setenv("BOOLEY_DIR", str(root / ".booley"))
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


@pytest.mark.parametrize("damaged", ["parent", "child"])
def test_layout_proof_requires_image_configuration(damaged):
    from booley.runtime import project_image

    records = _layout_inspections("parent", "child", {}, {})
    records[damaged]["Config"] = None
    with pytest.raises(RuntimeError, match="layout image configuration is missing"):
        project_image.verify_layout_image(records["parent"], records["child"])


@pytest.mark.parametrize("key", ["Architecture", "Os", "Variant"])
def test_layout_proof_refuses_a_changed_parent_platform(key):
    from booley.runtime import project_image

    records = _layout_inspections("parent", "child", {}, {})
    records["child"][key] = "substituted"
    with pytest.raises(RuntimeError, match="changed parent platform"):
        project_image.verify_layout_image(records["parent"], records["child"])


def test_layout_proof_requires_inspection_to_match_requested_identity(monkeypatch):
    from booley.runtime import project_image

    project_image.verify_layout_parent_identity.cache_clear()
    monkeypatch.setattr(
        project_image, "inspect_layout_image", lambda *_args, **_kwargs: {"Id": "substituted"}
    )
    with pytest.raises(RuntimeError, match="inspection changed immutable identity"):
        project_image.verify_layout_parent_identity("child", "parent")
    project_image.verify_layout_parent_identity.cache_clear()


def test_layout_proof_refuses_a_failed_alias_directory_probe(monkeypatch):
    from types import SimpleNamespace

    from booley.runtime import project_image

    parent = "sha256:" + "a" * 64
    child = "sha256:" + "b" * 64
    records = _layout_inspections(parent, child, {}, {})
    commands = []

    def probe(command, **_kwargs):
        commands.append(command)
        return SimpleNamespace(returncode=1)

    monkeypatch.setattr(project_image, "_image_history", _layout_history)
    monkeypatch.setattr(project_image.subprocess, "run", probe)
    with pytest.raises(RuntimeError, match="lacks its root-owned alias directory"):
        project_image.verify_layout_image(records[parent], records[child])
    assert len(commands) == 1
    assert "--pull=never" in commands[0] and "--network=none" in commands[0]


@pytest.mark.parametrize(
    "parent,child", [([], ["extra"]), (["base"], ["base"]), (["base"], ["extra", "other"])]
)
def test_layout_history_refuses_substituted_parent_layers(monkeypatch, parent, child):
    from booley.runtime import project_image

    monkeypatch.setattr(
        project_image,
        "_image_history",
        lambda image, _executable: parent if image == "parent" else child,
    )
    with pytest.raises(RuntimeError, match="build history has a substituted parent"):
        project_image._verify_layout_history("parent", "child", "docker")


def test_layout_history_requires_the_trusted_recipe(monkeypatch, tmp_path):
    from booley.runtime import paths, project_image

    parent = "sha256:" + "a" * 64
    child = "sha256:" + "b" * 64
    histories = {image: _layout_history(image, "docker") for image in (parent, child)}
    monkeypatch.setattr(
        project_image, "_image_history", lambda image, _executable: histories[image]
    )
    monkeypatch.setattr(paths, "docker_data_dir", lambda: tmp_path)
    with pytest.raises(RuntimeError, match="trusted recipe is unavailable or malformed"):
        project_image._verify_layout_history(parent, child, "docker")


@pytest.mark.parametrize(
    "with_nodes,parent,expected",
    [
        (False, "", "1eda4eb81bcc607229beac648e80b5a07ce75779a82b3dda5d4991a67870979c"),
        (
            False,
            "registry-parent",
            "1eda4eb81bcc607229beac648e80b5a07ce75779a82b3dda5d4991a67870979c",
        ),
        (True, "", "514e2db26e807d1dec453d4eacbbd7029d4b256054209f271b7ce888a6c44d14"),
        (
            True,
            "registry-parent",
            "6b348a8dd84e4876bb11c5d60b4eebd4fc2153c8c7bd7804ae38121257e0e6b8",
        ),
    ],
)
def test_logical_selection_fingerprint_chain_golden(with_nodes, parent, expected):
    nodes = [
        ("runtime-base", "runtime-inputs", "runtime-recipe", "runtime-v1", ""),
        ("standard-substrate", "standard-inputs", "standard-recipe", "runtime-v1", "standard-v1"),
        ("wheel-overlay", "", "wheel-recipe", "runtime-v1", "standard-v1"),
    ]
    assert (
        identity.logical_selection_fingerprint_for_chain(
            "booley-sandbox", nodes if with_nodes else [], initial_parent_key=parent
        )
        == expected
    )
