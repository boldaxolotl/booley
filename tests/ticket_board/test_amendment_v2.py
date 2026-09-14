"""Human amendments edit v2 syntax through converted Criterion identities."""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest
import yaml

from booley.ticket_board.acceptance_basis import (
    AcceptanceBasisError,
    authored_ticket_record_from_spec,
)
from booley.ticket_board.amendment_proposal import AmendmentProposalError
from booley.ticket_board.amendment_v2 import build_v2_amendment_proposal
from booley.ticket_board.ticket_document import (
    TicketAuthoringView,
    TicketConversionContext,
    TicketSpec,
    convert_ticket_document,
)


def _spec(criteria: str) -> tuple[TicketSpec, TicketAuthoringView]:
    text = (
        "---\nsummary: Amend thresholds\ntype: verification\nbranch: main\n"
        "scope: [README.md]\non_success: [merge]\nCRITERIA_MANDATORY:\n"
        f"{criteria}---\n\n## Description\n\nCheck the result.\n"
    )
    view = TicketAuthoringView(
        resolve_target=lambda selector, _flow: selector,
        tests_for_target=lambda _target: ("smoke",),
    )
    converted = convert_ticket_document(text, TicketConversionContext("draft", lambda _: view))
    assert converted.document is not None, converted.diagnostics
    return converted.document.spec, view


def _request(
    criterion: str, *, thresholds: dict | None = None, make_optional: bool = False
) -> dict:
    return {
        "actor": "QA Human",
        "reason": "Accept the measured result",
        "criteria": [
            {
                "criterion": criterion,
                "thresholds": thresholds or {},
                "make_optional": make_optional,
            }
        ],
    }


def test_v2_amendment_relaxes_one_synth_metric_and_preserves_sibling(tmp_path: Path) -> None:
    spec, view = _spec(
        "  SYNTH:\n"
        "    synth_a: {area_um2_max: 100, fmax_mhz_min: 500}\n"
        "    synth_b: {fmax_mhz_min: 500}\n"
    )
    criterion = next(
        row.identity
        for row in spec.criteria
        if row.target == "synth_a" and row.parameter == "fmax_mhz_min"
    )

    proposal = build_v2_amendment_proposal(
        spec, _request(criterion, thresholds={"fmax_mhz_min": 430}), tmp_path, view
    )

    synth = proposal.fields["CRITERIA_MANDATORY"]["SYNTH"]
    assert synth["synth_a"] == {"area_um2_max": 100, "fmax_mhz_min": 430}
    assert synth["synth_b"] == {"fmax_mhz_min": 500}
    assert proposal.changes[0].thresholds == {"fmax_mhz_min": (500, 430)}

    with pytest.raises(AmendmentProposalError, match="must relax"):
        build_v2_amendment_proposal(
            spec, _request(criterion, thresholds={"fmax_mhz_min": 510}), tmp_path, view
        )


def test_v2_amendment_moves_last_review_requirement_to_optional(tmp_path: Path) -> None:
    spec, view = _spec("  REVIEW: {rtl: {bugs: done}}\n")
    criterion = spec.criteria[0].identity

    proposal = build_v2_amendment_proposal(
        spec, _request(criterion, make_optional=True), tmp_path, view
    )

    assert proposal.fields["CRITERIA_MANDATORY"] == {}
    assert proposal.fields["CRITERIA_OPTIONAL"] == {"REVIEW": {"rtl": {"bugs": "done"}}}
    text = "---\n" + yaml.safe_dump(proposal.fields) + "---\n" + spec.body
    converted = convert_ticket_document(text, TicketConversionContext("draft", lambda _: view))
    assert converted.document is not None, converted.diagnostics
    with pytest.raises(AcceptanceBasisError, match="committed human amendment"):
        authored_ticket_record_from_spec(converted.document.spec, ())


def test_v2_amendment_moves_scalar_synth_pass_to_optional(tmp_path: Path) -> None:
    spec, view = _spec("  SYNTH: {synth_a: pass}\n")

    proposal = build_v2_amendment_proposal(
        spec, _request(spec.criteria[0].identity, make_optional=True), tmp_path, view
    )

    assert proposal.fields["CRITERIA_MANDATORY"] == {}
    assert proposal.fields["CRITERIA_OPTIONAL"] == {"SYNTH": {"synth_a": "pass"}}


