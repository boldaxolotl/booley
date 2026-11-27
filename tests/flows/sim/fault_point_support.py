"""Crash injection at the Simulation Flow's publication boundaries."""

from collections.abc import Callable, Iterator
from contextlib import contextmanager
from unittest.mock import patch

from booley.flows.sim import faults


@contextmanager
def inject_faults(hook: Callable[[str], None] | None) -> Iterator[None]:
    """Call *hook* at every publication boundary inside the block; ``None`` injects nothing."""
    if hook is None:
        yield
        return
    with patch.object(faults, "fault_point", hook):  # boundary: the crash-injection hook
        yield
