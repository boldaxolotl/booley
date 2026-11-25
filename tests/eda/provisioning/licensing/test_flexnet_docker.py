"""Command-contract and rollback tests for the production FlexNet topology."""

from __future__ import annotations

import hashlib
from pathlib import Path

import pytest
from tests.relay_docker_fake import (
    IMAGE_ID,
    INSPECT_FAILURE,
    SESSION_CONTAINER,
    RelayDocker,
    relay_state,
)
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


def _profile() -> RelayProfile:
    return RelayProfile("10.20.30.40", "license-server-01", 2100, 2101)


def _validate(docker: RelayDocker, labels: tuple[str, ...] = ()) -> None:
    validate_relay(
        docker.resources,
        SESSION_CONTAINER,
        _profile(),
        issuance_labels=labels,
        runner=docker,
    )


def _health_inspects(docker: RelayDocker) -> list[list[str]]:
    return [command for command in docker.commands if command[:2] == ["container", "inspect"]]


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


def test_image_build_failure_is_actionable() -> None:
    docker = RelayDocker(fail_prefix=("image", "build"))
    with pytest.raises(RelayDockerError, match=r"could not build.*injected failure"):
        build_relay_image(runner=docker)


def test_ensure_image_skips_present_or_builds_when_forced() -> None:
    present = RelayDocker()
    assert ensure_relay_image(runner=present) is False
    assert present.commands == [["image", "inspect", "booley-flexnet-relay:1"]]

    forced = RelayDocker()
    assert ensure_relay_image(runner=forced, force=True) is True
    assert forced.commands[0][:2] == ["image", "build"]


def test_relay_image_resolution_requires_immutable_docker_id() -> None:
    assert resolve_relay_image_id(runner=RelayDocker()) == IMAGE_ID
    with pytest.raises(RelayDockerError, match="invalid relay image ID"):
        resolve_relay_image_id(runner=RelayDocker(image_id="relay:latest"))


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


def test_provision_orders_topology_and_waits_for_health() -> None:
    docker = RelayDocker(health=("starting", "healthy"))
    resources = provision_relay(
        _profile(), "session-a", runner=docker, health_attempts=3, poll_interval=0
    )
    assert docker.commands[0] == [
        "image",
        "inspect",
        "booley-flexnet-relay:1",
        "--format",
        "{{.Id}}",
    ]
    assert docker.commands[1] == private_network_create_argv(resources)
    assert docker.commands[2] == outbound_network_create_argv(resources)
    assert docker.commands[3] == relay_run_argv(resources, _profile(), image=IMAGE_ID)
    assert docker.commands[4] == relay_private_connect_argv(resources, _profile())
    assert len(_health_inspects(docker)) == 2


def test_resume_validation_checks_labels_health_authority_and_network_separation() -> None:
    _validate(RelayDocker(labels=_LABELS), _LABELS)


def test_extra_relay_label_names_key_without_printing_value() -> None:
    docker = RelayDocker(labels=_LABELS, extra_labels={"booley.spec-digest-old": "stale"})

    with pytest.raises(RelayDockerError, match="labels differ from host issuance") as caught:
        _validate(docker, _LABELS)
    assert "unexpected_label_keys [] -> [booley.spec-digest-old]" in str(caught.value)
    assert "stale" not in str(caught.value)


def test_resume_validation_rejects_session_on_outbound_network() -> None:
    with pytest.raises(RelayDockerError, match="network membership") as caught:
        _validate(RelayDocker(session_on_outbound=True))
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
    state_kwargs: dict, message: str
) -> None:
    docker = RelayDocker(state=relay_state(_RESOURCES, **state_kwargs))

    with pytest.raises(RelayDockerError, match=message):
        _validate(docker)


def test_resume_validation_rejects_unauthorized_private_endpoint() -> None:
    docker = RelayDocker(private_containers=(SESSION_CONTAINER, "unrelated-container"))

    with pytest.raises(RelayDockerError, match="unauthorized endpoints"):
        _validate(docker)


def test_resume_validation_rejects_relay_alias_drift() -> None:
    state = relay_state(_RESOURCES)
    state["NetworkSettings"]["Networks"][_RESOURCES.private_network]["Aliases"] = [
        "booley-license-xilinx"
    ]

    with pytest.raises(RelayDockerError, match="aliases have drifted"):
        _validate(RelayDocker(state=state))


def test_partial_startup_rolls_back_container_then_networks() -> None:
    docker = RelayDocker(fail_prefix=("network", "connect"))
    with pytest.raises(RelayDockerError, match="startup failed"):
        provision_relay(_profile(), "session-a", runner=docker, poll_interval=0)
    assert docker.commands[-3:] == [
        ["container", "rm", "-f", _RESOURCES.relay_container],
        ["network", "rm", _RESOURCES.outbound_network],
        ["network", "rm", _RESOURCES.private_network],
    ]


