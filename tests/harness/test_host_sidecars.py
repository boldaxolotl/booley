from __future__ import annotations

import json
import os
import re
import shlex
import shutil
from dataclasses import dataclass, replace
from pathlib import Path

import pytest

import booley
from booley.config.host_config import SandboxHostPolicy
from booley.harness import bootstrap
from booley.harness import host_sidecars as sidecars
from booley.runtime import session_runtime
from booley.runtime.docker_capacity import SKIP_PREFLIGHT_ENV
from booley.runtime.image_lifecycle import Intent
from tests.fake_docker_executable import install_fake_docker
from tests.harness.session_docker_fake import route_session_commands, run_cli, serve_sandbox_docker
from tests.harness.sidecar_docker_fake import EgressDocker, serve_egress_docker

# `test_init_bootstrap.py` borrows the fake through this name.
_egress_docker = serve_egress_docker

_POLICY = SandboxHostPolicy(600, 2, ("example.com",))
_UNPROXIED = SandboxHostPolicy(egress="unproxied")
_SIDECAR_RESOURCES = ["proxy-image", "reaper-image", "network", "proxy", "reaper"]
_PROXY = sidecars.legacy.PROXY_CONTAINER
_REAPER = sidecars.legacy.REAPER_CONTAINER
_NETWORK = sidecars.legacy.EGRESS_NETWORK
_CURRENT = sidecars.SidecarState.CURRENT
_CHANGED = sidecars.SidecarState.CHANGED
_PENDING = sidecars.SidecarState.PENDING
_ERROR = sidecars.SidecarState.ERROR


@pytest.fixture
def docker(monkeypatch: pytest.MonkeyPatch) -> EgressDocker:
    return serve_egress_docker(monkeypatch)


def _states(result: sidecars.SidecarResult) -> list[tuple[str, sidecars.SidecarState]]:
    return [(finding.resource, finding.state) for finding in result.findings]


def _row(result: sidecars.SidecarResult, resource: str) -> sidecars.SidecarFinding:
    return next(finding for finding in result.findings if finding.resource == resource)


def _step(call: list[str]) -> str:
    """Name one mutating Docker call: its verb and, for build and run, its target."""
    if call[0] == "build":
        return f"build {call[2]}"
    if call[0] == "run":
        return f"run {call[call.index('--name') + 1]}"
    return " ".join(call[:2]) if call[0] == "network" else " ".join(call[:3])


def _install(docker: EgressDocker, policy: SandboxHostPolicy = _POLICY) -> None:
    """Converge an empty fake to a complete proxy-mode installation, then forget the calls."""
    assert sidecars.reconcile_sidecars(policy, Intent.ENSURE).ready
    docker.calls.clear()


def _damage(docker: EgressDocker, kind: str) -> None:
    """Break one resource of an installed fake."""
    if kind == "stopped-proxy":
        docker.containers[_PROXY]["running"] = False
    elif kind == "disconnected-proxy":
        docker.containers[_PROXY]["networks"] = set()
    elif kind == "stale-reaper":
        docker.containers[_REAPER]["labels"][sidecars.LABEL_POLICY_FINGERPRINT] = "old"  # type: ignore[index]
    elif kind == "moved-socket":
        docker.endpoint = "unix:///run/user/1000/docker.sock"
    elif kind == "stale-network":
        docker.stale_network = True
    elif kind == "missing-network":
        docker.network = False
    else:
        assert kind == "missing-proxy", kind
        del docker.containers[_PROXY]


def _recipes(package: Path) -> None:
    """Write the two Dockerfiles and three sources the sidecar images are built from."""
    for name in ("Dockerfile.egress-proxy", "Dockerfile.reaper"):
        path = package / "data" / "docker" / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"FROM scratch\n")
    for name in ("egress_proxy.py", "proxy_entry.py", "reaper.py"):
        path = package / "docker" / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(f"print({name!r})\n".encode())


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
    proxy = SandboxHostPolicy(600, 2, egress="proxy")
    unproxied = SandboxHostPolicy(600, 2, egress="unproxied")
    assert sidecars.policy_fingerprint(proxy) == sidecars.policy_fingerprint(unproxied)


