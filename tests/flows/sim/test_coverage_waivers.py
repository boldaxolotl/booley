from __future__ import annotations

from dataclasses import replace
from fractions import Fraction
from pathlib import Path
from types import MappingProxyType
from typing import Literal, cast

import pytest

from booley.flows.sim.coverage_campaign import (
    CoverageCampaign,
    CoverageCapability,
    CoverageCollector,
    CoveragePoint,
    CoveragePointIdentity,
    CoverageRollup,
    CoverageRun,
    CoverageTarget,
    DurableTargetIdentity,
)
from booley.flows.sim.coverage_policy import (
    CoverageCriterion,
    CoverageThreshold,
    evaluate_coverage_campaign,
)
from booley.flows.sim.coverage_waivers import (
    CoverageRepositoryRoots,
    CoverageWaiverConfig,
    CoverageWaiverValidationError,
    load_approved_waiver_set,
)
from tests.conftest import symlink_or_skip

_TARGET = DurableTargetIdentity("acme:demo:counter:1.0#sim_counter")
_POINT_ID = (
    "cp1:eyJjb2xsZWN0b3IiOnsibmF0aXZlX2tleSI6IjEwOmJhc2ljLWJsb2NrLTAiLCJyZWNvcm"
    "RfdHlwZSI6InZfbGluZSJ9LCJoaWVyYXJjaHkiOiJUT1AuY291bnRlciIsImxvY2F0aW9uIjp7"
    "ImVuZCI6eyJjb2x1bW4iOjI4LCJsaW5lIjoxMH0sInNvdXJjZSI6InJ0bC9jb3VudGVyLnN2Ii"
    "wic3RhcnQiOnsiY29sdW1uIjozLCJsaW5lIjoxMH19LCJtZXRyaWMiOiJsaW5lIiwic3ViamVj"
    "dCI6eyJiYXNpY19ibG9jayI6MH19"
)
_SOURCE_SHA256 = "sha256:bc3ff1435b5f77923d2dc27891f85bce29bacdfd0fd9107dea0066141ecb736e"
_OTHER_POINT_ID = (
    "cp1:eyJjb2xsZWN0b3IiOnsibmF0aXZlX2tleSI6IjEyOmJyYW5jaC10cnVlIiwicmVjb3JkX3"
    "R5cGUiOiJ2X2JyYW5jaCJ9LCJoaWVyYXJjaHkiOiJUT1AuY291bnRlciIsImxvY2F0aW9uIjp7"
    "ImVuZCI6eyJjb2x1bW4iOjI4LCJsaW5lIjoxMn0sInNvdXJjZSI6InJ0bC9jb3VudGVyLnN2Ii"
    "wic3RhcnQiOnsiY29sdW1uIjozLCJsaW5lIjoxMn19LCJtZXRyaWMiOiJicmFuY2giLCJzdWJq"
    "ZWN0Ijp7Im91dGNvbWUiOiJ0cnVlIn19"
)
_SECOND_SOURCE_POINT_ID = (
    "cp1:eyJjb2xsZWN0b3IiOnsibmF0aXZlX2tleSI6IjEwOmJhc2ljLWJsb2NrLTAiLCJyZWNvcm"
    "RfdHlwZSI6InZfbGluZSJ9LCJoaWVyYXJjaHkiOiJUT1AuY291bnRlciIsImxvY2F0aW9uIjp7"
    "ImVuZCI6eyJjb2x1bW4iOjI4LCJsaW5lIjoxMH0sInNvdXJjZSI6InRiL2NvdW50ZXJfdGIuc3"
    "YiLCJzdGFydCI6eyJjb2x1bW4iOjMsImxpbmUiOjEwfX0sIm1ldHJpYyI6ImxpbmUiLCJzdWJq"
    "ZWN0Ijp7ImJhc2ljX2Jsb2NrIjowfX0"
)


def _roots(tmp_path: Path) -> CoverageRepositoryRoots:
    rtl = tmp_path / "rtl-repository"
    project_data = tmp_path / "project-data-repository"
    rtl.mkdir(parents=True)
    project_data.mkdir(parents=True)
    return CoverageRepositoryRoots(rtl_repository=rtl, project_data_repository=project_data)


