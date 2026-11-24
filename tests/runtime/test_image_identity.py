"""Sandbox Image selection and embedded Booley identity contracts."""

from __future__ import annotations

import json
from collections.abc import Callable, Iterator
from dataclasses import dataclass, field
from pathlib import Path

import pytest
from tests.fake_docker_executable import FakeDocker, install_fake_docker

from booley.core.boundary import BoundaryError
from booley.runtime import image_identity as identity
from booley.runtime import project_image
from booley.runtime.image_identity import Status


def _metadata(
    reference: str,
    *,
    image_id: str,
    labels: dict[str, str],
    environment: dict[str, str] | None = None,
) -> identity.ImageMetadata:
    return identity.ImageMetadata(reference, image_id, labels, environment or {}, ())


def test_unavailable_build_values_and_ambiguous_payload_environment_read_as_absent() -> None:
    metadata = _metadata(
        "issued",
        image_id="sha256:issued",
        labels={
            "org.opencontainers.image.version": "unknown",
            "org.opencontainers.image.revision": "",
            "io.booley.wheel.source-fingerprint": "unknown",
        },
        environment={"BOOLEY_PAYLOAD_FINGERPRINT": "wheel-not-payload"},
    )

    assert identity.build_identity(metadata) == identity.BooleyBuildIdentity(
        None, None, None, None
    )


def _build(text: str) -> identity.BooleyBuildIdentity:
    """Parse ``"<version> <revision> <payload> <wheel>"``; ``-`` stands for an absent value."""
    return identity.BooleyBuildIdentity(*(None if word == "-" else word for word in text.split()))


_M, _X, _U = Status.MATCH, Status.MISMATCH, Status.UNKNOWN
_FRAGMENTS = ("revision abc123 -> def456", "wheel_source_fingerprint wheel-new -> wheel-old")
_BUILD_COMPARISONS = [
    # The wheel fingerprint decides despite the revision; a typed payload meets a legacy one.
    ("1.0 abc123 - wheel", "1.0 def456 - wheel", _M, ()),
    ("1.0 abc123 payload wheel", "1.0 abc123 payload -", _M, ()),
    ("1.0 abc123 payload wheel", "1.0 def456 other -", _X, ()),
    ("1.0 abc123+dirty - wheel", "1.0 abc123 payload -", _U, ()),
    ("1.0 abc123 - wheel-new", "1.0 def456 - wheel-old", _X, _FRAGMENTS),
    # A payload fingerprint compares only with a payload fingerprint.
    ("1.0 - payload -", "1.0 - payload -", _M, ()),
    ("1.0 - payload -", "1.0 - - payload", _U, ()),
    # Revision prefixes match; a dirty revision is not comparable.
    ("1.0 abc123 - -", "1.0 abc123def - -", _M, ()),
    ("1.0 abc123 - -", "1.0 abc123+dirty - -", _U, ()),
]


@pytest.mark.parametrize("expected,observed,status,detail", _BUILD_COMPARISONS)
def test_build_identity_comparison(
    expected: str, observed: str, status: Status, detail: tuple[str, ...]
) -> None:
    result = identity.compare_build_identity(_build(expected), _build(observed))

    assert result.status is status
    assert all(fragment in result.detail for fragment in detail)


@pytest.mark.parametrize(
    "record,labels,environment,layers",
    [
        (
            {
                "Id": "sha256:issued",
                "Config": {
                    "Labels": {"io.booley.provenance.schema": "3"},
                    "Env": ["BOOLEY_VERSION=1.2.3", "EMPTY=", "MALFORMED"],
                },
            },
            {"io.booley.provenance.schema": "3"},
            {"BOOLEY_VERSION": "1.2.3", "EMPTY": ""},
            (),
        ),
        (
            {"Id": "sha256:issued", "RootFS": {"Layers": ["sha256:a", "sha256:b"]}},
            {},
            {},
            ("sha256:a", "sha256:b"),
        ),
        ({"Id": "sha256:x", "Config": None}, {}, {}, ()),
        ({"Id": "sha256:x", "Config": {"Labels": None, "Env": None}}, {}, {}, ()),
        ({"Id": "sha256:x", "RootFS": {"Type": "layers"}}, {}, {}, ()),
    ],
)
def test_decode_docker_document(
    record: dict[str, object],
    labels: dict[str, str],
    environment: dict[str, str],
    layers: tuple[str, ...],
) -> None:
    decoded = identity.decode_image_metadata("issued", [record])

    assert (decoded.labels, decoded.environment, decoded.layers) == (labels, environment, layers)


