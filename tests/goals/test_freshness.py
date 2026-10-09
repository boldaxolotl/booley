"""Goal freshness: required categories, ``target_surface``, receipts, and suites (B5, B9)."""

from __future__ import annotations

from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Any

import pytest

from booley.evidence.fields import SOURCE_FINGERPRINT_DETAIL_KEY
from booley.goals.freshness import (
    DEFAULT_RESOLVERS,
    GoalFreshnessResolvers,
    evaluate_goal_freshness,
    goal_required_categories,
    stamp_goal_detail,
)
from booley.goals.model import GoalFamily, GoalSpec
from booley.goals.simulation import (
    GOAL_SUITE_DETAIL_KEY,
    resolved_simulation_suite,
    simulation_contract_violation,
)
from booley.goals.target_surface import TargetSurfaceError, target_surface_fingerprint
from booley.runtime.project_dir import reset_cache

LINT_KEY = "lint_clean_top"
SIM_KEY = "sim_pass_top"
REVIEW_KEY = "review_rtl_bugs_clean"
LINT_GOAL = GoalSpec(LINT_KEY, GoalFamily.LINT, "top", {}, ("ad-hoc",))
SIM_GOAL = GoalSpec(SIM_KEY, GoalFamily.SIM, "top", {}, ("ad-hoc",))

DEP_CORE = """CAPI=2:
name: ::dep:0
filesets:
  rtl: {files: [dep.v], file_type: verilogSource}
targets:
  default: {filesets: [rtl]}
"""
TOP_CORE = """CAPI=2:
name: ::top:0
filesets:
  rtl:
    files: [rtl.v]
    file_type: verilogSource
    depend: ["::dep:0"]
  constraints:
    files: [top.sdc]
    file_type: SDC
generators:
  gen: {interpreter: python3, command: gen.py}
parameters:
  WIDTH: {datatype: int, paramtype: vlogparam, default: 8}
targets:
  top: {filesets: [rtl, constraints], toplevel: top, parameters: [WIDTH]}
"""


@pytest.fixture
def project(tmp_path: Path) -> Iterator[Path]:
    """A Project with a ``top`` Target depending on ``dep``, a generator, and a suite."""
    root = tmp_path / "project"
    root.mkdir()
    files = {
        "dep.core": DEP_CORE,
        "top.core": TOP_CORE,
        "rtl.v": "module top; endmodule\n",
        "dep.v": "module dep; endmodule\n",
        "top.sdc": "create_clock -period 10 clk\n",
        "gen.py": "print('generated')\n",
        ".booley_project/tests.toml": '[top]\ntests = ["smoke"]\n',
    }
    for name, content in files.items():
        (root / name).parent.mkdir(parents=True, exist_ok=True)
        (root / name).write_text(content, encoding="utf-8")
    reset_cache()
    yield root
    reset_cache()


def _met(key: str, root: Path, goal: GoalSpec | None, **detail: Any) -> dict[str, Any]:
    return {"met": True, "detail": stamp_goal_detail(key, detail, work_dir=root, goal=goal)}


def _edit(path: Path, old: str, new: str) -> None:
    text = path.read_text(encoding="utf-8")
    assert old in text
    path.write_text(text.replace(old, new), encoding="utf-8")


def _evaluate(key: str, entry: Any, root: Path, goal: GoalSpec | None, **kwargs: Any):
    return evaluate_goal_freshness(key, entry, goal=goal, work_dir=root, **kwargs)


def test_surface_wraps_cross_drive_fileset_resolution_error(project: Path, monkeypatch) -> None:
    def cross_drive(*_args, **_kwargs):
        raise ValueError("path is on a different drive")

    monkeypatch.setattr("booley.fusesoc.fusesoc_registry.os.path.relpath", cross_drive)
    with pytest.raises(TargetSurfaceError, match="different drive"):
        target_surface_fingerprint(project, None)


# ---------------------------------------------------------------------------
# Stamps
# ---------------------------------------------------------------------------


def test_required_categories_add_target_surface_per_family() -> None:
    assert goal_required_categories(LINT_KEY) == {"rtl", "target_surface"}
    assert goal_required_categories(SIM_KEY) == {"rtl", "tb", "target_surface"}
    assert goal_required_categories("review_tb_quality_done") == {"tb", "target_surface"}
    assert goal_required_categories("_report_submitted") == frozenset()


