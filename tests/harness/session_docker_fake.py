"""Stateful Docker fake for headless Sessions whose Project directory may be gone."""

from __future__ import annotations

import copy
import json
import os
import stat
import subprocess
import sys
from collections.abc import Callable, Container
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from booley.core.project_identity import project_label_id
from booley.harness import booley as cli
from booley.harness import sandbox_stop_warning
from booley.runtime import (
    booley_dir as booley_dir_module,
)
from booley.runtime import host_docker, runtime_attachment, session_admission, session_runtime

_SRC_ROOT = Path(__file__).parents[2] / "src"


def completed(
    returncode: int = 0, stdout: str = "", stderr: str = ""
) -> subprocess.CompletedProcess[str]:
    return subprocess.CompletedProcess(["docker"], returncode, stdout, stderr)


def headless_session_entry(name: str, root: Path, *, running: bool = True) -> dict[str, Any]:
    """Describe one headless Session the way `docker container inspect` reports it.

    The canonical root is the spelling `session up` recorded: the label digest and the
    `/work` bind source both carry it, and neither needs the directory to exist later.
    """
    canonical = str(root.resolve())
    return {
        "Id": f"id-{name}",
        "Name": f"/{name}",
        "Created": "2026-01-01T00:00:00Z",
        "Image": "sha256:session",
        "State": {"Running": running, "StartedAt": "2026-01-01T00:00:01Z"},
        "Config": {
            "Env": [],
            "Labels": {
                "booley.role": "interactive",
                "booley.project-id": project_label_id(canonical),
            },
        },
        "NetworkSettings": {"Networks": {}},
        "Mounts": [{"Type": "bind", "Destination": "/work", "Source": canonical}],
    }


class SessionDockerFake:
    """Serve the `docker` argv `session down` and the hint builders issue."""

    def __init__(self) -> None:
        self.containers: dict[str, dict[str, Any]] = {}
        self.calls: list[list[str]] = []
        self.removed: list[str] = []
        self.stopped: list[str] = []
        self.probed: list[tuple[list[str], dict[str, Any]]] = []

    def add_session(self, name: str, root: Path) -> None:
        self.containers[name] = headless_session_entry(name, root)

    def _find(self, key: str) -> str | None:
        for name, entry in self.containers.items():
            if key in {name, entry["Id"]}:
                return name
        return None

    def run(self, argv: list[str], **_kwargs: object) -> subprocess.CompletedProcess[str]:
        self.calls.append(list(argv))
        args = argv[1:]
        if args[0] == "ps":
            return self._ps(args)
        if args[:2] == ["container", "inspect"]:
            name = self._find(args[2])
            if name is None:
                return completed(1, stderr="No such container")
            return completed(stdout=json.dumps(self.containers[name]))
        if args[0] == "stop":
            self.stopped.append(args[1])
            return completed()
        if args[:2] == ["rm", "-f"]:
            name = self._find(args[2])
            if name is not None:
                self.removed.append(name)
                del self.containers[name]
            return completed()
        raise AssertionError(f"unexpected docker call: {argv}")

    def _ps(self, args: list[str]) -> subprocess.CompletedProcess[str]:
        rows = []
        for name, entry in sorted(self.containers.items()):
            labels = entry["Config"]["Labels"]
            if "-a" in args:
                continue  # headless Sessions carry no VS Code folder label
            if "--no-trunc" in args:
                rows.append(
                    f"{entry['Id']}\t{name}\t\t{labels['booley.project-id']}\t{labels['booley.role']}"
                )
            elif entry["State"]["Running"]:
                rows.append(f"{entry['Id']}\t{name}")
        return completed(stdout="".join(f"{row}\n" for row in rows))


