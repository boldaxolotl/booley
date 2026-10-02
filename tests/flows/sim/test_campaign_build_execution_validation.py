"""Campaign store validation of recorded build-execution evidence.

A successful record either ran the compiler (``ran`` true) or names a reuse
``hit``; any other ``ran``-false record, or one whose process facts contradict
a successful build, is rejected.
"""

from __future__ import annotations

from typing import Any

import pytest

from booley.flows.sim.campaign.codec import SimulationCampaignIntegrityError
from booley.flows.sim.campaign.store import _validate_build_execution_document
from tests.flows.sim.test_campaign_phase3 import _build_execution

_HIT = "hit; Target=acme:lib:demo:1#sim; digest=abc; generation=0123456789abcdef"


def _reuse_record(**build: Any) -> dict[str, Any]:
    document = _build_execution()
    document["build"].update(
        ran=False, output="", elapsed_s=0.0, terminal_record=False, cache_decision=_HIT
    )
    document["build"].update(build)
    return document


def test_recorded_reuse_hit_is_a_successful_build() -> None:
    _validate_build_execution_document(_reuse_record())


def test_compiler_run_with_a_miss_decision_is_a_successful_build() -> None:
    document = _build_execution()
    document["build"]["cache_decision"] = "changed source; Target=t; digest=d; generation=g"
    _validate_build_execution_document(document)


@pytest.mark.parametrize(
    "decision",
    [
        "changed source, recipe, or tool; Target=t; digest=d; generation=g",
        "reuse unsupported; Target=t; digest=unavailable; generation=g",
        "",
        "hit",
        "hitch; Target=t",
        " hit; Target=t",
    ],
    ids=["miss", "unsupported", "empty", "bare-hit", "hit-prefix-word", "leading-space"],
)
def test_build_that_did_not_run_requires_a_hit_decision(decision: str) -> None:
    with pytest.raises(SimulationCampaignIntegrityError, match="not successful"):
        _validate_build_execution_document(_reuse_record(cache_decision=decision))


@pytest.mark.parametrize(
    "contradiction",
    [{"returncode": 1}, {"timed_out": True}, {"verdict": "fail"}],
    ids=["nonzero-returncode", "timed-out", "failed-verdict"],
)
def test_hit_record_with_contradicting_process_facts_is_rejected(
    contradiction: dict[str, Any],
) -> None:
    with pytest.raises(SimulationCampaignIntegrityError, match="not successful"):
        _validate_build_execution_document(_reuse_record(**contradiction))


def test_hit_record_with_non_integer_returncode_is_rejected() -> None:
    with pytest.raises(SimulationCampaignIntegrityError, match="measurement is invalid"):
        _validate_build_execution_document(_reuse_record(returncode=None))
