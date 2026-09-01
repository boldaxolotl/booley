"""Focused init ownership tests for the production FlexNet relay image/orphans."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

from booley.eda.provisioning.licensing.flexnet_docker import resources_for_session
from booley.harness import init_cmd


def test_unlicensed_reseed_removes_exact_orphan_topology(tmp_path: Path) -> None:
    resources = resources_for_session(str(tmp_path.resolve()))
    with (
        patch.object(
            init_cmd.idk,
            "container_exists",
            side_effect=lambda name: name == resources.relay_container,
        ),
        patch("booley.eda.provisioning.licensing.flexnet_docker.remove_relay") as remove,
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
        patch("booley.eda.provisioning.licensing.flexnet_docker.remove_relay") as remove,
    ):
        assert init_cmd._cleanup_unlicensed_relay(tmp_path) is True
    remove.assert_called_once()
