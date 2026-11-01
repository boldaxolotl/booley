from __future__ import annotations

import json
import subprocess
from dataclasses import replace
from pathlib import Path

import pytest

from booley.config.host_config import SandboxHostPolicy
from booley.harness import bootstrap
from booley.harness import host_sidecars as sidecars
from booley.runtime.docker_capacity import DockerCapacityError
from booley.runtime.image_lifecycle import Intent


def _cp(
    returncode: int = 0, stdout: str = "", stderr: str = ""
) -> subprocess.CompletedProcess[str]:
    return subprocess.CompletedProcess(["docker"], returncode, stdout, stderr)


class FakeDocker:
    def __init__(self, result: subprocess.CompletedProcess[str] | None = None) -> None:
        self.calls: list[list[str]] = []
        self.result = result or _cp()

    def run(self, args: list[str], *, timeout: int = 30) -> subprocess.CompletedProcess[str]:
        del timeout
        self.calls.append(args)
        return self.result


class SequenceDocker(FakeDocker):
    def __init__(self, *results: subprocess.CompletedProcess[str]) -> None:
        super().__init__()
        self.results = list(results)

    def run(self, args: list[str], *, timeout: int = 30) -> subprocess.CompletedProcess[str]:
        del timeout
        self.calls.append(args)
        return self.results.pop(0)


def _container_spec(
    resource: str,
    name: str,
    image: str,
    role: str,
    *,
    run_args: tuple[str, ...] = ("run",),
    required_network: str | None = None,
) -> sidecars._ContainerSpec:
    return sidecars._ContainerSpec(
        resource,
        name,
        image,
        role,
        run_args,
        required_network,
    )


def _active_session(name: str, project: str = "/projects/test") -> sidecars._ActiveSession:
    return sidecars._ActiveSession(name, project)


def test_policy_fingerprint_is_canonical_and_policy_sensitive() -> None:
    first = SandboxHostPolicy(600, 2, ("example.com",))
    same = SandboxHostPolicy(
        idle_timeout_seconds=600,
        max_sessions=2,
        egress_allowlist=("example.com",),
    )
    changed = SandboxHostPolicy(600, 3, ("example.com",))
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


def test_source_fingerprint_wraps_unreadable_inputs(tmp_path: Path) -> None:
    missing = tmp_path / "missing"
    spec = sidecars._ImageSpec("image", "image", "kind", missing, tmp_path, ())
    with pytest.raises(sidecars.SidecarError, match="cannot fingerprint"):
        _ = spec.fingerprint


def test_plan_image_builds_selects_missing_and_refresh_requested_images(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    current = sidecars._ImageSpec(
        "current", "current-image", "current", Path("Dockerfile"), Path(), ()
    )
    missing = sidecars._ImageSpec(
        "missing", "missing-image", "missing", Path("Dockerfile"), Path(), ()
    )
    labels = {
        current.reference: {"role": "current"},
        missing.reference: {"role": "missing"},
    }
    verified = []
    monkeypatch.setattr(sidecars, "_image_specs", lambda _root: (current, missing))
    monkeypatch.setattr(
        sidecars,
        "_inspect_image",
        lambda reference, _docker: (
            ("sha256:current", labels[reference]) if reference == current.reference else None
        ),
    )
    monkeypatch.setattr(
        sidecars, "_verify_image_ownership", lambda spec, image: verified.append((spec, image))
    )
    monkeypatch.setattr(sidecars, "_image_labels", lambda spec: labels[spec.reference])

    ensure = sidecars.plan_image_builds(Intent.ENSURE, docker=FakeDocker())
    refresh = sidecars.plan_image_builds(Intent.REFRESH, docker=FakeDocker())

    assert [request.output_tag for request in ensure] == [missing.reference]
    assert [request.output_tag for request in refresh] == [current.reference, missing.reference]
    assert all(request.estimate_class.value == "thin-overlay" for request in (*ensure, *refresh))
    assert len(verified) == 4


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
    assert sidecars.LABEL_BOOLEY_VERSION in finding.detail
    assert f"{sidecars._image_labels(spec)[sidecars.LABEL_BOOLEY_VERSION]} -> 0.0.0" in (
        finding.detail
    )
    assert sidecars.LABEL_SOURCE_FINGERPRINT not in finding.detail
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
        _container_spec("proxy", "booley-proxy", "proxy-image", "egress-proxy"),
        "policy",
        Intent.REFRESH,
        docker,
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
        _container_spec("reaper", "booley-reaper", "reaper-image", "reaper"),
        "policy",
        Intent.CHECK,
        docker,
    )

    assert finding.state is sidecars.SidecarState.PENDING
    assert "stale" in finding.detail
    assert "source_fingerprint policy -> <none>" in finding.detail
    assert "image_identity" not in finding.detail
    assert docker.calls == []


