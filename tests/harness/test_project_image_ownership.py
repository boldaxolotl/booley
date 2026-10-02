"""Project image transactions may only mutate their own descendants."""

from pathlib import Path

import pytest

from booley.config.sandbox import project_image_name
from booley.runtime import image_lifecycle as lifecycle
from tests.harness.test_image_lifecycle import (
    FakeDocker,
    TransactionBuilder,
    _install_planned_graph,
    _project,
    _wire,
)


def _bootstrap_base(monkeypatch, docker):
    _wire(monkeypatch, docker)
    monkeypatch.setattr(
        lifecycle.project_image, "project_data_alias_capable", lambda _image: False
    )
    contracts = lifecycle._expected_image_build_contracts()
    base, _ = lifecycle._source_graph_base(contracts, lifecycle.docker_data_dir())
    _install_planned_graph(docker, (base,))


def test_same_slug_projects_have_disjoint_writable_images(tmp_path: Path):
    assert project_image_name(tmp_path / "a" / "design") != project_image_name(
        tmp_path / "b" / "design"
    )


def test_local_project_requires_bootstrap_before_any_build(tmp_path, monkeypatch):
    root = _project(tmp_path)
    docker = FakeDocker({})
    _wire(monkeypatch, docker)
    docker.images.clear()
    with pytest.raises(lifecycle.ImageLifecycleError, match="booley bootstrap"):
        lifecycle.plan(lifecycle.ProjectImageScope(root), docker=docker)
    assert docker.mutations == []


def test_refresh_reuses_base_and_snapshots_only_project_outputs(tmp_path, monkeypatch):
    root = _project(tmp_path)
    docker = FakeDocker({})
    _bootstrap_base(monkeypatch, docker)
    planned = lifecycle.plan(
        lifecycle.ProjectImageScope(root), docker=docker, intent=lifecycle.Intent.REFRESH
    )
    assert planned.steps[0].action is lifecycle.PlanAction.REUSE
    assert all(node.reference.startswith(project_image_name(root)) for node in planned.nodes[1:])
    prepared = lifecycle.prepare(planned, docker=docker, builder=TransactionBuilder(docker))
    assert lifecycle.STABLE_RUNTIME_BASE_IMAGE not in {
        row.reference for row in prepared.prior_tags
    }
    assert all(row.reference.startswith(project_image_name(root)) for row in prepared.prior_tags)


@pytest.mark.parametrize("image", [None, "booley-sandbox", "booley-sandbox-riscv"])
@pytest.mark.parametrize("intent", list(lifecycle.Intent))
def test_project_matrix_never_mutates_shared_tags(tmp_path, monkeypatch, image, intent):
    root = _project(tmp_path, image)
    docker = FakeDocker({})
    _bootstrap_base(monkeypatch, docker)
    shared = dict(docker.images)
    planned = lifecycle.plan(lifecycle.ProjectImageScope(root), docker=docker, intent=intent)
    if intent is lifecycle.Intent.CHECK:
        result = lifecycle.planned_result(planned, docker)
        assert result.status is lifecycle.Status.STALE
        assert docker.mutations == []
    else:
        prepared = lifecycle.prepare(planned, docker=docker, builder=TransactionBuilder(docker))
        lifecycle.commit(prepared, docker=docker)
    assert all(docker.images[ref] == value for ref, value in shared.items())
    assert not any(
        row[0] == "tag"
        and row[-1]
        in {lifecycle.BASE_IMAGE, *lifecycle.FLAVOR_RECIPES, lifecycle.STABLE_RUNTIME_BASE_IMAGE}
        for row in docker.mutations
    )


@pytest.mark.parametrize(
    "parent",
    [
        "booley-sandbox-riscv",
        "booley-sandbox-riscv:latest",
        "docker.io/library/booley-sandbox-riscv:latest",
    ],
)
def test_matching_manual_recipe_stays_above_wheel_and_preserves_from(
    tmp_path, monkeypatch, parent
):
    root = _project(tmp_path, "booley-sandbox-riscv")
    docker = FakeDocker({})
    _bootstrap_base(monkeypatch, docker)
    recipe = root / ".booley_project/docker/Dockerfile"
    recipe.parent.mkdir()
    body = f"FROM {parent}\nRUN echo custom\n"
    recipe.write_text(body)
    planned = lifecycle.plan(lifecycle.ProjectImageScope(root), docker=docker)
    assert planned.nodes[-1].role is lifecycle.ImageRole.PROJECT_OVERLAY
    assert planned.nodes[-2].role is lifecycle.ImageRole.WHEEL_OVERLAY
    assert planned.nodes[-1].manual_parent == parent
    assert recipe.read_text() == body


