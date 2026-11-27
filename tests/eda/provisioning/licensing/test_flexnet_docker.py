"""Command-contract and rollback tests for the production FlexNet topology."""

from __future__ import annotations

import hashlib
import json
import subprocess
from pathlib import Path

import pytest
from tests.fake_docker_executable import FakeDocker, install_fake_docker
from tests.sidecar_image_helpers import SIDECAR_ALPINE_BASE

from booley.eda.provisioning.licensing import flexnet_docker
from booley.eda.provisioning.licensing.flexnet_docker import (
    OUTBOUND_NETWORK_LABEL,
    PRIVATE_NETWORK_LABEL,
    RELAY_ALIAS,
    RelayDockerError,
    RelayProfile,
    build_relay_image,
    cleanup_project_resources,
    ensure_relay_image,
    outbound_network_create_argv,
    private_network_create_argv,
    provision_relay,
    relay_image_build_argv,
    relay_private_connect_argv,
    relay_run_argv,
    remove_relay,
    resolve_relay_image_id,
    resources_for_session,
    session_private_connect_argv,
    validate_relay,
)
from booley.eda.provisioning.licensing.flexnet_relay import RelayConfigError

_RESOURCES = resources_for_session("session-a")
_LABELS = ("booley.project-id=project", "booley.spec-digest=spec")
IMAGE_ID = "sha256:" + "a" * 64
SESSION_CONTAINER = "session-container"
_RELAY = _RESOURCES.relay_container
_PRIVATE = _RESOURCES.private_network
_OUTBOUND = _RESOURCES.outbound_network
#: Words of the relay's health poll, and its answers.
_POLL = ("inspect", _RELAY, "{{.State.Status}}")
_STARTING = (0, "running starting", "")
_HEALTHY = (0, "running healthy", "")
_UNHEALTHY = (0, "running unhealthy", "")
_INSPECT_FAILURE = (1, "", "daemon busy")


def _relay_state(
    *,
    env: list[str] | None = None,
    config_overrides: dict | None = None,
    state_overrides: dict | None = None,
    **host_overrides,
) -> dict:
    """The relay's ``container inspect`` document; arguments override one drifted field."""
    host = {
        "Binds": None,
        "PortBindings": {},
        "PublishAllPorts": False,
        "ReadonlyRootfs": True,
        "CapAdd": None,
        "CapDrop": ["ALL"],
        "Privileged": False,
        "PidMode": "",
        "IpcMode": "private",
        "UsernsMode": "",
        "Devices": [],
        "DeviceRequests": None,
        "SecurityOpt": ["no-new-privileges:true"],
        "Tmpfs": {"/tmp": "rw,noexec,nosuid,nodev,size=1m"},
        "PidsLimit": 64,
        "Memory": 64 * 1024 * 1024,
        "NanoCpus": 500_000_000,
        "Ulimits": [{"Name": "nofile", "Hard": 256, "Soft": 256}],
        "RestartPolicy": {"Name": "no", "MaximumRetryCount": 0},
        "LogConfig": {"Type": "json-file", "Config": {"max-file": "2", "max-size": "1m"}},
        **host_overrides,
    }
    config = {
        "Image": IMAGE_ID,
        "User": "65532:65532",
        "Env": env
        or [
            "BOOLEY_FLEXNET_UPSTREAM_IPV4=10.20.30.40",
            "BOOLEY_FLEXNET_SERVER_HOSTID=license-server-01",
            "BOOLEY_FLEXNET_LMGRD_PORT=2100",
            "BOOLEY_FLEXNET_VENDOR_PORT=2101",
            "BOOLEY_FLEXNET_CONNECT_TIMEOUT=10",
            "BOOLEY_FLEXNET_IDLE_TIMEOUT=300",
            "BOOLEY_FLEXNET_MAX_CONNECTIONS=32",
            "PATH=/usr/local/bin",
        ],
        "ExposedPorts": None,
        **(config_overrides or {}),
    }
    networks = {
        _PRIVATE: {"Aliases": ["booley-license-xilinx", "license-server-01"]},
        _OUTBOUND: {"Aliases": []},
    }
    return {
        "Image": IMAGE_ID,
        "State": {"Running": True},
        "Config": config,
        "HostConfig": host,
        "Mounts": [],
        "NetworkSettings": {"Networks": networks},
        **(state_overrides or {}),
    }


