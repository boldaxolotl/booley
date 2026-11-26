"""Authoritative Sandbox Image reconciliation (GitHub issue #128)."""

from __future__ import annotations

import subprocess
from contextlib import contextmanager, nullcontext
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

import pytest

import booley
from booley.harness import image_lifecycle as harness_lifecycle
from booley.runtime import host_docker, project_image
from booley.runtime import image_lifecycle as lifecycle
from booley.runtime.docker_capacity import (
    BuildEstimateClass,
    DockerBuildPlan,
    DockerBuildRequest,
)
from booley.runtime.image_identity import ImageMetadata
from booley.runtime.image_provenance import ENV_SANDBOX_FLAVOR
from booley.runtime.version_attribution import VersionOrigin
from tests.conftest import symlink_or_skip
from tests.docker_cli_guard import route_bounded_capture_through_subprocess_run
from tests.docker_image_cli_fake import DockerImageCli, image_row, serve_docker_image_cli
from tests.python_modules import set_importable
from tests.subprocess_budget import run_bounded_child

_ACTUAL_ALIAS_PROBE = lifecycle.project_image.project_data_alias_capable


@pytest.fixture(autouse=True)
def _route_bounded_capture_through_subprocess_run(monkeypatch: pytest.MonkeyPatch) -> None:
    """Let this file's ``subprocess.run`` fakes see the bounded Docker commands of lifecycle, project_image."""
    route_bounded_capture_through_subprocess_run(monkeypatch, lifecycle, project_image)


@pytest.fixture(autouse=True)
def fake_images_have_ordinary_layout(monkeypatch):
    monkeypatch.setattr(
        lifecycle.project_image, "project_data_alias_capable", lambda _image: False
    )


class FakeDocker:
    """In-memory adapter for Docker, the lifecycle's true external dependency."""

    def __init__(self, images: dict[str, tuple[str, dict[str, str]]]) -> None:
        self.images = images
        self.registry_identities: dict[str, tuple[str, ...]] = {}
        self.used_image_ids: frozenset[str] = frozenset()
        self.mutations: list[tuple[str, ...]] = []
        # Every ``(method, reference)`` read of an image, for call-count assertions.
        self.reads: list[tuple[str, str]] = []
        # ``Config.Env`` per image ID; tags of one image share it.
        self.environments: dict[str, dict[str, str]] = {}

    def image_id(self, image: str) -> str | None:
        self.reads.append(("image_id", image))
        record = self.images.get(image)
        return record[0] if record else None

    def inspect(self, image: str) -> ImageMetadata | None:
        self.reads.append(("inspect", image))
        record = self.images.get(image) or next(
            (row for row in self.images.values() if row[0] == image), None
        )
        if record is None:
            return None
        environment = dict(self.environments.get(record[0], {}))
        return ImageMetadata(image, record[0], dict(record[1]), environment, self._layers(image))

    def _layers(self, image: str) -> tuple[str, ...]:
        chain = []
        seen = set()
        for _ in range(len(self.images) + 1):
            record = self.images.get(image) or next(
                (row for row in self.images.values() if row[0] == image), None
            )
            if record is None or record[0] in seen:
                break
            seen.add(record[0])
            chain.insert(0, record[0])
            image = record[1].get(lifecycle.LABEL_PARENT_ARTIFACT, "")
        return tuple(chain)

    def repo_digests(self, image: str) -> tuple[str, ...]:
        return self.registry_identities.get(image, ())

    def image_ids(self) -> frozenset[str]:
        return frozenset(row[0] for row in self.images.values())

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
        parents = {
            labels.get(lifecycle.LABEL_PARENT_ARTIFACT) for _id, labels in self.images.values()
        }
        if image in self.used_image_ids or image in parents:
            raise lifecycle.ImageLifecycleError(f"conflict: unable to delete {image}")
        self.mutations.append(("remove_tag", image))
        self.images.pop(image, None)


def test_capacity_plan_projects_only_build_steps_by_role(tmp_path: Path) -> None:
    def node(reference, role, contracts=(None, None)):
        return SimpleNamespace(
            reference=reference,
            role=role,
            build=SimpleNamespace(recipe_fingerprint=f"recipe-{reference}"),
            effective_inputs=f"inputs-{reference}",
            runtime_base_contract=contracts[0],
            standard_substrate_contract=contracts[1],
        )

    nodes = (
        node("reuse", lifecycle.ImageRole.RUNTIME_BASE),
        node(
            "heavy",
            lifecycle.ImageRole.PROJECT_OVERLAY,
            ("runtime-contract", "substrate-contract"),
        ),
        node("pull", lifecycle.ImageRole.WHEEL_OVERLAY),
        node("thin", lifecycle.ImageRole.PROJECT_DATA_LAYOUT),
    )
    steps = tuple(
        SimpleNamespace(action=action)
        for action in (
            lifecycle.PlanAction.REUSE,
            lifecycle.PlanAction.BUILD,
            lifecycle.PlanAction.PULL,
            lifecycle.PlanAction.BUILD,
        )
    )
    plan = SimpleNamespace(nodes=nodes, steps=steps, project_root=tmp_path)

    projected = harness_lifecycle.capacity_plan(plan)

    assert projected is not None
    assert [request.managed_image for request in projected.requests] == ["heavy", "thin"]
    assert projected.requests[0].estimate_class is BuildEstimateClass.HEAVYWEIGHT
    assert projected.requests[1].estimate_class is BuildEstimateClass.THIN_OVERLAY
    assert projected.requests[1].cache_evidence.reference == "thin"
    heavy = dict(projected.requests[0].cache_evidence.expected_labels)
    assert heavy[lifecycle.LABEL_RUNTIME_BASE_CONTRACT] == "runtime-contract"
    assert heavy[lifecycle.LABEL_STANDARD_SUBSTRATE_CONTRACT] == "substrate-contract"
    thin = dict(projected.requests[1].cache_evidence.expected_labels)
    assert lifecycle.LABEL_RUNTIME_BASE_CONTRACT not in thin


@pytest.fixture
def host_policy(monkeypatch: pytest.MonkeyPatch):
    """Select Host Bootstrap policy through the real installation identity."""

    def configure(
        origin: VersionOrigin, status: lifecycle.Status, source_root: Path | None
    ) -> None:
        monkeypatch.setattr(
            booley, "version_attribution", SimpleNamespace(origin=origin, source_root=source_root)
        )
        monkeypatch.setattr(harness_lifecycle, "embedded_official_release", lambda: True)
        monkeypatch.setattr(
            harness_lifecycle.runtime_lifecycle,
            "reconcile",
            lambda *_args, **_kwargs: SimpleNamespace(status=status),
        )

    return configure


@pytest.mark.parametrize(
    ("origin", "status", "expected"),
    [
        pytest.param(VersionOrigin.DISTRIBUTION, lifecycle.Status.STALE, [], id="release"),
        pytest.param(VersionOrigin.SOURCE, lifecycle.Status.CURRENT, [], id="current"),
        pytest.param(
            VersionOrigin.SOURCE,
            lifecycle.Status.STALE,
            ["runtime base", "Sandbox Image"],
            id="no-source-root",
        ),
    ],
)
def test_host_capacity_requests_follow_policy_and_check(
    host_policy, origin: VersionOrigin, status: lifecycle.Status, expected: list[str]
) -> None:
    host_policy(origin, status, None)

    requests = harness_lifecycle.host_capacity_requests(lifecycle.Intent.ENSURE)

    assert [request.managed_image for request in requests] == expected


def test_host_capacity_requests_use_local_build_projection(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, host_policy
) -> None:
    from booley.harness.setup import docker_image

    expected = (DockerBuildRequest("base", "base"),)
    observed = []
    host_policy(VersionOrigin.SOURCE, lifecycle.Status.STALE, tmp_path)
    monkeypatch.setattr(harness_lifecycle, "docker_data_dir", lambda: tmp_path / "docker")
    # Private seam: the projection reads the source tree's build fingerprint and
    # delegates the plan to the setup module; the test pins what it is asked for.
    monkeypatch.setattr(docker_image, "_image_build_fingerprint", lambda _root: "fingerprint")
    monkeypatch.setattr(
        docker_image,
        "_local_build_capacity_requests",
        lambda *args, **kwargs: observed.append((args, kwargs)) or expected,
    )

    assert harness_lifecycle.host_capacity_requests(lifecycle.Intent.REFRESH) == expected
    assert observed[0][1]["rebuild_runtime_base"] is True


def test_host_capacity_requests_reports_inspect_failure_as_lifecycle_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, host_policy
) -> None:
    """An unanswerable runtime-base lookup is a lifecycle error, not a silent rebuild plan."""
    from booley.harness.setup import docker_image

    docker_dir = tmp_path / "docker"
    docker_dir.mkdir()
    (docker_dir / "Dockerfile.base").write_bytes(b"FROM scratch\n")
    (docker_dir / "Dockerfile").write_bytes(b"FROM booley-runtime-base\n")
    host_policy(VersionOrigin.SOURCE, lifecycle.Status.STALE, tmp_path)
    monkeypatch.setattr(harness_lifecycle, "docker_data_dir", lambda: docker_dir)
    monkeypatch.setattr(
        docker_image,
        "source_image_build_contracts",
        lambda _root: SimpleNamespace(runtime_base="contract"),
    )

    def docker_missing(*_args: object, **_kwargs: object) -> object:
        raise FileNotFoundError("docker")

    # Fail every Docker seam (the shared helper and raw subprocess); an empty PATH
    # does not hide `docker` on Windows.
    monkeypatch.setattr(docker_image.subprocess, "run", docker_missing)
    # Private seam: the shared Docker CLI runner is the process boundary.
    monkeypatch.setattr(host_docker, "_run_docker", docker_missing)

    with pytest.raises(lifecycle.ImageLifecycleError, match="cannot inspect Docker image"):
        harness_lifecycle.host_capacity_requests(lifecycle.Intent.ENSURE)


@pytest.fixture
def stub_transaction(monkeypatch: pytest.MonkeyPatch) -> SimpleNamespace:
    """Stub plan, prepare, commit and abort around ``reconcile_planned``."""
    tx = SimpleNamespace(
        prepared=SimpleNamespace(),
        adopted=lifecycle.LifecycleResult("image", "sha256:" + "9" * 64, lifecycle.Status.CHANGED),
        aborted=[],
    )

    def commit(prepared: object) -> lifecycle.LifecycleResult:
        assert prepared is tx.prepared
        if isinstance(tx.adopted, Exception):
            raise tx.adopted
        return tx.adopted

    planned = SimpleNamespace(nodes=(SimpleNamespace(role=lifecycle.ImageRole.WHEEL_OVERLAY),))
    monkeypatch.setattr(harness_lifecycle, "plan", lambda *_args, **_kwargs: planned)
    monkeypatch.setattr(harness_lifecycle, "prepare", lambda *_args, **_kwargs: tx.prepared)
    monkeypatch.setattr(harness_lifecycle, "commit", commit)
    monkeypatch.setattr(harness_lifecycle, "abort", tx.aborted.append)
    # Private seam: ``reconcile_planned`` reads tag snapshots through the harness
    # Docker adapter; the transaction steps around it are stubbed.
    monkeypatch.setattr(harness_lifecycle, "_docker_adapter", lambda: FakeDocker({}))
    monkeypatch.setattr(harness_lifecycle, "own_tag_snapshot", lambda *_args: {})
    return tx


def test_reconcile_planned_commits_prepared_graph(
    monkeypatch: pytest.MonkeyPatch, stub_transaction: SimpleNamespace
) -> None:
    superseded = ("sha256:" + "1" * 64,)
    # Init records the images its adoption superseded, for cleanup after keeper reissue.
    snapshots = iter(({"tag": "old"}, {"tag": "new"}))
    monkeypatch.setattr(harness_lifecycle, "own_tag_snapshot", lambda *_args: next(snapshots))
    monkeypatch.setattr(
        harness_lifecycle,
        "record_superseded_images",
        lambda _identity, before, after: (
            superseded if (before, after) == ({"tag": "old"}, {"tag": "new"}) else ()
        ),
    )

    result = harness_lifecycle.reconcile_planned(
        SimpleNamespace(project_root=Path("/chip")), lifecycle.Intent.ENSURE
    )

    assert result == replace(stub_transaction.adopted, superseded=superseded)


@pytest.mark.parametrize("error", [RuntimeError("docker inspect failed"), OSError("disk full")])
def test_reconcile_planned_keeps_adoption_when_superseded_record_fails(
    monkeypatch: pytest.MonkeyPatch, stub_transaction: SimpleNamespace, error: Exception
) -> None:
    def fail(*_args: object) -> tuple[str, ...]:
        raise error

    monkeypatch.setattr(harness_lifecycle, "record_superseded_images", fail)

    scope = SimpleNamespace(project_root=Path("/chip"))
    result = harness_lifecycle.reconcile_planned(scope, lifecycle.Intent.REFRESH)
    assert result is stub_transaction.adopted


def test_reconcile_planned_aborts_failed_commit(stub_transaction: SimpleNamespace) -> None:
    stub_transaction.adopted = RuntimeError("adoption failed")

    with pytest.raises(RuntimeError, match="adoption failed"):
        harness_lifecycle.reconcile_planned(
            SimpleNamespace(project_root=Path("/chip")), lifecycle.Intent.ENSURE
        )

    assert stub_transaction.aborted == [stub_transaction.prepared]


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
        elif node.role in {
            lifecycle.ImageRole.PROJECT_DATA_LAYOUT,
            lifecycle.ImageRole.PROJECT_OVERLAY,
        }:
            labels[lifecycle.LABEL_WHEEL_SHA256] = (
                self.docker.images[parent_reference][1].get(lifecycle.LABEL_WHEEL_SHA256) or ""
            )
        self.docker.images[candidate_reference] = (image_id, labels)
        self.built.append((node.role, parent_id))
        return candidate_reference


def _project(tmp_path: Path, image: str | None = None) -> Path:
    root = tmp_path / "project"
    booley_dir = root / ".booley"
    booley_dir.mkdir(parents=True)
    body = "[sandbox]\n"
    if image is not None:
        body += f'image = "{image}"\n'
    (booley_dir / "booley.toml").write_text(body, encoding="utf-8")
    return root


def _check_project(
    root: Path,
    docker: FakeDocker,
    *,
    artifact_policy: lifecycle.ArtifactPolicy = lifecycle.ArtifactPolicy.LOCAL_ONLY,
) -> lifecycle.LifecycleResult:
    """Run a Project image CHECK: plan the graph, then report it without mutation."""
    observed = lifecycle.plan(
        lifecycle.ProjectImageScope(root),
        docker=docker,
        artifact_policy=artifact_policy,
        intent=lifecycle.Intent.CHECK,
    )
    return lifecycle.planned_result(observed, docker)


def _source_recipe_tree(tmp_path: Path) -> tuple[Path, Path]:
    source_root = tmp_path / "source"
    docker_dir = source_root / "src" / "booley" / "data" / "docker"
    docker_dir.mkdir(parents=True)
    (source_root / "pyproject.toml").write_text("[project]\nname='booley'\n", encoding="utf-8")
    (docker_dir / "Dockerfile").write_text("FROM scratch\n", encoding="utf-8")
    (docker_dir / "Dockerfile.riscv").write_text("FROM booley-sandbox\n", encoding="utf-8")
    (docker_dir / "stable-base-inputs.txt").write_text("pyproject.toml\n", encoding="utf-8")
    edalize = source_root / "src" / "booley" / "data" / "edalize" / "verible.py"
    edalize.parent.mkdir(parents=True)
    edalize.write_text("# adapter\n", encoding="utf-8")
    bwave = source_root / "crates" / "bwave"
    for relative in ("Cargo.toml", "Cargo.lock", "src/lib.rs", "schema/query.json", "docs/a.md"):
        path = bwave / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(relative + "\n", encoding="utf-8")
    return source_root, docker_dir


def pin_image_inputs(
    monkeypatch: pytest.MonkeyPatch,
    *,
    wheel: str | None = "wheel",
    contracts: lifecycle.ImageBuildContracts | None = None,
) -> None:
    """Pin the wheel-source fingerprint and, when given, the build contracts."""
    # Private seam: both are computed from the running Booley source or wheel; a
    # test cannot change them without editing the checkout.
    monkeypatch.setattr(lifecycle, "_expected_wheel_source_fingerprint", lambda: wheel)
    if contracts is not None:
        monkeypatch.setattr(lifecycle, "_expected_image_build_contracts", lambda: contracts)


def _host_base_node(
    fingerprint: str = "payload-new", schema: str = lifecycle.PROVENANCE_SCHEMA
) -> lifecycle.ImageNode:
    """The runtime-base node a host reconcile plans for the given payload."""
    # Private seam: the host base node is built inside ``reconcile``; no public
    # call returns it.
    return lifecycle._base_node(lifecycle.PayloadProvenance(schema, "0.2.6", fingerprint))


