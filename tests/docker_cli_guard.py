"""Guard that keeps unit tests from starting a real ``docker`` process.

``booley.runtime.interactive_docker._run_docker`` is the single seam every
Docker CLI call goes through.  Tests that must never reach Docker replace it
with a function that fails the test at once and records the call, so a call
made from a worker thread (where the failure may be swallowed) still fails the
test when :func:`assert_docker_cli_unused` runs at teardown.
"""

from __future__ import annotations

import ntpath
import posixpath
import subprocess

import pytest

from booley.runtime import interactive_docker, platform_paths, session_admission, session_runtime

#: Argument lists of every Docker CLI call attempted since the last install.
DOCKER_CLI_CALLS: list[list[str]] = []


def _fail_on_docker_call(args: list[str], *_args: object, **_kwargs: object):
    """Stand-in for ``_run_docker``; accepts ``timeout=`` and ``executable=``."""
    DOCKER_CLI_CALLS.append(list(args))
    pytest.fail(f"test reached the Docker CLI: docker {' '.join(args)}")


def forbid_docker_cli(monkeypatch: pytest.MonkeyPatch) -> None:
    """Make any ``_run_docker`` call fail the running test."""
    DOCKER_CLI_CALLS.clear()
    monkeypatch.setattr(interactive_docker, "_run_docker", _fail_on_docker_call)


def assert_docker_cli_unused() -> None:
    """Fail when a guarded ``_run_docker`` call happened (even if swallowed)."""
    calls = list(DOCKER_CLI_CALLS)
    DOCKER_CLI_CALLS.clear()
    assert not calls, f"test reached the Docker CLI: {calls}"


def route_docker_helper_through_subprocess_run(monkeypatch: pytest.MonkeyPatch) -> None:
    """Run the shared Docker helper through ``subprocess.run`` (looked up at call time).

    Many tests fake the Docker CLI by patching ``subprocess.run``.  The helper
    itself now starts the CLI with ``Popen`` (so it can kill the whole process
    tree on timeout), which those fakes cannot see.  Modules that fake
    ``subprocess.run`` call this from an autouse fixture so the helper reaches
    their fake again, exactly as it did before.  It also routes the session
    runtime's and admission's bounded capture (see
    :func:`route_session_helpers_through_subprocess_run`).
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

    monkeypatch.setattr(interactive_docker, "_run_docker", routed)
    route_session_helpers_through_subprocess_run(monkeypatch)


def _routed_bounded_capture(
    argv: list[str], *, timeout: float, cwd=None, env=None, errors: str = "replace"
):
    """Stand in for ``platform_paths.run_captured_bounded`` using ``subprocess.run``.

    Production starts the child with ``Popen`` so it can kill the whole process tree on
    timeout; tests that fake ``subprocess.run`` cannot see that.  This looks
    ``subprocess.run`` up at call time and takes the same ``timeout``, ``cwd``, ``env``
    and ``errors`` (decoding mode).
    """
    # Unbounded child on purpose: the tests' subprocess.run fakes must intercept this call.
    return subprocess.run(
        argv,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors=errors,
        timeout=timeout,
        cwd=cwd,
        env=env,
        check=False,
    )


def route_session_helpers_through_subprocess_run(monkeypatch: pytest.MonkeyPatch) -> None:
    """Run the session runtime's and admission's timed captures through ``subprocess.run``."""
    monkeypatch.setattr(session_runtime, "run_captured_bounded", _routed_bounded_capture)
    monkeypatch.setattr(session_admission, "run_captured_bounded", _routed_bounded_capture)


def route_bounded_captures_through_subprocess_run(monkeypatch: pytest.MonkeyPatch) -> None:
    """Run host-side Git captures (``platform_paths.run_captured_bounded``) through ``subprocess.run``.

    Those callers look the helper up on ``platform_paths`` at call time, so one patch
    reaches all of them.  Tests that fake ``subprocess.run`` for Git call this so the
    fake still sees the command.  The stand-in keeps each caller's ``errors`` mode.
    """
    monkeypatch.setattr(platform_paths, "run_captured_bounded", _routed_bounded_capture)


#: Command lines of every ``docker`` process a guarded test tried to start.
DOCKER_PROCESS_STARTS: list[str] = []


def _program_of(args: object, executable: object) -> str:
    """Name of the program a ``Popen`` call would start, or ``""`` when unknowable."""
    if executable:
        return str(executable)
    if isinstance(args, str):
        words = args.split()
        return words[0] if words else ""
    try:
        first = next(iter(args), "")  # type: ignore[call-overload]
    except TypeError:
        return ""
    return str(first)


def _is_docker(program: str) -> bool:
    """True when *program* is ``docker`` (any directory, any case, optional ``.exe``)."""
    name = ntpath.basename(posixpath.basename(program)).casefold()
    return name.removesuffix(".exe") == "docker"


def forbid_docker_processes(monkeypatch: pytest.MonkeyPatch) -> None:
    """Make starting a ``docker`` process fail the running test.

    Replaces ``subprocess.Popen`` with a subclass that records and fails the test
    when the program it would start is ``docker`` (the ``executable=`` keyword if
    given, else the first word of a string command or ``args[0]`` of a list).
    ``subprocess.run`` and ``platform_paths.run_captured_bounded`` both build a
    ``Popen``, so both are covered.  Pair it with :func:`assert_docker_processes_unused`
    at teardown to catch a failure swallowed by a broad ``except``.

    It does not catch a ``from subprocess import Popen`` binding made before the
    patch, ``os.spawn*``/``os.system``, or ``docker`` hidden inside a ``shell=True``
    pipeline.
    """
    DOCKER_PROCESS_STARTS.clear()
    real_popen = subprocess.Popen

    class _GuardedPopen(real_popen):  # type: ignore[misc, valid-type]
        def __init__(self, args, *popen_args, **kwargs):
            program = _program_of(args, kwargs.get("executable"))
            if _is_docker(program):
                shown = args if isinstance(args, str) else " ".join(map(str, args))
                DOCKER_PROCESS_STARTS.append(shown)
                pytest.fail(f"test started a docker process: {shown}")
            super().__init__(args, *popen_args, **kwargs)

    monkeypatch.setattr(subprocess, "Popen", _GuardedPopen)


def assert_docker_processes_unused() -> None:
    """Fail when a guarded test tried to start ``docker`` (even if swallowed)."""
    starts = list(DOCKER_PROCESS_STARTS)
    DOCKER_PROCESS_STARTS.clear()
    assert not starts, f"test started a docker process: {starts}"
