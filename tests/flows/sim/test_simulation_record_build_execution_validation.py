"""Simulation Record store validation of recorded build-execution evidence.

A successful record either ran the compiler (``ran`` true) or names a reuse
``hit``; any other ``ran``-false record, or one whose process facts contradict
a successful build, is rejected.
"""

from __future__ import annotations

from functools import partial
from typing import Any

import pytest

from booley.flows.sim.record.codec import SimulationRecordIntegrityError
from booley.flows.sim.record.store import _validate_build_execution_document
from tests.flows.sim.simulation_record_test_support import _build_execution

_HIT = "hit; Target=acme:lib:demo:1#sim; digest=abc; generation=0123456789abcdef"


def _record(*, ran: bool = False, **build: Any) -> dict[str, Any]:
    """A successful build record: a reuse hit, or with *ran* a compiler run."""
    document = _build_execution()
    if not ran:
        document["build"].update(
            ran=False, output="", elapsed_s=0.0, terminal_record=False, cache_decision=_HIT
        )
    document["build"].update(build)
    return document


_DECLINED = (
    "changed source, recipe, or tool; Target=t; digest=d; generation=g",
    "reuse unsupported; Target=t; digest=unavailable; generation=g",
    "",
    "hit",
    "hitch; Target=t",
    " hit; Target=t",
)
_CASES: list[tuple[dict[str, Any], str | None]] = [
    ({}, None),
    ({"ran": True, "cache_decision": "changed source; Target=t; digest=d; generation=g"}, None),
    *(({"cache_decision": decision}, "not successful") for decision in _DECLINED),
    ({"returncode": 1}, "not successful"),
    ({"timed_out": True}, "not successful"),
    ({"verdict": "fail"}, "not successful"),
    ({"returncode": None}, "measurement is invalid"),
]


@pytest.mark.parametrize(("build", "error"), _CASES)
def test_build_execution_evidence_is_validated(build: dict[str, Any], error: str | None) -> None:
    # Private seam: the store validates build-execution evidence only while loading a
    # published shared build, which needs a whole Simulation Record run.
    validate = partial(_validate_build_execution_document, _record(**build))
    if error is None:
        validate()
    else:
        with pytest.raises(SimulationRecordIntegrityError, match=error):
            validate()