class SandboxDocker:
    """Serve the Docker verbs of the Sandbox checks and of `session up`, from memory.

    A container state that is a string is inspect output Docker could not encode.
    *failing* names the verbs, containers and last arguments whose calls fail.
    A stop or remove names a container or its ``Id``. *images* maps a reference to its
    `docker image inspect` record; *networks* names the existing networks. `run`
    stores *created* (a copy) under the new name, or a minimal running state; `exec`
    answers *exec_reply*. *missing* makes every call raise like an absent CLI, and
    *on_call* observes each argv before it is answered.
    """

    def __init__(self) -> None:
        self.containers: dict[str, dict | str] = {}
        self.networks: set[str] = set()
        self.images: dict[str, dict] = {}
        self.calls: list[list[str]] = []
        self.removed: list[str] = []
        self.rm_attempts: list[list[str]] = []
        self.failing: set[str] = set()
        self.missing = False
        self.created: dict | None = None
        self.exec_reply: Callable[[list[str]], subprocess.CompletedProcess[str]] | None = None
        self.on_call: Callable[[list[str]], None] | None = None

    def find(self, key: str) -> str | None:
        for name, state in self.containers.items():
            if key == name or (isinstance(state, dict) and state.get("Id") == key):
                return name
        return None

    def run(self, argv: list[str], **_kwargs: object) -> subprocess.CompletedProcess[str]:
        if self.missing:
            raise FileNotFoundError("docker")
        if self.on_call is not None:
            self.on_call(argv)
        self.calls.append(argv)
        verb, *args = argv[1:]
        if verb == "rm":
            self.rm_attempts.append(argv)
        if verb in self.failing or (
            args and (args[0] in self.failing or args[-1] in self.failing)
        ):
            return subprocess.CompletedProcess(argv, 1, "", f"{verb} failed: container is running")
        # `_<verb>` names this fake's own handler methods, not a production name.
        handler = getattr(self, f"_{verb}", None)
        if handler is None:
            raise AssertionError(f"unexpected docker call: {argv}")
        return handler(argv, args)

    def _ps(self, argv: list[str], args: list[str]) -> subprocess.CompletedProcess[str]:
        filters = [
            args[i + 1].removeprefix("label=") for i, a in enumerate(args) if a == "--filter"
        ]
        rows = []
        for name, state in self.containers.items():
            if not isinstance(state, dict) or not (
                "-a" in args or state["State"].get("Running") is True
            ):
                continue
            labels = state["Config"]["Labels"]
            if not all(labels.get(k) == v for k, v in (f.split("=", 1) for f in filters)):
                continue
            if "--no-trunc" in args:
                rows.append(
                    "\t".join(
                        [
                            state["Id"],
                            name,
                            labels.get("devcontainer.local_folder", ""),
                            labels.get("booley.project-id", ""),
                            labels.get("booley.role", ""),
                        ]
                    )
                )
            else:
                rows.append(name)
        return subprocess.CompletedProcess(argv, 0, "".join(f"{row}\n" for row in rows), "")

    def _image(self, argv: list[str], args: list[str]) -> subprocess.CompletedProcess[str]:
        record = self.images.get(args[1])
        if args[0] != "inspect" or record is None:
            return subprocess.CompletedProcess(argv, 1, "", f"No such image: {args[-1]}")
        out = record["Id"] if "--format" in args else json.dumps([record])
        return subprocess.CompletedProcess(argv, 0, out, "")

    def _network(self, argv: list[str], args: list[str]) -> subprocess.CompletedProcess[str]:
        if args[0] == "create":
            self.networks.add(args[-1])
        elif args[0] == "inspect" and args[1] not in self.networks:
            return subprocess.CompletedProcess(argv, 1, "", "No such network")
        elif "--format" in args:
            return subprocess.CompletedProcess(argv, 0, "false", "")
        return subprocess.CompletedProcess(argv, 0, "", "")

    def _inspect(self, argv: list[str], args: list[str]) -> subprocess.CompletedProcess[str]:
        # `docker inspect <name> --format F` or `docker inspect --format F <name>`.
        name = next((n for a in args if (n := self.find(a)) is not None), None)
        if name is None:
            return subprocess.CompletedProcess(argv, 1, "", "No such container")
        state = self.containers[name]
        if "--format" in args and isinstance(state, dict):
            fmt = args[args.index("--format") + 1]
            if fmt == "{{.Image}}":
                return subprocess.CompletedProcess(argv, 0, state.get("Image", ""), "")
            if "devcontainer.local_folder" in fmt:
                folder = state["Config"]["Labels"].get("devcontainer.local_folder", "")
                return subprocess.CompletedProcess(argv, 0, folder, "")
            state = state["Config"]["Labels"]
        out = (
            state
            if isinstance(state, str)
            else json.dumps(state if "--format" in args else [state])
        )
        return subprocess.CompletedProcess(argv, 0, out, "")

    def _container(self, argv: list[str], args: list[str]) -> subprocess.CompletedProcess[str]:
        if args[0] == "ls":
            return subprocess.CompletedProcess(argv, 0, "", "")
        name = self.find(args[1])  # `docker container inspect <name> [--format F]`
        if name is None:
            return subprocess.CompletedProcess(argv, 1, "", "No such container")
        state = self.containers[name]
        if "{{.State.Running}}" in args and isinstance(state, dict):
            return subprocess.CompletedProcess(
                argv, 0, "true" if state["State"]["Running"] else "false", ""
            )
        out = (
            state
            if isinstance(state, str)
            else json.dumps(state if "--format" in args else [state])
        )
        return subprocess.CompletedProcess(argv, 0, out, "")

    def _run(self, argv: list[str], args: list[str]) -> subprocess.CompletedProcess[str]:
        name = args[args.index("--name") + 1]
        image = args[-3]  # `... <image> sleep infinity`
        labels = {
            args[i + 1].split("=", 1)[0]: args[i + 1].split("=", 1)[1]
            for i, a in enumerate(args)
            if a == "--label"
        }
        self.containers[name] = (
            copy.deepcopy(self.created)
            if self.created is not None
            else {
                "Id": f"id-{name}",
                "Name": f"/{name}",
                "Created": "2026-01-01T00:00:00Z",
                "Image": self.images.get(image, {}).get("Id", image),
                "State": {"Running": True, "StartedAt": "2026-01-01T00:00:01Z"},
                "Config": {"Labels": labels, "Env": []},
                "NetworkSettings": {"Networks": {}},
                "Mounts": [],
            }
        )
        return subprocess.CompletedProcess(argv, 0, f"id-{name}", "")

    def _start(self, argv: list[str], args: list[str]) -> subprocess.CompletedProcess[str]:
        state = self.containers[args[-1]]
        assert isinstance(state, dict)
        state["State"]["Running"] = True
        return subprocess.CompletedProcess(argv, 0, "", "")

    def _rename(self, argv: list[str], args: list[str]) -> subprocess.CompletedProcess[str]:
        self.containers[args[1]] = self.containers.pop(args[0])
        return subprocess.CompletedProcess(argv, 0, "", "")

    def _exec(self, argv: list[str], args: list[str]) -> subprocess.CompletedProcess[str]:
        del args
        return (
            self.exec_reply(argv)
            if self.exec_reply
            else subprocess.CompletedProcess(argv, 0, "", "")
        )

    def _stop(self, argv: list[str], args: list[str]) -> subprocess.CompletedProcess[str]:
        name = self.find(args[-1])
        assert name is not None, argv
        self.containers[name]["State"]["Running"] = False  # type: ignore[index]
        return subprocess.CompletedProcess(argv, 0, "", "")

    def _rm(self, argv: list[str], args: list[str]) -> subprocess.CompletedProcess[str]:
        name = self.find(args[-1])
        assert name is not None, argv
        self.removed.append(name)
        del self.containers[name]
        return subprocess.CompletedProcess(argv, 0, "", "")