def _role_labels(role: str, labels: tuple[str, ...]) -> str:
    issuance = dict(item.split("=", 1) for item in labels)
    return json.dumps(
        {**issuance, "booley.role": role, "booley.session-id": _RESOURCES.session_id}
    )


def _topology(labels: tuple[str, ...] = ()) -> dict[str, tuple[int, str]]:
    """Reply rules of one correctly configured, healthy relay topology."""
    nets = "{{json .NetworkSettings.Networks}}"
    rules = {
        "image inspect": IMAGE_ID,
        f"inspect {_RELAY}": json.dumps([_relay_state()]),
        f"inspect {_RELAY} .Config.Labels}}}}": _role_labels("license-relay", labels),
        " ".join(_POLL): "running healthy",
        f"inspect {_RELAY} {{{{.State.Health.Status}}}}": "healthy",
        f"inspect {_RELAY} {nets}": json.dumps({_PRIVATE: {}, _OUTBOUND: {}}),
        f"inspect {SESSION_CONTAINER} {nets}": json.dumps({_PRIVATE: {}}),
    }
    isolated = {"com.docker.network.bridge.gateway_mode_ipv4": "isolated"}
    for network, role, internal, options, members in (
        (_PRIVATE, "license-private-network", "true", isolated, (_RELAY, SESSION_CONTAINER)),
        (_OUTBOUND, "license-outbound-network", "false", {}, (_RELAY,)),
    ):
        endpoints = {name: {"Name": name} for name in members}
        rules[f"inspect {network}"] = json.dumps([{"Containers": endpoints}])
        rules[f"inspect {network} .Labels}}}}"] = _role_labels(role, labels)
        rules[f"inspect {network} {{{{.Internal}}}}"] = internal
        rules[f"inspect {network} .Options}}}}"] = json.dumps(options)
    return {words: (0, text) for words, text in rules.items()}


