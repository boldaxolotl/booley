from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from booley.config.host_config import InteractiveHostPolicy
from booley.harness import host_sidecars as sidecars
from booley.harness.image_lifecycle import Intent


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


def test_source_fingerprint_wraps_unreadable_inputs(tmp_path: Path) -> None:
    missing = tmp_path / "missing"
    spec = sidecars._ImageSpec("image", "image", "kind", missing, tmp_path, ())
    with pytest.raises(sidecars.SidecarError, match="cannot fingerprint"):
        _ = spec.fingerprint


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
    assert "booley session down --project-root" in detail
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
    monkeypatch.setattr(sidecars, "_reconcile_image", lambda *_args: image_error)
    result = sidecars.reconcile_sidecars(InteractiveHostPolicy(), Intent.CHECK)
    assert result.findings == (image_error,)
    assert not result.ready

    image_ok = sidecars.SidecarFinding("image", sidecars.SidecarState.CURRENT, "ok")
    network_error = sidecars.SidecarFinding("network", sidecars.SidecarState.ERROR, "bad")
    monkeypatch.setattr(sidecars, "_reconcile_image", lambda *_args: image_ok)
    monkeypatch.setattr(sidecars, "_reconcile_network", lambda *_args: network_error)
    result = sidecars.reconcile_sidecars(InteractiveHostPolicy(), Intent.CHECK)
    assert result.findings[-1] is network_error

    network_ok = sidecars.SidecarFinding("network", sidecars.SidecarState.CURRENT, "ok")
    proxy_error = sidecars.SidecarFinding("proxy", sidecars.SidecarState.ERROR, "bad")
    monkeypatch.setattr(sidecars, "_reconcile_network", lambda *_args: network_ok)
    monkeypatch.setattr(sidecars, "_reconcile_container", lambda *_args: proxy_error)
    result = sidecars.reconcile_sidecars(InteractiveHostPolicy(), Intent.CHECK)
    assert result.findings[-1] is proxy_error


def test_reconcile_sidecars_wraps_invalid_image_specs(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(sidecars, "_docker_adapter", FakeDocker)

    def fail(_root):
        raise sidecars.SidecarError("package missing")

    monkeypatch.setattr(sidecars, "_image_specs", fail)
    result = sidecars.reconcile_sidecars(InteractiveHostPolicy(), Intent.CHECK)
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
        lambda spec, *_args: sidecars.SidecarFinding(
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
    result = sidecars.reconcile_sidecars(InteractiveHostPolicy(), Intent.ENSURE)
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
    dockerfile = tmp_path / "Dockerfile"
    dockerfile.write_text("FROM scratch\n", encoding="utf-8")
    spec = sidecars._ImageSpec("image", "image", "kind", dockerfile, tmp_path, ())
    inspections = iter((None, ("sha", {})))
    monkeypatch.setattr(sidecars, "_inspect_image", lambda *_args: next(inspections))
    finding = sidecars._reconcile_image(spec, Intent.ENSURE, FakeDocker())
    assert finding.state is sidecars.SidecarState.ERROR
    assert "expected provenance" in finding.detail


def test_image_ownership_rejects_wrong_kind(tmp_path: Path) -> None:
    spec = sidecars._ImageSpec("image", "image", "kind", tmp_path, tmp_path, ())
    labels = {
        sidecars.LABEL_SIDECAR_SCHEMA: sidecars.IMAGE_SCHEMA,
        sidecars.LABEL_SIDECAR_KIND: "other",
        sidecars.LABEL_SOURCE_FINGERPRINT: "source",
        sidecars.LABEL_BOOLEY_VERSION: "version",
    }
    with pytest.raises(sidecars.SidecarError, match="kind does not match"):
        sidecars._verify_image_ownership(spec, ("sha", labels))


def test_build_image_reports_docker_failure(tmp_path: Path) -> None:
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
    policy = InteractiveHostPolicy(600, 2, ("example.com",))
    proxy = sidecars._proxy_run_args(policy, "fingerprint")
    reaper = sidecars._reaper_run_args(policy, "fingerprint")
    assert 'PROXY_ALLOWLIST=["example.com"]' in proxy
    assert f"{sidecars.ROLE_LABEL}=egress-proxy" in proxy
    assert "BOOLEY_IDLE_TIMEOUT_SECONDS=600" in reaper
    assert "BOOLEY_MAX_SESSIONS=2" in reaper
    assert "PROXY_ALLOWLIST" not in " ".join(
        sidecars._proxy_run_args(InteractiveHostPolicy(), "fingerprint")
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