def _write_valid_approval(roots: CoverageRepositoryRoots) -> Path:
    source = roots.rtl_repository / "rtl" / "counter.sv"
    source.parent.mkdir()
    source.write_bytes(b"module counter; endmodule\n")
    waiver_file = roots.project_data_repository / "coverage-waivers" / "rtl" / "counter.sv.toml"
    waiver_file.parent.mkdir(parents=True)
    waiver_file.write_text(
        f'''schema = "booley.coverage-waivers/v1"
source = "rtl/counter.sv"
source_sha256 = "{_SOURCE_SHA256}"

[[approval]]
id = "counter-basic-block"
target = "{_TARGET}"
point_id = "{_POINT_ID}"
reason = "excluded"
justification = "Reserved behavior is intentionally excluded."
approved_by = "verification-owner@example.test"
approved_at = "2026-08-31T09:00:00Z"
approval_ref = "review:CR-1042"
''',
        encoding="utf-8",
    )
    return waiver_file


def _write_second_approval(roots: CoverageRepositoryRoots) -> Path:
    source = roots.rtl_repository / "tb" / "counter_tb.sv"
    source.parent.mkdir(exist_ok=True)
    source.write_bytes(b"module counter_tb; endmodule\n")
    waiver_file = roots.project_data_repository / "coverage-waivers" / "tb" / "counter_tb.sv.toml"
    waiver_file.parent.mkdir(parents=True, exist_ok=True)
    waiver_file.write_text(
        f'''schema = "booley.coverage-waivers/v1"
source = "tb/counter_tb.sv"
source_sha256 = "sha256:d264138a111ea9255474244eb7e1068eafd5b328e3c6ba7f8c127853d5045231"

[[approval]]
id = "counter-testbench-block"
target = "{_TARGET}"
point_id = "{_SECOND_SOURCE_POINT_ID}"
reason = "excluded"
justification = "Reviewed second source."
approved_by = "verification-owner@example.test"
approved_at = "2026-08-31T09:00:00Z"
approval_ref = "review:CR-1043"
''',
        encoding="utf-8",
    )
    return waiver_file


def _campaign() -> CoverageCampaign:
    points = (
        CoveragePoint(
            id=_POINT_ID,
            identity=CoveragePointIdentity(
                metric="line",
                location=MappingProxyType({"source": "rtl/counter.sv"}),
                hierarchy="TOP.counter",
                subject=MappingProxyType({"basic_block": 0}),
                collector=MappingProxyType({"record_type": "v_line"}),
            ),
            hits_by_run=MappingProxyType({}),
            disposition=MappingProxyType({"kind": "eligible"}),
        ),
        CoveragePoint(
            id="covered-point",
            identity=CoveragePointIdentity(
                metric="line",
                location=MappingProxyType({"source": "rtl/counter.sv"}),
                hierarchy="TOP.counter",
                subject=MappingProxyType({"basic_block": 1}),
                collector=MappingProxyType({"record_type": "v_line"}),
            ),
            hits_by_run=MappingProxyType({"run:smoke": 1}),
            disposition=MappingProxyType({"kind": "eligible"}),
        ),
    )
    return CoverageCampaign(
        schema="booley.coverage-campaign/v1",
        campaign_id="campaign:sim_counter:12",
        invocation=MappingProxyType({"id": 12}),
        target=CoverageTarget(identity=str(_TARGET), selector="sim_counter"),
        collector=CoverageCollector(
            kind="verilator",
            version=MappingProxyType({}),
            native_format=MappingProxyType({"compatibility": "compatible"}),
            capabilities=(
                CoverageCapability(
                    record_class="line",
                    status="reported",
                    attributes=MappingProxyType(
                        {"collection": "supported", "scoring": "scored_v1"}
                    ),
                ),
            ),
        ),
        build=MappingProxyType({}),
        coverage_window=MappingProxyType({}),
        fingerprints=MappingProxyType({}),
        source_closure=MappingProxyType({}),
        declared_tests=("smoke",),
        selected_tests=("smoke",),
        runs=(
            CoverageRun(
                id="run:smoke",
                test="smoke",
                simulation_verdict="pass",
                collection="included",
                raw_artifact=None,
                attributes=MappingProxyType({}),
            ),
        ),
        artifacts=(),
        normalization=MappingProxyType({"status": "complete"}),
        points=points,
        rollups=(CoverageRollup("line", "line semantics", 2, 2, 1, 0, 50.0),),
        collection=MappingProxyType({"status": "complete"}),
        findings=(),
        evaluation=MappingProxyType({"status": "not_requested"}),
    )


