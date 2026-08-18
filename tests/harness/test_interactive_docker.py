"""Tests for the Interactive Mode Docker lifecycle helpers (ADR 0018 WS1/WS2).

The ``docker`` CLI is mocked at the single ``_run_docker`` seam, so these run
without Docker installed.
"""

from __future__ import annotations

import json
import subprocess

import pytest

from booley.harness import interactive_docker as idk
from booley.harness.config import InteractiveConfig


def _cp(returncode: int = 0, stdout: str = "", stderr: str = "") -> subprocess.CompletedProcess:
    return subprocess.CompletedProcess(
        args=["docker"], returncode=returncode, stdout=stdout, stderr=stderr
    )


class FakeDocker:
    """Records docker invocations and replies from a rules table.

    *rules* maps a matching predicate (callable on the args list) to a
    CompletedProcess. First match wins; unmatched calls return success.
    """

    def __init__(self, rules=None):
        self.calls: list[list[str]] = []
        self.rules = rules or []

    def __call__(self, args, *, timeout=30):
        self.calls.append(list(args))
        for predicate, response in self.rules:
            if predicate(args):
                return response
        return _cp(0)

    def ran(self, *needles: str) -> bool:
        """True if any recorded call contains all *needles* as a subsequence-ish match."""
        return any(all(n in call for n in needles) for call in self.calls)


@pytest.fixture
def fake_docker(monkeypatch):
    def install(rules=None):
        fd = FakeDocker(rules)
        monkeypatch.setattr(idk, "_run_docker", fd)
        return fd

    return install


class TestIssuedImageKeepers:
    def test_tag_image_surfaces_docker_failure(self, fake_docker):
        fake_docker([(lambda args: args[:2] == ["image", "tag"], _cp(1, stderr="no space"))])
        with pytest.raises(RuntimeError, match="no space"):
            idk.tag_image("sha256:abc", "booley-issued-" + "a" * 64 + ":session")

    def test_lists_only_well_formed_keeper_tags(self, fake_docker):
        keeper = "booley-issued-" + "a" * 64 + ":session"
        fake_docker(
            [
                (
                    lambda args: args[:2] == ["image", "ls"],
                    _cp(0, stdout=f"{keeper}\nubuntu:latest\nbooley-issued-bad:session\n"),
                )
            ]
        )
        assert idk.issued_image_tags() == [keeper]


# ===========================================================================
# Network
# ===========================================================================


class TestEgressNetwork:
    def test_creates_internal_network_when_missing(self, fake_docker):
        fd = fake_docker(
            [
                (lambda a: a[:2] == ["network", "inspect"], _cp(1, stderr="No such network")),
            ]
        )
        created = idk.ensure_egress_network()
        assert created is True
        assert fd.ran("network", "create", "--internal", idk.EGRESS_NETWORK)
        assert fd.ran(
            "--opt",
            f"{idk.GATEWAY_MODE_OPTION}={idk.GATEWAY_MODE_ISOLATED}",
        )

    def test_idempotent_when_present(self, fake_docker):
        fd = fake_docker(
            [
                (
                    lambda a: a[:2] == ["network", "inspect"] and "{{.Internal}}" in a,
                    _cp(0, stdout="true\n"),
                ),
                (
                    lambda a: a[:2] == ["network", "inspect"] and "{{json .Options}}" in a,
                    _cp(
                        0,
                        stdout=json.dumps({idk.GATEWAY_MODE_OPTION: idk.GATEWAY_MODE_ISOLATED}),
                    ),
                ),
                (lambda a: a[:2] == ["network", "inspect"], _cp(0)),
            ]
        )
        assert idk.ensure_egress_network() is False
        assert not fd.ran("network", "create")

    def test_rejects_legacy_internal_network_with_host_gateway(self, fake_docker):
        fake_docker(
            [
                (
                    lambda a: a[:2] == ["network", "inspect"] and "{{.Internal}}" in a,
                    _cp(0, stdout="true\n"),
                ),
                (
                    lambda a: a[:2] == ["network", "inspect"] and "{{json .Options}}" in a,
                    _cp(0, stdout="{}\n"),
                ),
                (lambda a: a[:2] == ["network", "inspect"], _cp(0)),
            ]
        )
        with pytest.raises(RuntimeError, match="unsafe or stale routing policy"):
            idk.ensure_egress_network()

    def test_raises_on_create_failure(self, fake_docker):
        fake_docker(
            [
                (lambda a: a[:2] == ["network", "inspect"], _cp(1)),
                (lambda a: a[:2] == ["network", "create"], _cp(1, stderr="boom")),
            ]
        )
        with pytest.raises(RuntimeError, match="failed to create network"):
            idk.ensure_egress_network()

    def test_network_is_internal_probe(self, fake_docker):
        fake_docker(
            [
                (lambda a: a[:2] == ["network", "inspect"], _cp(0, stdout="true\n")),
            ]
        )
        assert idk.network_is_internal() is True

    def test_network_is_host_isolated_probe(self, fake_docker):
        fake_docker(
            [
                (
                    lambda a: a[:2] == ["network", "inspect"],
                    _cp(
                        0,
                        stdout=json.dumps({idk.GATEWAY_MODE_OPTION: idk.GATEWAY_MODE_ISOLATED}),
                    ),
                ),
            ]
        )
        assert idk.network_is_host_isolated() is True


