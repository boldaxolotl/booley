"""Authoritative Session Image reconciliation (GitHub issue #128)."""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from booley.harness import image_lifecycle as lifecycle


class FakeDocker:
    """In-memory adapter for Docker, the lifecycle's true external dependency."""

    def __init__(self, images: dict[str, tuple[str, dict[str, str]]]) -> None:
        self.images = images
        self.mutations: list[tuple[str, ...]] = []

    def image_id(self, image: str) -> str | None:
        record = self.images.get(image)
        return record[0] if record else None

    def label(self, image: str, name: str) -> str | None:
        record = self.images.get(image)
        return record[1].get(name) if record else None

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

    def build(self, node, *, force: bool) -> None:
        del force
        self.built.append(node.reference)
        labels = dict(node.expected_labels)
        labels[lifecycle.LABEL_BUILD_ORIGIN] = "local"
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
    def build(self, node, *, force: bool) -> None:
        super().build(node, force=force)
        raise lifecycle.ImageLifecycleError("build failed")


class FailOnSecondBuilder(FakeBuilder):
    def build(self, node, *, force: bool) -> None:
        super().build(node, force=force)
        if len(self.built) == 2:
            raise lifecycle.ImageLifecycleError("derived build failed")


def _project(tmp_path: Path, image: str | None = None) -> Path:
    root = tmp_path / "project"
    project_dir = root / ".booley_project"
    project_dir.mkdir(parents=True)
    body = "[sandbox]\n"
    if image is not None:
        body += f'image = "{image}"\n'
    (project_dir / "booley.toml").write_text(body, encoding="utf-8")
    return root


def _wire(monkeypatch: pytest.MonkeyPatch, docker: FakeDocker) -> FakeBuilder:
    from booley.harness import docker_base_contract

    builder = FakeBuilder(docker)
    stable_id = "sha256:" + "9" * 64
    docker.images.setdefault(
        lifecycle.STABLE_RUNTIME_BASE_IMAGE,
        (stable_id, {"io.booley.runtime-base.contract": "stable-contract"}),
    )
    if lifecycle.BASE_IMAGE in docker.images:
        docker.images[lifecycle.BASE_IMAGE][1].setdefault(
            lifecycle.LABEL_BUILD_ORIGIN, "local"
        )
        if lifecycle.LABEL_SCHEMA in docker.images[lifecycle.BASE_IMAGE][1]:
            docker.images[lifecycle.BASE_IMAGE][1].setdefault(
                lifecycle.LABEL_PARENT_ARTIFACT,
                stable_id,
            )
    monkeypatch.setattr(lifecycle, "_expected_payload_fingerprint", lambda: "payload-new")
    monkeypatch.setattr(lifecycle, "_expected_version", lambda: "0.2.6")
    monkeypatch.setattr(docker_base_contract, "contract", lambda _root: "stable-contract")
    monkeypatch.setattr(lifecycle, "_docker_adapter", lambda: docker)
    monkeypatch.setattr(lifecycle, "_build_adapter", lambda *_args, **_kwargs: builder)
    return builder


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
    return values


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

    result = lifecycle.reconcile(root, lifecycle.Intent.CHECK)

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
    docker.images[lifecycle.STABLE_RUNTIME_BASE_IMAGE][1][
        "io.booley.runtime-base.contract"
    ] = "old-contract"

    result = lifecycle.reconcile(root, lifecycle.Intent.CHECK)

    assert result.status is lifecycle.Status.STALE


def test_packaged_install_accepts_exact_local_parent_when_contract_is_unavailable(
    tmp_path: Path, monkeypatch
):
    from booley.harness import docker_base_contract

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

    assert lifecycle.reconcile(root, lifecycle.Intent.CHECK).status is lifecycle.Status.CURRENT


def test_ensure_rebuilds_base_then_flavor_and_returns_exact_id(
    tmp_path: Path, monkeypatch
):
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

    result = lifecycle.reconcile(root, lifecycle.Intent.ENSURE)

    assert builder.built == ["booley-sandbox", "booley-sandbox-riscv"]
    assert result.status is lifecycle.Status.CHANGED
    assert result.selected_id == docker.image_id("booley-sandbox-riscv")
    assert result.requires_spec_reseed is True
    assert result.requires_runtime_recreation is True


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

    result = lifecycle.reconcile(root, lifecycle.Intent.ENSURE)

    assert builder.built == ["booley-sandbox", "booley-sandbox-riscv", generated]
    assert result.selected_reference == generated
    assert dockerfile.read_text(encoding="utf-8") == original