@pytest.mark.parametrize(
    "options,present,absent,inspects,attempts",
    [
        pytest.param(
            {"health": ("unhealthy",), "cleanup_failure": (_RESOURCES.outbound_network,)},
            ["did not become healthy", "rollback left: network"],
            [],
            None,
            None,
            id="rollback-residual",
        ),
        pytest.param(
            {
                "health": ("exited starting",),
                "relay_log": "INFO starting\nERROR invalid FlexNet relay configuration: bad port\n",
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
            {"health": ("starting", "unhealthy")},
            ["container: running, health: unhealthy"],
            [],
            2,
            None,
            id="unhealthy-verdict",
        ),
        pytest.param({"health": ("unhealthy",)}, [], ["relay log"], None, None, id="no-log"),
        pytest.param(
            {"health": (INSPECT_FAILURE,) * 3},
            ["container: inspect-error, health: unknown"],
            [],
            3,
            3,
            id="persistent-inspect-failure",
        ),
        pytest.param(
            {"health": ("unhealthy",), "relay_log": "never read", "log_error": True},
            ["container: running, health: unhealthy"],
            ["relay log"],
            None,
            None,
            id="log-fetch-error",
        ),
    ],
)
def test_startup_failure_reports_state_and_log_tail(
    options: dict,
    present: list[str],
    absent: list[str],
    inspects: int | None,
    attempts: int | None,
) -> None:
    docker = RelayDocker(**options)
    limit = {} if attempts is None else {"health_attempts": attempts}

    with pytest.raises(RelayDockerError) as raised:
        provision_relay(_profile(), "session-a", runner=docker, poll_interval=0, **limit)

    message = str(raised.value)
    assert all(text in message for text in present)
    assert not any(text in message for text in absent)
    if inspects is not None:
        assert len(_health_inspects(docker)) == inspects


def test_startup_failure_log_excerpt_is_bounded() -> None:
    docker = RelayDocker(health=("unhealthy",), relay_log="x" * 5000)
    with pytest.raises(RelayDockerError) as raised:
        provision_relay(_profile(), "session-a", runner=docker, poll_interval=0)
    excerpt = str(raised.value).split("relay log: ", 1)[1]
    assert excerpt == "x" * flexnet_docker.HEALTH_LOG_MAX_CHARS


@pytest.mark.parametrize(
    "health",
    [
        pytest.param(("starting",) * 100 + ("healthy",), id="slow-start-within-health-window"),
        pytest.param((INSPECT_FAILURE, "starting", "healthy"), id="transient-inspect-failure"),
    ],
)
def test_relay_that_becomes_healthy_is_kept(health: tuple[str, ...]) -> None:
    docker = RelayDocker(health=health)
    provision_relay(_profile(), "session-a", runner=docker, poll_interval=0)
    assert docker.health == []


def test_remove_is_bounded_and_reports_residual_objects() -> None:
    docker = RelayDocker(cleanup_failure=(_RESOURCES.private_network,))
    with pytest.raises(RelayDockerError, match=_RESOURCES.private_network):
        remove_relay(_RESOURCES, runner=docker)
    assert len(docker.commands) == 3


def test_project_cleanup_removes_all_sessions_before_owned_networks(tmp_path: Path) -> None:
    docker = RelayDocker(
        listed={"container": "headless\nvscode\nrelay\n", "network": "private\noutbound\n"}
    )

    assert cleanup_project_resources(tmp_path, runner=docker) == ()
    assert [command[:3] for command in docker.commands] == [
        ["container", "ls", "-aq"],
        ["container", "rm", "-f"],
        ["container", "rm", "-f"],
        ["container", "rm", "-f"],
        ["network", "ls", "-q"],
        ["network", "rm", "private"],
        ["network", "rm", "outbound"],
    ]
    assert [command[3] for command in docker.commands[1:4]] == ["headless", "vscode", "relay"]


def test_project_cleanup_reports_live_vscode_and_network_residue(tmp_path: Path) -> None:
    docker = RelayDocker(
        listed={"container": "vscode\n", "network": "private\n"},
        cleanup_failure=("vscode", "private"),
    )

    assert cleanup_project_resources(tmp_path, runner=docker) == (
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


def test_cleanup_identity_uses_only_canonical_full_label() -> None:
    root = "/deleted/Project"
    docker = RelayDocker()

    assert flexnet_docker.cleanup_project_resources_for_identity(root, runner=docker) == ()
    expected = "label=booley.project-id=" + hashlib.sha256(root.encode()).hexdigest()
    assert docker.commands == [
        ["container", "ls", "-aq", "--filter", expected],
        ["network", "ls", "-q", "--filter", expected],
    ]