def test_stamp_completes_an_existing_flow_stamp(project: Path) -> None:
    flow_stamp = {"target": "top", "categories": ["rtl"], "fingerprint": {"rtl": {"digest": "r"}}}

    detail = stamp_goal_detail(
        LINT_KEY, {SOURCE_FINGERPRINT_DETAIL_KEY: flow_stamp}, work_dir=project, goal=LINT_GOAL
    )

    stamp = detail[SOURCE_FINGERPRINT_DETAIL_KEY]
    assert stamp["categories"] == ["rtl", "target_surface"]
    assert stamp["fingerprint"]["rtl"] == {"digest": "r"}
    assert stamp["fingerprint"]["target_surface"] == target_surface_fingerprint(project, "top")
    assert flow_stamp["categories"] == ["rtl"]  # the caller's detail is not mutated


def test_freshly_stamped_evidence_is_fresh(project: Path) -> None:
    entry = _met(LINT_KEY, project, LINT_GOAL)

    assert _evaluate(LINT_KEY, entry, project, LINT_GOAL).stale is False


def test_unmet_evidence_is_never_stale(project: Path) -> None:
    assert _evaluate(LINT_KEY, {"met": False, "detail": {}}, project, LINT_GOAL).stale is False


def test_omitted_category_is_stale(project: Path) -> None:
    entry = _met(LINT_KEY, project, LINT_GOAL)
    entry["detail"][SOURCE_FINGERPRINT_DETAIL_KEY]["categories"] = ["rtl"]

    result = _evaluate(LINT_KEY, entry, project, LINT_GOAL)

    assert result.stale is True
    assert "does not stamp target_surface" in result.reason


def test_legacy_ticket_stamp_is_stale(project: Path) -> None:
    from booley.flows.criterion_freshness import build_criterion_freshness

    legacy = build_criterion_freshness(project, target="top", categories=["rtl"]).to_detail()
    entry = {"met": True, "detail": {SOURCE_FINGERPRINT_DETAIL_KEY: legacy}}

    result = _evaluate(LINT_KEY, entry, project, LINT_GOAL)

    assert result.stale is True
    assert "does not stamp target_surface" in result.reason


def test_missing_digest_is_stale(project: Path) -> None:
    entry = _met(LINT_KEY, project, LINT_GOAL)
    entry["detail"][SOURCE_FINGERPRINT_DETAIL_KEY]["fingerprint"]["target_surface"] = {}

    result = _evaluate(LINT_KEY, entry, project, LINT_GOAL)

    assert result.stale is True
    assert "no digest for target_surface" in result.reason


def test_missing_stamp_is_stale(project: Path) -> None:
    result = _evaluate(LINT_KEY, {"met": True, "detail": {}}, project, LINT_GOAL)

    assert result.stale is True
    assert "no source fingerprint" in result.reason


def test_unresolvable_surface_at_stamping_is_recorded_as_stale(project: Path) -> None:
    goal = GoalSpec("lint_clean_ghost", GoalFamily.LINT, "ghost", {}, ("ad-hoc",))
    entry = _met("lint_clean_ghost", project, goal)

    assert (
        "error" in entry["detail"][SOURCE_FINGERPRINT_DETAIL_KEY]["fingerprint"]["target_surface"]
    )
    assert _evaluate("lint_clean_ghost", entry, project, goal).stale is True


# ---------------------------------------------------------------------------
# Resolver failures
# ---------------------------------------------------------------------------


def _failing(error: Exception) -> Callable[..., dict[str, Any]]:
    def resolve(*_args: object, **_kwargs: object) -> dict[str, Any]:
        raise error

    return resolve


@pytest.mark.parametrize(
    "resolvers",
    [
        GoalFreshnessResolvers(target_surface=_failing(TargetSurfaceError("core gone"))),
        GoalFreshnessResolvers(source=_failing(OSError("unreadable"))),
    ],
    ids=["target_surface", "source"],
)
def test_resolver_failure_is_stale(project: Path, resolvers: GoalFreshnessResolvers) -> None:
    entry = _met(LINT_KEY, project, LINT_GOAL)

    result = _evaluate(LINT_KEY, entry, project, LINT_GOAL, resolvers=resolvers)

    assert result.stale is True
    assert "can no longer be resolved" in result.reason