def test_missing_container_is_created_without_enumerating_sessions(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    docker = FakeDocker()
    monkeypatch.setattr(
        sidecars, "_inspect_container", lambda *_args: sidecars._ContainerState(False)
    )
    monkeypatch.setattr(sidecars, "_inspect_image", lambda *_args: ("sha256:current", {}))
    monkeypatch.setattr(
        sidecars,
        "_active_sessions",
        lambda _docker: (_ for _ in ()).throw(AssertionError("must not enumerate")),
    )

    finding = sidecars._reconcile_container(
        _container_spec(
            "reaper",
            "booley-reaper",
            "reaper-image",
            "reaper",
            run_args=("run", "--label", "policy"),
        ),
        "policy",
        Intent.ENSURE,
        docker,
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
    monkeypatch.setattr(
        sidecars,
        "_active_sessions",
        lambda _docker: (_active_session("booley-session-a"),),
    )
    finding = sidecars._reconcile_container(
        _container_spec("reaper", "booley-reaper", "reaper-image", "reaper"),
        "new-policy",
        Intent.REFRESH,
        docker,
    )
    assert finding.state is sidecars.SidecarState.ERROR
    assert "booley-session-a" in finding.detail
    assert not any(call[:2] == ["rm", "-f"] for call in docker.calls)


def test_active_session_blocker_names_project_and_scoped_shutdown_command(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    docker = FakeDocker()
    project = "/projects/acme cpu"
    monkeypatch.setattr(
        sidecars,
        "_active_sessions",
        lambda _docker: (_active_session("session-a", project),),
    )

    with pytest.raises(sidecars.SidecarError) as raised:
        sidecars._replace_stale_container("booley-reaper", ["run"], docker)

    detail = str(raised.value)
    assert project in detail
    assert "booley session down --project" in detail
    assert docker.calls == []


def test_session_enumeration_failure_is_not_treated_as_empty() -> None:
    docker = FakeDocker(_cp(1, stderr="daemon unavailable"))
    with pytest.raises(sidecars.SidecarError, match="cannot enumerate"):
        sidecars._active_sessions(docker)


def test_foreign_sidecar_image_collision_is_never_retagged(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    dockerfile = tmp_path / "Dockerfile"
    source = tmp_path / "sidecar.py"
    dockerfile.write_text("FROM scratch\n", encoding="utf-8")
    source.write_text("pass\n", encoding="utf-8")
    spec = sidecars._ImageSpec(
        "proxy-image", "booley-egress-proxy:local", "egress-proxy", dockerfile, tmp_path, (source,)
    )
    monkeypatch.setattr(sidecars, "_inspect_image", lambda *_args: ("sha256:foreign", {}))
    docker = FakeDocker()

    finding = sidecars._reconcile_image(spec, Intent.ENSURE, docker)

    assert finding.state is sidecars.SidecarState.ERROR
    assert "foreign image collision" in finding.detail
    assert docker.calls == []


def test_stale_network_is_recreated_when_no_sessions_are_active(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    docker = FakeDocker()
    state = sidecars._NetworkState(True, False, (sidecars.legacy.PROXY_CONTAINER,))
    proxy = sidecars._ContainerState(
        True,
        "sha256:proxy",
        True,
        {sidecars.ROLE_LABEL: "egress-proxy"},
    )
    monkeypatch.setattr(sidecars, "_inspect_network", lambda _docker: state)
    monkeypatch.setattr(sidecars, "_active_sessions", lambda _docker: ())
    monkeypatch.setattr(sidecars, "_inspect_container", lambda *_args: proxy)

    finding = sidecars._reconcile_network(Intent.REFRESH, docker)

    assert finding.state is sidecars.SidecarState.CHANGED
    assert ["rm", "-f", sidecars.legacy.PROXY_CONTAINER] in docker.calls
    assert ["network", "rm", sidecars.legacy.EGRESS_NETWORK] in docker.calls
    assert any(call[:2] == ["network", "create"] for call in docker.calls)


def test_active_sessions_block_stale_network_replacement(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    docker = FakeDocker()
    monkeypatch.setattr(
        sidecars,
        "_inspect_network",
        lambda _docker: sidecars._NetworkState(True, False),
    )
    monkeypatch.setattr(
        sidecars,
        "_active_sessions",
        lambda _docker: (_active_session("session-a"),),
    )

    finding = sidecars._reconcile_network(Intent.ENSURE, docker)

    assert finding.state is sidecars.SidecarState.ERROR
    assert "session-a" in finding.detail
    assert not any(call[0] in {"rm", "start", "run"} for call in docker.calls)


def test_reconcile_sidecars_returns_early_for_image_network_and_proxy_errors(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    spec = sidecars._ImageSpec("image", "image", "kind", tmp_path, tmp_path, ())
    monkeypatch.setattr(sidecars, "_docker_adapter", FakeDocker)
    monkeypatch.setattr(sidecars, "_image_specs", lambda _root: (spec, spec))
    image_error = sidecars.SidecarFinding("image", sidecars.SidecarState.ERROR, "bad")
    monkeypatch.setattr(sidecars, "_reconcile_image", lambda *_args, **_kwargs: image_error)
    result = sidecars.reconcile_sidecars(SandboxHostPolicy(), Intent.CHECK)
    assert result.findings == (image_error,)
    assert not result.ready

    image_ok = sidecars.SidecarFinding("image", sidecars.SidecarState.CURRENT, "ok")
    network_error = sidecars.SidecarFinding("network", sidecars.SidecarState.ERROR, "bad")
    monkeypatch.setattr(sidecars, "_reconcile_image", lambda *_args, **_kwargs: image_ok)
    monkeypatch.setattr(sidecars, "_reconcile_network", lambda *_args: network_error)
    result = sidecars.reconcile_sidecars(SandboxHostPolicy(), Intent.CHECK)
    assert result.findings[-1] is network_error

    network_ok = sidecars.SidecarFinding("network", sidecars.SidecarState.CURRENT, "ok")
    proxy_error = sidecars.SidecarFinding("proxy", sidecars.SidecarState.ERROR, "bad")
    monkeypatch.setattr(sidecars, "_reconcile_network", lambda *_args: network_ok)
    monkeypatch.setattr(sidecars, "_reconcile_container", lambda *_args: proxy_error)
    result = sidecars.reconcile_sidecars(SandboxHostPolicy(), Intent.CHECK)
    assert result.findings[-1] is proxy_error


def test_reconcile_sidecars_wraps_invalid_image_specs(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(sidecars, "_docker_adapter", FakeDocker)

    def fail(_root):
        raise sidecars.SidecarError("package missing")

    monkeypatch.setattr(sidecars, "_image_specs", fail)
    result = sidecars.reconcile_sidecars(SandboxHostPolicy(), Intent.CHECK)
    assert result.findings == (
        sidecars.SidecarFinding("sidecar-images", sidecars.SidecarState.ERROR, "package missing"),
    )


def test_reconcile_sidecars_builds_both_container_specs(monkeypatch: pytest.MonkeyPatch, tmp_path):
    image_specs = tuple(
        sidecars._ImageSpec(name, name, name, tmp_path, tmp_path, ())
        for name in ("proxy-image", "reaper-image")
    )
    monkeypatch.setattr(sidecars, "_docker_adapter", FakeDocker)
    monkeypatch.setattr(sidecars, "_image_specs", lambda _root: image_specs)
    monkeypatch.setattr(
        sidecars,
        "_reconcile_image",
        lambda spec, *_args, **_kwargs: sidecars.SidecarFinding(
            spec.resource, sidecars.SidecarState.CURRENT, "ok"
        ),
    )
    monkeypatch.setattr(
        sidecars,
        "_reconcile_network",
        lambda *_args: sidecars.SidecarFinding("network", sidecars.SidecarState.CURRENT, "ok"),
    )
    seen: list[sidecars._ContainerSpec] = []

    def reconcile(spec, *_args):
        seen.append(spec)
        return sidecars.SidecarFinding(spec.resource, sidecars.SidecarState.CURRENT, "ok")

    monkeypatch.setattr(sidecars, "_reconcile_container", reconcile)
    result = sidecars.reconcile_sidecars(SandboxHostPolicy(), Intent.ENSURE)
    assert result.ready
    assert [spec.resource for spec in seen] == ["proxy", "reaper"]
    assert seen[0].required_network == sidecars.legacy.EGRESS_NETWORK
    assert seen[1].required_network is None


def test_image_specs_support_source_checkout_and_installed_layout(tmp_path: Path) -> None:
    source_package = tmp_path / "src" / "booley"
    source_package.mkdir(parents=True)
    source_specs = sidecars._image_specs(tmp_path)
    assert source_specs[0].dockerfile.parent == source_package / "data" / "docker"

    installed = tmp_path / "installed"
    installed.mkdir()
    installed_specs = sidecars._image_specs(installed)
    assert installed_specs[0].context == installed / "docker"


def test_image_reconciliation_current_builds_and_verifies(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(sidecars, "ensure_docker_build_capacity", lambda *_args, **_kwargs: None)
    dockerfile = tmp_path / "Dockerfile"
    dockerfile.write_text("FROM scratch\n", encoding="utf-8")
    spec = sidecars._ImageSpec("image", "image", "kind", dockerfile, tmp_path, ())
    expected = sidecars._image_labels(spec)
    monkeypatch.setattr(sidecars, "_inspect_image", lambda *_args: ("sha", expected))
    docker = FakeDocker()
    assert (
        sidecars._reconcile_image(spec, Intent.ENSURE, docker).state
        is sidecars.SidecarState.CURRENT
    )

    inspections = iter((None, ("sha", expected)))
    monkeypatch.setattr(sidecars, "_inspect_image", lambda *_args: next(inspections))
    finding = sidecars._reconcile_image(spec, Intent.ENSURE, docker)
    assert finding.state is sidecars.SidecarState.CHANGED
    assert docker.calls[-1][:3] == ["build", "-t", "image"]


def test_image_reconciliation_rejects_unverified_build(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(sidecars, "ensure_docker_build_capacity", lambda *_args, **_kwargs: None)
    dockerfile = tmp_path / "Dockerfile"
    dockerfile.write_text("FROM scratch\n", encoding="utf-8")
    spec = sidecars._ImageSpec("image", "image", "kind", dockerfile, tmp_path, ())
    inspections = iter((None, ("sha", {})))
    monkeypatch.setattr(sidecars, "_inspect_image", lambda *_args: next(inspections))
    finding = sidecars._reconcile_image(spec, Intent.ENSURE, FakeDocker())
    assert finding.state is sidecars.SidecarState.ERROR
    assert "expected provenance" in finding.detail


def test_image_reconciliation_reports_capacity_refusal_without_building(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    dockerfile = tmp_path / "Dockerfile"
    dockerfile.write_text("FROM scratch\n", encoding="utf-8")
    spec = sidecars._ImageSpec("image", "image", "kind", dockerfile, tmp_path, ())
    monkeypatch.setattr(sidecars, "_inspect_image", lambda *_args: None)
    monkeypatch.setattr(
        sidecars,
        "ensure_docker_build_capacity",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(DockerCapacityError("capacity refused")),
    )
    docker = FakeDocker()

    finding = sidecars._reconcile_image(spec, Intent.ENSURE, docker)

    assert finding.state is sidecars.SidecarState.ERROR
    assert finding.detail == "capacity refused"
    assert docker.calls == []


def test_image_ownership_rejects_wrong_kind(tmp_path: Path) -> None:
    dockerfile = tmp_path / "Dockerfile"
    dockerfile.write_text("FROM scratch\n", encoding="utf-8")
    spec = sidecars._ImageSpec("image", "image", "kind", dockerfile, tmp_path, ())
    labels = {
        sidecars.LABEL_SIDECAR_SCHEMA: sidecars.IMAGE_SCHEMA,
        sidecars.LABEL_SIDECAR_KIND: "other",
        sidecars.LABEL_SOURCE_FINGERPRINT: "source",
        sidecars.LABEL_BOOLEY_VERSION: "version",
    }
    with pytest.raises(sidecars.SidecarError, match="foreign image collision") as caught:
        sidecars._verify_image_ownership(spec, ("sha", labels))
    assert "io.booley.sidecar.kind kind -> other" in str(caught.value)


def test_build_image_reports_docker_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(sidecars, "ensure_docker_build_capacity", lambda *_args, **_kwargs: None)
    spec = sidecars._ImageSpec("image", "image", "kind", tmp_path, tmp_path, ())
    with pytest.raises(sidecars.SidecarError, match="failed to build"):
        sidecars._build_image(spec, {"label": "value"}, FakeDocker(_cp(1, stderr="bad")))


def test_inspect_image_handles_missing_failure_valid_and_incomplete() -> None:
    assert sidecars._inspect_image("image", FakeDocker(_cp(1, stderr="No such image"))) is None
    with pytest.raises(sidecars.SidecarError, match="cannot inspect"):
        sidecars._inspect_image("image", FakeDocker(_cp(1, stderr="daemon down")))
    document = '{"Id":"sha","Config":{"Labels":{"name":"value"}}}'
    assert sidecars._inspect_image("image", FakeDocker(_cp(stdout=document))) == (
        "sha",
        {"name": "value"},
    )
    with pytest.raises(sidecars.SidecarError, match="incomplete inspection"):
        sidecars._inspect_image("image", FakeDocker(_cp(stdout='{"Config":{}}')))


def test_network_reconciliation_current_pending_create_and_create_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    docker = FakeDocker()
    monkeypatch.setattr(
        sidecars, "_inspect_network", lambda _docker: sidecars._NetworkState(True, True)
    )
    assert (
        sidecars._reconcile_network(Intent.ENSURE, docker).state is sidecars.SidecarState.CURRENT
    )

    monkeypatch.setattr(
        sidecars, "_inspect_network", lambda _docker: sidecars._NetworkState(False)
    )
    assert sidecars._reconcile_network(Intent.CHECK, docker).state is sidecars.SidecarState.PENDING
    assert (
        sidecars._reconcile_network(Intent.ENSURE, docker).state is sidecars.SidecarState.CHANGED
    )

    finding = sidecars._reconcile_network(Intent.ENSURE, FakeDocker(_cp(1, stderr="denied")))
    assert finding.state is sidecars.SidecarState.ERROR
    assert "failed to create" in finding.detail


def test_stale_network_refuses_foreign_attachment_and_failed_removals(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(sidecars, "_active_sessions", lambda _docker: ())
    with pytest.raises(sidecars.SidecarError, match="foreign containers"):
        sidecars._replace_stale_network(
            sidecars._NetworkState(True, False, ("foreign",)), FakeDocker()
        )

    proxy = sidecars._ContainerState(True, "sha", True, {sidecars.ROLE_LABEL: "egress-proxy"})
    monkeypatch.setattr(sidecars, "_inspect_container", lambda *_args: proxy)
    with pytest.raises(sidecars.SidecarError, match="failed to remove stale"):
        sidecars._replace_stale_network(
            sidecars._NetworkState(True, False), FakeDocker(_cp(1, stderr="denied"))
        )

    docker = SequenceDocker(_cp(), _cp(1, stderr="busy"))
    with pytest.raises(sidecars.SidecarError, match="failed to replace"):
        sidecars._replace_stale_network(sidecars._NetworkState(True, False), docker)


def test_inspect_network_handles_missing_failure_valid_and_bad_container() -> None:
    assert not sidecars._inspect_network(FakeDocker(_cp(1, stderr="network not found"))).exists
    with pytest.raises(sidecars.SidecarError, match="cannot inspect network"):
        sidecars._inspect_network(FakeDocker(_cp(1, stderr="daemon down")))
    document = (
        '{"Internal":true,"Options":{"com.docker.network.bridge.gateway_mode_ipv4":'
        '"isolated"},"Containers":{"id":{"Name":"proxy"}}}'
    )
    state = sidecars._inspect_network(FakeDocker(_cp(stdout=document)))
    assert state == sidecars._NetworkState(True, True, ("proxy",))
    with pytest.raises(sidecars.SidecarError, match="incomplete inspection"):
        sidecars._inspect_network(
            FakeDocker(_cp(stdout='{"Internal":true,"Options":{},"Containers":{"id":{}}}'))
        )


def test_network_container_names_require_string_ids() -> None:
    with pytest.raises(sidecars.BoundaryError, match="IDs must be strings"):
        sidecars._network_container_names({1: {"Name": "proxy"}})


def test_container_reconciliation_handles_missing_image_current_and_network_connect(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    docker = FakeDocker()
    state = sidecars._ContainerState(
        True,
        "sha",
        True,
        {
            sidecars.ROLE_LABEL: "role",
            sidecars.LABEL_POLICY_FINGERPRINT: "policy",
        },
        frozenset({"network"}),
    )
    monkeypatch.setattr(sidecars, "_inspect_container", lambda *_args: state)
    monkeypatch.setattr(sidecars, "_inspect_image", lambda *_args: None)
    finding = sidecars._reconcile_container(
        _container_spec("resource", "name", "image", "role"),
        "policy",
        Intent.ENSURE,
        docker,
    )
    assert finding.state is sidecars.SidecarState.ERROR

    monkeypatch.setattr(sidecars, "_inspect_image", lambda *_args: ("sha", {}))
    finding = sidecars._reconcile_container(
        _container_spec("resource", "name", "image", "role", required_network="network"),
        "policy",
        Intent.ENSURE,
        docker,
    )
    assert finding.state is sidecars.SidecarState.CURRENT

    disconnected = sidecars._ContainerState(
        True,
        "sha",
        True,
        {
            sidecars.ROLE_LABEL: "role",
            sidecars.LABEL_POLICY_FINGERPRINT: "policy",
        },
    )
    monkeypatch.setattr(sidecars, "_inspect_container", lambda *_args: disconnected)
    finding = sidecars._reconcile_container(
        _container_spec("resource", "name", "image", "role", required_network="network"),
        "policy",
        Intent.ENSURE,
        docker,
    )
    assert finding.state is sidecars.SidecarState.CHANGED
    assert docker.calls[-1] == ["network", "connect", "network", "name"]


def test_apply_container_starts_stopped_current_and_reports_failure() -> None:
    state = sidecars._ContainerState(True, running=False)
    docker = FakeDocker()
    sidecars._apply_container("name", state, True, ["run"], docker)
    assert docker.calls == [["start", "name"]]
    with pytest.raises(sidecars.SidecarError, match="failed to start"):
        sidecars._apply_container("name", state, True, ["run"], FakeDocker(_cp(1)))


def test_replace_stale_container_removes_then_runs_and_wraps_remove_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(sidecars, "_active_sessions", lambda _docker: ())
    docker = SequenceDocker(_cp(), _cp())
    sidecars._replace_stale_container("name", ["run", "image"], docker)
    assert docker.calls == [["rm", "-f", "name"], ["run", "image"]]
    with pytest.raises(sidecars.SidecarError, match="failed to replace"):
        sidecars._replace_stale_container("name", ["run"], FakeDocker(_cp(1)))


def test_ensure_network_accepts_already_connected_and_rejects_other_failures(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        sidecars, "_inspect_container", lambda *_args: sidecars._ContainerState(True)
    )
    sidecars._ensure_container_network(
        "name", "network", FakeDocker(_cp(1, stderr="endpoint already exists"))
    )
    with pytest.raises(sidecars.SidecarError, match="failed to connect"):
        sidecars._ensure_container_network("name", "network", FakeDocker(_cp(1, stderr="denied")))


def test_inspect_container_handles_missing_failure_valid_and_incomplete(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    assert not sidecars._inspect_container(
        "name", FakeDocker(_cp(1, stderr="No such container"))
    ).exists
    with pytest.raises(sidecars.SidecarError, match="cannot inspect"):
        sidecars._inspect_container("name", FakeDocker(_cp(1, stderr="daemon down")))
    document = (
        '{"Image":"sha","Config":{"Labels":{"booley.role":"role"}},'
        '"State":{"Running":true},"NetworkSettings":{"Networks":{"network":{}}},'
        '"Mounts":[{"Type":"bind","Source":"/projects/acme",'
        '"Destination":"/work","RW":true}]}'
    )
    monkeypatch.setattr(
        sidecars,
        "host_path_from_docker_mount",
        lambda _source: "/projects/acme",
    )
    state = sidecars._inspect_container("name", FakeDocker(_cp(stdout=document)))
    assert state.image_id == "sha"
    assert state.running
    assert state.networks == frozenset({"network"})
    assert state.project_root == "/projects/acme"
    invalid_mounts = (
        '{"Image":"sha","Config":{"Labels":{"booley.role":"role"}},'
        '"State":{"Running":true},"NetworkSettings":{"Networks":{}},"Mounts":{}}'
    )
    with pytest.raises(sidecars.SidecarError, match="incomplete inspection"):
        sidecars._inspect_container("name", FakeDocker(_cp(stdout=invalid_mounts)))
    with pytest.raises(sidecars.SidecarError, match="incomplete inspection"):
        sidecars._inspect_container("name", FakeDocker(_cp(stdout="{}")))


def test_project_root_prefers_label_and_requires_a_workspace_mount() -> None:
    assert (
        sidecars._project_root_from_inspection(
            {"Mounts": []},
            {"devcontainer.local_folder": "/projects/from-label"},
            "session",
        )
        == "/projects/from-label"
    )
    assert sidecars._project_root_from_inspection({"Mounts": []}, {}, "session") is None


def test_active_session_enumeration_validates_rows_and_ownership(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    docker = FakeDocker(_cp(stdout="id\tsession-b\n"))
    valid = sidecars._ContainerState(
        True,
        running=True,
        labels={sidecars.ROLE_LABEL: sidecars.SESSION_ROLE},
        project_root="/projects/b",
    )
    monkeypatch.setattr(sidecars, "_inspect_container", lambda *_args: valid)
    assert sidecars._active_sessions(docker) == (
        sidecars._ActiveSession("session-b", "/projects/b"),
    )

    with pytest.raises(sidecars.SidecarError, match="incomplete active Session"):
        sidecars._active_sessions(FakeDocker(_cp(stdout="malformed")))

    monkeypatch.setattr(
        sidecars,
        "_inspect_container",
        lambda *_args: sidecars._ContainerState(True, running=False),
    )
    with pytest.raises(sidecars.SidecarError, match="cannot prove"):
        sidecars._active_sessions(FakeDocker(_cp(stdout="id\tsession\n")))

    monkeypatch.setattr(
        sidecars,
        "_inspect_container",
        lambda *_args: sidecars._ContainerState(
            True,
            running=True,
            labels={sidecars.ROLE_LABEL: sidecars.SESSION_ROLE},
        ),
    )
    with pytest.raises(sidecars.SidecarError, match="cannot identify owning Project"):
        sidecars._active_sessions(FakeDocker(_cp(stdout="id\tsession\n")))


def test_run_arguments_include_policy_and_optional_allowlist() -> None:
    policy = SandboxHostPolicy(600, 2, ("example.com",))
    proxy = sidecars._proxy_run_args(policy, "fingerprint")
    reaper = sidecars._reaper_run_args(policy, "fingerprint")
    assert 'PROXY_ALLOWLIST=["example.com"]' in proxy
    assert f"{sidecars.ROLE_LABEL}=egress-proxy" in proxy
    assert "BOOLEY_IDLE_TIMEOUT_SECONDS=600" in reaper
    assert not any("BOOLEY_MAX_SESSIONS=" in arg for arg in reaper)
    assert "PROXY_ALLOWLIST" not in " ".join(
        sidecars._proxy_run_args(SandboxHostPolicy(), "fingerprint")
    )


def test_json_and_string_boundary_helpers_fail_closed() -> None:
    with pytest.raises(sidecars.SidecarError, match="invalid image JSON"):
        sidecars._json_object("[]", "image")
    assert sidecars._string_labels(None, "image") == {}
    with pytest.raises(sidecars.SidecarError, match="invalid labels"):
        sidecars._string_labels({"name": 1}, "image")
    with pytest.raises(sidecars.BoundaryError, match="required"):
        sidecars._required_bool({}, "Running")
    with pytest.raises(sidecars.BoundaryError, match="string keys"):
        sidecars._string_keys({1: {}}, "networks")


def test_failure_helpers_and_docker_adapter() -> None:
    assert sidecars._is_missing(_cp(1, stdout="NOT FOUND"))
    assert sidecars._failure_detail(_cp(1)) == "Docker exited 1 without diagnostic output"
    adapter = sidecars._DockerCli()
    original = sidecars.legacy._run_docker
    try:
        sidecars.legacy._run_docker = lambda *_args, **_kwargs: (_ for _ in ()).throw(
            OSError("denied")
        )
        with pytest.raises(sidecars.SidecarError, match="Docker command failed"):
            adapter.run(["info"])
    finally:
        sidecars.legacy._run_docker = original


def test_check_only_missing_sidecar_images_and_containers_are_pending(
    monkeypatch, tmp_path, capsys
):
    from tests.harness.test_bootstrap import (
        _inspection_environment,
        _run_inspection,
        _tree_snapshot,
    )

    _inspection_environment(monkeypatch, tmp_path)
    before = _tree_snapshot(tmp_path)
    status, output = _run_inspection(capsys)
    for resource in ("proxy-image", "reaper-image", "network", "proxy", "reaper"):
        assert f"+  {resource}:" in output, f"sidecar {resource} must be pending"
    assert status == 1
    assert _tree_snapshot(tmp_path) == before


@pytest.mark.parametrize("resource", ["proxy-image", "reaper-image", "network", "proxy"])
def test_check_only_continues_after_sidecar_error(monkeypatch, tmp_path, capsys, resource):
    from tests.harness.test_bootstrap import (
        _assert_complete,
        _inspection_environment,
        _run_inspection,
    )

    environment = _inspection_environment(monkeypatch, tmp_path)
    environment.docker.failure = resource
    status, output = _run_inspection(capsys)
    _assert_complete(output)
    assert status == 2
    assert f"✗  {resource}:" in output
    assert "access" in output


def test_complete_inspection_foreign_container_with_missing_image_remains_error(
    monkeypatch, tmp_path, capsys
):
    from tests.harness.test_bootstrap import (
        _assert_complete,
        _inspection_environment,
        _run_inspection,
    )

    _inspection_environment(monkeypatch, tmp_path)

    def inspect(name, _docker):
        if name == sidecars.legacy.PROXY_CONTAINER:
            return sidecars._ContainerState(True, labels={})
        return sidecars._ContainerState(False)

    monkeypatch.setattr(sidecars, "_inspect_container", inspect)
    status, output = _run_inspection(capsys)
    _assert_complete(output)
    assert status == 2
    assert "✗  proxy:" in output and "foreign name collision" in output
    assert "+  reaper:" in output


@pytest.mark.parametrize("error_type", [sidecars.SidecarError, PermissionError])
def test_complete_inspection_checks_network_and_containers_after_recipe_failure(
    monkeypatch, error_type
):
    def unavailable(_root):
        raise error_type("packaged recipes unavailable")

    monkeypatch.setattr(sidecars, "_image_specs", unavailable)
    monkeypatch.setattr(
        sidecars, "_docker_adapter", lambda: FakeDocker(_cp(1, stderr="No such object"))
    )
    result = sidecars.inspect_sidecars(SandboxHostPolicy())
    assert [finding.resource for finding in result.findings] == [
        "sidecar-images",
        "network",
        "proxy",
        "reaper",
    ]
    assert result.findings[0].state is sidecars.SidecarState.ERROR


@pytest.mark.parametrize("blocker", ["foreign", "active-session", "foreign-proxy"])
def test_check_only_stale_network_blockers_are_errors(monkeypatch, tmp_path, capsys, blocker):
    from tests.harness.test_bootstrap import (
        _assert_complete,
        _inspection_environment,
        _run_inspection,
        _tree_snapshot,
    )

    _inspection_environment(monkeypatch, tmp_path)
    attached = ("foreign",) if blocker == "foreign" else ()
    monkeypatch.setattr(
        sidecars, "_inspect_network", lambda _docker: sidecars._NetworkState(True, False, attached)
    )
    active = (_active_session("session-a"),) if blocker == "active-session" else ()
    monkeypatch.setattr(sidecars, "_active_sessions", lambda _docker: active)
    if blocker == "foreign-proxy":
        monkeypatch.setattr(
            sidecars,
            "_inspect_container",
            lambda *_args: sidecars._ContainerState(True, labels={}),
        )
    before = _tree_snapshot(tmp_path)
    status, output = _run_inspection(capsys)
    assert "✗  network:" in output, "blocked network must be ERROR"
    assert status == 2
    _assert_complete(output)
    assert ("foreign" if blocker != "active-session" else "shut down") in output
    assert _tree_snapshot(tmp_path) == before


def test_check_only_stale_container_with_active_session_is_error(monkeypatch, tmp_path, capsys):
    from tests.harness.test_bootstrap import (
        _assert_complete,
        _inspection_environment,
        _run_inspection,
    )

    _inspection_environment(monkeypatch, tmp_path)
    images = {
        spec.reference: ("sha", sidecars._image_labels(spec))
        for spec in sidecars._image_specs(None)
    }
    monkeypatch.setattr(sidecars, "_inspect_image", lambda name, _docker: images[name])
    monkeypatch.setattr(
        sidecars,
        "_inspect_container",
        lambda name, _docker: (
            sidecars._ContainerState(True, "old-sha", True, {sidecars.ROLE_LABEL: "egress-proxy"})
            if name == sidecars.legacy.PROXY_CONTAINER
            else sidecars._ContainerState(False)
        ),
    )
    monkeypatch.setattr(
        sidecars, "_active_sessions", lambda _docker: (_active_session("session-a"),)
    )
    status, output = _run_inspection(capsys)
    assert "✗  proxy:" in output, "blocked container must be ERROR"
    assert status == 2
    _assert_complete(output)
    assert "shut down" in output


def test_check_only_pending_container_keeps_differences(monkeypatch, tmp_path, capsys):
    from tests.harness.test_bootstrap import _inspection_environment, _run_inspection

    _inspection_environment(monkeypatch, tmp_path)
    images = {
        spec.reference: ("sha", sidecars._image_labels(spec))
        for spec in sidecars._image_specs(None)
    }
    monkeypatch.setattr(sidecars, "_inspect_image", lambda name, _docker: images[name])
    monkeypatch.setattr(sidecars, "_active_sessions", lambda _docker: ())
    monkeypatch.setattr(
        sidecars, "_inspect_container", lambda *_args: sidecars._ContainerState(False)
    )
    status, output = _run_inspection(capsys)
    assert "image_identity" in output, "pending containers must explain desired-state differences"
    assert status == 1


# --- Host Bootstrap under host ``[sandbox] egress = "unproxied"`` (issue #2215) ---

_UNPROXIED = SandboxHostPolicy(egress="unproxied")
_SIDECAR_RESOURCES = ["proxy-image", "reaper-image", "network", "proxy", "reaper"]


class _EgressDocker:
    """Stateful Docker fake covering the sidecar, network, and Session commands."""

    def __init__(self) -> None:
        self.calls: list[list[str]] = []
        self.images: dict[str, tuple[str, dict[str, str]]] = {}
        self.containers: dict[str, dict[str, object]] = {}
        self.network = False

    def add_current_image(self, reference: str) -> None:
        spec = next(spec for spec in sidecars._image_specs(None) if spec.reference == reference)
        self.images[reference] = (f"sha256:{reference}", sidecars._image_labels(spec))

    def add_container(
        self,
        name: str,
        *,
        image: str = "sha256:unrelated",
        labels: dict[str, str] | None = None,
        running: bool = True,
        networks: tuple[str, ...] = (),
    ) -> None:
        self.containers[name] = {
            "image": image,
            "labels": dict(labels or {}),
            "running": running,
            "networks": set(networks),
        }

    def add_sidecar(self, name: str, role: str, image: str, policy: SandboxHostPolicy) -> None:
        self.add_current_image(image)
        self.add_container(
            name,
            image=self.images[image][0],
            labels={
                sidecars.ROLE_LABEL: role,
                sidecars.LABEL_POLICY_FINGERPRINT: sidecars.policy_fingerprint(policy),
            },
            networks=(sidecars.legacy.EGRESS_NETWORK,) if role == "egress-proxy" else (),
        )

    def add_session(
        self,
        name: str,
        *,
        running: bool = True,
        networks: tuple[str, ...] = (),
        state: str | None = None,
    ) -> None:
        """Add a Sandbox; ``state`` overrides the ``ps -a`` State derived from ``running``."""
        self.add_container(
            name,
            labels={
                sidecars.ROLE_LABEL: sidecars.SESSION_ROLE,
                sidecars._DEVCONTAINER_FOLDER_LABEL: "/projects/a",
            },
            running=running,
            networks=networks,
        )
        if state is not None:
            self.containers[name]["state"] = state

    def mutations(self) -> list[list[str]]:
        return [call for call in self.calls if "inspect" not in call and call[0] != "ps"]

    def run(self, args: list[str], *, timeout: int = 30) -> subprocess.CompletedProcess[str]:
        del timeout
        self.calls.append(list(args))
        handler = {
            ("image", "inspect"): self._inspect_image,
            ("container", "inspect"): self._inspect_container,
            ("network", "inspect"): self._inspect_network,
            ("network", "create"): self._create_network,
            ("network", "rm"): self._remove_network,
            ("network", "connect"): self._connect,
        }.get((args[0], args[1]))
        if handler is not None:
            return handler(args)
        return {
            "ps": self._ps,
            "rm": self._remove,
            "build": self._build,
            "run": self._run,
            "start": lambda _args: _cp(),
        }[args[0]](args)

    def _inspect_image(self, args: list[str]) -> subprocess.CompletedProcess[str]:
        if args[2] not in self.images:
            return _cp(1, stderr="No such image")
        image_id, labels = self.images[args[2]]
        return _cp(stdout=json.dumps({"Id": image_id, "Config": {"Labels": labels}}))

    def _inspect_container(self, args: list[str]) -> subprocess.CompletedProcess[str]:
        entry = self.containers.get(args[2])
        if entry is None:
            return _cp(1, stderr="No such container")
        document = {
            "Image": entry["image"],
            "Config": {"Labels": entry["labels"]},
            "State": {"Running": entry["running"]},
            "NetworkSettings": {"Networks": {name: {} for name in entry["networks"]}},
            "Mounts": [],
        }
        return _cp(stdout=json.dumps(document))

    def _inspect_network(self, _args: list[str]) -> subprocess.CompletedProcess[str]:
        if not self.network:
            return _cp(1, stderr="network booley-egress-v2 not found")
        attached = {
            f"id-{name}": {"Name": name}
            for name, entry in self.containers.items()
            if entry["running"] and sidecars.legacy.EGRESS_NETWORK in entry["networks"]
        }
        document = {
            "Internal": True,
            "Options": {
                sidecars.legacy.GATEWAY_MODE_OPTION: sidecars.legacy.GATEWAY_MODE_ISOLATED
            },
            "Containers": attached,
        }
        return _cp(stdout=json.dumps(document))

    def _create_network(self, _args: list[str]) -> subprocess.CompletedProcess[str]:
        self.network = True
        return _cp()

    def _remove_network(self, _args: list[str]) -> subprocess.CompletedProcess[str]:
        self.network = False
        return _cp()

    def _connect(self, args: list[str]) -> subprocess.CompletedProcess[str]:
        self.containers[args[3]]["networks"].add(args[2])  # type: ignore[union-attr]
        return _cp()

    def _ps(self, args: list[str]) -> subprocess.CompletedProcess[str]:
        rows = []
        for name, entry in sorted(self.containers.items()):
            role = entry["labels"].get(sidecars.ROLE_LABEL)  # type: ignore[union-attr]
            if role != sidecars.SESSION_ROLE:
                continue
            if "-a" in args:
                state = entry.get("state") or ("running" if entry["running"] else "exited")
                rows.append(f"{name}\t{name}\t{state}")
            elif entry["running"]:
                rows.append(f"{name}\t{name}")
        return _cp(stdout="".join(f"{row}\n" for row in rows))

    def _remove(self, args: list[str]) -> subprocess.CompletedProcess[str]:
        self.containers.pop(args[-1], None)
        return _cp()

    def _build(self, args: list[str]) -> subprocess.CompletedProcess[str]:
        labels = dict(
            args[index + 1].split("=", 1) for index, arg in enumerate(args) if arg == "--label"
        )
        self.images[args[2]] = (f"sha256:built-{args[2]}", labels)
        return _cp()

    def _run(self, args: list[str]) -> subprocess.CompletedProcess[str]:
        labels = dict(
            args[index + 1].split("=", 1) for index, arg in enumerate(args) if arg == "--label"
        )
        image = args[-1]
        self.add_container(
            args[args.index("--name") + 1], image=self.images[image][0], labels=labels
        )
        return _cp()


def _egress_docker(monkeypatch: pytest.MonkeyPatch) -> _EgressDocker:
    docker = _EgressDocker()
    monkeypatch.setattr(sidecars, "_docker_adapter", lambda: docker)
    monkeypatch.setattr(sidecars, "ensure_docker_build_capacity", lambda *_a, **_kw: None)
    return docker


def _leftover_egress(docker: _EgressDocker, *, reaper: bool = True) -> None:
    """Give the fake a proxy-mode installation: images, network, proxy, reaper."""
    docker.network = True
    docker.add_sidecar(
        sidecars.legacy.PROXY_CONTAINER, "egress-proxy", sidecars.legacy.PROXY_IMAGE, _UNPROXIED
    )
    docker.add_current_image(sidecars.legacy.REAPER_IMAGE)
    if reaper:
        docker.add_sidecar(
            sidecars.legacy.REAPER_CONTAINER, "reaper", sidecars.legacy.REAPER_IMAGE, _UNPROXIED
        )


def _states(result: sidecars.SidecarResult) -> list[tuple[str, sidecars.SidecarState]]:
    return [(finding.resource, finding.state) for finding in result.findings]


def test_policy_fingerprint_ignores_egress_mode() -> None:
    proxy = SandboxHostPolicy(600, 2, egress="proxy")
    unproxied = SandboxHostPolicy(600, 2, egress="unproxied")
    assert sidecars.policy_fingerprint(proxy) == sidecars.policy_fingerprint(unproxied)


def test_unproxied_bootstrap_builds_and_runs_only_the_reaper(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    docker = _egress_docker(monkeypatch)

    result = sidecars.reconcile_sidecars(_UNPROXIED, Intent.ENSURE)

    current, changed = sidecars.SidecarState.CURRENT, sidecars.SidecarState.CHANGED
    assert _states(result) == list(
        zip(_SIDECAR_RESOURCES, [current, changed, current, current, changed], strict=True)
    ), "unproxied Bootstrap must build and run only the reaper"
    assert result.ready
    built = [call[2] for call in docker.calls if call[0] == "build"]
    assert built == [sidecars.legacy.REAPER_IMAGE], "only the reaper image may be built"
    started = [call[call.index("--name") + 1] for call in docker.calls if call[0] == "run"]
    assert started == [sidecars.legacy.REAPER_CONTAINER], "only the reaper may run"
    assert not any(call[:2] == ["network", "create"] for call in docker.calls)
    assert not any(sidecars.legacy.PROXY_IMAGE in call for call in docker.calls)


def test_unproxied_bootstrap_removes_idle_proxy_and_network(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    docker = _egress_docker(monkeypatch)
    _leftover_egress(docker)

    result = sidecars.reconcile_sidecars(_UNPROXIED, Intent.ENSURE)

    current, changed = sidecars.SidecarState.CURRENT, sidecars.SidecarState.CHANGED
    assert _states(result) == list(
        zip(_SIDECAR_RESOURCES, [current, current, changed, changed, current], strict=True)
    ), "an idle leftover proxy and network must be removed"
    assert docker.mutations() == [
        ["rm", "-f", sidecars.legacy.PROXY_CONTAINER],
        ["network", "rm", sidecars.legacy.EGRESS_NETWORK],
    ]
    assert sidecars.legacy.PROXY_CONTAINER not in docker.containers
    assert not docker.network


@pytest.mark.parametrize("blocker", ["running", "parked-predecessor", "foreign-attachment"])
def test_unproxied_bootstrap_keeps_proxy_while_sandboxes_exist(
    monkeypatch: pytest.MonkeyPatch, blocker: str
) -> None:
    docker = _egress_docker(monkeypatch)
    _leftover_egress(docker, reaper=False)
    if blocker == "running":
        docker.add_session("booley-session-a", networks=(sidecars.legacy.EGRESS_NETWORK,))
        expected = ["booley-session-a", "/projects/a", "booley session down --project"]
    elif blocker == "parked-predecessor":
        name = "booley-session-a-pre-refresh"
        docker.add_session(name, running=False)
        expected = [name, "/projects/a", "docker rename", "booley bootstrap"]
    else:
        docker.add_container("someone-else", networks=(sidecars.legacy.EGRESS_NETWORK,))
        expected = ["someone-else", "detach foreign containers"]

    result = sidecars.reconcile_sidecars(_UNPROXIED, Intent.ENSURE)

    current, changed = sidecars.SidecarState.CURRENT, sidecars.SidecarState.CHANGED
    error = sidecars.SidecarState.ERROR
    assert _states(result) == list(
        zip(_SIDECAR_RESOURCES, [current, current, error, error, changed], strict=True)
    ), "blockers must keep the leftover egress; the reaper is still reconciled"
    for finding in result.findings[2:4]:
        for fragment in expected:
            assert fragment in finding.detail
    assert not any(call[0] == "rm" or call[:2] == ["network", "rm"] for call in docker.calls)
    assert docker.network
    assert sidecars.legacy.PROXY_CONTAINER in docker.containers


def test_unproxied_mode_flip_keeps_current_reaper(monkeypatch: pytest.MonkeyPatch) -> None:
    docker = _egress_docker(monkeypatch)
    docker.add_sidecar(
        sidecars.legacy.REAPER_CONTAINER,
        "reaper",
        sidecars.legacy.REAPER_IMAGE,
        SandboxHostPolicy(),
    )

    result = sidecars.reconcile_sidecars(
        replace(SandboxHostPolicy(), egress="unproxied"), Intent.ENSURE
    )

    assert _states(result) == [
        (resource, sidecars.SidecarState.CURRENT) for resource in _SIDECAR_RESOURCES
    ], "a mode flip must leave every sidecar row current"
    assert docker.mutations() == [], "a mode flip alone must not touch the reaper"


def test_readiness_check_accepts_unproxied_leftover_proxy(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    docker = _egress_docker(monkeypatch)
    _leftover_egress(docker)
    docker.add_session("booley-session-a", networks=(sidecars.legacy.EGRESS_NETWORK,))

    result = sidecars.reconcile_sidecars(_UNPROXIED, Intent.CHECK)

    assert _states(result) == [
        (resource, sidecars.SidecarState.CURRENT) for resource in _SIDECAR_RESOURCES
    ]
    assert not any(call[0] == "ps" for call in docker.calls), "readiness never enumerates"
    assert docker.mutations() == []


def _unproxied_inspection(monkeypatch, tmp_path) -> _EgressDocker:
    from tests.harness.test_bootstrap import _inspection_environment

    _inspection_environment(monkeypatch, tmp_path)
    monkeypatch.setattr(bootstrap, "load_host_policy", lambda **_kwargs: _UNPROXIED)
    docker = _egress_docker(monkeypatch)
    docker.add_sidecar(
        sidecars.legacy.REAPER_CONTAINER, "reaper", sidecars.legacy.REAPER_IMAGE, _UNPROXIED
    )
    return docker


def test_check_only_unproxied_absent_proxy_and_network_are_current(monkeypatch, tmp_path, capsys):
    from tests.harness.test_bootstrap import _assert_complete, _run_inspection

    docker = _unproxied_inspection(monkeypatch, tmp_path)

    status, output = _run_inspection(capsys)

    _assert_complete(output)
    for resource in ("proxy-image", "reaper-image", "network", "proxy", "reaper"):
        assert f"=  {resource}:" in output, f"{resource} must be current"
    assert not any(sidecars.legacy.PROXY_IMAGE in call for call in docker.calls)
    assert status == 1, "only the helper's stale base image stays pending"
    assert docker.mutations() == []


@pytest.mark.parametrize("blocker", ["idle", "running"])
def test_check_only_unproxied_reports_leftover_proxy_and_network(
    monkeypatch, tmp_path, capsys, blocker
):
    from tests.harness.test_bootstrap import _assert_complete, _run_inspection

    docker = _unproxied_inspection(monkeypatch, tmp_path)
    _leftover_egress(docker)
    if blocker == "running":
        docker.add_session("booley-session-a", networks=(sidecars.legacy.EGRESS_NETWORK,))

    status, output = _run_inspection(capsys)

    _assert_complete(output)
    glyph = "+" if blocker == "idle" else "✗"
    for resource in ("network", "proxy"):
        line = next(line for line in output.splitlines() if f" {resource}:" in line)
        assert glyph in line, f"leftover {resource} must be {blocker}-reported"
        assert "booley bootstrap" in line
        if blocker == "running":
            assert "booley-session-a" in line
    assert status == (1 if blocker == "idle" else 2)
    assert docker.mutations() == []


@pytest.mark.parametrize(
    ("intent", "lead"),
    [
        (Intent.ENSURE, "Bootstrap's recovery left it in place; rename it back"),
        (Intent.CHECK, "run `booley bootstrap` to recover it; if it remains, rename it back"),
    ],
    ids=["mutating", "check-only"],
)
def test_parked_predecessor_remedy_reflects_whether_recovery_ran(
    monkeypatch: pytest.MonkeyPatch, intent: Intent, lead: str
) -> None:
    docker = _egress_docker(monkeypatch)
    _leftover_egress(docker)
    docker.add_session("booley-session-a-pre-refresh", running=False)

    if intent is Intent.CHECK:
        result = sidecars.inspect_sidecars(_UNPROXIED)
    else:
        result = sidecars.reconcile_sidecars(_UNPROXIED, intent)

    proxy = next(finding for finding in result.findings if finding.resource == "proxy")
    assert proxy.state is sidecars.SidecarState.ERROR
    assert lead in proxy.detail, "mutating Bootstrap already ran recovery; never re-prescribe it"


def test_restarting_predecessor_is_listed_once(monkeypatch: pytest.MonkeyPatch) -> None:
    docker = _egress_docker(monkeypatch)
    _leftover_egress(docker)
    name = "booley-session-a-pre-refresh"
    docker.add_session(name, state="restarting")

    result = sidecars.reconcile_sidecars(_UNPROXIED, Intent.ENSURE)

    proxy = next(finding for finding in result.findings if finding.resource == "proxy")
    assert proxy.state is sidecars.SidecarState.ERROR
    assert proxy.detail.count(name) == 1, "an active predecessor must not also be listed parked"


def test_unproxied_reaper_image_error_skips_retirement(monkeypatch: pytest.MonkeyPatch) -> None:
    docker = _egress_docker(monkeypatch)
    _leftover_egress(docker, reaper=False)
    docker.images[sidecars.legacy.REAPER_IMAGE] = ("sha256:foreign", {})

    result = sidecars.reconcile_sidecars(_UNPROXIED, Intent.ENSURE)

    assert _states(result) == [
        ("proxy-image", sidecars.SidecarState.CURRENT),
        ("reaper-image", sidecars.SidecarState.ERROR),
    ]
    assert docker.mutations() == [], "an image error must stop before any retirement mutation"


def test_unproxied_network_removal_failure_reports_removed_proxy(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    docker = _egress_docker(monkeypatch)
    _leftover_egress(docker)
    monkeypatch.setattr(docker, "_remove_network", lambda _args: _cp(1, stderr="in use"))

    result = sidecars.reconcile_sidecars(_UNPROXIED, Intent.ENSURE)

    rows = {finding.resource: finding for finding in result.findings}
    assert rows["network"].state is sidecars.SidecarState.ERROR
    assert "in use" in rows["network"].detail
    assert rows["proxy"].state is sidecars.SidecarState.CHANGED
    assert sidecars.legacy.PROXY_CONTAINER not in docker.containers