# ===========================================================================
# Egress proxy image
# ===========================================================================


class TestProxyImage:
    def test_skips_build_when_image_present(self, fake_docker, tmp_path):
        fd = fake_docker(
            [
                (lambda a: a[:2] == ["image", "inspect"], _cp(0)),
            ]
        )
        assert idk.ensure_egress_proxy_image(tmp_path) is True
        assert not fd.ran("build")

    def test_builds_when_missing(self, fake_docker, tmp_path):
        # Provide the Dockerfile so the build path proceeds.
        df = tmp_path / "src" / "booley" / "data" / "docker"
        df.mkdir(parents=True)
        (df / "Dockerfile.egress-proxy").write_text("FROM python:3.13-slim\n")
        fd = fake_docker(
            [
                (lambda a: a[:2] == ["image", "inspect"], _cp(1)),
            ]
        )
        assert idk.ensure_egress_proxy_image(tmp_path) is True
        assert fd.ran("build", "-t", idk.PROXY_IMAGE)

    def test_build_fails_without_dockerfile(self, fake_docker, tmp_path):
        fake_docker([(lambda a: a[:2] == ["image", "inspect"], _cp(1))])
        with pytest.raises(RuntimeError, match="Dockerfile not found"):
            idk.ensure_egress_proxy_image(tmp_path)

    def test_build_failure_surfaces_docker_stderr(self, fake_docker, tmp_path):
        df = tmp_path / "src" / "booley" / "data" / "docker"
        df.mkdir(parents=True)
        (df / "Dockerfile.egress-proxy").write_text("FROM python:3.13-slim\n")
        fake_docker(
            [
                (lambda a: a[:2] == ["image", "inspect"], _cp(1)),
                (lambda a: a[:1] == ["build"], _cp(1, stderr="failed to fetch base layer")),
            ]
        )

        with pytest.raises(RuntimeError, match="failed to fetch base layer"):
            idk.ensure_egress_proxy_image(tmp_path)


# ===========================================================================
# Egress proxy container (dual-homed)
# ===========================================================================