# ---------------------------------------------------------------------------
# Edit one construct
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("path", "old", "new", "changed"),
    [
        ("rtl.v", "endmodule", "wire w; endmodule", ("rtl",)),
        ("top.core", "default: 8", "default: 16", ("target_surface",)),
        (
            "dep.core",
            "default: {filesets: [rtl]}",
            "default: {filesets: [rtl]}  # edited",
            ("target_surface",),
        ),
        (".booley_project/tests.toml", '["smoke"]', '["smoke", "regress"]', ("target_surface",)),
        ("gen.py", "generated", "regenerated", ("target_surface",)),
        # A constraints file is a selected Target input too, so the rtl stamp sees it.
        ("top.sdc", "-period 10", "-period 5", ("rtl", "target_surface")),
    ],
    ids=["rtl", "core_parameter", "dependency_core", "tests_toml", "generator", "constraints"],
)
def test_editing_one_construct_makes_lint_evidence_stale(
    project: Path, path: str, old: str, new: str, changed: tuple[str, ...]
) -> None:
    entry = _met(LINT_KEY, project, LINT_GOAL)
    _edit(project / path, old, new)
    reset_cache()

    result = _evaluate(LINT_KEY, entry, project, LINT_GOAL)

    assert result.stale is True
    assert result.changed_categories == changed


def test_editing_an_unrelated_core_keeps_evidence_fresh(project: Path) -> None:
    (project / "other.core").write_text(
        "CAPI=2:\nname: ::other:0\nfilesets:\n  rtl: {files: [dep.v]}\n"
        "targets:\n  other: {filesets: [rtl], toplevel: dep}\n",
        encoding="utf-8",
    )
    entry = _met(LINT_KEY, project, LINT_GOAL)
    _edit(project / "other.core", "toplevel: dep}\n", "toplevel: dep}\n# edited\n")

    assert _evaluate(LINT_KEY, entry, project, LINT_GOAL).stale is False


# ---------------------------------------------------------------------------
# Review Goals: receipt and source
# ---------------------------------------------------------------------------


@pytest.fixture
def receipt(monkeypatch: pytest.MonkeyPatch) -> dict[str, list[str]]:
    """Stand in for the Reviewer receipt: ``drift`` holds what it reports changed."""
    state: dict[str, list[str]] = {"drift": []}
    monkeypatch.setattr(
        "booley.criteria.freshness.review_receipt_drift",
        lambda *_args, **_kwargs: list(state["drift"]),
    )
    monkeypatch.setattr(
        "booley.criteria.freshness.current_review_receipt_identity",
        lambda *_args, **_kwargs: {"receipt": "current"},
    )
    return state


def _review_entry(project: Path) -> dict[str, Any]:
    return _met(REVIEW_KEY, project, None, review_detail_version=4, contract={"category": "rtl"})


def test_review_fresh_receipt_and_source_is_fresh(project: Path, receipt: dict) -> None:
    assert _evaluate(REVIEW_KEY, _review_entry(project), project, None).stale is False


def test_review_receipt_stale_alone_is_stale(project: Path, receipt: dict) -> None:
    entry = _review_entry(project)
    receipt["drift"] = ["spec"]

    result = _evaluate(REVIEW_KEY, entry, project, None)

    assert result.stale is True
    assert "Reviewer requirement changed" in result.reason


def test_review_source_stale_alone_is_stale_despite_a_fresh_receipt(
    project: Path, receipt: dict
) -> None:
    entry = _review_entry(project)
    _edit(project / "top.core", "default: 8", "default: 16")

    result = _evaluate(REVIEW_KEY, entry, project, None)

    assert result.stale is True
    assert result.changed_categories == ("target_surface",)


# ---------------------------------------------------------------------------
# Simulation suite (B9)
# ---------------------------------------------------------------------------


def test_resolved_suite_follows_tests_and_skips(project: Path) -> None:
    tests = project / ".booley_project" / "tests.toml"
    assert resolved_simulation_suite(project, "top") == ("smoke",)
    tests.write_text('[top]\ntests = ["smoke", "regress"]\nskip = ["regress"]\n')
    assert resolved_simulation_suite(project, "top") == ("smoke",)
    tests.write_text('[top]\ntests = ["smoke"]\nskip = ["smoke"]\n')
    assert resolved_simulation_suite(project, "top") == ()
    tests.write_text("[other]\ntests = ['x']\n")
    assert resolved_simulation_suite(project, "top") == ("default",)