def test_decode_malformed_docker_document_raises() -> None:
    malformed: list[object] = [
        {"Id": "sha256:x"},
        [],
        [{"Id": "sha256:x"}, {"Id": "sha256:y"}],
        ["not-a-record"],
        [{"Id": 3}],
        [{"Id": "sha256:x", "Config": "text"}],
        [{"Id": "sha256:x", "RootFS": None}],
        [{"Id": "sha256:x", "RootFS": {"Layers": [1]}}],
        [{"Id": "sha256:x", "RootFS": {"Layers": "layer"}}],
    ]
    for document in malformed:
        with pytest.raises(BoundaryError):
            identity.decode_image_metadata("issued", document)


def test_label_reads_blank_as_absent() -> None:
    metadata = identity.ImageMetadata("r", "sha256:x", {"a": " v ", "b": "  ", "c": ""}, {}, ())

    assert (metadata.label("a"), metadata.label("b"), metadata.label("c")) == ("v", None, None)
    assert metadata.label("missing") is None


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
    assert result.status is Status.UNKNOWN


def _layout_inspections(parent_id, child_id, parent_labels, child_labels):
    return {
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


@dataclass
class _Layout:
    """A proven layout child and its parent behind the shared fake Docker."""

    root: Path | None
    docker: FakeDocker
    monkeypatch: pytest.MonkeyPatch
    images: dict[str, identity.ImageMetadata]
    records: dict[str, dict]
    labels: dict[str, str]  # the child's, shared with its inspection record
    histories: dict[str, list[str]]
    extra: dict[str, tuple[int, str]] = field(default_factory=dict)
    parent_id: str = "sha256:" + "a" * 64
    child_id: str = "sha256:" + "b" * 64

    @property
    def executable(self) -> str:
        return str(self.docker.path)

    def edit(self, record: str, path: str, value: object) -> None:
        """Set a dotted *path* of the ``parent`` or ``child`` inspection record."""
        *parents, key = path.split(".")
        target = self.records[self.parent_id if record == "parent" else self.child_id]
        for part in parents:
            target = target[part]
        target[key] = value

    def publish(self) -> None:
        """Rewrite the fake's replies from the records and histories, then forget proofs."""
        replies = {f"image {i}": (0, json.dumps([record])) for i, record in self.records.items()}
        replies |= {f"history {i}": (0, "\n".join(lines)) for i, lines in self.histories.items()}
        self.docker.replies_path.write_bytes(json.dumps(replies | self.extra).encode("utf-8"))
        project_image.verify_layout_parent_identity.cache_clear()

    def verify(self) -> None:
        self.publish()
        project_image.verify_layout_image(
            self.records[self.parent_id], self.records[self.child_id], executable=self.executable
        )

    def status(self) -> Status:
        self.publish()
        return identity.compare_logical_selection(
            "issued",
            "configured",
            self.images.get,
            project_root=self.root,
            executable=self.executable,
        ).status


@pytest.fixture
def layout(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[_Layout]:
    from booley.core.booley_dir import reset_cache

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
    (tmp_path / "external").mkdir()
    monkeypatch.setenv("BOOLEY_DIR", str(tmp_path / "external"))
    reset_cache()
    topology = project_image.layout_topology(root)
    reference, inputs = topology.identity(parent_id)
    labels = parent_labels | {
        identity.LABEL_ARTIFACT_ROLE: "project-data-layout",
        identity.LABEL_RECIPE_FINGERPRINT: topology.recipe_fingerprint,
        identity.LABEL_EFFECTIVE_INPUTS: inputs,
        identity.LABEL_PARENT_ARTIFACT: parent_id,
        project_image.LABEL_LAYOUT_REFERENCE: reference,
    }
    parent = _metadata(parent_id, image_id=parent_id, labels=parent_labels)
    child = _metadata(child_id, image_id=child_id, labels=labels)
    docker = install_fake_docker(tmp_path, monkeypatch, {})
    (tmp_path / "empty").mkdir()
    monkeypatch.setenv("PATH", str(tmp_path / "empty"))  # a call that drops the executable fails
    yield _Layout(
        root,
        docker,
        monkeypatch,
        {"issued": child, "configured": parent, parent_id: parent},
        _layout_inspections(parent_id, child_id, parent_labels, labels),
        labels,
        {image: _layout_history(image, "docker") for image in (parent_id, child_id)},
    )
    project_image.verify_layout_parent_identity.cache_clear()


def test_proven_layout_derivative_preserves_parent_logical_selection(layout: _Layout) -> None:
    assert layout.status() is Status.MATCH
    # Every call went through the configured executable: ``docker`` resolves to nothing.
    calls = layout.docker.calls
    assert [(call[0], call[-1]) for call in calls[:4]] == [
        ("image", layout.parent_id),
        ("image", layout.child_id),
        ("history", layout.parent_id),
        ("history", layout.child_id),
    ]
    assert [call[0] for call in calls[4:]] == ["run"]


def _change_configured_recipe(layout: _Layout) -> None:
    labels = layout.images["configured"].labels | {identity.LABEL_RECIPE_FINGERPRINT: "1" * 64}
    layout.images["configured"] = _metadata("configured", image_id="sha256:2", labels=labels)


def _foreign_topology(layout: _Layout) -> None:
    foreign = layout.docker.path.parent / "foreign"
    foreign.mkdir()
    reference, inputs = project_image.layout_topology(foreign).identity(layout.parent_id)
    layout.labels[project_image.LABEL_LAYOUT_REFERENCE] = reference
    layout.labels[identity.LABEL_EFFECTIVE_INPUTS] = inputs


def _local_data_topology(layout: _Layout) -> None:
    from booley.core.booley_dir import reset_cache

    (layout.root / ".booley").mkdir()
    layout.monkeypatch.setenv("BOOLEY_DIR", str(layout.root / ".booley"))
    reset_cache()


_FORGEABLE = (
    identity.LABEL_RECIPE_FINGERPRINT,
    identity.LABEL_EFFECTIVE_INPUTS,
    identity.LABEL_LOGICAL_SELECTION_FINGERPRINT,
    identity.LABEL_PARENT_ARTIFACT,
)
_COMPARISONS = {
    "changed-configured-recipe": (_change_configured_recipe, Status.MISMATCH),
    **{
        f"forged-{label.rsplit('.', 1)[-1]}": (
            lambda layout, label=label: layout.labels.update({label: "3" * 64}),
            Status.UNKNOWN,
        )
        for label in _FORGEABLE
    },
    "no-project-root": (lambda layout: setattr(layout, "root", None), Status.UNKNOWN),
    "foreign-topology": (_foreign_topology, Status.UNKNOWN),
    "local-data-topology": (_local_data_topology, Status.UNKNOWN),
    "changed-user-fails-the-proof": (
        lambda layout: layout.edit("child", "Config.User", "root"),
        Status.UNKNOWN,
    ),
}


@pytest.mark.parametrize("change,status", _COMPARISONS.values(), ids=_COMPARISONS)
def test_layout_derivative_comparison_from_a_proven_pair(
    layout: _Layout, change: Callable[[_Layout], None], status: Status
) -> None:
    change(layout)

    assert layout.status() is status


@pytest.mark.parametrize(
    "record,path,value,message",
    [
        ("parent", "Config.Labels", ["untrusted"], "labels are invalid"),
        ("parent", "Config.Labels", "untrusted", "labels are invalid"),
        ("parent", "Config", None, "configuration is missing"),
        ("child", "Config", None, "configuration is missing"),
        ("child", "Architecture", "x", "changed parent platform"),
        ("child", "Os", "x", "changed parent platform"),
        ("child", "Variant", "x", "changed parent platform"),
        ("child", "Config.User", "root", "runtime configuration"),
        ("child", "RootFS.Layers", ["foreign", "layout"], "substituted parent"),
        ("child", "RootFS.Layers", ["base", "layout", "extra"], "substituted parent"),
    ],
)
def test_layout_proof_refuses_changed_configuration_or_layers(
    layout: _Layout, record: str, path: str, value: object, message: str
) -> None:
    layout.edit(record, path, value)

    with pytest.raises(RuntimeError, match=message):
        layout.verify()


@pytest.mark.parametrize(
    "parent,child,message",
    [
        ([], ["extra"], "substituted parent"),
        (["base"], ["base"], "substituted parent"),
        (["base"], ["extra", "other"], "substituted parent"),
        (["base"], ["unknown operation", "base"], "history"),
        (["base"], ["/bin/sh -c change payload", "base"], "history"),
        (["base"], ["x" * 1_048_577], "exceeds bounds"),
    ],
)
def test_layout_proof_refuses_untrusted_build_history(
    layout: _Layout, parent: list[str], child: list[str], message: str
) -> None:
    layout.histories = {layout.parent_id: parent, layout.child_id: child}

    with pytest.raises(RuntimeError, match=message):
        layout.verify()


def test_layout_proof_refuses_a_failed_alias_directory_probe(layout: _Layout) -> None:
    layout.extra["run"] = (1, "")

    with pytest.raises(RuntimeError, match="lacks its root-owned alias directory"):
        layout.verify()
    runs = [call for call in layout.docker.calls if call[0] == "run"]
    assert len(runs) == 1 and "--pull=never" in runs[0] and "--network=none" in runs[0]


def test_layout_proof_requires_inspection_to_match_requested_identity(layout: _Layout) -> None:
    layout.edit("child", "Id", "substituted")
    layout.publish()

    with pytest.raises(RuntimeError, match="inspection changed immutable identity"):
        project_image.verify_layout_parent_identity(
            layout.child_id, layout.parent_id, layout.executable
        )


def test_layout_history_requires_the_trusted_recipe(layout: _Layout, tmp_path: Path) -> None:
    from booley.runtime import paths

    # The packaged recipe always exists and ``package_data_dir`` has no override: only a
    # replaced data directory reaches the unavailable-recipe refusal.
    layout.monkeypatch.setattr(paths, "docker_data_dir", lambda: tmp_path)

    with pytest.raises(RuntimeError, match="trusted recipe is unavailable or malformed"):
        layout.verify()


def test_layout_docker_transport_failure_is_controlled(layout: _Layout, tmp_path: Path) -> None:
    missing = str(tmp_path / "missing-docker")
    records = layout.records
    parent, child = records[layout.parent_id], records[layout.child_id]
    with pytest.raises(RuntimeError, match="cannot inspect"):
        project_image.inspect_layout_image("candidate", executable=missing)
    with pytest.raises(RuntimeError, match="cannot verify Booley Directory layout build history"):
        project_image.verify_layout_image(parent, child, executable=missing)
    layout.extra[f"history {layout.parent_id}"] = (1, "")
    with pytest.raises(RuntimeError, match="cannot verify Booley Directory layout build history"):
        layout.verify()
    # The probe runs only after both histories succeeded through the same executable, so no
    # public call reaches this ``except`` exit; call the step itself.
    with pytest.raises(RuntimeError, match="cannot verify Booley Directory layout directory"):
        project_image._probe_layout_directory("image", executable=missing)


@pytest.mark.parametrize(
    "code,output",
    [(0, "not json"), (0, "{}"), (0, "[]"), (0, "[{}, {}]"), (0, "[null]"), (1, "no such image")],
)
def test_layout_inspection_rejects_unusable_docker_output(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, code: int, output: str
) -> None:
    docker = install_fake_docker(tmp_path, monkeypatch, {"image": (code, output)})

    with pytest.raises(RuntimeError, match="layout image"):
        project_image.inspect_layout_image("candidate", executable=str(docker.path))


def test_layout_inspection_preserves_exact_docker_record(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    record = {"Id": "sha256:" + "a" * 64, "Config": {"User": "1000"}}
    docker = install_fake_docker(tmp_path, monkeypatch, {"image": (0, json.dumps([record]))})

    assert project_image.inspect_layout_image("candidate", executable=str(docker.path)) == record
    assert docker.calls == [["image", "inspect", "candidate"]]


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


def test_layout_topology_accepts_symlinked_booley_dir_entry(tmp_path, monkeypatch) -> None:
    """A linked entry is checkout-local, like a plain ``.booley``; #2159 owns its Sandbox mount."""
    from tests.conftest import symlink_or_skip

    from booley.core.booley_dir import reset_cache

    root = tmp_path / "project"
    root.mkdir()
    (root / ".git").mkdir()
    data = tmp_path / "booley-data"
    data.mkdir()
    symlink_or_skip(root / ".booley", data)
    monkeypatch.delenv("BOOLEY_DIR", raising=False)
    reset_cache()

    assert project_image.layout_topology(root) is None
