"""Command-contract and rollback tests for the production FlexNet topology."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

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

IMAGE_ID = "sha256:" + "a" * 64


def _profile() -> RelayProfile:
    return RelayProfile("10.20.30.40", "license-server-01", 2100, 2101)


def _result(code: int = 0, stdout: str = "", stderr: str = "") -> subprocess.CompletedProcess[str]:
    return subprocess.CompletedProcess([], code, stdout, stderr)


class FakeDocker:
    def __init__(
        self,
        *,
        fail_prefix: tuple[str, ...] | None = None,
        health: tuple[str, ...] = ("healthy",),
        cleanup_failure: str | None = None,
    ) -> None:
        self.commands: list[list[str]] = []
        self.fail_prefix = fail_prefix
        self.health = list(health)
        self.cleanup_failure = cleanup_failure

    def __call__(self, args: list[str], _timeout: int) -> subprocess.CompletedProcess[str]:
        self.commands.append(args)
        if args[:2] == ["image", "inspect"]:
            return _result(stdout=f"{IMAGE_ID}\n")
        if args[:3] == ["container", "inspect", args[2]]:
            status = self.health.pop(0) if self.health else "starting"
            return _result(stdout=f"{status}\n")
        if self.fail_prefix and tuple(args[: len(self.fail_prefix)]) == self.fail_prefix:
            return _result(1, stderr="injected failure")
        if self.cleanup_failure and args[-1] == self.cleanup_failure and "rm" in args:
            return _result(1, stderr="still attached")
        return _result()


def _relay_state(
    resources,
    *,
    env: list[str] | None = None,
    config_overrides: dict | None = None,
    state_overrides: dict | None = None,
    **host_overrides,
) -> str:
    expected_env = env or [
        "BOOLEY_FLEXNET_UPSTREAM_IPV4=10.20.30.40",
        "BOOLEY_FLEXNET_SERVER_HOSTID=license-server-01",
        "BOOLEY_FLEXNET_LMGRD_PORT=2100",
        "BOOLEY_FLEXNET_VENDOR_PORT=2101",
        "BOOLEY_FLEXNET_CONNECT_TIMEOUT=10",
        "BOOLEY_FLEXNET_IDLE_TIMEOUT=300",
        "BOOLEY_FLEXNET_MAX_CONNECTIONS=32",
        "PATH=/usr/local/bin",
    ]
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
        "LogConfig": {
            "Type": "json-file",
            "Config": {"max-file": "2", "max-size": "1m"},
        },
    }
    host.update(host_overrides)
    config = {
        "Image": IMAGE_ID,
        "User": "65532:65532",
        "Env": expected_env,
        "ExposedPorts": None,
    }
    config.update(config_overrides or {})
    state = {
        "Image": IMAGE_ID,
        "State": {"Running": True},
        "Config": config,
        "HostConfig": host,
        "Mounts": [],
        "NetworkSettings": {
            "Networks": {
                resources.private_network: {
                    "Aliases": ["booley-license-xilinx", "license-server-01"]
                },
                resources.outbound_network: {"Aliases": []},
            }
        },
    }
    state.update(state_overrides or {})
    return json.dumps([state])


def _validation_runner(resources, *, relay_state: str | None = None):
    def inspect(  # noqa: PLR0911 - compact Docker inspect protocol fixture
        args: list[str], _timeout: int
    ) -> subprocess.CompletedProcess[str]:
        if args[:2] == ["image", "inspect"]:
            return _result(stdout=IMAGE_ID)
        name, template = args[2], args[-1]
        if args == ["container", "inspect", resources.relay_container]:
            return _result(stdout=relay_state or _relay_state(resources))
        if args == ["network", "inspect", resources.private_network]:
            return _result(
                stdout=json.dumps(
                    [
                        {
                            "Containers": {
                                "relay": {"Name": resources.relay_container},
                                "session": {"Name": "session-container"},
                            }
                        }
                    ]
                )
            )
        if args == ["network", "inspect", resources.outbound_network]:
            return _result(
                stdout=json.dumps([{"Containers": {"relay": {"Name": resources.relay_container}}}])
            )
        if "Labels" in template:
            role = (
                "booley.role=license-relay"
                if name == resources.relay_container
                else "booley.role=license-private-network"
                if name == resources.private_network
                else "booley.role=license-outbound-network"
            )
            key, value = role.split("=", 1)
            return _result(
                stdout=json.dumps({key: value, "booley.session-id": resources.session_id})
            )
        if "Health.Status" in template:
            return _result(stdout="healthy")
        if ".NetworkSettings.Networks" in template:
            networks = (
                {resources.private_network: {}, resources.outbound_network: {}}
                if name == resources.relay_container
                else {resources.private_network: {}}
            )
            return _result(stdout=json.dumps(networks))
        if ".Internal" in template:
            return _result(stdout="true" if name == resources.private_network else "false")
        if ".Options" in template:
            options = (
                {"com.docker.network.bridge.gateway_mode_ipv4": "isolated"}
                if name == resources.private_network
                else {}
            )
            return _result(stdout=json.dumps(options))
        return _result(stderr="unexpected inspect", code=1)

    return inspect


class TestProfileAndNames:
    @pytest.mark.parametrize(
        "args",
        [
            ("licenses.example", "host", 2100, 2101),
            ("127.0.0.1", "host", 2100, 2101),
            ("10.0.0.1", "192.0.2.4", 2100, 2101),
            ("10.0.0.1", "host", True, 2101),
            ("10.0.0.1", "host", 2100, 2100),
        ],
    )
    def test_profile_revalidates_every_authority_field(self, args: tuple[object, ...]) -> None:
        with pytest.raises(RelayConfigError):
            RelayProfile(*args)  # type: ignore[arg-type]

    def test_names_are_deterministic_and_do_not_leak_identity(self) -> None:
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
    docker = FakeDocker(fail_prefix=("image", "build"))
    with pytest.raises(RelayDockerError, match=r"could not build.*injected failure"):
        build_relay_image(runner=docker)


def test_ensure_image_skips_present_or_builds_when_forced() -> None:
    present = FakeDocker()
    assert ensure_relay_image(runner=present) is False
    assert present.commands == [["image", "inspect", "booley-flexnet-relay:1"]]

    forced = FakeDocker()
    assert ensure_relay_image(runner=forced, force=True) is True
    assert forced.commands[0][:2] == ["image", "build"]


def test_relay_image_resolution_requires_immutable_docker_id() -> None:
    assert resolve_relay_image_id(runner=FakeDocker()) == IMAGE_ID

    def invalid(_args: list[str], _timeout: int) -> subprocess.CompletedProcess[str]:
        return _result(stdout="relay:latest")

    with pytest.raises(RelayDockerError, match="invalid relay image ID"):
        resolve_relay_image_id(runner=invalid)


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
    docker = FakeDocker(health=("starting", "healthy"))
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
    assert [command[0:2] for command in docker.commands[5:]] == [
        ["container", "inspect"],
        ["container", "inspect"],
    ]


def test_resume_validation_checks_labels_health_authority_and_network_separation() -> None:
    resources = resources_for_session("session-a")
    labels = ("booley.project-id=project", "booley.spec-digest=spec")
    relay_labels = "\n".join(
        ["booley.role=license-relay", f"booley.session-id={resources.session_id}", *labels]
    )
    private_labels = "\n".join(
        [
            "booley.role=license-private-network",
            f"booley.session-id={resources.session_id}",
            *labels,
        ]
    )
    outbound_labels = "\n".join(
        [
            "booley.role=license-outbound-network",
            f"booley.session-id={resources.session_id}",
            *labels,
        ]
    )

    def inspect(  # noqa: PLR0911 - compact Docker inspect protocol fixture
        args: list[str], _timeout: int
    ) -> subprocess.CompletedProcess[str]:
        if args[:2] == ["image", "inspect"]:
            return _result(stdout=IMAGE_ID)
        name, template = args[2], args[-1]
        if args == ["container", "inspect", resources.relay_container]:
            return _result(stdout=_relay_state(resources))
        if args == ["network", "inspect", resources.private_network]:
            return _result(
                stdout=json.dumps(
                    [
                        {
                            "Containers": {
                                "relay": {"Name": resources.relay_container},
                                "session": {"Name": "session-container"},
                            }
                        }
                    ]
                )
            )
        if args == ["network", "inspect", resources.outbound_network]:
            return _result(
                stdout=json.dumps([{"Containers": {"relay": {"Name": resources.relay_container}}}])
            )
        if "Health.Status" in template:
            return _result(stdout="healthy\n")
        if ".Config.Env" in template:
            return _result(
                stdout=(
                    '["BOOLEY_FLEXNET_UPSTREAM_IPV4=10.20.30.40",'
                    '"BOOLEY_FLEXNET_SERVER_HOSTID=license-server-01",'
                    '"BOOLEY_FLEXNET_LMGRD_PORT=2100",'
                    '"BOOLEY_FLEXNET_VENDOR_PORT=2101"]\n'
                )
            )
        if ".NetworkSettings.Networks" in template:
            networks = (
                {resources.private_network: {}, resources.outbound_network: {}}
                if name == resources.relay_container
                else {"booley-egress-v2": {}, resources.private_network: {}}
            )
            return _result(stdout=json.dumps(networks))
        if ".Internal" in template:
            return _result(stdout="true" if name == resources.private_network else "false")
        if ".Options" in template:
            options = (
                {"com.docker.network.bridge.gateway_mode_ipv4": "isolated"}
                if name == resources.private_network
                else {}
            )
            return _result(stdout=json.dumps(options))
        if name == resources.relay_container:
            selected = relay_labels
        else:
            selected = private_labels if name == resources.private_network else outbound_labels
        return _result(
            stdout=json.dumps(dict(item.split("=", 1) for item in selected.splitlines()))
        )

    validate_relay(
        resources,
        "session-container",
        _profile(),
        issuance_labels=labels,
        runner=inspect,
    )

    def inspect_with_extra_label(
        args: list[str], timeout: int
    ) -> subprocess.CompletedProcess[str]:
        result = inspect(args, timeout)
        if "Labels" in args[-1]:
            decoded = json.loads(result.stdout)
            decoded["booley.spec-digest-old"] = "stale"
            return _result(stdout=json.dumps(decoded))
        return result

    with pytest.raises(RelayDockerError, match="labels differ from host issuance"):
        validate_relay(
            resources,
            "session-container",
            _profile(),
            issuance_labels=labels,
            runner=inspect_with_extra_label,
        )


def test_resume_validation_rejects_session_on_outbound_network() -> None:
    resources = resources_for_session("session-a")

    def inspect(  # noqa: PLR0911 - compact Docker inspect protocol fixture
        args: list[str], _timeout: int
    ) -> subprocess.CompletedProcess[str]:
        if args[:2] == ["image", "inspect"]:
            return _result(stdout=IMAGE_ID)
        name, template = args[2], args[-1]
        if args == ["container", "inspect", resources.relay_container]:
            return _result(stdout=_relay_state(resources))
        if "Labels" in template:
            role = (
                "booley.role=license-relay"
                if name == resources.relay_container
                else "booley.role=license-private-network"
                if name == resources.private_network
                else "booley.role=license-outbound-network"
            )
            key, value = role.split("=", 1)
            return _result(
                stdout=json.dumps({key: value, "booley.session-id": resources.session_id})
            )
        if "Health.Status" in template:
            return _result(stdout="healthy")
        if ".Config.Env" in template:
            return _result(
                stdout=(
                    '["BOOLEY_FLEXNET_UPSTREAM_IPV4=10.20.30.40",'
                    '"BOOLEY_FLEXNET_SERVER_HOSTID=license-server-01",'
                    '"BOOLEY_FLEXNET_LMGRD_PORT=2100",'
                    '"BOOLEY_FLEXNET_VENDOR_PORT=2101"]'
                )
            )
        if ".NetworkSettings.Networks" in template:
            return _result(
                stdout=(
                    f'{{"{resources.private_network}":{{}},"{resources.outbound_network}":{{}}}}'
                )
            )
        return _result(stdout="true" if name == resources.private_network else "false")

    with pytest.raises(RelayDockerError, match=r"Session Runtime is attached.*outbound"):
        validate_relay(
            resources,
            "session-container",
            _profile(),
            issuance_labels=(),
            runner=inspect,
        )


@pytest.mark.parametrize(
    "state_kwargs,message",
    [
        ({"state_overrides": {"Image": "sha256:" + "b" * 64}}, "image identity"),
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
    resources = resources_for_session("session-a")
    runner = _validation_runner(resources, relay_state=_relay_state(resources, **state_kwargs))

    with pytest.raises(RelayDockerError, match=message):
        validate_relay(
            resources,
            "session-container",
            _profile(),
            issuance_labels=(),
            runner=runner,
        )


def test_resume_validation_rejects_unauthorized_private_endpoint() -> None:
    resources = resources_for_session("session-a")
    original = _validation_runner(resources)

    def runner(args: list[str], timeout: int) -> subprocess.CompletedProcess[str]:
        result = original(args, timeout)
        if args == ["network", "inspect", resources.private_network]:
            raw = json.loads(result.stdout)
            raw[0]["Containers"]["attacker"] = {"Name": "unrelated-container"}
            return _result(stdout=json.dumps(raw))
        return result

    with pytest.raises(RelayDockerError, match="unauthorized endpoints"):
        validate_relay(
            resources,
            "session-container",
            _profile(),
            issuance_labels=(),
            runner=runner,
        )


def test_resume_validation_rejects_relay_alias_drift() -> None:
    resources = resources_for_session("session-a")
    raw = json.loads(_relay_state(resources))
    raw[0]["NetworkSettings"]["Networks"][resources.private_network]["Aliases"] = [
        "booley-license-xilinx"
    ]
    runner = _validation_runner(resources, relay_state=json.dumps(raw))
    with pytest.raises(RelayDockerError, match="aliases have drifted"):
        validate_relay(
            resources,
            "session-container",
            _profile(),
            issuance_labels=(),
            runner=runner,
        )


def test_partial_startup_rolls_back_container_then_networks() -> None:
    docker = FakeDocker(fail_prefix=("network", "connect"))
    resources = resources_for_session("session-a")
    with pytest.raises(RelayDockerError, match="startup failed"):
        provision_relay(_profile(), "session-a", runner=docker, poll_interval=0)
    assert docker.commands[-3:] == [
        ["container", "rm", "-f", resources.relay_container],
        ["network", "rm", resources.outbound_network],
        ["network", "rm", resources.private_network],
    ]


def test_unhealthy_startup_rolls_back_and_reports_cleanup_residual() -> None:
    resources = resources_for_session("session-a")
    docker = FakeDocker(health=("unhealthy",), cleanup_failure=resources.outbound_network)
    with pytest.raises(RelayDockerError, match=r"did not become healthy.*rollback left: network"):
        provision_relay(_profile(), "session-a", runner=docker, health_attempts=1, poll_interval=0)


def test_remove_is_bounded_and_reports_residual_objects() -> None:
    resources = resources_for_session("session-a")
    docker = FakeDocker(cleanup_failure=resources.private_network)
    with pytest.raises(RelayDockerError, match=resources.private_network):
        remove_relay(resources, runner=docker)
    assert len(docker.commands) == 3


def test_project_cleanup_removes_all_sessions_before_owned_networks(tmp_path: Path) -> None:
    commands: list[list[str]] = []

    def docker(args: list[str], _timeout: int) -> subprocess.CompletedProcess[str]:
        commands.append(args)
        if args[:3] == ["container", "ls", "-aq"]:
            return _result(stdout="headless\nvscode\nrelay\n")
        if args[:3] == ["network", "ls", "-q"]:
            return _result(stdout="private\noutbound\n")
        return _result()

    assert cleanup_project_resources(tmp_path, runner=docker) == ()
    assert commands[1:4] == [
        ["container", "rm", "-f", "headless"],
        ["container", "rm", "-f", "vscode"],
        ["container", "rm", "-f", "relay"],
    ]
    network_list = commands.index(
        next(command for command in commands if command[:3] == ["network", "ls", "-q"])
    )
    assert all(command[:2] != ["network", "rm"] for command in commands[:network_list])
    assert commands[network_list + 1 :] == [
        ["network", "rm", "private"],
        ["network", "rm", "outbound"],
    ]


def test_project_cleanup_reports_live_vscode_and_network_residue(tmp_path: Path) -> None:
    def docker(args: list[str], _timeout: int) -> subprocess.CompletedProcess[str]:
        if args[:3] == ["container", "ls", "-aq"]:
            return _result(stdout="vscode\n")
        if args[:3] == ["network", "ls", "-q"]:
            return _result(stdout="private\n")
        return _result(code=1, stderr="still attached")

    assert cleanup_project_resources(tmp_path, runner=docker) == (
        "container:vscode",
        "network:private",
    )


def test_dockerfile_pins_minimal_base_and_runs_as_numeric_user() -> None:
    path = Path("src/booley/data/docker/Dockerfile.flexnet-relay")
    dockerfile = path.read_text(encoding="utf-8")
    assert (
        "FROM python:3.14.7-alpine3.24@sha256:"
        "05b2b8b732ecd268fee8727a369f936f022d1321b59befd13c30ede22769dcdc" in dockerfile
    )
    assert "USER 65532:65532" in dockerfile
    assert "HEALTHCHECK" in dockerfile
    assert "--healthcheck" in dockerfile
    assert 'ENTRYPOINT ["python3", "/app/flexnet_relay.py"]' in dockerfile
