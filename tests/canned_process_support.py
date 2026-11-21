"""A real child process that replays a canned subprocess result."""

from __future__ import annotations

import sys
from pathlib import Path

from booley.flows.base import SubprocessResult
from booley.flows.sim.flow import SimulateFlow
from booley.flows.sim.request import SimRequest

_REPLAY = "import sys; sys.stdout.write(sys.argv[1]); sys.stderr.write(sys.argv[2]); sys.exit(int(sys.argv[3]))"


def canned_command(result: SubprocessResult) -> list[str]:
    """An argv whose process writes *result*'s stdout and stderr and exits with its code."""
    return [sys.executable, "-c", _REPLAY, result.stdout, result.stderr, str(result.returncode)]


def measured_flow(project: Path) -> SimulateFlow:
    """A ``SimulateFlow`` bound the way ``execute`` binds it, for its measured ``_execute_boundary``.

    ``_execute_boundary`` is the ``invoke`` callback the Simulation execution takes;
    no public Flow method hands it out.
    """
    flow = SimulateFlow()
    flow._args = SimRequest(target="sim", work_dir=project, report_dir=project / "reports")
    return flow