def _criterion() -> CoverageCriterion:
    return CoverageCriterion(
        target=_TARGET,
        thresholds=(CoverageThreshold("line", Fraction(100)),),
        tests=None,
    )


def test_omitted_configuration_returns_explicit_immutable_empty_set(tmp_path: Path) -> None:
    roots = CoverageRepositoryRoots(
        rtl_repository=tmp_path / "missing-rtl",
        project_data_repository=tmp_path / "missing-project-data",
    )

    approved = load_approved_waiver_set(None, roots, known_targets=())

    assert approved.configuration == {"enabled": False}
    assert isinstance(approved.configuration, MappingProxyType)
    assert (
        approved.digest
        == "sha256:ae024874fd9c68b49bff42973ac809b623666de39afb78cb51d6a01e901ca5f3"
    )
    assert approved.waivers == ()


def test_valid_source_mirrored_file_loads_exact_immutable_approval(tmp_path: Path) -> None:
    roots = _roots(tmp_path)
    waiver_file = _write_valid_approval(roots)

    approved = load_approved_waiver_set(
        CoverageWaiverConfig(
            anchor="project_data_repository",
            directory="coverage-waivers",
        ),
        roots,
        known_targets=(_TARGET,),
    )

    assert approved.configuration == {
        "anchor": "project_data_repository",
        "directory": "coverage-waivers",
    }
    assert approved.digest.startswith("sha256:")
    assert len(approved.digest) == 71
    assert len(approved.waivers) == 1
    waiver = approved.waivers[0]
    assert waiver.target == _TARGET
    assert waiver.point_id == _POINT_ID
    assert waiver.reason == "excluded"
    assert waiver.waiver_id == "counter-basic-block"
    assert waiver.waiver_file == "rtl/counter.sv.toml"
    assert waiver.waiver_fingerprint != _SOURCE_SHA256
    assert waiver.provenance == {
        "justification": "Reserved behavior is intentionally excluded.",
        "approved_by": "verification-owner@example.test",
        "approved_at": "2026-08-31T09:00:00Z",
        "approval_ref": "review:CR-1042",
    }
    assert waiver_file.is_file()


def test_invalid_anchor_and_unsafe_directory_are_aggregated_before_access(tmp_path: Path) -> None:
    roots = CoverageRepositoryRoots(
        rtl_repository=tmp_path / "missing-rtl",
        project_data_repository=tmp_path / "missing-project-data",
    )
    config = CoverageWaiverConfig(
        anchor=cast(Literal["rtl_repository", "project_data_repository"], "elsewhere"),
        directory="../coverage-waivers",
    )

    with pytest.raises(CoverageWaiverValidationError) as raised:
        load_approved_waiver_set(config, roots, known_targets=())

    assert [(item.code, item.pointer) for item in raised.value.findings] == [
        ("COV_WAIVER_ANCHOR_INVALID", "/anchor"),
        ("COV_WAIVER_DIRECTORY_UNSAFE", "/directory"),
    ]


def test_configured_directory_symlink_is_rejected_without_following_it(tmp_path: Path) -> None:
    roots = _roots(tmp_path)
    outside = tmp_path / "outside"
    outside.mkdir()
    symlink_or_skip(roots.project_data_repository / "coverage-waivers", outside)

    with pytest.raises(CoverageWaiverValidationError) as raised:
        load_approved_waiver_set(
            CoverageWaiverConfig("project_data_repository", "coverage-waivers"),
            roots,
            known_targets=(),
        )

    assert [(item.code, item.pointer) for item in raised.value.findings] == [
        ("COV_WAIVER_DIRECTORY_SYMLINK", "/directory")
    ]


