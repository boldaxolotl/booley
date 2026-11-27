"""Fail a unit test that starts the Docker CLI.

A test outside ``tests/docker/`` and ``tests/smoke/`` fails when it starts a ``docker``
process through ``subprocess.Popen``: at once, or at teardown when the failure was
swallowed.  The ``docker_cli`` marker and installing a shared Docker CLI fake opt out.
It does not see a ``docker`` started by a child process, a shell command line,
``os.system``, ``os.posix_spawn`` or ``os.exec*``.
See ``.github/ci/README.md#docker-cli-guard``.

Standard library and pytest only: the production-image smoke jobs load it too.
"""

from __future__ import annotations

import ntpath
import os
import posixpath
import sys
from collections.abc import Generator
from typing import Any, ClassVar

import pytest

_UNGUARDED_SUITES = ("docker", "smoke")
_FAILED = pytest.StashKey[bool]()


class _Guard:
    """State of the running test."""

    armed: ClassVar[bool] = False
    refused: ClassVar[list[str]] = []


def allow_docker_cli() -> None:
    """Let the running test start ``docker``; the shared fakes call this on install."""
    _Guard.armed = False


def _is_docker(executable: Any, command: Any) -> bool:
    # Windows may pass no executable, only a ``list2cmdline`` string.
    if not executable:
        words = command.split('"' if command.startswith('"') else None)
        executable = next(filter(None, words), "")
    name = ntpath.basename(posixpath.basename(os.fsdecode(executable))).casefold()
    return name.removesuffix(".exe") == "docker"


def _audit(event: str, args: tuple[Any, ...]) -> None:
    # The audit event, unlike a replaced ``Popen``, leaves tests' ``Popen`` patches alone.
    if event != "subprocess.Popen" or not _Guard.armed or not _is_docker(args[0], args[1]):
        return
    command = args[1] if isinstance(args[1], str) else " ".join(map(os.fsdecode, args[1]))
    _Guard.refused.append(command)
    pytest.fail(
        f"test reached the Docker CLI: {command}. Use a shared Docker CLI fake "
        "(docs/internals/TEST_FAKES.md) or mark the test docker_cli "
        "(.github/ci/README.md#docker-cli-guard)."
    )


sys.addaudithook(_audit)  # once per process: an audit hook cannot be removed


@pytest.hookimpl(wrapper=True, tryfirst=True)
def pytest_runtest_setup(item: pytest.Item) -> Generator[None, None, None]:
    # Armed before the fixtures run, so a fixture that installs a fake disarms it.
    suites = (item.config.rootpath / "tests" / name for name in _UNGUARDED_SUITES)
    unguarded = any(item.path.resolve().is_relative_to(suite.resolve()) for suite in suites)
    _Guard.armed = not unguarded and item.get_closest_marker("docker_cli") is None
    _Guard.refused.clear()
    item.stash[_FAILED] = False
    return (yield)


@pytest.hookimpl(wrapper=True)
def pytest_runtest_makereport(
    item: pytest.Item,
) -> Generator[None, pytest.TestReport, pytest.TestReport]:
    report = yield
    if report.failed:
        item.stash[_FAILED] = True
    return report


@pytest.hookimpl(wrapper=True, tryfirst=True)
def pytest_runtest_teardown(item: pytest.Item) -> Generator[None, None, None]:
    try:
        result = yield  # every finalizer runs inside
    finally:
        _Guard.armed = False
    # A test that already failed is not reported twice.
    if _Guard.refused and not item.stash.get(_FAILED, False):
        pytest.fail(
            "test reached the Docker CLI where its failure was swallowed "
            f"(another thread or a broad except): {_Guard.refused}",
            pytrace=False,
        )
    return result