@pytest.mark.parametrize(
    ("detail", "suite", "violation"),
    [
        ({"required_tests": ["smoke"], "passed_tests": ["smoke"]}, ("smoke",), ""),
        ({"required_tests": [], "passed_tests": []}, (), "suite is empty"),
        ({"required_tests": ["smoke"], "passed_tests": []}, ("smoke",), "did not pass smoke"),
        ({"required_tests": ["smoke"], "passed_tests": ["smoke"]}, ("smoke", "b"), "complete"),
        # Coverage names what it ran as selected_tests, never required_tests.
        ({"selected_tests": ["smoke"], "passed_tests": ["smoke"]}, ("smoke",), ""),
        ({"selected_tests": ["smoke"], "passed_tests": ["smoke"]}, ("smoke", "b"), "complete"),
        ({"passed_tests": ["smoke"]}, ("smoke",), "does not name"),
        ({}, ("smoke",), "does not name the tests"),
    ],
)
def test_simulation_contract(detail: dict, suite: tuple[str, ...], violation: str) -> None:
    found = simulation_contract_violation(detail, suite)
    assert (violation in found) if violation else found == ""


def _constant_surface(work_dir: Path, target: str | None) -> dict[str, Any]:
    return {"digest": "constant", "files": []}


def test_sim_required_set_change_is_stale(project: Path) -> None:
    resolvers = GoalFreshnessResolvers(target_surface=_constant_surface)
    detail = stamp_goal_detail(SIM_KEY, {}, work_dir=project, goal=SIM_GOAL, resolvers=resolvers)
    entry = {"met": True, "detail": {**detail, GOAL_SUITE_DETAIL_KEY: ["smoke"]}}
    assert _evaluate(SIM_KEY, entry, project, SIM_GOAL, resolvers=resolvers).stale is False

    (project / ".booley_project" / "tests.toml").write_text(
        '[top]\ntests = ["smoke", "regress"]\n'
    )
    result = _evaluate(SIM_KEY, entry, project, SIM_GOAL, resolvers=resolvers)

    assert result.stale is True
    assert "suite changed (added regress)" in result.reason


def test_sim_evidence_without_a_recorded_suite_is_stale(project: Path) -> None:
    entry = _met(SIM_KEY, project, SIM_GOAL)

    result = _evaluate(SIM_KEY, entry, project, SIM_GOAL, resolvers=DEFAULT_RESOLVERS)

    assert result.stale is True
    assert "does not record the simulation suite" in result.reason


def test_flow_required_suite_and_goal_suite_agree_on_durable_skips(project: Path) -> None:
    """The Flow's ``required_tests`` and the Goal suite both exclude durable skips (B9).

    The Simulation Flow takes its Required Simulation Suite from
    ``resolve_target_test_suite(...).tests`` (runnable tests, skips removed)
    and an ``all`` contract requires exactly that set; the Goal suite resolves
    the same way, so a real pass on a Target with a skipped test is met.
    """
    from booley.config.project_config import load_test_configuration_field
    from booley.criteria.simulation import resolve_simulation_criterion_contract
    from booley.flows.target_test_suite import resolve_target_test_suite

    (project / ".booley_project" / "tests.toml").write_text(
        '[top]\ntests = ["smoke", "hang"]\nskip = ["hang"]\n'
    )
    suite = resolve_target_test_suite(
        "top",
        test_names=load_test_configuration_field(project, "tests"),
        test_skips=load_test_configuration_field(project, "skip"),
    )
    registered = {name for name in suite.tests if name is not None}
    contract = resolve_simulation_criterion_contract({}, registered, registered)
    flow_detail = {"required_tests": sorted(contract.required_tests), "passed_tests": ["smoke"]}

    goal_suite = resolved_simulation_suite(project, "top")

    assert goal_suite == ("smoke",)
    assert flow_detail["required_tests"] == ["smoke"]
    assert simulation_contract_violation(flow_detail, goal_suite) == ""
    with_skipped = {"required_tests": ["hang", "smoke"], "passed_tests": ["smoke"]}
    assert "complete resolved suite" in simulation_contract_violation(with_skipped, goal_suite)