@pytest.mark.parametrize(
    "body",
    [
        "FROM booley-sandbox\n",
        "FROM scratch\nFROM booley-sandbox-riscv\n",
        "# booley:parent=booley-sandbox-riscv\nFROM scratch\n",
    ],
)
def test_mismatched_or_false_manual_parent_is_ignored(tmp_path, monkeypatch, caplog, body):
    root = _project(tmp_path, "booley-sandbox-riscv")
    docker = FakeDocker({})
    _bootstrap_base(monkeypatch, docker)
    recipe = root / ".booley_project/docker/Dockerfile"
    recipe.parent.mkdir()
    recipe.write_text(body)
    planned = lifecycle.plan(lifecycle.ProjectImageScope(root), docker=docker)
    assert planned.nodes[-1].role is lifecycle.ImageRole.WHEEL_OVERLAY
    assert "recipe ignored" in caplog.text
    assert recipe.read_text() == body


def test_base_change_between_plan_and_prepare_fails_before_build(tmp_path, monkeypatch):
    root = _project(tmp_path)
    docker = FakeDocker({})
    _bootstrap_base(monkeypatch, docker)
    planned = lifecycle.plan(lifecycle.ProjectImageScope(root), docker=docker)
    old = docker.images[lifecycle.STABLE_RUNTIME_BASE_IMAGE]
    docker.images[lifecycle.STABLE_RUNTIME_BASE_IMAGE] = ("sha256:changed", old[1])
    builder = TransactionBuilder(docker)
    with pytest.raises(lifecycle.ImageLifecycleError, match="changed before preparation"):
        lifecycle.prepare(planned, docker=docker, builder=builder)
    assert builder.built == []


def test_external_selection_does_not_require_local_base(tmp_path):
    root = _project(tmp_path, "external:custom")
    docker = FakeDocker({"external:custom": ("sha256:external", {})})
    observed = lifecycle.plan(lifecycle.ProjectImageScope(root), docker=docker)
    assert lifecycle.planned_result(observed, docker).status is lifecycle.Status.EXTERNAL
    assert docker.mutations == []


def test_other_project_compatible_artifact_is_reused_without_shared_retag(tmp_path, monkeypatch):
    first = _project(tmp_path / "first")
    second = _project(tmp_path / "second")
    docker = FakeDocker({})
    _bootstrap_base(monkeypatch, docker)
    planned = lifecycle.plan(lifecycle.ProjectImageScope(first), docker=docker)
    _install_planned_graph(docker, planned.nodes)
    original = dict(docker.images)
    other = lifecycle.plan(lifecycle.ProjectImageScope(second), docker=docker)
    assert all(step.action is lifecycle.PlanAction.REUSE for step in other.steps)
    builder = TransactionBuilder(docker)
    prepared = lifecycle.prepare(other, docker=docker, builder=builder)
    lifecycle.commit(prepared, docker=docker)
    assert builder.built == []
    assert all(docker.images[ref] == value for ref, value in original.items())


def test_manual_final_scratch_cannot_claim_parent_with_labels(tmp_path, monkeypatch):
    from booley.harness import image_lifecycle as harness

    parent = {"RootFS": {"Layers": ["base", "wheel"]}}
    child = {"RootFS": {"Layers": []}}
    monkeypatch.setattr(
        harness.project_image,
        "inspect_layout_image",
        lambda ref: parent if ref == "parent" else child,
    )
    with pytest.raises(lifecycle.ImageLifecycleError, match="does not descend"):
        harness._IncrementalBuildAdapter._verify_manual_ancestry("child", "parent")


