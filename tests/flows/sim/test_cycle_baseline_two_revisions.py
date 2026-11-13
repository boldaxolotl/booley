"""Both revisions of a relative Cycle Count run the candidate's Test Run (ADR 0072).

End to end on the Simulation Campaign path with a fake simulator: the baseline
revision declares another default Test Variant and Default Seed than the
candidate, and must still run exactly what the candidate ran. A baseline
revision that cannot run that Test Run makes the comparison inconclusive
with a named reason; it neither fails the invocation nor launches anything.
"""

from __future__ import annotations

from contextlib import contextmanager
from pathlib import Path
from typing import Any
from unittest.mock import patch

import pytest

from booley.criteria.state import DevelopmentState
from booley.criteria.templates import CriteriaTemplate
from booley.flows.base import SubprocessResult
from booley.runtime.endpoint_execution import EXIT_SUCCESS
from tests.flows.sim.simulation_record_boundary import CampaignSimRun, Launch, run_campaign_sim
from tests.flows.sim.test_coverage_flow import _AcceptanceAdapter

_REVISION = "b" * 40
# Test Variants of ``stress``: the candidate defaults to ``soak``, the baseline to ``short``.
_VARIANT_TABLES = (
    'short = ["+iters=10"]\nsoak = { args = ["+iters=900"], env = { MODE = "soak" } }\n'
)
_CORE = (
    "CAPI=2:\nname: ::lite:0\ntargets:\n  lite:\n    flow: sim\n"
    "    flow_options: {{tool: {tool}}}\n    toplevel: alu_tb\n"
)


def _tests_toml(tests: list[str], *, default_variant: str | None, default_seed: int | None) -> str:
    """A ``tests.toml`` registering *tests* of ``lite`` with the ``stress`` Test Variants."""
    seed = f"default_seed = {default_seed}\n" if default_seed is not None else ""
    variants = (
        f'[lite.variants.stress]\ndefault = "{default_variant}"\n{_VARIANT_TABLES}'
        if default_variant
        else ""
    )
    registered = ", ".join(f'"{test}"' for test in tests)
    return f'[lite]\ntests = [{registered}]\nselect = "+test={{name}}"\n{seed}{variants}'


_CANDIDATE = _tests_toml(["smoke", "stress"], default_variant="soak", default_seed=7)
# Another default Test Variant and (by omission) another Default Seed.
_BASELINE = _tests_toml(["smoke", "stress"], default_variant="short", default_seed=None)
_BASELINE_WITHOUT_STRESS = _tests_toml(["smoke"], default_variant=None, default_seed=None)


def _ticket_with_relative_cycle_count(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, tests: tuple[str, ...]
) -> tuple[Path, str]:
    """Save a Ticket state with a relative Cycle Count Criterion per Test; return it and ``stress``'s key."""
    template = CriteriaTemplate.from_yaml(
        {
            "mandatory": {
                "cycle_count": [
                    {"target": "lite", "test": test, "cycle_count_increase_at_most": "50%"}
                    for test in tests
                ]
            }
        }
    )
    state_path = tmp_path / "state.json"
    state = DevelopmentState.load(state_path)
    state.slug, state.ticket_type = "ticket", "implementation"
    state.init_criteria(template.expand([]), criterion_params=template.expand_params([]))
    for criterion in state.criteria.values():
        criterion.params["_baseline_ref"] = _REVISION
    (key,) = (
        name for name, criterion in state.criteria.items() if criterion.params["test"] == "stress"
    )
    state.save()
    monkeypatch.setenv("BOOLEY_STATE_FILE", str(state_path))
    return state_path, key