@pytest.mark.parametrize(
    ("link_kind", "expected_code"),
    [
        ("directory", "COV_WAIVER_DIRECTORY_SYMLINK"),
        ("file", "COV_WAIVER_FILE_SYMLINK"),
    ],
)
def test_nested_symlinks_are_rejected_without_loading_targets(
    tmp_path: Path,
    link_kind: str,
    expected_code: str,
) -> None:
    roots = _roots(tmp_path)
    waiver_directory = roots.project_data_repository / "coverage-waivers"
    waiver_directory.mkdir()
    outside = tmp_path / "outside"
    if link_kind == "directory":
        outside.mkdir()
        symlink_or_skip(waiver_directory / "rtl", outside, target_is_directory=True)
    else:
        outside.write_text('schema = "waiver_candidate"\n', encoding="utf-8")
        (waiver_directory / "rtl").mkdir()
        symlink_or_skip(waiver_directory / "rtl" / "counter.sv.toml", outside)

    with pytest.raises(CoverageWaiverValidationError) as raised:
        load_approved_waiver_set(
            CoverageWaiverConfig("project_data_repository", "coverage-waivers"),
            roots,
            known_targets=(),
        )

    assert [item.code for item in raised.value.findings] == [expected_code]


def test_malformed_and_candidate_files_are_aggregated_transactionally(tmp_path: Path) -> None:
    roots = _roots(tmp_path)
    directory = roots.project_data_repository / "coverage-waivers"
    directory.mkdir()
    (directory / "candidate.toml").write_text(
        'schema = "booley.coverage-waiver-candidate/v1"\n[[waiver_candidate]]\nid = "x"\n',
        encoding="utf-8",
    )
    (directory / "malformed.toml").write_text("schema = [\n", encoding="utf-8")

    with pytest.raises(CoverageWaiverValidationError) as raised:
        load_approved_waiver_set(
            CoverageWaiverConfig("project_data_repository", "coverage-waivers"),
            roots,
            known_targets=(),
        )

    assert [(item.code, item.pointer) for item in raised.value.findings] == [
        ("COV_WAIVER_CANDIDATE_FORBIDDEN", "/files/candidate.toml/schema"),
        ("COV_WAIVER_FILE_MALFORMED", "/files/malformed.toml"),
    ]


def test_mirrored_location_and_source_fingerprint_fail_together(tmp_path: Path) -> None:
    roots = _roots(tmp_path)
    waiver_file = _write_valid_approval(roots)
    misplaced = waiver_file.parents[1] / "copies" / "counter.sv.toml"
    misplaced.parent.mkdir()
    waiver_file.rename(misplaced)
    (roots.rtl_repository / "rtl" / "counter.sv").write_bytes(b"changed\n")

    with pytest.raises(CoverageWaiverValidationError) as raised:
        load_approved_waiver_set(
            CoverageWaiverConfig("project_data_repository", "coverage-waivers"),
            roots,
            known_targets=(_TARGET,),
        )

    assert [item.code for item in raised.value.findings] == [
        "COV_WAIVER_SOURCE_FILE_MISMATCH",
        "COV_WAIVER_SOURCE_STALE",
    ]


@pytest.mark.parametrize("link_kind", ["directory", "file"])
def test_rtl_source_symlink_is_rejected(tmp_path: Path, link_kind: str) -> None:
    roots = _roots(tmp_path)
    _write_valid_approval(roots)
    source = roots.rtl_repository / "rtl" / "counter.sv"
    source.unlink()
    outside = tmp_path / "outside-source"
    if link_kind == "directory":
        source.parent.rmdir()
        outside.mkdir()
        (outside / "counter.sv").write_bytes(b"module counter; endmodule\n")
        symlink_or_skip(source.parent, outside, target_is_directory=True)
    else:
        outside.write_bytes(b"module counter; endmodule\n")
        symlink_or_skip(source, outside)

    with pytest.raises(CoverageWaiverValidationError) as raised:
        load_approved_waiver_set(
            CoverageWaiverConfig("project_data_repository", "coverage-waivers"),
            roots,
            known_targets=(_TARGET,),
        )

    assert [item.code for item in raised.value.findings] == ["COV_WAIVER_SOURCE_SYMLINK"]


