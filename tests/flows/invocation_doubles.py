"""Per-instance doubles for a built-in Flow's prepared invocation.

A double replaces what preparation hands to admission and invocation, the
supported seam, instead of patching the Flow's private run.  Doubles select no
Target, like a Flow that never resolves one.
"""

from collections.abc import Callable

import pytest

from booley.flows.base import BuiltinFlow
from booley.flows.prepared_invocation import OrdinaryRunInvocation, PreparedInvocation
from booley.runtime.endpoint_execution import EndpointOutcome


def prepare_as(
    monkeypatch: pytest.MonkeyPatch,
    flow: BuiltinFlow,
    invocation: PreparedInvocation | EndpointOutcome,
) -> None:
    """Make *flow* select no Target and prepare *invocation* (or refuse with it)."""
    monkeypatch.setattr(flow, "prepare_target_endpoint", lambda: None)
    monkeypatch.setattr(flow, "prepare_endpoint_invocation", lambda: invocation)


def replace_run(
    monkeypatch: pytest.MonkeyPatch, flow: BuiltinFlow, run: Callable[[], EndpointOutcome]
) -> None:
    """Replace *flow*'s run with *run* through the ordinary-run prepared invocation."""
    prepare_as(monkeypatch, flow, OrdinaryRunInvocation(run))
