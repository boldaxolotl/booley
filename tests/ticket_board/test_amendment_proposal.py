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
            "actor": "QA Human",
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
            "actor": "QA Human",
            "reason": "Human accepted residual review risk",
            "criteria": [{"criterion": "review_rtl_bugs_clean", "make_optional": True}],
        },
        tmp_path,
    )
    assert proposal.fields["criteria"] == {"mandatory": {}, "optional": {"review_rtl_bugs": True}}


@pytest.mark.parametrize("actor", [None, "", "Alice\nBob"])
def test_amendment_requires_one_human_actor(tmp_path: Path, actor: str | None) -> None:
    fields = {"scope": ["README.md"], "criteria": {"mandatory": {"review_rtl_bugs": True}}}
    request = {
        "reason": "Approve optional review",
        "criteria": [{"criterion": "review_rtl_bugs_clean", "make_optional": True}],
    }
    if actor is not None:
        request["actor"] = actor
    with pytest.raises(AmendmentProposalError, match=r"actor|missing"):
        build_amendment_proposal(fields, request, tmp_path)


@pytest.mark.parametrize(
    ("update", "message"),
    [
        ({"unexpected": True}, "unknown fields"),
        ({"feedback": 12}, "feedback must be a string"),
        ({"reason": "  "}, "reason must be nonblank"),
        ({"criteria": {}}, "must be lists"),
        ({"scope_add": "README.md"}, "must be lists"),
        ({}, "no acceptance change"),
    ],
)
def test_amendment_request_rejects_malformed_or_empty_inputs(
    tmp_path: Path, update: dict, message: str
) -> None:
    fields = {"scope": ["README.md"], "criteria": {"mandatory": {"review_rtl_bugs": True}}}
    request = {"actor": "QA Human", "reason": "Review change"}
    request.update(update)
    with pytest.raises(AmendmentProposalError, match=message):
        build_amendment_proposal(fields, request, tmp_path)


@pytest.mark.parametrize(
    ("edit", "message"),
    [
        ({"criterion": "review_rtl_bugs_clean", "remove": True}, "unknown fields"),
        ({"criterion": "missing", "make_optional": True}, "unknown or ambiguous"),
        ({"criterion": "review_rtl_bugs_clean", "make_optional": 1}, "must be boolean"),
        ({"criterion": "review_rtl_bugs_clean", "thresholds": []}, "must be a mapping"),
        ({"criterion": "review_rtl_bugs_clean"}, "has no change"),
    ],
)
def test_amendment_rejects_ambiguous_or_unsupported_criterion_edits(
    tmp_path: Path, edit: dict, message: str
) -> None:
    fields = {"scope": ["README.md"], "criteria": {"mandatory": {"review_rtl_bugs": True}}}
    request = {"actor": "QA Human", "reason": "Review change", "criteria": [edit]}
    with pytest.raises(AmendmentProposalError, match=message):
        build_amendment_proposal(fields, request, tmp_path)


def test_amendment_rejects_duplicate_criterion_edits(tmp_path: Path) -> None:
    fields = {"scope": ["README.md"], "criteria": {"mandatory": {"review_rtl_bugs": True}}}
    edit = {"criterion": "review_rtl_bugs_clean", "make_optional": True}
    request = {"actor": "QA Human", "reason": "Review change", "criteria": [edit, edit]}
    with pytest.raises(AmendmentProposalError, match="unique"):
        build_amendment_proposal(fields, request, tmp_path)


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
                "actor": "QA Human",
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
            "actor": "QA Human",
            "reason": "Need one additional source file",
            "scope_add": ["rtl/extra.sv", "rtl/new.sv [new]"],
        },
        tmp_path,
    )
    assert proposal.fields["scope"] == ["rtl/design.sv", "rtl/extra.sv", "rtl/new.sv [new]"]
    with pytest.raises(AmendmentProposalError, match="already exists"):
        build_amendment_proposal(
            proposal.fields,
            {"actor": "QA Human", "reason": "duplicate", "scope_add": ["rtl/extra.sv"]},
            tmp_path,
        )
    for name in ("../outside.sv", "build/core.core", "rtl/*.sv", "rtl/extra.sv [new]"):
        with pytest.raises(AmendmentProposalError):
            build_amendment_proposal(
                fields, {"actor": "QA Human", "reason": "bad", "scope_add": [name]}, tmp_path
            )