def test_unknown_target_inexact_binding_bad_timestamp_and_missing_proof_aggregate(
    tmp_path: Path,
) -> None:
    roots = _roots(tmp_path)
    waiver_file = _write_valid_approval(roots)
    document = waiver_file.read_text(encoding="utf-8")
    document = document.replace(str(_TARGET), "acme:demo:retired:1.0#sim_old")
    document = document.replace(_POINT_ID, "line:rtl/counter.sv:10")
    document = document.replace('reason = "excluded"', 'reason = "unreachable"')
    document = document.replace("2026-08-31T09:00:00Z", "tomorrow")
    waiver_file.write_text(document, encoding="utf-8")

    with pytest.raises(CoverageWaiverValidationError) as raised:
        load_approved_waiver_set(
            CoverageWaiverConfig("project_data_repository", "coverage-waivers"),
            roots,
            known_targets=(_TARGET,),
        )

    assert [item.code for item in raised.value.findings] == [
        "COV_WAIVER_TARGET_UNKNOWN",
        "COV_WAIVER_BINDING_NOT_EXACT",
        "COV_WAIVER_APPROVED_AT_INVALID",
        "COV_WAIVER_PROOF_REQUIRED",
    ]


def test_unreachable_proof_artifact_must_match_approved_digest(tmp_path: Path) -> None:
    roots = _roots(tmp_path)
    waiver_file = _write_valid_approval(roots)
    proof = roots.project_data_repository / "proofs" / "counter.sby"
    proof.parent.mkdir()
    proof.write_bytes(b"changed-proof\n")
    document = waiver_file.read_text(encoding="utf-8")
    document = document.replace('reason = "excluded"', 'reason = "unreachable"')
    document += """
[approval.proof]
kind = "formal"
reference = "proofs/counter.sby#cover_17"
sha256 = "sha256:69ad078dd3a1c4e5796b11fbbf4e8faca01fe6cf98ff5501698b322da1d13bd3"
"""
    waiver_file.write_text(document, encoding="utf-8")

    with pytest.raises(CoverageWaiverValidationError) as raised:
        load_approved_waiver_set(
            CoverageWaiverConfig("project_data_repository", "coverage-waivers"),
            roots,
            known_targets=(_TARGET,),
        )

    assert [item.code for item in raised.value.findings] == ["COV_WAIVER_PROOF_STALE"]


def test_valid_unreachable_proof_is_authenticated_and_retained(tmp_path: Path) -> None:
    roots = _roots(tmp_path)
    waiver_file = _write_valid_approval(roots)
    proof = roots.project_data_repository / "proofs" / "counter.sby"
    proof.parent.mkdir()
    proof.write_bytes(b"proof-result\n")
    document = waiver_file.read_text(encoding="utf-8")
    document = document.replace('reason = "excluded"', 'reason = "unreachable"')
    document += """
[approval.proof]
kind = "formal"
reference = "proofs/counter.sby#cover_17"
sha256 = "sha256:69ad078dd3a1c4e5796b11fbbf4e8faca01fe6cf98ff5501698b322da1d13bd3"
"""
    waiver_file.write_text(document, encoding="utf-8")

    approved = load_approved_waiver_set(
        CoverageWaiverConfig("project_data_repository", "coverage-waivers"),
        roots,
        known_targets=(_TARGET,),
    )

    assert approved.waivers[0].reason == "unreachable"
    assert approved.waivers[0].provenance["proof"] == {
        "kind": "formal",
        "reference": "proofs/counter.sby#cover_17",
        "sha256": "sha256:69ad078dd3a1c4e5796b11fbbf4e8faca01fe6cf98ff5501698b322da1d13bd3",
    }


def test_duplicate_ids_and_target_point_bindings_invalidate_the_whole_set(tmp_path: Path) -> None:
    roots = _roots(tmp_path)
    waiver_file = _write_valid_approval(roots)
    document = waiver_file.read_text(encoding="utf-8")
    approval = document[document.index("[[approval]]") :]
    waiver_file.write_text(f"{document}\n{approval}", encoding="utf-8")

    with pytest.raises(CoverageWaiverValidationError) as raised:
        load_approved_waiver_set(
            CoverageWaiverConfig("project_data_repository", "coverage-waivers"),
            roots,
            known_targets=(_TARGET,),
        )

    assert [item.code for item in raised.value.findings] == [
        "COV_WAIVER_ID_DUPLICATE",
        "COV_WAIVER_BINDING_DUPLICATE",
    ]