def serve_sandbox_docker(monkeypatch: pytest.MonkeyPatch) -> SandboxDocker:
    """Route every Docker command of `session_runtime` and `host_docker` to a new fake."""
    docker = SandboxDocker()
    # The two seams that replace the Docker CLI; no public entry takes a runner.
    monkeypatch.setattr(session_runtime, "_run", docker.run)
    monkeypatch.setattr(
        host_docker, "_run_docker", lambda args, **_kw: docker.run(["docker", *args])
    )
    return docker


def attach_commands(
    monkeypatch: pytest.MonkeyPatch,
    *,
    error: Exception | None = None,
    before: Callable[[], None] | None = None,
) -> list[tuple[Path, str, list[str], dict[str, Any]]]:
    """Stand in for the Sandbox attachment; return the `(root, container, command, kwargs)` it saw.

    The real attachment spawns `docker exec` with the in-Sandbox supervisor and is
    covered by `test_runtime_attachment.py` on Linux with real processes.
    """
    attached: list[tuple[Path, str, list[str], dict[str, Any]]] = []

    def run_command(root: Path, container: str, command: list[str], **kwargs: Any) -> object:
        attached.append((root, container, command, kwargs))
        if before is not None:
            before()
        if error is not None:
            raise error
        return SimpleNamespace(exit_code=0)

    monkeypatch.setattr(runtime_attachment, "run_command", run_command)
    return attached


