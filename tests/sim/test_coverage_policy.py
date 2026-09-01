from __future__ import annotations

from dataclasses import replace
from fractions import Fraction
from types import MappingProxyType

import pytest

from booley.sim.coverage_campaign import (
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
from booley.sim.coverage_policy import (
    ApprovedWaiver,
    ApprovedWaiverSet,
    CoverageCriterion,
    CoverageThreshold,
    evaluate_coverage_campaign,
)

_TARGET = DurableTargetIdentity("acme:demo:counter:1.0#sim_counter")


def _point(point_id: str, hits: int, *, metric: str = "line") -> CoveragePoint:
    return CoveragePoint(
        id=point_id,
        identity=CoveragePointIdentity(
            metric=metric,
            location=MappingProxyType({"source": "rtl/counter.sv"}),
            hierarchy="TOP.counter",
            subject=MappingProxyType({"basic_block": int(point_id[-1])}),
            collector=MappingProxyType({"record_type": "v_line", "native_key": point_id}),
        ),
        hits_by_run=MappingProxyType({"run:smoke": hits}) if hits else MappingProxyType({}),
        disposition=MappingProxyType({"kind": "eligible"}),
    )


def _campaign(*, simulation_verdict: str = "pass") -> CoverageCampaign:
    points = (_point("point-0", 1), _point("point-1", 0))
    return CoverageCampaign(
        schema="booley.coverage-campaign/v1",
        campaign_id="campaign:sim_counter:12",
        invocation=MappingProxyType({"id": 12}),
        target=CoverageTarget(identity=str(_TARGET), selector="sim_counter"),
        collector=CoverageCollector(
            kind="verilator",
            version=MappingProxyType({"tag": "v5.046", "commit": "abc"}),
            native_format=MappingProxyType(
                {"name": "verilator-coverage", "compatibility": "compatible"}
            ),
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
                simulation_verdict=simulation_verdict,
                collection="included",
                raw_artifact="artifact:smoke",
                attributes=MappingProxyType({}),
            ),
        ),
        artifacts=(),
        normalization=MappingProxyType({"status": "complete"}),
        points=points,
        rollups=(
            CoverageRollup(
                metric="line",
                semantics="line semantics",
                total_points=2,
                eligible_points=2,
                covered_points=1,
                waived_points=0,
                percent=50.0,
            ),
        ),
        collection=MappingProxyType({"status": "complete"}),
        findings=(),
        evaluation=MappingProxyType({"status": "not_requested"}),
    )


def _criterion(minimum: Fraction = Fraction(50)) -> CoverageCriterion:
    return CoverageCriterion(
        target=_TARGET,
        thresholds=(CoverageThreshold(metric="line", minimum_percent=minimum),),
        tests=None,
    )


def _empty_waivers() -> ApprovedWaiverSet:
    return ApprovedWaiverSet(
        configuration=MappingProxyType({"status": "disabled"}),
        digest="sha256:empty",
        waivers=(),
    )


def _two_metric_campaign() -> CoverageCampaign:
    campaign = _campaign()
    branch_points = (
        _point("branch-0", 1, metric="branch"),
        _point("branch-1", 0, metric="branch"),
    )
    branch_capability = CoverageCapability(
        record_class="branch",
        status="reported",
        attributes=MappingProxyType({"collection": "supported", "scoring": "scored_v1"}),
    )
    branch_rollup = CoverageRollup(
        metric="branch",
        semantics="branch semantics",
        total_points=2,
        eligible_points=2,
        covered_points=1,
        waived_points=0,
        percent=50.0,
    )
    return replace(
        campaign,
        collector=replace(
            campaign.collector,
            capabilities=(*campaign.collector.capabilities, branch_capability),
        ),
        points=(*campaign.points, *branch_points),
        rollups=(*campaign.rollups, branch_rollup),
    )


