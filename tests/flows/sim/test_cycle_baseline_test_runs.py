"""A Cycle Count baseline runs the candidate's concrete Test Run (ADR 0072)."""

from __future__ import annotations

from pathlib import Path

import pytest

from booley.flows.sim.execution import NamedTests, SelectedTestRuns
from booley.flows.sim.execution.engine import target_test_plan
from booley.flows.test_runs import UnseededTargetError
from tests.flows.sim.test_flow_test_run_resolution import _MAP, _flow
from tests.flows.sim.test_test_run_launch import (
    _handle,
    _inspection,
    _plusargs,
    _preview,
    _project,
    _work,
)

_CANDIDATE = """
[sim]
tests = ["stress"]
select = "+test={name}"
default_seed = 7

[sim.variants.stress]
default = "soak"
short = ["+iters=10"]
soak = { args = ["+iters=900"], env = { MODE = "soak" } }
"""

# The baseline revision declares another default Test Variant and Default Seed.
_BASELINE = """
[sim]
tests = ["stress"]
select = "+test={name}"

[sim.variants.stress]
default = "short"
short = ["+iters=10"]
soak = ["+iters=5"]
"""


def test_baseline_checkout_launches_the_candidates_test_run(tmp_path: Path):
    candidate = _project(tmp_path / "candidate", _CANDIDATE)
    baseline = _project(tmp_path / "baseline", _BASELINE)
    inspection = _inspection("verilator")
    run = target_test_plan(_handle(candidate), "verilator", {}).run("stress")
    assert run.name == "stress+soak@7"

    on_candidate = _preview(candidate, inspection, SelectedTestRuns((run,)))
    on_baseline = _preview(baseline, inspection, SelectedTestRuns((run,)))
    own_default = _preview(baseline, inspection, NamedTests(("stress",)))

    expected = ["test=stress", "iters=900", "verilator+seed+7"]
    assert _plusargs(on_candidate.commands[0]) == expected
    assert _plusargs(on_baseline.commands[0]) == expected
    assert on_baseline.run_groups == ((run,),)
    launched = _work(baseline, inspection, ("stress",), (run,))
    assert launched.environment == (("MODE", "soak"),)
    # What the baseline would have run from its own tests.toml, and must not.
    assert _plusargs(own_default.commands[0]) == ["test=stress", "iters=10", "verilator+seed+1"]


def test_baseline_that_cannot_apply_the_seed_refuses_instead_of_dropping_it(tmp_path: Path):
    candidate = _project(tmp_path / "candidate", _CANDIDATE)
    baseline = _project(tmp_path / "baseline", _BASELINE)
    run = target_test_plan(_handle(candidate), "verilator", {}).run("stress")
    with pytest.raises(UnseededTargetError) as excinfo:
        _preview(baseline, _inspection("icarus"), SelectedTestRuns((run,)))
    assert str(excinfo.value).startswith(
        "Test Run 'stress+soak@7' has a seed, but Target 'sim' is unseeded"
    )


def _links(flow, monkeypatch, *requirements):
    monkeypatch.setattr(flow, "_campaign_baseline_requirements", lambda _targets: requirements)
    return flow._baseline_links(["sim"], _MAP)


def test_each_baseline_link_carries_the_candidates_test_run(tmp_path, monkeypatch):
    flow = _flow(tmp_path, monkeypatch, "--test", "stress+soak@9")

    links = _links(flow, monkeypatch, ("sim", "sim_ref", "stress"), ("sim", "sim", "reset"))

    assert [(link.baseline, link.test) for link in links] == [
        ("sim_ref", "stress"),
        ("sim", "reset"),
    ]
    assert links[0].run is not None and links[0].run.name == "stress+soak@9"
    # The candidate does not run "reset" in this invocation.
    assert links[1].run is None


def test_default_request_carries_the_default_test_run(tmp_path, monkeypatch):
    flow = _flow(tmp_path, monkeypatch)
    (link,) = _links(flow, monkeypatch, ("sim", "sim", "stress"))
    assert link.run is not None and link.run.name == "stress+short@1"