def isolate_session_cli(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """Isolate host state under *tmp_path* and skip the host-only and host-install gates."""
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "xdg-config"))
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "xdg-state"))
    # Private: both gates read the test runner's install location, which no CLI argument sets.
    monkeypatch.setattr(cli, "_enforce_runtime_location", lambda _command: None)
    monkeypatch.setattr(cli, "_host_install_authority_error", lambda _command: None)
    monkeypatch.setattr(cli.runtime_context, "ensure_proxy_env", lambda: False)


def route_session_commands(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    run: Callable[..., subprocess.CompletedProcess[str]],
    existing: Container[str],
    *,
    probe: Callable[[list[str]], subprocess.CompletedProcess[str]] | None = None,
) -> list[tuple[list[str], dict[str, Any]]]:
    """Send every Docker call `booley session down` makes to *run*.

    Also isolates host state under *tmp_path* and skips the host-only and host-install
    gates. *probe* answers the argv of the Standalone Job probe's `docker exec` (default:
    no running Job); returns the `(argv, kwargs)` of each probe call.
    """
    isolate_session_cli(monkeypatch, tmp_path)
    # `_run` is each module's one Docker CLI runner and no public entry takes a runner.
    monkeypatch.setattr(session_runtime, "_run", run)
    monkeypatch.setattr(session_admission, "_run", run)
    monkeypatch.setattr(host_docker, "container_exists", lambda name: name in existing)
    monkeypatch.setattr(host_docker, "network_exists", lambda _name: False)
    probed: list[tuple[list[str], dict[str, Any]]] = []

    def run_probe(argv: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        probed.append((list(argv), kwargs))
        if probe is None:
            return completed(stdout='{"running": [], "unreadable": []}')
        return probe(argv)

    # The probe's `docker exec` is the one `session down` call that bypasses `_run`.
    monkeypatch.setattr(sandbox_stop_warning, "run_captured_bounded", run_probe)
    return probed


def install_sandbox_exec(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, booley_dir: Path, *, protocol: bool = True
) -> None:
    """Put a `docker` on PATH whose `exec` runs the in-Sandbox `python3` command here.

    It applies the `-e` pairs, points `PYTHONPATH` at `src/` and sets `BOOLEY_DIR`. With
    *protocol* false it runs only `python3 -I` commands (the cwd and Standalone Job
    probes) and exits 127 for the execution supervisor, a Sandbox without an execution
    protocol. Linux only: the shim has a shebang and the Sandbox processes read `/proc`.
    """
    guard = "" if protocol else "if args[start+1]!='-I':sys.exit(127)\n"
    binary = tmp_path / "docker"
    binary.write_text(
        "#!/usr/bin/python3\n"
        "import os,sys\n"
        "args=sys.argv[1:]\n"
        "for index,arg in enumerate(args):\n"
        " if arg=='-e':\n"
        "  key,value=args[index+1].split('=',1)\n"
        "  os.environ[key]=value\n"
        "start=args.index('python3')\n"
        f"{guard}"
        "os.execvp(args[start],args[start:])\n",
        encoding="utf-8",
    )
    binary.chmod(binary.stat().st_mode | stat.S_IXUSR)
    monkeypatch.setenv("PATH", f"{tmp_path}{os.pathsep}{os.environ['PATH']}")
    monkeypatch.setenv("PYTHONPATH", f"{_SRC_ROOT}{os.pathsep}{os.environ.get('PYTHONPATH', '')}")
    monkeypatch.setenv("BOOLEY_DIR", str(booley_dir))
    booley_dir_module.reset_cache()


def run_cli(monkeypatch: pytest.MonkeyPatch, argv: list[str]) -> int:
    """Run `booley <argv>` in-process and return its exit code."""
    monkeypatch.setattr(sys, "argv", ["booley", *argv])
    return cli.main()
