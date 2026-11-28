"""Stateful fake of the Docker CLI boundary for headless Sessions whose Project directory may be gone."""

from __future__ import annotations

import contextlib
import copy
import io
import itertools
import json
import os
import shutil
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
from booley.harness import incontainer_register, sandbox_stop_warning
from booley.runtime import (
    booley_dir as booley_dir_module,
)
from booley.runtime import (
    booley_dir_migration,
    host_docker,
    incontainer_setup,
    runtime_attachment,
    session_runtime,
    worktree_mount_repair,
)
from tests.docker_cli_guard_plugin import allow_docker_cli

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


def container_inspect_output(state: dict[str, Any], template: str | None) -> str:
    """Render what `docker container inspect <name> [--format template]` prints for *state*."""
    if template is None:
        return json.dumps([state])
    if template == "{{.State.Running}}":
        return "true" if state["State"]["Running"] else "false"
    if template == "{{json .Image}}":
        return json.dumps(state.get("Image", ""))
    if ".Config.Labels" not in template:
        return json.dumps(state)
    labels = state.get("Config", {}).get("Labels", {})
    if template.startswith("{{json (index "):
        return json.dumps(labels.get(template.split('"')[1]))
    return json.dumps(labels)


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
    *on_call* observes each argv before it is answered. *uncaptured* lists the calls
    whose output stays on the caller's terminal.
    """

    def __init__(self) -> None:
        self.containers: dict[str, dict | str] = {}
        self.networks: set[str] = set()
        self.images: dict[str, dict] = {}
        self.calls: list[list[str]] = []
        self.uncaptured: list[list[str]] = []
        self.removed: list[str] = []
        self.rm_attempts: list[list[str]] = []
        self.failing: set[str] = set()
        self.missing = False
        self.created: dict | None = None
        self.exec_reply: Callable[[list[str]], subprocess.CompletedProcess[str]] | None = None
        self.on_call: Callable[[list[str]], None] | None = None
        self.scripted: list[
            tuple[tuple[str, ...], subprocess.CompletedProcess[str] | Exception]
        ] = []

    def script(
        self, prefix: tuple[str, ...], reply: subprocess.CompletedProcess[str] | Exception
    ) -> None:
        """Answer (or raise, for an exception) every call whose words start with *prefix*."""
        self.scripted.append((prefix, reply))

    def find(self, key: str) -> str | None:
        for name, state in self.containers.items():
            if key == name or (isinstance(state, dict) and state.get("Id") == key):
                return name
        return None

    def run(self, argv: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        if self.missing:
            raise FileNotFoundError("docker")
        if self.on_call is not None:
            self.on_call(argv)
        self.calls.append(argv)
        if kwargs.get("capture") is False:
            self.uncaptured.append(argv)
        for prefix, reply in self.scripted:
            if tuple(argv[1 : 1 + len(prefix)]) == prefix:
                if isinstance(reply, Exception):
                    raise reply
                return reply
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
                {"-a", "-aq"} & set(args) or state["State"].get("Running") is True
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
        template = args[args.index("--format") + 1] if "--format" in args else None
        documents = {None: [record], "{{json .}}": record, "{{json .Id}}": record["Id"]}
        out = json.dumps(documents[template]) if template in documents else record["Id"]
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
        template = args[args.index("--format") + 1] if "--format" in args else None
        out = state if isinstance(state, str) else container_inspect_output(state, template)
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
        host_docker,
        "_run_docker",
        lambda args, *, executable="docker", **_kw: docker.run([executable, *args]),
    )
    return docker


def sandbox_exec_on_path(
    monkeypatch: pytest.MonkeyPatch, bin_dir: Path, target_port: int, container: str
) -> None:
    """Put a `docker` script first on PATH whose `exec -i <container> python3 ...` is a local run.

    The in-Sandbox command runs on this host with its last argument, the T3 Code port,
    replaced by *target_port*: a Sandbox whose loopback is the host's. Other calls fail.
    """
    bin_dir.mkdir()
    script = bin_dir / "docker"
    script.write_bytes(
        f"""#!{sys.executable}
import os, sys
args = sys.argv[1:]
if args[:3] != ["exec", "-i", {container!r}] or args[3] != "python3" or args[-1] != "{incontainer_setup.T3_PORT}":
    sys.exit(1)