def test_legacy_journal_refuses_shared_restore_before_any_recovery(tmp_path, monkeypatch):
    from types import SimpleNamespace

    from booley.runtime import session_refresh as refresh

    root = _project(tmp_path)
    image = refresh.RefreshPreparedImage(
        lifecycle.BASE_IMAGE, "booley-lifecycle-old:candidate", "sha256:candidate", "sha256:prior"
    )
    journal = SimpleNamespace(project_root=root, prepared_images=(image,))
    monkeypatch.setattr(refresh, "_load_journal", lambda _root: journal)
    monkeypatch.setattr(
        refresh,
        "_restore_journal",
        lambda _journal: pytest.fail("legacy journal performed recovery"),
    )
    with pytest.raises(refresh.sr.SessionError, match="legacy image state"):
        refresh.recover_project_locked(root)


def test_never_initialized_project_check_does_not_create_files(tmp_path, monkeypatch):
    docker = FakeDocker({})
    _bootstrap_base(monkeypatch, docker)
    observed = lifecycle.plan(
        lifecycle.ProjectImageScope(tmp_path), docker=docker, intent=lifecycle.Intent.CHECK
    )
    assert lifecycle.planned_result(observed, docker).status is lifecycle.Status.STALE
    assert list(tmp_path.iterdir()) == []
    assert docker.mutations == []


@pytest.mark.parametrize("logical", ["booley-sandbox", "booley-sandbox-riscv"])
def test_complete_release_adopts_two_private_outputs_without_runtime_base(
    tmp_path, monkeypatch, logical
):
    monkeypatch.setattr(lifecycle.project_image, "project_data_alias_capable", lambda _id: False)
    docker = FakeDocker({})
    monkeypatch.setattr(lifecycle, "_expected_wheel_source_fingerprint", lambda: "wheel")
    shared = lifecycle._complete_release_node(logical, lifecycle._expected_image_build_contracts())
    labels = lifecycle._prepared_provenance(shared, None)
    labels.update(
        {
            lifecycle.LABEL_PARENT_ARTIFACT_KIND: lifecycle.PARENT_ARTIFACT_REGISTRY_DIGEST,
            lifecycle.LABEL_PARENT_ARTIFACT: "ghcr.io/boldaxolotl/booley-sandbox-base@sha256:"
            + "a" * 64,
            lifecycle.LABEL_WHEEL_SHA256: "f" * 64,
        }
    )
    docker.images[logical] = ("sha256:release", labels)
    for directory in ["first", "second"]:
        project = _project(tmp_path / directory, logical)
        observed = lifecycle.plan(
            lifecycle.ProjectImageScope(project),
            docker=docker,
            artifact_policy=lifecycle.ArtifactPolicy.VERIFIED_RELEASE_ONLY,
        )
        prepared = lifecycle.prepare(observed, docker=docker, builder=TransactionBuilder(docker))
        result = lifecycle.commit(prepared, docker=docker)
        assert result.selected_reference == project_image_name(project)
        assert result.selected_id == "sha256:release"
    assert docker.images[logical] == ("sha256:release", labels)
    assert lifecycle.STABLE_RUNTIME_BASE_IMAGE not in docker.images


def test_failed_private_tag_commit_restores_private_outputs_only(tmp_path, monkeypatch):
    root = _project(tmp_path)

    class FailedTagDocker(FakeDocker):
        fail = None

        def tag(self, source, target):
            if target == self.fail:
                self.fail = None
                raise lifecycle.ImageLifecycleError("adoption failed")
            super().tag(source, target)

    docker = FailedTagDocker({})
    _bootstrap_base(monkeypatch, docker)
    planned = lifecycle.plan(lifecycle.ProjectImageScope(root), docker=docker)
    _install_planned_graph(docker, planned.nodes)
    original = dict(docker.images)
    for record in original.values():
        docker.images[record[0]] = record
    planned = lifecycle.plan(
        lifecycle.ProjectImageScope(root), docker=docker, intent=lifecycle.Intent.REFRESH
    )
    prepared = lifecycle.prepare(planned, docker=docker, builder=TransactionBuilder(docker))
    docker.fail = planned.selected_reference
    docker.mutations.clear()
    with pytest.raises(lifecycle.ImageLifecycleError, match="adoption failed"):
        lifecycle.commit(prepared, docker=docker)
    assert all(docker.images[ref] == value for ref, value in original.items())
    assert not any(
        row[0] == "tag" and row[-1] == lifecycle.STABLE_RUNTIME_BASE_IMAGE
        for row in docker.mutations
    )