class TestProxyContainer:
    def test_creates_and_connects_to_egress(self, fake_docker):
        fd = fake_docker(
            [
                (lambda a: a[:2] == ["container", "inspect"], _cp(1)),  # not present
                # after create, networks query returns only the default bridge
                (lambda a: a[:2] == ["container", "inspect"] or "Networks" in " ".join(a), _cp(1)),
            ]
        )
        status = idk.ensure_egress_proxy()
        assert status == "created"
        # run with restart policy + label + proxy port
        assert fd.ran("run", "-d", "--name", idk.PROXY_CONTAINER)
        assert fd.ran("--restart", "unless-stopped")
        # dual-homing: connect to egress network
        assert fd.ran("network", "connect", idk.EGRESS_NETWORK, idk.PROXY_CONTAINER)

    def test_passes_allowlist_env_as_json(self, fake_docker):
        fake_docker([(lambda a: a[:2] == ["container", "inspect"], _cp(1))])
        idk.ensure_egress_proxy(allowlist=("example.com", "foo.test"))
        # Find the run call and check the env var
        run_call = next(c for c in idk._run_docker.calls if c[:2] == ["run", "-d"])
        env_idx = run_call.index("PROXY_ALLOWLIST=" + json.dumps(["example.com", "foo.test"]))
        assert env_idx > 0

    def test_starts_stopped_container(self, fake_docker):
        fd = fake_docker(
            [
                (
                    lambda a: "Networks" in " ".join(a),
                    _cp(0, stdout=json.dumps({idk.EGRESS_NETWORK: {}})),
                ),  # already connected
                (lambda a: "{{.State.Running}}" in a, _cp(0, stdout="false\n")),  # not running
                (lambda a: a[:2] == ["container", "inspect"], _cp(0)),  # exists
            ]
        )
        status = idk.ensure_egress_proxy()
        assert status == "started"
        assert fd.ran("start", idk.PROXY_CONTAINER)
        # already connected → no connect call
        assert not fd.ran("network", "connect")

    def test_running_container_is_noop_run(self, fake_docker):
        fake_docker(
            [
                (
                    lambda a: "Networks" in " ".join(a),
                    _cp(0, stdout=json.dumps({idk.EGRESS_NETWORK: {}, "bridge": {}})),
                ),
                (lambda a: "{{.State.Running}}" in a, _cp(0, stdout="true\n")),
                (lambda a: a[:2] == ["container", "inspect"], _cp(0)),
            ]
        )
        assert idk.ensure_egress_proxy() == "running"
        assert not idk._run_docker.ran("run", "-d")


# ===========================================================================
# Reaper container (WS2)
# ===========================================================================


