"""Focused init ownership tests for the production FlexNet relay image/orphans."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

from booley.eda.flexnet_docker import resources_for_session
from booley.harness import init_cmd
from booley.harness.init_common import InitContext


def test_wheel_install_reuses_existing_sidecar_image() -> None:
    with patch.object(init_cmd.idk, "image_exists", return_value=True) as exists:
        ready = init_cmd._ensure_sidecar_image(
            None,
            lambda *_args, **_kwargs: False,
            image="booley-sidecar",
            force=False,
        )

    assert ready
    exists.assert_called_once_with("booley-sidecar")


def test_licensed_interactive_init_ensures_relay_image(tmp_path: Path) -> None:
    ctx = InitContext(project_root=tmp_path)
    with (
        patch("booley.eda.flexnet_docker.ensure_relay_image", return_value=True) as ensure,
        patch.object(init_cmd, "_booley_repo_root", return_value=None),
        patch.object(init_cmd.idk, "ensure_egress_network", return_value=False),
    ):
        notes = init_cmd._ensure_interactive_docker(ctx, license_required=True)

    ensure.assert_called_once_with(force=False)
    assert "license-relay-image:built" in notes


def test_unlicensed_reseed_removes_exact_orphan_topology(tmp_path: Path) -> None:
    resources = resources_for_session(str(tmp_path.resolve()))
    with (
        patch.object(
            init_cmd.idk,
            "container_exists",
            side_effect=lambda name: name == resources.relay_container,
        ),
        patch("booley.eda.flexnet_docker.remove_relay") as remove,
    ):
        assert init_cmd._cleanup_unlicensed_relay(tmp_path) is True

    remove.assert_called_once_with(resources)


def test_unlicensed_reseed_is_silent_without_orphan(tmp_path: Path) -> None:
    with (
        patch.object(init_cmd.idk, "container_exists", return_value=False),
        patch.object(init_cmd.idk, "network_exists", return_value=False),
    ):
        assert init_cmd._cleanup_unlicensed_relay(tmp_path) is False


def test_unlicensed_reseed_removes_network_only_orphan(tmp_path: Path) -> None:
    with (
        patch.object(init_cmd.idk, "container_exists", return_value=False),
        patch.object(init_cmd.idk, "network_exists", side_effect=[True]),
        patch("booley.eda.flexnet_docker.remove_relay") as remove,
    ):
        assert init_cmd._cleanup_unlicensed_relay(tmp_path) is True
    remove.assert_called_once()