def test_ensure_converges_a_missing_installation_then_stays_current(docker: EgressDocker) -> None:
    result = sidecars.reconcile_sidecars(_POLICY, Intent.ENSURE)

    assert result.ready
    assert _states(result) == [(resource, _CHANGED) for resource in _SIDECAR_RESOURCES]
    assert [_step(call) for call in docker.mutations()] == [
        f"build {sidecars.legacy.PROXY_IMAGE}",
        f"build {sidecars.legacy.REAPER_IMAGE}",
        "network create",
        f"run {_PROXY}",
        "network connect",
        f"run {_REAPER}",
    ]
    proxy = next(call for call in docker.calls if _step(call) == f"run {_PROXY}")
    assert 'PROXY_ALLOWLIST=["example.com"]' in proxy
    assert f"{sidecars.ROLE_LABEL}=egress-proxy" in proxy
    reaper = next(call for call in docker.calls if _step(call) == f"run {_REAPER}")
    assert "BOOLEY_IDLE_TIMEOUT_SECONDS=600" in reaper
    assert not any("BOOLEY_MAX_SESSIONS=" in arg for arg in reaper)
    assert ["network", "connect", _NETWORK, _PROXY] in docker.calls
    assert not any(call[0] == "ps" for call in docker.calls), "a fresh install enumerates nothing"

    docker.calls.clear()
    again = sidecars.reconcile_sidecars(_POLICY, Intent.ENSURE)

    assert _states(again) == [(resource, _CURRENT) for resource in _SIDECAR_RESOURCES]
    assert docker.mutations() == []


@pytest.mark.parametrize("layout", ["installed", "source"])
def test_image_recipes_are_fingerprinted_in_either_layout(
    docker: EgressDocker, tmp_path: Path, layout: str
) -> None:
    package = tmp_path if layout == "installed" else tmp_path / "src" / "booley"
    _recipes(package)
    policy = SandboxHostPolicy()

    assert sidecars.reconcile_sidecars(policy, Intent.ENSURE, booley_root=tmp_path).ready
    build = next(call for call in docker.calls if _step(call).startswith("build"))
    assert build[build.index("-f") + 1] == str(
        package / "data" / "docker" / "Dockerfile.egress-proxy"
    )

    (package / "docker" / "reaper.py").write_bytes(b"print('edited')\n")
    edited = sidecars.reconcile_sidecars(policy, Intent.CHECK, booley_root=tmp_path)
    assert _row(edited, "reaper-image").state is _PENDING
    assert sidecars.LABEL_SOURCE_FINGERPRINT in _row(edited, "reaper-image").detail
    assert _row(edited, "proxy-image").state is _CURRENT

    (package / "docker" / "proxy_entry.py").unlink()
    broken = sidecars.reconcile_sidecars(policy, Intent.CHECK, booley_root=tmp_path)
    assert _row(broken, "proxy-image").state is _ERROR
    assert "cannot fingerprint sidecar input" in _row(broken, "proxy-image").detail


@pytest.mark.parametrize(
    ("case", "resource", "fragments"),
    [
        ("old-booley-image", "reaper-image", [f"{booley.__version__} -> 0.0.0"]),
        ("unstamped-container", "reaper", ["stale", "source_fingerprint {policy} -> <none>"]),
        ("stale-network", "network", ["has stale routing policy"]),
        ("moved-socket", "reaper", ["stale", "source_fingerprint"]),
    ],
)
def test_check_reports_drift_without_mutating(
    docker: EgressDocker, case: str, resource: str, fragments: list[str]
) -> None:
    _install(docker)
    if case == "old-booley-image":
        docker.images[sidecars.legacy.REAPER_IMAGE][1][sidecars.LABEL_BOOLEY_VERSION] = "0.0.0"
    elif case == "unstamped-container":
        docker.containers[_REAPER]["labels"] = {sidecars.ROLE_LABEL: "reaper"}
    else:
        _damage(docker, case)

    row = _row(sidecars.reconcile_sidecars(_POLICY, Intent.CHECK), resource)

    assert row.state is _PENDING
    for fragment in fragments:
        assert fragment.format(policy=sidecars.policy_fingerprint(_POLICY)) in row.detail
    if case == "old-booley-image":
        assert sidecars.LABEL_BOOLEY_VERSION in row.detail
        assert sidecars.LABEL_SOURCE_FINGERPRINT not in row.detail
    if case == "unstamped-container":
        assert "image_identity" not in row.detail
    assert docker.mutations() == []


@pytest.mark.parametrize(
    ("kind", "expected"),
    [
        ("stopped-proxy", [f"start {_PROXY}"]),
        ("disconnected-proxy", ["network connect"]),
        ("stale-reaper", [f"rm -f {_REAPER}", f"run {_REAPER}"]),
        ("moved-socket", [f"rm -f {_REAPER}", f"run {_REAPER}"]),
        (
            "stale-network",
            [
                f"rm -f {_PROXY}",
                "network rm",
                "network create",
                f"run {_PROXY}",
                "network connect",
            ],
        ),
    ],
)
def test_ensure_repairs_damaged_resources(
    docker: EgressDocker, kind: str, expected: list[str]
) -> None:
    _install(docker)
    _damage(docker, kind)

    result = sidecars.reconcile_sidecars(_POLICY, Intent.ENSURE)

    assert result.ready
    assert [_step(call) for call in docker.mutations()] == expected