def test_amendment_rejects_malformed_published_scope(tmp_path: Path) -> None:
    fields = {"scope": "README.md", "criteria": {"mandatory": {"review_rtl_bugs": True}}}
    with pytest.raises(AmendmentProposalError, match="published Scope must be a list"):
        build_amendment_proposal(
            fields,
            {
                "actor": "QA Human",
                "reason": "Review change",
                "criteria": [{"criterion": "review_rtl_bugs_clean", "make_optional": True}],
            },
            tmp_path,
        )


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
        "actor": "QA Human",
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


def test_coverage_relaxation_changes_only_named_metric(tmp_path: Path) -> None:
    fields = {
        "scope": ["rtl/design.sv"],
        "criteria": {
            "mandatory": {
                "coverage": [
                    {
                        "targets": ["sim_a", "sim_b"],
                        "metrics": {"line": {"min_pct": 90}},
                        "tests": "all",
                    }
                ]
            }
        },
    }
    proposal = build_amendment_proposal(
        fields,
        {
            "actor": "QA Human",
            "reason": "Accept lower line coverage",
            "criteria": [{"criterion": "coverage_sim_a", "thresholds": {"line.min_pct": 85}}],
        },
        tmp_path,
    )
    records = proposal.fields["criteria"]["mandatory"]["coverage"]
    assert [record["metrics"]["line"]["min_pct"] for record in records] == [85, 90]
    assert [record["targets"] for record in records] == [["sim_a"], ["sim_b"]]


def test_coverage_relaxation_rejects_unknown_metric_and_tightening(tmp_path: Path) -> None:
    fields = {
        "scope": ["rtl/design.sv"],
        "criteria": {
            "mandatory": {
                "coverage": [
                    {
                        "targets": ["sim"],
                        "metrics": {"line": {"min_pct": 90}},
                        "tests": "all",
                    }
                ]
            }
        },
    }
    for thresholds in ({"branch.min_pct": 80}, {"line.min_pct": 95}, {"line": 80}):
        with pytest.raises(AmendmentProposalError):
            build_amendment_proposal(
                fields,
                {
                    "actor": "QA Human",
                    "reason": "Review threshold",
                    "criteria": [{"criterion": "coverage_sim", "thresholds": thresholds}],
                },
                tmp_path,
            )


def test_mutation_threshold_can_relax_but_not_change_total(tmp_path: Path) -> None:
    fields = {
        "scope": ["rtl/design.sv"],
        "criteria": {
            "mandatory": {
                "mutation_score": [
                    {
                        "target": "sim",
                        "scope": ["rtl/design.sv"],
                        "min_detected": 8,
                        "total": 10,
                    }
                ]
            }
        },
    }
    base = {"actor": "QA Human", "reason": "Accept fewer detected mutations"}
    proposal = build_amendment_proposal(
        fields,
        {
            **base,
            "criteria": [
                {
                    "criterion": "mutation_score_sim",
                    "thresholds": {"min_detected": 7},
                }
            ],
        },
        tmp_path,
    )
    assert proposal.fields["criteria"]["mandatory"]["mutation_score"][0]["min_detected"] == 7
    with pytest.raises(AmendmentProposalError, match="only supports"):
        build_amendment_proposal(
            fields,
            {
                **base,
                "criteria": [{"criterion": "mutation_score_sim", "thresholds": {"total": 11}}],
            },
            tmp_path,
        )
