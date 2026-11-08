"""Read the prepared work document out of a captured Simulation adapter launch."""

from __future__ import annotations

import shlex
from collections.abc import Sequence

from booley.flows.sim.adapter_contract import PreparedSimulationWork

_ADAPTER_MODULE_PREFIX = "booley.flows.sim.backends."


def launched_work(command: Sequence[str] | str) -> PreparedSimulationWork:
    """Decode the work document that follows ``-m booley.flows.sim.backends.<adapter>``.

    *command* is an adapter argv, a shell script, or an ``sh -c <script>``
    tuple. The document is read with the production decoder, so a launch that
    does not carry one valid document fails here.
    """
    from booley.flows.sim.adapter_contract import decode_simulation_work

    words = shlex.split(command) if isinstance(command, str) else list(command)
    if len(words) == 3 and words[1] == "-c":  # an ``sh -c <script>`` tuple
        words = shlex.split(words[2])
    for index in range(1, len(words) - 1):
        if words[index - 1] == "-m" and words[index].startswith(_ADAPTER_MODULE_PREFIX):
            return decode_simulation_work(words[index + 1])
    raise AssertionError(f"no adapter launch in {command!r}")
