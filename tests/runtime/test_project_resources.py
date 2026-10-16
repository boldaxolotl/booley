"""Exact resource proof and all-consumer protection at the Docker boundary."""

from pathlib import Path
from types import SimpleNamespace

import pytest
from tests.harness.test_image_lifecycle import resource_environment

from booley.core.project_identity import project_label_id, project_name_id
from booley.runtime import image_lifecycle as lifecycle
from booley.runtime import project_resources as resources


def _claim(root, docker, image):
    alias = resources._project_image_name(str(root))
    docker.tag(image, alias)
    resources.record_adoption(
        SimpleNamespace(
            plan=SimpleNamespace(project_root=root),
            candidates=(SimpleNamespace(reference=alias, image_id=image),),
            prior_tags=(),
        )
    )
    return alias


def test_audit_inventory_covers_project_resources_and_excludes_shared(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root, docker = resource_environment(tmp_path, monkeypatch)
    identity = str(root)
    own = next(iter(resources.volume_names(identity)))
    docker.volumes.update({own: {"Name": own}, "foreign": {"Name": "foreign"}})
    docker.containers["owned-container"] = {
        "Image": next(iter(docker.image_ids())),
        "Config": {"Labels": {"booley.project-id": project_label_id(identity)}},
        "Mounts": [],
    }
    docker.networks["owned-network"] = {
        "Labels": {"booley.project-id": project_label_id(identity)}
    }
    docker.networks["shared-network"] = {"Labels": {"booley.role": "shared-egress"}}
    report = resources.snapshot(identity, docker=docker)
    assert len(report.images) == 1 and report.images[0].size == 12345
    assert report.images[0].references == (resources._project_image_name(identity),)
    assert [row.name for row in report.volumes] == [own]
    assert [row.name for row in report.containers] == ["owned-container"]
    assert [row.name for row in report.networks] == ["owned-network"]
    assert report.networks[0].size == "N/A" and report.volumes[0].size is None


@pytest.mark.parametrize("consumer", ["container", "foreign-alias", "registry", "child"])
def test_purge_revalidates_usage_and_retains_foreign_aliases(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, consumer: str
) -> None:
    root, docker = resource_environment(tmp_path, monkeypatch)
    docker.engine_last_tag_deletion = True
    image = next(iter(docker.image_ids()))
    own = next(iter(resources.volume_names(str(root))))
    docker.volumes[own] = {"Name": own}
    if consumer == "container":
        docker.used_image_ids = frozenset({image})
        docker.containers["foreign-stopped"] = {"Image": image, "Mounts": [{"Name": own}]}
    elif consumer == "foreign-alias":
        docker.tag(image, "user-owned:latest")
    elif consumer == "registry":
        docker.registry_identities[image] = ("registry/image@" + image,)
    else:
        child = "sha256:" + "f" * 64
        docker.images[child] = (child, {lifecycle.LABEL_PARENT_ARTIFACT: image})
    resources.purge(str(root), docker=docker)
    assert image in docker.image_ids()
    assert ("remove_tag", image) not in docker.mutations
    assert (own in docker.volumes) == (consumer == "container")


def test_inherited_origin_without_acquisition_proof_is_retained(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root, docker = resource_environment(tmp_path, monkeypatch)
    parent = next(iter(docker.image_ids()))
    derivative = "sha256:" + "d" * 64
    docker.images[derivative] = (derivative, dict(docker.images[parent][1]))
    resources.purge(str(root), docker=docker)
    assert derivative in docker.image_ids()
    assert "unproven" in resources.snapshot(str(root), docker=docker).images[0].reason


def test_two_projects_release_only_their_acquired_aliases(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root, docker = resource_environment(tmp_path, monkeypatch)
    docker.engine_last_tag_deletion = True
    image = next(iter(docker.image_ids()))
    other = tmp_path / "other"
    other_alias = _claim(other, docker, image)
    resources.purge(str(root), docker=docker)
    assert docker.image_id(other_alias) == image
    assert image in docker.image_ids()
    assert resources.proven_origin(image, docker)


@pytest.mark.parametrize("reverse", [False, True])
@pytest.mark.parametrize("child", [False, True])
def test_last_project_claimant_collects_shared_origin_after_both_purges(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, reverse: bool, child: bool
) -> None:
    root, docker = resource_environment(tmp_path, monkeypatch)
    docker.engine_last_tag_deletion = True
    image = next(iter(docker.image_ids()))
    other = tmp_path / "other"
    _claim(other, docker, image)
    derivative = "sha256:" + "f" * 64
    if child:
        docker.images[derivative] = (derivative, {lifecycle.LABEL_PARENT_ARTIFACT: image})
    first, last = (other, root) if reverse else (root, other)
    resources.purge(str(first), docker=docker)
    assert image in docker.image_ids() and resources.proven_origin(image, docker)
    resources.purge(str(last), docker=docker)
    assert (image in docker.image_ids()) == child
    if child:
        docker.images.pop(derivative)
        resources.purge(str(first), docker=docker)
        resources.purge(str(last), docker=docker)
        assert image not in docker.image_ids()
    assert not any(image in raw["origins"] for raw in resources._records())


def test_stored_identity_survives_ancestor_symlink_retarget(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    ancestor = tmp_path / "owner"
    root, docker = resource_environment(tmp_path, monkeypatch, root=ancestor / "project")
    identity = str(root)
    resources.retain_cleanup_identity(identity)
    volume = next(iter(resources.volume_names(identity)))
    docker.volumes[volume] = {"Name": volume}
    moved, decoy = tmp_path / "moved", tmp_path / "decoy"
    ancestor.rename(moved)
    (decoy / "project").mkdir(parents=True)
    try:
        ancestor.symlink_to(decoy, target_is_directory=True)
    except OSError as exc:
        pytest.skip(f"symlinks unavailable: {exc}")
    decoy_volume = next(iter(resources.volume_names(str(root.resolve()))))
    docker.volumes[decoy_volume] = {"Name": decoy_volume}
    assert resources.has_cleanup_identity(identity)
    resources.purge(identity, docker=docker)
    assert volume not in docker.volumes and decoy_volume in docker.volumes
    assert not docker.image_ids()


def test_report_failure_precedes_purge_mutation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root, docker = resource_environment(tmp_path, monkeypatch)
    before = dict(docker.images)

    def broken(_kind):
        raise resources.ResourceError("incomplete inventory")

    monkeypatch.setattr(resources, "_inventory", broken)
    with pytest.raises(resources.ResourceError, match="incomplete inventory"):
        resources.purge(str(root), docker=docker)
    assert docker.images == before


def test_report_includes_historical_project_label(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root, docker = resource_environment(tmp_path, monkeypatch)
    docker.containers["historical"] = {
        "Image": next(iter(docker.image_ids())),
        "Config": {"Labels": {"booley.project-id": project_name_id(str(root))}},
        "Mounts": [],
    }
    assert [row.name for row in resources.snapshot(str(root), docker=docker).containers] == [
        "historical"
    ]


def test_proof_compacts_only_after_artifact_disappears(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root, docker = resource_environment(tmp_path, monkeypatch)
    image = next(iter(docker.image_ids()))
    docker.tag(image, "user:keep")
    resources.purge(str(root), docker=docker)
    assert resources.proven_origin(image, docker)
    docker.remove_tag("user:keep")
    resources.purge(str(root), docker=docker)
    assert image not in docker.image_ids()
    assert all(image not in raw["origins"] for raw in resources._records())


@pytest.mark.parametrize("corruption", ["schema", "dangling-symlink"])
def test_invalid_retained_identity_fails_closed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, corruption: str
) -> None:
    root, docker = resource_environment(tmp_path, monkeypatch)
    resources.retain_cleanup_identity(str(root))
    store = resources._store()
    name = resources._name(str(root))
    before = dict(docker.images)
    if corruption == "schema":
        store.atomic_write_text(name, '{"version": true}')
    else:
        (store.root / name).unlink()
        try:
            (store.root / name).symlink_to(store.root / "absent")
        except OSError as exc:
            pytest.skip(f"symlinks unavailable: {exc}")
    with pytest.raises(resources.ResourceError):
        resources.has_cleanup_identity(str(root))
    assert before == docker.images


def test_keeper_observation_never_becomes_an_acquisition_or_alias_claim(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from booley.runtime.session_issuance import keeper_image_for_identity

    root, docker = resource_environment(tmp_path, monkeypatch)
    image = next(iter(docker.image_ids()))
    identity = str(tmp_path / "other")
    keeper = keeper_image_for_identity(identity)
    docker.tag(image, keeper)
    resources.retain_cleanup_identity(identity)
    resources.observe_keeper(identity, docker)
    replacement = "sha256:" + "f" * 64
    docker.images[replacement] = (replacement, {})
    alias = resources._project_image_name(str(root))
    # Retagging leaves old bytes dangling; deleting their last tag would not.
    docker.tag(replacement, alias)
    resources.record_adoption(
        SimpleNamespace(
            plan=SimpleNamespace(project_root=root),
            candidates=(SimpleNamespace(reference=alias, image_id=replacement),),
            prior_tags=(),
        )
    )
    docker.tag(replacement, keeper)
    assert resources.proven_origin(image, docker)
    assert resources.snapshot(identity, docker=docker).images[0].image_id == image
    resources.purge(identity, docker=docker)
    assert image in docker.image_ids()
    assert ("remove_tag", image) not in docker.mutations


def test_corrupt_claim_cannot_widen_alias_authority(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import json

    root, docker = resource_environment(tmp_path, monkeypatch)
    image = next(iter(docker.image_ids()))
    docker.tag(image, "user:foreign")
    store = resources._store()
    raw = resources._load(str(root))
    raw["aliases"]["user:foreign"] = image
    store.atomic_write_text(resources._name(str(root)), json.dumps(raw))
    before = dict(docker.images)
    with pytest.raises(resources.ResourceError, match="namespace"):
        resources.purge(str(root), docker=docker)
    assert docker.images == before


def _strict_chain(tmp_path, monkeypatch, *, owned_child):
    root, docker = resource_environment(tmp_path, monkeypatch)
    docker.engine_last_tag_deletion = True
    parent = next(iter(docker.image_ids()))
    child = "sha256:" + "c" * 64
    labels = dict(docker.images[parent][1])
    labels[lifecycle.LABEL_PARENT_ARTIFACT] = parent
    docker.images[child] = (child, labels)
    if owned_child:
        resources.record_acquisition(str(root), (child,), docker)
        alias = resources._project_image_name(str(root)) + "-child"
        docker.tag(child, alias)
        resources.record_adoption(
            SimpleNamespace(
                plan=SimpleNamespace(project_root=root),
                candidates=(SimpleNamespace(reference=alias, image_id=child),),
                prior_tags=(),
            )
        )
    return root, docker, parent, child


def test_last_tag_purge_preserves_registry_identity_and_bytes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root, docker = resource_environment(tmp_path, monkeypatch)
    docker.engine_last_tag_deletion = True
    image = next(iter(docker.image_ids()))
    digest = "registry/image@" + image
    docker.registry_identities[image] = (digest,)
    resources.purge(str(root), docker=docker)
    assert image in docker.image_ids(), "registry-protected bytes were removed by last-tag cleanup"
    assert docker.repo_digests(image) == (digest,)


def test_last_tag_purge_cleans_owned_chain_leaves_before_parents(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root, docker, parent, child = _strict_chain(tmp_path, monkeypatch, owned_child=True)
    failure = None
    try:
        resources.purge(str(root), docker=docker)
    except resources.ResourceError as exc:
        failure = str(exc)
    assert failure is None, "owned leaf chain cleanup did not finish: " + str(failure)
    assert parent not in docker.image_ids() and child not in docker.image_ids()


def test_last_tag_purge_retains_foreign_child_without_failing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root, docker, parent, child = _strict_chain(tmp_path, monkeypatch, owned_child=False)
    failure = None
    try:
        resources.purge(str(root), docker=docker)
    except resources.ResourceError as exc:
        failure = str(exc)
    assert failure is None, "protected child made purge fail: " + str(failure)
    assert parent in docker.image_ids() and child in docker.image_ids()
    assert docker.image_id(resources._project_image_name(str(root))) == parent


def test_last_tag_missing_other_project_alias_keeps_durable_claim(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root, docker = resource_environment(tmp_path, monkeypatch)
    docker.engine_last_tag_deletion = True
    image = next(iter(docker.image_ids()))
    other = tmp_path / "other"
    alias = _claim(other, docker, image)
    docker.remove_tag(alias)
    resources.purge(str(root), docker=docker)
    assert image in docker.image_ids(), "missing shared alias claim lost its image bytes"
    resources.purge(str(other), docker=docker)
    assert image in docker.image_ids()
    resources.purge(str(root), docker=docker)
    assert image not in docker.image_ids()


@pytest.mark.parametrize("multiple_tags", [False, True])
def test_last_tag_unproven_aliases_remain_without_created_id_authority(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, multiple_tags: bool
) -> None:
    root, docker = resource_environment(tmp_path, monkeypatch)
    docker.engine_last_tag_deletion = True
    labels = dict(next(iter(docker.images.values()))[1])
    docker.images.clear()
    image = "sha256:" + "e" * 64
    docker.images[image] = (image, labels)
    alias = _claim(root, docker, image)
    if multiple_tags:
        docker.tag(image, "foreign:alias")
    resources.purge(str(root), docker=docker)
    assert image in docker.image_ids() and docker.image_id(alias) == image
    assert ("remove_tag", alias) not in docker.mutations


def test_exact_image_removal_is_bounded_and_nonforce(monkeypatch: pytest.MonkeyPatch) -> None:
    import subprocess

    observed = {}
    image = "sha256:" + "a" * 64

    def timeout(command, **kwargs):
        observed.update(command=command, **kwargs)
        raise subprocess.TimeoutExpired(command, kwargs.get("timeout", 0))

    monkeypatch.setattr(lifecycle.subprocess, "run", timeout)
    with pytest.raises(lifecycle.ImageLifecycleError, match="could not remove"):
        lifecycle._DockerCli().remove_tag(image)
    assert observed["timeout"] == 30
    assert observed["command"] == ["docker", "image", "rm", "--no-prune", image]


def test_absent_alias_claim_does_not_block_purge_retry(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root, docker = resource_environment(tmp_path, monkeypatch)
    image = next(iter(docker.image_ids()))
    docker.images.clear()
    layers = docker.image_layers

    def strict_layers(identity: str) -> tuple[str, ...]:
        if identity not in docker.image_ids():
            raise resources.ResourceError("image no longer exists")
        return layers(identity)

    monkeypatch.setattr(docker, "image_layers", strict_layers)
    try:
        resources.purge(str(root), docker=docker)
    except RuntimeError as exc:
        pytest.fail(f"absent claim made purge fail: {exc}")
    assert image not in docker.image_ids()
    assert not resources._load(str(root))["aliases"]


def test_verbose_docker_sizes_match_untruncated_container_identity(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import json

    from booley.core.project_identity import project_label_id

    root, docker = resource_environment(tmp_path, monkeypatch)
    image = next(iter(docker.image_ids()))
    container = "c" * 64
    volume = next(iter(resources.volume_names(str(root))))
    docker.containers[container] = {
        "Image": image,
        "Mounts": [],
        "Config": {"Labels": {"booley.project-id": project_label_id(str(root))}},
    }
    docker.volumes[volume] = {"Name": volume}
    original = resources._docker

    def output(args: list[str]) -> str:
        if args[:2] == ["system", "df"]:
            # Docker CLI formatter/disk_usage.go verboseWrite plus ContainerContext.ID:
            # JSON template contexts use full IDs; ordinary quiet ls truncates them.
            return json.dumps(
                {
                    "Images": [{"ID": image, "Size": "12.35kB"}],
                    "Containers": [{"ID": container, "Size": "17B"}],
                    "Volumes": [{"Name": volume, "Size": "128B"}],
                }
            )
        if args[:2] == ["container", "ls"]:
            return container if "--no-trunc" in args else container[:12]
        if args[:2] == ["container", "inspect"]:
            return json.dumps([docker.containers[container]])
        return original(args)

    monkeypatch.setattr(resources, "_docker", output)
    report = resources.snapshot(str(root), docker=docker)
    assert report.containers[0].size == "17B", "verbose size lost by truncated container identity"
    assert report.volumes[0].size == "128B"
    assert report.images[0].size == 12345