def test_v2_amendment_relaxes_one_coverage_metric(tmp_path: Path) -> None:
    spec, view = _spec(
        "  COVERAGE: {sim_a: {tests: all, metrics: {line: {min_pct: 90}, branch: {min_pct: 80}}}}\n"
    )
    criterion = next(row.identity for row in spec.criteria if row.parameter == "line")

    proposal = build_v2_amendment_proposal(
        spec, _request(criterion, thresholds={"line.min_pct": 85}), tmp_path, view
    )

    metrics = proposal.fields["CRITERIA_MANDATORY"]["COVERAGE"]["sim_a"]["metrics"]
    assert metrics == {"line": {"min_pct": 85}, "branch": {"min_pct": 80}}


@pytest.mark.parametrize(
    ("criteria", "parameter", "relaxed", "tightened"),
    [
        ("  SYNTH: {synth_a: {area_um2_max: 100}}\n", "area_um2_max", 120, 80),
        ("  CYCLE_COUNT: {sim_a: {smoke: {cycle_count_max: 100}}}\n", "cycle_count_max", 120, 80),
        (
            "  MUTATION: {sim_a: {scope: [README.md], min_detected: 8, total: 10}}\n",
            "min_detected",
            7,
            9,
        ),
    ],
)
def test_v2_amendment_enforces_threshold_direction(
    tmp_path: Path, criteria: str, parameter: str, relaxed: int, tightened: int
) -> None:
    spec, view = _spec(criteria)
    [row] = spec.criteria
    proposal = build_v2_amendment_proposal(
        spec, _request(row.identity, thresholds={parameter: relaxed}), tmp_path, view
    )
    assert proposal.changes[0].thresholds[parameter][1] == relaxed
    with pytest.raises(AmendmentProposalError, match="must relax"):
        build_v2_amendment_proposal(
            spec, _request(row.identity, thresholds={parameter: tightened}), tmp_path, view
        )


@pytest.mark.parametrize("actor", (None, "", "Alice\nBob"))
def test_v2_amendment_requires_one_human_actor(tmp_path: Path, actor: str | None) -> None:
    spec, view = _spec("  REVIEW: {rtl: {bugs: done}}\n")
    request = _request(spec.criteria[0].identity, make_optional=True)
    if actor is None:
        request.pop("actor")
    else:
        request["actor"] = actor
    with pytest.raises(AmendmentProposalError):
        build_v2_amendment_proposal(spec, request, tmp_path, view)


def test_v2_amendment_rejects_duplicate_or_unknown_criterion(tmp_path: Path) -> None:
    spec, view = _spec("  REVIEW: {rtl: {bugs: done}}\n")
    edit = {"criterion": spec.criteria[0].identity, "make_optional": True}
    request = {"actor": "QA Human", "reason": "Review change", "criteria": [edit, edit]}
    with pytest.raises(AmendmentProposalError, match="unique"):
        build_v2_amendment_proposal(spec, request, tmp_path, view)
    request["criteria"] = [{"criterion": "missing", "make_optional": True}]
    with pytest.raises(AmendmentProposalError, match="unknown Criterion"):
        build_v2_amendment_proposal(spec, request, tmp_path, view)


def test_v2_amendment_scope_addition_preserves_existing_scope(tmp_path: Path) -> None:
    subprocess.run(["git", "init", "-q", "-b", "main", str(tmp_path)], check=True)
    (tmp_path / "EXTRA.md").write_text("extra\n")
    spec, view = _spec("  REVIEW: {rtl: {bugs: done}}\n")
    request = {
        "actor": "QA Human",
        "reason": "Add an existing source",
        "scope_add": ["EXTRA.md"],
    }
    proposal = build_v2_amendment_proposal(spec, request, tmp_path, view)
    assert proposal.fields["scope"] == ["README.md", "EXTRA.md"]
    request["scope_add"] = ["../outside.md"]
    with pytest.raises(AmendmentProposalError, match="unsafe Scope"):
        build_v2_amendment_proposal(spec, request, tmp_path, view)