@pytest.fixture
def docker(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> FakeDocker:
    return install_fake_docker(tmp_path / "docker-bin", monkeypatch, _topology())


def _profile() -> RelayProfile:
    return RelayProfile("10.20.30.40", "license-server-01", 2100, 2101)


def _validate(docker: FakeDocker, labels: tuple[str, ...] = ()) -> None:
    validate_relay(
        _RESOURCES,
        SESSION_CONTAINER,
        _profile(),
        issuance_labels=labels,
        runner=docker.runner,
    )


def _health_inspects(docker: FakeDocker) -> list[list[str]]:
    return [command for command in docker.calls if command[:2] == ["container", "inspect"]]


@pytest.mark.parametrize(
    "args",
    [
        ("licenses.example", "host", 2100, 2101),
        ("10.0.0.1", "192.0.2.4", 2100, 2101),
        ("10.0.0.1", "host", True, 2101),
        ("10.0.0.1", "host", 2100, 2100),
    ],
)
def test_profile_revalidates_every_authority_field(args: tuple[object, ...]) -> None:
    with pytest.raises(RelayConfigError):
        RelayProfile(*args)  # type: ignore[arg-type]


def test_names_are_deterministic_and_do_not_leak_identity() -> None:
    first = resources_for_session("/private/project/path:spec-digest")
    second = resources_for_session("/private/project/path:spec-digest")
    assert first == second
    assert "/private/project/path" not in repr(first)
    assert len(first.session_id) == 16
    assert first.private_network != first.outbound_network


def test_networks_are_separate_and_only_private_is_internal() -> None:
    resources = resources_for_session("session-a")
    private = private_network_create_argv(resources)
    outbound = outbound_network_create_argv(resources)
    assert "--internal" in private
    assert "--internal" not in outbound
    assert "com.docker.network.bridge.gateway_mode_ipv4=isolated" in private
    assert PRIVATE_NETWORK_LABEL in private
    assert OUTBOUND_NETWORK_LABEL in outbound
    assert resources.private_network in private
    assert resources.outbound_network in outbound


def test_exact_issuance_labels_are_applied_to_all_relay_objects() -> None:
    resources = resources_for_session("session-a")
    labels = ("booley.project-id=project", "booley.spec-digest=spec")
    commands = [
        private_network_create_argv(resources, issuance_labels=labels),
        outbound_network_create_argv(resources, issuance_labels=labels),
        relay_run_argv(resources, _profile(), issuance_labels=labels),
    ]
    for command in commands:
        values = [command[index + 1] for index, item in enumerate(command) if item == "--label"]
        assert set(labels).issubset(values)


def test_image_build_uses_packaged_dockerfile_and_relay_only_context() -> None:
    argv = relay_image_build_argv(image="relay:test")
    assert argv[:4] == ["image", "build", "--tag", "relay:test"]
    assert Path(argv[-2]).parts[-5:] == (
        "src",
        "booley",
        "data",
        "docker",
        "Dockerfile.flexnet-relay",
    )
    assert Path(argv[-1]).parts[-2:] == ("provisioning", "licensing")


def test_image_build_failure_is_actionable(docker: FakeDocker) -> None:
    docker.answer("image", "build", code=1, stderr="injected failure")
    with pytest.raises(RelayDockerError, match=r"could not build.*injected failure"):
        build_relay_image(runner=docker.runner)


def test_ensure_image_skips_present_or_builds_when_forced(docker: FakeDocker) -> None:
    assert ensure_relay_image(runner=docker.runner) is False
    assert docker.calls == [["image", "inspect", "booley-flexnet-relay:1"]]

    assert ensure_relay_image(runner=docker.runner, force=True) is True
    assert docker.calls[1][:2] == ["image", "build"]


def test_relay_image_resolution_requires_immutable_docker_id(docker: FakeDocker) -> None:
    assert resolve_relay_image_id(runner=docker.runner) == IMAGE_ID
    docker.answer("image", "inspect", stdout="relay:latest")
    with pytest.raises(RelayDockerError, match="invalid relay image ID"):
        resolve_relay_image_id(runner=docker.runner)


def test_relay_argv_is_hardened_and_has_no_mount_or_published_port() -> None:
    resources = resources_for_session("session-a")
    argv = relay_run_argv(resources, _profile(), image="relay@example")
    required_pairs = {
        ("--network", resources.outbound_network),
        ("--user", "65532:65532"),
        ("--cap-drop", "ALL"),
        ("--security-opt", "no-new-privileges"),
        ("--pids-limit", "64"),
        ("--memory", "64m"),
        ("--memory-swap", "64m"),
        ("--ulimit", "nofile=256:256"),
        ("--restart", "no"),
    }
    assert all(
        list(pair) == argv[index : index + 2]
        for pair in required_pairs
        for index in [argv.index(pair[0])]
    )
    assert "--read-only" in argv
    assert "--tmpfs" in argv
    assert "max-size=1m" in argv
    assert "max-file=2" in argv
    assert "json-file" in argv
    assert not {"--mount", "--volume", "-v", "--publish", "-p"}.intersection(argv)
    assert all("LICENSE_FILE" not in item for item in argv)
    assert argv[-1] == "relay@example"


def test_private_aliases_are_fixed_and_session_never_joins_outbound() -> None:
    resources = resources_for_session("session-a")
    relay = relay_private_connect_argv(resources, _profile())
    session = session_private_connect_argv(resources, "session-container")
    assert relay == [
        "network",
        "connect",
        "--alias",
        RELAY_ALIAS,
        "--alias",
        "license-server-01",
        resources.private_network,
        resources.relay_container,
    ]
    assert resources.private_network in session
    assert resources.outbound_network not in session


def test_provision_orders_topology_and_waits_for_health(docker: FakeDocker) -> None:
    docker.answer_in_turn(*_POLL, answers=[_STARTING, _HEALTHY])
    resources = provision_relay(
        _profile(), "session-a", runner=docker.runner, health_attempts=3, poll_interval=0
    )
    assert docker.calls[0] == [
        "image",
        "inspect",
        "booley-flexnet-relay:1",
        "--format",
        "{{.Id}}",
    ]
    assert docker.calls[1] == private_network_create_argv(resources)
    assert docker.calls[2] == outbound_network_create_argv(resources)
    assert docker.calls[3] == relay_run_argv(resources, _profile(), image=IMAGE_ID)
    assert docker.calls[4] == relay_private_connect_argv(resources, _profile())
    assert len(_health_inspects(docker)) == 2


def test_resume_validation_checks_labels_health_authority_and_network_separation(
    docker: FakeDocker,
) -> None:
    docker.replies.update(_topology(_LABELS))
    _validate(docker, _LABELS)


def test_extra_relay_label_names_key_without_printing_value(docker: FakeDocker) -> None:
    docker.replies.update(_topology(_LABELS))
    labels = json.loads(_role_labels("license-relay", _LABELS))
    extra = json.dumps(labels | {"booley.spec-digest-old": "stale"})
    docker.answer("inspect", _RELAY, ".Config.Labels}}", stdout=extra)

    with pytest.raises(RelayDockerError, match="labels differ from host issuance") as caught:
        _validate(docker, _LABELS)
    assert "unexpected_label_keys [] -> [booley.spec-digest-old]" in str(caught.value)
    assert "stale" not in str(caught.value)


def test_resume_validation_rejects_session_on_outbound_network(docker: FakeDocker) -> None:
    nets = "{{json .NetworkSettings.Networks}}"
    both = json.dumps({_PRIVATE: {}, _OUTBOUND: {}})
    docker.answer("inspect", SESSION_CONTAINER, nets, stdout=both)
    with pytest.raises(RelayDockerError, match="network membership") as caught:
        _validate(docker)
    assert "outbound_network_present false -> true" in str(caught.value)
    assert "private_network_present" not in str(caught.value)


@pytest.mark.parametrize(
    "state_kwargs,message",
    [
        (
            {
                "state_overrides": {"Image": "sha256:" + "b" * 64},
                "config_overrides": {"Image": "sha256:" + "c" * 64},
            },
            "(?s)image identity.*state_image sha256:a+ -> sha256:b+.*"
            "config_image sha256:a+ -> sha256:c+",
        ),
        ({"state_overrides": {"Image": "sha256:" + "b" * 64}}, "image identity"),
        ({"config_overrides": {"Image": "sha256:" + "c" * 64}}, "image identity"),
        ({"config_overrides": {"User": "0:0"}}, "user"),
        ({"state_overrides": {"Mounts": [{"Destination": "/host"}]}}, "mounts"),
        ({"PortBindings": {"2100/tcp": [{"HostPort": "2100"}]}}, "port publication"),
        ({"ReadonlyRootfs": False}, "root filesystem"),
        ({"CapDrop": []}, "capability"),
        ({"Privileged": True}, "host authority"),
        ({"PidMode": "host"}, "host authority"),
        ({"IpcMode": "host"}, "host authority"),
        ({"UsernsMode": "host"}, "host authority"),
        ({"Devices": [{"PathOnHost": "/dev/kvm"}]}, "host authority"),
        ({"DeviceRequests": [{"Driver": "nvidia"}]}, "host authority"),
        ({"state_overrides": {"State": {"Running": False}}}, "not running"),
        ({"SecurityOpt": []}, "security options"),
        ({"Tmpfs": {}}, "tmpfs"),
        ({"PidsLimit": 65}, "resource limits"),
        ({"RestartPolicy": {"Name": "always", "MaximumRetryCount": 0}}, "restart"),
        ({"LogConfig": {"Type": "json-file", "Config": {}}}, "logging"),
        (
            {"env": ["BOOLEY_FLEXNET_UPSTREAM_IPV4=192.0.2.99"]},
            "environment differs",
        ),
    ],
)
def test_resume_validation_rejects_each_container_security_drift(
    docker: FakeDocker, state_kwargs: dict, message: str
) -> None:
    docker.answer("inspect", _RELAY, stdout=json.dumps([_relay_state(**state_kwargs)]))

    with pytest.raises(RelayDockerError, match=message):
        _validate(docker)


def test_resume_validation_rejects_unauthorized_private_endpoint(docker: FakeDocker) -> None:
    members = (_RELAY, SESSION_CONTAINER, "unrelated-container")
    endpoints = {name: {"Name": name} for name in members}
    docker.answer("inspect", _PRIVATE, stdout=json.dumps([{"Containers": endpoints}]))

    with pytest.raises(RelayDockerError, match="unauthorized endpoints"):
        _validate(docker)


def test_resume_validation_rejects_relay_alias_drift(docker: FakeDocker) -> None:
    state = _relay_state()
    state["NetworkSettings"]["Networks"][_PRIVATE]["Aliases"] = ["booley-license-xilinx"]
    docker.answer("inspect", _RELAY, stdout=json.dumps([state]))

    with pytest.raises(RelayDockerError, match="aliases have drifted"):
        _validate(docker)


def test_partial_startup_rolls_back_container_then_networks(docker: FakeDocker) -> None:
    docker.answer("network", "connect", code=1, stderr="injected failure")
    with pytest.raises(RelayDockerError, match="startup failed"):
        provision_relay(_profile(), "session-a", runner=docker.runner, poll_interval=0)
    assert docker.calls[-3:] == [
        ["container", "rm", "-f", _RESOURCES.relay_container],
        ["network", "rm", _RESOURCES.outbound_network],
        ["network", "rm", _RESOURCES.private_network],
    ]


@pytest.mark.parametrize(
    "health,replies,present,absent,inspects,attempts",
    [
        pytest.param(
            [_UNHEALTHY],
            {f"rm {_OUTBOUND}": (1, "still attached")},
            ["did not become healthy", "rollback left: network"],
            [],
            None,
            None,
            id="rollback-residual",
        ),
        pytest.param(
            [(0, "exited starting", "")],
            {
                "container logs": (
                    0,
                    "INFO starting\nERROR invalid FlexNet relay configuration: bad port",
                )
            },
            [
                "container: exited, health: starting",
                "relay log: INFO starting | ERROR invalid FlexNet relay configuration",
            ],
            [],
            1,
            None,
            id="exited-with-log-tail",
        ),
        pytest.param(
            [_STARTING, _UNHEALTHY],
            {},
            ["container: running, health: unhealthy"],
            [],
            2,
            None,
            id="unhealthy-verdict",
        ),
        pytest.param([_UNHEALTHY], {}, [], ["relay log"], None, None, id="no-log"),
        pytest.param(
            [_INSPECT_FAILURE] * 3,
            {},
            ["container: inspect-error, health: unknown"],
            [],
            3,
            3,
            id="persistent-inspect-failure",
        ),
    ],
)
def test_startup_failure_reports_state_and_log_tail(
    docker: FakeDocker,
    health: list[tuple[int, str, str]],
    replies: dict[str, tuple[int, str]],
    present: list[str],
    absent: list[str],
    inspects: int | None,
    attempts: int | None,
) -> None:
    docker.answer_in_turn(*_POLL, answers=health)
    docker.replies.update(replies)
    limit = {} if attempts is None else {"health_attempts": attempts}

    with pytest.raises(RelayDockerError) as raised:
        provision_relay(_profile(), "session-a", runner=docker.runner, poll_interval=0, **limit)

    message = str(raised.value)
    assert all(text in message for text in present)
    assert not any(text in message for text in absent)
    if inspects is not None:
        assert len(_health_inspects(docker)) == inspects


def test_startup_failure_when_the_log_fetch_times_out_omits_it(docker: FakeDocker) -> None:
    docker.answer_in_turn(*_POLL, answers=[_UNHEALTHY])
    docker.answer("container", "logs", stderr="never read")
    docker.raise_on("container", "logs", error=subprocess.TimeoutExpired(["docker"], 30))

    with pytest.raises(RelayDockerError) as raised:
        provision_relay(_profile(), "session-a", runner=docker.runner, poll_interval=0)

    assert "container: running, health: unhealthy" in str(raised.value)
    assert "relay log" not in str(raised.value)


def test_startup_failure_log_excerpt_is_bounded(docker: FakeDocker) -> None:
    docker.answer_in_turn(*_POLL, answers=[_UNHEALTHY])
    docker.answer("container", "logs", stderr="x" * 5000)
    with pytest.raises(RelayDockerError) as raised:
        provision_relay(_profile(), "session-a", runner=docker.runner, poll_interval=0)
    excerpt = str(raised.value).split("relay log: ", 1)[1]
    assert excerpt == "x" * flexnet_docker.HEALTH_LOG_MAX_CHARS


@pytest.mark.parametrize(
    "health",
    [
        pytest.param([_STARTING] * 100 + [_HEALTHY], id="slow-start-within-health-window"),
        pytest.param([_INSPECT_FAILURE, _STARTING, _HEALTHY], id="transient-inspect-failure"),
    ],
)
def test_relay_that_becomes_healthy_is_kept(
    docker: FakeDocker, health: list[tuple[int, str, str]]
) -> None:
    docker.answer_in_turn(*_POLL, answers=health)
    provision_relay(_profile(), "session-a", runner=docker.runner, poll_interval=0)
    assert len(_health_inspects(docker)) == len(health)


def test_remove_is_bounded_and_reports_residual_objects(docker: FakeDocker) -> None:
    docker.answer("rm", _PRIVATE, code=1, stderr="still attached")
    with pytest.raises(RelayDockerError, match=_PRIVATE):
        remove_relay(_RESOURCES, runner=docker.runner)
    assert len(docker.calls) == 3


def test_project_cleanup_removes_all_sessions_before_owned_networks(
    docker: FakeDocker, tmp_path: Path
) -> None:
    docker.answer("container", "ls", stdout="headless\nvscode\nrelay\n")
    docker.answer("network", "ls", stdout="private\noutbound\n")

    assert cleanup_project_resources(tmp_path, runner=docker.runner) == ()
    assert [command[:3] for command in docker.calls] == [
        ["container", "ls", "-aq"],
        ["container", "rm", "-f"],
        ["container", "rm", "-f"],
        ["container", "rm", "-f"],
        ["network", "ls", "-q"],
        ["network", "rm", "private"],
        ["network", "rm", "outbound"],
    ]
    assert [command[3] for command in docker.calls[1:4]] == ["headless", "vscode", "relay"]


def test_project_cleanup_reports_live_vscode_and_network_residue(
    docker: FakeDocker, tmp_path: Path
) -> None:
    docker.answer("container", "ls", stdout="vscode\n")
    docker.answer("network", "ls", stdout="private\n")
    for name in ("vscode", "private"):
        docker.answer("rm", name, code=1, stderr="still attached")

    assert cleanup_project_resources(tmp_path, runner=docker.runner) == (
        "container:vscode",
        "network:private",
    )


def test_dockerfile_pins_minimal_base_and_runs_as_numeric_user() -> None:
    path = Path("src/booley/data/docker/Dockerfile.flexnet-relay")
    dockerfile = path.read_text(encoding="utf-8")
    assert f"FROM {SIDECAR_ALPINE_BASE}" in dockerfile
    assert "USER 65532:65532" in dockerfile
    assert "HEALTHCHECK" in dockerfile
    assert "--healthcheck" in dockerfile
    assert 'ENTRYPOINT ["python3", "/app/flexnet_relay.py"]' in dockerfile


def test_cleanup_identity_uses_only_canonical_full_label(docker: FakeDocker) -> None:
    root = "/deleted/Project"

    cleanup = flexnet_docker.cleanup_project_resources_for_identity
    assert cleanup(root, runner=docker.runner) == ()
    expected = "label=booley.project-id=" + hashlib.sha256(root.encode()).hexdigest()
    assert docker.calls == [
        ["container", "ls", "-aq", "--filter", expected],
        ["network", "ls", "-q", "--filter", expected],
    ]