def test_plan_image_builds_selects_missing_and_refresh_requested_images(
    docker: EgressDocker,
) -> None:
    docker.add_current_image(sidecars.legacy.PROXY_IMAGE)

    ensure = sidecars.plan_image_builds(Intent.ENSURE, docker=docker)
    refresh = sidecars.plan_image_builds(Intent.REFRESH, docker=docker)

    assert [request.output_tag for request in ensure] == [sidecars.legacy.REAPER_IMAGE]
    assert [request.output_tag for request in refresh] == [
        sidecars.legacy.PROXY_IMAGE,
        sidecars.legacy.REAPER_IMAGE,
    ]
    assert all(request.estimate_class.value == "thin-overlay" for request in (*ensure, *refresh))

    docker.images[sidecars.legacy.PROXY_IMAGE] = ("sha256:foreign", {})
    with pytest.raises(sidecars.SidecarError, match="foreign image collision"):
        sidecars.plan_image_builds(Intent.ENSURE, docker=docker)


@pytest.mark.parametrize(
    ("case", "resource", "fragment"),
    [
        ("proxy-image-unlabelled", "proxy-image", "foreign image collision"),
        ("reaper-image-kind", "reaper-image", f"{sidecars.LABEL_SIDECAR_KIND} reaper -> other"),
        ("proxy-container-role", "proxy", "foreign name collision"),
        (
            "network-foreign-attachment",
            "network",
            "attached foreign containers were not modified: someone-else",
        ),
    ],
)
def test_foreign_resources_are_never_mutated(
    docker: EgressDocker, case: str, resource: str, fragment: str
) -> None:
    _install(docker)
    if case == "proxy-image-unlabelled":
        unlabelled = json.dumps({"Id": "sha", "Config": {"Labels": None}})
        docker.reply(
            "image", "inspect", sidecars.legacy.PROXY_IMAGE, returncode=0, stdout=unlabelled
        )
    elif case == "reaper-image-kind":
        docker.images[sidecars.legacy.REAPER_IMAGE][1][sidecars.LABEL_SIDECAR_KIND] = "other"
    elif case == "proxy-container-role":
        docker.containers[_PROXY]["labels"][sidecars.ROLE_LABEL] = "someone-else"  # type: ignore[index]
    else:
        _damage(docker, "stale-network")
        docker.add_container("someone-else", networks=(_NETWORK,))

    result = sidecars.reconcile_sidecars(_POLICY, Intent.ENSURE)

    assert result.findings[-1].resource == resource, "reconciliation stops at the foreign resource"
    assert result.findings[-1].state is _ERROR
    assert fragment in result.findings[-1].detail
    assert docker.mutations() == []


@pytest.mark.parametrize("kind", ["stale-network", "stale-reaper"])
def test_active_sessions_block_stale_replacement(
    docker: EgressDocker, tmp_path: Path, kind: str
) -> None:
    _install(docker)
    _damage(docker, kind)
    # The folder label names the Project; the conflicting `/work` bind must not override it.
    docker.add_session("booley-session-vscode", workspace="/projects/elsewhere")
    root = tmp_path / "proj"
    docker.add_headless_session("booley-session-headless", root)

    result = sidecars.reconcile_sidecars(_POLICY, Intent.ENSURE)

    detail = result.findings[-1].detail
    assert result.findings[-1].state is _ERROR
    assert (
        "booley-session-vscode (Project /projects/a; "
        "close its VS Code window or stop it with Dev Containers)"
    ) in detail
    assert (
        f"booley-session-headless (Project {root}; run `booley session down --project {root}`)"
    ) in detail
    assert "/projects/elsewhere" not in detail
    assert not any(call[0] == "rm" or call[:2] == ["network", "rm"] for call in docker.calls)