def test_a_baseline_test_the_candidate_does_not_run_is_never_resolved_from_the_baseline(
    tmp_path, monkeypatch
):
    """A2: with Criteria on two Tests and ``--test stress``, only ``stress`` runs.

    The baseline runs the candidate's Test Run of ``stress``. ``reset`` has no
    candidate Test Run, so the baseline does not run it from its own
    ``tests.toml`` defaults; the comparison is refused by name.
    """
    flow = _flow(tmp_path, monkeypatch, "--test", "stress+soak@9")
    links = _links(flow, monkeypatch, ("sim", "sim", "stress"), ("sim", "sim", "reset"))

    with flow._baseline_checkout(tmp_path / "baseline", links, "a" * 40) as baseline:
        assert Path(flow.args.work_dir) == tmp_path / "baseline"
        assert [run.name for run in baseline.runs_of("sim")] == ["stress+soak@9"]
        assert flow._role_selection("sim", _MAP, "baseline") == SelectedTestRuns(
            baseline.runs_of("sim")
        )

    assert Path(flow.args.work_dir) == tmp_path
    assert dict(baseline.refusals) == {
        ("sim", "reset"): (
            "this invocation runs no Test Run of Test 'reset' on Target 'sim', "
            "so there is nothing to compare"
        )
    }
    assert flow._test_runs.baseline is baseline


def _variant_plan(target: str, default: str, default_seed: int | None = None):
    from booley.config.project_config import normalize_tests_toml
    from booley.flows.target_test_plan import build_target_test_plan

    section = {
        "tests": ["stress"],
        "variants": {"stress": {"default": default, "short": ["+iters=1"], "soak": ["+iters=9"]}},
        **({"default_seed": default_seed} if default_seed is not None else {}),
    }
    return build_target_test_plan(
        target, normalize_tests_toml({target: section})[target], adapter="verilator"
    )


def test_candidates_that_share_a_baseline_each_get_their_own_test_run(tmp_path, monkeypatch):
    """A1: two candidates with other defaults share one baseline Target.

    The baseline runs both candidates' Test Runs, and each candidate is
    linked to the baseline observation of its own.
    """
    from booley.flows.sim.flow import _link_baseline_prerequisites

    flow = _flow(tmp_path, monkeypatch)
    plans = {
        "fast": _variant_plan("fast", "short"),
        "slow": _variant_plan("slow", "soak", default_seed=7),
        "ref": _variant_plan("ref", "short"),
    }
    monkeypatch.setattr(flow, "_target_test_plan", plans.__getitem__)
    monkeypatch.setattr(flow, "_effective_skips", lambda _target: set())
    monkeypatch.setattr(
        flow,
        "_campaign_baseline_requirements",
        lambda _targets: (("fast", "ref", "stress"), ("slow", "ref", "stress")),
    )
    names = {"fast": ["stress"], "slow": ["stress"], "ref": ["stress"]}

    links = flow._baseline_links(["fast", "slow"], names)
    with flow._baseline_checkout(tmp_path / "baseline", links, "a" * 40) as baseline:
        pass

    assert not baseline.refusals
    assert [run.name for run in baseline.runs_of("ref")] == ["stress+short@1", "stress+soak@7"]
    prerequisites: dict[str, list] = {"fast": [], "slow": []}
    by_run = {"stress+short@1": {"work_item_id": "a"}, "stress+soak@7": {"work_item_id": "b"}}
    _link_baseline_prerequisites(prerequisites, baseline.links_of("ref"), by_run)
    assert prerequisites == {
        "fast": [{"work_item_id": "a"}],
        "slow": [{"work_item_id": "b"}],
    }