def _run(
    tmp_path: Path,
    tmp_path_factory: pytest.TempPathFactory,
    monkeypatch: pytest.MonkeyPatch,
    baseline_tests_toml: str,
    *,
    baseline_tool: str = "verilator",
    criterion_tests: tuple[str, ...] = ("stress",),
    tests: tuple[str, ...] = ("stress",),
) -> tuple[CampaignSimRun, list[tuple[str, Launch]], Any]:
    """Run ``--test stress`` with a relative Cycle Count Criterion per *criterion_tests*.

    Returns the run, the launches tagged with their revision, and the saved
    Criterion of ``stress``.
    """
    # A sibling of the candidate, so the candidate's Target catalog does not see its core.
    baseline = tmp_path_factory.mktemp("baseline")
    (baseline / "lite.core").write_text(_CORE.format(tool=baseline_tool), encoding="utf-8")
    (baseline / ".booley").mkdir()
    (baseline / ".booley/tests.toml").write_text(baseline_tests_toml, encoding="utf-8")
    state_path, key = _ticket_with_relative_cycle_count(tmp_path, monkeypatch, criterion_tests)

    def process(launch: Launch) -> SubprocessResult:
        cycles = 100 if launch.cwd.resolve() == baseline.resolve() else 120
        return SubprocessResult(
            returncode=0,
            stdout=f"[SIM_RESULT] PASSED\n[SIM_CYCLES] stress {cycles}\n",
            duration_s=0.1,
        )

    @contextmanager
    def worktree(_project_root, revision, *, paired_project):
        del paired_project
        assert revision == _REVISION
        yield baseline

    recorder = _AcceptanceAdapter(
        log_dir=tmp_path / "logs",
        identity={"generation": "d" * 32, "authored_sha256": "e" * 64},
    )
    with (
        patch("booley.flows.sim.flow.git_full_sha", return_value=_REVISION),
        patch("booley.flows.sim.flow.baseline_worktree", worktree),
    ):
        run = run_campaign_sim(
            tmp_path,
            monkeypatch,
            {"lite": ["smoke", "stress"]},
            process,
            files={".booley/tests.toml": _CANDIDATE},
            adapter=recorder,
            target="lite",
            test=tests,
        )
    launches = [
        ("baseline" if launch.cwd.resolve() == baseline.resolve() else "candidate", launch)
        for launch in run.launches
    ]
    return run, launches, DevelopmentState.load(state_path).criteria[key]


def _launch(launch: Launch) -> tuple[list[str], bool]:
    """The plusargs of one simulator launch, and whether it exported MODE=soak."""
    return list(launch.work.plusargs), "export MODE=soak" in launch.script


def test_both_revisions_run_the_candidates_test_run(tmp_path, tmp_path_factory, monkeypatch):
    run, launches, criterion = _run(tmp_path, tmp_path_factory, monkeypatch, _BASELINE)
    result = run.result.outcome

    assert run.result.exit_code == EXIT_SUCCESS, result.report_text
    assert [revision for revision, _ in launches] == ["baseline", "candidate"]
    expected = (["test=stress", "iters=900", "verilator+seed+7"], True)
    assert [_launch(launch) for _, launch in launches] == [expected, expected]
    assert criterion.met is True
    assert (criterion.detail["baseline_cycles"], criterion.detail["cycles"]) == (100, 120)
    assert [row["name"] for row in criterion.detail["test_runs"]] == ["stress+soak@7"]
    assert "cycle_count_baseline_inconclusive" not in result.detail


@pytest.mark.parametrize(
    ("baseline_tests_toml", "baseline_tool", "reason"),
    [
        (
            _BASELINE_WITHOUT_STRESS,
            "verilator",
            "baseline revision bbbbbbbbbbbb does not register Test 'stress' on Target 'lite'",
        ),
        (
            _BASELINE,
            "icarus",
            "baseline revision bbbbbbbbbbbb cannot apply the seed: Test Run 'stress+soak@7' "
            "has a seed, but Target 'lite' is unseeded",
        ),
    ],
    ids=["test-missing", "unseeded"],
)
def test_a_baseline_that_cannot_run_the_test_run_makes_the_comparison_inconclusive(
    tmp_path, tmp_path_factory, monkeypatch, baseline_tests_toml, baseline_tool, reason
):
    run, launches, criterion = _run(
        tmp_path,
        tmp_path_factory,
        monkeypatch,
        baseline_tests_toml,
        baseline_tool=baseline_tool,
    )
    result = run.result.outcome

    # The candidate still runs and passes; nothing is launched on the baseline.
    assert run.result.exit_code == EXIT_SUCCESS, result.report_text
    assert [revision for revision, _launch in launches] == ["candidate"]
    assert criterion.met is False
    assert criterion.detail["baseline_observation"] == "inapplicable"
    assert criterion.detail["baseline_cycles"] is None
    assert criterion.detail["reason"].startswith(reason)
    (entry,) = result.detail["cycle_count_baseline_inconclusive"]
    assert (entry["target"], entry["test"]) == ("lite", "stress")
    assert entry["reason"].startswith(reason)
    assert entry["test_run"] == "stress+soak@7"
    assert (
        "Cycle Count comparison inconclusive for lite Test stress "
        f"(Test Run stress+soak@7): {reason}"
    ) in result.report_text


