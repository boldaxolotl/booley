"""Test doubles for Simulation runs that go through a leased build session.

Production always holds a ``SimulationBuildSession``. Tests that fake the
process boundary keep that session real and replace only the parts that need
a real EDAM or image: input capture, reuse keys and image authorization. The
build and launch run as two separate commands, so one-call canned invokers
are split into those two phases here.
"""

import re
from collections.abc import Callable, Iterator
from contextlib import ExitStack, contextmanager
from dataclasses import replace
from typing import Literal
from unittest.mock import patch

from booley.flows.base import SubprocessResult
from booley.flows.sim.build_session import SimulationBuildSession

_MARKER = re.compile(r"BOOLEY_BUILD_STAGE token=(\S+) rc=(\d+)")
_TOKEN = re.compile(r"token=(\S+)")

Invoker = Callable[..., SubprocessResult]


@contextmanager
def canned_build_session() -> Iterator[None]:
    """Keep the leased session real; stub capture, reuse key and authorization."""
    with ExitStack() as stack:
        stack.enter_context(
            patch.object(SimulationBuildSession, "capture_inputs", return_value={})
        )
        stack.enter_context(
            patch.object(SimulationBuildSession, "reusable_key", return_value=None)
        )
        stack.enter_context(
            patch.object(SimulationBuildSession, "authorize_fresh_image", return_value=())
        )
        yield


def without_build_record(process: SubprocessResult) -> SubprocessResult:
    """Drop build-stage records; the engine classifies build and launch separately."""
    return replace(
        process,
        stdout="".join(
            line
            for line in process.stdout.splitlines(keepends=True)
            if "BOOLEY_BUILD_STAGE" not in line
        ),
    )


def split_invoker(invoke: Invoker, *, canned_at: Literal["launch", "build"] = "launch") -> Invoker:
    """Turn a one-call canned invoker into separate build and launch calls.

    ``canned_at="launch"`` runs the canned invoker at launch and answers the
    build with a passing record. ``canned_at="build"`` runs it at the build,
    for fakes that model a failed build; a passing canned process is replayed
    at launch.
    """
    stash: list[SubprocessResult] = []

    def wrapped(command: list[str], *, timeout: float) -> SubprocessResult:
        if "BOOLEY_BUILD_STAGE" not in command[-1]:
            if canned_at == "build":
                return without_build_record(stash.pop())
            launched = invoke(command, timeout=timeout)
            record = _MARKER.search(launched.stdout)
            assert record is None or record.group(2) == "0", (
                "launch-mode fake modelled a failed build"
            )
            return without_build_record(launched)
        token = _TOKEN.search(command[-1])
        assert token is not None
        built = SubprocessResult(
            returncode=0, stdout=f"BOOLEY_BUILD_STAGE token={token.group(1)} rc=0\n"
        )
        if canned_at == "launch":
            return built
        canned = invoke(command, timeout=timeout)
        record = _MARKER.search(canned.stdout)
        if canned.timed_out or record is None or record.group(2) != "0":
            return canned
        stash.append(canned)
        return built

    return wrapped
