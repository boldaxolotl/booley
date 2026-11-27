"""Answers and routing at the Docker CLI boundary.

``booley.runtime.host_docker._run_docker`` is the seam the interactive Docker CLI
calls go through; other runtime modules call
``booley.runtime.platform_paths.run_captured_bounded`` directly.  The guard that fails a
test starting ``docker`` is ``tests/docker_cli_guard_plugin.py``.
"""

from __future__ import annotations

import subprocess
from types import ModuleType

import pytest

from booley.flows import runtime_reality
from booley.flows.fpga import vivado_startup
from booley.harness import bootstrap, doctor, sandbox, sandbox_stop_warning
from booley.harness.setup import docker_image, readiness
from booley.mcp import server_inspection
from booley.runtime import (
    booley_dir_migration,
    bundled_extensions,
    docker_capacity,
    host_base_acquisition,
    host_docker,
    image_lifecycle,
    inspection,
    platform_paths,
    project_image,
    session_admission,
    session_runtime,
)


def docker_cli_absent(monkeypatch: pytest.MonkeyPatch) -> list[list[str]]:
    """Make ``_run_docker`` fail as if no ``docker`` executable were installed.

    Returns the argument list of every call, so a test can check what was asked.
    """
    calls: list[list[str]] = []

    def absent(args: list[str], *_args: object, **_kwargs: object):
        calls.append(list(args))
        raise FileNotFoundError(2, "No such file or directory", "docker")

    monkeypatch.setattr(host_docker, "_run_docker", absent)
    return calls


def docker_cli_answers(monkeypatch: pytest.MonkeyPatch, stdout: str) -> list[list[str]]:
    """Make every ``_run_docker`` call succeed with *stdout*; return each call's arguments."""
    calls: list[list[str]] = []

    def answer(args: list[str], *_args: object, **_kwargs: object):
        calls.append(list(args))
        return subprocess.CompletedProcess(["docker", *args], 0, stdout=stdout, stderr="")

    monkeypatch.setattr(host_docker, "_run_docker", answer)
    return calls


def route_docker_helper_through_subprocess_run(monkeypatch: pytest.MonkeyPatch) -> None:
    """Run the shared Docker helper through ``subprocess.run`` (looked up at call time).

    Many tests fake the Docker CLI by patching ``subprocess.run``.  The helper
    itself now starts the CLI with ``Popen`` (so it can kill the whole process
    tree on timeout), which those fakes cannot see.  Modules that fake
    ``subprocess.run`` call this from an autouse fixture so the helper reaches
    their fake again, exactly as it did before.  It also routes the session
    runtime's and admission's bounded capture (see
    :func:`route_session_helpers_through_subprocess_run`) and that of every other
    host-side module that runs a timed Docker CLI command (``HOST_DOCKER_MODULES``).
    """

    def routed(args: list[str], *, timeout: float = 30, executable: str = "docker"):
        # Unbounded child: stands in for production _run_docker; tests' subprocess.run fakes must intercept this call
        return subprocess.run(
            [executable, *args],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout,
            check=False,
        )

    monkeypatch.setattr(host_docker, "_run_docker", routed)
    route_session_helpers_through_subprocess_run(monkeypatch)
    route_bounded_capture_through_subprocess_run(monkeypatch, *HOST_DOCKER_MODULES)


#: Host-side modules, besides the session runtime and admission, whose timed Docker CLI
#: commands go through ``platform_paths.run_captured_bounded``.
HOST_DOCKER_MODULES = (
    bootstrap,
    doctor,
    docker_image,
    readiness,
    runtime_reality,
    sandbox,
    sandbox_stop_warning,
    server_inspection,
    vivado_startup,
    booley_dir_migration,
    docker_capacity,
    host_base_acquisition,
    image_lifecycle,
    inspection,
    project_image,
    bundled_extensions,
)


def _routed_bounded_capture(
    argv: list[str],
    *,
    timeout: float,
    cwd=None,
    env=None,
    errors: str = "replace",
    input: bytes | None = None,  # noqa: A002 - mirrors run_captured_bounded
    stdin=None,
    stdout=None,
    text: bool = True,
):
    """Stand in for ``platform_paths.run_captured_bounded`` using ``subprocess.run``.

    Production starts the child with ``Popen`` so it can kill the whole process tree on
    timeout; tests that fake ``subprocess.run`` cannot see that.  This looks
    ``subprocess.run`` up at call time and takes the same ``timeout``, ``cwd``, ``env``,
    ``errors`` (decoding mode), ``input``, ``stdin``, ``stdout`` (a redirect leaves
    ``result.stdout`` ``None``) and ``text`` (``False`` returns bytes).
    """
    codec = {"encoding": "utf-8", "errors": errors} if text else {}
    # Unbounded child on purpose: the tests' subprocess.run fakes must intercept this call.
    return subprocess.run(
        argv,
        input=input,
        stdin=stdin,
        stdout=subprocess.PIPE if stdout is None else stdout,
        stderr=subprocess.PIPE,
        text=text,
        timeout=timeout,
        cwd=cwd,
        env=env,
        check=False,
        **codec,
    )


def route_bounded_capture_through_subprocess_run(
    monkeypatch: pytest.MonkeyPatch, *modules: ModuleType
) -> None:
    """Run each given module's timed captures through ``subprocess.run``.

    Name only the modules whose commands the test fakes: a module with real-process
    tests must keep the real helper.
    """
    for module in modules:
        monkeypatch.setattr(module, "run_captured_bounded", _routed_bounded_capture)


def route_session_helpers_through_subprocess_run(monkeypatch: pytest.MonkeyPatch) -> None:
    """Run the session runtime's and admission's timed captures through ``subprocess.run``."""
    route_bounded_capture_through_subprocess_run(monkeypatch, session_runtime, session_admission)


def route_bounded_captures_through_subprocess_run(monkeypatch: pytest.MonkeyPatch) -> None:
    """Run host-side Git captures (``platform_paths.run_captured_bounded``) through ``subprocess.run``.

    Those callers look the helper up on ``platform_paths`` at call time, so one patch
    reaches all of them.  Tests that fake ``subprocess.run`` for Git call this so the
    fake still sees the command.  The stand-in keeps each caller's ``errors`` mode.
    """
    monkeypatch.setattr(platform_paths, "run_captured_bounded", _routed_bounded_capture)
