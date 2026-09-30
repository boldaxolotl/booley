"""A promotion plan predicts and produces the real Approved Waiver Set (ADR 0066)."""

from __future__ import annotations

import pytest

from booley.flows.sim.coverage_policy import evaluate_coverage_campaign
from booley.flows.sim.coverage_waiver_application import (
    WaiverPlanError,
    WaiverPromotionPlan,
    apply_promotion_plan,
    load_promoted_waiver_set,
    strict_reevaluation,
)
from booley.flows.sim.coverage_waiver_promotion import WaiverCollisionError
from booley.flows.sim.coverage_waivers import (
    CoverageWaiverConfig,
    load_approved_waiver_set,
)
from tests.flows.sim.test_coverage_waiver_promotion import _STAMPS, _candidate
from tests.flows.sim.test_coverage_waivers import (
    _TARGET,
    _campaign,
    _criterion,
    _roots,
    _write_valid_approval,
)

_DIRECTORY = "coverage-waivers"


def _plan(anchor: str = "project_data_repository", **candidate) -> WaiverPromotionPlan:
    return WaiverPromotionPlan(anchor, _DIRECTORY, _STAMPS, (_candidate(**candidate),))


def _rtl(roots) -> None:
    source = roots.rtl_repository / "rtl" / "counter.sv"
    source.parent.mkdir(parents=True)
    source.write_bytes(b"module counter; endmodule\n")


def test_plan_json_round_trips_with_a_stable_digest() -> None:
    plan = _plan()

    restored = WaiverPromotionPlan.from_json(plan.to_json())

    assert restored == plan
    assert restored.sha256() == plan.sha256()
    assert plan.waiver_ids == ("wc-0123456789ab",)


@pytest.mark.parametrize(
    "mutate",
    [
        lambda doc: doc.update(schema=2),
        lambda doc: doc["candidates"][0].update(extra="x"),
        lambda doc: doc["candidates"].clear(),
        lambda doc: doc["stamps"].pop("approved_by"),
    ],
)
def test_malformed_plans_are_rejected(mutate) -> None:
    document = _plan().to_json()
    mutate(document)

    with pytest.raises(WaiverPlanError):
        WaiverPromotionPlan.from_json(document)


def test_duplicate_candidates_are_rejected() -> None:
    with pytest.raises(WaiverPlanError, match="twice"):
        WaiverPromotionPlan(
            "project_data_repository", _DIRECTORY, _STAMPS, (_candidate(), _candidate())
        )


@pytest.mark.parametrize("anchor", ["project_data_repository", "rtl_repository"])
def test_prediction_overlay_loads_the_promoted_set_without_touching_the_real_one(
    tmp_path, anchor
) -> None:
    roots = _roots(tmp_path)
    _rtl(roots)
    real_dir = getattr(roots, anchor) / _DIRECTORY
    plan = _plan(anchor)

    waivers = load_promoted_waiver_set(plan, roots, tmp_path / "overlay", (_TARGET,))

    assert [item.waiver_id for item in waivers.waivers] == ["wc-0123456789ab"]
    assert waivers.configuration == {"anchor": anchor, "directory": _DIRECTORY}
    assert not real_dir.exists()


def test_applying_to_the_real_directory_matches_the_prediction(tmp_path) -> None:
    roots = _roots(tmp_path)
    _rtl(roots)
    plan = _plan()
    predicted = load_promoted_waiver_set(plan, roots, tmp_path / "overlay", (_TARGET,))

    written = apply_promotion_plan(plan, roots.project_data_repository)

    assert written == (
        "coverage-waivers/rtl/counter.sv.toml",
        "coverage-waivers/proofs/wc-0123456789ab.md",
    )
    config = CoverageWaiverConfig("project_data_repository", _DIRECTORY)
    assert load_approved_waiver_set(config, roots, (_TARGET,)).digest == predicted.digest