@pytest.mark.parametrize(
    ("criteria", "parameter", "mandatory_fragment", "optional_fragment"),
    [
        (
            "  SYNTH: {synth_a: {area_um2_max: 100, fmax_mhz_min: 500}}\n",
            "area_um2_max",
            {"SYNTH": {"synth_a": {"fmax_mhz_min": 500}}},
            {"SYNTH": {"synth_a": {"area_um2_max": 100}}},
        ),
        (
            "  FPGA: {fpga_a: {lut_count_max: 100, fmax_mhz_min: 300}}\n",
            "lut_count_max",
            {"FPGA": {"fpga_a": {"fmax_mhz_min": 300}}},
            {"FPGA": {"fpga_a": {"lut_count_max": 100}}},
        ),
        (
            "  CYCLE_COUNT: {sim_a: {smoke: {cycle_count_max: 100, cycle_count_min: 20}}}\n",
            "cycle_count_max",
            {"CYCLE_COUNT": {"sim_a": {"smoke": {"cycle_count_min": 20}}}},
            {"CYCLE_COUNT": {"sim_a": {"smoke": {"cycle_count_max": 100}}}},
        ),
        (
            "  COVERAGE: {sim_a: {tests: all, metrics: {line: {min_pct: 90}, branch: {min_pct: 80}}}}\n",
            "line",
            {"COVERAGE": {"sim_a": {"tests": "all", "metrics": {"branch": {"min_pct": 80}}}}},
            {"COVERAGE": {"sim_a": {"tests": "all", "metrics": {"line": {"min_pct": 90}}}}},
        ),
        (
            "  SIM: {sim_a: {smoke: pass, all: pass}}\n",
            "smoke",
            {"SIM": {"sim_a": {"all": "pass"}}},
            {"SIM": {"sim_a": {"smoke": "pass"}}},
        ),
        (
            "  REVIEW: {rtl: {bugs: [done, clean]}}\n",
            "done",
            {"REVIEW": {"rtl": {"bugs": "clean"}}},
            {"REVIEW": {"rtl": {"bugs": "done"}}},
        ),
    ],
)
def test_v2_amendment_moves_one_atomic_requirement_without_losing_siblings(
    tmp_path: Path,
    criteria: str,
    parameter: str,
    mandatory_fragment: dict,
    optional_fragment: dict,
) -> None:
    spec, view = _spec(criteria)
    row = next(item for item in spec.criteria if parameter in (item.parameter, item.test))
    proposal = build_v2_amendment_proposal(
        spec, _request(row.identity, make_optional=True), tmp_path, view
    )
    assert proposal.fields["CRITERIA_MANDATORY"] == mandatory_fragment
    assert proposal.fields["CRITERIA_OPTIONAL"] == optional_fragment


@pytest.mark.parametrize(
    "criteria",
    [
        "  LINT: {lint_a: clean}\n",
        "  ELAB: {elab_a: pass}\n",
        "  MUTATION: {sim_a: {scope: [README.md], min_detected: 8, total: 10}}\n",
        "  SIM: {sim_a: {smoke: pass}}\n",
        "  CYCLE_COUNT: {sim_a: {smoke: {cycle_count_max: 100}}}\n",
        "  COVERAGE: {sim_a: {tests: all, metrics: {line: {min_pct: 90}}}}\n",
    ],
)
def test_v2_amendment_can_move_last_requirement_to_optional(tmp_path: Path, criteria: str) -> None:
    spec, view = _spec(criteria)
    proposal = build_v2_amendment_proposal(
        spec, _request(spec.criteria[0].identity, make_optional=True), tmp_path, view
    )
    assert proposal.fields["CRITERIA_MANDATORY"] == {}
    assert proposal.fields["CRITERIA_OPTIONAL"]


@pytest.mark.parametrize(
    ("change", "message"),
    [
        ({"actor": ""}, "actor"),
        ({"reason": ""}, "reason"),
        ({"feedback": 7}, "feedback"),
        ({"criteria": "bad"}, "must be lists"),
        ({"scope_add": "bad"}, "must be lists"),
        ({"unexpected": True}, "unknown fields"),
    ],
)
def test_v2_amendment_rejects_invalid_request_envelope(
    tmp_path: Path, change: dict, message: str
) -> None:
    spec, view = _spec("  REVIEW: {rtl: {bugs: done}}\n")
    request = _request(spec.criteria[0].identity, make_optional=True)
    request.update(change)
    with pytest.raises(AmendmentProposalError, match=message):
        build_v2_amendment_proposal(spec, request, tmp_path, view)


@pytest.mark.parametrize(
    ("change", "message"),
    [
        ({"unexpected": True}, "unknown fields"),
        ({"make_optional": "yes"}, "must be boolean"),
        ({"thresholds": []}, "must be a mapping"),
        ({"make_optional": False}, "has no change"),
        ({"thresholds": {"other": 5}}, "no relaxable threshold"),
    ],
)
def test_v2_amendment_rejects_invalid_atomic_edit(
    tmp_path: Path, change: dict, message: str
) -> None:
    spec, view = _spec("  REVIEW: {rtl: {bugs: done}}\n")
    request = _request(spec.criteria[0].identity)
    request["criteria"][0].update(change)
    with pytest.raises(AmendmentProposalError, match=message):
        build_v2_amendment_proposal(spec, request, tmp_path, view)