def test_a_baseline_test_the_candidate_does_not_run_is_not_run_from_baseline_defaults(
    tmp_path, tmp_path_factory, monkeypatch
):
    """Criteria on ``smoke`` and ``stress``, request ``--test stress``.

    The baseline still runs the candidate's Test Run of ``stress`` and
    nothing else: ``smoke`` is not resolved from the baseline checkout's own
    defaults, and its comparison is reported as inconclusive by name.
    """
    run, launches, criterion = _run(
        tmp_path, tmp_path_factory, monkeypatch, _BASELINE, criterion_tests=("smoke", "stress")
    )
    result = run.result.outcome

    assert run.result.exit_code == EXIT_SUCCESS, result.report_text
    expected = (["test=stress", "iters=900", "verilator+seed+7"], True)
    assert [(revision, _launch(launch)) for revision, launch in launches] == [
        ("baseline", expected),
        ("candidate", expected),
    ]
    assert criterion.met is True
    (entry,) = result.detail["cycle_count_baseline_inconclusive"]
    assert entry == {
        "target": "lite",
        "test": "smoke",
        "reason": (
            "this invocation runs no Test Run of Test 'smoke' on Target 'lite', "
            "so there is nothing to compare"
        ),
    }


@pytest.mark.parametrize(
    ("tests", "count"),
    [
        (("stress@x3",), 3),
        (("stress+short", "stress+soak"), 2),
        (("stress+soak@4", "stress@5"), 2),
    ],
    ids=["count-selector", "two-variants", "two-seeds"],
)
def test_every_candidate_test_run_of_a_compared_test_runs_on_the_baseline(
    tmp_path, tmp_path_factory, monkeypatch, tests, count
):
    """Not only the first: ``stress@x3`` and two Test Variants each get all their baselines."""
    run, launches, _criterion = _run(
        tmp_path, tmp_path_factory, monkeypatch, _BASELINE, tests=tests
    )

    assert run.result.exit_code == EXIT_SUCCESS, run.result.outcome.report_text
    on_baseline = [_launch(launch) for revision, launch in launches if revision == "baseline"]
    on_candidate = [_launch(launch) for revision, launch in launches if revision == "candidate"]
    assert len(on_candidate) == count
    assert on_baseline == on_candidate
    assert len({tuple(plusargs) for plusargs, _mode in on_baseline}) == count
    assert "cycle_count_baseline_inconclusive" not in run.result.outcome.detail


def test_every_candidate_test_run_gets_its_own_named_refusal(
    tmp_path, tmp_path_factory, monkeypatch
):
    """A baseline that cannot seed: each of the three runs is refused by its own name."""
    run, launches, _criterion = _run(
        tmp_path,
        tmp_path_factory,
        monkeypatch,
        _BASELINE,
        baseline_tool="icarus",
        tests=("stress@x3",),
    )
    result = run.result.outcome

    assert [revision for revision, _launch in launches] == ["candidate"] * 3
    entries = result.detail["cycle_count_baseline_inconclusive"]
    assert len(entries) == 3
    names = [entry["test_run"] for entry in entries]
    assert len(set(names)) == 3 and all(name.startswith("stress+soak@") for name in names)
    for entry in entries:
        assert (entry["target"], entry["test"]) == ("lite", "stress")
        assert f"Test Run {entry['test_run']!r} has a seed" in entry["reason"]
        assert (
            f"Cycle Count comparison inconclusive for lite Test stress "
            f"(Test Run {entry['test_run']}): "
        ) in result.report_text
