"""Focused init ownership tests for the production FlexNet relay image/orphans."""

from __future__ import annotations

from pathlib import Path

import pytest

from booley.eda.provisioning.licensing.flexnet_docker import resources_for_session
from booley.harness.setup import interactive as interactive_init
from tests.harness.session_docker_fake import serve_sandbox_docker


@pytest.mark.parametrize("orphan", ["relay_container", "private_network", None])
def test_unlicensed_reseed_removes_only_an_orphan_topology(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, orphan: str | None
) -> None:
    resources = resources_for_session(str(tmp_path.resolve()))
    docker = serve_sandbox_docker(monkeypatch)
    if orphan == "relay_container":
        docker.containers[resources.relay_container] = {"Id": "id-relay"}
    elif orphan == "private_network":
        docker.networks.add(resources.private_network)

    # private: its only caller, `interactive.apply`, runs after Sandbox Issuance,
    # which needs a Docker-backed Sandbox Image.
    assert interactive_init._remove_unlicensed_relay(tmp_path) is (orphan is not None)

    removals = [argv[1:] for argv in docker.calls if "rm" in argv[1:3]]
    # The relay goes first, then its two networks, in reverse creation order.
    assert removals == (
        []
        if orphan is None
        else [
            ["container", "rm", "-f", resources.relay_container],
            ["network", "rm", resources.outbound_network],
            ["network", "rm", resources.private_network],
        ]
    )
