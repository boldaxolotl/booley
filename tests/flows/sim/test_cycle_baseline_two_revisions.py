"""Both revisions of a relative Cycle Count run the candidate's Test Run (ADR 0072).

End to end on the direct Simulation path with a fake simulator: the baseline
revision declares another default Test Variant and Default Seed than the
candidate, and must still run exactly what the candidate ran. A baseline
revision that cannot run that Test Run makes the comparison inconclusive
with a named reason; it neither fails the invocation nor launches anything.
"""

from __future__ import annotations

from contextlib import contextmanager
from pathlib import Path
from unittest.mock import patch

import pytest

from booley.config.project_config import normalize_tests_toml
from booley.criteria.templates import CriteriaTemplate
from booley.flows.base import SubprocessResult
from booley.flows.sim.flow import SimulateFlow
from booley.runtime.endpoint_execution import EXIT_SUCCESS
from tests.flows.sim.test_flow import (
    _FLOW_ENABLED,
    _make_flow,
    _use_legacy_build_transport_for_canned_flow_tests,  # noqa: F401 -- autouse fixture
)

_REVISION = "b" * 40
_VARIANTS = {
    "short": ["+iters=10"],
    "soak": {"args": ["+iters=900"], "env": {"MODE": "soak"}},
}
_CANDIDATE = {
    "tests": ["smoke", "stress"],
    "select": "+test={name}",
    "default_seed": 7,
    "variants": {"stress": {"default": "soak", **_VARIANTS}},
}
# Another default Test Variant and (by omission) another Default Seed.
_BASELINE = {
    "tests": ["smoke", "stress"],
    "select": "+test={name}",
    "variants": {"stress": {"default": "short", **_VARIANTS}},
}
_CORE = (
    "CAPI=2:\nname: ::lite:0\ntargets:\n  lite:\n    flow: sim\n"
    "    flow_options: {{tool: {tool}}}\n    toplevel: alu_tb\n"
)


def _run(
    tmp_path: Path,
    baseline_section: dict,
    *,
    baseline_tool: str = "verilator",
    criterion_tests: tuple[str, ...] = ("stress",),
    tests: tuple[str, ...] = ("stress",),
):
    """Run ``--test stress`` with a relative Cycle Count Criterion per *criterion_tests*.

    Returns the result, the launches, and the Criterion of ``stress``.
    """
    candidate = tmp_path / "candidate"
    baseline = tmp_path / "baseline"
    for root in (candidate, baseline):
        root.mkdir()
    (baseline / "lite.core").write_text(_CORE.format(tool=baseline_tool), encoding="utf-8")
    selectors = [word for test in tests for word in ("--test", test)]
    flow = _make_flow(candidate, config="lite", extra_args=selectors)
    template = CriteriaTemplate.from_yaml(
        {
            "mandatory": {
                "cycle_count": [
                    {"target": "lite", "test": test, "cycle_count_increase_at_most": "50%"}
                    for test in criterion_tests
                ]
            }
        }
    )
    flow.state.init_criteria(template.expand([]), criterion_params=template.expand_params([]))
    keys = [name for name in flow.state.criteria if name.startswith("cycle_count_")]
    for name in keys:
        flow.state.criteria[name].params["_baseline_ref"] = _REVISION
    (key,) = (name for name in keys if flow.state.criteria[name].params["test"] == "stress")
    sections = {
        candidate.resolve(): normalize_tests_toml({"lite": _CANDIDATE}),
        baseline.resolve(): normalize_tests_toml({"lite": baseline_section}),
    }
    launched: list[tuple[str, str]] = []

    def execute(command):
        revision = "baseline" if Path(flow.args.work_dir) == baseline else "candidate"
        launched.append((revision, command[-1]))
        cycles = 100 if revision == "baseline" else 120
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

    with (
        patch(
            "booley.flows.target_test_plan_loading.load_test_configuration",
            side_effect=lambda root: sections[Path(root).resolve()],
        ),
        patch("booley.flows.sim.flow._get_test_names", return_value={"lite": ["smoke", "stress"]}),
        patch("booley.flows.sim.flow.git_full_sha", return_value=_REVISION),
        patch("booley.flows.sim.flow.baseline_worktree", worktree),
        patch.object(SimulateFlow, "_flow_enabled", return_value=_FLOW_ENABLED),
        patch.object(flow, "_execute", side_effect=execute),
        flow.publication_resources,
    ):
        result = flow._run()
    return result, launched, flow.state.criteria[key]


