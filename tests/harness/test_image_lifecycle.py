"""Authoritative Sandbox Image reconciliation (GitHub issue #128)."""

from __future__ import annotations

import subprocess
from contextlib import contextmanager
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

import pytest

from booley.harness import image_lifecycle as harness_lifecycle
from booley.runtime import image_lifecycle as lifecycle


class FakeDocker:
    """In-memory adapter for Docker, the lifecycle's true external dependency."""

    def __init__(self, images: dict[str, tuple[str, dict[str, str]]]) -> None:
        self.images = images
        self.registry_identities: dict[str, tuple[str, ...]] = {}
        self.used_image_ids: frozenset[str] = frozenset()
        self.mutations: list[tuple[str, ...]] = []

    def image_id(self, image: str) -> str | None:
        record = self.images.get(image)
        return record[0] if record else None

    def label(self, image: str, name: str) -> str | None:
        record = self.images.get(image)
        return record[1].get(name) if record else None

    def repo_digests(self, image: str) -> tuple[str, ...]:
        return self.registry_identities.get(image, ())

    def image_references(self) -> tuple[lifecycle.ImageReference, ...]:
        return tuple(
            lifecycle.ImageReference(reference, image_id)
            for reference, (image_id, _labels) in sorted(self.images.items())
            if not reference.startswith("sha256:")
        )

    def container_image_ids(self) -> frozenset[str]:
        return self.used_image_ids

    def tag(self, source: str, target: str) -> None:
        self.mutations.append(("tag", source, target))
        source_record = self.images.get(source)
        if source_record is None:
            source_record = next(record for record in self.images.values() if record[0] == source)
        self.images[target] = source_record

    def remove_tag(self, image: str) -> None:
        self.mutations.append(("remove_tag", image))
        self.images.pop(image, None)


class FakeBuilder:
    def __init__(self, docker: FakeDocker) -> None:
        self.docker = docker
        self.built: list[str] = []

    def build(self, node, *, force: bool, source: lifecycle.ArtifactSource) -> None:
        del force, source
        self.built.append(node.reference)
        labels = dict(node.expected_labels)
        labels[lifecycle.LABEL_BUILD_ORIGIN] = "local"
        labels[lifecycle.LABEL_PARENT_ARTIFACT_KIND] = lifecycle.PARENT_ARTIFACT_LOCAL_IMAGE_ID
        if node.reference == lifecycle.BASE_IMAGE:
            labels[lifecycle.LABEL_PARENT_ARTIFACT] = (
                self.docker.image_id(lifecycle.STABLE_RUNTIME_BASE_IMAGE) or ""
            )
        if node.parent is not None:
            labels[lifecycle.LABEL_PARENT_ARTIFACT] = self.docker.image_id(node.parent) or ""
        self.docker.images[node.reference] = (
            f"sha256:{len(self.built):064x}",
            labels,
        )


class FailingBuilder(FakeBuilder):
    def build(self, node, *, force: bool, source: lifecycle.ArtifactSource) -> None:
        super().build(node, force=force, source=source)
        raise lifecycle.ImageLifecycleError("build failed")


class FailOnSecondBuilder(FakeBuilder):
    def build(self, node, *, force: bool, source: lifecycle.ArtifactSource) -> None:
        super().build(node, force=force, source=source)
        if len(self.built) == 2:
            raise lifecycle.ImageLifecycleError("derived build failed")


def _install_planned_graph(docker: FakeDocker, nodes: tuple[lifecycle.ImageNode, ...]) -> None:
    for index, node in enumerate(nodes, start=1):
        image_id = f"sha256:{index:064x}"
        labels = dict(node.expected_labels)
        labels[lifecycle.LABEL_BUILD_ORIGIN] = "local"
        if node.parent is not None:
            labels[lifecycle.LABEL_PARENT_ARTIFACT] = docker.image_id(node.parent) or ""
            labels[lifecycle.LABEL_PARENT_ARTIFACT_KIND] = lifecycle.PARENT_ARTIFACT_LOCAL_IMAGE_ID
        if node.role is lifecycle.ImageRole.WHEEL_OVERLAY:
            labels[lifecycle.LABEL_WHEEL_SHA256] = "f" * 64
        docker.images[node.reference] = (image_id, labels)


class TransactionBuilder:
    def __init__(self, docker: FakeDocker) -> None:
        self.docker = docker
        self.built: list[tuple[lifecycle.ImageRole, str | None]] = []

    def prepare(self, node, *, candidate_reference: str, parent_reference: str | None) -> str:
        parent_id = self.docker.image_id(parent_reference) if parent_reference else None
        image_id = f"sha256:{len(self.built) + 20:064x}"
        labels = dict(node.expected_labels)
        labels[lifecycle.LABEL_BUILD_ORIGIN] = "local"
        if parent_id is not None:
            labels[lifecycle.LABEL_PARENT_ARTIFACT] = parent_id
            labels[lifecycle.LABEL_PARENT_ARTIFACT_KIND] = lifecycle.PARENT_ARTIFACT_LOCAL_IMAGE_ID
        if node.role is lifecycle.ImageRole.WHEEL_OVERLAY:
            labels[lifecycle.LABEL_WHEEL_SHA256] = "e" * 64
        self.docker.images[candidate_reference] = (image_id, labels)
        self.built.append((node.role, parent_id))
        return candidate_reference


def _project(tmp_path: Path, image: str | None = None) -> Path:
    root = tmp_path / "project"
    project_dir = root / ".booley_project"
    project_dir.mkdir(parents=True)
    body = "[sandbox]\n"
    if image is not None:
        body += f'image = "{image}"\n'
    (project_dir / "booley.toml").write_text(body, encoding="utf-8")
    return root


def _source_recipe_tree(tmp_path: Path) -> tuple[Path, Path]:
    source_root = tmp_path / "source"
    docker_dir = source_root / "src" / "booley" / "data" / "docker"
    docker_dir.mkdir(parents=True)
    (source_root / "pyproject.toml").write_text("[project]\nname='booley'\n", encoding="utf-8")
    (docker_dir / "Dockerfile").write_text("FROM scratch\n", encoding="utf-8")
    (docker_dir / "Dockerfile.riscv").write_text("FROM booley-sandbox\n", encoding="utf-8")
    return source_root, docker_dir


def _wire(monkeypatch: pytest.MonkeyPatch, docker: FakeDocker) -> FakeBuilder:
    from booley.runtime import docker_base_contract

    builder = FakeBuilder(docker)
    stable_id = "sha256:" + "9" * 64
    docker.images.setdefault(
        lifecycle.STABLE_RUNTIME_BASE_IMAGE,
        (stable_id, {"io.booley.runtime-base.contract": "stable-contract"}),
    )
    if lifecycle.BASE_IMAGE in docker.images:
        docker.images[lifecycle.BASE_IMAGE][1].setdefault(lifecycle.LABEL_BUILD_ORIGIN, "local")
        if lifecycle.LABEL_SCHEMA in docker.images[lifecycle.BASE_IMAGE][1]:
            docker.images[lifecycle.BASE_IMAGE][1].setdefault(
                lifecycle.LABEL_PARENT_ARTIFACT,
                stable_id,
            )
            docker.images[lifecycle.BASE_IMAGE][1].setdefault(
                lifecycle.LABEL_PARENT_ARTIFACT_KIND,
                lifecycle.PARENT_ARTIFACT_LOCAL_IMAGE_ID,
            )
    monkeypatch.setattr(lifecycle, "_expected_payload_fingerprint", lambda: "payload-new")
    monkeypatch.setattr(lifecycle, "_expected_version", lambda: "0.2.6")
    monkeypatch.setattr(docker_base_contract, "contract", lambda _root: "stable-contract")
    monkeypatch.setattr(lifecycle, "_docker_adapter", lambda: docker)
    monkeypatch.setattr(lifecycle, "_build_adapter", lambda *_args, **_kwargs: builder)
    return builder