@pytest.mark.parametrize(
    ("endpoint", "version", "source"),
    [
        pytest.param(
            "unix:///run/user/1000/docker.sock",
            "linux\tDocker Engine - Community",
            "/run/user/1000/docker.sock",
            id="rootless-docker-host",
        ),
        pytest.param(
            "unix:///srv/docker/second.sock",
            "linux\tDocker Engine - Community",
            "/srv/docker/second.sock",
            id="second-daemon-context",
        ),
        pytest.param(
            "unix:///var/run/docker.sock",
            "linux\tDocker Engine - Community",
            "/var/run/docker.sock",
            id="default-socket",
        ),
        pytest.param(
            "npipe:////./pipe/dockerDesktopLinuxEngine",
            "windows\tDocker Desktop 4.37.1 (178610)",
            "/var/run/docker.sock",
            id="desktop-windows-npipe",
        ),
        pytest.param(
            "unix:///Users/dev/.docker/run/docker.sock",
            "darwin\tDocker Desktop 4.37.1 (178610)",
            "/var/run/docker.sock",
            id="desktop-macos-user-socket",
        ),
        pytest.param(
            "unix:///home/dev/.docker/desktop/docker.sock",
            "linux\tDocker Desktop 4.37.1 (178610)",
            "/var/run/docker.sock",
            id="desktop-linux",
        ),
        pytest.param(
            "unix:///Users/dev/.colima/default/docker.sock",
            "darwin\t",
            "/var/run/docker.sock",
            id="colima-macos",
        ),
    ],
)
def test_reaper_mounts_the_socket_of_the_selected_docker_endpoint(
    docker: EgressDocker, endpoint: str, version: str, source: str
) -> None:
    docker.endpoint = endpoint
    docker.version = version

    result = sidecars.reconcile_sidecars(_POLICY, Intent.ENSURE)

    assert result.ready
    reaper = next(call for call in docker.calls if _step(call) == f"run {_REAPER}")
    assert ["-v", f"{source}:/var/run/docker.sock"] == reaper[
        reaper.index("-v") : reaper.index("-v") + 2
    ]


@pytest.mark.parametrize(
    ("endpoint", "version"),
    [
        pytest.param("tcp://192.0.2.7:2375", "linux\tDocker Engine - Community", id="tcp-linux"),
        pytest.param("ssh://dev@build-host", "linux\tDocker Engine - Community", id="ssh-linux"),
        pytest.param("ssh://dev@build-host", "darwin\tDocker Engine - Community", id="ssh-macos"),
    ],
)
def test_remote_docker_endpoint_refuses_the_reaper(
    docker: EgressDocker, endpoint: str, version: str
) -> None:
    docker.endpoint = endpoint
    docker.version = version

    result = sidecars.reconcile_sidecars(_POLICY, Intent.ENSURE)

    assert not result.ready
    row = _row(result, "reaper")
    assert row.state is _ERROR
    assert endpoint in row.detail
    assert f"run {_REAPER}" not in [_step(call) for call in docker.mutations()]


@dataclass(frozen=True)
class _Failure:
    """One Docker failure: the fake's starting state, its canned answers and the expected row."""

    resource: str
    fragment: str
    damage: str | None = None
    answers: tuple[tuple[tuple[str, ...], dict[str, object]], ...] = ()
    installed: bool = True
    intent: Intent = Intent.ENSURE
    state: sidecars.SidecarState = _ERROR
    unlabelled_builds: bool = False
    unowned_session: bool = False
    stopped_session: bool = False


def _answer(*prefix: str, **result: object) -> tuple[tuple[str, ...], dict[str, object]]:
    return prefix, result