def _launch(script: str) -> tuple[list[str], bool]:
    """The plusargs of one simulator launch, and whether it exported MODE=soak."""
    words = script.rsplit(" && ", maxsplit=1)[-1].split()
    plusargs = [word.removeprefix("--plusarg=") for word in words if "--plusarg=" in word]
    return plusargs, "export MODE=soak" in script


def test_both_revisions_run_the_candidates_test_run(tmp_path: Path):
    result, launched, criterion = _run(tmp_path, _BASELINE)

    assert result.exit_code == EXIT_SUCCESS, result.report_text
    assert [revision for revision, _script in launched] == ["baseline", "candidate"]
    expected = (["test=stress", "iters=900", "verilator+seed+7"], True)
    assert [_launch(script) for _revision, script in launched] == [expected, expected]
    assert criterion.met is True
    assert (criterion.detail["baseline_cycles"], criterion.detail["cycles"]) == (100, 120)
    assert [row["name"] for row in criterion.detail["test_runs"]] == ["stress+soak@7"]
    assert "cycle_count_baseline_inconclusive" not in result.detail


@pytest.mark.parametrize(
    ("baseline_section", "baseline_tool", "reason"),
    [
        (
            {**_BASELINE, "tests": ["smoke"], "variants": {}},
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
    tmp_path: Path, baseline_section, baseline_tool, reason
):
    result, launched, criterion = _run(tmp_path, baseline_section, baseline_tool=baseline_tool)

    # The candidate still runs and passes; nothing is launched on the baseline.
    assert result.exit_code == EXIT_SUCCESS, result.report_text
    assert [revision for revision, _script in launched] == ["candidate"]
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
    tmp_path: Path,
):
    """Criteria on ``smoke`` and ``stress``, request ``--test stress``.

    The baseline still runs the candidate's Test Run of ``stress`` and
    nothing else: ``smoke`` is not resolved from the baseline checkout's own
    defaults, and its comparison is reported as inconclusive by name.
    """
    result, launched, criterion = _run(tmp_path, _BASELINE, criterion_tests=("smoke", "stress"))

    assert result.exit_code == EXIT_SUCCESS, result.report_text
    expected = (["test=stress", "iters=900", "verilator+seed+7"], True)
    assert [(revision, _launch(script)) for revision, script in launched] == [
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
    tmp_path: Path, tests, count
):
    """Not only the first: ``stress@x3`` and two Test Variants each get all their baselines."""
    result, launched, _criterion = _run(tmp_path, _BASELINE, tests=tests)

    assert result.exit_code == EXIT_SUCCESS, result.report_text
    on_baseline = [_launch(script) for revision, script in launched if revision == "baseline"]
    on_candidate = [_launch(script) for revision, script in launched if revision == "candidate"]
    assert len(on_candidate) == count
    assert on_baseline == on_candidate
    assert len({tuple(plusargs) for plusargs, _mode in on_baseline}) == count
    assert "cycle_count_baseline_inconclusive" not in result.detail


def test_every_candidate_test_run_gets_its_own_named_refusal(tmp_path: Path):
    """A baseline that cannot seed: each of the three runs is refused by its own name."""
    result, launched, _criterion = _run(
        tmp_path, _BASELINE, baseline_tool="icarus", tests=("stress@x3",)
    )

    assert [revision for revision, _script in launched] == ["candidate"] * 3
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
