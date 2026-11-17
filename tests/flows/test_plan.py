from __future__ import annotations

from collections.abc import Callable
from math import inf, nan
from pathlib import Path

import pytest

from booley.flows.plan import (
    CommandPlan,
    FlowPlan,
    WorkUnitPlan,
    normalize_plan_argv,
    normalize_plan_path,
    plan_value_fingerprint,
    stable_unit_id,
)
from tests.conftest import symlink_or_skip


def _unit(**overrides: object) -> WorkUnitPlan:
    values: dict[str, object] = {
        "unit_id": "sim-ordinary-t-123",
        "role": "ordinary",
        "revision": None,
        "selector": "t",
        "target_identity": "::core:0#t",
        "test_or_module_scope": ("smoke",),
        "eda_tool": "verilator",
        "timeout_ms": 600_000,
        "sources": ("rtl/top.sv",),
        "constraints": (),
        "parameters": {"WIDTH": 8},
        "recipe": {"mode": "simulate", "trace": False},
        "commands": (CommandPlan(("make", "-C", ".booley_work/sim/t"), cwd="."),),
        "expected_artifacts": (".booley_work/sim/t/run.log",),
    }
    values.update(overrides)
    return WorkUnitPlan(**values)  # type: ignore[arg-type]


def test_plan_render_is_deterministic_and_carries_fingerprint() -> None:
    plan = FlowPlan(flow="sim", mode="simulate", work_units=(_unit(),))

    first = plan.as_dict()
    second = plan.as_dict()

    assert first == second
    assert first["schema_version"] == 1
    assert first["semantic_plan_fingerprint"] == plan.semantic_plan_fingerprint
    assert first["work_units"][0]["commands"][0] == {
        "argv": ["make", "-C", ".booley_work/sim/t"],
        "cwd": ".",
    }


@pytest.mark.parametrize(
    ("field", "left", "right"),
    [
        ("target_identity", "::a:0#t", "::b:0#t"),
        ("revision", "abc", "def"),
        ("test_or_module_scope", ("one",), ("two",)),
        ("eda_tool", "verilator", "icarus"),
        ("timeout_ms", 1_000, 2_000),
        ("sources", ("a.sv",), ("b.sv",)),
        ("constraints", ("a.sdc",), ("b.sdc",)),
        ("parameters", {"WIDTH": 8}, {"WIDTH": 16}),
        ("recipe", {"trace": False}, {"trace": True}),
        ("recipe", {"build_timeout_ms": 7000}, {"build_timeout_ms": 8000}),
        ("commands", (CommandPlan(("make",), cwd="."),), (CommandPlan(("ninja",), cwd="."),)),
    ],
)
def test_semantic_inputs_change_fingerprint(field: str, left: object, right: object) -> None:
    first = FlowPlan(flow="sim", mode="simulate", work_units=(_unit(**{field: left}),))
    second = FlowPlan(flow="sim", mode="simulate", work_units=(_unit(**{field: right}),))

    assert first.semantic_plan_fingerprint != second.semantic_plan_fingerprint


def test_invocation_local_fields_do_not_change_fingerprint() -> None:
    first = _unit(
        unit_id="invocation-a",
        commands=(CommandPlan(("make",), cwd="/tmp/run-a"),),
        expected_artifacts=("reports/a.json",),
        errors=("display-only",),
    )
    second = _unit(
        unit_id="invocation-b",
        commands=(CommandPlan(("make",), cwd="/tmp/run-b"),),
        expected_artifacts=("reports/b.json",),
        errors=("another-display-error",),
    )

    assert (
        FlowPlan("sim", "simulate", (first,)).semantic_plan_fingerprint
        == FlowPlan("sim", "simulate", (second,)).semantic_plan_fingerprint
    )


def test_mode_changes_fingerprint() -> None:
    unit = _unit()

    assert (
        FlowPlan("sim", "simulate", (unit,)).semantic_plan_fingerprint
        != FlowPlan("sim", "elab_only", (unit,)).semantic_plan_fingerprint
    )


def test_paths_and_argv_are_normalized_to_checkout(tmp_path: Path) -> None:
    nested = tmp_path / ".booley_work" / "sim"

    assert normalize_plan_path(nested, tmp_path) == ".booley_work/sim"
    assert normalize_plan_argv(("tool", str(tmp_path), str(nested)), tmp_path) == (
        "tool",
        ".",
        ".booley_work/sim",
    )
    assert normalize_plan_argv(
        ("sh", "-c", f"tool --root {tmp_path} --file {nested}"),
        tmp_path,
    ) == ("sh", "-c", "tool --root . --file .booley_work/sim")
    outside = tmp_path.parent / "outside.sv"
    assert normalize_plan_path(outside, tmp_path) == outside.as_posix()


@pytest.mark.parametrize(
    ("spell", "separator"),
    [
        (lambda root: root.as_posix().replace("/", "\\"), "\\"),
        (str, "\\"),
        (lambda root: root.as_posix(), "/"),
    ],
    ids=("windows", "native", "posix"),
)
def test_argv_normalization_accepts_every_separator_spelling(
    tmp_path: Path, spell: Callable[[Path], str], separator: str
) -> None:
    root = spell(tmp_path)

    assert normalize_plan_argv(
        ("tool", root, f"{root}{separator}.booley_work{separator}sim"), tmp_path
    ) == ("tool", ".", ".booley_work/sim")


@pytest.mark.parametrize("value", [nan, inf, -inf])
def test_non_finite_plan_values_are_rejected(value: float) -> None:
    with pytest.raises(ValueError, match="finite number"):
        _unit(parameters={"unsafe": value})


def test_plan_values_normalize_supported_containers_and_paths() -> None:
    first = plan_value_fingerprint({"items": {Path("rtl/top.sv")}, "ratio": 1.5})
    second = plan_value_fingerprint({"ratio": 1.5, "items": frozenset({"rtl/top.sv"})})

    assert first == second
    # A constant fingerprint would pass the check above; FlowPlan digests on its own.
    assert plan_value_fingerprint({"FLAVOR": "fast"}) != plan_value_fingerprint({"FLAVOR": "safe"})


def test_unsupported_plan_value_is_rejected() -> None:
    with pytest.raises(TypeError, match="not JSON-serializable"):
        plan_value_fingerprint(object())


def test_plan_path_names_symlinked_booley_dir_lexically(tmp_path: Path) -> None:
    root = tmp_path / "project"
    root.mkdir()
    data = tmp_path / "booley-data"
    data.mkdir()
    symlink_or_skip(root / ".booley", data)

    assert normalize_plan_path(root / ".booley" / "runtime" / "run.log", root) == (
        ".booley/runtime/run.log"
    )
    # A link below the entry is not re-spelled: it still names another place.
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    symlink_or_skip(data / "linked", elsewhere)
    below = root / ".booley" / "linked" / "x.log"
    assert normalize_plan_path(below, root) == below.as_posix()


def test_stable_unit_id_is_repeatable_and_scope_sensitive() -> None:
    first = stable_unit_id("sim", "alu", ("smoke",))

    assert first == stable_unit_id("sim", "alu", ("smoke",))
    assert first != stable_unit_id("sim", "alu", ("corner",))


def test_work_unit_timeout_must_be_positive() -> None:
    with pytest.raises(ValueError, match="must be positive"):
        _unit(timeout_ms=0)