_PROXY_IMAGE_INSPECT = ("image", "inspect", sidecars.legacy.PROXY_IMAGE)
_DOCUMENT = '{"Id":"sha","Config":{"Labels":%s}}'
_BAD_MOUNTS = (
    '{"Image":"sha","Config":{"Labels":{"booley.role":"x"}},"State":{"Running":true},'
    '"NetworkSettings":{"Networks":{}},"Mounts":{}}'
)
_FAILURES = {
    "build-fails": _Failure(
        "proxy-image",
        "failed to build",
        answers=(_answer("build", stderr="bad"),),
        installed=False,
    ),
    "unlabelled-build": _Failure(
        "proxy-image", "expected provenance", installed=False, unlabelled_builds=True
    ),
    "image-inspect-fails": _Failure(
        "proxy-image",
        "cannot inspect image",
        answers=(_answer(*_PROXY_IMAGE_INSPECT, stderr="daemon down"),),
        installed=False,
    ),
    "image-list-json": _Failure(
        "proxy-image",
        "invalid image",
        answers=(_answer(*_PROXY_IMAGE_INSPECT, returncode=0, stdout="[]"),),
        installed=False,
    ),
    "image-incomplete": _Failure(
        "proxy-image",
        "incomplete inspection for image",
        answers=(_answer(*_PROXY_IMAGE_INSPECT, returncode=0, stdout='{"Config":{}}'),),
        installed=False,
    ),
    "image-bad-labels": _Failure(
        "proxy-image",
        "invalid labels",
        answers=(_answer(*_PROXY_IMAGE_INSPECT, returncode=0, stdout=_DOCUMENT % '{"name":1}'),),
        installed=False,
    ),
    "image-silent-failure": _Failure(
        "proxy-image",
        "Docker exited 1 without diagnostic output",
        answers=(_answer(*_PROXY_IMAGE_INSPECT),),
        installed=False,
    ),
    "network-inspect-fails": _Failure(
        "network",
        "cannot inspect network",
        answers=(_answer("network", "inspect", stderr="daemon down"),),
    ),
    "network-no-internal": _Failure(
        "network",
        "incomplete inspection for network",
        answers=(
            _answer("network", "inspect", returncode=0, stdout='{"Options":{},"Containers":{}}'),
        ),
    ),
    "network-nameless-container": _Failure(
        "network",
        "incomplete inspection for network",
        answers=(
            _answer(
                "network",
                "inspect",
                returncode=0,
                stdout='{"Internal":true,"Options":{},"Containers":{"id":{}}}',
            ),
        ),
    ),
    "network-create-denied": _Failure(
        "network",
        "failed to create",
        damage="missing-network",
        answers=(_answer("network", "create", stderr="denied"),),
    ),
    "container-inspect-fails": _Failure(
        "proxy",
        "cannot inspect container",
        answers=(_answer("container", "inspect", stderr="daemon down"),),
    ),
    "container-empty": _Failure(
        "proxy",
        "incomplete inspection for container",
        answers=(_answer("container", "inspect", returncode=0, stdout="{}"),),
    ),
    "container-bad-mounts": _Failure(
        "proxy",
        "incomplete inspection for container",
        answers=(_answer("container", "inspect", returncode=0, stdout=_BAD_MOUNTS),),
    ),
    "run-fails": _Failure(
        "proxy", "failed to start", damage="missing-proxy", answers=(_answer("run", stderr="no"),)
    ),
    "connect-denied": _Failure(
        "proxy",
        "failed to connect",
        damage="disconnected-proxy",
        answers=(_answer("network", "connect", stderr="denied"),),
    ),
    "connect-already-exists": _Failure(
        "proxy",
        "reconciled",
        damage="disconnected-proxy",
        answers=(_answer("network", "connect", stderr="endpoint already exists"),),
        state=_CHANGED,
    ),
    "check-empty": _Failure(
        "proxy",
        f"image {sidecars.legacy.PROXY_IMAGE} is missing",
        installed=False,
        intent=Intent.CHECK,
    ),
    "start-fails": _Failure(
        "proxy",
        "failed to start",
        damage="stopped-proxy",
        answers=(_answer("start", stderr="no"),),
    ),
    "replace-fails": _Failure(
        "reaper",
        "failed to replace",
        damage="stale-reaper",
        answers=(_answer("rm", "-f", _REAPER),),
    ),
    "ps-fails": _Failure(
        "reaper",
        "cannot enumerate active Booley Sessions safely",
        damage="stale-reaper",
        answers=(_answer("ps", stderr="down"),),
    ),
    "ps-malformed": _Failure(
        "reaper",
        "incomplete active Session listing",
        damage="stale-reaper",
        answers=(_answer("ps", returncode=0, stdout="malformed"),),
    ),
    "session-unproven": _Failure(
        "reaper",
        "cannot prove active Session ownership",
        damage="stale-reaper",
        answers=(_answer("ps", returncode=0, stdout="ghost-id\tghost\n"),),
    ),
    "session-stopped": _Failure(
        "reaper",
        "cannot prove active Session ownership",
        damage="stale-reaper",
        answers=(_answer("ps", returncode=0, stdout="booley-session-x\tbooley-session-x\n"),),
        stopped_session=True,
    ),
    "session-without-project": _Failure(
        "reaper",
        "cannot identify owning Project",
        damage="stale-reaper",
        unowned_session=True,
    ),
    "proxy-remove-fails": _Failure(
        "network",
        "failed to remove stale",
        damage="stale-network",
        answers=(_answer("rm", "-f", _PROXY, stderr="denied"),),
    ),
    "endpoint-unreadable": _Failure(
        "reaper",
        "cannot read the active Docker endpoint",
        answers=(_answer("context", "inspect", stderr="no context"),),
    ),
    "reaper-reaches-other-daemon": _Failure(
        "reaper",
        "daemon-b",
        answers=(_answer("exec", _REAPER, returncode=0, stdout="daemon-b\n"),),
        installed=False,
    ),
    "reaper-reaches-no-daemon": _Failure(
        "reaper",
        "Cannot connect",
        answers=(_answer("exec", _REAPER, stderr="Cannot connect to the Docker daemon"),),
    ),
    "network-remove-fails": _Failure(
        "network",
        f"failed to replace {_NETWORK}",
        damage="stale-network",
        answers=(_answer("network", "rm", stderr="busy"),),
    ),
}


