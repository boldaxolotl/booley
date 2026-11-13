"""Forgetting a Project cleans the images owned by the root inventory forgot."""

from pathlib import Path
from unittest.mock import Mock

from booley.runtime import image_keepers, image_lifecycle


def test_forget_cleans_images_of_the_forgotten_root(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))  # withdrawal stays private
    project = tmp_path / "project"
    forgotten = tmp_path / "canonical-root"  # inventory may canonicalize the requested path
    monkeypatch.setattr(image_keepers, "_require_recovered_host", lambda: None)
    monkeypatch.setattr(
        image_keepers, "_release_root", lambda _root: image_keepers.KeeperRelease("absent", "k")
    )

    def forget(_root, *, before_forget, allow_forgotten):
        before_forget(forgotten)
        return forgotten

    monkeypatch.setattr(image_keepers.inventory, "forget_project", forget)
    cleanup = Mock(return_value=())
    monkeypatch.setattr(image_lifecycle, "cleanup_owned_images", cleanup)
    image_keepers.forget_project(project)
    cleanup.assert_called_once_with(forgotten)
