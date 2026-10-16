"""Exact resource proof and all-consumer protection at the Docker boundary."""

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


def test_audit_inventory_covers_project_resources_and_excludes_shared(tmp_path, monkeypatch):
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
    assert [row.name for row in report.volumes] == [own]
    assert [row.name for row in report.containers] == ["owned-container"]
    assert [row.name for row in report.networks] == ["owned-network"]
    assert report.networks[0].size == "N/A" and report.volumes[0].size is None


@pytest.mark.parametrize("consumer", ["container", "foreign-alias", "registry", "child"])
def test_purge_revalidates_usage_and_retains_foreign_aliases(tmp_path, monkeypatch, consumer):
    root, docker = resource_environment(tmp_path, monkeypatch)
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


def test_inherited_origin_without_acquisition_proof_is_retained(tmp_path, monkeypatch):
    root, docker = resource_environment(tmp_path, monkeypatch)
    parent = next(iter(docker.image_ids()))
    derivative = "sha256:" + "d" * 64
    docker.images[derivative] = (derivative, dict(docker.images[parent][1]))
    resources.purge(str(root), docker=docker)
    assert derivative in docker.image_ids()
    assert "unproven" in resources.snapshot(str(root), docker=docker).images[0].reason


def test_two_projects_release_only_their_acquired_aliases(tmp_path, monkeypatch):
    root, docker = resource_environment(tmp_path, monkeypatch)
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
    tmp_path, monkeypatch, reverse, child
):
    root, docker = resource_environment(tmp_path, monkeypatch)
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
        resources.purge(str(last), docker=docker)
        assert image not in docker.image_ids()
    assert not any(image in raw["origins"] for raw in resources._records())


def test_stored_identity_survives_ancestor_symlink_retarget(tmp_path, monkeypatch):
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


def test_report_failure_precedes_purge_mutation(tmp_path, monkeypatch):
    root, docker = resource_environment(tmp_path, monkeypatch)
    before = dict(docker.images)

    def broken(_kind):
        raise resources.ResourceError("incomplete inventory")

    monkeypatch.setattr(resources, "_inventory", broken)
    with pytest.raises(resources.ResourceError, match="incomplete inventory"):
        resources.purge(str(root), docker=docker)
    assert docker.images == before


def test_report_includes_historical_project_label(tmp_path, monkeypatch):
    root, docker = resource_environment(tmp_path, monkeypatch)
    docker.containers["historical"] = {
        "Image": next(iter(docker.image_ids())),
        "Config": {"Labels": {"booley.project-id": project_name_id(str(root))}},
        "Mounts": [],
    }
    assert [row.name for row in resources.snapshot(str(root), docker=docker).containers] == [
        "historical"
    ]


def test_proof_compacts_only_after_artifact_disappears(tmp_path, monkeypatch):
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
def test_invalid_retained_identity_fails_closed(tmp_path, monkeypatch, corruption):
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