def test_stale_base_requires_bootstrap_and_is_never_repaired(tmp_path, monkeypatch):
    root = _project(tmp_path)
    docker = FakeDocker({})
    _bootstrap_base(monkeypatch, docker)
    docker.images[lifecycle.STABLE_RUNTIME_BASE_IMAGE][1][lifecycle.LABEL_EFFECTIVE_INPUTS] = "old"
    with pytest.raises(lifecycle.ImageLifecycleError, match="booley bootstrap --update"):
        lifecycle.plan(lifecycle.ProjectImageScope(root), docker=docker)
    assert docker.mutations == []


@pytest.mark.parametrize("base_state", ["missing", "stale", "unavailable"])
@pytest.mark.parametrize("force", [False, True])
def test_run_init_observes_bootstrap_only_and_fails_base_before_project_mutation(
    tmp_path, monkeypatch, base_state, force
):
    from contextlib import nullcontext
    from types import SimpleNamespace

    from booley.harness import image_lifecycle as harness
    from booley.harness import init_cmd
    from tests.harness.test_init_bootstrap import _args

    root = _project(tmp_path)
    docker = FakeDocker({})
    _bootstrap_base(monkeypatch, docker)
    if base_state == "missing":
        docker.images.clear()
    elif base_state == "stale":
        docker.images[lifecycle.STABLE_RUNTIME_BASE_IMAGE][1][lifecycle.LABEL_EFFECTIVE_INPUTS] = (
            "stale"
        )
    else:
        monkeypatch.setattr(
            docker,
            "image_id",
            lambda _image: (_ for _ in ()).throw(
                lifecycle.ImageLifecycleError("Docker unavailable")
            ),
        )
    intents = []
    monkeypatch.setattr(
        init_cmd,
        "reconcile_bootstrap",
        lambda intent, **_kw: intents.append(intent) or init_cmd.BootstrapResult(intent, ()),
    )
    monkeypatch.setattr(harness, "_docker_adapter", lambda: docker)
    monkeypatch.setattr(
        init_cmd, "_resolve_agent_selection", lambda *_args: SimpleNamespace(provider="codex")
    )
    monkeypatch.setattr(init_cmd, "_plan_existing_guidance", lambda _ctx: (None, True))
    monkeypatch.setattr(init_cmd, "init_project_dir_scope", lambda _root: nullcontext())
    monkeypatch.setattr(
        init_cmd,
        "_run_project_init_steps",
        lambda ctx, *_args, **_kw: (
            init_cmd._step_image_lifecycle(ctx),
            init_cmd._print_summary(ctx),
        )[-1],
    )
    assert init_cmd.run_init(_args(force=force), root) == 2
    assert intents == [lifecycle.Intent.CHECK]
    assert docker.mutations == []


def test_generated_file_symlink_is_user_owned(tmp_path):
    from booley.runtime import project_image

    target = tmp_path / "user-recipe"
    target.write_text("FROM scratch\n")
    recipe = tmp_path / "Dockerfile"
    recipe.symlink_to(target)
    assert not project_image.is_managed_generated_file(recipe)
    target.unlink()
    assert not project_image.is_managed_generated_file(recipe)


def test_external_node_preserves_layout_parent_provenance(tmp_path):
    root = _project(tmp_path, "external:custom")
    labels = {
        lifecycle.LABEL_WHEEL_SOURCE_FINGERPRINT: "wheel-source",
        lifecycle.LABEL_LOGICAL_SELECTION_FINGERPRINT: "logical-selection",
        lifecycle.LABEL_RUNTIME_BASE_CONTRACT: "runtime-contract",
        lifecycle.LABEL_STANDARD_SUBSTRATE_CONTRACT: "substrate-contract",
    }
    docker = FakeDocker({"external:custom": ("sha256:external", labels)})
    node = lifecycle.plan(lifecycle.ProjectImageScope(root), docker=docker).nodes[0]
    assert node.wheel_source_fingerprint == "wheel-source"
    assert node.logical_selection_fingerprint == "logical-selection"
    assert node.runtime_base_contract == "runtime-contract"
    assert node.standard_substrate_contract == "substrate-contract"