def test_two_test_runs_of_one_name_but_other_inputs_cannot_share_a_baseline():
    from booley.flows.sim.test_run_invocation import BaselineLink, decide_baseline_runs
    from booley.flows.test_runs import build_test_run

    first = build_test_run("stress", variant="soak", seed=1, args=("+iters=9",))
    other = build_test_run("stress", variant="soak", seed=1, args=("+iters=99",))
    plan = _variant_plan("ref", "short")
    links = (
        BaselineLink("fast", "ref", "stress", first),
        BaselineLink("slow", "ref", "stress", other),
    )

    baseline = decide_baseline_runs(links, lambda _target: plan, "a" * 40)

    assert baseline.runs_of("ref") == (first,)
    assert dict(baseline.refusals) == {
        ("slow", "stress+soak@1"): (
            "Test Run 'stress+soak@1' has other declared arguments or environment on "
            "Target 'slow' than on another candidate compared against baseline Target 'ref'"
        )
    }


def test_a_baseline_that_owns_an_option_of_the_test_run_refuses_it():
    from booley.config.project_config import normalize_tests_toml
    from booley.flows.sim.test_run_invocation import BaselineLink, baseline_link_refusal
    from booley.flows.target_test_plan import build_target_test_plan
    from booley.flows.test_runs import build_test_run

    run = build_test_run("stress", variant="soak", seed=1, args=("+iters=9",))
    section = normalize_tests_toml({"ref": {"tests": ["stress"]}})["ref"]
    plan = build_target_test_plan("ref", section, adapter="verilator", target_plusargs=("iters",))

    reason = baseline_link_refusal(plan, BaselineLink("sim", "ref", "stress", run), "a" * 40)

    assert reason is not None
    assert reason.startswith(
        "baseline revision aaaaaaaaaaaa cannot apply the Test Variant arguments: "
        "Test Run 'stress+soak@1' of Target 'ref': argument '+iters=9' is not allowed"
    )


def test_campaign_planning_runs_no_baseline_for_a_refused_test_run(tmp_path, monkeypatch):
    """A baseline that cannot run the Test Run gets no campaign and no prerequisite."""
    from booley.flows.target_test_plan import build_target_test_plan
    from booley.targets.catalog import TargetCatalog
    from tests.flows.sim.test_cycle_observation import _criterion_flow, _pin_baseline
    from tests.flows.test_baseline_worktree import _project_topology_checkout

    root, revision = _project_topology_checkout(tmp_path, monkeypatch, "standalone")
    flow, _key = _criterion_flow(relative=True)
    flow._args.work_dir = root
    flow._target_handles = {"sim_core": TargetCatalog.build(root).select("sim_core")}
    _pin_baseline(flow, revision)

    def plan_of(target):
        # The candidate registers the compared Test; the baseline revision does not.
        tests = ["coremark"] if Path(flow.args.work_dir) == root else ["other"]
        return build_target_test_plan(target, {"tests": tests}, adapter="verilator")

    monkeypatch.setattr(flow, "_target_test_plan", plan_of)
    monkeypatch.setattr(
        flow, "_planned_baseline_request", lambda *_args: pytest.fail("baseline was planned")
    )
    try:
        requests, prerequisites = flow._plan_campaign_baselines(
            tmp_path / "invocation", None, ["sim_core"], {"sim_core": ["coremark"]}
        )
    finally:
        flow.context.publication_resources.close()

    assert (requests, prerequisites) == ([], {"sim_core": []})
    reason = flow._test_runs.baseline.refusals[("sim_core", "coremark@1")]
    assert reason == (
        f"baseline revision {revision[:12]} does not register Test 'coremark' on Target 'sim_core'"
    )
    from booley.runtime.endpoint_execution import EndpointOutcome

    noted = flow._with_baseline_refusal_note(EndpointOutcome(report_text="sim: PASS"))
    assert noted.report_text.splitlines() == [
        "sim: PASS",
        f"Cycle Count comparison inconclusive for sim_core Test coremark "
        f"(Test Run coremark@1): {reason}",
    ]