def test_exact_threshold_pass_preserves_failed_simulation_truth() -> None:
    evaluated = evaluate_coverage_campaign(
        _campaign(simulation_verdict="fail"),
        _criterion(),
        _empty_waivers(),
    )

    assert evaluated.evaluation["status"] == "pass"
    assert evaluated.evaluation["metrics"] == (
        MappingProxyType(
            {
                "metric": "line",
                "total_points": 2,
                "eligible_points": 2,
                "covered_points": 1,
                "waived_points": 0,
                "actual_numerator": 100,
                "actual_denominator": 2,
                "actual_percent": 50.0,
                "minimum_percent": 50,
                "verdict": "pass",
            }
        ),
    )
    assert evaluated.runs[0].simulation_verdict == "fail"


def test_threshold_miss_uses_exact_rational_not_display_percentage() -> None:
    evaluated = evaluate_coverage_campaign(
        _campaign(),
        _criterion(Fraction(500001, 10000)),
        _empty_waivers(),
    )

    assert evaluated.evaluation["status"] == "fail"
    assert evaluated.evaluation["metrics"][0]["actual_percent"] == 50.0
    assert evaluated.evaluation["metrics"][0]["minimum_percent"] == 50.0001
    assert evaluated.evaluation["metrics"][0]["verdict"] == "fail"


def test_target_mismatch_blocks_with_stable_diagnostic() -> None:
    criterion = CoverageCriterion(
        target=DurableTargetIdentity("acme:demo:other:1.0#sim_other"),
        thresholds=_criterion().thresholds,
        tests=None,
    )

    evaluated = evaluate_coverage_campaign(_campaign(), criterion, _empty_waivers())

    assert evaluated.evaluation["status"] == "blocked"
    assert [item["code"] for item in evaluated.evaluation["diagnostics"]] == [
        "COV_EVAL_TARGET_MISMATCH"
    ]


def test_exact_suite_mismatch_blocks_with_stable_diagnostic() -> None:
    criterion = CoverageCriterion(
        target=_TARGET,
        thresholds=_criterion().thresholds,
        tests=("reset",),
    )

    evaluated = evaluate_coverage_campaign(_campaign(), criterion, _empty_waivers())

    assert evaluated.evaluation["status"] == "blocked"
    assert evaluated.evaluation["suite"]["status"] == "mismatch"
    assert [item["code"] for item in evaluated.evaluation["diagnostics"]] == [
        "COV_EVAL_SUITE_MISMATCH"
    ]


def test_incomplete_collection_blocks_instead_of_failing_thresholds() -> None:
    campaign = replace(
        _campaign(),
        collection=MappingProxyType({"status": "incomplete"}),
    )

    evaluated = evaluate_coverage_campaign(campaign, _criterion(), _empty_waivers())

    assert evaluated.evaluation["status"] == "blocked"
    assert [item["code"] for item in evaluated.evaluation["diagnostics"]] == [
        "COV_EVAL_COLLECTION_INCOMPLETE"
    ]


def test_incompatible_normalization_blocks_with_stable_diagnostic() -> None:
    campaign = replace(
        _campaign(),
        normalization=MappingProxyType({"status": "incompatible"}),
    )

    evaluated = evaluate_coverage_campaign(campaign, _criterion(), _empty_waivers())

    assert evaluated.evaluation["status"] == "blocked"
    assert [item["code"] for item in evaluated.evaluation["diagnostics"]] == [
        "COV_EVAL_NORMALIZATION_INCOMPATIBLE"
    ]


def test_required_reported_only_metric_blocks_as_unavailable() -> None:
    campaign = _campaign()
    capability = replace(
        campaign.collector.capabilities[0],
        attributes=MappingProxyType({"collection": "supported", "scoring": "reported_only"}),
    )
    campaign = replace(
        campaign,
        collector=replace(campaign.collector, capabilities=(capability,)),
    )

    evaluated = evaluate_coverage_campaign(campaign, _criterion(), _empty_waivers())

    assert evaluated.evaluation["status"] == "blocked"
    assert [item["code"] for item in evaluated.evaluation["diagnostics"]] == [
        "COV_EVAL_METRIC_UNAVAILABLE"
    ]