def test_duplicate_source_claim_is_reported_with_its_mirror_mismatch(tmp_path: Path) -> None:
    roots = _roots(tmp_path)
    waiver_file = _write_valid_approval(roots)
    duplicate = waiver_file.parents[1] / "copies" / "counter.sv.toml"
    duplicate.parent.mkdir()
    duplicate.write_bytes(waiver_file.read_bytes())

    with pytest.raises(CoverageWaiverValidationError) as raised:
        load_approved_waiver_set(
            CoverageWaiverConfig("project_data_repository", "coverage-waivers"),
            roots,
            known_targets=(_TARGET,),
        )

    assert [item.code for item in raised.value.findings] == [
        "COV_WAIVER_SOURCE_FILE_MISMATCH",
        "COV_WAIVER_SOURCE_DUPLICATE",
    ]


def test_one_stale_match_blocks_evaluation_and_applies_no_valid_sibling(tmp_path: Path) -> None:
    roots = _roots(tmp_path)
    waiver_file = _write_valid_approval(roots)
    document = waiver_file.read_text(encoding="utf-8")
    approval = document[document.index("[[approval]]") :]
    stale = approval.replace("counter-basic-block", "stale-branch").replace(
        _POINT_ID, _OTHER_POINT_ID
    )
    waiver_file.write_text(f"{document}\n{stale}", encoding="utf-8")
    approved = load_approved_waiver_set(
        CoverageWaiverConfig("project_data_repository", "coverage-waivers"),
        roots,
        known_targets=(_TARGET,),
    )

    evaluated = evaluate_coverage_campaign(_campaign(), _criterion(), approved)

    assert evaluated.evaluation["status"] == "blocked"
    assert [item["code"] for item in evaluated.evaluation["diagnostics"]] == [
        "COV_WAIVER_POINT_STALE"
    ]
    assert [point.disposition["kind"] for point in evaluated.points] == [
        "eligible",
        "eligible",
    ]
    assert evaluated.rollups[0].waived_points == 0


def test_exact_match_applies_approval_provenance_and_passes_policy(tmp_path: Path) -> None:
    roots = _roots(tmp_path)
    _write_valid_approval(roots)
    approved = load_approved_waiver_set(
        CoverageWaiverConfig("project_data_repository", "coverage-waivers"),
        roots,
        known_targets=(_TARGET,),
    )

    evaluated = evaluate_coverage_campaign(_campaign(), _criterion(), approved)

    assert evaluated.evaluation["status"] == "pass"
    assert evaluated.points[0].disposition == {
        "kind": "waived",
        "reason": "excluded",
        "waiver_id": "counter-basic-block",
        "waiver_file": "rtl/counter.sv.toml",
        "waiver_fingerprint": approved.waivers[0].waiver_fingerprint,
        "provenance": {
            "justification": "Reserved behavior is intentionally excluded.",
            "approved_by": "verification-owner@example.test",
            "approved_at": "2026-08-31T09:00:00Z",
            "approval_ref": "review:CR-1042",
        },
    }
    assert evaluated.rollups[0].eligible_points == 1
    assert evaluated.rollups[0].waived_points == 1


def test_complete_normalization_with_unknown_records_still_matches(tmp_path: Path) -> None:
    roots = _roots(tmp_path)
    _write_valid_approval(roots)
    approved = load_approved_waiver_set(
        CoverageWaiverConfig("project_data_repository", "coverage-waivers"),
        roots,
        known_targets=(_TARGET,),
    )
    campaign = replace(
        _campaign(),
        normalization=MappingProxyType({"status": "complete_with_unknown_records"}),
    )

    evaluated = evaluate_coverage_campaign(campaign, _criterion(), approved)

    assert evaluated.evaluation["status"] == "pass"
    assert evaluated.points[0].disposition["kind"] == "waived"


