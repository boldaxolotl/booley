from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from booley.config.host_config import InteractiveHostPolicy
from booley.harness import host_sidecars as sidecars
from booley.harness.image_lifecycle import Intent


def _cp(returncode: int = 0, stdout: str = "", stderr: str = "") -> subprocess.CompletedProcess[str]:
    return subprocess.CompletedProcess(["docker"], returncode, stdout, stderr)


class FakeDocker:
    def __init__(self, result: subprocess.CompletedProcess[str] | None = None) -> None:
        self.calls: list[list[str]] = []
        self.result = result or _cp()

    def run(self, args: list[str], *, timeout: int = 30) -> subprocess.CompletedProcess[str]:
        del timeout
        self.calls.append(args)
        return self.result


def test_policy_fingerprint_is_canonical_and_policy_sensitive() -> None:
    first = InteractiveHostPolicy(600, 2, ("example.com",))
    same = InteractiveHostPolicy(
        idle_timeout_seconds=600,
        max_sessions=2,
        egress_allowlist=("example.com",),
    )
    changed = InteractiveHostPolicy(600, 3, ("example.com",))
    assert sidecars.policy_fingerprint(first) == sidecars.policy_fingerprint(same)
    assert sidecars.policy_fingerprint(first) != sidecars.policy_fingerprint(changed)


def test_source_fingerprint_covers_every_exact_build_input(tmp_path: Path) -> None:
    dockerfile = tmp_path / "Dockerfile"
    source = tmp_path / "sidecar.py"
    dockerfile.write_text("FROM scratch\n", encoding="utf-8")
    source.write_text("print('one')\n", encoding="utf-8")
    spec = sidecars._ImageSpec("image", "image", "kind", dockerfile, tmp_path, (source,))
    before = spec.fingerprint
    source.write_text("print('two')\n", encoding="utf-8")
    assert spec.fingerprint != before


def test_prior_booley_version_makes_sidecar_image_pending(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    dockerfile = tmp_path / "Dockerfile"
    source = tmp_path / "sidecar.py"
    dockerfile.write_text("FROM scratch\n", encoding="utf-8")
    source.write_text("pass\n", encoding="utf-8")
    spec = sidecars._ImageSpec("image", "image", "kind", dockerfile, tmp_path, (source,))
    labels = sidecars._image_labels(spec)
    labels[sidecars.LABEL_BOOLEY_VERSION] = "0.0.0"
    monkeypatch.setattr(sidecars, "_inspect_image", lambda *_args: ("sha256:same", labels))
    docker = FakeDocker()

    finding = sidecars._reconcile_image(spec, Intent.CHECK, docker)

    assert finding.state is sidecars.SidecarState.PENDING
    assert docker.calls == []


def test_foreign_fixed_name_collision_is_never_mutated(monkeypatch: pytest.MonkeyPatch) -> None:
    docker = FakeDocker()
    state = sidecars._ContainerState(
        True,
        "sha256:current",
        True,
        {sidecars.ROLE_LABEL: "someone-else"},
    )
    monkeypatch.setattr(sidecars, "_inspect_container", lambda *_args: state)
    monkeypatch.setattr(sidecars, "_inspect_image", lambda *_args: ("sha256:current", {}))
    finding = sidecars._reconcile_container(
        "proxy",
        "booley-proxy",
        "proxy-image",
        "egress-proxy",
        "policy",
        Intent.REFRESH,
        docker,
        run_args=["run"],
    )
    assert finding.state is sidecars.SidecarState.ERROR
    assert "foreign name collision" in finding.detail
    assert docker.calls == []


def test_unstamped_container_with_exact_role_is_stale_not_foreign(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    docker = FakeDocker()
    state = sidecars._ContainerState(
        True,
        "sha256:current",
        True,
        {sidecars.ROLE_LABEL: "reaper"},
    )
    monkeypatch.setattr(sidecars, "_inspect_container", lambda *_args: state)
    monkeypatch.setattr(sidecars, "_inspect_image", lambda *_args: ("sha256:current", {}))

    finding = sidecars._reconcile_container(
        "reaper",
        "booley-reaper",
        "reaper-image",
        "reaper",
        "policy",
        Intent.CHECK,
        docker,
        run_args=["run"],
    )

    assert finding.state is sidecars.SidecarState.PENDING
    assert "stale" in finding.detail
    assert docker.calls == []


def test_missing_container_is_created_without_enumerating_sessions(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    docker = FakeDocker()
    monkeypatch.setattr(sidecars, "_inspect_container", lambda *_args: sidecars._ContainerState(False))
    monkeypatch.setattr(sidecars, "_inspect_image", lambda *_args: ("sha256:current", {}))
    monkeypatch.setattr(
        sidecars,
        "_active_session_names",
        lambda _docker: (_ for _ in ()).throw(AssertionError("must not enumerate")),
    )

    finding = sidecars._reconcile_container(
        "reaper",
        "booley-reaper",
        "reaper-image",
        "reaper",
        "policy",
        Intent.ENSURE,
        docker,
        run_args=["run", "--label", "policy"],
    )

    assert finding.state is sidecars.SidecarState.CHANGED
    assert docker.calls == [["run", "--label", "policy"]]


def test_active_sessions_block_stale_container_replacement(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    docker = FakeDocker()
    state = sidecars._ContainerState(
        True,
        "sha256:old",
        True,
        {
            sidecars.ROLE_LABEL: "reaper",
            sidecars.LABEL_POLICY_FINGERPRINT: "old-policy",
        },
    )
    monkeypatch.setattr(sidecars, "_inspect_container", lambda *_args: state)
    monkeypatch.setattr(sidecars, "_inspect_image", lambda *_args: ("sha256:new", {}))
    monkeypatch.setattr(sidecars, "_active_session_names", lambda _docker: ("booley-session-a",))
    finding = sidecars._reconcile_container(
        "reaper",
        "booley-reaper",
        "reaper-image",
        "reaper",
        "new-policy",
        Intent.REFRESH,
        docker,
        run_args=["run"],
    )
    assert finding.state is sidecars.SidecarState.ERROR
    assert "booley-session-a" in finding.detail
    assert not any(call[:2] == ["rm", "-f"] for call in docker.calls)


def test_session_enumeration_failure_is_not_treated_as_empty() -> None:
    docker = FakeDocker(_cp(1, stderr="daemon unavailable"))
    with pytest.raises(sidecars.SidecarError, match="cannot enumerate"):
        sidecars._active_session_names(docker)
