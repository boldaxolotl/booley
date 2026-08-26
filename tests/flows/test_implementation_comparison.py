from types import SimpleNamespace

import pytest

from booley.dev_support.criteria import BASELINE_TARGET_PARAM
from booley.flows.implementation_comparison import (
    ImplementationComparisonError,
    ImplementationTargetPair,
    target_pairs_for_candidates,
)


def test_missing_metadata_preserves_equal_target_behavior() -> None:
    assert target_pairs_for_candidates({}, "synthesis_ok_", ["synth_default"]) == (
        ImplementationTargetPair("synth_default", "synth_default"),
    )


def test_reads_paired_baseline_from_candidate_criterion() -> None:
    criteria = {
        "synthesis_ok_synth_after": SimpleNamespace(params={BASELINE_TARGET_PARAM: "synth_before"})
    }

    assert target_pairs_for_candidates(criteria, "synthesis_ok_", ["synth_after"]) == (
        ImplementationTargetPair("synth_before", "synth_after"),
    )


def test_invalid_persisted_baseline_fails_closed() -> None:
    criteria = {"synthesis_ok_synth_after": SimpleNamespace(params={BASELINE_TARGET_PARAM: ""})}

    with pytest.raises(ImplementationComparisonError, match="invalid baseline Target"):
        target_pairs_for_candidates(criteria, "synthesis_ok_", ["synth_after"])
