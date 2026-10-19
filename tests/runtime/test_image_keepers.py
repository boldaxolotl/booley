"""Refresh ownership survives keeper release and process restarts."""

from pathlib import Path
from unittest.mock import Mock

from booley.runtime import image_keepers, image_lifecycle


def test_forget_retries_only_forgotten_project_proof(tmp_path: Path, monkeypatch):
    project = tmp_path / "project"
    project.mkdir()
    monkeypatch.setattr(image_keepers, "_require_recovered_host", lambda: None)
    release = image_keepers.KeeperRelease("absent", "keeper")
    monkeypatch.setattr(image_keepers, "_release_root", lambda _root: release)

    def forget(root, *, before_forget, allow_forgotten):
        before_forget(root)
        return root

    monkeypatch.setattr(image_keepers.inventory, "forget_project", forget)
    cleanup = Mock(return_value=())
    monkeypatch.setattr(image_lifecycle, "cleanup_owned_images", cleanup)
    assert image_keepers.forget_project(project) == (project, release, None)
    cleanup.assert_called_once_with(project)