def test_campaign_criterion_names_why_the_comparison_is_inconclusive(tmp_path: Path):
    from booley.criteria.state import DevelopmentState
    from booley.criteria.templates import cycle_count_criterion_key
    from booley.flows.sim.acceptance import SimulationAcceptanceCoordinator
    from booley.flows.test_runs import build_test_run
    from tests.flows.sim.test_campaign_test_runs import _hdl_plan, _run_campaign

    run = build_test_run("count", seed=1)
    outcome = _run_campaign(tmp_path, _hdl_plan(tmp_path, ((run,),)))
    name, selector = str(outcome.target["name"]), str(outcome.target["selector"])
    key = cycle_count_criterion_key(name, "count")
    params = {
        "target": f"{outcome.target['vlnv']}#{name}",
        "_target_selector": selector,
        "test": "count",
        "cycle_count_increase_at_most": 5,
    }
    state = DevelopmentState.load(tmp_path / "state.json")
    state.init_criteria({key: True}, criterion_params={key: params}, strict=True)

    def detail(refusals):
        (change,) = SimulationAcceptanceCoordinator._derive_changes(outcome, state, refusals)
        return change.met, change.detail

    assert detail({(selector, "count@1"): "baseline revision abc cannot apply the seed"}) == (
        False,
        {
            **detail({})[1],
            "baseline_observation": "inapplicable",
            "reason": "baseline revision abc cannot apply the seed",
        },
    )
    # Without a refusal a missing baseline keeps its own state and no reason.
    assert detail({})[1]["baseline_observation"] == "mismatched"
    assert "reason" not in detail({})[1]


def test_a_cocotb_baseline_without_a_registry_discovers_the_test():
    from booley.flows.sim.test_run_invocation import BaselineLink, baseline_link_refusal
    from booley.flows.target_test_plan import build_target_test_plan
    from booley.flows.test_runs import build_test_run

    link = BaselineLink("sim", "sim", "stress", build_test_run("stress", seed=7))
    discovering = build_target_test_plan("sim", {}, adapter="cocotb")
    registered = build_target_test_plan("sim", {"tests": ["reset"]}, adapter="cocotb")
    hdl = build_target_test_plan("sim", {}, adapter="verilator")

    assert baseline_link_refusal(discovering, link, "a" * 40) is None
    for plan in (registered, hdl):
        assert baseline_link_refusal(plan, link, "a" * 40) == (
            "baseline revision aaaaaaaaaaaa does not register Test 'stress' on Target 'sim'"
        )


def _baseline_fact(test_run: str | None, fingerprint: str = "sha256:" + "9" * 64) -> dict:
    cycle: dict[str, object] = {"test": "stress", "cycle_count": 100, "unit": "cycles"}
    if test_run is not None:
        cycle |= {"test_run": test_run, "test_run_fingerprint": fingerprint}
    return {
        "role": "cycle_count_baseline",
        "target": {"role": "cycle_count_baseline", "selector": "ref"},
        "manifest": {"owner": "campaign"},
        "campaign_id": "campaign",
        "result": {},
        "work_item_id": "item",
        "cycle_observation": cycle,
    }


def test_a_criterion_binds_only_the_baseline_observation_of_the_candidates_test_run():
    """A1/A2: the same Test with another Test Variant, seed or inputs does not bind."""
    from booley.flows.sim.acceptance import _bound_baseline

    params = {"test": "stress", "_baseline_target": "ref", "cycle_count_increase_at_most": 5}
    fingerprint = "sha256:" + "9" * 64
    current = {"test": "stress", "test_run": "stress+soak@7", "test_run_fingerprint": fingerprint}
    other = _baseline_fact("stress+short@1")
    same = _baseline_fact("stress+soak@7")

    assert _bound_baseline((other, same), params, "stress", current) == (
        same["cycle_observation"],
        "observed",
    )
    assert _bound_baseline((other,), params, "stress", current) == (None, "mismatched")
    changed_inputs = _baseline_fact("stress+soak@7", "sha256:" + "8" * 64)
    assert _bound_baseline((changed_inputs,), params, "stress", current) == (None, "mismatched")
    # No candidate observation: nothing to compare, so nothing binds.
    assert _bound_baseline((same,), params, "stress", None) == (None, "mismatched")