@pytest.mark.parametrize("failure", [pytest.param(f, id=name) for name, f in _FAILURES.items()])
def test_docker_failures_are_reported_on_their_row(
    docker: EgressDocker, failure: _Failure
) -> None:
    if failure.installed:
        _install(docker)
    if failure.damage is not None:
        _damage(docker, failure.damage)
    docker.unlabelled_builds = failure.unlabelled_builds
    if failure.unowned_session:
        docker.add_container("booley-session-x", labels={sidecars.ROLE_LABEL: "interactive"})
    if failure.stopped_session:
        docker.add_session("booley-session-x", running=False)
    for prefix, result in failure.answers:
        docker.reply(*prefix, **result)  # type: ignore[arg-type]

    result = sidecars.reconcile_sidecars(_POLICY, failure.intent)

    row = _row(result, failure.resource)
    assert result.ready is (failure.state is not _ERROR)
    assert row.state is failure.state
    assert failure.fragment in row.detail
    if failure.state is _ERROR:
        assert result.findings[-1] is row, "no later resource is reconciled after the failure"


@pytest.mark.skipif(
    os.name == "nt", reason="the fake docker there is docker.cmd, which the probe cannot run"
)
def test_image_reconciliation_reports_capacity_refusal_without_building(
    docker: EgressDocker, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv(SKIP_PREFLIGHT_ENV)
    install_fake_docker(tmp_path, monkeypatch, {"info": (1, "Docker daemon unavailable")})

    result = sidecars.reconcile_sidecars(_POLICY, Intent.ENSURE)

    assert result.findings[-1].resource == "proxy-image"
    assert result.findings[-1].state is _ERROR
    assert "could not query Docker storage root" in result.findings[-1].detail
    assert not any(call[0] == "build" for call in docker.calls)


def test_docker_cli_failure_is_reported_on_every_row(monkeypatch: pytest.MonkeyPatch) -> None:
    serve_sandbox_docker(monkeypatch).missing = True

    result = sidecars.inspect_sidecars(SandboxHostPolicy())

    assert [(row.resource, row.state) for row in result.findings] == [
        (resource, _ERROR) for resource in _SIDECAR_RESOURCES
    ]
    assert all("Docker command failed" in row.detail for row in result.findings)


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
    serve_egress_docker(monkeypatch).add_container(_PROXY, labels={})
    status, output = _run_inspection(capsys)
    _assert_complete(output)
    assert status == 2
    assert "✗  proxy:" in output and "foreign name collision" in output
    assert "+  reaper:" in output


def test_complete_inspection_checks_network_and_containers_after_recipe_failure(
    monkeypatch: pytest.MonkeyPatch, docker: EgressDocker
) -> None:
    def unavailable(_root):
        raise PermissionError("packaged recipes unavailable")

    # private: on Python 3.11 `Path.is_dir` raises PermissionError for an unreadable layout;
    # 3.12+ swallows it, so no recipe tree reaches this from the public entry.
    monkeypatch.setattr(sidecars, "_image_specs", unavailable)
    result = sidecars.inspect_sidecars(SandboxHostPolicy())
    assert [finding.resource for finding in result.findings] == [
        "sidecar-images",
        "network",
        "proxy",
        "reaper",
    ]
    assert result.findings[0].state is _ERROR


@pytest.mark.parametrize("blocker", ["foreign", "active-session", "foreign-proxy"])
def test_check_only_stale_network_blockers_are_errors(monkeypatch, tmp_path, capsys, blocker):
    from tests.harness.test_bootstrap import (
        _assert_complete,
        _inspection_environment,
        _run_inspection,
        _tree_snapshot,
    )

    _inspection_environment(monkeypatch, tmp_path)
    docker = serve_egress_docker(monkeypatch)
    docker.network = docker.stale_network = True
    if blocker == "foreign":
        docker.add_container("foreign", networks=(_NETWORK,))
    elif blocker == "active-session":
        docker.add_session("session-a")
    else:
        docker.add_container(_PROXY, labels={})
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
    docker = serve_egress_docker(monkeypatch)
    docker.add_current_image(sidecars.legacy.PROXY_IMAGE)
    docker.add_current_image(sidecars.legacy.REAPER_IMAGE)
    docker.add_container(_PROXY, image="old-sha", labels={sidecars.ROLE_LABEL: "egress-proxy"})
    docker.add_session("session-a")
    status, output = _run_inspection(capsys)
    assert "✗  proxy:" in output, "blocked container must be ERROR"
    assert status == 2
    _assert_complete(output)
    assert "shut down" in output


def test_check_only_pending_container_keeps_differences(monkeypatch, tmp_path, capsys):
    from tests.harness.test_bootstrap import _inspection_environment, _run_inspection

    _inspection_environment(monkeypatch, tmp_path)
    docker = serve_egress_docker(monkeypatch)
    docker.add_current_image(sidecars.legacy.PROXY_IMAGE)
    docker.add_current_image(sidecars.legacy.REAPER_IMAGE)
    status, output = _run_inspection(capsys)
    assert "image_identity" in output, "pending containers must explain desired-state differences"
    assert status == 1


# --- Host Bootstrap under host ``[sandbox] egress = "unproxied"`` (issue #2215) ---


def _leftover_egress(docker: EgressDocker, *, reaper: bool = True) -> None:
    """Give the fake a proxy-mode installation: images, network, proxy, reaper."""
    docker.network = True
    docker.add_sidecar(_PROXY, "egress-proxy", sidecars.legacy.PROXY_IMAGE, _UNPROXIED)
    docker.add_current_image(sidecars.legacy.REAPER_IMAGE)
    if reaper:
        docker.add_sidecar(_REAPER, "reaper", sidecars.legacy.REAPER_IMAGE, _UNPROXIED)


@pytest.mark.parametrize("start", ["fresh", "leftover-egress", "mode-flip"])
def test_unproxied_bootstrap_converges_to_the_reaper(docker: EgressDocker, start: str) -> None:
    policy = _UNPROXIED
    if start == "leftover-egress":
        _leftover_egress(docker)
        states = [_CURRENT, _CURRENT, _CHANGED, _CHANGED, _CURRENT]
        steps = [f"rm -f {_PROXY}", "network rm"]
    elif start == "fresh":
        states = [_CURRENT, _CHANGED, _CURRENT, _CURRENT, _CHANGED]
        steps = [f"build {sidecars.legacy.REAPER_IMAGE}", f"run {_REAPER}"]
    else:
        docker.add_sidecar(_REAPER, "reaper", sidecars.legacy.REAPER_IMAGE, SandboxHostPolicy())
        policy = replace(SandboxHostPolicy(), egress="unproxied")
        states = [_CURRENT] * 5
        steps = []

    result = sidecars.reconcile_sidecars(policy, Intent.ENSURE)

    assert _states(result) == list(zip(_SIDECAR_RESOURCES, states, strict=True))
    assert result.ready
    assert [_step(call) for call in docker.mutations()] == steps
    if start == "fresh":
        assert not any(sidecars.legacy.PROXY_IMAGE in call for call in docker.calls)
    if start == "leftover-egress":
        assert _PROXY not in docker.containers
        assert not docker.network


@pytest.mark.parametrize("blocker", ["running", "parked-predecessor", "foreign-attachment"])
def test_unproxied_bootstrap_keeps_proxy_while_sandboxes_exist(
    docker: EgressDocker, blocker: str
) -> None:
    _leftover_egress(docker, reaper=False)
    if blocker == "running":
        docker.add_session("booley-session-a", networks=(_NETWORK,))
        # The fake Session carries the VS Code folder label: `session down` refuses it.
        expected = [
            "booley-session-a",
            "/projects/a",
            "close its VS Code window or stop it with Dev Containers",
        ]
    elif blocker == "parked-predecessor":
        name = "booley-session-a-pre-refresh"
        docker.add_session(name, running=False)
        expected = [name, "/projects/a", "docker rename", "booley bootstrap"]
    else:
        docker.add_container("someone-else", networks=(_NETWORK,))
        expected = ["someone-else", "detach foreign containers"]

    result = sidecars.reconcile_sidecars(_UNPROXIED, Intent.ENSURE)

    assert _states(result) == list(
        zip(_SIDECAR_RESOURCES, [_CURRENT, _CURRENT, _ERROR, _ERROR, _CHANGED], strict=True)
    ), "blockers must keep the leftover egress; the reaper is still reconciled"
    for finding in result.findings[2:4]:
        for fragment in expected:
            assert fragment in finding.detail
    assert not any(call[0] == "rm" or call[:2] == ["network", "rm"] for call in docker.calls)
    assert docker.network
    assert _PROXY in docker.containers


@pytest.mark.skipif(os.name == "nt", reason="the hint uses cmd quoting, which shlex cannot parse")
def test_bootstrap_replaces_proxy_after_deleted_project_session_down(
    monkeypatch: pytest.MonkeyPatch, docker: EgressDocker, tmp_path: Path
) -> None:
    _leftover_egress(docker, reaper=False)
    root = tmp_path / "gone"
    root.mkdir()
    name = session_runtime.session_container_name(root)
    docker.add_headless_session(name, root, networks=(_NETWORK,))
    shutil.rmtree(root)
    route_session_commands(
        monkeypatch, tmp_path, lambda argv, **_kw: docker.run(argv[1:]), docker.containers
    )

    blocked = sidecars.reconcile_sidecars(_UNPROXIED, Intent.ENSURE)

    hint = next(finding.detail for finding in blocked.findings if "session down" in finding.detail)
    command = re.search(r"`(booley session down [^`]+)`", hint)
    assert command, hint
    assert run_cli(monkeypatch, shlex.split(command.group(1))[1:]) == 0
    assert name not in docker.containers, "session down must remove the deleted Project's Session"

    result = sidecars.reconcile_sidecars(_UNPROXIED, Intent.ENSURE)

    assert _ERROR not in {state for _, state in _states(result)}
    assert _PROXY not in docker.containers
    assert ["rm", "-f", _PROXY] in docker.calls
    removed = [call[-1] for call in docker.calls if call[:2] == ["rm", "-f"]]
    assert removed.count(f"id-{name}") == 1, "only `session down` removes the Session"


def test_readiness_check_accepts_unproxied_leftover_proxy(docker: EgressDocker) -> None:
    _leftover_egress(docker)
    docker.add_session("booley-session-a", networks=(_NETWORK,))

    result = sidecars.reconcile_sidecars(_UNPROXIED, Intent.CHECK)

    assert _states(result) == [(resource, _CURRENT) for resource in _SIDECAR_RESOURCES]
    assert not any(call[0] == "ps" for call in docker.calls), "readiness never enumerates"
    assert docker.mutations() == []


def _unproxied_inspection(monkeypatch, tmp_path) -> EgressDocker:
    from tests.harness.test_bootstrap import _inspection_environment

    _inspection_environment(monkeypatch, tmp_path)
    # Patching the host policy loader: it reads the machine's policy file, and no public entry
    # point pins an unproxied policy without writing that file into the real Booley directory.
    monkeypatch.setattr(bootstrap, "load_host_policy", lambda **_kwargs: _UNPROXIED)
    docker = serve_egress_docker(monkeypatch)
    docker.add_sidecar(_REAPER, "reaper", sidecars.legacy.REAPER_IMAGE, _UNPROXIED)
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
        docker.add_session("booley-session-a", networks=(_NETWORK,))

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


def test_parked_predecessor_remedy_leads_with_rename(docker: EgressDocker) -> None:
    """Check-only and mutating Bootstrap give the same remedy; neither says Bootstrap recovers it."""
    _leftover_egress(docker)
    docker.add_session("booley-session-a-pre-refresh", running=False)

    for intent in (Intent.CHECK, Intent.ENSURE):
        if intent is Intent.CHECK:
            result = sidecars.inspect_sidecars(_UNPROXIED)
        else:
            result = sidecars.reconcile_sidecars(_UNPROXIED, intent)

        proxy = _row(result, "proxy")
        assert proxy.state is _ERROR
        assert "rename it back with `docker rename booley-session-a-pre-refresh" in proxy.detail
        assert "or remove it after inspection" in proxy.detail
        assert "booley bootstrap` to recover" not in proxy.detail
        assert "recovery left it in place" not in proxy.detail


def test_restarting_predecessor_is_listed_once(docker: EgressDocker) -> None:
    _leftover_egress(docker)
    name = "booley-session-a-pre-refresh"
    docker.add_session(name, state="restarting")

    result = sidecars.reconcile_sidecars(_UNPROXIED, Intent.ENSURE)

    proxy = _row(result, "proxy")
    assert proxy.state is _ERROR
    assert proxy.detail.count(name) == 1, "an active predecessor must not also be listed parked"


def test_unproxied_reaper_image_error_skips_retirement(docker: EgressDocker) -> None:
    _leftover_egress(docker, reaper=False)
    docker.images[sidecars.legacy.REAPER_IMAGE] = ("sha256:foreign", {})

    result = sidecars.reconcile_sidecars(_UNPROXIED, Intent.ENSURE)

    assert _states(result) == [("proxy-image", _CURRENT), ("reaper-image", _ERROR)]
    assert docker.mutations() == [], "an image error must stop before any retirement mutation"


def test_unproxied_network_removal_failure_reports_removed_proxy(docker: EgressDocker) -> None:
    _leftover_egress(docker)
    docker.reply("network", "rm", stderr="in use")

    result = sidecars.reconcile_sidecars(_UNPROXIED, Intent.ENSURE)

    assert _row(result, "network").state is _ERROR
    assert "in use" in _row(result, "network").detail
    assert _row(result, "proxy").state is _CHANGED
    assert _PROXY not in docker.containers