def test_waiving_every_point_blocks_zero_eligible_denominator() -> None:
    waivers = ApprovedWaiverSet(
        configuration=MappingProxyType(
            {"anchor": "rtl_repository", "directory": "coverage-waivers"}
        ),
        digest="sha256:approved",
        waivers=tuple(
            ApprovedWaiver(
                target=_TARGET,
                point_id=point_id,
                reason="excluded",
                waiver_id=f"waiver:{point_id}",
                waiver_file=f"rtl/counter.sv/{point_id}.toml",
                waiver_fingerprint=f"sha256:{point_id}",
                provenance=MappingProxyType({"approval": f"{point_id}.toml"}),
            )
            for point_id in ("point-0", "point-1")
        ),
    )

    evaluated = evaluate_coverage_campaign(_campaign(), _criterion(), waivers)

    assert evaluated.evaluation["status"] == "blocked"
    assert [point.disposition["kind"] for point in evaluated.points] == ["waived", "waived"]
    assert evaluated.points[0].disposition == {
        "kind": "waived",
        "reason": "excluded",
        "waiver_id": "waiver:point-0",
        "waiver_file": "rtl/counter.sv/point-0.toml",
        "waiver_fingerprint": "sha256:point-0",
        "provenance": {"approval": "point-0.toml"},
    }
    assert evaluated.rollups[0].eligible_points == 0
    assert evaluated.rollups[0].waived_points == 2
    assert evaluated.rollups[0].percent is None
    assert [item["code"] for item in evaluated.evaluation["diagnostics"]] == [
        "COV_EVAL_EMPTY_DENOMINATOR"
    ]


def test_ungated_campaign_is_not_requested_and_preserves_observations() -> None:
    campaign = _campaign(simulation_verdict="fail")

    evaluated = evaluate_coverage_campaign(campaign, None, _empty_waivers())

    assert evaluated.evaluation == {
        "status": "not_requested",
        "criterion_fingerprint": None,
        "suite": {"status": "not_evaluated"},
        "thresholds": {},
        "metrics": (),
        "diagnostics": (),
    }
    assert evaluated.points == campaign.points
    assert evaluated.runs[0].simulation_verdict == "fail"


def test_ungated_campaign_rejects_nonempty_waiver_input() -> None:
    waiver = ApprovedWaiver(
        target=_TARGET,
        point_id="point-0",
        reason="excluded",
        waiver_id="waiver:point-0",
        waiver_file="rtl/counter.sv/point-0.toml",
        waiver_fingerprint="sha256:point-0",
        provenance=MappingProxyType({}),
    )
    waivers = replace(_empty_waivers(), waivers=(waiver,))

    with pytest.raises(ValueError, match="requires an empty approved waiver set"):
        evaluate_coverage_campaign(_campaign(), None, waivers)


def test_blocked_diagnostics_are_aggregated_in_stable_order() -> None:
    campaign = _campaign()
    capability = replace(
        campaign.collector.capabilities[0],
        attributes=MappingProxyType({"collection": "supported", "scoring": "reported_only"}),
    )
    campaign = replace(
        campaign,
        collector=replace(campaign.collector, capabilities=(capability,)),
        collection=MappingProxyType({"status": "incomplete"}),
        normalization=MappingProxyType({"status": "incompatible"}),
    )

    evaluated = evaluate_coverage_campaign(campaign, _criterion(), _empty_waivers())

    assert [item["code"] for item in evaluated.evaluation["diagnostics"]] == [
        "COV_EVAL_COLLECTION_INCOMPLETE",
        "COV_EVAL_NORMALIZATION_INCOMPATIBLE",
        "COV_EVAL_METRIC_UNAVAILABLE",
    ]


