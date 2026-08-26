from pathlib import Path
from types import SimpleNamespace

import pytest

from booley.dev_support.criteria import BASELINE_TARGET_PARAM, TargetPair
from booley.flows.implementation_comparison import (
    ImplementationComparisonError,
    target_pairs_for_candidates,
)
from booley.ticket_board.target_contract import ContractTargetBinding, TargetContract


def test_missing_metadata_preserves_equal_target_behavior() -> None:
    assert target_pairs_for_candidates({}, "synthesis_ok_", ["synth_default"]) == (
        TargetPair("synth_default", "synth_default"),
    )


def test_reads_paired_baseline_from_candidate_criterion() -> None:
    criteria = {
        "synthesis_ok_synth_after": SimpleNamespace(params={BASELINE_TARGET_PARAM: "synth_before"})
    }

    assert target_pairs_for_candidates(criteria, "synthesis_ok_", ["synth_after"]) == (
        TargetPair("synth_before", "synth_after"),
    )


def test_invalid_persisted_baseline_fails_closed() -> None:
    criteria = {"synthesis_ok_synth_after": SimpleNamespace(params={BASELINE_TARGET_PARAM: ""})}

    with pytest.raises(ImplementationComparisonError, match="invalid baseline Target"):
        target_pairs_for_candidates(criteria, "synthesis_ok_", ["synth_after"])


def _sealed_project(tmp_path: Path) -> TargetContract:
    (tmp_path / "toy.core").write_text(
        """CAPI=2:
name: acme:lib:toy:1.0
targets:
  synth_before: {flow: generic}
  synth_after: {flow: generic}
  synth_other: {flow: generic}
""",
        encoding="utf-8",
    )
    return TargetContract(
        outer_sha="a" * 40,
        project_sha="",
        surface_digest="b" * 64,
        targets=("synth_after", "synth_before"),
        bindings=(
            ContractTargetBinding(
                flow="synth",
                criterion="synthesis_ok",
                baseline="acme:lib:toy:1.0#synth_before",
                candidate="acme:lib:toy:1.0#synth_after",
            ),
        ),
    )


def test_schema_two_executes_selector_verified_against_sealed_pair(tmp_path: Path) -> None:
    contract = _sealed_project(tmp_path)
    criteria = {
        "synthesis_ok_synth_after": SimpleNamespace(params={BASELINE_TARGET_PARAM: "synth_before"})
    }

    pairs = target_pairs_for_candidates(
        criteria,
        "synthesis_ok_",
        ["synth_after"],
        contract=contract,
        project_root=tmp_path,
        flow="synth",
    )

    assert pairs == (TargetPair("synth_before", "synth_after"),)


@pytest.mark.parametrize("baseline", [None, "synth_other"])
def test_schema_two_rejects_resumed_state_that_disagrees_with_contract(
    tmp_path: Path, baseline: str | None
) -> None:
    contract = _sealed_project(tmp_path)
    params = {} if baseline is None else {BASELINE_TARGET_PARAM: baseline}
    criteria = {"synthesis_ok_synth_after": SimpleNamespace(params=params)}

    with pytest.raises(ImplementationComparisonError, match="does not match the sealed"):
        target_pairs_for_candidates(
            criteria,
            "synthesis_ok_",
            ["synth_after"],
            contract=contract,
            project_root=tmp_path,
            flow="synth",
        )
