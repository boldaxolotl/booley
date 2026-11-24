"""Forgetting a Project cleans the images owned by the root inventory forgot."""

from pathlib import Path
from unittest.mock import Mock

from booley.runtime import image_keepers, image_lifecycle


def test_forget_cleans_images_of_the_forgotten_root(tmp_path: Path, monkeypatch, caplog):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))  # withdrawal stays private
    project = tmp_path / "project"
    forgotten = tmp_path / "canonical-root"  # inventory may canonicalize the requested path
    # forget_project reaches _require_recovered_host and _release_root only through a real
    # keeper Docker host, so the two private patches replace it here. This test pins the
    # root passed to cleanup and the warning filter; tests/test_project_inventory_cli.py
    # runs the public forget path against a fake keeper host.
    monkeypatch.setattr(image_keepers, "_require_recovered_host", lambda: None)
    monkeypatch.setattr(
        image_keepers, "_release_root", lambda _root: image_keepers.KeeperRelease("absent", "k")
    )

    def forget(_root, *, before_forget, allow_forgotten):
        before_forget(forgotten)
        return forgotten

    monkeypatch.setattr(image_keepers.inventory, "forget_project", forget)
    tagged, refused, failed = (f"sha256:{c * 64}" for c in "abc")
    refusal = f"retained (conflict: unable to delete {refused})"
    cleanup = Mock(
        return_value=(
            f"{tagged}: retained (tag/keeper)",
            f"{refused}: {refusal}",
            f"{failed}: deferred (daemon unavailable)",
        )
    )
    monkeypatch.setattr(image_lifecycle, "cleanup_owned_images", cleanup)
    image_keepers.forget_project(project)
    cleanup.assert_called_once_with(forgotten)
    # A tagged image is already in forget's leftover table; no "refresh" wording.
    assert [r.getMessage() for r in caplog.records if r.name == image_keepers.__name__] == [
        f"forget image cleanup: {refused}: {refusal}",
        f"forget image cleanup: {failed}: deferred (daemon unavailable)",
    ]
