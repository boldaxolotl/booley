"""Refresh ownership survives keeper release and process restarts."""

from pathlib import Path
from unittest.mock import Mock

from booley.runtime import image_keepers, image_lifecycle


def test_forget_retries_only_forgotten_project_proof(tmp_path: Path, monkeypatch):
    from booley.runtime import project_resources

    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    monkeypatch.setattr(
        project_resources, "snapshot", lambda *_a, **_k: project_resources.ResourceReport()
    )
    project = tmp_path / "project"
    project.mkdir()
    monkeypatch.setattr(image_keepers, "_require_recovered_host", lambda: None)
    release = image_keepers.KeeperRelease("absent", "keeper")
    monkeypatch.setattr(image_keepers, "_release_root", lambda _root: release)

    def forget(root, *, before_forget):
        before_forget(root)
        return root

    monkeypatch.setattr(image_keepers.inventory, "forget_project", forget)
    adapter = Mock()
    adapter.image_id.return_value = None
    adapter.image_ids.return_value = frozenset()
    monkeypatch.setattr(image_lifecycle, "_docker_adapter", lambda: adapter)
    cleanup = Mock(return_value=())
    monkeypatch.setattr(image_lifecycle, "cleanup_owned_images", cleanup)
    root, outcome = image_keepers.forget_project(project)
    assert root == project and outcome.status == release.status
    assert outcome.resources is not None
    cleanup.assert_called_once_with(project, docker=image_lifecycle._docker_adapter())


def test_purge_failure_is_retryable_and_preserves_inventory(tmp_path, monkeypatch):
    import pytest
    from tests.test_project_inventory_cli import _resource_project

    from booley.projects import inventory
    from booley.runtime import project_resources

    root, docker = _resource_project(tmp_path, monkeypatch)
    from booley.runtime.session_issuance import keeper_image

    keeper = keeper_image(root)
    docker.tag(next(iter(docker.image_ids())), keeper)
    volume = next(iter(docker.volumes))
    docker.fail_remove = volume
    with pytest.raises(inventory.ProjectInventoryError, match="removal denied") as failure:
        image_keepers.forget_project(root, purge=True)
    assert "keeper released: " + keeper in str(failure.value)
    assert inventory.project_inventory()[0].remembered
    assert project_resources.has_cleanup_identity(str(root))
    docker.fail_remove = None
    image_keepers.forget_project(root, purge=True)
    assert inventory.project_inventory() == () and not docker.volumes
    assert not project_resources.has_cleanup_identity(str(root))


def test_retained_cleanup_identity_survives_partial_failure(tmp_path, monkeypatch):
    import pytest
    from tests.test_project_inventory_cli import _resource_project

    from booley.projects import inventory
    from booley.runtime import project_resources

    root, docker = _resource_project(tmp_path, monkeypatch)
    image_keepers.forget_project(root)
    docker.fail_remove = next(iter(docker.volumes))
    with pytest.raises(inventory.ProjectInventoryError, match="removal denied"):
        image_keepers.forget_project(root, purge=True)
    assert inventory.project_inventory() == ()
    assert project_resources.has_cleanup_identity(str(root))
    docker.fail_remove = None
    image_keepers.forget_project(root, purge=True)
    assert not project_resources.has_cleanup_identity(str(root))


def test_keeper_only_historical_image_is_reported_without_new_deletion_authority(
    tmp_path, monkeypatch
):
    from tests.test_project_inventory_cli import _resource_project

    from booley.runtime import project_resources
    from booley.runtime.session_issuance import keeper_image

    root, docker = _resource_project(tmp_path, monkeypatch)
    docker.images.clear()
    historical = "sha256:" + "e" * 64
    docker.images[historical] = (historical, {})
    keeper = keeper_image(root)
    docker.tag(historical, keeper)
    docker.used_image_ids = frozenset({historical})
    docker.containers["foreign-stopped"] = {"Image": historical, "Mounts": []}
    _, outcome = image_keepers.forget_project(root)
    assert outcome.status == "retained-in-use"
    assert outcome.resources is not None
    assert any(
        row["image_id"] == historical and keeper in row["references"]
        for row in outcome.resources["images"]
    )
    assert project_resources.has_cleanup_identity(str(root))
    docker.used_image_ids = frozenset()
    docker.containers.clear()
    image_keepers.forget_project(root, purge=True)
    assert historical in docker.image_ids()
    assert project_resources.has_cleanup_identity(str(root))
    assert ("remove_tag", historical) not in docker.mutations
    docker.images.pop(historical)
    image_keepers.forget_project(root, purge=True)
    assert not project_resources.has_cleanup_identity(str(root))
