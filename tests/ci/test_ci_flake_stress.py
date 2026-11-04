"""Selection and cost-cap contracts for the flake-stress CI job."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parents[2] / ".github/scripts"))

from ci_flake_stress import (
    MAX_COUNT,
    StressPlan,
    changed_test_modules,
    module_costs,
    plan_stress,
    stress_enabled,
)


def test_selects_surviving_test_modules_and_marks_added_ones(tmp_path: Path) -> None:
    entries = [
        ("A", ("tests/ci/test_new.py",)),
        ("M", ("tests/runtime/test_pid.py",)),
        ("D", ("tests/runtime/test_gone.py",)),
        ("R087", ("tests/old/test_moved.py", "tests/new/test_moved.py")),
        ("C100", ("tests/a/test_src.py", "tests/a/test_copy.py")),
        ("A", ("tests/goals/finish_test.py",)),
        ("M", ("tests/conftest.py",)),
        ("A", ("tests/fixtures/cocotb_counter/test_counter.py",)),
        ("M", ("src/booley/test_like_name.py",)),
        ("A", ("tests/bwave/test_native.py",)),
        ("A", ("tests/bwave/test_native_list.py",)),
        ("A", ("tests/bwave/test_broken.py",)),
    ]
    for _status, paths in entries:
        if _status != "D":
            target = tmp_path / paths[-1]
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text("def test_ok():\n    pass\n", encoding="utf-8")
    (tmp_path / "tests/bwave/test_native.py").write_text(
        "import pytest\npytestmark = pytest.mark.native_bwave\n", encoding="utf-8"
    )
    (tmp_path / "tests/bwave/test_native_list.py").write_text(
        "import pytest\npytestmark = [pytest.mark.slow, pytest.mark.native_bwave]\n",
        encoding="utf-8",
    )
    (tmp_path / "tests/bwave/test_broken.py").write_text("def broken(:\n", encoding="utf-8")
    # A newline in a path could forge an Actions log command; no file is needed.
    entries.append(("A", ("tests/ci/test_x\n::error::forged_test.py",)))

    assert changed_test_modules(entries, tmp_path) == {
        "tests/ci/test_new.py": True,
        "tests/runtime/test_pid.py": False,
        "tests/new/test_moved.py": False,
        "tests/a/test_copy.py": True,
        "tests/goals/finish_test.py": True,
        "tests/bwave/test_broken.py": True,
    }


@pytest.mark.parametrize(
    ("event_name", "head_ref", "expected"),
    [
        ("pull_request", "codex/feature", True),
        ("pull_request", "mergify/merge-queue/5f9ee8efb7", False),
        ("push", "", False),
        ("workflow_dispatch", "", False),
    ],
)
def test_only_ordinary_pull_requests_are_stressed(
    event_name: str, head_ref: str, expected: bool
) -> None:
    assert stress_enabled(event_name, head_ref) is expected


def test_cheap_modules_get_the_full_count() -> None:
    plan = plan_stress({"tests/test_a.py": True, "tests/test_b.py": False}, {})

    assert plan == StressPlan(("tests/test_a.py", "tests/test_b.py"), MAX_COUNT, ())


def test_count_shrinks_to_fit_a_slow_module() -> None:
    plan = plan_stress({"tests/test_slow.py": False}, {"tests/test_slow.py": 568.0})

    assert plan == StressPlan(("tests/test_slow.py",), 4, ())


def test_broad_change_falls_back_to_added_modules() -> None:
    modified = {f"tests/test_old_{index}.py": False for index in range(4)}
    costs = dict.fromkeys(modified, 400.0)

    plan = plan_stress({**modified, "tests/test_new.py": True}, costs)

    assert plan.modules == ("tests/test_new.py",)
    assert plan.count == MAX_COUNT
    assert plan.unstressed == tuple(sorted(modified))


def test_unaffordable_change_stresses_nothing_and_reports_it() -> None:
    plan = plan_stress({"tests/test_huge.py": True}, {"tests/test_huge.py": 2400.0})

    assert plan == StressPlan((), 0, ("tests/test_huge.py",))


def test_cost_charges_recorded_time_and_the_default_per_unrecorded_function(
    tmp_path: Path,
) -> None:
    module = tmp_path / "tests/test_mixed.py"
    module.parent.mkdir()
    module.write_text(
        "def test_slow(n): pass\ndef test_fast(): pass\ndef helper(): pass\n"
        "class TestGroup:\n    def test_member(self): pass\n",
        encoding="utf-8",
    )
    model = tmp_path / "timings.json"
    model.write_text(
        '{"schema": 1, "default_seconds": 0.5, "tests": {'
        '"tests/test_mixed.py::test_slow[1]": 30.0, "tests/test_mixed.py::test_slow[2]": 20.0,'
        '"tests/test_other.py::test_fast": 99.0}}',
        encoding="utf-8",
    )

    assert module_costs(["tests/test_mixed.py"], tmp_path, model) == {"tests/test_mixed.py": 51.0}


def test_malformed_timing_model_is_rejected(tmp_path: Path) -> None:
    model = tmp_path / "timings.json"
    model.write_text('{"schema": 1, "default_seconds": 1.0}', encoding="utf-8")

    with pytest.raises(ValueError, match="tests must be an object"):
        module_costs(["tests/test_a.py"], tmp_path, model)
