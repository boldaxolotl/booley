"""Human amendment requests must preserve unaffected Criterion instances."""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from booley.criteria.templates import CriteriaTemplate
from booley.ticket_board.amendment_proposal import (
    AmendmentProposalError,
    build_amendment_proposal,
)


def _fields() -> dict:
    return {
        "scope": ["rtl/design.sv"],
        "criteria": {
            "mandatory": {
                "synthesis_ok": {"targets": ["synth_a", "synth_b"], "fmax_mhz_min": 500},
                "sim_pass": ["tb.sv @ sim_a @ pass -> pass"],
            },
            "optional": {},
        },
    }


def test_relax_one_target_and_keep_its_sibling(tmp_path: Path) -> None:
    fields = _fields()
    proposal = build_amendment_proposal(
        fields,
        {
            "reason": "The available clock tops out at 438 MHz",
            "criteria": [
                {"criterion": "synthesis_ok_synth_a", "thresholds": {"fmax_mhz_min": 430}}
            ],
        },
        tmp_path,
    )

    assert proposal.fields["criteria"]["mandatory"]["synthesis_ok"] == [
        {"target": "synth_a", "fmax_mhz_min": 430},
        {"target": "synth_b", "fmax_mhz_min": 500},
    ]
    assert CriteriaTemplate.from_yaml(proposal.fields["criteria"]).expand(["default"]) == (
        CriteriaTemplate.from_yaml(fields["criteria"]).expand(["default"])
    )
    assert fields["criteria"]["mandatory"]["synthesis_ok"]["fmax_mhz_min"] == 500


def test_last_mandatory_can_become_optional(tmp_path: Path) -> None:
    fields = {"scope": ["rtl/design.sv"], "criteria": {"mandatory": {"review_rtl_bugs": True}}}
    proposal = build_amendment_proposal(
        fields,
        {
            "reason": "Human accepted residual review risk",
            "criteria": [{"criterion": "review_rtl_bugs_clean", "make_optional": True}],
        },
        tmp_path,
    )
    assert proposal.fields["criteria"] == {"mandatory": {}, "optional": {"review_rtl_bugs": True}}


@pytest.mark.parametrize(
    "threshold",
    [500, 510, float("nan"), True, "430", -1],
)
def test_tightening_and_invalid_floors_fail_atomically(tmp_path: Path, threshold: object) -> None:
    fields = _fields()
    with pytest.raises(AmendmentProposalError):
        build_amendment_proposal(
            fields,
            {
                "reason": "requested change",
                "criteria": [
                    {
                        "criterion": "synthesis_ok_synth_a",
                        "thresholds": {"fmax_mhz_min": threshold},
                    }
                ],
            },
            tmp_path,
        )
    assert fields == _fields()


def test_scope_additions_are_explicit_and_protected_paths_rejected(tmp_path: Path) -> None:
    subprocess.run(["git", "init", "-b", "main", str(tmp_path)], check=True, capture_output=True)
    (tmp_path / "rtl").mkdir()
    (tmp_path / "rtl/extra.sv").write_text("module extra; endmodule\n")
    fields = _fields()
    proposal = build_amendment_proposal(
        fields,
        {
            "reason": "Need one additional source file",
            "scope_add": ["rtl/extra.sv", "rtl/new.sv [new]"],
        },
        tmp_path,
    )
    assert proposal.fields["scope"] == ["rtl/design.sv", "rtl/extra.sv", "rtl/new.sv [new]"]
    for name in ("../outside.sv", "build/core.core", "rtl/*.sv", "rtl/extra.sv [new]"):
        with pytest.raises(AmendmentProposalError):
            build_amendment_proposal(fields, {"reason": "bad", "scope_add": [name]}, tmp_path)


@pytest.mark.parametrize(
    ("key", "value", "param", "relaxed", "tightened"),
    [
        ("synthesis_ok", {"targets": ["synth"], "area_um2_max": 100}, "area_um2_max", 120, 80),
        (
            "synthesis_ok",
            {"targets": ["synth"], "area_increase_at_most": "10%"},
            "area_increase_at_most",
            "12%",
            "8%",
        ),
        (
            "synthesis_ok",
            {"targets": ["synth"], "area_reduce_at_least": "10%"},
            "area_reduce_at_least",
            "8%",
            "12%",
        ),
        (
            "cycle_count",
            [{"target": "sim", "test": "bench", "cycle_count_max": 100}],
            "cycle_count_max",
            120,
            80,
        ),
        (
            "cycle_count",
            [{"target": "sim", "test": "bench", "cycle_count_reduce_at_most_cycles": 10}],
            "cycle_count_reduce_at_most_cycles",
            15,
            5,
        ),
    ],
)
def test_declared_threshold_directions(
    tmp_path: Path, key: str, value: object, param: str, relaxed: object, tightened: object
) -> None:
    fields = {"scope": ["rtl/design.sv"], "criteria": {"mandatory": {key: value}}}
    name = next(iter(CriteriaTemplate.from_yaml(fields["criteria"]).expand(["default"])))
    request = {
        "reason": "revised requirement",
        "criteria": [{"criterion": name, "thresholds": {param: relaxed}}],
    }
    assert build_amendment_proposal(fields, request, tmp_path).changes[0].thresholds[param] == (
        value[0][param] if isinstance(value, list) else value[param],
        relaxed,
    )
    request["criteria"][0]["thresholds"][param] = tightened
    with pytest.raises(AmendmentProposalError):
        build_amendment_proposal(fields, request, tmp_path)