class TestReaper:
    def test_creates_with_socket_and_policy_env(self, fake_docker):
        fd = fake_docker(
            [
                (lambda a: a[:2] == ["container", "inspect"], _cp(1)),  # not present
            ]
        )
        cfg = InteractiveConfig(idle_timeout_seconds=600, max_sessions=3)
        assert idk.ensure_reaper(cfg) == "created"
        assert fd.ran("run", "-d", "--name", idk.REAPER_CONTAINER)
        assert fd.ran("--restart", "unless-stopped")
        assert fd.ran("-v", f"{idk.DOCKER_SOCK}:{idk.DOCKER_SOCK}")
        run_call = next(c for c in fd.calls if c[:2] == ["run", "-d"])
        assert "BOOLEY_IDLE_TIMEOUT_SECONDS=600" in run_call
        assert "BOOLEY_MAX_SESSIONS=3" in run_call

    def test_starts_stopped_reaper(self, fake_docker):
        fd = fake_docker(
            [
                (lambda a: "{{.State.Running}}" in a, _cp(0, stdout="false\n")),
                (lambda a: a[:2] == ["container", "inspect"], _cp(0)),  # exists
            ]
        )
        assert idk.ensure_reaper(InteractiveConfig()) == "started"
        assert fd.ran("start", idk.REAPER_CONTAINER)
        assert not fd.ran("run", "-d")

    def test_running_reaper_is_noop(self, fake_docker):
        fd = fake_docker(
            [
                (lambda a: "{{.State.Running}}" in a, _cp(0, stdout="true\n")),
                (lambda a: a[:2] == ["container", "inspect"], _cp(0)),
            ]
        )
        assert idk.ensure_reaper(InteractiveConfig()) == "running"
        assert not fd.ran("run", "-d")
        assert not fd.ran("start")

    def test_recreates_reaper_after_image_rebuild(self, fake_docker):
        fd = fake_docker(
            [
                (
                    lambda a: "{{.Image}}" in a,
                    _cp(0, stdout="sha256:old\n"),
                ),
                (
                    lambda a: a[:2] == ["image", "inspect"] and "{{.Id}}" in a,
                    _cp(0, stdout="sha256:new\n"),
                ),
                (lambda a: a[:2] == ["container", "inspect"], _cp(0)),
            ]
        )

        assert idk.ensure_reaper(InteractiveConfig()) == "recreated"
        assert fd.ran("rm", "-f", idk.REAPER_CONTAINER)
        assert fd.ran("run", "-d", "--name", idk.REAPER_CONTAINER)
        assert not fd.ran("start", idk.REAPER_CONTAINER)

    def test_force_recreates_reaper_for_policy_change(self, fake_docker):
        fd = fake_docker(
            [
                (lambda a: a[:2] == ["container", "inspect"], _cp(0)),
            ]
        )
        cfg = InteractiveConfig(idle_timeout_seconds=900, max_sessions=2)

        assert idk.ensure_reaper(cfg, force=True) == "recreated"
        assert fd.ran("rm", "-f", idk.REAPER_CONTAINER)
        run_call = next(c for c in fd.calls if c[:2] == ["run", "-d"])
        assert "BOOLEY_IDLE_TIMEOUT_SECONDS=900" in run_call
        assert "BOOLEY_MAX_SESSIONS=2" in run_call

    def test_keeps_reaper_when_image_ids_match(self, fake_docker):
        fd = fake_docker(
            [
                (
                    lambda a: "{{.Image}}" in a,
                    _cp(0, stdout="sha256:current\n"),
                ),
                (
                    lambda a: a[:2] == ["image", "inspect"] and "{{.Id}}" in a,
                    _cp(0, stdout="sha256:current\n"),
                ),
                (lambda a: "{{.State.Running}}" in a, _cp(0, stdout="true\n")),
                (lambda a: a[:2] == ["container", "inspect"], _cp(0)),
            ]
        )

        assert idk.ensure_reaper(InteractiveConfig()) == "running"
        assert not fd.ran("rm", "-f", idk.REAPER_CONTAINER)
        assert not fd.ran("run", "-d")

    def test_unknown_image_ids_do_not_remove_reaper(self, fake_docker):
        fd = fake_docker(
            [
                (lambda a: "{{.Image}}" in a, _cp(1, stderr="inspect failed")),
                (lambda a: "{{.State.Running}}" in a, _cp(0, stdout="true\n")),
                (lambda a: a[:2] == ["container", "inspect"], _cp(0)),
            ]
        )

        assert idk.ensure_reaper(InteractiveConfig()) == "running"
        assert not fd.ran("rm", "-f", idk.REAPER_CONTAINER)

    def test_build_reaper_image_missing_dockerfile(self, fake_docker, tmp_path):
        fake_docker([(lambda a: a[:2] == ["image", "inspect"], _cp(1))])
        with pytest.raises(RuntimeError, match="Dockerfile not found"):
            idk.ensure_reaper_image(tmp_path)


# ===========================================================================
# State volumes (persistent interactive home-state)
# ===========================================================================


class TestStateVolumes:
    def test_filters_to_booley_state_volumes(self, fake_docker):
        listing = "\n".join(
            [
                "booley-claude-state-myproj",
                "booley-codex-state-other",
                "booley-cargo-registry",  # not a state volume
                "booley-pip-local",  # not a state volume
                "some-unrelated-volume",
            ]
        )
        fake_docker([(lambda a: a[:2] == ["volume", "ls"], _cp(0, stdout=listing))])
        assert idk.state_volumes() == [
            "booley-claude-state-myproj",
            "booley-codex-state-other",
        ]

    def test_empty_when_none(self, fake_docker):
        fake_docker([(lambda a: a[:2] == ["volume", "ls"], _cp(0, stdout="\n"))])
        assert idk.state_volumes() == []

    def test_empty_on_docker_error(self, fake_docker):
        fake_docker([(lambda a: a[:2] == ["volume", "ls"], _cp(1, stderr="boom"))])
        assert idk.state_volumes() == []