def _wire(
    monkeypatch: pytest.MonkeyPatch, docker: FakeDocker, *, pin_contracts: bool = True
) -> FakeBuilder:
    # Private seam: the lifecycle reads its payload, wheel, version, Docker and
    # builders from the running installation; this is the one place a test replaces them.
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
    monkeypatch.setattr(lifecycle, "_expected_wheel_source_fingerprint", lambda: "wheel")
    monkeypatch.setattr(
        lifecycle,
        "_transaction_build_adapter",
        lambda *_args, **_kwargs: TransactionBuilder(docker),
    )
    monkeypatch.setattr(lifecycle, "_expected_version", lambda: "0.2.6")
    monkeypatch.setattr(docker_base_contract, "contract", lambda _root: "stable-contract")
    monkeypatch.setattr(lifecycle, "_docker_adapter", lambda: docker)
    monkeypatch.setattr(lifecycle, "_build_adapter", lambda *_args, **_kwargs: builder)
    try:
        contracts = lifecycle._expected_image_build_contracts()
    except (lifecycle.ImageLifecycleError, ValueError):
        contracts = lifecycle.ImageBuildContracts("stable-contract", "standard-contract")
    else:
        if pin_contracts:
            monkeypatch.setattr(
                lifecycle,
                "_expected_image_build_contracts",
                lambda: replace(contracts, runtime_base="stable-contract"),
            )
    directory = lifecycle.docker_data_dir()
    for recipe in ("Dockerfile.base", "Dockerfile.substrate", "Dockerfile.wheel"):
        if not (directory / recipe).is_file():
            (directory / recipe).write_text("FROM scratch\n", encoding="utf-8")
    base, _ = lifecycle._source_graph_base(
        replace(contracts, runtime_base="stable-contract"), directory
    )
    docker.images[lifecycle.STABLE_RUNTIME_BASE_IMAGE] = (
        stable_id,
        dict(base.expected_labels) | {lifecycle.LABEL_BUILD_ORIGIN: "local"},
    )
    return builder