def test_facts_older_than_test_runs_still_bind_by_test():
    from booley.flows.sim.acceptance import _bound_baseline

    params = {"test": "stress", "_baseline_target": "ref", "cycle_count_increase_at_most": 5}
    legacy = _baseline_fact(None)
    assert _bound_baseline((legacy,), params, "stress", {"test": "stress"})[1] == "observed"
    # A candidate observation that names its Test Run needs a baseline that does too.
    named = {
        "test": "stress",
        "test_run": "stress@1",
        "test_run_fingerprint": "sha256:" + "9" * 64,
    }
    assert _bound_baseline((legacy,), params, "stress", named) == (None, "mismatched")


def test_prerequisite_facts_carry_the_baseline_work_items_test_run():
    from booley.flows.sim.campaign.codec import SimulationCampaignIntegrityError
    from booley.flows.sim.campaign.coordinator import _baseline_test_run_facts
    from booley.flows.test_runs import build_test_run, test_run_document

    run = build_test_run("stress", variant="soak", seed=7, args=("+iters=9",))
    assert _baseline_test_run_facts(test_run_document(run)) == {
        "test_run": "stress+soak@7",
        "test_run_fingerprint": run.fingerprint,
    }
    with pytest.raises(SimulationCampaignIntegrityError, match="records no Test Run to compare"):
        _baseline_test_run_facts(None)


@pytest.mark.parametrize(
    ("argv", "names"),
    [
        (("--test", "stress+short", "--test", "stress+soak"), ["stress+short@1", "stress+soak@1"]),
        (("--test", "stress@3", "--test", "stress@4", "--test", "stress+soak@4"), None),
    ],
)
def test_a_candidate_with_several_test_runs_of_one_test_links_each_of_them(
    tmp_path, monkeypatch, argv, names
):
    """Each run has its own baseline run and its own prerequisite, not only the first."""
    from booley.flows.sim.flow import _link_baseline_prerequisites

    flow = _flow(tmp_path, monkeypatch, *argv)
    candidate_runs = [run for run in flow._resolve_test_runs("sim", _MAP) if run.test == "stress"]
    links = _links(flow, monkeypatch, ("sim", "sim", "stress"))

    assert [link.run for link in links] == candidate_runs
    assert names is None or [run.name for run in candidate_runs] == names
    with flow._baseline_checkout(tmp_path / "baseline", links, "a" * 40) as baseline:
        pass
    assert list(baseline.runs_of("sim")) == candidate_runs
    assert not baseline.refused
    prerequisites: dict[str, list] = {"sim": []}
    by_run = {run.name: {"work_item_id": run.name} for run in candidate_runs}
    _link_baseline_prerequisites(prerequisites, baseline.links_of("sim"), by_run)
    assert [item["work_item_id"] for item in prerequisites["sim"]] == [
        run.name for run in candidate_runs
    ]


def test_count_selector_links_every_drawn_seed_and_refuses_each_by_name(tmp_path, monkeypatch):
    from booley.flows.sim.test_run_invocation import baseline_refusal_key
    from booley.flows.target_test_plan import build_target_test_plan

    flow = _flow(tmp_path, monkeypatch, "--test", "stress@x3")
    runs = flow._resolve_test_runs("sim", _MAP)
    links = _links(flow, monkeypatch, ("sim", "ref", "stress"))
    assert [link.run for link in links] == list(runs) and len(runs) == 3

    # A baseline Target that cannot apply a seed refuses all three, each by name.
    unseeded = build_target_test_plan("ref", {"tests": ["stress"]}, adapter="icarus")
    monkeypatch.setattr(flow, "_target_test_plan", lambda _target: unseeded)
    with flow._baseline_checkout(tmp_path / "baseline", links, "a" * 40) as baseline:
        pass

    assert baseline.runs_of("ref") == ()
    assert sorted(baseline.refusals) == sorted(("sim", run.name) for run in runs)
    assert all("cannot apply the seed" in reason for reason in baseline.refusals.values())
    assert baseline_refusal_key("stress", runs[0]) == runs[0].name
    assert baseline_refusal_key("stress", None) == "stress"