def test_incremental_plan_builds_only_wheel_overlay_for_python_change(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = _project(tmp_path)
    docker = FakeDocker({})
    _wire(monkeypatch, docker)
    monkeypatch.setattr(lifecycle, "_expected_wheel_source_fingerprint", lambda: "wheel-old")
    initial = lifecycle.plan(lifecycle.ProjectImageScope(root), docker=docker)
    _install_planned_graph(docker, initial.nodes)

    monkeypatch.setattr(lifecycle, "_expected_wheel_source_fingerprint", lambda: "wheel-new")
    refreshed = lifecycle.plan(lifecycle.ProjectImageScope(root), docker=docker)

    assert [step.action for step in refreshed.steps] == [
        lifecycle.PlanAction.REUSE,
        lifecycle.PlanAction.REUSE,
        lifecycle.PlanAction.BUILD,
    ]
    assert refreshed.steps[-1].role is lifecycle.ImageRole.WHEEL_OVERLAY
    assert refreshed.steps[-1].reason.code == "inputs-changed"


def test_incremental_plan_produces_a_true_noop_for_current_graph(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = _project(tmp_path)
    docker = FakeDocker({})
    _wire(monkeypatch, docker)
    monkeypatch.setattr(lifecycle, "_expected_wheel_source_fingerprint", lambda: "wheel")
    initial = lifecycle.plan(lifecycle.ProjectImageScope(root), docker=docker)
    _install_planned_graph(docker, initial.nodes)

    current = lifecycle.plan(lifecycle.ProjectImageScope(root), docker=docker)

    assert all(step.action is lifecycle.PlanAction.REUSE for step in current.steps)


def test_incremental_plan_propagates_standard_change_only_to_descendants(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = _project(tmp_path, "booley-sandbox-riscv")
    docker = FakeDocker({})
    _wire(monkeypatch, docker)
    monkeypatch.setattr(lifecycle, "_expected_wheel_source_fingerprint", lambda: "wheel")
    initial = lifecycle.plan(lifecycle.ProjectImageScope(root), docker=docker)
    _install_planned_graph(docker, initial.nodes)
    original_hash = lifecycle._hash_paths
    monkeypatch.setattr(
        lifecycle,
        "_hash_paths",
        lambda root, paths: (
            "standard-new"
            if any("bwave" in path.as_posix() for path in paths)
            else original_hash(root, paths)
        ),
    )

    refreshed = lifecycle.plan(lifecycle.ProjectImageScope(root), docker=docker)

    assert [step.action for step in refreshed.steps] == [
        lifecycle.PlanAction.REUSE,
        lifecycle.PlanAction.BUILD,
        lifecycle.PlanAction.BUILD,
        lifecycle.PlanAction.BUILD,
    ]
    assert refreshed.steps[1].reason.code == "inputs-changed"
    assert all(step.reason.code == "parent-changed" for step in refreshed.steps[2:])


def test_riscv_requirements_select_generated_project_overlay(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = _project(tmp_path, "booley-sandbox-riscv")
    requirements = root / "requirements.txt"
    requirements.write_text("cocotb==2.0.0\n", encoding="utf-8")
    config = root / ".booley_project" / "booley.toml"
    config.write_text(
        '[sandbox]\nimage = "booley-sandbox-riscv"\npip_requirements = ["requirements.txt"]\n',
        encoding="utf-8",
    )
    docker = FakeDocker({})
    _wire(monkeypatch, docker)
    monkeypatch.setattr(lifecycle, "_expected_wheel_source_fingerprint", lambda: "wheel")

    planned = lifecycle.plan(lifecycle.ProjectImageScope(root), docker=docker)

    assert planned.selected_reference == lifecycle.project_image.project_image_name(root)
    assert [node.role for node in planned.nodes] == [
        lifecycle.ImageRole.RUNTIME_BASE,
        lifecycle.ImageRole.STANDARD_SUBSTRATE,
        lifecycle.ImageRole.RISCV_SUBSTRATE,
        lifecycle.ImageRole.PROJECT_SUBSTRATE,
        lifecycle.ImageRole.WHEEL_OVERLAY,
    ]


def test_prepare_uses_realized_parent_ids_and_commit_adopts_after_verification(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = _project(tmp_path)
    docker = FakeDocker({})
    _wire(monkeypatch, docker)
    monkeypatch.setattr(lifecycle, "_expected_wheel_source_fingerprint", lambda: "wheel")
    planned = lifecycle.plan(lifecycle.ProjectImageScope(root), docker=docker)
    builder = TransactionBuilder(docker)

    prepared = lifecycle.prepare(planned, docker=docker, builder=builder)

    assert docker.image_id(lifecycle.BASE_IMAGE) is None
    assert builder.built[1][1] == prepared.candidates[0].image_id
    assert builder.built[2][1] == prepared.candidates[1].image_id

    result = lifecycle.commit(prepared, docker=docker)

    assert result.changed_images == tuple(node.reference for node in planned.nodes)
    assert result.selected_id == prepared.candidates[-1].image_id
    assert result.wheel_source_fingerprint == "wheel"
    assert result.wheel_sha256 == "e" * 64


def test_prepare_removes_orphaned_transaction_candidates(tmp_path: Path) -> None:
    stale = "booley-lifecycle-crashed-wheel-overlay:candidate"
    docker = FakeDocker({stale: ("sha256:stale", {})})

    lifecycle._discard_orphaned_candidates(docker)

    assert stale not in docker.images
    assert docker.mutations == [("remove_tag", stale)]


def test_parent_artifact_kind_is_part_of_reuse_ancestry(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = _project(tmp_path)
    docker = FakeDocker({})
    _wire(monkeypatch, docker)
    monkeypatch.setattr(lifecycle, "_expected_wheel_source_fingerprint", lambda: "wheel")
    planned = lifecycle.plan(lifecycle.ProjectImageScope(root), docker=docker)
    _install_planned_graph(docker, planned.nodes)
    child = planned.nodes[1].reference
    docker.images[child][1][lifecycle.LABEL_PARENT_ARTIFACT_KIND] = (
        lifecycle.PARENT_ARTIFACT_REGISTRY_DIGEST
    )

    refreshed = lifecycle.plan(lifecycle.ProjectImageScope(root), docker=docker)

    assert refreshed.steps[1].reason.code == "parent-changed"


def test_validate_rejects_candidates_changed_after_preparation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = _project(tmp_path)
    docker = FakeDocker({})
    _wire(monkeypatch, docker)
    monkeypatch.setattr(lifecycle, "_expected_wheel_source_fingerprint", lambda: "wheel")
    planned = lifecycle.plan(lifecycle.ProjectImageScope(root), docker=docker)
    prepared = lifecycle.prepare(planned, docker=docker, builder=TransactionBuilder(docker))
    selected = prepared.candidates[-1]
    docker.images[selected.candidate_reference] = (
        "sha256:changed",
        docker.images[selected.candidate_reference][1],
    )

    with pytest.raises(lifecycle.ImageLifecycleError, match="changed before commit"):
        lifecycle.validate(prepared, docker=docker)


def test_incremental_adapter_build_inputs_cover_each_image_role(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from booley.harness.setup import docker_image

    root = _project(tmp_path)
    adapter = harness_lifecycle._IncrementalBuildAdapter(root, FakeDocker({}), verbose=False)
    build_root = tmp_path / "build"
    (build_root / "dist").mkdir(parents=True)
    wheel = build_root / "dist" / "booley_rtl-0.2.6.whl"
    wheel.write_bytes(b"wheel")
    monkeypatch.setattr(docker_image, "_runtime_base_build_metadata_args", lambda _root: ())
    monkeypatch.setattr(docker_image, "_image_build_metadata_args", lambda _root: ())
    monkeypatch.setattr(docker_image, "_docker_build_wheel", lambda *_args: True)
    monkeypatch.setattr(
        harness_lifecycle, "wheel_embedded_source_fingerprint", lambda _wheel: "wheel"
    )

    def node(role: lifecycle.ImageRole, source: str | None = None) -> lifecycle.ImageNode:
        payload = lifecycle.PayloadProvenance("3", "0.2.6", "payload")
        return lifecycle.ImageNode(
            role.value,
            tmp_path / f"{role.value}.Dockerfile",
            payload,
            lifecycle.BuildProvenance("recipe", None),
            role=role,
            wheel_source_fingerprint=source,
        )

    for role in (
        lifecycle.ImageRole.RUNTIME_BASE,
        lifecycle.ImageRole.STANDARD_SUBSTRATE,
        lifecycle.ImageRole.RISCV_SUBSTRATE,
        lifecycle.ImageRole.PROJECT_SUBSTRATE,
    ):
        build_context, _contexts, _args = adapter._role_build_inputs(
            SimpleNamespace(), node(role), build_root, "parent"
        )
        assert build_context

    assert adapter._role_build_inputs(
        SimpleNamespace(),
        node(lifecycle.ImageRole.RISCV_SUBSTRATE),
        build_root,
        "parent",
    ) == (
        harness_lifecycle.docker_data_dir(),
        (("booley-standard-substrate", "docker-image://parent"),),
        (),
    )

    _context, contexts, args = adapter._role_build_inputs(
        SimpleNamespace(), node(lifecycle.ImageRole.WHEEL_OVERLAY, "wheel"), build_root, "parent"
    )
    assert contexts == (("booley-substrate", "docker-image://parent"),)
    assert "BOOLEY_WHEEL_SHA256=" + "".join([]) not in args
    assert args[-2] == "--build-arg"

    monkeypatch.setattr(docker_image, "_docker_build_wheel", lambda *_args: False)
    assert (
        adapter._role_build_inputs(
            SimpleNamespace(),
            node(lifecycle.ImageRole.WHEEL_OVERLAY, "wheel"),
            build_root,
            "parent",
        )
        is None
    )


def _incremental_node(
    tmp_path: Path,
    role: lifecycle.ImageRole,
    *,
    source: str | None = None,
    policy: lifecycle.ArtifactPolicy = lifecycle.ArtifactPolicy.LOCAL_ONLY,
) -> lifecycle.ImageNode:
    return lifecycle.ImageNode(
        role.value,
        tmp_path / f"{role.value}.Dockerfile",
        lifecycle.PayloadProvenance("3", "0.2.6", "payload"),
        lifecycle.BuildProvenance("recipe", None),
        role=role,
        effective_inputs="inputs",
        wheel_source_fingerprint=source,
        acquisition_policy=policy,
    )


def test_incremental_adapter_prepare_rejects_missing_parent_and_missing_candidate(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    docker = FakeDocker({})
    adapter = harness_lifecycle._IncrementalBuildAdapter(tmp_path, docker, verbose=False)
    node = _incremental_node(tmp_path, lifecycle.ImageRole.STANDARD_SUBSTRATE)

    with pytest.raises(harness_lifecycle.ImageLifecycleError, match="prepared parent"):
        adapter.prepare(node, candidate_reference="candidate", parent_reference="parent")

    monkeypatch.setattr(adapter, "_build_role", lambda *_args: None)
    with pytest.raises(harness_lifecycle.ImageLifecycleError, match="produced no image"):
        adapter.prepare(node, candidate_reference="candidate", parent_reference=None)


def test_incremental_adapter_prepare_handles_release_and_build_failures(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    docker = FakeDocker({})
    adapter = harness_lifecycle._IncrementalBuildAdapter(tmp_path, docker, verbose=False)
    release = _incremental_node(
        tmp_path,
        lifecycle.ImageRole.WHEEL_OVERLAY,
        source="wheel",
        policy=lifecycle.ArtifactPolicy.VERIFIED_RELEASE_ONLY,
    )
    monkeypatch.setattr(adapter, "_pull_complete_release", lambda _node: "remote:image")
    assert adapter.prepare(release, candidate_reference="candidate", parent_reference=None) == (
        "remote:image"
    )

    def fail_build(context, *_args) -> None:
        context.record("docker_image", "err", "build failed")

    monkeypatch.setattr(adapter, "_build_role", fail_build)
    with pytest.raises(harness_lifecycle.ImageLifecycleError, match="build failed"):
        adapter.prepare(
            _incremental_node(tmp_path, lifecycle.ImageRole.STANDARD_SUBSTRATE),
            candidate_reference="candidate",
            parent_reference=None,
        )


def test_incremental_adapter_build_role_records_result_and_wheel_labels(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from booley.harness.setup import docker_image

    docker = FakeDocker({"parent": ("sha256:" + "a" * 64, {})})
    adapter = harness_lifecycle._IncrementalBuildAdapter(tmp_path, docker, verbose=False)
    node = _incremental_node(tmp_path, lifecycle.ImageRole.WHEEL_OVERLAY, source="wheel")
    adapter._wheel_sha256 = "f" * 64
    captured: list[object] = []

    monkeypatch.setattr(
        harness_lifecycle,
        "docker_data_dir",
        lambda: tmp_path / "repo" / "src" / "booley" / "data" / "docker",
    )
    monkeypatch.setattr(
        adapter,
        "_role_build_inputs",
        lambda *_args: (tmp_path, (("booley-substrate", "docker-image://parent"),), ()),
    )
    monkeypatch.setattr(
        docker_image,
        "_docker_build_image",
        lambda _context, spec: captured.append(spec) or 1,
    )

    class Context:
        def __init__(self) -> None:
            self.results: list[SimpleNamespace] = []

        def record(self, *args: str) -> None:
            self.results.append(SimpleNamespace(status=args[1], detail=args[2]))

    context = Context()
    adapter._build_role(context, node, "candidate", "parent", docker.image_id("parent"))

    assert context.results[-1].detail == "wheel-overlay build failed"
    spec = captured[0]
    assert (harness_lifecycle.runtime_lifecycle.LABEL_WHEEL_SHA256, "f" * 64) in spec.labels


def test_incremental_riscv_build_spec_preserves_context_and_parent(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from booley.harness.setup import docker_image

    parent_id = "sha256:" + "a" * 64
    docker = FakeDocker({"parent": (parent_id, {})})
    adapter = harness_lifecycle._IncrementalBuildAdapter(tmp_path, docker, verbose=False)
    node = _incremental_node(tmp_path, lifecycle.ImageRole.RISCV_SUBSTRATE)
    captured = []
    monkeypatch.setattr(harness_lifecycle, "docker_data_dir", lambda: tmp_path / "docker")
    monkeypatch.setattr(
        docker_image,
        "_docker_build_image",
        lambda _context, spec: captured.append(spec) or 0,
    )

    adapter._build_role(
        SimpleNamespace(record=lambda *_args: None), node, "candidate", "parent", parent_id
    )

    assert len(captured) == 1
    assert captured[0].build_contexts == (("booley-standard-substrate", "docker-image://parent"),)
    assert captured[0].parent_artifact == parent_id


@pytest.mark.parametrize("wheel_count", [0, 2])
def test_incremental_adapter_rejects_ambiguous_wheel_output(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, wheel_count: int
) -> None:
    from booley.harness.setup import docker_image

    root = tmp_path / "build"
    (root / "dist").mkdir(parents=True)
    for index in range(wheel_count):
        (root / "dist" / f"booley_rtl-0.2.{index}.whl").write_bytes(b"wheel")
    adapter = harness_lifecycle._IncrementalBuildAdapter(tmp_path, FakeDocker({}), verbose=False)
    node = _incremental_node(tmp_path, lifecycle.ImageRole.WHEEL_OVERLAY, source="wheel")
    monkeypatch.setattr(docker_image, "_docker_build_wheel", lambda *_args: True)
    with pytest.raises(harness_lifecycle.ImageLifecycleError, match="exactly one wheel"):
        adapter._role_build_inputs(SimpleNamespace(), node, root, "parent")


def test_incremental_adapter_rejects_wheel_source_drift(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from booley.harness.setup import docker_image

    root = tmp_path / "build"
    (root / "dist").mkdir(parents=True)
    (root / "dist" / "booley_rtl-0.2.6.whl").write_bytes(b"wheel")
    adapter = harness_lifecycle._IncrementalBuildAdapter(tmp_path, FakeDocker({}), verbose=False)
    node = _incremental_node(tmp_path, lifecycle.ImageRole.WHEEL_OVERLAY, source="expected")
    monkeypatch.setattr(docker_image, "_docker_build_wheel", lambda *_args: True)
    monkeypatch.setattr(
        harness_lifecycle, "wheel_embedded_source_fingerprint", lambda _wheel: "actual"
    )
    with pytest.raises(harness_lifecycle.ImageLifecycleError, match="source fingerprint"):
        adapter._role_build_inputs(SimpleNamespace(), node, root, "parent")


def test_runtime_graph_rejects_missing_wheel_and_user_owned_project_recipe(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = _project(tmp_path)
    monkeypatch.setattr(lifecycle, "_expected_wheel_source_fingerprint", lambda: None)
    with pytest.raises(lifecycle.ImageLifecycleError, match="wheel-source fingerprint"):
        lifecycle._source_graph(root, lifecycle.BASE_IMAGE)

    project_docker = root / ".booley_project" / "docker"
    project_docker.mkdir()
    (project_docker / "Dockerfile").write_text("FROM custom:image\n", encoding="utf-8")
    monkeypatch.setattr(lifecycle, "_expected_wheel_source_fingerprint", lambda: "wheel")
    with pytest.raises(lifecycle.ImageLifecycleError, match="user-owned Project Docker"):
        lifecycle._source_graph_project(
            root,
            lifecycle.project_image.project_image_name(root),
            _incremental_node(tmp_path, lifecycle.ImageRole.STANDARD_SUBSTRATE),
        )


def test_runtime_plan_rejects_wrong_scope_and_external_image(tmp_path: Path) -> None:
    docker = FakeDocker({})
    with pytest.raises(TypeError, match="ProjectImageScope"):
        lifecycle.plan(lifecycle.HostImageScope(), docker=docker)
    with pytest.raises(lifecycle.ImageLifecycleError, match="externally managed"):
        lifecycle.plan(
            lifecycle.ProjectImageScope(_project(tmp_path, "acme/custom")), docker=docker
        )


def test_runtime_provenance_and_prepared_candidate_validation(tmp_path: Path) -> None:
    node = _incremental_node(tmp_path, lifecycle.ImageRole.WHEEL_OVERLAY, source="wheel")
    labels = dict(node.expected_labels)
    labels.update(
        {
            lifecycle.LABEL_BUILD_ORIGIN: "local",
            lifecycle.LABEL_WHEEL_SHA256: "f" * 64,
        }
    )
    docker = FakeDocker(
        {
            node.reference: ("sha256:" + "a" * 64, labels),
            "candidate": ("sha256:" + "a" * 64, labels),
        }
    )
    assert lifecycle._node_provenance_reason(node, docker) is None
    docker.images["candidate"][1][lifecycle.LABEL_RECIPE_FINGERPRINT] = "changed"
    assert lifecycle._node_provenance_reason(node, docker).code == "recipe-changed"
    docker.images["candidate"][1][lifecycle.LABEL_RECIPE_FINGERPRINT] = (
        node.build.recipe_fingerprint
    )
    docker.images["candidate"][1].pop(lifecycle.LABEL_WHEEL_SHA256)
    assert lifecycle._node_provenance_reason(node, docker).code == "inputs-changed"
    with pytest.raises(lifecycle.ImageLifecycleError, match="no wheel SHA"):
        lifecycle._verify_prepared_image(node, "candidate", None, docker)
    with pytest.raises(lifecycle.ImageLifecycleError, match="disappeared"):
        lifecycle._verify_prepared_image(node, "missing", None, docker)


def test_runtime_release_parent_validation_and_adapter_guards(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    node = _incremental_node(
        tmp_path,
        lifecycle.ImageRole.WHEEL_OVERLAY,
        source="wheel",
        policy=lifecycle.ArtifactPolicy.VERIFIED_RELEASE_ONLY,
    )
    docker = FakeDocker({"release": ("sha256:" + "a" * 64, {})})
    with pytest.raises(lifecycle.ImageLifecycleError, match="registry ancestry"):
        lifecycle._verify_prepared_image(node, "release", None, docker)
    with pytest.raises(TypeError, match="build adapter"):
        lifecycle._build_adapter(tmp_path, docker, verbose=False)
    with pytest.raises(TypeError, match="transaction build adapter"):
        lifecycle._transaction_build_adapter(tmp_path, docker, verbose=False)
    monkeypatch.setattr(lifecycle, "_expected_wheel_source_fingerprint", lambda: None)
    with pytest.raises(lifecycle.ImageLifecycleError, match="wheel-source fingerprint"):
        lifecycle._complete_release_node(lifecycle.BASE_IMAGE)


def test_incremental_adapter_release_pull_and_project_recipe_paths(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from booley.harness.setup import docker_image

    adapter = harness_lifecycle._IncrementalBuildAdapter(tmp_path, FakeDocker({}), verbose=False)
    release = _incremental_node(tmp_path, lifecycle.ImageRole.WHEEL_OVERLAY, source="wheel")
    release = replace(release, reference=lifecycle.BASE_IMAGE)
    monkeypatch.setattr(docker_image, "_try_pull_image", lambda *_args, **_kwargs: True)
    monkeypatch.setattr(
        docker_image, "remote_tag", lambda image, version: f"remote:{image}:{version}"
    )
    assert adapter._pull_complete_release(release).startswith("remote:booley-sandbox")
    monkeypatch.setattr(docker_image, "_try_pull_image", lambda *_args, **_kwargs: False)
    with pytest.raises(harness_lifecycle.ImageLifecycleError, match="could not pull"):
        adapter._pull_complete_release(release)
    with pytest.raises(harness_lifecycle.ImageLifecycleError, match="no complete"):
        adapter._pull_complete_release(
            _incremental_node(tmp_path, lifecycle.ImageRole.STANDARD_SUBSTRATE)
        )

    project = _incremental_node(tmp_path, lifecycle.ImageRole.PROJECT_SUBSTRATE)
    monkeypatch.setattr(harness_lifecycle, "resolve_checkout_project_dir", lambda _root: tmp_path)
    monkeypatch.setattr(
        harness_lifecycle.runtime_lifecycle, "_project_requirements_body", lambda _root: "req"
    )
    written: list[tuple[Path, str]] = []
    monkeypatch.setattr(
        harness_lifecycle.project_image,
        "write_project_image_files",
        lambda path, body, **_kwargs: written.append((path, body)),
    )
    adapter._materialize_managed_project_recipe(project)
    assert written == [(tmp_path / "docker", "req")]


def test_incremental_adapter_handles_noop_build_and_unsupported_role(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    adapter = harness_lifecycle._IncrementalBuildAdapter(tmp_path, FakeDocker({}), verbose=False)
    context = SimpleNamespace(record=lambda *_args: None)
    node = _incremental_node(tmp_path, lifecycle.ImageRole.STANDARD_SUBSTRATE)
    monkeypatch.setattr(adapter, "_role_build_inputs", lambda *_args: None)
    adapter._build_role(context, node, "candidate", None, None)
    monkeypatch.setattr(adapter, "_role_build_inputs", lambda *_args: (tmp_path, (), ()))
    from booley.harness.setup import docker_image

    monkeypatch.setattr(docker_image, "_docker_build_image", lambda *_args: None)
    adapter._build_role(context, node, "candidate", None, None)
    unsupported = SimpleNamespace(role="unsupported")
    with pytest.raises(harness_lifecycle.ImageLifecycleError, match="unsupported image role"):
        harness_lifecycle._IncrementalBuildAdapter._role_build_inputs(
            adapter, context, unsupported, tmp_path, None
        )


def test_harness_image_lifecycle_wrappers_and_distribution_policy(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import booley
    from booley.runtime.version_attribution import VersionOrigin

    docker = FakeDocker({})
    monkeypatch.setattr(harness_lifecycle, "_docker_adapter", lambda: docker)
    monkeypatch.setattr(
        harness_lifecycle, "_transaction_build_adapter", lambda *args, **kwargs: "builder"
    )
    expected = SimpleNamespace(project_root=tmp_path)
    monkeypatch.setattr(
        harness_lifecycle.runtime_lifecycle, "plan", lambda *args, **kwargs: expected
    )
    monkeypatch.setattr(
        harness_lifecycle.runtime_lifecycle, "prepare", lambda *args, **kwargs: expected
    )
    monkeypatch.setattr(
        harness_lifecycle.runtime_lifecycle, "commit", lambda *args, **kwargs: expected
    )
    monkeypatch.setattr(
        harness_lifecycle.runtime_lifecycle, "validate", lambda *args, **kwargs: None
    )
    monkeypatch.setattr(harness_lifecycle.runtime_lifecycle, "abort", lambda *args, **kwargs: None)
    scope = lifecycle.ProjectImageScope(tmp_path)
    assert harness_lifecycle.plan(scope) is expected
    assert harness_lifecycle.prepare(expected) is expected
    assert harness_lifecycle.commit(expected) is expected
    harness_lifecycle.validate(expected)
    harness_lifecycle.abort(expected)

    monkeypatch.setattr(
        booley,
        "version_attribution",
        SimpleNamespace(origin=VersionOrigin.DISTRIBUTION),
    )
    monkeypatch.setattr(harness_lifecycle, "embedded_official_release", lambda: True)
    assert harness_lifecycle._artifact_policy() is lifecycle.ArtifactPolicy.VERIFIED_RELEASE_ONLY
    monkeypatch.setattr(harness_lifecycle, "embedded_official_release", lambda: False)
    assert harness_lifecycle._artifact_policy() is lifecycle.ArtifactPolicy.LOCAL_ONLY
    monkeypatch.setattr(harness_lifecycle, "_uses_managed_riscv_project", lambda _root: True)
    assert (
        harness_lifecycle._artifact_policy(tmp_path)
        is lifecycle.ArtifactPolicy.VERIFIED_RELEASE_THEN_LOCAL
    )


def test_official_release_plan_observes_only_selected_complete_image(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = _project(tmp_path, "booley-sandbox-riscv")
    docker = FakeDocker({})
    _wire(monkeypatch, docker)
    monkeypatch.setattr(lifecycle, "_expected_wheel_source_fingerprint", lambda: "wheel")
    node = lifecycle._complete_release_node("booley-sandbox-riscv")
    labels = dict(node.expected_labels)
    labels.update(
        {
            lifecycle.LABEL_BUILD_ORIGIN: "registry",
            lifecycle.LABEL_PARENT_ARTIFACT_KIND: (lifecycle.PARENT_ARTIFACT_REGISTRY_DIGEST),
            lifecycle.LABEL_PARENT_ARTIFACT: (
                "ghcr.io/boldaxolotl/booley-sandbox-base@sha256:" + "d" * 64
            ),
            lifecycle.LABEL_WHEEL_SHA256: "e" * 64,
        }
    )
    docker.images[node.reference] = ("sha256:" + "a" * 64, labels)

    planned = lifecycle.plan(
        lifecycle.ProjectImageScope(root),
        docker=docker,
        artifact_policy=lifecycle.ArtifactPolicy.VERIFIED_RELEASE_ONLY,
    )

    assert planned.nodes == (node,)
    assert planned.steps[0].action is lifecycle.PlanAction.REUSE


def _labels(*, payload: str, recipe: str, parent: str | None = None) -> dict[str, str]:
    values = {
        lifecycle.LABEL_SCHEMA: lifecycle.PROVENANCE_SCHEMA,
        lifecycle.LABEL_PAYLOAD_FINGERPRINT: payload,
        lifecycle.LEGACY_FINGERPRINT_LABEL: payload,
        lifecycle.LABEL_RECIPE_FINGERPRINT: recipe,
        lifecycle.LABEL_VERSION: "0.2.6",
        lifecycle.LABEL_BUILD_ORIGIN: "local",
    }
    if parent is not None:
        values[lifecycle.LABEL_PARENT_ARTIFACT] = parent
        values[lifecycle.LABEL_PARENT_ARTIFACT_KIND] = lifecycle.PARENT_ARTIFACT_LOCAL_IMAGE_ID
    return values


def _current_registry_base() -> tuple[str, dict[str, str]]:
    payload = lifecycle.PayloadProvenance(
        lifecycle.PROVENANCE_SCHEMA,
        "0.2.6",
        "payload-new",
    )
    node = lifecycle._base_node(payload)
    image_id = "sha256:" + "a" * 64
    labels = _registry_labels(
        node,
        "ghcr.io/boldaxolotl/booley-sandbox-base@sha256:" + "e" * 64,
    )
    return image_id, labels


def _registry_labels(
    node: lifecycle.ImageNode,
    parent: str,
    *,
    legacy: bool = False,
    overrides: dict[str, str] | None = None,
) -> dict[str, str]:
    labels = dict(node.expected_labels)
    labels.update(
        {
            lifecycle.LABEL_BUILD_ORIGIN: "registry",
            lifecycle.LABEL_PARENT_ARTIFACT: parent,
            lifecycle.LABEL_PARENT_ARTIFACT_KIND: lifecycle.PARENT_ARTIFACT_REGISTRY_DIGEST,
            **(overrides or {}),
        }
    )
    if legacy:
        labels[lifecycle.LABEL_SCHEMA] = lifecycle.LEGACY_PROVENANCE_SCHEMA
        labels.pop(lifecycle.LABEL_PARENT_ARTIFACT_KIND)
    return labels


def _stage_registry_candidate(
    docker: FakeDocker,
    node: lifecycle.ImageNode,
    reference: str,
    *,
    image_id: str | None = None,
    label_overrides: dict[str, str] | None = None,
) -> str:
    labels = _registry_labels(
        node,
        "ghcr.io/boldaxolotl/booley-sandbox-base@sha256:" + "e" * 64,
        overrides=label_overrides,
    )
    docker.images[reference] = (image_id or "sha256:" + "a" * 64, labels)
    return reference


class RegistryChainBuilder:
    def __init__(self, docker: FakeDocker, *, base_digest: str | None = None) -> None:
        self.docker = docker
        self.built: list[str] = []
        self.base_digest = base_digest or ("ghcr.io/boldaxolotl/booley-sandbox@sha256:" + "d" * 64)

    def build(
        self,
        node: lifecycle.ImageNode,
        *,
        force: bool,
        source: lifecycle.ArtifactSource,
    ) -> str:
        del force
        assert source is lifecycle.ArtifactSource.VERIFIED_RELEASE_PULL
        self.built.append(node.reference)
        release = lifecycle._published_release_repository(node.reference) + ":0.2.6"
        if node.reference == lifecycle.BASE_IMAGE:
            self.docker.registry_identities[lifecycle.BASE_IMAGE] = (self.base_digest,)
            return _stage_registry_candidate(self.docker, node, release)
        return _stage_registry_candidate(
            self.docker,
            node,
            release,
            image_id="sha256:" + "c" * 64,
            label_overrides={lifecycle.LABEL_PARENT_ARTIFACT: self.base_digest},
        )


def test_host_scope_never_reads_project_configuration(monkeypatch):
    docker = FakeDocker({})
    _wire(monkeypatch, docker)
    monkeypatch.setattr(
        lifecycle,
        "_selected_reference",
        lambda _root: (_ for _ in ()).throw(AssertionError("Project config read")),
    )

    result = lifecycle.reconcile(lifecycle.HostImageScope(), lifecycle.Intent.CHECK)

    assert result.selected_reference == lifecycle.BASE_IMAGE
    assert result.status is lifecycle.Status.STALE


def test_host_ensure_removes_all_bootstrap_release_tags_when_base_is_current(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    image_id, labels = _current_registry_base()
    current_release = "ghcr.io/boldaxolotl/booley-sandbox:0.2.6"
    prior_release = "ghcr.io/boldaxolotl/booley-sandbox:0.2.5"
    docker = FakeDocker(
        {
            lifecycle.BASE_IMAGE: (image_id, labels),
            current_release: (image_id, labels),
            prior_release: ("sha256:" + "b" * 64, {}),
        }
    )
    docker.used_image_ids = frozenset({image_id})
    builder = _wire(monkeypatch, docker)

    result = lifecycle.reconcile(
        lifecycle.HostImageScope(),
        lifecycle.Intent.ENSURE,
        artifact_policy=lifecycle.ArtifactPolicy.VERIFIED_RELEASE_ONLY,
    )

    assert not builder.built
    assert result.status is lifecycle.Status.CHANGED
    assert result.changed_images == ()
    assert result.cleanup.removed == (prior_release, current_release)
    assert docker.image_id(lifecycle.BASE_IMAGE) == image_id
    assert docker.image_id(prior_release) is None
    assert docker.image_id(current_release) is None


def test_host_check_reports_release_cleanup_without_mutating(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    image_id, labels = _current_registry_base()
    prior_release = "ghcr.io/boldaxolotl/booley-sandbox:0.2.5"
    docker = FakeDocker(
        {
            lifecycle.BASE_IMAGE: (image_id, labels),
            prior_release: ("sha256:" + "b" * 64, {}),
            "ghcr.io/boldaxolotl/booley-sandbox:latest": (image_id, labels),
            "example.com/booley-sandbox:0.2.4": ("sha256:" + "c" * 64, {}),
        }
    )
    _wire(monkeypatch, docker)

    result = lifecycle.reconcile(
        lifecycle.HostImageScope(),
        lifecycle.Intent.CHECK,
        artifact_policy=lifecycle.ArtifactPolicy.VERIFIED_RELEASE_ONLY,
    )

    assert result.status is lifecycle.Status.CURRENT
    assert result.cleanup.pending == (prior_release,)
    assert not result.cleanup.removed
    assert not docker.mutations


def test_host_ensure_retains_release_image_required_by_container(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    prior_id = "sha256:" + "b" * 64
    prior_release = "ghcr.io/boldaxolotl/booley-sandbox:0.2.5"
    docker = FakeDocker({prior_release: (prior_id, {})})
    docker.used_image_ids = frozenset({prior_id})
    _wire(monkeypatch, docker)

    result = lifecycle.reconcile(lifecycle.HostImageScope(), lifecycle.Intent.ENSURE)

    assert result.cleanup.retained_required == (prior_release,)
    assert docker.image_id(prior_release) == prior_id


def test_host_cleanup_refuses_reference_that_changed_after_inventory(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    release = "ghcr.io/boldaxolotl/booley-sandbox:0.2.5"

    class RetaggedDocker(FakeDocker):
        def image_id(self, image: str) -> str | None:
            if image == release:
                return "sha256:" + "c" * 64
            return super().image_id(image)

    docker = RetaggedDocker({release: ("sha256:" + "b" * 64, {})})
    _wire(monkeypatch, docker)

    with pytest.raises(lifecycle.ImageLifecycleError, match="refused to clean changed"):
        lifecycle.reconcile(lifecycle.HostImageScope(), lifecycle.Intent.ENSURE)

    assert not any(mutation == ("remove_tag", release) for mutation in docker.mutations)


def test_host_cleanup_failure_does_not_roll_back_reconciled_base(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    release = "ghcr.io/boldaxolotl/booley-sandbox:0.2.5"

    class CleanupFailureDocker(FakeDocker):
        def remove_tag(self, image: str) -> None:
            if image == release:
                raise lifecycle.ImageLifecycleError("cleanup denied")
            super().remove_tag(image)

    docker = CleanupFailureDocker({release: ("sha256:" + "b" * 64, {})})
    builder = _wire(monkeypatch, docker)

    with pytest.raises(lifecycle.ImageLifecycleError, match="cleanup denied"):
        lifecycle.reconcile(lifecycle.HostImageScope(), lifecycle.Intent.ENSURE)

    assert builder.built == [lifecycle.BASE_IMAGE]
    assert docker.image_id(lifecycle.BASE_IMAGE) is not None
    assert docker.image_id(release) is not None


def test_composed_force_refreshes_host_base_exactly_once(tmp_path: Path, monkeypatch):
    root = _project(tmp_path, "booley-sandbox-riscv")
    docker = FakeDocker({})
    builder = _wire(monkeypatch, docker)

    base = lifecycle.reconcile(lifecycle.HostImageScope(), lifecycle.Intent.REFRESH)
    result = lifecycle.reconcile(
        lifecycle.ProjectImageScope(root, base),
        lifecycle.Intent.REFRESH,
    )

    assert builder.built.count(lifecycle.BASE_IMAGE) == 1
    assert builder.built == [lifecycle.BASE_IMAGE, "booley-sandbox-riscv"]
    assert result.status is lifecycle.Status.CHANGED


def test_project_ensure_cleans_release_tags_for_selected_shipped_flavor(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = _project(tmp_path, "booley-sandbox-riscv")
    docker = FakeDocker({})
    builder = _wire(monkeypatch, docker)
    lifecycle.reconcile(lifecycle.ProjectImageScope(root), lifecycle.Intent.ENSURE)
    flavor_id = docker.image_id("booley-sandbox-riscv")
    assert flavor_id is not None
    current_release = "ghcr.io/boldaxolotl/booley-sandbox-riscv:0.2.6"
    prior_release = "ghcr.io/boldaxolotl/booley-sandbox-riscv:0.2.5"
    flavor_labels = docker.images["booley-sandbox-riscv"][1]
    docker.images[current_release] = (flavor_id, flavor_labels)
    docker.images[prior_release] = ("sha256:" + "e" * 64, {})
    docker.mutations.clear()
    builder.built.clear()

    result = lifecycle.reconcile(lifecycle.ProjectImageScope(root), lifecycle.Intent.ENSURE)

    assert not builder.built
    assert result.status is lifecycle.Status.CHANGED
    assert result.cleanup.removed == (prior_release, current_release)
    assert docker.image_id("booley-sandbox-riscv") == flavor_id


def test_check_rejects_same_version_with_different_payload(tmp_path: Path, monkeypatch):
    root = _project(tmp_path)
    docker = FakeDocker(
        {
            "booley-sandbox": (
                "sha256:" + "a" * 64,
                _labels(payload="payload-old", recipe="ignored"),
            )
        }
    )
    builder = _wire(monkeypatch, docker)

    result = lifecycle.reconcile(lifecycle.ProjectImageScope(root), lifecycle.Intent.CHECK)

    assert result.status is lifecycle.Status.STALE
    assert result.selected_id == "sha256:" + "a" * 64
    assert not builder.built
    assert not docker.mutations


def test_check_rejects_local_base_when_stable_contract_changed(tmp_path: Path, monkeypatch):
    root = _project(tmp_path)
    stable_id = "sha256:" + "9" * 64
    payload = lifecycle.PayloadProvenance("1", "0.2.6", "payload-new")
    recipe = lifecycle._base_node(payload).build.recipe_fingerprint
    docker = FakeDocker(
        {
            lifecycle.STABLE_RUNTIME_BASE_IMAGE: (
                stable_id,
                {"io.booley.runtime-base.contract": "old-contract"},
            ),
            lifecycle.BASE_IMAGE: (
                "sha256:" + "a" * 64,
                _labels(payload="payload-new", recipe=recipe, parent=stable_id),
            ),
        }
    )
    _wire(monkeypatch, docker)
    docker.images[lifecycle.STABLE_RUNTIME_BASE_IMAGE][1]["io.booley.runtime-base.contract"] = (
        "old-contract"
    )

    result = lifecycle.reconcile(lifecycle.ProjectImageScope(root), lifecycle.Intent.CHECK)

    assert result.status is lifecycle.Status.STALE


def test_packaged_install_accepts_exact_local_parent_when_contract_is_unavailable(
    tmp_path: Path, monkeypatch
):
    from booley.runtime import docker_base_contract

    root = _project(tmp_path)
    stable_id = "sha256:" + "9" * 64
    payload = lifecycle.PayloadProvenance("1", "0.2.6", "payload-new")
    recipe = lifecycle._base_node(payload).build.recipe_fingerprint
    docker = FakeDocker(
        {
            lifecycle.STABLE_RUNTIME_BASE_IMAGE: (stable_id, {}),
            lifecycle.BASE_IMAGE: (
                "sha256:" + "a" * 64,
                _labels(payload="payload-new", recipe=recipe, parent=stable_id),
            ),
        }
    )
    _wire(monkeypatch, docker)
    monkeypatch.setattr(
        docker_base_contract,
        "contract",
        lambda _root: (_ for _ in ()).throw(ValueError("manifest absent")),
    )

    assert (
        lifecycle.reconcile(lifecycle.ProjectImageScope(root), lifecycle.Intent.CHECK).status
        is lifecycle.Status.CURRENT
    )


def test_ensure_rebuilds_base_then_flavor_and_returns_exact_id(tmp_path: Path, monkeypatch):
    root = _project(tmp_path, "booley-sandbox-riscv")
    base_id = "sha256:" + "b" * 64
    docker = FakeDocker(
        {
            "booley-sandbox": (
                base_id,
                _labels(payload="payload-old", recipe="old-base"),
            ),
            "booley-sandbox-riscv": (
                "sha256:" + "c" * 64,
                _labels(payload="payload-old", recipe="old-flavor", parent=base_id),
            ),
        }
    )
    builder = _wire(monkeypatch, docker)

    result = lifecycle.reconcile(lifecycle.ProjectImageScope(root), lifecycle.Intent.ENSURE)

    assert builder.built == ["booley-sandbox", "booley-sandbox-riscv"]
    assert result.status is lifecycle.Status.CHANGED
    assert result.selected_id == docker.image_id("booley-sandbox-riscv")
    assert result.requires_spec_reseed is True
    assert result.requires_runtime_recreation is True


def test_source_checkout_missing_flavor_builds_without_registry_pull(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = _project(tmp_path, "booley-sandbox-riscv")
    _docker, pulls, builds = _wire_source_chain(root, tmp_path, monkeypatch)
    harness_lifecycle.reconcile(lifecycle.HostImageScope(), lifecycle.Intent.ENSURE)
    builds.clear()

    result = harness_lifecycle.reconcile(
        lifecycle.ProjectImageScope(root), lifecycle.Intent.ENSURE
    )

    assert pulls == []
    assert builds == ["booley-sandbox-riscv"]
    assert result.status is lifecycle.Status.CHANGED


def _wire_distribution_pull(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> tuple[FakeDocker, list[tuple[str, str]]]:
    import booley
    from booley.harness.setup import docker_image as init_docker_image
    from booley.runtime.version_attribution import VersionAttribution, VersionOrigin

    docker_dir = tmp_path / "site-packages" / "booley" / "data" / "docker"
    docker_dir.mkdir(parents=True)
    (docker_dir / "Dockerfile").write_text("FROM scratch\n", encoding="utf-8")
    monkeypatch.setattr(harness_lifecycle, "docker_data_dir", lambda: docker_dir)
    monkeypatch.setattr(lifecycle, "docker_data_dir", lambda: docker_dir)
    monkeypatch.setattr(
        booley,
        "version_attribution",
        VersionAttribution(
            version="0.2.6",
            origin=VersionOrigin.DISTRIBUTION,
            distribution_name="booley-rtl",
        ),
    )
    docker = FakeDocker({})
    _wire(monkeypatch, docker)
    monkeypatch.setattr(harness_lifecycle, "_docker_adapter", lambda: docker)
    pulls: list[tuple[str, str]] = []

    def pull(version: str, image: str = lifecycle.BASE_IMAGE, *, adopt: bool = True) -> bool:
        assert adopt is False
        pulls.append((version, image))
        payload = lifecycle.PayloadProvenance(lifecycle.PROVENANCE_SCHEMA, version, "payload-new")
        node = lifecycle._base_node(payload)
        _stage_registry_candidate(docker, node, init_docker_image.remote_tag(image, version))
        return True

    monkeypatch.setattr(init_docker_image, "_try_pull_image", pull)
    return docker, pulls


def test_packaged_distribution_missing_base_pulls_verified_release(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from booley.harness.setup import docker_image as init_docker_image

    _docker, pulls = _wire_distribution_pull(tmp_path, monkeypatch)
    monkeypatch.setattr(harness_lifecycle, "embedded_official_release", lambda: True)
    monkeypatch.setattr(
        init_docker_image,
        "_step_docker_image",
        lambda *_args, **_kwargs: pytest.fail("packaged distribution attempted a local build"),
    )

    result = harness_lifecycle.reconcile(lifecycle.HostImageScope(), lifecycle.Intent.ENSURE)

    assert pulls == [("0.2.6", lifecycle.BASE_IMAGE)]
    assert result.status is lifecycle.Status.CHANGED


def test_development_distribution_missing_base_builds_locally(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from booley.harness.setup import docker_image as init_docker_image

    docker, pulls = _wire_distribution_pull(tmp_path, monkeypatch)
    context_root = tmp_path / "verified-context"
    context_docker = context_root / "src" / "booley" / "data" / "docker"
    context_docker.mkdir(parents=True)
    calls: list[tuple[Path, bool, str | None, bool]] = []

    @contextmanager
    def _context():
        yield context_root

    def _local_build(ctx, docker_dir, exists, fingerprint, *, preserve_build_stamp=False):
        calls.append((docker_dir, exists, fingerprint, preserve_build_stamp))
        payload = lifecycle.PayloadProvenance(lifecycle.PROVENANCE_SCHEMA, "0.2.6", fingerprint)
        FakeBuilder(docker).build(
            lifecycle._base_node(payload),
            force=False,
            source=lifecycle.ArtifactSource.LOCAL_BUILD,
        )

    monkeypatch.setattr(harness_lifecycle, "extracted_development_context", _context)
    monkeypatch.setattr(init_docker_image, "_docker_local_build", _local_build)
    monkeypatch.setattr(
        harness_lifecycle,
        "embedded_official_release",
        lambda: False,
        raising=False,
    )
    monkeypatch.setattr(
        init_docker_image,
        "_try_pull_image",
        lambda *_args, **_kwargs: pytest.fail("development wheel attempted a registry pull"),
    )

    result = harness_lifecycle.reconcile(lifecycle.HostImageScope(), lifecycle.Intent.ENSURE)

    assert pulls == []
    assert calls == [(context_docker, False, "payload-new", True)]
    assert result.status is lifecycle.Status.CHANGED


def test_distribution_replaces_current_local_base_before_pulling_flavor(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = _project(tmp_path, "booley-sandbox-riscv")
    docker = FakeDocker({})
    local_builder = _wire(monkeypatch, docker)
    payload = lifecycle.PayloadProvenance(lifecycle.PROVENANCE_SCHEMA, "0.2.6", "payload-new")
    local_builder.build(
        lifecycle._base_node(payload),
        force=False,
        source=lifecycle.ArtifactSource.LOCAL_BUILD,
    )
    registry_builder = RegistryChainBuilder(docker)

    result = lifecycle.reconcile(
        lifecycle.ProjectImageScope(root),
        lifecycle.Intent.ENSURE,
        docker=docker,
        builder=registry_builder,
        artifact_policy=lifecycle.ArtifactPolicy.VERIFIED_RELEASE_ONLY,
    )

    assert registry_builder.built == [lifecycle.BASE_IMAGE, "booley-sandbox-riscv"]
    assert result.status is lifecycle.Status.CHANGED
    assert docker.label(lifecycle.BASE_IMAGE, lifecycle.LABEL_BUILD_ORIGIN) == "registry"


def test_source_policy_replaces_current_registry_base(monkeypatch: pytest.MonkeyPatch) -> None:
    docker = FakeDocker({lifecycle.BASE_IMAGE: _current_registry_base()})
    builder = _wire(monkeypatch, docker)

    lifecycle.reconcile(lifecycle.HostImageScope(), lifecycle.Intent.ENSURE)

    assert builder.built == [lifecycle.BASE_IMAGE]
    assert docker.label(lifecycle.BASE_IMAGE, lifecycle.LABEL_BUILD_ORIGIN) == "local"


@pytest.mark.parametrize(
    ("origin", "attribution_kwargs", "expected_action"),
    [
        (
            "source",
            {"source_root": Path("/tmp/booley-source")},
            "would build locally",
        ),
        (
            "distribution",
            {"distribution_name": "booley-rtl"},
            "would pull verified release",
        ),
    ],
)
def test_check_reports_selected_artifact_source_without_mutation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    origin: str,
    attribution_kwargs: dict[str, Path | str],
    expected_action: str,
) -> None:
    import booley
    from booley.runtime.version_attribution import VersionAttribution, VersionOrigin

    docker_dir = tmp_path / "src" / "booley" / "data" / "docker"
    docker_dir.mkdir(parents=True)
    (docker_dir / "Dockerfile").write_text("FROM scratch\n", encoding="utf-8")
    monkeypatch.setattr(harness_lifecycle, "docker_data_dir", lambda: docker_dir)
    monkeypatch.setattr(lifecycle, "docker_data_dir", lambda: docker_dir)
    monkeypatch.setattr(
        booley,
        "version_attribution",
        VersionAttribution(
            version="0.2.6",
            origin=VersionOrigin(origin),
            **attribution_kwargs,
        ),
    )
    monkeypatch.setattr(
        harness_lifecycle,
        "embedded_official_release",
        lambda: origin == "distribution",
        raising=False,
    )
    docker = FakeDocker({})
    _wire(monkeypatch, docker)
    monkeypatch.setattr(harness_lifecycle, "_docker_adapter", lambda: docker)

    result = harness_lifecycle.reconcile(lifecycle.HostImageScope(), lifecycle.Intent.CHECK)

    assert result.status is lifecycle.Status.STALE
    assert expected_action in result.diagnostics[0].message
    assert not docker.mutations


def test_wrong_verified_pull_falls_back_to_exact_local_build(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    release = "ghcr.io/boldaxolotl/booley-sandbox:0.2.6"

    class PullThenLocalBuilder(FakeBuilder):
        def __init__(self, docker: FakeDocker) -> None:
            super().__init__(docker)
            self.sources: list[lifecycle.ArtifactSource] = []

        def build(
            self,
            node,
            *,
            force: bool,
            source: lifecycle.ArtifactSource,
        ) -> str | None:
            self.sources.append(source)
            if source is lifecycle.ArtifactSource.LOCAL_BUILD:
                super().build(node, force=force, source=source)
                return None
            return _stage_registry_candidate(
                self.docker,
                node,
                release,
                label_overrides={lifecycle.LABEL_PAYLOAD_FINGERPRINT: "wrong-payload"},
            )

    docker = FakeDocker({})
    _wire(monkeypatch, docker)
    builder = PullThenLocalBuilder(docker)

    result = lifecycle.reconcile(
        lifecycle.HostImageScope(),
        lifecycle.Intent.ENSURE,
        docker=docker,
        builder=builder,
        artifact_policy=lifecycle.ArtifactPolicy.VERIFIED_RELEASE_THEN_LOCAL,
    )

    assert builder.sources == [
        lifecycle.ArtifactSource.VERIFIED_RELEASE_PULL,
        lifecycle.ArtifactSource.LOCAL_BUILD,
    ]
    assert result.status is lifecycle.Status.CHANGED
    assert docker.label(lifecycle.BASE_IMAGE, lifecycle.LABEL_BUILD_ORIGIN) == "local"
    assert docker.image_id(release) is None
    assert ("tag", "sha256:" + "a" * 64, lifecycle.BASE_IMAGE) not in docker.mutations


def test_verified_pull_is_validated_before_canonical_adoption(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    release = "ghcr.io/boldaxolotl/booley-sandbox:0.2.6"

    class StagedPullBuilder:
        def __init__(self, docker: FakeDocker) -> None:
            self.docker = docker

        def build(
            self,
            node,
            *,
            force: bool,
            source: lifecycle.ArtifactSource,
        ) -> str:
            del force
            assert source is lifecycle.ArtifactSource.VERIFIED_RELEASE_PULL
            _stage_registry_candidate(self.docker, node, release)
            assert self.docker.image_id(node.reference) is None
            return release

    docker = FakeDocker({})
    _wire(monkeypatch, docker)

    result = lifecycle.reconcile(
        lifecycle.HostImageScope(),
        lifecycle.Intent.ENSURE,
        docker=docker,
        builder=StagedPullBuilder(docker),
        artifact_policy=lifecycle.ArtifactPolicy.VERIFIED_RELEASE_ONLY,
    )

    assert result.selected_id == "sha256:" + "a" * 64
    assert ("tag", "sha256:" + "a" * 64, lifecycle.BASE_IMAGE) in docker.mutations


def test_pull_and_local_fallback_failure_restore_prior_tag(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    release = "ghcr.io/boldaxolotl/booley-sandbox:0.2.6"

    class FailedRecoveryBuilder:
        def __init__(self, docker: FakeDocker) -> None:
            self.docker = docker

        def build(
            self,
            node,
            *,
            force: bool,
            source: lifecycle.ArtifactSource,
        ) -> str | None:
            del force
            if source is lifecycle.ArtifactSource.LOCAL_BUILD:
                raise lifecycle.ImageLifecycleError("local compiler failed")
            return _stage_registry_candidate(
                self.docker,
                node,
                release,
                label_overrides={lifecycle.LABEL_PAYLOAD_FINGERPRINT: "wrong-payload"},
            )

    prior_id = "sha256:" + "d" * 64
    docker = FakeDocker({lifecycle.BASE_IMAGE: (prior_id, {lifecycle.LABEL_SCHEMA: "stale"})})
    _wire(monkeypatch, docker)

    with pytest.raises(lifecycle.ImageLifecycleError) as error:
        lifecycle.reconcile(
            lifecycle.HostImageScope(),
            lifecycle.Intent.ENSURE,
            docker=docker,
            builder=FailedRecoveryBuilder(docker),
            artifact_policy=lifecycle.ArtifactPolicy.VERIFIED_RELEASE_THEN_LOCAL,
        )

    assert "verified-release-pull" in str(error.value)
    assert "local-build" in str(error.value)
    assert docker.image_id(lifecycle.BASE_IMAGE) == prior_id
    assert ("tag", "sha256:" + "a" * 64, lifecycle.BASE_IMAGE) not in docker.mutations
    assert not any(reference.startswith("booley-lifecycle-backup-") for reference in docker.images)


def _wire_source_chain(
    root: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> tuple[FakeDocker, list[tuple[str, str]], list[str]]:
    docker_dir = _set_source_installation(tmp_path, monkeypatch)
    docker, pulls = _wire_source_docker(docker_dir, monkeypatch)
    builds = _wire_source_builds(root, docker, monkeypatch)
    return docker, pulls, builds


def _set_source_installation(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    import booley
    from booley.harness.setup import docker_image as init_docker_image
    from booley.runtime.version_attribution import VersionAttribution, VersionOrigin

    source_root, docker_dir = _source_recipe_tree(tmp_path)
    monkeypatch.setattr(harness_lifecycle, "docker_data_dir", lambda: docker_dir)
    monkeypatch.setattr(lifecycle, "docker_data_dir", lambda: docker_dir)
    monkeypatch.setattr(init_docker_image, "docker_data_dir", lambda: docker_dir)
    monkeypatch.setattr(
        booley,
        "version_attribution",
        VersionAttribution(
            version="0.2.6",
            origin=VersionOrigin.SOURCE,
            source_root=source_root,
        ),
    )
    return docker_dir


def _wire_source_docker(
    docker_dir: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> tuple[FakeDocker, list[tuple[str, str]]]:
    from booley.harness.setup import docker_image as init_docker_image

    docker = FakeDocker({})
    _wire(monkeypatch, docker)
    monkeypatch.setattr(harness_lifecycle, "_docker_adapter", lambda: docker)
    monkeypatch.setattr(init_docker_image.shutil, "which", lambda _name: "/usr/bin/docker")
    monkeypatch.setattr(
        init_docker_image,
        "_docker_image_exists",
        lambda image=lifecycle.BASE_IMAGE: docker.image_id(image) is not None,
    )
    monkeypatch.setattr(
        init_docker_image,
        "_image_build_fingerprint",
        lambda _root: "payload-new",
    )
    monkeypatch.setattr(
        init_docker_image,
        "_expected_version",
        lambda _root: "0.2.6",
    )
    pulls: list[tuple[str, str]] = []
    monkeypatch.setattr(
        init_docker_image,
        "_try_pull_image",
        lambda version, image=lifecycle.BASE_IMAGE: pulls.append((version, image)) or False,
    )
    return docker, pulls


def _wire_source_builds(
    root: Path,
    docker: FakeDocker,
    monkeypatch: pytest.MonkeyPatch,
) -> list[str]:
    from booley.harness.setup import docker_image as init_docker_image

    builds: list[str] = []

    def stamp_local(node: lifecycle.ImageNode, image_id: str) -> None:
        labels = dict(node.expected_labels)
        labels[lifecycle.LABEL_BUILD_ORIGIN] = "local"
        labels[lifecycle.LABEL_PARENT_ARTIFACT_KIND] = lifecycle.PARENT_ARTIFACT_LOCAL_IMAGE_ID
        if node.reference == lifecycle.BASE_IMAGE:
            labels[lifecycle.LABEL_PARENT_ARTIFACT] = (
                docker.image_id(lifecycle.STABLE_RUNTIME_BASE_IMAGE) or ""
            )
        docker.images[node.reference] = (image_id, labels)

    def build_base(context, _docker_dir, _exists, _fingerprint) -> None:
        payload = lifecycle.PayloadProvenance(
            lifecycle.PROVENANCE_SCHEMA,
            "0.2.6",
            "payload-new",
        )
        stamp_local(lifecycle._base_node(payload), "sha256:" + "b" * 64)
        context.record("docker_image", "ok", "built")
        builds.append(lifecycle.BASE_IMAGE)

    def build_flavor(
        context, image: str, _recipe: Path, _exists: bool, _fingerprint: str | None
    ) -> bool:
        stamp_local(lifecycle._nodes(root, image, docker)[-1], "sha256:" + "c" * 64)
        context.record("project_image", "ok", f"flavor {image} built")
        builds.append(image)
        return True

    monkeypatch.setattr(init_docker_image, "_docker_local_build", build_base)
    monkeypatch.setattr(init_docker_image, "_flavor_build", build_flavor)
    return builds


def _install_current_legacy_base(docker: FakeDocker) -> str:
    payload = lifecycle.PayloadProvenance(
        lifecycle.PROVENANCE_SCHEMA,
        "0.2.6",
        "payload-new",
    )
    node = lifecycle._base_node(payload)
    parent_id = docker.image_id(lifecycle.STABLE_RUNTIME_BASE_IMAGE) or ""
    labels = dict(node.expected_labels)
    labels.update(
        {
            lifecycle.LABEL_BUILD_ORIGIN: "local",
            lifecycle.LABEL_PARENT_ARTIFACT_KIND: lifecycle.PARENT_ARTIFACT_LOCAL_IMAGE_ID,
            lifecycle.LABEL_PARENT_ARTIFACT: parent_id,
        }
    )
    image_id = "sha256:" + "b" * 64
    docker.images[lifecycle.BASE_IMAGE] = (image_id, labels)
    return image_id


def test_legacy_reconcile_builds_riscv_with_managed_parent_context(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from booley.harness.setup import docker_image as init_docker_image

    root = _project(tmp_path, "booley-sandbox-riscv")
    docker_dir = _set_source_installation(tmp_path, monkeypatch)
    docker, pulls = _wire_source_docker(docker_dir, monkeypatch)
    parent_id = _install_current_legacy_base(docker)
    captured = []
    monkeypatch.setattr(
        init_docker_image,
        "_docker_image_id",
        docker.image_id,
    )
    monkeypatch.setattr(init_docker_image, "_report_build_cache", lambda: None)

    def build_flavor(_context, spec) -> int:
        captured.append(spec)
        node = lifecycle._nodes(root, spec.image, docker)[-1]
        labels = dict(node.expected_labels)
        labels.update(
            {
                lifecycle.LABEL_BUILD_ORIGIN: "local",
                lifecycle.LABEL_PARENT_ARTIFACT_KIND: (lifecycle.PARENT_ARTIFACT_LOCAL_IMAGE_ID),
                lifecycle.LABEL_PARENT_ARTIFACT: parent_id,
            }
        )
        docker.images[spec.image] = ("sha256:" + "c" * 64, labels)
        return 0

    monkeypatch.setattr(init_docker_image, "_docker_build_image", build_flavor)

    result = harness_lifecycle.reconcile(
        lifecycle.ProjectImageScope(root), lifecycle.Intent.ENSURE
    )

    assert pulls == []
    assert result.status is lifecycle.Status.CHANGED
    assert len(captured) == 1
    assert captured[0].build_contexts == (
        ("booley-standard-substrate", "docker-image://booley-sandbox"),
    )
    assert captured[0].parent_artifact == parent_id


def test_legacy_reconcile_restores_riscv_tag_after_real_flavor_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from booley.harness.setup import docker_image as init_docker_image

    root = _project(tmp_path, "booley-sandbox-riscv")
    docker_dir = _set_source_installation(tmp_path, monkeypatch)
    docker, _pulls = _wire_source_docker(docker_dir, monkeypatch)
    parent_id = _install_current_legacy_base(docker)
    old_flavor_id = "sha256:" + "d" * 64
    docker.images["booley-sandbox-riscv"] = (
        old_flavor_id,
        _labels(payload="payload-old", recipe="old-flavor", parent=parent_id),
    )
    captured = []
    monkeypatch.setattr(
        init_docker_image,
        "_docker_image_id",
        docker.image_id,
    )
    monkeypatch.setattr(
        init_docker_image,
        "_docker_build_image",
        lambda _context, spec: captured.append(spec) or 1,
    )

    with pytest.raises(lifecycle.ImageLifecycleError, match="build failed"):
        harness_lifecycle.reconcile(lifecycle.ProjectImageScope(root), lifecycle.Intent.ENSURE)

    assert len(captured) == 1
    assert captured[0].parent_artifact == parent_id
    assert docker.image_id("booley-sandbox-riscv") == old_flavor_id


def test_source_host_then_project_builds_one_exact_local_chain(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = _project(tmp_path, "booley-sandbox-riscv")
    docker, pulls, builds = _wire_source_chain(root, tmp_path, monkeypatch)

    base = harness_lifecycle.reconcile(lifecycle.HostImageScope(), lifecycle.Intent.ENSURE)
    result = harness_lifecycle.reconcile(
        lifecycle.ProjectImageScope(root, base), lifecycle.Intent.ENSURE
    )

    assert pulls == []
    assert builds == [lifecycle.BASE_IMAGE, "booley-sandbox-riscv"]
    assert result.selected_id == docker.image_id("booley-sandbox-riscv")
    assert docker.label(
        "booley-sandbox-riscv", lifecycle.LABEL_PARENT_ARTIFACT
    ) == docker.image_id(lifecycle.BASE_IMAGE)

    builds.clear()
    refreshed_base = harness_lifecycle.reconcile(
        lifecycle.HostImageScope(), lifecycle.Intent.REFRESH
    )
    harness_lifecycle.reconcile(
        lifecycle.ProjectImageScope(root, refreshed_base), lifecycle.Intent.REFRESH
    )

    assert pulls == []
    assert builds == [lifecycle.BASE_IMAGE, "booley-sandbox-riscv"]


def test_published_flavor_uses_registry_parent_when_local_ids_differ(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = _project(tmp_path, "booley-sandbox-riscv")
    payload = lifecycle.PayloadProvenance(lifecycle.PROVENANCE_SCHEMA, "0.2.6", "payload-new")
    publisher_parent_id = "sha256:" + "1" * 64
    consumer_base_id = "sha256:" + "2" * 64
    consumer_flavor_id = "sha256:" + "3" * 64
    base_digest = "ghcr.io/boldaxolotl/booley-sandbox@sha256:" + "d" * 64
    runtime_digest = "ghcr.io/boldaxolotl/booley-sandbox-base@sha256:" + "e" * 64
    base_labels = _registry_labels(lifecycle._base_node(payload), runtime_digest)
    flavor = lifecycle._flavor_node("booley-sandbox-riscv", lifecycle._base_node(payload), payload)
    legacy_flavor_labels = _registry_labels(flavor, publisher_parent_id, legacy=True)
    flavor_labels = _registry_labels(flavor, base_digest)
    docker = FakeDocker(
        {
            lifecycle.BASE_IMAGE: (consumer_base_id, base_labels),
            "booley-sandbox-riscv": (consumer_flavor_id, legacy_flavor_labels),
        }
    )
    docker.registry_identities[lifecycle.BASE_IMAGE] = (base_digest,)
    builder = _wire(monkeypatch, docker)

    legacy_result = lifecycle.reconcile(
        lifecycle.ProjectImageScope(root),
        lifecycle.Intent.CHECK,
        artifact_policy=lifecycle.ArtifactPolicy.VERIFIED_RELEASE_ONLY,
    )

    assert publisher_parent_id != consumer_base_id
    assert legacy_result.status is lifecycle.Status.STALE

    docker.images["booley-sandbox-riscv"] = (consumer_flavor_id, flavor_labels)
    result = lifecycle.reconcile(
        lifecycle.ProjectImageScope(root),
        lifecycle.Intent.ENSURE,
        artifact_policy=lifecycle.ArtifactPolicy.VERIFIED_RELEASE_ONLY,
    )

    assert result.status is lifecycle.Status.CURRENT
    assert result.selected_id == consumer_flavor_id
    assert docker.image_id("booley-sandbox-riscv") == result.selected_id
    assert not builder.built


def test_missing_published_pair_is_acquired_and_keeps_flavor_short_tag(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    class RegistryPullBuilder:
        def __init__(self, docker: FakeDocker) -> None:
            self.docker = docker
            self.built: list[str] = []

        def build(self, node, *, force: bool, source: lifecycle.ArtifactSource) -> None:
            del force, source
            self.built.append(node.reference)
            labels = dict(node.expected_labels)
            labels[lifecycle.LABEL_BUILD_ORIGIN] = "registry"
            labels[lifecycle.LABEL_PARENT_ARTIFACT_KIND] = (
                lifecycle.PARENT_ARTIFACT_REGISTRY_DIGEST
            )
            if node.reference == lifecycle.BASE_IMAGE:
                labels[lifecycle.LABEL_PARENT_ARTIFACT] = runtime_digest
                image_id = consumer_base_id
                self.docker.registry_identities[node.reference] = (base_digest,)
            else:
                labels[lifecycle.LABEL_PARENT_ARTIFACT] = base_digest
                image_id = consumer_flavor_id
            self.docker.images[node.reference] = (image_id, labels)

    root = _project(tmp_path, "booley-sandbox-riscv")
    consumer_base_id = "sha256:" + "2" * 64
    consumer_flavor_id = "sha256:" + "3" * 64
    base_digest = "ghcr.io/boldaxolotl/booley-sandbox@sha256:" + "d" * 64
    runtime_digest = "ghcr.io/boldaxolotl/booley-sandbox-base@sha256:" + "e" * 64
    docker = FakeDocker({})
    _wire(monkeypatch, docker)
    builder = RegistryPullBuilder(docker)
    monkeypatch.setattr(lifecycle, "_build_adapter", lambda *_args, **_kwargs: builder)

    result = lifecycle.reconcile(
        lifecycle.ProjectImageScope(root),
        lifecycle.Intent.ENSURE,
        artifact_policy=lifecycle.ArtifactPolicy.VERIFIED_RELEASE_ONLY,
    )

    assert builder.built == [lifecycle.BASE_IMAGE, "booley-sandbox-riscv"]
    assert result.status is lifecycle.Status.CHANGED
    assert result.selected_id == consumer_flavor_id
    assert docker.image_id(lifecycle.BASE_IMAGE) == consumer_base_id
    assert docker.image_id("booley-sandbox-riscv") == consumer_flavor_id


def test_published_flavor_rejects_different_registry_parent(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = _project(tmp_path, "booley-sandbox-riscv")
    payload = lifecycle.PayloadProvenance(lifecycle.PROVENANCE_SCHEMA, "0.2.6", "payload-new")
    recorded_digest = "ghcr.io/boldaxolotl/booley-sandbox@sha256:" + "d" * 64
    current_digest = "ghcr.io/boldaxolotl/booley-sandbox@sha256:" + "c" * 64
    runtime_digest = "ghcr.io/boldaxolotl/booley-sandbox-base@sha256:" + "e" * 64
    base_labels = _labels(
        payload="payload-new",
        recipe=lifecycle._base_node(payload).build.recipe_fingerprint,
        parent=runtime_digest,
    )
    base_labels.update(
        {
            lifecycle.LABEL_BUILD_ORIGIN: "registry",
            lifecycle.LABEL_PARENT_ARTIFACT_KIND: (lifecycle.PARENT_ARTIFACT_REGISTRY_DIGEST),
        }
    )
    flavor = lifecycle._flavor_node("booley-sandbox-riscv", lifecycle._base_node(payload), payload)
    flavor_labels = _labels(
        payload="payload-new",
        recipe=flavor.build.recipe_fingerprint,
        parent=recorded_digest,
    )
    flavor_labels.update(
        {
            lifecycle.LABEL_BUILD_ORIGIN: "registry",
            lifecycle.LABEL_PARENT_ARTIFACT_KIND: (lifecycle.PARENT_ARTIFACT_REGISTRY_DIGEST),
        }
    )
    docker = FakeDocker(
        {
            lifecycle.BASE_IMAGE: ("sha256:" + "a" * 64, base_labels),
            "booley-sandbox-riscv": ("sha256:" + "b" * 64, flavor_labels),
        }
    )
    docker.registry_identities[lifecycle.BASE_IMAGE] = (current_digest,)
    _wire(monkeypatch, docker)
    builder = RegistryChainBuilder(docker, base_digest=current_digest)

    lifecycle.reconcile(
        lifecycle.ProjectImageScope(root),
        lifecycle.Intent.ENSURE,
        docker=docker,
        builder=builder,
        artifact_policy=lifecycle.ArtifactPolicy.VERIFIED_RELEASE_ONLY,
    )

    assert builder.built == ["booley-sandbox-riscv"]


def test_keep_recipe_is_not_rewritten_when_parent_forces_rebuild(tmp_path: Path, monkeypatch):
    root = _project(tmp_path)
    dockerfile = root / ".booley_project" / "docker" / "Dockerfile"
    dockerfile.parent.mkdir()
    original = "# booley:keep\nFROM booley-sandbox-riscv\nRUN echo mine\n"
    dockerfile.write_text(original, encoding="utf-8")
    generated = "project-booley-sandbox"
    docker = FakeDocker(
        {
            "booley-sandbox": (
                "sha256:" + "1" * 64,
                _labels(payload="payload-old", recipe="old"),
            ),
            "booley-sandbox-riscv": (
                "sha256:" + "2" * 64,
                _labels(payload="payload-old", recipe="old", parent="sha256:" + "1" * 64),
            ),
            generated: (
                "sha256:" + "3" * 64,
                _labels(payload="payload-old", recipe="old", parent="sha256:" + "2" * 64),
            ),
        }
    )
    builder = _wire(monkeypatch, docker)

    result = lifecycle.reconcile(lifecycle.ProjectImageScope(root), lifecycle.Intent.ENSURE)

    assert builder.built == ["booley-sandbox", "booley-sandbox-riscv", generated]
    assert result.selected_reference == generated
    assert dockerfile.read_text(encoding="utf-8") == original


def test_explicit_external_image_receives_zero_mutations(tmp_path: Path, monkeypatch):
    root = _project(tmp_path, "registry.example/team/custom:latest")
    docker = FakeDocker({})
    builder = _wire(monkeypatch, docker)

    result = lifecycle.reconcile(lifecycle.ProjectImageScope(root), lifecycle.Intent.REFRESH)

    assert result.status is lifecycle.Status.EXTERNAL
    assert not builder.built
    assert not docker.mutations


def test_ensure_generates_project_recipe_for_configured_requirements(tmp_path: Path, monkeypatch):
    root = _project(tmp_path)
    requirement = root / "requirements.txt"
    requirement.write_text("cocotb==2.0.1\n", encoding="utf-8")
    config = root / ".booley_project" / "booley.toml"
    config.write_text(
        '[sandbox]\npip_requirements = ["requirements.txt"]\n',
        encoding="utf-8",
    )
    docker = FakeDocker({})
    builder = _wire(monkeypatch, docker)

    result = lifecycle.reconcile(lifecycle.ProjectImageScope(root), lifecycle.Intent.ENSURE)

    generated = "project-booley-sandbox"
    assert result.selected_reference == generated
    assert builder.built == ["booley-sandbox", generated]
    docker_dir = root / ".booley_project" / "docker"
    assert (docker_dir / "Dockerfile").is_file()
    assert "cocotb==2.0.1" in (docker_dir / "requirements.txt").read_text(encoding="utf-8")


@pytest.mark.parametrize(
    "body, message",
    [
        ("[sandbox\n", "could not parse"),
        ("sandbox = 'wrong shape'\n", "sandbox.*mapping"),
        ("[sandbox]\nimage = 42\n", "sandbox.image.*string"),
        ("[sandbox]\npip_requirements = [42]\n", "pip_requirements.*strings"),
    ],
)
def test_invalid_sandbox_configuration_fails_loudly(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    body: str,
    message: str,
) -> None:
    root = _project(tmp_path)
    (root / ".booley_project" / "booley.toml").write_text(body, encoding="utf-8")
    _wire(monkeypatch, FakeDocker({}))

    with pytest.raises(lifecycle.ImageLifecycleError, match=message):
        lifecycle.reconcile(lifecycle.ProjectImageScope(root), lifecycle.Intent.CHECK)


def test_missing_configured_requirement_fails_loudly(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = _project(tmp_path)
    (root / ".booley_project" / "booley.toml").write_text(
        '[sandbox]\npip_requirements = ["missing.txt"]\n',
        encoding="utf-8",
    )
    _wire(monkeypatch, FakeDocker({}))

    with pytest.raises(lifecycle.ImageLifecycleError, match=r"missing\.txt"):
        lifecycle.reconcile(lifecycle.ProjectImageScope(root), lifecycle.Intent.CHECK)


def test_check_uses_desired_requirements_without_rewriting_recipe(tmp_path: Path, monkeypatch):
    root = _project(tmp_path)
    requirement = root / "requirements.txt"
    requirement.write_text("cocotb==2.0.1\n", encoding="utf-8")
    (root / ".booley_project" / "booley.toml").write_text(
        '[sandbox]\npip_requirements = ["requirements.txt"]\n',
        encoding="utf-8",
    )
    docker = FakeDocker({})
    _wire(monkeypatch, docker)
    lifecycle.reconcile(lifecycle.ProjectImageScope(root), lifecycle.Intent.ENSURE)
    generated_recipe = root / ".booley_project" / "docker" / "requirements.txt"
    before = generated_recipe.read_bytes()

    requirement.write_text("cocotb==2.0.2\n", encoding="utf-8")
    result = lifecycle.reconcile(lifecycle.ProjectImageScope(root), lifecycle.Intent.CHECK)

    assert result.status is lifecycle.Status.STALE
    assert generated_recipe.read_bytes() == before


def test_checkout_project_dir_override_wins_over_literal_directory(tmp_path: Path, monkeypatch):
    root = tmp_path / "project"
    local = root / ".booley_project"
    custom = root / "control"
    local.mkdir(parents=True)
    custom.mkdir()
    (root / "booley.toml").write_text('[project]\ndir = "control"\n', encoding="utf-8")
    (custom / "booley.toml").write_text("[sandbox]\n", encoding="utf-8")
    docker = FakeDocker({})
    _wire(monkeypatch, docker)

    lifecycle.reconcile(lifecycle.ProjectImageScope(root), lifecycle.Intent.CHECK)

    assert lifecycle._direct_project_dir(root) == custom


def test_ambiguous_project_dockerfile_fails_without_building(tmp_path: Path, monkeypatch):
    root = _project(tmp_path)
    docker_dir = root / ".booley_project" / "docker"
    docker_dir.mkdir()
    (docker_dir / "Dockerfile").write_text(
        "FROM booley-sandbox AS build\nFROM build AS final\n",
        encoding="utf-8",
    )
    docker = FakeDocker({})
    builder = _wire(monkeypatch, docker)

    with pytest.raises(lifecycle.ImageLifecycleError, match="ambiguous ancestry"):
        lifecycle.reconcile(lifecycle.ProjectImageScope(root), lifecycle.Intent.ENSURE)

    assert not builder.built


def test_failed_refresh_restores_selected_tag(tmp_path: Path, monkeypatch):
    root = _project(tmp_path)
    old_id = "sha256:" + "f" * 64
    docker = FakeDocker(
        {
            "booley-sandbox": (
                old_id,
                _labels(payload="payload-old", recipe="old"),
            )
        }
    )
    _wire(monkeypatch, docker)
    monkeypatch.setattr(
        lifecycle,
        "_build_adapter",
        lambda *_args, **_kwargs: FailingBuilder(docker),
    )

    with pytest.raises(lifecycle.ImageLifecycleError, match="build failed"):
        lifecycle.reconcile(lifecycle.ProjectImageScope(root), lifecycle.Intent.REFRESH)

    assert docker.image_id("booley-sandbox") == old_id


def test_failed_derived_refresh_restores_every_managed_tag(tmp_path: Path, monkeypatch):
    root = _project(tmp_path, "booley-sandbox-riscv")
    old_base = "sha256:" + "b" * 64
    old_flavor = "sha256:" + "c" * 64
    docker = FakeDocker(
        {
            "booley-sandbox": (
                old_base,
                _labels(payload="payload-old", recipe="old-base"),
            ),
            "booley-sandbox-riscv": (
                old_flavor,
                _labels(payload="payload-old", recipe="old-flavor", parent=old_base),
            ),
        }
    )
    _wire(monkeypatch, docker)
    monkeypatch.setattr(
        lifecycle,
        "_build_adapter",
        lambda *_args, **_kwargs: FailOnSecondBuilder(docker),
    )

    with pytest.raises(lifecycle.ImageLifecycleError, match="derived build failed"):
        lifecycle.reconcile(lifecycle.ProjectImageScope(root), lifecycle.Intent.REFRESH)

    assert docker.image_id("booley-sandbox") == old_base
    assert docker.image_id("booley-sandbox-riscv") == old_flavor


def test_failed_derived_build_removes_new_parent_tag(tmp_path: Path, monkeypatch):
    root = _project(tmp_path, "booley-sandbox-riscv")
    docker = FakeDocker({})
    _wire(monkeypatch, docker)
    monkeypatch.setattr(
        lifecycle,
        "_build_adapter",
        lambda *_args, **_kwargs: FailOnSecondBuilder(docker),
    )

    with pytest.raises(lifecycle.ImageLifecycleError, match="derived build failed"):
        lifecycle.reconcile(lifecycle.ProjectImageScope(root), lifecycle.Intent.REFRESH)

    assert docker.image_id("booley-sandbox") is None
    assert docker.image_id("booley-sandbox-riscv") is None


def test_legacy_base_payload_is_accepted_until_next_rebuild(tmp_path: Path, monkeypatch):
    root = _project(tmp_path)
    docker = FakeDocker(
        {
            "booley-sandbox": (
                "sha256:" + "a" * 64,
                {
                    lifecycle.LEGACY_FINGERPRINT_LABEL: "payload-new",
                    lifecycle.LABEL_VERSION: "0.2.6",
                },
            )
        }
    )
    builder = _wire(monkeypatch, docker)

    result = lifecycle.reconcile(lifecycle.ProjectImageScope(root), lifecycle.Intent.ENSURE)

    assert result.status is lifecycle.Status.CURRENT
    assert result.diagnostics[0].code == "legacy-provenance"
    assert not builder.built


def test_legacy_derived_image_is_rebuilt_for_exact_ancestry(tmp_path: Path, monkeypatch):
    root = _project(tmp_path, "booley-sandbox-riscv")
    docker = FakeDocker(
        {
            "booley-sandbox": (
                "sha256:" + "a" * 64,
                {
                    lifecycle.LEGACY_FINGERPRINT_LABEL: "payload-new",
                    lifecycle.LABEL_VERSION: "0.2.6",
                },
            ),
            "booley-sandbox-riscv": (
                "sha256:" + "b" * 64,
                {
                    lifecycle.LEGACY_FINGERPRINT_LABEL: "payload-new",
                    lifecycle.LABEL_VERSION: "0.2.6",
                },
            ),
        }
    )
    builder = _wire(monkeypatch, docker)

    lifecycle.reconcile(lifecycle.ProjectImageScope(root), lifecycle.Intent.ENSURE)

    assert builder.built == ["booley-sandbox-riscv"]


def test_legacy_adapter_builds_user_owned_project_recipe_without_rewriting(
    tmp_path: Path, monkeypatch
):
    root = _project(tmp_path)
    docker_dir = root / ".booley_project" / "docker"
    docker_dir.mkdir()
    dockerfile = docker_dir / "Dockerfile"
    original = "# booley:keep\nFROM booley-sandbox\nRUN echo mine\n"
    dockerfile.write_text(original, encoding="utf-8")
    node = lifecycle.ImageNode(
        "project-booley-sandbox",
        dockerfile,
        lifecycle.PayloadProvenance("1", "0.2.6", "payload"),
        lifecycle.BuildProvenance("recipe", "sha256:parent"),
        "booley-sandbox",
    )
    calls: list[tuple[str, Path, bool]] = []
    monkeypatch.setattr(
        lifecycle.project_image,
        "build_project_image",
        lambda image, directory, *, verbose=False: (
            calls.append((image, directory, verbose)) or True
        ),
    )

    harness_lifecycle._LegacyBuildAdapter(root, verbose=True).build(
        node,
        force=True,
        source=lifecycle.ArtifactSource.LOCAL_BUILD,
    )

    assert calls == [(node.reference, docker_dir, True)]
    assert dockerfile.read_text(encoding="utf-8") == original


@pytest.mark.parametrize("reference", [lifecycle.BASE_IMAGE, "booley-sandbox-riscv"])
def test_packaged_refresh_uses_pull_capable_builder(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    reference: str,
) -> None:
    from booley.harness.setup import docker_image as init_docker_image

    root = _project(tmp_path)
    docker_dir = tmp_path / "installed" / "src" / "booley" / "data" / "docker"
    docker_dir.mkdir(parents=True)
    recipe = docker_dir / (
        "Dockerfile" if reference == lifecycle.BASE_IMAGE else "Dockerfile.riscv"
    )
    recipe.write_text("FROM scratch\n", encoding="utf-8")
    monkeypatch.setattr(harness_lifecycle, "docker_data_dir", lambda: docker_dir)
    pulls: list[tuple[str, str]] = []
    monkeypatch.setattr(
        init_docker_image,
        "_try_pull_image",
        lambda version, image=lifecycle.BASE_IMAGE, **_kwargs: (
            pulls.append((version, image)) or True
        ),
    )
    monkeypatch.setattr(
        init_docker_image,
        "_step_docker_image",
        lambda *_args, **_kwargs: pytest.fail("packaged refresh attempted a source build"),
    )
    monkeypatch.setattr(
        init_docker_image,
        "ensure_flavor_image",
        lambda *_args, **_kwargs: pytest.fail("packaged refresh attempted a flavor build"),
    )
    node = lifecycle.ImageNode(
        reference,
        recipe,
        lifecycle.PayloadProvenance("1", "0.2.6", "payload"),
        lifecycle.BuildProvenance("recipe", None),
    )

    harness_lifecycle._LegacyBuildAdapter(root, verbose=False).build(
        node,
        force=True,
        source=lifecycle.ArtifactSource.VERIFIED_RELEASE_PULL,
    )

    assert pulls == [("0.2.6", reference)]


def test_docker_inspect_daemon_failure_is_not_an_absent_image(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        lifecycle.subprocess,
        "run",
        lambda *_args, **_kwargs: subprocess.CompletedProcess(
            [], 125, stdout="", stderr="Cannot connect to the Docker daemon"
        ),
    )

    with pytest.raises(lifecycle.ImageLifecycleError, match="Docker daemon"):
        lifecycle._DockerCli().image_id("booley-sandbox")


def test_docker_label_daemon_failure_is_not_an_absent_label(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        lifecycle.subprocess,
        "run",
        lambda *_args, **_kwargs: subprocess.CompletedProcess(
            [], 125, stdout="", stderr="Cannot connect to the Docker daemon"
        ),
    )

    with pytest.raises(lifecycle.ImageLifecycleError, match="Docker daemon"):
        lifecycle._DockerCli().label("booley-sandbox", lifecycle.LABEL_SCHEMA)


def test_docker_repo_digests_are_normalized(monkeypatch: pytest.MonkeyPatch) -> None:
    digest = "ghcr.io/boldaxolotl/booley-sandbox@sha256:" + "A" * 64
    monkeypatch.setattr(
        lifecycle.subprocess,
        "run",
        lambda *_args, **_kwargs: subprocess.CompletedProcess(
            [], 0, stdout=f'["{digest}"]\n', stderr=""
        ),
    )

    assert lifecycle._DockerCli().repo_digests("booley-sandbox") == (digest.lower(),)


def test_docker_repo_digests_reject_malformed_inventory(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        lifecycle.subprocess,
        "run",
        lambda *_args, **_kwargs: subprocess.CompletedProcess(
            [], 0, stdout='["not-a-digest"]\n', stderr=""
        ),
    )

    with pytest.raises(lifecycle.ImageLifecycleError, match="malformed RepoDigests"):
        lifecycle._DockerCli().repo_digests("booley-sandbox")


def test_docker_repo_digest_inspection_failures_are_preserved(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    cli = lifecycle._DockerCli()
    monkeypatch.setattr(
        lifecycle.subprocess,
        "run",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(OSError("docker unavailable")),
    )

    with pytest.raises(lifecycle.ImageLifecycleError, match="inspect Docker RepoDigests"):
        cli.repo_digests("booley-sandbox")

    monkeypatch.setattr(
        lifecycle.subprocess,
        "run",
        lambda *_args, **_kwargs: subprocess.CompletedProcess(
            [], 1, stdout="", stderr="No such image: booley-sandbox"
        ),
    )
    assert cli.repo_digests("booley-sandbox") == ()

    monkeypatch.setattr(
        lifecycle.subprocess,
        "run",
        lambda *_args, **_kwargs: subprocess.CompletedProcess(
            [], 1, stdout="", stderr="Cannot connect to the Docker daemon"
        ),
    )
    with pytest.raises(lifecycle.ImageLifecycleError, match="Docker daemon"):
        cli.repo_digests("booley-sandbox")


@pytest.mark.parametrize("empty_inventory", ["null\n", "[]\n"])
def test_docker_repo_digests_accept_empty_inventory(
    monkeypatch: pytest.MonkeyPatch, empty_inventory: str
) -> None:
    monkeypatch.setattr(
        lifecycle.subprocess,
        "run",
        lambda *_args, **_kwargs: subprocess.CompletedProcess(
            [], 0, stdout=empty_inventory, stderr=""
        ),
    )

    assert lifecycle._DockerCli().repo_digests("booley-sandbox") == ()


@pytest.mark.parametrize("invalid_inventory", ["not-json\n", "{}\n", '["valid", 42]\n'])
def test_docker_repo_digests_reject_invalid_json_shape(
    monkeypatch: pytest.MonkeyPatch, invalid_inventory: str
) -> None:
    monkeypatch.setattr(
        lifecycle.subprocess,
        "run",
        lambda *_args, **_kwargs: subprocess.CompletedProcess(
            [], 0, stdout=invalid_inventory, stderr=""
        ),
    )

    with pytest.raises(lifecycle.ImageLifecycleError, match="invalid RepoDigests"):
        lifecycle._DockerCli().repo_digests("booley-sandbox")


def test_docker_tag_removal_failure_is_reported(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        lifecycle.subprocess,
        "run",
        lambda *_args, **_kwargs: subprocess.CompletedProcess(
            [], 1, stdout="", stderr="image is in use"
        ),
    )

    with pytest.raises(lifecycle.ImageLifecycleError, match="image is in use"):
        lifecycle._DockerCli().remove_tag("booley-lifecycle-backup:prior")


def test_docker_image_inventory_accepts_windows_newlines_and_full_ids(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    image_id = "sha256:" + "a" * 64
    output = '{"Repository":"booley-sandbox","Tag":"latest","ID":"' + image_id + '"}\r\n'
    monkeypatch.setattr(
        lifecycle.subprocess,
        "run",
        lambda *_args, **_kwargs: subprocess.CompletedProcess([], 0, stdout=output, stderr=""),
    )

    assert lifecycle._DockerCli().image_references() == (
        lifecycle.ImageReference("booley-sandbox:latest", image_id),
    )


@pytest.mark.parametrize(
    "output",
    [
        "not-json\n",
        "{}\n",
        '{"Repository":"booley-sandbox","Tag":"latest","ID":"short"}\n',
    ],
)
def test_docker_image_inventory_rejects_malformed_rows(
    monkeypatch: pytest.MonkeyPatch,
    output: str,
) -> None:
    monkeypatch.setattr(
        lifecycle.subprocess,
        "run",
        lambda *_args, **_kwargs: subprocess.CompletedProcess([], 0, stdout=output, stderr=""),
    )

    with pytest.raises(
        lifecycle.ImageLifecycleError,
        match=r"malformed image inventory row 1:",
    ):
        lifecycle._DockerCli().image_references()


def test_docker_image_inventory_rejects_conflicting_reference_ids(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    first_id = "sha256:" + "a" * 64
    second_id = "sha256:" + "b" * 64
    output = (
        f'{{"Repository":"booley-sandbox","Tag":"latest","ID":"{first_id}"}}\n'
        f'{{"Repository":"booley-sandbox","Tag":"latest","ID":"{second_id}"}}\n'
    )
    monkeypatch.setattr(
        lifecycle.subprocess,
        "run",
        lambda *_args, **_kwargs: subprocess.CompletedProcess([], 0, stdout=output, stderr=""),
    )

    with pytest.raises(lifecycle.ImageLifecycleError, match="conflicting IDs"):
        lifecycle._DockerCli().image_references()


@pytest.mark.parametrize(
    ("method_name", "message"),
    [
        ("image_references", "inventory Docker image references"),
        ("container_image_ids", "inventory Docker containers"),
    ],
)
def test_docker_inventory_reports_command_start_failure(
    monkeypatch: pytest.MonkeyPatch,
    method_name: str,
    message: str,
) -> None:
    monkeypatch.setattr(
        lifecycle.subprocess,
        "run",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(OSError("docker unavailable")),
    )

    with pytest.raises(lifecycle.ImageLifecycleError, match=message):
        getattr(lifecycle._DockerCli(), method_name)()


@pytest.mark.parametrize(
    ("method_name", "message"),
    [
        ("image_references", "image inventory denied"),
        ("container_image_ids", "container inventory denied"),
    ],
)
def test_docker_inventory_reports_command_failure(
    monkeypatch: pytest.MonkeyPatch,
    method_name: str,
    message: str,
) -> None:
    monkeypatch.setattr(
        lifecycle.subprocess,
        "run",
        lambda *_args, **_kwargs: subprocess.CompletedProcess([], 1, stdout="", stderr=message),
    )

    with pytest.raises(lifecycle.ImageLifecycleError, match=message):
        getattr(lifecycle._DockerCli(), method_name)()


def test_docker_container_inventory_includes_running_and_stopped_containers(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    first_id = "sha256:" + "a" * 64
    second_id = "sha256:" + "b" * 64
    results = iter(
        (
            subprocess.CompletedProcess([], 0, stdout="one\r\ntwo\r\n", stderr=""),
            subprocess.CompletedProcess([], 0, stdout=first_id + "\r\n", stderr=""),
            subprocess.CompletedProcess([], 0, stdout=second_id + "\r\n", stderr=""),
        )
    )
    monkeypatch.setattr(lifecycle.subprocess, "run", lambda *_args, **_kwargs: next(results))

    assert lifecycle._DockerCli().container_image_ids() == frozenset({first_id, second_id})


def test_docker_container_inventory_reports_inspect_start_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    results = iter(
        (
            subprocess.CompletedProcess([], 0, stdout="container-id\n", stderr=""),
            OSError("inspect unavailable"),
        )
    )

    def run(*_args: object, **_kwargs: object) -> subprocess.CompletedProcess[str]:
        result = next(results)
        if isinstance(result, OSError):
            raise result
        return result

    monkeypatch.setattr(lifecycle.subprocess, "run", run)

    with pytest.raises(lifecycle.ImageLifecycleError, match="inspect unavailable"):
        lifecycle._DockerCli().container_image_ids()


def test_docker_container_inventory_rejects_invalid_inspect_result(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    results = iter(
        (
            subprocess.CompletedProcess([], 0, stdout="container-id\n", stderr=""),
            subprocess.CompletedProcess([], 1, stdout="", stderr="container disappeared"),
        )
    )
    monkeypatch.setattr(lifecycle.subprocess, "run", lambda *_args, **_kwargs: next(results))

    with pytest.raises(lifecycle.ImageLifecycleError, match="container disappeared"):
        lifecycle._DockerCli().container_image_ids()


def test_docker_tag_removal_is_exact_non_forced_and_does_not_prune_parents(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    commands: list[list[str]] = []

    def run(args: list[str], **_kwargs: object) -> subprocess.CompletedProcess[str]:
        commands.append(args)
        return subprocess.CompletedProcess(args, 0, stdout="", stderr="")

    monkeypatch.setattr(lifecycle.subprocess, "run", run)

    lifecycle._DockerCli().remove_tag("ghcr.io/boldaxolotl/booley-sandbox:0.2.5")

    assert commands == [
        [
            "docker",
            "image",
            "rm",
            "--no-prune",
            "ghcr.io/boldaxolotl/booley-sandbox:0.2.5",
        ]
    ]
    assert "--force" not in commands[0]


def test_tagged_parent_is_treated_as_its_exact_external_artifact(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = _project(tmp_path)
    dockerfile = root / ".booley_project" / "docker" / "Dockerfile"
    dockerfile.parent.mkdir()
    dockerfile.write_text(
        "# booley:keep\nFROM booley-sandbox-riscv:old\nRUN echo mine\n",
        encoding="utf-8",
    )
    exact_parent = "booley-sandbox-riscv:old"
    docker = FakeDocker({exact_parent: ("sha256:" + "e" * 64, {})})
    _wire(monkeypatch, docker)
    selected = lifecycle.project_image.project_image_name(root)

    nodes = lifecycle._nodes(root, selected, docker)

    assert [node.reference for node in nodes] == [selected]
    assert nodes[0].parent == exact_parent


def test_nodes_rejects_unsupported_managed_runtime_image(tmp_path: Path) -> None:
    root = _project(tmp_path)

    with pytest.raises(lifecycle.ImageLifecycleError, match="unsupported managed Sandbox Image"):
        lifecycle._nodes(root, "foreign:latest", FakeDocker({}))


def test_project_recipe_fingerprint_includes_arbitrary_context_files(tmp_path: Path):
    root = _project(tmp_path)
    docker_dir = root / ".booley_project" / "docker"
    docker_dir.mkdir()
    dockerfile = docker_dir / "Dockerfile"
    dockerfile.write_text(
        "# booley:parent=booley-sandbox\nFROM booley-sandbox\nCOPY setup.sh /setup.sh\n",
        encoding="utf-8",
    )
    helper = docker_dir / "setup.sh"
    helper.write_text("echo one\n", encoding="utf-8")
    payload = lifecycle.PayloadProvenance("1", "0.2.6", "payload")
    parent = lifecycle._base_node(payload)

    before = lifecycle._project_node(root, parent, payload).build.recipe_fingerprint
    helper.write_text("echo two\n", encoding="utf-8")
    after = lifecycle._project_node(root, parent, payload).build.recipe_fingerprint

    assert before != after


def test_docker_cli_preserves_label_and_mutation_failures(monkeypatch: pytest.MonkeyPatch) -> None:
    cli = lifecycle._DockerCli()

    monkeypatch.setattr(
        lifecycle.subprocess,
        "run",
        lambda *_args, **_kwargs: subprocess.CompletedProcess(
            [], 1, stdout="", stderr="No such image: absent"
        ),
    )
    assert cli.label("absent", lifecycle.LABEL_SCHEMA) is None

    monkeypatch.setattr(
        lifecycle.subprocess,
        "run",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(OSError("docker unavailable")),
    )
    with pytest.raises(lifecycle.ImageLifecycleError, match="inspect Docker label"):
        cli.label("image", lifecycle.LABEL_SCHEMA)
    with pytest.raises(lifecycle.ImageLifecycleError, match="retain image"):
        cli.tag("source", "target")
    with pytest.raises(lifecycle.ImageLifecycleError, match="remove retained tag"):
        cli.remove_tag("backup")

    monkeypatch.setattr(
        lifecycle.subprocess,
        "run",
        lambda *_args, **_kwargs: subprocess.CompletedProcess(
            [], 1, stdout="", stderr="tag rejected"
        ),
    )
    with pytest.raises(lifecycle.ImageLifecycleError, match="tag rejected"):
        cli.tag("source", "target")


def test_packaged_builder_reports_pull_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from booley.harness.setup import docker_image as init_docker_image

    root = _project(tmp_path)
    docker_dir = tmp_path / "installed" / "src" / "booley" / "data" / "docker"
    docker_dir.mkdir(parents=True)
    recipe = docker_dir / "Dockerfile"
    recipe.write_text("FROM scratch\n", encoding="utf-8")
    monkeypatch.setattr(harness_lifecycle, "docker_data_dir", lambda: docker_dir)
    monkeypatch.setattr(
        init_docker_image,
        "_try_pull_image",
        lambda *_args, **_kwargs: False,
    )
    node = lifecycle.ImageNode(
        lifecycle.BASE_IMAGE,
        recipe,
        lifecycle.PayloadProvenance("1", "0.2.6", "payload"),
        lifecycle.BuildProvenance("recipe", None),
    )

    with pytest.raises(lifecycle.ImageLifecycleError, match="could not pull"):
        harness_lifecycle._LegacyBuildAdapter(root, verbose=False).build(
            node,
            force=True,
            source=lifecycle.ArtifactSource.VERIFIED_RELEASE_PULL,
        )


def test_local_builder_dispatches_each_managed_recipe(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from booley.harness import init_cmd
    from booley.harness.setup import docker_image as init_docker_image

    root = _project(tmp_path)
    calls: list[str] = []
    monkeypatch.setattr(
        init_docker_image,
        "_step_docker_image",
        lambda *_args, **_kwargs: calls.append(lifecycle.BASE_IMAGE),
    )
    monkeypatch.setattr(
        init_docker_image,
        "ensure_flavor_image",
        lambda *_args, **_kwargs: calls.append("booley-sandbox-riscv"),
    )
    monkeypatch.setattr(
        init_cmd,
        "_step_project_image",
        lambda *_args: calls.append("project-booley-sandbox"),
    )
    payload = lifecycle.PayloadProvenance("1", "0.2.6", "payload")
    for reference in (
        lifecycle.BASE_IMAGE,
        "booley-sandbox-riscv",
        "project-booley-sandbox",
    ):
        node = lifecycle.ImageNode(
            reference,
            tmp_path / "recipe",
            payload,
            lifecycle.BuildProvenance("recipe", None),
        )
        harness_lifecycle._LegacyBuildAdapter(root, verbose=False).build(
            node,
            force=False,
            source=lifecycle.ArtifactSource.LOCAL_BUILD,
        )

    assert calls == [
        lifecycle.BASE_IMAGE,
        "booley-sandbox-riscv",
        "project-booley-sandbox",
    ]


def test_local_builder_reports_step_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from booley.harness.setup import docker_image as init_docker_image

    root = _project(tmp_path)
    payload = lifecycle.PayloadProvenance("1", "0.2.6", "payload")
    base = lifecycle.ImageNode(
        lifecycle.BASE_IMAGE,
        tmp_path / "recipe",
        payload,
        lifecycle.BuildProvenance("recipe", None),
    )

    def fail_step(context, _reference, **_kwargs) -> None:
        context.results.append(SimpleNamespace(status="err", detail="base build failed"))

    monkeypatch.setattr(init_docker_image, "_step_docker_image", fail_step)
    with pytest.raises(lifecycle.ImageLifecycleError, match="base build failed"):
        harness_lifecycle._LegacyBuildAdapter(root, verbose=False).build(
            base,
            force=True,
            source=lifecycle.ArtifactSource.LOCAL_BUILD,
        )


def _user_project_node(tmp_path: Path) -> tuple[Path, lifecycle.ImageNode]:
    root = _project(tmp_path)
    docker_dir = root / ".booley_project" / "docker"
    docker_dir.mkdir()
    (docker_dir / "requirements.txt").write_text("user-owned\n", encoding="utf-8")
    payload = lifecycle.PayloadProvenance("1", "0.2.6", "payload")
    node = lifecycle.ImageNode(
        "project-booley-sandbox",
        docker_dir / "Dockerfile",
        payload,
        lifecycle.BuildProvenance("recipe", lifecycle.BASE_IMAGE),
        lifecycle.BASE_IMAGE,
    )
    return root, node


def test_local_builder_reports_missing_user_recipe(tmp_path: Path) -> None:
    root, project_node = _user_project_node(tmp_path)
    with pytest.raises(lifecycle.ImageLifecycleError, match=r"Dockerfile.*missing"):
        harness_lifecycle._LegacyBuildAdapter(root, verbose=False).build(
            project_node,
            force=True,
            source=lifecycle.ArtifactSource.LOCAL_BUILD,
        )


def test_local_builder_reports_user_recipe_build_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root, project_node = _user_project_node(tmp_path)
    project_node.recipe.write_text("FROM scratch\n", encoding="utf-8")
    monkeypatch.setattr(
        lifecycle.project_image, "build_project_image", lambda *_args, **_kwargs: False
    )
    with pytest.raises(lifecycle.ImageLifecycleError, match="failed to rebuild"):
        harness_lifecycle._LegacyBuildAdapter(root, verbose=False).build(
            project_node,
            force=True,
            source=lifecycle.ArtifactSource.LOCAL_BUILD,
        )


def test_failed_backup_creation_cleans_earlier_backup(tmp_path: Path) -> None:
    class BackupFailureDocker(FakeDocker):
        def tag(self, source: str, target: str) -> None:
            if source == "sha256:second":
                raise lifecycle.ImageLifecycleError("second backup failed")
            super().tag(source, target)

    docker = BackupFailureDocker(
        {
            "first": ("sha256:first", {}),
            "second": ("sha256:second", {}),
        }
    )
    payload = lifecycle.PayloadProvenance("1", "0.2.6", "payload")
    nodes = tuple(
        lifecycle.ImageNode(
            reference,
            tmp_path / reference,
            payload,
            lifecycle.BuildProvenance("recipe", None),
        )
        for reference in ("first", "second")
    )

    with pytest.raises(lifecycle.ImageLifecycleError, match="second backup failed"):
        lifecycle._retain_prior_tags(tmp_path, nodes, docker)

    assert [mutation[0] for mutation in docker.mutations] == ["tag", "remove_tag"]


def test_mutation_retries_unstamped_build_then_fails(tmp_path: Path) -> None:
    class NoOpBuilder:
        def __init__(self) -> None:
            self.calls = 0

        def build(self, _node, *, force: bool, source: lifecycle.ArtifactSource) -> None:
            del force, source
            self.calls += 1

    node = lifecycle._base_node(lifecycle.PayloadProvenance("1", "0.2.6", "payload"))
    docker = FakeDocker({})
    builder = NoOpBuilder()

    with pytest.raises(lifecycle.ImageLifecycleError, match="expected provenance"):
        lifecycle._mutate(tmp_path, (node,), lifecycle.Intent.ENSURE, docker, builder)

    assert builder.calls == 2


def test_mutation_reports_failed_rollback_with_recovery_tag(tmp_path: Path) -> None:
    class RestoreFailureDocker(FakeDocker):
        def tag(self, source: str, target: str) -> None:
            if source.startswith("booley-lifecycle-backup-"):
                raise lifecycle.ImageLifecycleError("restore rejected")
            super().tag(source, target)

    node = lifecycle._base_node(lifecycle.PayloadProvenance("1", "0.2.6", "payload"))
    docker = RestoreFailureDocker(
        {node.reference: ("sha256:old", {lifecycle.LABEL_SCHEMA: "stale"})}
    )

    with pytest.raises(lifecycle.ImageLifecycleError, match="recovery names"):
        lifecycle._mutate(
            tmp_path,
            (node,),
            lifecycle.Intent.REFRESH,
            docker,
            FailingBuilder(docker),
        )


def test_incremental_adapter_prepare_and_policy_helpers(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    docker = FakeDocker({"candidate": ("sha256:candidate", {})})
    adapter = harness_lifecycle._IncrementalBuildAdapter(tmp_path, docker, verbose=False)
    node = _incremental_node(tmp_path, lifecycle.ImageRole.STANDARD_SUBSTRATE)
    monkeypatch.setattr(adapter, "_materialize_managed_project_recipe", lambda _node: None)
    monkeypatch.setattr(adapter, "_build_role", lambda *_args: None)

    assert adapter.prepare(node, candidate_reference="candidate", parent_reference=None) == (
        "candidate"
    )
    assert isinstance(
        harness_lifecycle._transaction_build_adapter(tmp_path, docker, verbose=False),
        harness_lifecycle._IncrementalBuildAdapter,
    )
    monkeypatch.setattr(
        harness_lifecycle.runtime_lifecycle,
        "_configured_image",
        lambda _root: "booley-sandbox-riscv",
    )
    monkeypatch.setattr(
        harness_lifecycle.runtime_lifecycle, "_project_requirements_body", lambda _root: "req"
    )
    assert harness_lifecycle._uses_managed_riscv_project(tmp_path)


def test_runtime_identity_and_ancestry_edge_guards(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path / "root"
    root.mkdir()
    outside = tmp_path / "outside.txt"
    outside.write_text("outside", encoding="utf-8")
    monkeypatch.setattr(lifecycle, "docker_data_dir", lambda: root / "a" / "b" / "c" / "d")
    monkeypatch.setattr(lifecycle, "resolve_wheel_source_fingerprint", lambda _root: "resolved")
    assert lifecycle._expected_wheel_source_fingerprint() == "resolved"
    assert lifecycle._hash_paths(root, (outside,))

    node = _incremental_node(tmp_path, lifecycle.ImageRole.STANDARD_SUBSTRATE)
    labels = dict(node.expected_labels)
    labels[lifecycle.LABEL_BUILD_ORIGIN] = "registry"
    docker = FakeDocker({node.reference: ("sha256:" + "a" * 64, labels)})
    assert lifecycle._node_provenance_reason(node, docker).code == "wrong-origin"
    child = replace(node, parent="missing-parent")
    assert lifecycle._node_ancestry_reason(child, docker).code == "parent-changed"
