"""Guard that keeps unit tests from starting a real ``docker`` process.

``booley.runtime.interactive_docker._run_docker`` is the single seam every
Docker CLI call goes through.  Tests that must never reach Docker replace it
with a function that fails the test at once and records the call, so a call
made from a worker thread (where the failure may be swallowed) still fails the
test when :func:`assert_docker_cli_unused` runs at teardown.
"""

from __future__ import annotations

import subprocess

import pytest

from booley.runtime import interactive_docker

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
    their fake again, exactly as it did before.
    """

    def routed(args: list[str], *, timeout: float = 30, executable: str = "docker"):
        # Unbounded child: test double; tests' subprocess.run fakes must intercept this call
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