def test_plan_appends_to_an_existing_approval_document(tmp_path) -> None:
    roots = _roots(tmp_path)
    existing = _write_valid_approval(roots)
    before = existing.read_bytes()

    apply_promotion_plan(_plan(point_id=_other_line_point()), roots.project_data_repository)

    assert existing.read_bytes().startswith(before)


def _other_line_point() -> str:
    """A second line point on rtl/counter.sv (basic block 1)."""
    import base64
    import json

    from tests.flows.sim.test_coverage_waivers import _POINT_ID

    payload = _POINT_ID.removeprefix("cp1:")
    identity = json.loads(base64.urlsafe_b64decode(payload + "=" * (-len(payload) % 4)))
    identity["subject"]["basic_block"] = 1
    encoded = json.dumps(identity, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return "cp1:" + base64.urlsafe_b64encode(encoded.encode()).decode().rstrip("=")


def test_reapplying_the_same_plan_to_its_output_collides(tmp_path) -> None:
    """Recovery recomputes from the unfinalized candidate, never from its own output."""
    roots = _roots(tmp_path)
    _rtl(roots)
    plan = _plan()
    apply_promotion_plan(plan, roots.project_data_repository)

    with pytest.raises(WaiverCollisionError):
        apply_promotion_plan(plan, roots.project_data_repository)


def test_strict_reevaluation_passes_with_the_promoted_set(tmp_path) -> None:
    roots = _roots(tmp_path)
    _rtl(roots)
    (roots.project_data_repository / _DIRECTORY).mkdir()
    config = CoverageWaiverConfig("project_data_repository", _DIRECTORY)
    empty = load_approved_waiver_set(config, roots, (_TARGET,))
    persisted = evaluate_coverage_campaign(_campaign(), _criterion(), empty)
    assert persisted.evaluation["status"] == "fail"
    promoted = load_promoted_waiver_set(_plan(), roots, tmp_path / "overlay", (_TARGET,))

    evaluated = strict_reevaluation(persisted, promoted)

    assert evaluated.evaluation["status"] == "pass"
    assert evaluated.evaluation["approved_waiver_set_digest"] == promoted.digest


def test_strict_reevaluation_replaces_previously_applied_waivers(tmp_path) -> None:
    """A persisted Campaign already carries waived points; re-evaluation must not trip on them."""
    roots = _roots(tmp_path)
    _write_valid_approval(roots)
    config = CoverageWaiverConfig("project_data_repository", _DIRECTORY)
    approved = load_approved_waiver_set(config, roots, (_TARGET,))
    persisted = evaluate_coverage_campaign(_campaign(), _criterion(), approved)
    assert persisted.evaluation["status"] == "pass"

    evaluated = strict_reevaluation(persisted, approved)

    assert evaluated.evaluation["status"] == "pass"
    assert evaluated.evaluation["diagnostics"] == ()


@pytest.mark.parametrize("link_kind", ["directory", "document", "proof"])
def test_prediction_rejects_symlinks_without_changing_their_targets(tmp_path, link_kind):
    roots = _roots(tmp_path)
    document = _write_valid_approval(roots)
    directory = roots.project_data_repository / _DIRECTORY
    (directory / "proofs").mkdir()
    (directory / "proofs" / "existing.md").write_bytes(b"Existing proof.\n")
    linked = {"directory": directory, "document": document, "proof": directory / "proofs"}[
        link_kind
    ]
    external = tmp_path / "external"
    linked.rename(external)
    try:
        linked.symlink_to(external, target_is_directory=external.is_dir())
    except OSError as exc:
        pytest.skip(f"symlink creation unavailable: {exc}")
    files = list(external.rglob("*")) if external.is_dir() else [external]
    before = {path: path.read_bytes() for path in files if path.is_file()}

    with pytest.raises(WaiverPlanError, match="symlink"):
        load_promoted_waiver_set(
            _plan(point_id=_other_line_point()), roots, tmp_path / "overlay", (_TARGET,)
        )

    assert {path: path.read_bytes() for path in before} == before
