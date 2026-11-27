"""Shared fake of the Docker CLI boundary for the host sidecars (proxy, reaper, network, Sessions).

``EgressDocker`` answers the ``docker`` verbs that ``booley.harness.host_sidecars`` and
Session admission issue, from memory. Tests of Host Bootstrap, Doctor and init that need a
sidecar installation to exist, drift or break use ``serve_egress_docker`` instead of faking
the sidecar adapter again.
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from booley.config.host_config import SandboxHostPolicy
from booley.harness import host_sidecars as sidecars
from booley.harness.host_sidecars import plan_image_builds
from booley.runtime.docker_capacity import SKIP_PREFLIGHT_ENV
from booley.runtime.image_lifecycle import Intent
from tests.harness.session_docker_fake import completed, headless_session_entry

# The Dev Containers label that names a VS Code Sandbox's Project.
_FOLDER_LABEL = "devcontainer.local_folder"


class EgressDocker:
    """Stateful Docker fake covering the sidecar, network, and Session commands.

    ``stale_network`` makes the network report ``Internal: false`` until it is created
    again; ``unlabelled_builds`` makes ``build`` drop the ``--label`` stamps; ``reply``
    answers every later call that starts with the given arguments with a canned result.
    ``endpoint``, ``version`` (client OS, a tab, server platform) and ``daemon_id`` are what
    the Docker CLI reports about the selected daemon.
    """

    def __init__(self) -> None:
        self.calls: list[list[str]] = []
        self.images: dict[str, tuple[str, dict[str, str]]] = {}
        self.containers: dict[str, dict[str, object]] = {}
        self.network = False
        self.stale_network = False
        self.unlabelled_builds = False
        self.endpoint = "unix:///var/run/docker.sock"
        self.version = "linux\tDocker Engine - Community"
        self.daemon_id = "daemon-a"
        self._replies: list[tuple[tuple[str, ...], subprocess.CompletedProcess[str]]] = []

    def reply(self, *prefix: str, returncode: int = 1, stdout: str = "", stderr: str = "") -> None:
        """Answer later calls whose arguments start with ``prefix``; the call is still recorded."""
        self._replies.append((prefix, completed(returncode, stdout, stderr)))

    def add_current_image(self, reference: str) -> None:
        # The labels a build would stamp come from the public planner, on an empty fake. It is
        # imported by name: Bootstrap tests replace the module attribute with an empty plan.
        request = next(
            request
            for request in plan_image_builds(Intent.REFRESH, docker=EgressDocker())
            if request.output_tag == reference
        )
        assert request.cache_evidence is not None
        labels = dict(request.cache_evidence.expected_labels)
        self.images[reference] = (f"sha256:{reference}", labels)

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
        workspace: str | None = None,
    ) -> None:
        """Add a VS Code Sandbox (folder label ``/projects/a``).

        ``state`` overrides the ``ps -a`` State derived from ``running``; ``workspace`` is
        the source of a ``/work`` bind mount the inspection also reports.
        """
        self.add_container(
            name,
            labels={sidecars.ROLE_LABEL: sidecars.SESSION_ROLE, _FOLDER_LABEL: "/projects/a"},
            running=running,
            networks=networks,
        )
        if state is not None:
            self.containers[name]["state"] = state
        if workspace is not None:
            self.containers[name]["workspace"] = workspace

    def add_headless_session(
        self, name: str, root: Path, *, networks: tuple[str, ...] = ()
    ) -> None:
        """Add a headless Sandbox whose Project is identified by its `/work` bind."""
        self.add_session(name, networks=networks)
        entry = self.containers[name]
        description = headless_session_entry(name, root)
        entry["labels"] = description["Config"]["Labels"]
        entry["session"] = description

    def mutations(self) -> list[list[str]]:
        read_only = {"ps", "version", "info", "exec"}
        return [call for call in self.calls if "inspect" not in call and call[0] not in read_only]

    def run(self, args: list[str], *, timeout: int = 30) -> subprocess.CompletedProcess[str]:
        del timeout
        self.calls.append(list(args))
        for prefix, canned in reversed(self._replies):
            if tuple(args[: len(prefix)]) == prefix:
                return canned
        handler = {
            ("context", "inspect"): lambda _args: completed(stdout=f"{self.endpoint}\n"),
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
            "stop": lambda _args: completed(),
            "build": self._build,
            "run": self._run,
            "start": lambda _args: completed(),
            "version": lambda _args: completed(stdout=f"{self.version}\n"),
            "info": lambda _args: completed(stdout=f"{self.daemon_id}\n"),
            "exec": self._exec,
        }[args[0]](args)

    def _exec(self, args: list[str]) -> subprocess.CompletedProcess[str]:
        """Answer the daemon-ID probe a running container makes through its mounted socket."""
        if self._find(args[1]) is None:
            return completed(1, stderr="No such container")
        return completed(stdout=f"{self.daemon_id}\n")

    def _inspect_image(self, args: list[str]) -> subprocess.CompletedProcess[str]:
        if args[2] not in self.images:
            return completed(1, stderr="No such image")
        image_id, labels = self.images[args[2]]
        return completed(stdout=json.dumps({"Id": image_id, "Config": {"Labels": labels}}))

    def _find(self, key: str) -> str | None:
        """Resolve a container name or, for a headless Session, its Id."""
        for name, entry in self.containers.items():
            session = entry.get("session")
            if key == name or (session is not None and key == session["Id"]):  # type: ignore[index]
                return name
        return None

    def _inspect_container(self, args: list[str]) -> subprocess.CompletedProcess[str]:
        name = self._find(args[2])
        if name is None:
            return completed(1, stderr="No such container")
        entry = self.containers[name]
        mounts = (
            [{"Type": "bind", "Source": entry["workspace"], "Destination": "/work", "RW": True}]
            if "workspace" in entry
            else []
        )
        document = {
            "Image": entry["image"],
            "Config": {"Labels": entry["labels"]},
            "State": {"Running": entry["running"]},
            "NetworkSettings": {"Networks": {net: {} for net in entry["networks"]}},
            "Mounts": mounts,
        }
        if (session := entry.get("session")) is not None:
            # Full identity, so Session admission accepts the container as a Sandbox.
            document = {
                **session,  # type: ignore[dict-item]
                "State": {**session["State"], "Running": entry["running"]},  # type: ignore[index]
                "NetworkSettings": document["NetworkSettings"],
            }
        return completed(stdout=json.dumps(document))

    def _inspect_network(self, _args: list[str]) -> subprocess.CompletedProcess[str]:
        if not self.network:
            return completed(1, stderr="network booley-egress-v2 not found")
        attached = {
            f"id-{name}": {"Name": name}
            for name, entry in self.containers.items()
            if entry["running"] and sidecars.legacy.EGRESS_NETWORK in entry["networks"]
        }
        document = {
            "Internal": not self.stale_network,
            "Options": {
                sidecars.legacy.GATEWAY_MODE_OPTION: sidecars.legacy.GATEWAY_MODE_ISOLATED
            },
            "Containers": attached,
        }
        return completed(stdout=json.dumps(document))

    def _create_network(self, _args: list[str]) -> subprocess.CompletedProcess[str]:
        self.network = True
        self.stale_network = False
        return completed()

    def _remove_network(self, _args: list[str]) -> subprocess.CompletedProcess[str]:
        self.network = False
        return completed()

    def _connect(self, args: list[str]) -> subprocess.CompletedProcess[str]:
        self.containers[args[3]]["networks"].add(args[2])  # type: ignore[union-attr]
        return completed()

    def _ps(self, args: list[str]) -> subprocess.CompletedProcess[str]:
        rows = []
        for name, entry in sorted(self.containers.items()):
            role = entry["labels"].get(sidecars.ROLE_LABEL)  # type: ignore[union-attr]
            if role != sidecars.SESSION_ROLE:
                continue
            session = entry.get("session")
            if "--no-trunc" in args:
                # Session admission's Project listing: id, name, folder, project-id, role.
                if entry["running"] and session is not None:
                    labels = session["Config"]["Labels"]  # type: ignore[index]
                    rows.append(
                        f"{session['Id']}\t{name}\t\t{labels['booley.project-id']}\t{role}"  # type: ignore[index]
                    )
            elif "-a" in args:
                state = entry.get("state") or ("running" if entry["running"] else "exited")
                rows.append(f"{name}\t{name}\t{state}")
            elif entry["running"]:
                ident = session["Id"] if session is not None else name  # type: ignore[index]
                rows.append(f"{ident}\t{name}")
        return completed(stdout="".join(f"{row}\n" for row in rows))

    def _remove(self, args: list[str]) -> subprocess.CompletedProcess[str]:
        name = self._find(args[-1])
        if name is not None:
            del self.containers[name]
        return completed()

    def _build(self, args: list[str]) -> subprocess.CompletedProcess[str]:
        labels = dict(
            args[index + 1].split("=", 1) for index, arg in enumerate(args) if arg == "--label"
        )
        stamps = {} if self.unlabelled_builds else labels
        self.images[args[2]] = (f"sha256:built-{args[2]}", stamps)
        return completed()

    def _run(self, args: list[str]) -> subprocess.CompletedProcess[str]:
        labels = dict(
            args[index + 1].split("=", 1) for index, arg in enumerate(args) if arg == "--label"
        )
        image = args[-1]
        self.add_container(
            args[args.index("--name") + 1], image=self.images[image][0], labels=labels
        )
        return completed()


def serve_egress_docker(monkeypatch: pytest.MonkeyPatch) -> EgressDocker:
    """Route the sidecar module's Docker calls to a new fake and skip the disk preflight."""
    docker = EgressDocker()
    # private: the sidecar module's one Docker seam; no public entry takes a Docker port.
    monkeypatch.setattr(sidecars, "_docker_adapter", lambda: docker)
    monkeypatch.setenv(SKIP_PREFLIGHT_ENV, "1")
    return docker