def test_incremental_plan_builds_only_wheel_overlay_for_python_change(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = _project(tmp_path)
    docker = FakeDocker({})
    _wire(monkeypatch, docker)
    pin_image_inputs(monkeypatch, wheel="wheel-old")
    initial = lifecycle.plan(lifecycle.ProjectImageScope(root), docker=docker)
    _install_planned_graph(docker, initial.nodes)
    current = lifecycle.plan(lifecycle.ProjectImageScope(root), docker=docker)
    assert all(step.action is lifecycle.PlanAction.REUSE for step in current.steps)

    pin_image_inputs(monkeypatch, wheel="wheel-new")
    refreshed = lifecycle.plan(lifecycle.ProjectImageScope(root), docker=docker)

    assert [step.action for step in refreshed.steps] == [
        lifecycle.PlanAction.REUSE,
        lifecycle.PlanAction.REUSE,
        lifecycle.PlanAction.BUILD,
    ]
    assert refreshed.steps[-1].role is lifecycle.ImageRole.WHEEL_OVERLAY
    assert refreshed.steps[-1].reason.code == "inputs-changed"


def test_source_and_release_final_images_share_selection_fingerprint(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = _project(tmp_path)
    docker = FakeDocker({})
    _wire(monkeypatch, docker)

    source = lifecycle.plan(lifecycle.ProjectImageScope(root), docker=docker)
    release = lifecycle.plan(
        lifecycle.ProjectImageScope(root),
        docker=docker,
        artifact_policy=lifecycle.ArtifactPolicy.VERIFIED_RELEASE_ONLY,
    )

    assert source.nodes[-1].logical_selection_fingerprint
    assert (
        source.nodes[-1].logical_selection_fingerprint
        == release.nodes[-1].logical_selection_fingerprint
    )
    assert (
        dict(release.nodes[-1].expected_labels)[lifecycle.LABEL_LOGICAL_SELECTION_FINGERPRINT]
        == release.nodes[-1].logical_selection_fingerprint
    )


def test_incremental_plan_propagates_standard_change_only_to_descendants(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = _project(tmp_path, "booley-sandbox-riscv")
    docker = FakeDocker({})
    _wire(monkeypatch, docker)
    initial = lifecycle.plan(lifecycle.ProjectImageScope(root), docker=docker)
    _install_planned_graph(docker, initial.nodes)
    pin_image_inputs(
        monkeypatch,
        contracts=lifecycle.ImageBuildContracts(
            runtime_base=initial.nodes[0].effective_inputs,
            standard_substrate="standard-new",
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


def test_prepare_uses_realized_parent_ids_and_commit_adopts_after_verification(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = _project(tmp_path)
    docker = FakeDocker({})
    _wire(monkeypatch, docker)
    planned = lifecycle.plan(lifecycle.ProjectImageScope(root), docker=docker)
    builder = TransactionBuilder(docker)

    prepared = lifecycle.prepare(planned, docker=docker, builder=builder)

    assert docker.image_id(lifecycle.BASE_IMAGE) is None
    assert builder.built[0][1] == prepared.candidates[0].image_id
    assert builder.built[1][1] == prepared.candidates[1].image_id

    result = lifecycle.commit(prepared, docker=docker)

    assert result.changed_images == tuple(node.reference for node in planned.nodes[1:])
    assert result.selected_id == prepared.candidates[-1].image_id
    assert result.wheel_source_fingerprint == "wheel"
    assert result.wheel_sha256 == "e" * 64


def test_parent_artifact_kind_is_part_of_reuse_ancestry(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = _project(tmp_path)
    docker = FakeDocker({})
    _wire(monkeypatch, docker)
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
    planned = lifecycle.plan(lifecycle.ProjectImageScope(root), docker=docker)
    prepared = lifecycle.prepare(planned, docker=docker, builder=TransactionBuilder(docker))
    selected = prepared.candidates[-1]
    docker.images[selected.candidate_reference] = (
        "sha256:changed",
        docker.images[selected.candidate_reference][1],
    )

    with pytest.raises(lifecycle.ImageLifecycleError, match="changed before commit"):
        lifecycle.validate(prepared, docker=docker)


_BUILD_CONTRACTS = lifecycle.ImageBuildContracts("stable-contract", "standard-contract")
_WHEEL_SHA256 = (
    "ba59926159d2aa256eb8739b8da7e2b574b960e1202c6d624cbe981cef996c91"  # sha256(b"wheel")
)


@pytest.fixture
def docker_builds(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> SimpleNamespace:
    """A source installation whose Docker builds are recorded and planted in ``FakeDocker``.

    Knobs: ``wheels`` (wheels one wheel build leaves), ``wheel_built`` (False declines
    the wheel build), ``fingerprint`` (what the built wheel embeds), ``contracts`` (what
    the source tree computes) and ``results`` (exit code per built role).
    """
    from booley.harness.setup import docker_image

    _set_source_installation(tmp_path, monkeypatch)
    set_importable(monkeypatch, ensurepip=True)
    docker = FakeDocker({})
    _wire(monkeypatch, docker)
    pin_image_inputs(monkeypatch, contracts=_BUILD_CONTRACTS)
    env = SimpleNamespace(
        docker=docker,
        specs=[],
        capacity=[],
        wheels=1,
        wheel_built=True,
        fingerprint="wheel",
        contracts=_BUILD_CONTRACTS,
        results={},
    )

    def build_wheel(_context, root: Path, *, preserve_stamp: bool) -> bool:
        del preserve_stamp
        if env.wheel_built:
            (root / "dist").mkdir(exist_ok=True)
            for index in range(env.wheels):
                (root / "dist" / f"booley_rtl-0.2.{index}.whl").write_bytes(b"wheel")
        return env.wheel_built

    def build_image(_context, spec) -> int | None:
        env.specs.append(spec)
        labels = dict(spec.labels)
        if spec.parent_artifact:
            labels[lifecycle.LABEL_PARENT_ARTIFACT] = spec.parent_artifact
            labels[lifecycle.LABEL_PARENT_ARTIFACT_KIND] = lifecycle.PARENT_ARTIFACT_LOCAL_IMAGE_ID
        docker.images[spec.image] = (f"sha256:{len(env.specs) + 40:064x}", labels)
        return env.results.get(lifecycle.ImageRole(labels[lifecycle.LABEL_ARTIFACT_ROLE]), 0)

    def refresh_plan(root: Path) -> lifecycle.LifecyclePlan:
        """Plan a REFRESH of *root* with only the runtime base installed."""
        scope = lifecycle.ProjectImageScope(root)
        _install_planned_graph(docker, lifecycle.plan(scope, docker=docker).nodes[:1])
        return lifecycle.plan(scope, docker=docker, intent=lifecycle.Intent.REFRESH)

    env.refresh_plan = refresh_plan
    # Private seam: ``docker build`` is the process boundary; ``FakeDocker`` has no build port.
    monkeypatch.setattr(docker_image, "_docker_build_wheel", build_wheel)
    monkeypatch.setattr(docker_image, "_docker_build_image", build_image)
    monkeypatch.setattr(
        harness_lifecycle,
        "ensure_docker_build_capacity",
        lambda *_args, **kwargs: env.capacity.append(kwargs["remaining_plan"].requests),
    )
    monkeypatch.setattr(
        harness_lifecycle, "wheel_embedded_source_fingerprint", lambda _wheel: env.fingerprint
    )
    monkeypatch.setattr(
        harness_lifecycle, "source_image_build_contracts", lambda _root: env.contracts
    )
    return env


def _requirements_project(tmp_path: Path) -> Path:
    """A RISC-V Project whose pip requirements select the generated Project substrate."""
    root = _project(tmp_path, "booley-sandbox-riscv")
    (root / "requirements.txt").write_text("cocotb==2.0.0\n", encoding="utf-8")
    (root / ".booley" / "booley.toml").write_text(
        '[sandbox]\nimage = "booley-sandbox-riscv"\npip_requirements = ["requirements.txt"]\n',
        encoding="utf-8",
    )
    return root


def test_prepare_refuses_a_wheel_build_without_ensurepip_before_any_image_build(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, docker_builds: SimpleNamespace
) -> None:
    set_importable(monkeypatch, ensurepip=False, virtualenv=False)
    planned = docker_builds.refresh_plan(_project(tmp_path))

    with pytest.raises(lifecycle.ImageLifecycleError, match=r"apt install python\d+\.\d+-venv"):
        harness_lifecycle.prepare(planned)

    assert docker_builds.specs == []
    assert docker_builds.capacity == []


def test_prepare_builds_each_role_from_its_parent_candidate(
    tmp_path: Path, docker_builds: SimpleNamespace
) -> None:
    root = _requirements_project(tmp_path)
    planned = docker_builds.refresh_plan(root)
    proxy = DockerBuildRequest("proxy", "proxy", BuildEstimateClass.THIN_OVERLAY)

    prepared = harness_lifecycle.prepare(planned, future_requests=(proxy,))

    assert planned.selected_reference == lifecycle.project_image.project_image_name(root)
    assert [node.role for node in planned.nodes] == [
        lifecycle.ImageRole.RUNTIME_BASE,
        lifecycle.ImageRole.STANDARD_SUBSTRATE,
        lifecycle.ImageRole.RISCV_SUBSTRATE,
        lifecycle.ImageRole.PROJECT_SUBSTRATE,
        lifecycle.ImageRole.WHEEL_OVERLAY,
    ]
    # The capacity check sees every build still to run, then the sidecar.
    assert [request.managed_image for request in docker_builds.capacity[0]] == [
        *(node.reference for node in planned.nodes[1:]),
        "proxy",
    ]
    specs = docker_builds.specs
    candidates = prepared.candidates
    assert [spec.image for spec in specs] == [c.candidate_reference for c in candidates[1:]]
    keys = [
        "booley-runtime-base",
        "booley-standard-substrate",
        "booley-project-parent",
        "booley-substrate",
    ]
    for spec, key, parent in zip(specs, keys, candidates[:-1], strict=True):
        assert spec.build_contexts == ((key, "docker-image://" + parent.candidate_reference),)
        assert spec.parent_artifact == parent.image_id
    assert specs[-1].build_args[-2:] == ("--build-arg", "BOOLEY_WHEEL_SHA256=" + _WHEEL_SHA256)
    assert (lifecycle.LABEL_WHEEL_SHA256, _WHEEL_SHA256) in specs[-1].labels
    assert candidates[-1].wheel_sha256 == _WHEEL_SHA256
    recipe = root / ".booley" / "docker" / "Dockerfile"
    assert (
        recipe.read_text(encoding="utf-8") == dict(planned.nodes[3].generated_files)["Dockerfile"]
    )


@pytest.mark.parametrize(
    ("knobs", "message"),
    [
        pytest.param({"wheels": 0}, "exactly one wheel", id="no-wheel"),
        pytest.param({"wheels": 2}, "exactly one wheel", id="two-wheels"),
        pytest.param({"fingerprint": "actual"}, "source fingerprint", id="wheel-source-drift"),
        pytest.param(
            {"results": {lifecycle.ImageRole.WHEEL_OVERLAY: 1}},
            "wheel-overlay build failed",
            id="build-failure",
        ),
        pytest.param({"wheel_built": False}, "produced no image", id="declined-wheel-build"),
        pytest.param(
            {"contracts": replace(_BUILD_CONTRACTS, standard_substrate="other")},
            "standard-substrate build context differs",
            id="standard-contract-drift",
        ),
        pytest.param(
            {"parent": "missing:parent"}, "could not resolve prepared parent", id="parent"
        ),
        pytest.param({"role": lifecycle.ImageRole.EXTERNAL}, "unsupported image role", id="role"),
    ],
)
def test_prepare_refuses_inconsistent_build_output(
    tmp_path: Path, docker_builds: SimpleNamespace, knobs: dict, message: str
) -> None:
    planned = docker_builds.refresh_plan(_project(tmp_path))
    # A crafted plan reaches guards no real plan can: the wheel node names the change.
    knobs = dict(knobs)  # parametrize objects are reused when CI repeats a test
    changes = {key: knobs.pop(key) for key in ("parent", "role") if key in knobs}
    if changes:
        planned = replace(
            planned, nodes=(*planned.nodes[:-1], replace(planned.nodes[-1], **changes))
        )
    for name, value in knobs.items():
        setattr(docker_builds, name, value)

    with pytest.raises(lifecycle.ImageLifecycleError, match=message):
        harness_lifecycle.prepare(planned)


def test_prepare_accepts_a_build_that_reports_no_change(
    tmp_path: Path, docker_builds: SimpleNamespace
) -> None:
    docker_builds.results = {lifecycle.ImageRole.WHEEL_OVERLAY: None}

    prepared = harness_lifecycle.prepare(docker_builds.refresh_plan(_project(tmp_path)))

    assert len(prepared.candidates) == 3


def _release_puller(
    monkeypatch: pytest.MonkeyPatch, docker: FakeDocker, labels: dict[str, str] | None
) -> None:
    """Fake the registry pull: plant the remote tag with *labels*, or fail when None."""
    from booley.harness.setup import docker_image

    def pull(version: str, image: str = lifecycle.BASE_IMAGE, *, adopt: bool = True) -> bool:
        assert adopt is False
        if labels is not None:
            docker.images[docker_image.remote_tag(image, version)] = ("sha256:" + "a" * 64, labels)
        return labels is not None

    # Private seam: the registry pull is the network boundary.
    monkeypatch.setattr(docker_image, "_try_pull_image", pull)


@pytest.mark.parametrize(
    ("case", "message"),
    [
        pytest.param("verified", None, id="verified"),
        pytest.param("pull-fails", "could not pull", id="pull-fails"),
        pytest.param("local-parent", "registry ancestry", id="local-parent"),
        pytest.param("other-role", "no complete published", id="other-role"),
        pytest.param("other-flavor", "image is booley-sandbox-riscv", id="other-flavor"),
    ],
)
def test_prepare_pulls_complete_release_and_refuses_other_roles(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    docker_builds: SimpleNamespace,
    case: str,
    message: str | None,
) -> None:
    from booley.harness.setup import docker_image

    planned = lifecycle.plan(
        lifecycle.ProjectImageScope(_project(tmp_path)),
        docker=docker_builds.docker,
        artifact_policy=lifecycle.ArtifactPolicy.VERIFIED_RELEASE_ONLY,
    )
    node = planned.nodes[0]
    # A published release without the selection label still verifies.
    labels = _registry_labels(node, "ghcr.io/boldaxolotl/booley-sandbox-base@sha256:" + "d" * 64)
    labels.pop(lifecycle.LABEL_LOGICAL_SELECTION_FINGERPRINT)
    labels[lifecycle.LABEL_WHEEL_SHA256] = "e" * 64
    if case == "local-parent":
        labels[lifecycle.LABEL_PARENT_ARTIFACT_KIND] = lifecycle.PARENT_ARTIFACT_LOCAL_IMAGE_ID
    if case == "other-role":
        planned = replace(
            planned, nodes=(replace(node, role=lifecycle.ImageRole.STANDARD_SUBSTRATE),)
        )
    if case == "other-flavor":
        docker_builds.docker.environments["sha256:" + "a" * 64] = RISCV_ENVIRONMENT
    _release_puller(monkeypatch, docker_builds.docker, None if case == "pull-fails" else labels)

    if message is not None:
        with pytest.raises(lifecycle.ImageLifecycleError, match=message):
            harness_lifecycle.prepare(planned)
        return
    prepared = harness_lifecycle.prepare(planned)

    assert prepared.candidates[-1].candidate_reference == docker_image.remote_tag(
        lifecycle.BASE_IMAGE, "0.2.6"
    )


@pytest.mark.parametrize(
    "policy", [lifecycle.ArtifactPolicy.LOCAL_ONLY, lifecycle.ArtifactPolicy.VERIFIED_RELEASE_ONLY]
)
def test_plan_refuses_a_source_without_wheel_fingerprint(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, policy: lifecycle.ArtifactPolicy
) -> None:
    pin_image_inputs(monkeypatch, wheel=None)

    with pytest.raises(lifecycle.ImageLifecycleError, match="wheel-source fingerprint"):
        lifecycle.plan(
            lifecycle.ProjectImageScope(_project(tmp_path)),
            docker=FakeDocker({}),
            artifact_policy=policy,
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


def test_runtime_plan_rejects_wrong_scope_and_external_image(tmp_path: Path) -> None:
    docker = FakeDocker({})
    with pytest.raises(TypeError, match="ProjectImageScope"):
        lifecycle.plan(object(), docker=docker)  # type: ignore[arg-type]
    with pytest.raises(lifecycle.ImageLifecycleError, match="externally managed"):
        lifecycle.plan(
            lifecycle.ProjectImageScope(_project(tmp_path, "acme/custom")), docker=docker
        )


class _CandidateBuilder(FakeBuilder):
    """Builder that returns its result under a separate candidate reference."""

    def build(self, node, *, force: bool, source: lifecycle.ArtifactSource) -> str:
        super().build(node, force=force, source=source)
        candidate = f"candidate-{node.reference}"
        _id, labels = self.docker.images.pop(node.reference)
        self.docker.images[candidate] = (f"sha256:{0xCA:064x}", labels)
        return candidate


def _installed_project_graph(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> tuple[FakeDocker, lifecycle.LifecyclePlan]:
    """A current Project graph in a fake Docker, plus the plan that produced it."""
    docker = FakeDocker({})
    _wire(monkeypatch, docker)
    planned = lifecycle.plan(lifecycle.ProjectImageScope(_project(tmp_path)), docker=docker)
    _install_planned_graph(docker, planned.nodes)
    return docker, planned


# Private seam: the next four checks call ``_planned_reason``, ``_inspect_nodes``,
# ``_verify_prepared_image`` and ``_mutate`` directly. ``_inspect_nodes`` and ``_mutate``
# run only on Project nodes in these tests; production passes them the host base node alone, and
# the plan and prepare paths read the selected image more than once by design.
def _check_planned_reason(docker: FakeDocker, planned: lifecycle.LifecyclePlan) -> str:
    node = planned.nodes[-1]
    assert lifecycle._planned_reason(node, docker, parent_invalid=False) is None
    return node.reference


# Private seam: see the note above ``_check_planned_reason``.
def _check_inspect_nodes(docker: FakeDocker, planned: lifecycle.LifecyclePlan) -> str:
    # The schema-two substrate node is checked for staleness.
    node = planned.nodes[1]
    stale, last_id = lifecycle._inspect_nodes(
        (node,), docker, (lifecycle.ArtifactSource.LOCAL_BUILD,)
    )
    assert stale == ()
    assert last_id == docker.images[node.reference][0]
    return node.reference


def _check_planned_result(docker: FakeDocker, planned: lifecycle.LifecyclePlan) -> str:
    result = lifecycle.planned_result(planned, docker)
    assert result.selected_id is not None
    return planned.steps[-1].source_reference or planned.selected_reference


# Private seam: see the note above ``_check_planned_reason``.
def _check_verify_prepared(docker: FakeDocker, planned: lifecycle.LifecyclePlan) -> str:
    node = planned.nodes[-1]
    parent_id = docker.image_id(node.parent)
    docker.reads.clear()
    prepared = lifecycle._verify_prepared_image(node, node.reference, parent_id, docker)
    assert prepared.image_id == docker.images[node.reference][0]
    return node.reference


# Private seam: see the note above ``_check_planned_reason``.
def _check_mutate_adopts_candidate(docker: FakeDocker, planned: lifecycle.LifecyclePlan) -> str:
    node = replace(planned.nodes[-1], effective_inputs="changed-inputs")
    changed = lifecycle._mutate(
        "scope",
        (node,),
        lifecycle.Intent.ENSURE,
        docker,
        _CandidateBuilder(docker),
        shipped_sources=(lifecycle.ArtifactSource.LOCAL_BUILD,),
    )
    assert changed == (node.reference,)
    return f"candidate-{node.reference}"


@pytest.mark.parametrize(
    "check",
    [
        _check_planned_reason,
        _check_inspect_nodes,
        _check_planned_result,
        _check_verify_prepared,
        _check_mutate_adopts_candidate,
    ],
    ids=lambda check: check.__name__,
)
def test_per_image_check_inspects_checked_image_once(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, check
) -> None:
    docker, planned = _installed_project_graph(tmp_path, monkeypatch)
    docker.reads.clear()

    checked = check(docker, planned)

    # Reads of other images (parent, runtime base) are separate and not counted.
    reads = [read for read in docker.reads if read[1] == checked]
    assert reads == [("inspect", checked)]


def test_host_check_inspects_base_image_once(monkeypatch: pytest.MonkeyPatch) -> None:
    docker = FakeDocker({lifecycle.BASE_IMAGE: ("sha256:" + "a" * 64, {})})
    _wire(monkeypatch, docker)
    docker.reads.clear()

    result = lifecycle.reconcile(lifecycle.Intent.CHECK)

    assert result.selected_id == "sha256:" + "a" * 64
    reads = [read for read in docker.reads if read[1] == lifecycle.BASE_IMAGE]
    assert reads == [("inspect", lifecycle.BASE_IMAGE)]


def test_descends_from_needs_both_images_and_a_nonempty_layer_prefix() -> None:
    def image(*layers: str) -> ImageMetadata:
        return ImageMetadata("ref", "sha256:id", {}, {}, layers)

    assert lifecycle.descends_from(image("a"), image("a", "b"))
    assert not lifecycle.descends_from(image("a", "b"), image("a"))
    assert not lifecycle.descends_from(image("a"), image("x", "b"))
    assert not lifecycle.descends_from(image(), image("a"))
    assert not lifecycle.descends_from(None, image("a"))
    assert not lifecycle.descends_from(image("a"), None)


def test_plan_and_prepare_reject_wrong_provenance(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    docker, planned = _installed_project_graph(tmp_path, monkeypatch)
    scope = lifecycle.ProjectImageScope(planned.project_root)
    final = planned.nodes[-1]
    labels = docker.images[final.reference][1]

    labels[lifecycle.LABEL_RECIPE_FINGERPRINT] = "changed"
    assert lifecycle.plan(scope, docker=docker).steps[-1].reason.code == "recipe-changed"
    labels[lifecycle.LABEL_RECIPE_FINGERPRINT] = final.build.recipe_fingerprint
    labels.pop(lifecycle.LABEL_WHEEL_SHA256)
    assert lifecycle.plan(scope, docker=docker).steps[-1].reason.code == "inputs-changed"

    refresh = lifecycle.plan(scope, docker=docker, intent=lifecycle.Intent.REFRESH)

    class NoWheelShaBuilder(TransactionBuilder):
        def prepare(self, node, *, candidate_reference: str, parent_reference: str | None) -> str:
            super().prepare(
                node, candidate_reference=candidate_reference, parent_reference=parent_reference
            )
            self.docker.images[candidate_reference][1].pop(lifecycle.LABEL_WHEEL_SHA256, None)
            return candidate_reference

    class VanishingBuilder(TransactionBuilder):
        def prepare(self, node, *, candidate_reference: str, parent_reference: str | None) -> str:
            return "missing"

    with pytest.raises(lifecycle.ImageLifecycleError, match="no wheel SHA"):
        lifecycle.prepare(refresh, docker=docker, builder=NoWheelShaBuilder(docker))
    with pytest.raises(lifecycle.ImageLifecycleError, match="disappeared"):
        lifecycle.prepare(refresh, docker=docker, builder=VanishingBuilder(docker))


def test_runtime_mutation_requires_an_orchestrator_adapter(tmp_path: Path) -> None:
    docker = FakeDocker({})
    with pytest.raises(TypeError, match="build adapter"):
        lifecycle.reconcile(lifecycle.Intent.ENSURE, docker=docker)
    with pytest.raises(TypeError, match="transaction build adapter"):
        lifecycle.prepare(SimpleNamespace(project_root=tmp_path), docker=docker)


# Selection fingerprints the release path produced when it repeated the image
# chain by hand, for the fixture in ``_release_recipes`` below. Never recompute.
_PRE_CHANGE_RELEASE_FINGERPRINTS = {
    "booley-sandbox": "8e3626aec0404786a141cb691f924a4a9f3cadc74ab80122596f0951ec96f9b6",
    "booley-sandbox-riscv": "04bc281c2e53e83b0a1f94ce35d22fae0f6d0b9d2e218276342803f28da8f95a",
}


def _release_recipes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> lifecycle.ImageBuildContracts:
    """Shipped recipe files whose body names the file, with fixed build contracts."""
    for name in (
        "Dockerfile.base",
        "Dockerfile.substrate",
        "Dockerfile.riscv",
        "Dockerfile.wheel",
    ):
        (tmp_path / name).write_text(f"FROM scratch\n# {name}\n", encoding="utf-8", newline="\n")
    monkeypatch.setattr(lifecycle, "docker_data_dir", lambda: tmp_path)
    return lifecycle.ImageBuildContracts("runtime-contract", "standard-contract")


@pytest.mark.parametrize("selected", sorted(_PRE_CHANGE_RELEASE_FINGERPRINTS))
def test_release_selection_fingerprint_is_pinned_to_pre_change_chain(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, selected: str
) -> None:
    contracts = _release_recipes(tmp_path, monkeypatch)
    scope = lifecycle.ProjectImageScope(_project(tmp_path, selected))

    def fingerprint() -> str | None:
        planned = lifecycle.plan(
            scope,
            docker=FakeDocker({}),
            artifact_policy=lifecycle.ArtifactPolicy.VERIFIED_RELEASE_ONLY,
        )
        return planned.nodes[0].logical_selection_fingerprint

    pin_image_inputs(monkeypatch, contracts=contracts)
    assert fingerprint() == _PRE_CHANGE_RELEASE_FINGERPRINTS[selected]
    # The selection follows the chain's contracts.
    pin_image_inputs(monkeypatch, contracts=replace(contracts, runtime_base="another"))
    assert fingerprint() != _PRE_CHANGE_RELEASE_FINGERPRINTS[selected]


@pytest.mark.parametrize("intent", [lifecycle.Intent.CHECK, lifecycle.Intent.ENSURE])
def test_host_reconcile_of_distribution_without_contract_metadata_is_refused(
    monkeypatch: pytest.MonkeyPatch, intent: lifecycle.Intent
) -> None:
    from booley.runtime.version_attribution import VersionOrigin

    monkeypatch.setattr(
        booley,
        "version_attribution",
        SimpleNamespace(origin=VersionOrigin.DISTRIBUTION, source_root=None),
    )
    monkeypatch.setattr(
        lifecycle,
        "expected_image_build_contracts",
        lambda _attribution: (_ for _ in ()).throw(lifecycle.ImageBuildContractMetadataError("x")),
    )

    with pytest.raises(lifecycle.InstalledImageContractError):
        harness_lifecycle.reconcile(intent)


def test_recipe_write_never_follows_a_planted_link(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, docker_builds: SimpleNamespace
) -> None:
    root = _requirements_project(tmp_path)
    planned = docker_builds.refresh_plan(root)
    destination = tmp_path / "outside-recipe"
    destination.write_text("user file", encoding="utf-8")
    (root / ".booley" / "docker").mkdir()
    recipe = root / ".booley" / "docker" / "Dockerfile"
    symlink_or_skip(recipe, destination)
    planted_target = recipe.readlink()
    # Model the race: the link appears after the ownership check has passed.
    monkeypatch.setattr(project_image, "is_managed_generated_file", lambda _path: True)

    with pytest.raises(lifecycle.ImageLifecycleError, match="symlink"):
        harness_lifecycle.prepare(planned)

    assert destination.read_text(encoding="utf-8") == "user file"
    assert recipe.readlink() == planted_target


def test_official_release_plan_observes_only_selected_complete_image(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = _project(tmp_path, "booley-sandbox-riscv")
    docker = FakeDocker({})
    _wire(monkeypatch, docker)
    contracts = lifecycle.ImageBuildContracts("a" * 64, "b" * 64)
    pin_image_inputs(monkeypatch, contracts=contracts)
    # Private seam: the expected node is the planner's own release node; the
    # assertion below pins that the plan uses it unchanged.
    node = lifecycle._complete_release_node("booley-sandbox-riscv", contracts)
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
    docker.environments["sha256:" + "a" * 64] = RISCV_ENVIRONMENT

    planned = lifecycle.plan(
        lifecycle.ProjectImageScope(root),
        docker=docker,
        artifact_policy=lifecycle.ArtifactPolicy.VERIFIED_RELEASE_ONLY,
    )

    assert planned.nodes == (
        replace(
            node,
            reference=lifecycle.project_image.project_image_name(root),
            published_reference="booley-sandbox-riscv",
        ),
    )
    assert planned.steps[0].action is lifecycle.PlanAction.REUSE

    docker.images[node.reference][1][lifecycle.LABEL_RUNTIME_BASE_CONTRACT] = "wrong"
    stale = lifecycle.plan(
        lifecycle.ProjectImageScope(root),
        docker=docker,
        artifact_policy=lifecycle.ArtifactPolicy.VERIFIED_RELEASE_ONLY,
    )
    assert stale.steps[0].action is lifecycle.PlanAction.PULL
    assert stale.steps[0].reason.code == "missing"


def test_official_release_project_requirements_use_local_overlay_only(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = _project(tmp_path, "booley-sandbox-riscv")
    requirement = root / "requirements.txt"
    requirement.write_text("cocotb==2.0.1\n", encoding="utf-8")
    config = root / ".booley" / "booley.toml"
    config.write_text(
        '[sandbox]\nimage = "booley-sandbox-riscv"\npip_requirements = ["requirements.txt"]\n',
        encoding="utf-8",
    )
    pin_image_inputs(monkeypatch, contracts=lifecycle.ImageBuildContracts("a" * 64, "b" * 64))

    planned = lifecycle.plan(
        lifecycle.ProjectImageScope(root),
        docker=FakeDocker({}),
        artifact_policy=lifecycle.ArtifactPolicy.VERIFIED_RELEASE_THEN_LOCAL,
    )

    assert [node.reference for node in planned.nodes] == [
        lifecycle.project_image.project_image_name(root) + "-release",
        lifecycle.project_image.project_image_name(root),
    ]
    assert [node.role for node in planned.nodes] == [
        lifecycle.ImageRole.WHEEL_OVERLAY,
        lifecycle.ImageRole.PROJECT_OVERLAY,
    ]
    assert [node.acquisition_policy for node in planned.nodes] == [
        lifecycle.ArtifactPolicy.VERIFIED_RELEASE_ONLY,
        lifecycle.ArtifactPolicy.LOCAL_ONLY,
    ]
    assert [step.action for step in planned.steps] == [
        lifecycle.PlanAction.PULL,
        lifecycle.PlanAction.BUILD,
    ]


def test_hybrid_project_selection_fingerprint_includes_release_flavor(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = _project(tmp_path)
    (root / "requirements.txt").write_text("cocotb==2.0.1\n", encoding="utf-8")
    pin_image_inputs(monkeypatch, contracts=lifecycle.ImageBuildContracts("a" * 64, "b" * 64))
    fingerprints = []
    for configured in ("booley-sandbox", "booley-sandbox-riscv"):
        (root / ".booley" / "booley.toml").write_text(
            f'[sandbox]\nimage = "{configured}"\npip_requirements = ["requirements.txt"]\n',
            encoding="utf-8",
        )
        planned = lifecycle.plan(
            lifecycle.ProjectImageScope(root),
            docker=FakeDocker({}),
            artifact_policy=lifecycle.ArtifactPolicy.VERIFIED_RELEASE_THEN_LOCAL,
        )
        fingerprints.append(planned.nodes[-1].logical_selection_fingerprint)

    assert fingerprints[0] != fingerprints[1]


@pytest.mark.parametrize(
    ("installed", "switched"),
    [
        (lifecycle.BASE_IMAGE, "booley-sandbox-riscv"),
        ("booley-sandbox-riscv", lifecycle.BASE_IMAGE),
    ],
)
def test_source_project_check_detects_a_flavor_switch(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, installed: str, switched: str
) -> None:
    root = _project(tmp_path, installed)
    docker = FakeDocker({})
    _wire(monkeypatch, docker)
    pin_image_inputs(monkeypatch)
    _install_planned_graph(
        docker, lifecycle.plan(lifecycle.ProjectImageScope(root), docker=docker).nodes
    )
    assert _check_project(root, docker).status is lifecycle.Status.CURRENT

    (root / ".booley" / "booley.toml").write_text(
        f'[sandbox]\nimage = "{switched}"\n', encoding="utf-8", newline="\n"
    )

    assert _check_project(root, docker).status is lifecycle.Status.STALE


@pytest.mark.parametrize(
    ("embedded", "requirements", "policy"),
    [
        (True, True, lifecycle.ArtifactPolicy.VERIFIED_RELEASE_THEN_LOCAL),
        (True, False, lifecycle.ArtifactPolicy.VERIFIED_RELEASE_ONLY),
        (False, True, lifecycle.ArtifactPolicy.LOCAL_ONLY),
    ],
)
def test_official_release_artifact_policy_follows_embedding_and_overlay(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    embedded: bool,
    requirements: bool,
    policy: lifecycle.ArtifactPolicy,
) -> None:
    root = _project(tmp_path)
    if requirements:
        (root / "requirements.txt").write_text("cocotb==2.0.1\n", encoding="utf-8")
        (root / ".booley" / "booley.toml").write_text(
            '[sandbox]\npip_requirements = ["requirements.txt"]\n',
            encoding="utf-8",
        )
    _wire(monkeypatch, FakeDocker({}))
    monkeypatch.setattr(
        booley,
        "version_attribution",
        SimpleNamespace(origin=VersionOrigin.DISTRIBUTION, version="0.2.6", source_root=None),
    )
    monkeypatch.setattr(harness_lifecycle, "embedded_official_release", lambda: embedded)

    planned = harness_lifecycle.plan(lifecycle.ProjectImageScope(root))

    assert planned.acquisition_policy is policy
    if policy is lifecycle.ArtifactPolicy.VERIFIED_RELEASE_THEN_LOCAL:
        assert [node.reference for node in planned.nodes] == [
            lifecycle.project_image.project_image_name(root) + "-release",
            lifecycle.project_image.project_image_name(root),
        ]
        assert [node.acquisition_policy for node in planned.nodes] == [
            lifecycle.ArtifactPolicy.VERIFIED_RELEASE_ONLY,
            lifecycle.ArtifactPolicy.LOCAL_ONLY,
        ]


def _labels(*, payload: str, recipe: str, parent: str | None = None) -> dict[str, str]:
    values = {
        lifecycle.LABEL_SCHEMA: lifecycle.PROVENANCE_SCHEMA,
        lifecycle.LABEL_PAYLOAD_FINGERPRINT: payload,
        lifecycle.LABEL_RECIPE_FINGERPRINT: recipe,
        lifecycle.LABEL_VERSION: "0.2.6",
        lifecycle.LABEL_BUILD_ORIGIN: "local",
    }
    if parent is not None:
        values[lifecycle.LABEL_PARENT_ARTIFACT] = parent
        values[lifecycle.LABEL_PARENT_ARTIFACT_KIND] = lifecycle.PARENT_ARTIFACT_LOCAL_IMAGE_ID
    return values


def _current_registry_base() -> tuple[str, dict[str, str]]:
    node = _host_base_node()
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


def test_host_reconcile_never_reads_project_configuration(monkeypatch):
    docker = FakeDocker({})
    _wire(monkeypatch, docker)
    # Private seam: host reconcile takes no Project root, so the only observable
    # proof that it reads no Project configuration is a tripwire on the reader.
    monkeypatch.setattr(
        lifecycle,
        "_selected_reference",
        lambda _root: (_ for _ in ()).throw(AssertionError("Project config read")),
    )

    result = lifecycle.reconcile(lifecycle.Intent.CHECK)

    assert result.selected_reference == lifecycle.BASE_IMAGE
    assert result.status is lifecycle.Status.STALE


@pytest.mark.parametrize("schema", ["1", None], ids=["schema-1", "no-schema"])
def test_host_base_without_current_provenance_schema_is_stale_and_rebuilt(
    monkeypatch: pytest.MonkeyPatch, schema: str | None
) -> None:
    """Pre-0.3.0 schema one and schema-less base images are no longer migrated."""
    labels = _local_build_labels(_host_base_node(), "sha256:" + "9" * 64)
    if schema is None:
        del labels[lifecycle.LABEL_SCHEMA]
        labels["booley.build-fingerprint"] = "payload-new"
    else:
        labels[lifecycle.LABEL_SCHEMA] = schema
    docker = FakeDocker({lifecycle.BASE_IMAGE: ("sha256:" + "a" * 64, labels)})
    builder = _wire(monkeypatch, docker)

    checked = lifecycle.reconcile(lifecycle.Intent.CHECK)

    assert checked.status is lifecycle.Status.STALE
    assert [item.code for item in checked.diagnostics] == ["stale"]
    assert lifecycle.BASE_IMAGE in checked.diagnostics[0].message
    assert not builder.built

    lifecycle.reconcile(lifecycle.Intent.ENSURE)

    assert builder.built == [lifecycle.BASE_IMAGE]


def test_host_ensure_removes_all_bootstrap_release_tags_when_base_is_current(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    image_id, labels = "sha256:" + "a" * 64, _published_release_labels()
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
    builder = _wire_official_release(monkeypatch, docker)

    result = lifecycle.reconcile(
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
    image_id, labels = "sha256:" + "a" * 64, _published_release_labels()
    prior_release = "ghcr.io/boldaxolotl/booley-sandbox:0.2.5"
    docker = FakeDocker(
        {
            lifecycle.BASE_IMAGE: (image_id, labels),
            prior_release: ("sha256:" + "b" * 64, {}),
            "ghcr.io/boldaxolotl/booley-sandbox:latest": (image_id, labels),
            "example.com/booley-sandbox:0.2.4": ("sha256:" + "c" * 64, {}),
        }
    )
    _wire_official_release(monkeypatch, docker)

    result = lifecycle.reconcile(
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

    result = lifecycle.reconcile(lifecycle.Intent.ENSURE)

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
        lifecycle.reconcile(lifecycle.Intent.ENSURE)

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
        lifecycle.reconcile(lifecycle.Intent.ENSURE)

    assert builder.built == [lifecycle.BASE_IMAGE]
    assert docker.image_id(lifecycle.BASE_IMAGE) is not None
    assert docker.image_id(release) is not None


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

    result = _check_project(root, docker)

    assert result.status is lifecycle.Status.STALE
    assert result.selected_id is None
    assert not builder.built
    assert not docker.mutations


def test_check_rejects_local_base_when_stable_contract_changed(tmp_path: Path, monkeypatch):
    root = _project(tmp_path)
    stable_id = "sha256:" + "9" * 64
    recipe = _host_base_node(schema="1").build.recipe_fingerprint
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

    with pytest.raises(lifecycle.ImageLifecycleError, match="booley bootstrap"):
        _check_project(root, docker)


def test_packaged_install_rejects_exact_local_parent_when_contract_is_unavailable(
    tmp_path: Path, monkeypatch
):
    import booley
    from booley.runtime.version_attribution import VersionAttribution, VersionOrigin

    root = _project(tmp_path)
    stable_id = "sha256:" + "9" * 64
    recipe = _host_base_node(schema="1").build.recipe_fingerprint
    docker = FakeDocker(
        {
            lifecycle.STABLE_RUNTIME_BASE_IMAGE: (stable_id, {}),
            lifecycle.BASE_IMAGE: (
                "sha256:" + "a" * 64,
                _labels(payload="payload-new", recipe=recipe, parent=stable_id),
            ),
        }
    )
    _wire(monkeypatch, docker, pin_contracts=False)
    monkeypatch.setattr(
        booley,
        "version_attribution",
        VersionAttribution(
            version="0.2.6",
            origin=VersionOrigin.DISTRIBUTION,
            distribution_name="booley-rtl",
        ),
    )

    with pytest.raises(
        lifecycle.InstalledImageContractError, match="compatibility metadata"
    ) as caught:
        _check_project(root, docker)

    assert "then run the command again" in str(caught.value)
    assert "session refresh" not in str(caught.value)


def _wire_distribution_pull(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    *,
    stamped: bool = True,
) -> tuple[FakeDocker, list[tuple[str, str]]]:
    import booley
    from booley.harness.setup import docker_image as init_docker_image
    from booley.runtime.version_attribution import VersionAttribution, VersionOrigin

    docker_dir = tmp_path / "site-packages" / "booley" / "data" / "docker"
    docker_dir.mkdir(parents=True)
    (docker_dir / "Dockerfile").write_text("FROM scratch\n", encoding="utf-8")
    (docker_dir / "Dockerfile.wheel").write_text("FROM booley-substrate\n", encoding="utf-8")
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
    if stamped:
        _wire_official_release(monkeypatch, docker)
    else:
        _wire(monkeypatch, docker)
    pulls: list[tuple[str, str]] = []

    def pull(version: str, image: str = lifecycle.BASE_IMAGE, *, adopt: bool = True) -> bool:
        assert adopt is False
        pulls.append((version, image))
        docker.images[init_docker_image.remote_tag(image, version)] = (
            "sha256:" + "a" * 64,
            _published_release_labels(version=version),
        )
        return True

    # Private seam: the registry pull is the network boundary.
    monkeypatch.setattr(init_docker_image, "_try_pull_image", pull)
    return docker, pulls


def test_source_policy_replaces_current_registry_base(monkeypatch: pytest.MonkeyPatch) -> None:
    docker = FakeDocker({lifecycle.BASE_IMAGE: _current_registry_base()})
    builder = _wire(monkeypatch, docker)

    lifecycle.reconcile(lifecycle.Intent.ENSURE)

    assert builder.built == [lifecycle.BASE_IMAGE]
    assert docker.images[lifecycle.BASE_IMAGE][1][lifecycle.LABEL_BUILD_ORIGIN] == "local"


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
    (docker_dir / "Dockerfile.wheel").write_text("FROM booley-substrate\n", encoding="utf-8")
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
    if origin == "distribution":
        _wire_official_release(monkeypatch, docker)

    result = harness_lifecycle.reconcile(lifecycle.Intent.CHECK)

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
    assert docker.images[lifecycle.BASE_IMAGE][1][lifecycle.LABEL_BUILD_ORIGIN] == "local"
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


def _install_stale_outer_base(docker: FakeDocker) -> None:
    labels = _local_build_labels(
        _host_base_node(),
        docker.image_id(lifecycle.STABLE_RUNTIME_BASE_IMAGE) or "",
    )
    labels[lifecycle.LABEL_PAYLOAD_FINGERPRINT] = "payload-old"
    docker.images[lifecycle.BASE_IMAGE] = ("sha256:" + "b" * 64, labels)


@pytest.mark.parametrize(
    ("intent", "expected"),
    [
        (lifecycle.Intent.ENSURE, False),
        (lifecycle.Intent.REFRESH, True),
    ],
)
def test_source_reconcile_separates_outer_replacement_from_runtime_base_refresh(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    intent: lifecycle.Intent,
    expected: bool,
) -> None:
    from booley.harness.setup import docker_image as init_docker_image

    _set_source_installation(tmp_path, monkeypatch)
    docker = FakeDocker({})
    _wire(monkeypatch, docker)
    _install_stale_outer_base(docker)
    plan = DockerBuildPlan(
        (
            DockerBuildRequest("base", lifecycle.BASE_IMAGE),
            DockerBuildRequest("proxy", "proxy", BuildEstimateClass.THIN_OVERLAY),
        )
    )
    calls: list[tuple[bool, bool, DockerBuildPlan]] = []

    def build_base(context, *, rebuild_runtime_base, capacity_plan, **_kwargs) -> None:
        calls.append((context.force, rebuild_runtime_base, capacity_plan))
        FakeBuilder(docker).build(
            _host_base_node(),
            force=True,
            source=lifecycle.ArtifactSource.LOCAL_BUILD,
        )
        context.record("docker_image", "ok", "built")

    # Private seam: the source build runs only through this setup step.
    monkeypatch.setattr(init_docker_image, "_step_docker_image", build_base)
    harness_lifecycle.reconcile(intent, capacity_plan=plan)

    assert calls == [(True, expected, plan)]


def _local_build_labels(node: lifecycle.ImageNode, parent_id: str) -> dict[str, str]:
    labels = dict(node.expected_labels)
    labels.update(
        {
            lifecycle.LABEL_BUILD_ORIGIN: "local",
            lifecycle.LABEL_PARENT_ARTIFACT_KIND: lifecycle.PARENT_ARTIFACT_LOCAL_IMAGE_ID,
            lifecycle.LABEL_PARENT_ARTIFACT: parent_id,
        }
    )
    return labels


def test_explicit_external_image_receives_zero_mutations(tmp_path: Path, monkeypatch):
    root = _project(tmp_path, "registry.example/team/custom:latest")
    docker = FakeDocker({"registry.example/team/custom:latest": ("sha256:external", {})})
    builder = _wire(monkeypatch, docker)

    def fail_prepare(*_args: object, **_kwargs: object) -> None:
        pytest.fail("an explicit external image must never be prepared")

    monkeypatch.setattr(harness_lifecycle, "prepare", fail_prepare)

    result = harness_lifecycle.reconcile_planned(
        lifecycle.ProjectImageScope(root), lifecycle.Intent.REFRESH
    )

    assert result.status is lifecycle.Status.EXTERNAL
    assert not builder.built
    assert not docker.mutations


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
    (root / ".booley" / "booley.toml").write_text(body, encoding="utf-8")
    docker = FakeDocker({})
    _wire(monkeypatch, docker)

    with pytest.raises(lifecycle.ImageLifecycleError, match=message):
        _check_project(root, docker)


def test_missing_configured_requirement_fails_loudly(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = _project(tmp_path)
    (root / ".booley" / "booley.toml").write_text(
        '[sandbox]\npip_requirements = ["missing.txt"]\n',
        encoding="utf-8",
    )
    docker = FakeDocker({})
    _wire(monkeypatch, docker)

    with pytest.raises(lifecycle.ImageLifecycleError, match=r"missing\.txt"):
        _check_project(root, docker)


def test_checkout_booley_dir_override_wins_over_literal_directory(tmp_path: Path, monkeypatch):
    root = tmp_path / "project"
    local = root / ".booley"
    custom = root / "control"
    local.mkdir(parents=True)
    custom.mkdir()
    (root / "booley.toml").write_text('[project]\ndir = "control"\n', encoding="utf-8")
    # Only the override directory is readable: the literal one cannot be parsed.
    (local / "booley.toml").write_text("[sandbox\n", encoding="utf-8")
    (custom / "booley.toml").write_text("[sandbox]\n", encoding="utf-8")
    docker = FakeDocker({})
    _wire(monkeypatch, docker)

    assert _check_project(root, docker).status is lifecycle.Status.STALE


def test_packaged_refresh_uses_pull_capable_builder(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from booley.harness.setup import docker_image as init_docker_image

    _docker, pulls = _wire_distribution_pull(tmp_path, monkeypatch)
    monkeypatch.setattr(harness_lifecycle, "embedded_official_release", lambda: True)
    # Private seam: a source build is the bug; it runs only through this setup step.
    monkeypatch.setattr(
        init_docker_image,
        "_step_docker_image",
        lambda *_args, **_kwargs: pytest.fail("packaged refresh attempted a source build"),
    )

    harness_lifecycle.reconcile(lifecycle.Intent.REFRESH)

    assert pulls == [("0.2.6", lifecycle.BASE_IMAGE)]


@pytest.fixture
def docker_cli(monkeypatch: pytest.MonkeyPatch) -> DockerImageCli:
    """The Docker CLI's image verbs, answered in process for the real adapter."""
    return serve_docker_image_cli(monkeypatch)


_ID = "sha256:" + "a" * 64
_RELEASE = "ghcr.io/boldaxolotl/booley-sandbox:0.2.5"
_NO_DOCKER = {"error": OSError("no docker")}


def _fails(message: str, code: int = 1) -> dict[str, object]:
    return {"stderr": message, "code": code}


_DAEMON = _fails("Cannot connect to the Docker daemon", 125)


@pytest.mark.parametrize(
    ("intent", "words", "reply", "outcome"),
    [
        (
            lifecycle.Intent.CHECK,
            ("image", "inspect"),
            _fails("Error: No such image: x"),
            "absent",
        ),
        (lifecycle.Intent.CHECK, ("image", "inspect"), _DAEMON, "Docker daemon"),
        (lifecycle.Intent.CHECK, ("image", "inspect"), _NO_DOCKER, "cannot inspect Docker image"),
        (lifecycle.Intent.ENSURE, ("image", "inspect", "--format"), _DAEMON, "Docker daemon"),
        (lifecycle.Intent.ENSURE, ("tag",), _fails("tag rejected"), "tag rejected"),
        (lifecycle.Intent.ENSURE, ("tag",), _NO_DOCKER, "retain image"),
    ],
)
def test_host_reconcile_distinguishes_absent_images_from_docker_failures(
    docker_cli, intent, words, reply, outcome
) -> None:
    if intent is lifecycle.Intent.ENSURE:
        docker_cli.images[lifecycle.BASE_IMAGE] = _ID
    docker_cli.answer(*words, **reply)
    if outcome == "absent":
        result = lifecycle.reconcile(intent)
        assert (result.status, result.selected_id) == (lifecycle.Status.STALE, None)
        return
    with pytest.raises(lifecycle.ImageLifecycleError, match=outcome):
        lifecycle.reconcile(intent, builder=object())  # a build would fail the test


def _plant_owned(tmp_path, monkeypatch) -> None:
    """Record the owned image and one that is already gone (cleanup skips it)."""
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    lifecycle.OwnedImages(tmp_path).record({}, (_ID, "sha256:" + "d" * 64))


@pytest.mark.parametrize(
    ("rows", "retained"),
    [
        ([("ghcr.io/boldaxolotl/booley-sandbox-base", "<none>", _ID)], False),
        ([("<none>", "latest", _ID)], False),
        ([("<none>", "<none>", _ID)], False),
        ([], True),  # a tag still names the image; CRLF line ends
    ],
    ids=["digest-only", "no-repository", "dangling", "tagged"],
)
def test_owned_image_cleanup_reads_the_docker_inventory(
    tmp_path, monkeypatch, docker_cli, rows, retained
) -> None:
    _plant_owned(tmp_path, monkeypatch)
    docker_cli.untagged_rows = [("<none>", "<none>", "sha256:" + "b" * 64), *rows]
    if retained:
        docker_cli.eol = "\r\n"
        docker_cli.images["booley-sandbox:latest"] = _ID

    result = lifecycle.cleanup_owned_images(tmp_path)

    # Only an untagged image goes: by exact ID, never forced, parents kept.
    assert result == ((f"{_ID}: retained (tag/keeper)",) if retained else ())
    removal = [] if retained else [["image", "rm", "--no-prune", _ID]]
    assert [call[1:] for call in docker_cli.removals()] == removal


@pytest.mark.parametrize(
    ("reply", "detail"),
    [(_fails("image is in use"), "image is in use"), (_NO_DOCKER, "no docker")],
)
def test_owned_image_cleanup_reports_a_refused_removal(
    tmp_path, monkeypatch, docker_cli, reply, detail
) -> None:
    _plant_owned(tmp_path, monkeypatch)
    docker_cli.untagged_rows = [("<none>", "<none>", _ID)]
    docker_cli.answer("image", "rm", **reply)

    assert lifecycle.cleanup_owned_images(tmp_path) == (
        f"{_ID}: deferred (could not remove retained tag {_ID}: {detail})",
    )


_ROW = image_row("booley-sandbox", "latest", _ID)
_OTHER_ROW = image_row("booley-sandbox", "latest", "sha256:" + "b" * 64)
_DANGLING = image_row("<none>", "<none>", "sha256:" + "c" * 64)
_CONFLICT = r"conflicting IDs for booley-sandbox:latest$"
_MALFORMED = r"malformed image inventory row 1:"
_INVENTORY_FAILURES = [
    (("--format",), {"stdout": "not-json\n"}, _MALFORMED),
    (("--format",), {"stdout": "{}\n"}, _MALFORMED),
    (("--format",), {"stdout": image_row("booley-sandbox", "latest", "short")}, _MALFORMED),
    (("--format",), {"stdout": image_row("<none>", "<none>", "short")}, _MALFORMED),
    (("--format",), {"stdout": '{"Repository":"<none>","Tag":"<none>"}\n'}, _MALFORMED),
    (("--format",), {"stdout": _ROW + _OTHER_ROW}, _CONFLICT),
    (("--format",), {"stdout": _DANGLING * 2 + _ROW + _DANGLING + _OTHER_ROW}, _CONFLICT),
    (("--format",), _fails("image inventory denied"), "image inventory denied"),
    (("--format",), _NO_DOCKER, "inventory Docker image references"),
    (("--quiet",), {"stdout": "short\n"}, "cannot inventory"),
    (("--quiet",), {"code": 1}, "cannot inventory"),
    (("--quiet",), {"error": subprocess.TimeoutExpired("docker", 30)}, "cannot inventory"),
]


@pytest.mark.parametrize(("words", "reply", "message"), _INVENTORY_FAILURES)
def test_owned_image_cleanup_refuses_incomplete_docker_inventory(
    tmp_path,
    monkeypatch,
    docker_cli,
    words,
    reply,
    message,
) -> None:
    _plant_owned(tmp_path, monkeypatch)
    docker_cli.untagged_rows = [("<none>", "<none>", _ID)]
    docker_cli.answer("image", "ls", *words, **reply)

    with pytest.raises(lifecycle.ImageLifecycleError, match=message):
        lifecycle.cleanup_owned_images(tmp_path)

    assert docker_cli.removals() == []


@pytest.mark.parametrize(
    ("digest_only_alias", "failure"),
    [
        (False, None),
        (True, None),
        (False, (("container", "ls"), _NO_DOCKER, "inventory Docker containers")),
        (False, (("container", "ls"), _fails("denied"), "denied")),
        (False, (("container", "inspect"), {"error": OSError("no inspect")}, "no inspect")),
        (False, (("container", "inspect"), _fails("disappeared"), "disappeared")),
    ],
)
def test_host_check_reads_the_container_inventory(docker_cli, digest_only_alias, failure) -> None:
    docker_cli.images[_RELEASE] = _ID
    docker_cli.containers = {"one": "sha256:" + "b" * 64, "two": _ID}
    docker_cli.stopped = {"two"}
    docker_cli.eol = "\r\n"
    if digest_only_alias:
        docker_cli.untagged_rows = [("example.com/mirror", "<none>", _ID)]
    if failure:
        docker_cli.answer(*failure[0], **failure[1])
        with pytest.raises(lifecycle.ImageLifecycleError, match=failure[2]):
            lifecycle.reconcile(lifecycle.Intent.CHECK)
        return

    result = lifecycle.reconcile(lifecycle.Intent.CHECK)

    assert result.cleanup.retained_required == (_RELEASE,)
    assert result.cleanup.pending == ()


_DIGEST = "ghcr.io/boldaxolotl/booley-sandbox@sha256:" + "A" * 64


@pytest.mark.parametrize(
    ("reply", "expected"),
    [
        ({"stdout": f'["{_DIGEST}"]\n'}, (_DIGEST.lower(),)),
        ({"stdout": "null\n"}, ()),
        ({"stdout": "[]\n"}, ()),
        (_fails("No such image: booley-sandbox"), ()),
        ({"stdout": '["not-a-digest"]\n'}, "malformed RepoDigests"),
        ({"stdout": "not-json\n"}, "invalid RepoDigests"),
        ({"stdout": "{}\n"}, "invalid RepoDigests"),
        ({"stdout": '["valid", 42]\n'}, "invalid RepoDigests"),
        (_NO_DOCKER, "inspect Docker RepoDigests"),
        (_DAEMON, "Docker daemon"),
    ],
)
def test_docker_repo_digests_classify_inspect_output(docker_cli, reply, expected) -> None:
    docker_cli.answer("image", "inspect", "-f", **reply)
    # Private seam: only a Project graph node with a registry-digest parent reads
    # RepoDigests, and no public entry builds that graph without staging a
    # published release chain.
    adapter = lifecycle._DockerCli()

    if isinstance(expected, tuple):
        assert adapter.repo_digests("booley-sandbox") == expected
    else:
        with pytest.raises(lifecycle.ImageLifecycleError, match=expected):
            adapter.repo_digests("booley-sandbox")


def test_docker_tag_removal_is_exact_non_forced_and_does_not_prune_parents(docker_cli) -> None:
    docker_cli.images.update({_RELEASE: _ID, "booley-sandbox:latest": _ID})
    # Private seam: a tag goes by name only after an ENSURE that needs a whole labelled chain.
    lifecycle._DockerCli().remove_tag(_RELEASE)
    assert docker_cli.removals() == [["docker", "image", "rm", "--no-prune", _RELEASE]]
    assert docker_cli.images == {"booley-sandbox:latest": _ID}


def _planned_kept_recipe(
    tmp_path, monkeypatch, recipe: str
) -> tuple[Path, FakeDocker, lifecycle.LifecyclePlan]:
    """Plan a Project whose kept user recipe builds the overlay, then install the planned graph."""
    root = _project(tmp_path)
    docker = FakeDocker({})
    _wire(monkeypatch, docker)
    docker_dir = root / ".booley" / "docker"
    docker_dir.mkdir()
    (docker_dir / "Dockerfile").write_text(recipe, encoding="utf-8")
    (docker_dir / "setup.sh").write_text("echo one\n", encoding="utf-8")
    planned = lifecycle.plan(lifecycle.ProjectImageScope(root), docker=docker)
    assert planned.nodes[-1].role is lifecycle.ImageRole.PROJECT_OVERLAY
    _install_planned_graph(docker, planned.nodes)
    docker.images[planned.nodes[-1].reference][1][lifecycle.LABEL_WHEEL_SHA256] = "f" * 64
    return root, docker, planned


_KEPT_RECIPE = "# booley:keep\nFROM booley-sandbox\nCOPY setup.sh /setup.sh\n"


def test_project_recipe_fingerprint_includes_arbitrary_context_files(tmp_path, monkeypatch):
    root, docker, _ = _planned_kept_recipe(tmp_path, monkeypatch, _KEPT_RECIPE)
    scope = lifecycle.ProjectImageScope(root)
    assert lifecycle.plan(scope, docker=docker).steps[-1].action is lifecycle.PlanAction.REUSE

    (root / ".booley" / "docker" / "setup.sh").write_text("echo two\n", encoding="utf-8")

    assert lifecycle.plan(scope, docker=docker).steps[-1].action is lifecycle.PlanAction.BUILD


_WHEEL_ROLE = {lifecycle.LABEL_ARTIFACT_ROLE: "wheel-overlay"}


@pytest.mark.parametrize(
    ("labels", "code"),
    [
        ({lifecycle.LABEL_BUILD_ORIGIN: "registry"}, "wrong-origin"),
        (_WHEEL_ROLE, "topology-changed"),
        ({**_WHEEL_ROLE, lifecycle.LABEL_SCHEMA: "legacy"}, "legacy-migration"),
        ({**_WHEEL_ROLE, lifecycle.LABEL_SCHEMA: "1"}, "legacy-migration"),
        ({**_WHEEL_ROLE, lifecycle.LABEL_SCHEMA: None}, "legacy-migration"),
    ],
)
def test_plan_reports_why_the_selected_image_is_stale(tmp_path, monkeypatch, labels, code):
    root, docker, planned = _planned_kept_recipe(tmp_path, monkeypatch, _KEPT_RECIPE)
    terminal = docker.images[planned.nodes[-1].reference][1]
    terminal.update({name: value for name, value in labels.items() if value is not None})
    if None in labels.values():
        del terminal[lifecycle.LABEL_SCHEMA]

    stale = lifecycle.plan(lifecycle.ProjectImageScope(root), docker=docker)

    assert stale.steps[-1].reason.code == code


def test_missing_parent_artifact_is_a_parent_change(tmp_path):
    node = replace(
        _incremental_node(tmp_path, lifecycle.ImageRole.STANDARD_SUBSTRATE),
        parent="missing-parent",
    )
    docker = FakeDocker({node.reference: ("sha256:" + "a" * 64, dict(node.expected_labels))})

    # Private seam: `plan` marks a node stale when its parent step is, so a parent that
    # vanishes after the parent step reused it is reachable only here.
    reason = lifecycle._node_ancestry_reason(node, docker.inspect(node.reference), docker)

    assert reason is not None and reason.code == "parent-changed"


@pytest.mark.parametrize(
    ("source", "seam", "message"),
    [
        (lifecycle.ArtifactSource.VERIFIED_RELEASE_PULL, "_try_pull_image", "could not pull"),
        (lifecycle.ArtifactSource.LOCAL_BUILD, "_step_docker_image", "base build failed"),
    ],
)
def test_legacy_builder_reports_acquisition_failure(
    tmp_path, monkeypatch, source, seam, message
) -> None:
    from booley.harness.setup import docker_image as init_docker_image

    def fail_step(context, **_kwargs) -> None:
        context.results.append(SimpleNamespace(status="err", detail="base build failed"))

    recipe = tmp_path / "Dockerfile"
    recipe.write_text("FROM scratch\n", encoding="utf-8")
    monkeypatch.setattr(harness_lifecycle, "docker_data_dir", lambda: tmp_path)
    # Private seam: the public host ENSURE reaches this builder only through a real
    # `docker build` or registry pull, the two boundaries these stand in for.
    stand_in = fail_step if seam == "_step_docker_image" else lambda *_a, **_k: False
    monkeypatch.setattr(init_docker_image, seam, stand_in)
    payload = lifecycle.PayloadProvenance("1", "0.2.6", "payload")
    node = lifecycle.ImageNode(
        lifecycle.BASE_IMAGE, recipe, payload, lifecycle.BuildProvenance("recipe", None)
    )

    with pytest.raises(lifecycle.ImageLifecycleError, match=message):
        harness_lifecycle._LegacyBuildAdapter(_project(tmp_path), verbose=False).build(
            node, force=True, source=source
        )


def test_failed_backup_creation_cleans_earlier_backup(tmp_path) -> None:
    class BackupFailureDocker(FakeDocker):
        def tag(self, source: str, target: str) -> None:
            if source == "sha256:second":
                raise lifecycle.ImageLifecycleError("second backup failed")
            super().tag(source, target)

    docker = BackupFailureDocker({"first": ("sha256:first", {}), "second": ("sha256:second", {})})
    base = _incremental_node(tmp_path, lifecycle.ImageRole.STANDARD_SUBSTRATE)
    nodes = tuple(replace(base, reference=name) for name in ("first", "second"))

    # Private seam: the host reconcile hands the helper one node, so the cleanup of
    # earlier backups when a later one fails is reachable only by calling it.
    with pytest.raises(lifecycle.ImageLifecycleError, match="second backup failed"):
        lifecycle._retain_prior_tags(tmp_path, nodes, docker)

    assert [mutation[0] for mutation in docker.mutations] == ["tag", "remove_tag"]


def test_mutation_retries_unstamped_build_then_fails(monkeypatch) -> None:
    class NoOpBuilder:
        def __init__(self) -> None:
            self.calls = 0

        def build(self, _node, *, force: bool, source: lifecycle.ArtifactSource) -> None:
            del force, source
            self.calls += 1

    docker = FakeDocker({})
    _wire(monkeypatch, docker)
    builder = NoOpBuilder()

    with pytest.raises(lifecycle.ImageLifecycleError, match="expected provenance"):
        lifecycle.reconcile(lifecycle.Intent.ENSURE, docker=docker, builder=builder)

    assert builder.calls == 2


def test_mutation_reports_failed_rollback_with_recovery_tag(
    monkeypatch,
) -> None:
    class RestoreFailureDocker(FakeDocker):
        def tag(self, source: str, target: str) -> None:
            if source.startswith("booley-lifecycle-backup-"):
                raise lifecycle.ImageLifecycleError("restore rejected")
            super().tag(source, target)

    docker = RestoreFailureDocker(
        {lifecycle.BASE_IMAGE: ("sha256:old", {lifecycle.LABEL_SCHEMA: "stale"})}
    )
    _wire(monkeypatch, docker)

    with pytest.raises(lifecycle.ImageLifecycleError, match="recovery names"):
        lifecycle.reconcile(
            lifecycle.Intent.REFRESH, docker=docker, builder=FailingBuilder(docker)
        )


def test_failed_mutation_untags_base_without_prior_image(monkeypatch) -> None:
    docker = FakeDocker({})
    _wire(monkeypatch, docker)

    with pytest.raises(lifecycle.ImageLifecycleError, match="build failed"):
        lifecycle.reconcile(lifecycle.Intent.ENSURE, docker=docker, builder=FailingBuilder(docker))

    assert docker.image_id(lifecycle.BASE_IMAGE) is None


def _unexpected_call(*_args, **_kwargs):
    raise AssertionError("adoption must reuse the prepared result")


@pytest.fixture
def external_layout(tmp_path, monkeypatch):
    """An alias-capable Project whose switchable Booley Directory is external."""
    dirs = {"external": tmp_path / "external", "other": tmp_path / "other"}
    root = _project(tmp_path)
    dirs["internal"] = root / "inner"
    for directory in dirs.values():
        directory.mkdir()
    layout = SimpleNamespace(
        root=root, docker=FakeDocker({}), dirs=dirs, directory=dirs["external"]
    )
    monkeypatch.setattr(
        "booley.runtime.booley_dir.resolve_booley_dir", lambda _r: layout.directory
    )
    _wire(monkeypatch, layout.docker)
    monkeypatch.setattr(lifecycle.project_image, "project_data_alias_capable", lambda _image: True)
    return layout


def test_external_layout_late_parent_graph_preserves_terminal_identity_and_reuses(
    external_layout, monkeypatch
):
    root, docker = external_layout.root, external_layout.docker
    initial = lifecycle.plan(lifecycle.ProjectImageScope(root), docker=docker)
    assert initial.nodes[-1].role is lifecycle.ImageRole.WHEEL_OVERLAY
    builder = TransactionBuilder(docker)
    prepared = lifecycle.prepare(initial, docker=docker, builder=builder)
    terminal = prepared.plan.nodes[-1]
    assert terminal.role is lifecycle.ImageRole.PROJECT_DATA_LAYOUT
    assert terminal.parent == initial.nodes[-1].reference
    assert terminal.payload == initial.nodes[-1].payload
    assert terminal.wheel_source_fingerprint == "wheel"
    assert terminal.acquisition_policy is lifecycle.ArtifactPolicy.LOCAL_ONLY
    assert terminal.reference != lifecycle.BASE_IMAGE
    assert builder.built[-1][1] == prepared.candidates[-2].image_id
    assert prepared.candidates[-1].wheel_sha256 == prepared.candidates[-2].wheel_sha256
    with monkeypatch.context() as late:  # adoption reuses the prepared result, not a new plan
        late.setattr(lifecycle.project_image, "project_data_alias_capable", _unexpected_call)
        late.setattr(lifecycle, "plan", _unexpected_call)
        lifecycle.validate(prepared, docker=docker)
        committed = lifecycle.commit(prepared, docker=docker)
    assert committed.selected_reference == terminal.reference
    assert committed.wheel_sha256 == "e" * 64
    repeated = lifecycle.plan(lifecycle.ProjectImageScope(root), docker=docker)
    assert repeated.selected_reference == terminal.reference
    assert all(step.action is lifecycle.PlanAction.REUSE for step in repeated.steps)
    second = TransactionBuilder(docker)
    replay = lifecycle.prepare(repeated, docker=docker, builder=second)
    lifecycle.validate(replay, docker=docker)
    assert second.built == []
    assert replay.candidates[-1].image_id == committed.selected_id


def test_layout_preparation_reuses_planned_topology(external_layout):
    docker = external_layout.docker
    planned = lifecycle.plan(lifecycle.ProjectImageScope(external_layout.root), docker=docker)
    external_layout.directory = external_layout.dirs["other"]
    prepared = lifecycle.prepare(planned, docker=docker, builder=TransactionBuilder(docker))
    with pytest.raises(lifecycle.ImageLifecycleError, match="Booley Directory topology"):
        lifecycle.validate(prepared, docker=docker)
    parent_id = prepared.candidates[-2].image_id
    assert prepared.plan.nodes[-1].reference == planned.layout_topology.identity(parent_id)[0]


_ADOPTION_CHANGES = [
    pytest.param(change, alias, id=change + ("" if alias else "-no-alias"))
    for change in ("to-external", "to-internal", "directory", "recipe")
    for alias in (True, False)
] + [pytest.param(change, True, id=change) for change in ("parent-rebound", "candidate-rebound")]


_SWITCHED_DIRECTORY = {"to-external": "external", "to-internal": "internal", "directory": "other"}


@pytest.mark.parametrize(("change", "alias"), _ADOPTION_CHANGES)
def test_layout_adoption_rejects_changed_mutable_inputs(
    external_layout, monkeypatch, tmp_path, change, alias
):
    docker, dirs = external_layout.docker, external_layout.dirs
    monkeypatch.setattr(lifecycle.project_image, "project_data_alias_capable", lambda _i: alias)
    external_layout.directory = dirs["internal" if change == "to-external" else "external"]
    planned = lifecycle.plan(lifecycle.ProjectImageScope(external_layout.root), docker=docker)
    prepared = lifecycle.prepare(planned, docker=docker, builder=TransactionBuilder(docker))
    managed = {n.reference: docker.image_id(n.reference) for n in prepared.plan.nodes}
    rebound = None
    if change in ("parent-rebound", "candidate-rebound"):
        rebound = prepared.candidates[-2 if change == "parent-rebound" else -1].candidate_reference
        docker.images[rebound] = ("sha256:rebound", docker.images[rebound][1])
    elif change == "recipe":
        (tmp_path / "recipe").write_text("different recipe")
        monkeypatch.setattr(project_image, "layout_recipe", lambda: tmp_path / "recipe")
    else:
        external_layout.directory = dirs[_SWITCHED_DIRECTORY[change]]
    for adopt in (lifecycle.validate, lifecycle.commit):
        with pytest.raises(lifecycle.ImageLifecycleError):
            adopt(prepared, docker=docker)
    assert {ref: docker.image_id(ref) for ref in managed} == managed
    if rebound:
        assert docker.image_id(rebound) == "sha256:rebound"  # abort preserves foreign rebinding
    else:
        assert not any(ref.startswith("booley-lifecycle-") for ref in docker.images)


def test_layout_identity_changes_for_parent_topology_recipe_and_project(tmp_path, monkeypatch):
    root = _project(tmp_path)
    selected = {"dir": tmp_path / "external"}
    selected["dir"].mkdir()
    monkeypatch.setattr("booley.runtime.booley_dir.resolve_booley_dir", lambda _r: selected["dir"])
    parent = "sha256:" + "a" * 64

    def identity(project=root, parent_id=parent):
        return project_image.layout_topology(project).identity(parent_id)

    first = identity()
    assert identity(parent_id="sha256:" + "b" * 64) != first
    assert identity(tmp_path / "other-project") != first
    other_dir = tmp_path / "different"
    other_dir.mkdir()
    selected["dir"] = other_dir
    assert identity() != first
    selected["dir"] = tmp_path / "external"
    recipe = tmp_path / "recipe"
    recipe.write_text("different recipe")
    monkeypatch.setattr(project_image, "layout_recipe", lambda: recipe)
    assert identity() != first
    inner = root / "inner"
    inner.mkdir()
    (tmp_path / "alias").symlink_to(inner)
    for internal in (inner, tmp_path / "alias"):
        selected["dir"] = internal
        assert project_image.layout_topology(root) is None


def test_release_layout_child_retains_verified_parent_policy_and_zero_build_check(
    external_layout, monkeypatch
):
    root, docker = external_layout.root, external_layout.docker
    pin_image_inputs(monkeypatch, contracts=lifecycle.ImageBuildContracts("a" * 64, "b" * 64))
    policy = lifecycle.ArtifactPolicy.VERIFIED_RELEASE_ONLY
    parent = lifecycle.plan(
        lifecycle.ProjectImageScope(root), docker=docker, artifact_policy=policy
    ).nodes[0]
    _stage_registry_candidate(
        docker, parent, parent.reference, label_overrides={lifecycle.LABEL_WHEEL_SHA256: "f" * 64}
    )
    planned = lifecycle.plan(
        lifecycle.ProjectImageScope(root), docker=docker, artifact_policy=policy
    )
    assert planned.nodes[0].acquisition_policy is policy
    assert planned.nodes[-1].acquisition_policy is lifecycle.ArtifactPolicy.LOCAL_ONLY
    prepared = lifecycle.prepare(planned, docker=docker, builder=TransactionBuilder(docker))
    assert prepared.plan.acquisition_policy is policy
    lifecycle.validate(prepared, docker=docker)
    committed = lifecycle.commit(prepared, docker=docker)
    before = list(docker.mutations)
    checked = _check_project(root, docker, artifact_policy=policy)
    assert checked.status is lifecycle.Status.CURRENT
    assert checked.selected_reference == committed.selected_reference
    assert checked.wheel_sha256 == "f" * 64
    assert docker.mutations == before
    docker.images.pop(checked.selected_reference)
    missing = _check_project(root, docker, artifact_policy=policy)
    assert missing.status is lifecycle.Status.STALE
    assert docker.mutations == before


def test_obsolete_layout_cleanup_is_project_scoped_and_retains_running_artifacts(tmp_path):
    roots = [tmp_path / "one" / "same-name", tmp_path / "two" / "same-name"]
    prefix = lifecycle.project_image.project_layout_prefix(roots[0])
    other_prefix = lifecycle.project_image.project_layout_prefix(roots[1])
    assert prefix != other_prefix
    selected, obsolete, active, foreign = (
        prefix + "1" * 16,
        prefix + "2" * 16 + ":latest",  # Docker reports the implicit tag
        prefix + "3" * 16,
        other_prefix + "2" * 16,
    )

    def record(identity, suffix):
        role = lifecycle.ImageRole.PROJECT_DATA_LAYOUT.value
        labels = {lifecycle.LABEL_SCHEMA: lifecycle.PROVENANCE_SCHEMA}
        labels |= {
            lifecycle.LABEL_ARTIFACT_ROLE: role,
            lifecycle.LABEL_EFFECTIVE_INPUTS: suffix * 64,
        }
        return identity, labels

    docker = FakeDocker(
        {
            selected: record("selected-id", "1"),
            obsolete: record("obsolete-id", "2"),
            active: record("active-id", "3"),
            foreign: record("foreign-id", "2"),
        }
    )
    docker.used_image_ids = frozenset({"active-id"})
    # Private seam: `commit` projects this cleanup only with Intent.ENSURE, so the CHECK
    # projection (also called by tests/docker/test_worktree_mount_paths_e2e.py) is
    # reachable only directly.
    checked = lifecycle._reconcile_layout_tag_cleanup(
        roots[0], selected, lifecycle.Intent.CHECK, docker
    )
    assert checked.pending == (obsolete,)
    assert checked.retained_required == (active,)
    assert docker.mutations == []
    cleaned = lifecycle._reconcile_layout_tag_cleanup(
        roots[0], selected, lifecycle.Intent.ENSURE, docker
    )
    assert cleaned.removed == (obsolete,)
    assert docker.image_id(selected) == "selected-id"
    assert docker.image_id(active) == "active-id"
    assert docker.image_id(foreign) == "foreign-id"


@pytest.mark.parametrize(
    ("user", "expected"),
    [
        ("", "root"),
        ("root", "root"),
        ("agent", "agent"),
        ("1000:1000", "1000:1000"),
        ("agent\nUSER root", None),
        ("$(id)", None),
        ("agent root", None),
        (None, None),
    ],
)
def test_layout_parent_user_keeps_supported_identity_and_refuses_unsafe_ones(user, expected):
    refused = pytest.raises(RuntimeError, match="unsupported User")
    with refused if expected is None else nullcontext():
        assert lifecycle.project_image.layout_image_user({"Config": {"User": user}}) == expected


def test_layout_config_retains_default_user_and_rejects_payload_substitution(docker_cli):
    import copy

    parent = {
        "Config": {
            "User": "",
            "WorkingDir": "/work",
            "Env": ["A=B"],
            "Labels": {"payload": "original"},
        },
        "RootFS": {"Layers": ["parent"]},
        "Architecture": "amd64",
        "Os": "linux",
        "Id": "parent",
    }
    candidate = copy.deepcopy(parent)
    candidate["Config"]["User"] = "root"
    candidate["RootFS"]["Layers"].append("layout")
    candidate["Id"] = "candidate"
    from tests.runtime.test_image_identity import _layout_history

    docker_cli.answer("run")
    for name, digit in (("parent", "a"), ("candidate", "b")):
        docker_cli.answer("history", name, stdout="\n".join(_layout_history(digit * 64, "docker")))
    lifecycle.project_image.verify_layout_image(parent, candidate)
    candidate["Config"]["Labels"]["payload"] = "substituted"
    with pytest.raises(RuntimeError, match="payload labels"):
        lifecycle.project_image.verify_layout_image(parent, candidate)
    candidate["Config"]["Labels"]["payload"] = "original"
    candidate["Config"]["WorkingDir"] = "/other"
    with pytest.raises(RuntimeError, match="runtime configuration"):
        lifecycle.project_image.verify_layout_image(parent, candidate)
    candidate["Config"]["WorkingDir"] = "/work"
    candidate["RootFS"]["Layers"] = ["foreign", "layout"]
    with pytest.raises(RuntimeError, match="substituted parent"):
        lifecycle.project_image.verify_layout_image(parent, candidate)


def test_layout_probe_failure_is_not_cached_or_treated_as_directory(docker_cli):
    image_id = "sha256:" + "1" * 64
    _ACTUAL_ALIAS_PROBE.cache_clear()
    docker_cli.answer("run", error=subprocess.TimeoutExpired("docker", 30))
    with pytest.raises(lifecycle.project_image.DockerImageError, match="cannot probe"):
        _ACTUAL_ALIAS_PROBE(image_id)
    docker_cli.answer("run", stdout="canonical-alias\n")
    assert _ACTUAL_ALIAS_PROBE(image_id)
    assert _ACTUAL_ALIAS_PROBE(image_id)
    assert len([call for call in docker_cli.calls if call[1] == "run"]) == 2
    for reply in (_fails("invalid alias"), {"stdout": "unknown"}):
        docker_cli.answer("run", **reply)
        with pytest.raises(lifecycle.project_image.DockerImageError, match="cannot prove"):
            _ACTUAL_ALIAS_PROBE("sha256:" + "2" * 64)
        _ACTUAL_ALIAS_PROBE.cache_clear()


def test_release_project_overlay_retains_wheel_identity_through_layout_convergence(
    external_layout, monkeypatch
):
    root, docker = external_layout.root, external_layout.docker
    (root / "requirements.txt").write_text("cocotb\n")
    (root / ".booley/booley.toml").write_text(
        '[sandbox]\npip_requirements = ["requirements.txt"]\n'
    )
    pin_image_inputs(monkeypatch, contracts=lifecycle.ImageBuildContracts("a" * 64, "b" * 64))
    policy = lifecycle.ArtifactPolicy.VERIFIED_RELEASE_THEN_LOCAL
    parent = lifecycle.plan(
        lifecycle.ProjectImageScope(root), docker=docker, artifact_policy=policy
    ).nodes[0]
    _stage_registry_candidate(
        docker, parent, parent.reference, label_overrides={lifecycle.LABEL_WHEEL_SHA256: "f" * 64}
    )
    planned = lifecycle.plan(
        lifecycle.ProjectImageScope(root), docker=docker, artifact_policy=policy
    )
    assert planned.nodes[-1].role is lifecycle.ImageRole.PROJECT_OVERLAY
    assert planned.nodes[-1].wheel_source_fingerprint == "wheel"
    prepared = lifecycle.prepare(planned, docker=docker, builder=TransactionBuilder(docker))
    assert prepared.candidates[-1].wheel_sha256 == "f" * 64
    lifecycle.validate(prepared, docker=docker)
    committed = lifecycle.commit(prepared, docker=docker)
    before = list(docker.mutations)
    checked = _check_project(root, docker, artifact_policy=policy)
    assert checked.status is lifecycle.Status.CURRENT
    assert checked.selected_id == committed.selected_id
    assert docker.mutations == before


def test_prepared_parent_losing_alias_can_validate_and_commit_without_obsolete_layout(
    external_layout, monkeypatch
):
    root, docker = external_layout.root, external_layout.docker
    pin_image_inputs(monkeypatch, wheel="old-wheel")
    original = lifecycle.plan(lifecycle.ProjectImageScope(root), docker=docker)
    _install_planned_graph(docker, original.nodes)
    old_parent_id = docker.image_id(lifecycle.project_image.project_image_name(root))
    monkeypatch.setattr(
        lifecycle.project_image, "project_data_alias_capable", lambda image: image == old_parent_id
    )
    pin_image_inputs(monkeypatch, wheel="new-wheel")
    planned = lifecycle.plan(lifecycle.ProjectImageScope(root), docker=docker)
    assert planned.nodes[-1].role is lifecycle.ImageRole.PROJECT_DATA_LAYOUT
    prepared = lifecycle.prepare(planned, docker=docker, builder=TransactionBuilder(docker))
    assert prepared.plan.nodes[-1].role is lifecycle.ImageRole.WHEEL_OVERLAY
    lifecycle.validate(prepared, docker=docker)
    assert lifecycle.commit(
        prepared, docker=docker
    ).selected_reference == lifecycle.project_image.project_image_name(root)


def test_plan_rebuilds_stale_managed_image_without_booley_mount_point(
    external_layout, monkeypatch, docker_cli
):
    """A stale managed tag built for an older Booley has no mount point; it is rebuilt."""
    root, docker = external_layout.root, external_layout.docker
    pin_image_inputs(monkeypatch, wheel="old-wheel")
    original = lifecycle.plan(lifecycle.ProjectImageScope(root), docker=docker)
    _install_planned_graph(docker, original.nodes)
    pin_image_inputs(monkeypatch, wheel="new-wheel")
    _ACTUAL_ALIAS_PROBE.cache_clear()
    monkeypatch.setattr(lifecycle.project_image, "project_data_alias_capable", _ACTUAL_ALIAS_PROBE)
    docker_cli.answer("run", stdout="absent\n")

    planned = lifecycle.plan(lifecycle.ProjectImageScope(root), docker=docker)
    _ACTUAL_ALIAS_PROBE.cache_clear()

    assert planned.steps[-1].action is lifecycle.PlanAction.BUILD
    assert planned.nodes[-1].role is lifecycle.ImageRole.WHEEL_OVERLAY


class InventoryFailureDocker(FakeDocker):
    """FakeDocker whose reference inventory fails after ``reads_left`` more reads."""

    reads_left: int | None = None

    def image_references(self) -> tuple[lifecycle.ImageReference, ...]:
        if self.reads_left == 0:
            raise lifecycle.ImageLifecycleError("inventory temporarily unavailable")
        if self.reads_left is not None:
            self.reads_left -= 1
        return super().image_references()


def test_cleanup_failure_after_adoption_is_diagnostic_and_abort_is_idempotent(
    tmp_path, monkeypatch
):
    root = _project(tmp_path)
    docker = InventoryFailureDocker({})
    _wire(monkeypatch, docker)
    prepared = lifecycle.prepare(
        lifecycle.plan(lifecycle.ProjectImageScope(root), docker=docker),
        docker=docker,
        builder=TransactionBuilder(docker),
    )
    # Planning reads the inventory, and so does the abort inside commit; the layout
    # cleanup after it is the read that fails.
    docker.reads_left = 1
    committed = lifecycle.commit(prepared, docker=docker)
    assert docker.image_id(committed.selected_reference) == committed.selected_id
    assert committed.changed_images
    assert committed.diagnostics[-1].code == "cleanup-deferred"
    docker.reads_left = None
    before = list(docker.mutations)
    lifecycle.abort(prepared, docker=docker)
    lifecycle.abort(prepared, docker=docker)
    assert docker.mutations == before


def test_real_project_overlay_build_labels_inherit_prepared_release_wheel(tmp_path, monkeypatch):
    # Private seam: the build adapter's role build runs only inside a real `docker build`.
    from booley.harness.setup import docker_image

    parent_id = "sha256:" + "a" * 64
    node = _incremental_node(tmp_path, lifecycle.ImageRole.PROJECT_OVERLAY, source="wheel")
    docker = FakeDocker(
        {
            "release": (
                parent_id,
                {
                    lifecycle.LABEL_WHEEL_SOURCE_FINGERPRINT: node.wheel_source_fingerprint,
                    lifecycle.LABEL_WHEEL_SHA256: "f" * 64,
                },
            )
        }
    )
    adapter = harness_lifecycle._IncrementalBuildAdapter(tmp_path, docker, verbose=False)
    captured = []
    monkeypatch.setattr(adapter, "_role_build_inputs", lambda *_args: (tmp_path, (), ()))
    monkeypatch.setattr(
        docker_image, "_docker_build_image", lambda _ctx, spec: captured.append(spec) or 0
    )
    adapter._build_role(
        SimpleNamespace(record=lambda *_args: None), node, "candidate", "release", parent_id
    )
    assert adapter._wheel_sha256 is None
    assert dict(captured[0].labels)[lifecycle.LABEL_WHEEL_SHA256] == "f" * 64
    docker.images["release"][1][lifecycle.LABEL_WHEEL_SOURCE_FINGERPRINT] = "substituted"
    with pytest.raises(lifecycle.ImageLifecycleError, match="matching wheel identity"):
        adapter._build_role(SimpleNamespace(), node, "candidate", "release", parent_id)


@pytest.mark.parametrize("shape", ["layout-parent", "layout"])
def test_layout_candidate_tags_are_not_recorded_or_discarded(tmp_path, monkeypatch, shape):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    root, docker = _project(tmp_path), FakeDocker({})
    _wire(monkeypatch, docker)
    planned = lifecycle.plan(lifecycle.ProjectImageScope(root), docker=docker)
    image = "sha256:" + "a" * 64
    live = f"booley-lifecycle-{'a' * 32}-{shape}:candidate"
    recorded = "booley-lifecycle-recorded:candidate"
    docker.images.update({live: (image, {}), recorded: (image, {})})
    lifecycle.OwnedImages(root).record({recorded: image})

    lifecycle.prepare(planned, docker=docker, builder=TransactionBuilder(docker))

    assert docker.image_id(live) == image
    assert docker.image_id(recorded) is None
    assert ("remove_tag", live) not in docker.mutations


def test_init_reports_layout_probe_failure_without_traceback(tmp_path, monkeypatch, docker_cli):
    from booley.harness import init_cmd
    from booley.harness.setup.common import InitContext

    _ACTUAL_ALIAS_PROBE.cache_clear()
    monkeypatch.setattr(lifecycle.project_image, "project_data_alias_capable", _ACTUAL_ALIAS_PROBE)

    docker_cli.answer("run", error=subprocess.TimeoutExpired("docker", 30))

    # Private seam: init's other steps need a whole Project, so the image step runs
    # against this stand-in for `reconcile_planned` that makes the probe fail.
    def reconcile(*_args, **_kwargs):
        return lifecycle._layout_alias_capable("sha256:" + "a" * 64)

    monkeypatch.setattr(init_cmd.image_lifecycle, "reconcile_planned", reconcile)
    ctx = InitContext(project_root=tmp_path)
    assert init_cmd._step_image_lifecycle(ctx) is None
    assert ctx.results[-1].status == "err"
    assert "cannot probe Booley Directory layout" in ctx.results[-1].detail


@pytest.mark.parametrize(
    ("field", "value", "message"),
    [
        ("Id", None, "immutable identity is missing"),
        ("RootFS", None, "filesystem ancestry is missing"),
        ("RootFS", [], "filesystem ancestry is missing"),
        ("RootFS", "malformed", "filesystem ancestry is missing"),
    ],
)
def test_layout_verification_corrupt_candidate_is_a_controlled_error(field, value, message):
    from tests.runtime.test_image_identity import _layout_inspections

    parent_id, child_id = "sha256:" + "a" * 64, "sha256:" + "b" * 64
    records = _layout_inspections(parent_id, child_id, {}, {})
    records[child_id][field] = value
    with pytest.raises(RuntimeError, match=message):
        lifecycle.project_image.verify_layout_image(records[parent_id], records[child_id])


# The published Sandbox Images are wheel overlays on a registry substrate.
# These labels mirror .github/workflows/docker-publish.yml rather than any
# lifecycle node model, so a model drift fails here instead of in release CI.
_PUBLISHED_CONTRACTS = lifecycle.ImageBuildContracts("a" * 64, "b" * 64)
_PUBLISHED_WHEEL_SOURCE = "5" * 64
# Only the published RISC-V image sets this marker (Dockerfile.riscv).
RISCV_ENVIRONMENT = {ENV_SANDBOX_FLAVOR: "riscv"}


def _published_release_labels(*, version: str = "0.2.6") -> dict[str, str]:
    overlay_recipe = lifecycle.docker_data_dir() / "Dockerfile.wheel"
    return {
        lifecycle.LABEL_SCHEMA: lifecycle.PROVENANCE_SCHEMA,
        lifecycle.LABEL_VERSION: version,
        lifecycle.LABEL_ARTIFACT_ROLE: "wheel-overlay",
        lifecycle.LABEL_EFFECTIVE_INPUTS: _PUBLISHED_WHEEL_SOURCE,
        lifecycle.LABEL_WHEEL_SOURCE_FINGERPRINT: _PUBLISHED_WHEEL_SOURCE,
        lifecycle.LABEL_WHEEL_SHA256: "6" * 64,
        lifecycle.LABEL_RUNTIME_BASE_CONTRACT: _PUBLISHED_CONTRACTS.runtime_base,
        lifecycle.LABEL_STANDARD_SUBSTRATE_CONTRACT: _PUBLISHED_CONTRACTS.standard_substrate,
        lifecycle.LABEL_RECIPE_FINGERPRINT: lifecycle.resolve_recipe_fingerprint(
            (overlay_recipe,)
        ),
        lifecycle.LABEL_PARENT_ARTIFACT_KIND: lifecycle.PARENT_ARTIFACT_REGISTRY_DIGEST,
        lifecycle.LABEL_PARENT_ARTIFACT: (
            "ghcr.io/boldaxolotl/booley-sandbox-base@sha256:" + "7" * 64
        ),
        lifecycle.LABEL_BUILD_ORIGIN: "registry",
    }


def _wire_official_release(monkeypatch: pytest.MonkeyPatch, docker: FakeDocker) -> FakeBuilder:
    builder = _wire(monkeypatch, docker)
    pin_image_inputs(monkeypatch, wheel=_PUBLISHED_WHEEL_SOURCE, contracts=_PUBLISHED_CONTRACTS)
    return builder


class PublishedReleasePuller:
    """Pull exactly what the release workflow publishes, then adopt it by name."""

    def __init__(self, docker: FakeDocker) -> None:
        self.docker = docker
        self.pulled: list[str] = []

    def build(
        self,
        node: lifecycle.ImageNode,
        *,
        force: bool,
        source: lifecycle.ArtifactSource,
    ) -> str:
        del force
        assert source is lifecycle.ArtifactSource.VERIFIED_RELEASE_PULL
        self.pulled.append(node.reference)
        release = f"{lifecycle.PUBLISHED_REGISTRY}/{node.reference}:0.2.6"
        self.docker.images[release] = ("sha256:" + "8" * 64, _published_release_labels())
        return release


@pytest.mark.parametrize(
    ("wheel_source", "status"),
    [(_PUBLISHED_WHEEL_SOURCE, lifecycle.Status.CURRENT), ("0" * 64, lifecycle.Status.STALE)],
)
def test_official_host_check_accepts_only_a_release_for_the_current_wheel_sources(
    monkeypatch, wheel_source, status: lifecycle.Status
) -> None:
    labels = _published_release_labels()
    labels[lifecycle.LABEL_WHEEL_SOURCE_FINGERPRINT] = wheel_source
    labels[lifecycle.LABEL_EFFECTIVE_INPUTS] = wheel_source
    docker = FakeDocker({lifecycle.BASE_IMAGE: ("sha256:" + "8" * 64, labels)})
    _wire_official_release(monkeypatch, docker)

    result = lifecycle.reconcile(
        lifecycle.Intent.CHECK,
        artifact_policy=lifecycle.ArtifactPolicy.VERIFIED_RELEASE_ONLY,
    )

    assert result.status is status
    if status is lifecycle.Status.CURRENT:
        assert result.payload_fingerprint == _PUBLISHED_WHEEL_SOURCE


def test_official_host_bootstrap_pulls_and_verifies_the_published_release_image(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Regression: bootstrap pulled the release, then rejected its overlay labels."""
    docker = FakeDocker({})
    _wire_official_release(monkeypatch, docker)
    puller = PublishedReleasePuller(docker)

    result = lifecycle.reconcile(
        lifecycle.Intent.ENSURE,
        docker=docker,
        builder=puller,
        artifact_policy=lifecycle.ArtifactPolicy.VERIFIED_RELEASE_ONLY,
    )

    assert puller.pulled == [lifecycle.BASE_IMAGE]
    assert result.status is lifecycle.Status.CHANGED
    assert docker.image_id(lifecycle.BASE_IMAGE) == "sha256:" + "8" * 64
    assert result.payload_fingerprint == _PUBLISHED_WHEEL_SOURCE


@pytest.mark.parametrize("held_by", ["project-tag", "published-name"])
@pytest.mark.parametrize("configured", [lifecycle.BASE_IMAGE, "booley-sandbox-riscv"])
@pytest.mark.parametrize("found", [lifecycle.BASE_IMAGE, "booley-sandbox-riscv"])
def test_official_project_check_accepts_only_the_configured_release_flavor(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, found: str, configured: str, held_by: str
) -> None:
    root = _project(tmp_path, configured)
    source = (
        lifecycle.project_image.project_image_name(root) if held_by == "project-tag" else found
    )
    image_id = "sha256:" + "8" * 64
    docker = FakeDocker({source: (image_id, _published_release_labels())})
    if found == "booley-sandbox-riscv":
        docker.environments[image_id] = RISCV_ENVIRONMENT
    _wire_official_release(monkeypatch, docker)
    policy = lifecycle.ArtifactPolicy.VERIFIED_RELEASE_ONLY

    check = _check_project(root, docker, artifact_policy=policy)
    ensured = lifecycle.plan(
        lifecycle.ProjectImageScope(root), docker=docker, artifact_policy=policy
    )

    matches = found == configured
    assert check.status is (lifecycle.Status.CURRENT if matches else lifecycle.Status.STALE)
    assert [step.action for step in ensured.steps] == [
        lifecycle.PlanAction.REUSE if matches else lifecycle.PlanAction.PULL
    ]


def test_official_overlay_on_the_other_flavor_reacquires_the_configured_parent(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = _requirements_project(tmp_path)
    docker = FakeDocker({})
    _wire_official_release(monkeypatch, docker)
    policy = lifecycle.ArtifactPolicy.VERIFIED_RELEASE_THEN_LOCAL
    scope = lifecycle.ProjectImageScope(root)
    parent, overlay = lifecycle.plan(scope, docker=docker, artifact_policy=policy).nodes
    # The release parent still holds the standard image; the overlay was built on it
    # with the RISC-V selection label.
    docker.images[parent.reference] = ("sha256:" + "8" * 64, _published_release_labels())
    TransactionBuilder(docker).prepare(
        overlay, candidate_reference=overlay.reference, parent_reference=parent.reference
    )

    check = _check_project(root, docker, artifact_policy=policy)
    planned = lifecycle.plan(scope, docker=docker, artifact_policy=policy)

    assert check.status is lifecycle.Status.STALE
    assert [(step.action, step.reason.message) for step in planned.steps] == [
        (
            lifecycle.PlanAction.PULL,
            "configured Sandbox Image is booley-sandbox-riscv but the image is booley-sandbox",
        ),
        (lifecycle.PlanAction.BUILD, "a selected ancestor must be replaced"),
    ]


def _layout_build_probe(probe: SimpleNamespace, context, spec) -> int:
    probe.specs.append(spec)
    probe.contexts.append(context)
    assert spec.context.is_dir()
    assert spec.network == "none"
    assert spec.parent_artifact == probe.parent["Id"]
    assert spec.build_args == ("--build-arg", "BOOLEY_LAYOUT_USER=1000:1000")
    assert spec.capacity_plan.requests == (spec.capacity_request,)
    assert dict(spec.labels)[lifecycle.LABEL_WHEEL_SHA256] == "b" * 64
    tag = spec.build_contexts[0][1].removeprefix("docker-image://")
    assert probe.docker.image_id(tag) == probe.parent["Id"]
    if probe.failure == "parent":
        probe.docker.images[tag] = ("substituted", probe.labels)
    if probe.failure == "cleanup":

        def refuse_cleanup(_tag: str) -> None:
            raise OSError("cleanup refused")

        probe.monkeypatch.setattr(probe.docker, "remove_tag", refuse_cleanup)
    probe.docker.images[spec.image] = ("candidate-id", dict(spec.labels))
    return 1 if probe.failure == "build" else 0


def _layout_builder_probe(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, failure: str | None
) -> SimpleNamespace:
    from booley.harness.setup import docker_image

    node = _incremental_node(tmp_path, lifecycle.ImageRole.PROJECT_DATA_LAYOUT, source="wheel")
    parent_id = "sha256:" + "a" * 64
    labels = {
        lifecycle.LABEL_WHEEL_SOURCE_FINGERPRINT: "wheel",
        lifecycle.LABEL_WHEEL_SHA256: "b" * 64,
    }
    parent = {"Id": parent_id, "Config": {"User": "1000:1000", "Labels": labels}}
    docker = FakeDocker({"parent": (parent_id, labels)})
    adapter = harness_lifecycle._IncrementalBuildAdapter(
        tmp_path, docker, verbose=False, requests=(harness_lifecycle._capacity_request(node),)
    )
    probe = SimpleNamespace(
        node=node,
        parent=parent,
        labels=labels,
        docker=docker,
        adapter=adapter,
        failure=failure,
        monkeypatch=monkeypatch,
        specs=[],
        verified=[],
        contexts=[],
    )
    monkeypatch.setattr(
        lifecycle.project_image,
        "inspect_layout_image",
        lambda image: parent if image == parent_id else {"Id": image},
    )

    def verify(candidate_id, parent_image_id, executable) -> None:
        probe.verified.append((candidate_id, parent_image_id, executable))
        if failure == "verify":
            raise RuntimeError("candidate changed runtime configuration")

    monkeypatch.setattr(
        docker_image,
        "_docker_build_image",
        lambda context, spec: _layout_build_probe(probe, context, spec),
    )
    monkeypatch.setattr(lifecycle.project_image, "verify_layout_parent_identity", verify)
    return probe


@pytest.mark.parametrize("failure", [None, "build", "parent", "verify", "cleanup"])
def test_layout_builder_pins_parent_and_cleans_temporary_reference(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, failure: str | None
) -> None:
    # Private seam: the adapter's Docker build and parent pinning run only inside a real `docker build`.
    probe = _layout_builder_probe(tmp_path, monkeypatch, failure)
    adapter = probe.adapter
    if failure in {"build", "parent", "verify"}:
        with pytest.raises(lifecycle.ImageLifecycleError):
            adapter.prepare(probe.node, candidate_reference="candidate", parent_reference="parent")
        assert adapter._next_request_index == 0
    else:
        assert (
            adapter.prepare(probe.node, candidate_reference="candidate", parent_reference="parent")
            == "candidate"
        )
        assert adapter._next_request_index == 1
        assert probe.verified == [("candidate-id", probe.parent["Id"], "docker")]
    assert probe.docker.image_id("parent") == probe.parent["Id"]
    assert not probe.specs[0].context.exists()
    temporary = [name for name in probe.docker.images if "layout-parent:candidate" in name]
    assert bool(temporary) == (failure in {"parent", "cleanup"})
    if failure == "cleanup":
        warning = probe.contexts[0].results[-1]
        assert warning.status == "warn"
        assert warning.detail == "cleanup refused"


@pytest.mark.parametrize("failure", ["missing", "identity", "wheel", "user", "inspect-error"])
def test_layout_builder_rejects_untrusted_parent_before_tagging(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, failure: str
) -> None:
    # Private seam: the adapter's Docker build and parent pinning run only inside a real `docker build`.
    node = _incremental_node(tmp_path, lifecycle.ImageRole.PROJECT_DATA_LAYOUT, source="wheel")
    parent_id = "sha256:" + "a" * 64
    labels = {
        lifecycle.LABEL_WHEEL_SOURCE_FINGERPRINT: "wheel",
        lifecycle.LABEL_WHEEL_SHA256: "b" * 64,
    }
    docker = FakeDocker({"parent": (parent_id, labels)})
    parent = {"Id": parent_id, "Config": {"User": "1000", "Labels": labels}}
    if failure == "identity":
        parent["Id"] = "other-id"
    elif failure == "wheel":
        labels.pop(lifecycle.LABEL_WHEEL_SHA256)
    elif failure == "user":
        parent["Config"]["User"] = "root;exit"
    elif failure == "missing":
        docker.images.clear()

    def inspect(_image):
        if failure == "inspect-error":
            raise RuntimeError("Docker inspection unavailable")
        return parent

    monkeypatch.setattr(lifecycle.project_image, "inspect_layout_image", inspect)
    adapter = harness_lifecycle._IncrementalBuildAdapter(tmp_path, docker, verbose=False)
    expected = "inspection unavailable" if failure == "inspect-error" else None
    with pytest.raises(lifecycle.ImageLifecycleError, match=expected):
        adapter.prepare(node, candidate_reference="candidate", parent_reference="parent")
    assert docker.mutations == []
    assert docker.image_id("candidate") is None


@pytest.mark.parametrize("failure", ["absent", "tag-substitution", "missing-child"])
def test_layout_builder_refuses_parent_before_docker_build(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, failure: str
) -> None:
    # Private seam: the adapter's Docker build and parent pinning run only inside a real `docker build`.
    probe = _layout_builder_probe(tmp_path, monkeypatch, None)
    if failure == "missing-child":
        from booley.harness.setup import docker_image

        monkeypatch.setattr(
            docker_image,
            "_docker_build_image",
            lambda _context, _spec: 0,
        )
        with pytest.raises(lifecycle.ImageLifecycleError, match="produced no image"):
            probe.adapter.prepare(
                probe.node, candidate_reference="candidate", parent_reference="parent"
            )
        assert probe.verified == []
        assert not [name for name in probe.docker.images if "layout-parent:candidate" in name]
        return
    parent_reference = None if failure == "absent" else "parent"
    if failure == "tag-substitution":
        tag = probe.docker.tag

        def substitute(source: str, target: str) -> None:
            tag(source, target)
            probe.docker.images[target] = ("substituted", probe.labels)

        monkeypatch.setattr(probe.docker, "tag", substitute)
    with pytest.raises(lifecycle.ImageLifecycleError, match="parent"):
        probe.adapter.prepare(
            probe.node, candidate_reference="candidate", parent_reference=parent_reference
        )
    assert probe.specs == []
    assert probe.docker.image_id("candidate") is None
    assert probe.docker.image_id("parent") == probe.parent["Id"]


# Private seam: the stamp recovery below reaches `init` and `bootstrap` only through a whole
# Project and host install, so each consumer's step function is called directly.
@pytest.fixture
def runtime_image_stamp_environment(tmp_path, monkeypatch):
    import sys
    from types import ModuleType

    docker, _pulls = _wire_distribution_pull(tmp_path, monkeypatch, stamped=False)
    stamp = ModuleType("booley._build_commit")
    stamp.DEVELOPMENT_CONTEXT_SHA256 = ""
    stamp.PAYLOAD_FINGERPRINT = "a" * 64
    stamp.WHEEL_SOURCE_FINGERPRINT = "b" * 64
    stamp.RUNTIME_BASE_CONTRACT = "c" * 64
    stamp.STANDARD_SUBSTRATE_CONTRACT = "d" * 64
    stamp.OFFICIAL_RELEASE = False
    monkeypatch.setitem(sys.modules, "booley._build_commit", stamp)
    from booley.runtime import image_build_contracts

    stamp_file = tmp_path / "_build_commit.py"
    stamp_file.write_text(
        f'RUNTIME_BASE_CONTRACT = "{stamp.RUNTIME_BASE_CONTRACT}"\n'
        f'STANDARD_SUBSTRATE_CONTRACT = "{stamp.STANDARD_SUBSTRATE_CONTRACT}"\n'
    )
    monkeypatch.setattr(image_build_contracts, "_embedded_stamp_path", lambda: stamp_file)
    monkeypatch.setattr(harness_lifecycle, "capacity_plan", lambda _plan: None)
    plan = SimpleNamespace(
        project_root=tmp_path,
        steps=(),
        nodes=(
            SimpleNamespace(
                role=lifecycle.ImageRole.WHEEL_OVERLAY,
                acquisition_policy=lifecycle.ArtifactPolicy.LOCAL_ONLY,
            ),
        ),
    )
    monkeypatch.setattr(harness_lifecycle, "plan", lambda *_args: plan)
    monkeypatch.setattr(
        harness_lifecycle,
        "_transaction_build_adapter",
        lambda *_a, **_kw: pytest.fail("must fail before building"),
    )
    return docker, plan


@pytest.mark.parametrize("consumer", ["host", "prepare", "init", "refresh", "bootstrap"])
def test_runtime_image_stamp_recovery_reaches_consumers(
    tmp_path, monkeypatch, capsys, consumer, runtime_image_stamp_environment
):
    # Private seam: `init` and `bootstrap` need a whole Project and host install, so call their steps.
    from booley.harness import bootstrap, bootstrap_cli, init_cmd
    from booley.harness import session_refresh as harness_refresh

    docker, plan = runtime_image_stamp_environment
    if consumer == "init":
        monkeypatch.setattr(
            harness_lifecycle,
            "reconcile_planned",
            lambda *_a, **_kw: harness_lifecycle.prepare(plan),
        )
        ctx = init_cmd.InitContext(project_root=tmp_path)
        assert init_cmd._step_image_lifecycle(ctx) is None
        message = ctx.results[-1].detail
    elif consumer == "bootstrap":
        monkeypatch.setattr(bootstrap, "reconcile_images", harness_lifecycle.reconcile)
        result, finding = bootstrap._reconcile_base_image(lifecycle.Intent.ENSURE, verbose=False)
        assert result is None
        assert finding.state is bootstrap.BootstrapState.ERROR
        monkeypatch.setattr(
            bootstrap_cli,
            "inspect_bootstrap",
            lambda *_a, **_kw: bootstrap.BootstrapResult(lifecycle.Intent.ENSURE, (finding,)),
        )
        assert (
            bootstrap_cli.run_bootstrap(
                SimpleNamespace(force=False, check_only=True, verbose=True)
            )
            == 2
        )
        message = capsys.readouterr().out
    else:
        with pytest.raises(lifecycle.ImageLifecycleError) as caught:
            if consumer == "host":
                harness_lifecycle.reconcile(lifecycle.Intent.ENSURE)
            elif consumer == "refresh":
                monkeypatch.setattr(
                    harness_lifecycle,
                    "reconcile_planned",
                    lambda *_a, **_kw: harness_lifecycle.prepare(plan),
                )
                monkeypatch.setattr(
                    init_cmd, "_report_project_requirements", lambda *_a, **_k: None
                )
                harness_refresh._RuntimeImages().converge(tmp_path, verbose=False)
            else:
                harness_lifecycle.prepare(plan)
        message = str(caught.value)
        if consumer != "host":
            assert isinstance(caught.value.__cause__, ValueError)
    assert "runtime-image-style" in message
    assert "BuildProfile.DEVELOPMENT_WHEEL" in message
    assert "python3 -P -m build --wheel" in message
    assert "python3 -P .github/scripts/build_development_wheel.py" in message
    assert docker.mutations == []


def test_verified_source_context_preserves_builder_errors_and_cleanup(monkeypatch):
    # Private seam: a distribution `prepare` is the only public caller of this context.
    from booley.runtime.version_attribution import VersionAttribution, VersionOrigin

    monkeypatch.setattr(
        booley,
        "version_attribution",
        VersionAttribution(
            version="0.2.6", origin=VersionOrigin.DISTRIBUTION, distribution_name="booley-rtl"
        ),
    )
    closed = []

    @contextmanager
    def context():
        try:
            yield Path("verified-source")
        finally:
            closed.append(True)

    monkeypatch.setattr(harness_lifecycle, "extracted_development_context", context)
    with (
        pytest.raises(ValueError, match="builder failure"),
        harness_lifecycle._verified_source_context(True) as root,
    ):
        assert root == Path("verified-source")
        raise ValueError("builder failure")
    assert closed == [True]


@pytest.mark.parametrize("consumer", ["alias", "keeper", "container", "child", "docker-conflict"])
def test_owned_gc_defers_every_live_consumer(tmp_path, monkeypatch, consumer):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    image = "sha256:" + "a" * 64
    child = "sha256:" + "b" * 64
    docker = FakeDocker({image: (image, {})})
    if consumer in {"alias", "keeper"}:
        docker.images["foreign" if consumer == "alias" else "booley-issued-other:session"] = (
            image,
            {},
        )
    if consumer == "container":
        docker.used_image_ids = frozenset({image})
    if consumer == "child":
        docker.images[child] = (child, {lifecycle.LABEL_PARENT_ARTIFACT: image})
    if consumer == "docker-conflict":

        def conflict(_identity):
            raise lifecycle.ImageLifecycleError("image is being used by a running container")

        monkeypatch.setattr(docker, "remove_tag", conflict)
    lifecycle.OwnedImages(tmp_path).record({}, (image,))
    assert lifecycle.cleanup_owned_images(tmp_path, docker=docker)
    assert ("remove_tag", image) not in docker.mutations
    assert lifecycle.OwnedImages(tmp_path).ids == [image]


@pytest.mark.parametrize("unrelated", [0, 40])
def test_owned_gc_docker_calls_do_not_scale_with_unrelated_inventory(
    tmp_path, monkeypatch, unrelated
):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    owned, tagged = "sha256:" + "a" * 64, "sha256:" + "b" * 64
    foreign = [f"sha256:{index:064x}" for index in range(unrelated)]
    docker = FakeDocker({owned: (owned, {}), "user:alias": (tagged, {})})
    docker.images.update({f"other-{image[-8:]}:latest": (image, {}) for image in foreign})
    docker.used_image_ids = frozenset(foreign)
    calls = []
    for name in ("image_id", "inspect", "repo_digests", "container_image_ids"):
        monkeypatch.setattr(docker, name, lambda *_args, name=name: calls.append(name) or ())
    for name in ("image_ids", "image_references", "remove_tag"):
        method = getattr(docker, name)
        monkeypatch.setattr(
            docker,
            name,
            lambda *args, name=name, method=method: calls.append(name) or method(*args),
        )
    assert lifecycle.cleanup_owned_images(tmp_path / "unrecorded", docker=docker) == ()
    assert calls == []
    lifecycle.OwnedImages(tmp_path).record({}, (owned, tagged))
    retained = lifecycle.cleanup_owned_images(tmp_path, docker=docker)
    assert retained == (f"{tagged}: retained (tag/keeper)",)
    assert calls == ["image_ids", "image_references", "remove_tag"]


def test_owned_gc_retries_record_in_fresh_process(tmp_path, monkeypatch):
    import os
    import sys

    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    image, second = "sha256:" + "a" * 64, "sha256:" + "b" * 64
    lifecycle.OwnedImages(tmp_path).record({}, (image,))
    lifecycle.OwnedImages(tmp_path).record({}, (second,))  # a later refresh adds, never replaces
    code = """
from pathlib import Path
from booley.runtime import image_lifecycle as lifecycle
class Docker:  # only the two inventories and exact-ID removal exist
    def image_ids(self): return frozenset({IMAGE, SECOND})
    def image_references(self): return ()
    def remove_tag(self, image): print("deleted", image)
assert lifecycle.cleanup_owned_images(Path(PROJECT), docker=Docker()) == ()
"""
    code = f"IMAGE={image!r}\nSECOND={second!r}\nPROJECT={str(tmp_path)!r}\n" + code
    result = run_bounded_child(
        [sys.executable, "-c", code],
        env={**os.environ, "PYTHONPATH": str(Path(__file__).parents[2] / "src")},
    )
    assert result.returncode == 0, result.stderr
    assert "deleted " + image in result.stdout and "deleted " + second in result.stdout
    assert list(lifecycle.OwnedImages(tmp_path).store.root.glob("*.json")) == []


def test_cached_candidate_bytes_are_not_new_transaction_ownership(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    image = "sha256:" + "a" * 64
    docker = FakeDocker({"preexisting": (image, {})})

    reference = "booley-lifecycle-transaction:candidate"
    before = docker.image_ids()
    docker.tag(image, reference)  # a cached build reuses preexisting bytes
    lifecycle.OwnedImages(tmp_path).acquire(docker, before, [reference])
    record = lifecycle.OwnedImages(tmp_path)
    assert record.refs == {reference: image} and record.ids == []
    docker.remove_tag(reference)
    assert lifecycle.cleanup_owned_images(tmp_path, docker=docker) == ()
    assert ("remove_tag", image) not in docker.mutations


@pytest.mark.parametrize("reassigned", [False, True])
def test_recorded_orphan_cleanup_checks_identity_and_preserves_foreign_alias(
    tmp_path, monkeypatch, reassigned
):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    root, docker = _project(tmp_path), FakeDocker({})
    _wire(monkeypatch, docker)
    planned = lifecycle.plan(lifecycle.ProjectImageScope(root), docker=docker)
    image, foreign = "sha256:" + "a" * 64, "sha256:" + "b" * 64
    ours, candidate = "booley-lifecycle-first:candidate", "booley-lifecycle-recorded:candidate"
    docker.images.update(
        {
            ours: (image, {}),
            candidate: (foreign if reassigned else image, {}),
            "foreign": (image, {}),
        }
    )
    lifecycle.OwnedImages(root).record({ours: image, candidate: image, "foreign": image})
    builder = TransactionBuilder(docker)

    if reassigned:
        # One recorded tag now names another image: refuse all cleanup before any removal.
        with pytest.raises(lifecycle.ImageLifecycleError, match="changed before cleanup"):
            lifecycle.prepare(planned, docker=docker, builder=builder)
        assert docker.mutations == []
        return
    lifecycle.prepare(planned, docker=docker, builder=builder)
    assert docker.image_id(candidate) is None and docker.image_id(ours) is None
    assert docker.image_id("foreign") == image
    assert [m for m in docker.mutations if m[0] == "remove_tag"] == [
        ("remove_tag", ours),
        ("remove_tag", candidate),
    ]


@pytest.mark.parametrize("identity", ["sha256:short", "../escape"])
def test_acquisition_proof_rejects_invalid_identity_before_persistence(
    tmp_path, monkeypatch, identity
):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    with pytest.raises(lifecycle.ImageLifecycleError):
        lifecycle.OwnedImages(tmp_path).record({}, (identity,))
    assert not lifecycle.OwnedImages(tmp_path).store.root.exists()


@pytest.mark.parametrize(
    "field,value",
    [
        ("version", 2),
        ("project_root", "relative"),
        ("owned_ids", [123]),
        ("candidate_refs", {"candidate": 123}),
        ("owned_ids", ["sha256:short"]),
        ("candidate_refs", []),
    ],
)
def test_corrupt_ownership_record_cannot_authorize_deletion(tmp_path, monkeypatch, field, value):
    import json

    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    image = "sha256:" + "a" * 64
    lifecycle.OwnedImages(tmp_path).record({}, (image,))
    store = lifecycle.OwnedImages(tmp_path).store
    name = lifecycle.OwnedImages(tmp_path).name
    record = store.read_json(name)
    record[field] = value
    store.atomic_write_text(name, json.dumps(record))
    docker = FakeDocker({image: (image, {})})
    with pytest.raises(lifecycle.ImageLifecycleError):
        lifecycle.cleanup_owned_images(tmp_path, docker=docker)
    assert docker.mutations == []
    assert (store.root / name).exists()


def test_failed_candidate_construction_releases_only_recorded_tags(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    root = _project(tmp_path)
    docker = FakeDocker({})
    _wire(monkeypatch, docker)
    planned = lifecycle.plan(lifecycle.ProjectImageScope(root), docker=docker)
    before = dict(docker.images)

    class FailingBuilder(TransactionBuilder):
        def prepare(self, node, *, candidate_reference, parent_reference):
            super().prepare(
                node, candidate_reference=candidate_reference, parent_reference=parent_reference
            )
            raise lifecycle.ImageLifecycleError("partial construction failed")

    with pytest.raises(lifecycle.ImageLifecycleError, match="partial construction failed"):
        lifecycle.prepare(planned, docker=docker, builder=FailingBuilder(docker))
    assert docker.images == before
    assert list(lifecycle.OwnedImages(tmp_path).store.root.glob("*.json")) == []


def test_prepare_records_built_images_and_candidate_refs_for_later_cleanup(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    root = _project(tmp_path)
    docker = FakeDocker({})
    _wire(monkeypatch, docker)
    planned = lifecycle.plan(lifecycle.ProjectImageScope(root), docker=docker)

    prepared = lifecycle.prepare(planned, docker=docker, builder=TransactionBuilder(docker))

    record = lifecycle.OwnedImages(root)
    built = {
        image.candidate_reference: image.image_id
        for image in prepared.candidates
        if image.candidate_reference != image.reference
    }
    assert built and built.items() <= record.refs.items()
    assert set(built.values()) <= set(record.ids)


def test_prepare_records_alias_candidate_of_a_reused_source(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    root = _project(tmp_path)
    docker = FakeDocker({})
    _wire(monkeypatch, docker)
    first = lifecycle.prepare(
        lifecycle.plan(lifecycle.ProjectImageScope(root), docker=docker),
        docker=docker,
        builder=TransactionBuilder(docker),
    )
    for image in first.candidates:  # adopt: managed tags now carry the built bytes
        docker.images[image.reference] = docker.images[image.candidate_reference]
    planned = lifecycle.plan(lifecycle.ProjectImageScope(root), docker=docker)
    last = planned.steps[-1]
    docker.images["alias-source"] = docker.images[last.reference]
    steps = (*planned.steps[:-1], replace(last, source_reference="alias-source"))
    reused = lifecycle.prepare(
        replace(planned, steps=steps), docker=docker, builder=TransactionBuilder(docker)
    )

    alias = reused.candidates[-1].candidate_reference
    assert alias and lifecycle.OwnedImages(root).refs[alias] == docker.image_id("alias-source")


def _empty_ownership_record(tmp_path, monkeypatch):
    import json

    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    owned = lifecycle.OwnedImages(tmp_path)
    owned.store.ensure_directory()
    record = {"version": 1, "owned_ids": [], "candidate_refs": {}}
    owned.store.atomic_write_text(owned.name, json.dumps(record))
    return lifecycle.OwnedImages(tmp_path), owned.store.root / owned.name


@pytest.mark.parametrize(("holds", "lasting"), [(1, False), (99, True)])
def test_empty_ownership_removal_retries_a_sharing_error(tmp_path, monkeypatch, holds, lasting):
    from tests.sharing_error_support import hold_unlinks

    owned, record = _empty_ownership_record(tmp_path, monkeypatch)
    attempts = hold_unlinks(monkeypatch, {record: holds})
    if lasting:
        with pytest.raises(PermissionError):
            owned.record({})
        assert attempts[record] > 1
    else:
        owned.record({})
        assert attempts[record] == 2
        assert not record.exists()