def test_blocked_gated_campaign_still_resolves_approved_dispositions() -> None:
    campaign = replace(
        _campaign(),
        collection=MappingProxyType({"status": "incomplete"}),
    )
    waiver = ApprovedWaiver(
        target=_TARGET,
        point_id="point-1",
        reason="excluded",
        waiver_id="waiver:point-1",
        waiver_file="rtl/counter.sv/point-1.toml",
        waiver_fingerprint="sha256:point-1",
        provenance=MappingProxyType({"approval": "point-1.toml"}),
    )
    waivers = replace(_empty_waivers(), waivers=(waiver,))

    evaluated = evaluate_coverage_campaign(campaign, _criterion(), waivers)

    assert evaluated.evaluation["status"] == "blocked"
    assert evaluated.points[1].disposition["kind"] == "waived"
    assert evaluated.rollups[0].eligible_points == 1
    assert evaluated.rollups[0].waived_points == 1


@pytest.mark.parametrize(
    ("collection_status", "expected_status"),
    [("complete", "pass"), ("incomplete", "blocked")],
)
def test_gated_evaluation_records_approved_waiver_set_digest(
    collection_status: str,
    expected_status: str,
) -> None:
    campaign = replace(
        _campaign(),
        collection=MappingProxyType({"status": collection_status}),
    )
    waivers = replace(_empty_waivers(), digest="sha256:reviewed-waivers")

    evaluated = evaluate_coverage_campaign(campaign, _criterion(), waivers)

    assert evaluated.evaluation["status"] == expected_status
    assert evaluated.evaluation["approved_waiver_set_digest"] == waivers.digest


def test_metrics_use_fixed_order_and_logical_and() -> None:
    criterion = CoverageCriterion(
        target=_TARGET,
        thresholds=(
            CoverageThreshold(metric="branch", minimum_percent=Fraction(60)),
            CoverageThreshold(metric="line", minimum_percent=Fraction(50)),
        ),
        tests=None,
    )

    evaluated = evaluate_coverage_campaign(_two_metric_campaign(), criterion, _empty_waivers())

    assert evaluated.evaluation["status"] == "fail"
    assert [item["metric"] for item in evaluated.evaluation["metrics"]] == [
        "line",
        "branch",
    ]
    assert [item["verdict"] for item in evaluated.evaluation["metrics"]] == [
        "pass",
        "fail",
    ]


def test_fingerprint_ignores_metric_authoring_order_and_numeric_spelling() -> None:
    first = CoverageCriterion(
        target=_TARGET,
        thresholds=(
            CoverageThreshold(metric="line", minimum_percent=Fraction(50)),
            CoverageThreshold(metric="branch", minimum_percent=Fraction("60.0")),
        ),
        tests=("smoke",),
    )
    second = CoverageCriterion(
        target=_TARGET,
        thresholds=(
            CoverageThreshold(metric="branch", minimum_percent=Fraction(60)),
            CoverageThreshold(metric="line", minimum_percent=Fraction("50.00")),
        ),
        tests=("smoke",),
    )

    first_result = evaluate_coverage_campaign(_two_metric_campaign(), first, _empty_waivers())
    second_result = evaluate_coverage_campaign(_two_metric_campaign(), second, _empty_waivers())

    assert (
        first_result.evaluation["criterion_fingerprint"]
        == second_result.evaluation["criterion_fingerprint"]
    )


def test_fingerprint_binds_campaign_schema_version() -> None:
    campaign_v1 = _campaign()
    campaign_v2 = replace(campaign_v1, schema="booley.coverage-campaign/v2")

    first = evaluate_coverage_campaign(campaign_v1, _criterion(), _empty_waivers())
    second = evaluate_coverage_campaign(campaign_v2, _criterion(), _empty_waivers())

    assert first.evaluation["criterion_fingerprint"] != second.evaluation["criterion_fingerprint"]