@pytest.mark.parametrize(
    ("case", "expected_code"),
    [
        ("ambiguous", "COV_WAIVER_MATCH_AMBIGUOUS"),
        ("source_mismatch", "COV_WAIVER_POINT_SOURCE_MISMATCH"),
        ("unscored", "COV_WAIVER_POINT_UNSCORABLE"),
    ],
)
def test_ambiguous_or_ineligible_match_blocks_without_applying(
    tmp_path: Path,
    case: str,
    expected_code: str,
) -> None:
    roots = _roots(tmp_path)
    _write_valid_approval(roots)
    approved = load_approved_waiver_set(
        CoverageWaiverConfig("project_data_repository", "coverage-waivers"),
        roots,
        known_targets=(_TARGET,),
    )
    campaign = _campaign()
    first = campaign.points[0]
    if case == "ambiguous":
        campaign = replace(campaign, points=(first, *campaign.points))
    elif case == "source_mismatch":
        identity = replace(first.identity, location=MappingProxyType({"source": "rtl/other.sv"}))
        campaign = replace(
            campaign, points=(replace(first, identity=identity), campaign.points[1])
        )
    else:
        campaign = replace(
            campaign,
            points=(
                replace(first, disposition=MappingProxyType({"kind": "unscored"})),
                campaign.points[1],
            ),
        )

    evaluated = evaluate_coverage_campaign(campaign, _criterion(), approved)

    assert evaluated.evaluation["status"] == "blocked"
    assert evaluated.evaluation["diagnostics"][0]["code"] == expected_code
    assert all(point.disposition["kind"] != "waived" for point in evaluated.points)


def test_known_other_target_approval_is_valid_but_inapplicable(tmp_path: Path) -> None:
    other = DurableTargetIdentity("acme:demo:counter:1.0#sim_other")
    roots = _roots(tmp_path)
    waiver_file = _write_valid_approval(roots)
    waiver_file.write_text(
        waiver_file.read_text(encoding="utf-8").replace(str(_TARGET), str(other)),
        encoding="utf-8",
    )
    approved = load_approved_waiver_set(
        CoverageWaiverConfig("project_data_repository", "coverage-waivers"),
        roots,
        known_targets=(_TARGET, other),
    )

    evaluated = evaluate_coverage_campaign(_campaign(), _criterion(), approved)

    assert evaluated.evaluation["status"] == "fail"
    assert evaluated.evaluation["diagnostics"] == ()
    assert evaluated.points[0].disposition["kind"] == "eligible"


def test_digest_is_stable_for_creation_order_and_sensitive_to_exact_file_bytes(
    tmp_path: Path,
) -> None:
    first_roots = _roots(tmp_path / "first")
    second_roots = _roots(tmp_path / "second")
    _write_valid_approval(first_roots)
    _write_second_approval(first_roots)
    _write_second_approval(second_roots)
    second_file = _write_valid_approval(second_roots)
    config = CoverageWaiverConfig("project_data_repository", "coverage-waivers")

    first = load_approved_waiver_set(config, first_roots, known_targets=(_TARGET,))
    second = load_approved_waiver_set(config, second_roots, known_targets=(_TARGET,))
    second_file.write_text(
        f"# reviewed formatting edit\n{second_file.read_text(encoding='utf-8')}",
        encoding="utf-8",
    )
    formatted = load_approved_waiver_set(config, second_roots, known_targets=(_TARGET,))

    assert first.digest == second.digest
    assert formatted.digest != second.digest
    assert formatted.waivers[0].waiver_fingerprint != second.waivers[0].waiver_fingerprint
    assert (
        replace(
            formatted.waivers[0],
            waiver_fingerprint=second.waivers[0].waiver_fingerprint,
        )
        == second.waivers[0]
    )


def test_digest_binds_exact_anchor_and_directory_configuration(tmp_path: Path) -> None:
    roots = _roots(tmp_path)
    project_file = _write_valid_approval(roots)
    raw = project_file.read_bytes()
    rtl_file = roots.rtl_repository / "coverage-waivers" / "rtl" / "counter.sv.toml"
    rtl_file.parent.mkdir(parents=True)
    rtl_file.write_bytes(raw)
    alternate_file = (
        roots.project_data_repository / "alternate-waivers" / "rtl" / "counter.sv.toml"
    )
    alternate_file.parent.mkdir(parents=True)
    alternate_file.write_bytes(raw)

    project = load_approved_waiver_set(
        CoverageWaiverConfig("project_data_repository", "coverage-waivers"),
        roots,
        known_targets=(_TARGET,),
    )
    rtl = load_approved_waiver_set(
        CoverageWaiverConfig("rtl_repository", "coverage-waivers"),
        roots,
        known_targets=(_TARGET,),
    )
    alternate = load_approved_waiver_set(
        CoverageWaiverConfig("project_data_repository", "alternate-waivers"),
        roots,
        known_targets=(_TARGET,),
    )

    assert len({project.digest, rtl.digest, alternate.digest}) == 3
