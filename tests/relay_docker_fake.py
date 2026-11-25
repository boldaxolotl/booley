"""A fake of the Docker boundary the FlexNet relay code receives as its ``Runner``.

``RelayDocker`` answers the Docker CLI calls of ``booley.eda.provisioning.licensing
.flexnet_docker``: provisioning and rollback, health polling, project cleanup, and the full
resume validation of one correctly configured relay topology. A test changes only the
answer it needs and reads ``commands`` for what was asked.
"""

from __future__ import annotations

import json
import subprocess

from booley.eda.provisioning.licensing.flexnet_docker import RelayResources, resources_for_session

IMAGE_ID = "sha256:" + "a" * 64
#: Health entry that makes the relay's health ``container inspect`` itself fail.
INSPECT_FAILURE = "<inspect-failure>"
SESSION_CONTAINER = "session-container"


def result(code: int = 0, stdout: str = "", stderr: str = "") -> subprocess.CompletedProcess[str]:
    return subprocess.CompletedProcess([], code, stdout, stderr)


def relay_state(
    resources: RelayResources,
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
        resources.private_network: {"Aliases": ["booley-license-xilinx", "license-server-01"]},
        resources.outbound_network: {"Aliases": []},
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


class RelayDocker:
    """A callable ``Runner`` that records ``commands`` and answers like Docker would.

    ``health`` is the sequence of relay health answers (a bare value means a running
    container, ``"<state> <health>"`` a stopped one); ``fail_prefix`` makes every call that
    starts with it exit 1; ``cleanup_failure`` names objects whose removal fails; ``listed``
    maps ``"container"`` and ``"network"`` to the names the ``ls`` calls print. The rest
    describes the relay topology the validation calls inspect.
    """

    def __init__(
        self,
        *,
        resources: RelayResources | None = None,
        health: tuple[str, ...] = ("healthy",),
        relay_log: str = "",
        log_error: bool = False,
        fail_prefix: tuple[str, ...] = (),
        cleanup_failure: tuple[str, ...] = (),
        listed: dict[str, str] | None = None,
        image_id: str = IMAGE_ID,
        state: dict | None = None,
        labels: tuple[str, ...] = (),
        extra_labels: dict[str, str] | None = None,
        session_on_outbound: bool = False,
        private_containers: tuple[str, ...] = (SESSION_CONTAINER,),
    ) -> None:
        self.resources = resources or resources_for_session("session-a")
        self.commands: list[list[str]] = []
        self.health = list(health)
        self.relay_log = relay_log
        self.log_error = log_error
        self.fail_prefix = fail_prefix
        self.cleanup_failure = cleanup_failure
        self.listed = listed or {}
        self.image_id = image_id
        self.state = state or relay_state(self.resources)
        self.labels = labels
        self.extra_labels = extra_labels or {}
        self.session_on_outbound = session_on_outbound
        self.private_containers = private_containers

    def __call__(  # noqa: PLR0911 - compact Docker CLI protocol fixture
        self, args: list[str], timeout: int
    ) -> subprocess.CompletedProcess[str]:
        self.commands.append(args)
        if self.fail_prefix and tuple(args[: len(self.fail_prefix)]) == self.fail_prefix:
            return result(1, stderr="injected failure")
        if args[:2] == ["image", "inspect"]:
            return result(stdout=f"{self.image_id}\n")
        if args[:2] == ["container", "logs"]:
            if self.log_error:
                raise subprocess.TimeoutExpired(args, timeout)
            return result(stderr=self.relay_log)
        if args[1] == "ls":
            return result(stdout=self.listed.get(args[0], ""))
        if args[1] == "rm" and args[-1] in self.cleanup_failure:
            return result(1, stderr="still attached")
        if args[1] == "inspect":
            return self._inspect(args[0], args[2], args[-1])
        return result()

    def _inspect(  # noqa: PLR0911 - compact Docker inspect protocol fixture
        self, kind: str, name: str, template: str
    ) -> subprocess.CompletedProcess[str]:
        resources = self.resources
        if "State.Status" in template:
            return self._next_health()
        if name == resources.relay_container and template == name:
            return result(stdout=json.dumps([self.state]))
        if kind == "network" and template == name:
            members = {"relay": resources.relay_container}
            if name == resources.private_network:
                members = {"relay": resources.relay_container} | {
                    str(index): endpoint for index, endpoint in enumerate(self.private_containers)
                }
            return result(
                stdout=json.dumps(
                    [{"Containers": {key: {"Name": v} for key, v in members.items()}}]
                )
            )
        if "Labels" in template:
            return result(stdout=json.dumps(self._labels(name)))
        if "Health.Status" in template:
            return result(stdout="healthy")
        if ".NetworkSettings.Networks" in template:
            networks = {resources.private_network: {}}
            if name == resources.relay_container or self.session_on_outbound:
                networks[resources.outbound_network] = {}
            return result(stdout=json.dumps(networks))
        if ".Internal" in template:
            return result(stdout="true" if name == resources.private_network else "false")
        if ".Options" in template:
            isolated = {"com.docker.network.bridge.gateway_mode_ipv4": "isolated"}
            return result(stdout=json.dumps(isolated if name == resources.private_network else {}))
        return result(1, stderr="unexpected inspect")

    def _labels(self, name: str) -> dict[str, str]:
        resources = self.resources
        role = {
            resources.relay_container: "license-relay",
            resources.private_network: "license-private-network",
        }.get(name, "license-outbound-network")
        issuance = dict(item.split("=", 1) for item in self.labels)
        return {
            **issuance,
            "booley.role": role,
            "booley.session-id": resources.session_id,
            **self.extra_labels,
        }

    def _next_health(self) -> subprocess.CompletedProcess[str]:
        status = self.health.pop(0) if self.health else "starting"
        if status == INSPECT_FAILURE:
            return result(1, stderr="daemon busy")
        return result(stdout=f"{status if ' ' in status else f'running {status}'}\n")