def test_explicit_external_image_receives_zero_mutations(tmp_path: Path, monkeypatch):
    root = _project(tmp_path, "registry.example/team/custom:latest")
    docker = FakeDocker({})
    builder = _wire(monkeypatch, docker)

    result = lifecycle.reconcile(root, lifecycle.Intent.REFRESH)

    assert result.status is lifecycle.Status.EXTERNAL
    assert not builder.built
    assert not docker.mutations


def test_ensure_generates_project_recipe_for_configured_requirements(
    tmp_path: Path, monkeypatch
):
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

    result = lifecycle.reconcile(root, lifecycle.Intent.ENSURE)

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
        lifecycle.reconcile(root, lifecycle.Intent.CHECK)


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
        lifecycle.reconcile(root, lifecycle.Intent.CHECK)


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
    lifecycle.reconcile(root, lifecycle.Intent.ENSURE)
    generated_recipe = root / ".booley_project" / "docker" / "requirements.txt"
    before = generated_recipe.read_bytes()

    requirement.write_text("cocotb==2.0.2\n", encoding="utf-8")
    result = lifecycle.reconcile(root, lifecycle.Intent.CHECK)

    assert result.status is lifecycle.Status.STALE
    assert generated_recipe.read_bytes() == before


def test_checkout_project_dir_override_wins_over_literal_directory(
    tmp_path: Path, monkeypatch
):
    root = tmp_path / "project"
    local = root / ".booley_project"
    custom = root / "control"
    local.mkdir(parents=True)
    custom.mkdir()
    (root / "booley.toml").write_text('[project]\ndir = "control"\n', encoding="utf-8")
    (custom / "booley.toml").write_text("[sandbox]\n", encoding="utf-8")
    docker = FakeDocker({})
    _wire(monkeypatch, docker)

    lifecycle.reconcile(root, lifecycle.Intent.CHECK)

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
        lifecycle.reconcile(root, lifecycle.Intent.ENSURE)

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
        lifecycle.reconcile(root, lifecycle.Intent.REFRESH)

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
        lifecycle.reconcile(root, lifecycle.Intent.REFRESH)

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
        lifecycle.reconcile(root, lifecycle.Intent.REFRESH)

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

    result = lifecycle.reconcile(root, lifecycle.Intent.ENSURE)

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

    lifecycle.reconcile(root, lifecycle.Intent.ENSURE)

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
    node = lifecycle._ImageNode(
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

    lifecycle._LegacyBuildAdapter(root, verbose=True).build(node, force=True)

    assert calls == [(node.reference, docker_dir, True)]
    assert dockerfile.read_text(encoding="utf-8") == original


@pytest.mark.parametrize("reference", [lifecycle.BASE_IMAGE, "booley-sandbox-riscv"])
def test_packaged_refresh_uses_pull_capable_builder(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    reference: str,
) -> None:
    from booley.harness import init_docker_image

    root = _project(tmp_path)
    docker_dir = tmp_path / "installed" / "src" / "booley" / "data" / "docker"
    docker_dir.mkdir(parents=True)
    recipe = docker_dir / (
        "Dockerfile" if reference == lifecycle.BASE_IMAGE else "Dockerfile.riscv"
    )
    recipe.write_text("FROM scratch\n", encoding="utf-8")
    monkeypatch.setattr(lifecycle, "docker_data_dir", lambda: docker_dir)
    pulls: list[tuple[str, str]] = []
    monkeypatch.setattr(
        init_docker_image,
        "_try_pull_image",
        lambda version, image=lifecycle.BASE_IMAGE: pulls.append((version, image)) or True,
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
    node = lifecycle._ImageNode(
        reference,
        recipe,
        lifecycle.PayloadProvenance("1", "0.2.6", "payload"),
        lifecycle.BuildProvenance("recipe", None),
    )

    lifecycle._LegacyBuildAdapter(root, verbose=False).build(node, force=True)

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