os.execv({sys.executable!r}, [{sys.executable!r}, *args[4:-1], "{target_port}"])
""".encode()
    )
    script.chmod(0o755)
    monkeypatch.setenv("PATH", f"{bin_dir}{os.pathsep}{os.environ['PATH']}")


def run_registrar_on_post_start(
    docker: SandboxDocker, workspace: Path, monkeypatch: pytest.MonkeyPatch
) -> list[str]:
    """Run the registrar in-process for each `docker exec` of it; return its stderr per run."""
    # The advisory upgrade and health steps spawn a worker and read host install state.
    monkeypatch.setattr(incontainer_register, "observe_upgrade", lambda: "current")
    monkeypatch.setattr(incontainer_register, "launch_auto_doctor", lambda: "started")
    monkeypatch.setattr(
        worktree_mount_repair,
        "repair_mount_registrations",
        lambda _root: worktree_mount_repair.MountRepair(),
    )
    for name, value in {
        "HOME": str(workspace / "home"),
        "PWD": str(workspace),
        "BOOLEY_AGENT_APP": "claude",
        "BOOLEY_DIR": str(workspace / ".booley"),
    }.items():
        monkeypatch.setenv(name, value)
    monkeypatch.delenv("BOOLEY_T3_BASE_DIR", raising=False)  # never the host Sandbox's own
    monkeypatch.chdir(workspace)
    runs: list[str] = []

    def reply(argv: list[str]) -> subprocess.CompletedProcess[str]:
        registrar = "booley.harness.incontainer_register" in argv[-1]
        if not registrar and "booley.harness.incontainer_t3" not in argv[-1]:
            return completed()
        booley_dir_module.reset_cache()
        out, err = io.StringIO(), io.StringIO()
        with (
            monkeypatch.context() as scope,
            contextlib.redirect_stdout(out),
            contextlib.redirect_stderr(err),
        ):
            for flag, pair in itertools.pairwise(argv):
                if flag == "-e":  # the exec's own environment
                    scope.setenv(*pair.split("=", 1))
            code = 0
            if registrar:
                incontainer_register.main()
                runs.append(err.getvalue())
            else:
                from booley.harness import incontainer_t3

                code = incontainer_t3.main(argv[-1].split()[3:])
        return completed(code, out.getvalue(), err.getvalue())

    docker.exec_reply = reply
    return runs


def route_docker_commands(
    monkeypatch: pytest.MonkeyPatch, docker: SandboxDocker, *, executable: str = "docker"
) -> SandboxDocker:
    """Answer every *executable* argv that reaches ``subprocess.run`` from *docker*; run others for real.

    Pairs with the routing fixtures of ``tests/docker_cli_guard.py``, which send each
    host-side Docker capture through ``subprocess.run``.
    """
    real_run = subprocess.run

    def run(argv: Any, *args: Any, **kwargs: Any) -> Any:
        if isinstance(argv, list) and argv and argv[0] == executable:
            return docker.run(argv, **kwargs)
        return real_run(argv, *args, **kwargs)

    monkeypatch.setattr(subprocess, "run", run)
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


def docker_runner(
    run: Callable[..., subprocess.CompletedProcess[str]],
) -> Callable[[list[str], int], subprocess.CompletedProcess[str]]:
    """Serve admission's ``docker_cli.Runner`` from a full-argv Session fake."""
    return lambda args, timeout: run(["docker", *args], timeout=timeout)


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
    # `_run` is the Session's one Docker CLI runner and no public entry takes a runner.
    monkeypatch.setattr(session_runtime, "_run", run)
    monkeypatch.setattr(host_docker, "container_exists", lambda name: name in existing)
    monkeypatch.setattr(host_docker, "network_exists", lambda _name: False)
    return serve_standalone_probe(monkeypatch, probe)


def serve_standalone_probe(
    monkeypatch: pytest.MonkeyPatch,
    probe: Callable[[list[str]], subprocess.CompletedProcess[str]] | None = None,
) -> list[tuple[list[str], dict[str, Any]]]:
    """Answer the `docker exec` of `session down`'s Standalone Job probe.

    *probe* answers its argv (default: no running Job); returns the `(argv, kwargs)` of
    each probe call.
    """
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
    Installing it lets the test start `docker`: see `tests/docker_cli_guard_plugin.py`.
    """
    allow_docker_cli()
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


def serve_migration_docker(monkeypatch: pytest.MonkeyPatch) -> SandboxDocker:
    """Answer the Booley Directory migration's Docker inventory from a new fake.

    The fake's ``missing`` also hides ``docker`` from command lookup.
    """
    docker = SandboxDocker()
    which = shutil.which

    def lookup(name: str, *args: Any, **kwargs: Any) -> str | None:
        if name == "docker":
            return None if docker.missing else "docker"
        return which(name, *args, **kwargs)

    # The migration's two Docker seams: command lookup and its bounded capture.
    monkeypatch.setattr(shutil, "which", lookup)
    monkeypatch.setattr(
        booley_dir_migration, "run_captured_bounded", lambda argv, **_kw: docker.run(argv)
    )
    return docker
