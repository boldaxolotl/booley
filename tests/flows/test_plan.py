from __future__ import annotations

from math import inf, nan
from pathlib import Path
from types import SimpleNamespace

import pytest

from booley.flows.plan import (
    CommandPlan,
    FlowPlan,
    WorkUnitPlan,
    normalize_plan_argv,
    normalize_plan_inputs,
    normalize_plan_path,
    plan_value_fingerprint,
    stable_unit_id,
)


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


def test_mode_and_command_argv_change_fingerprint() -> None:
    unit = _unit()
    other_command = _unit(commands=(CommandPlan(("ninja",), cwd="."),))

    assert (
        FlowPlan("sim", "simulate", (unit,)).semantic_plan_fingerprint
        != FlowPlan("sim", "elab_only", (unit,)).semantic_plan_fingerprint
    )
    assert (
        FlowPlan("sim", "simulate", (unit,)).semantic_plan_fingerprint
        != FlowPlan("sim", "simulate", (other_command,)).semantic_plan_fingerprint
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


def test_windows_argv_spelling_is_normalized_to_checkout(tmp_path: Path) -> None:
    checkout = tmp_path.as_posix().replace("/", "\\")

    assert normalize_plan_argv(
        ("tool", f"{checkout}\\.booley_work\\sim"),
        tmp_path,
    ) == ("tool", ".booley_work/sim")


def test_argv_normalization_is_checkout_independent() -> None:
    first = normalize_plan_argv(
        ("sh", "-c", "export BOOLEY_PROJECT_ROOT=/tmp/first && tool /tmp/first/rtl/a.sv"),
        Path("/tmp/first"),
    )
    second = normalize_plan_argv(
        ("sh", "-c", "export BOOLEY_PROJECT_ROOT=/tmp/second && tool /tmp/second/rtl/a.sv"),
        Path("/tmp/second"),
    )

    assert first == second


def test_argv_normalization_accepts_native_and_posix_separators(tmp_path: Path) -> None:
    root = str(tmp_path)
    native = normalize_plan_argv(
        (root, f"{root}\\.booley_work\\sim"),
        tmp_path,
    )
    posix_root = tmp_path.as_posix()
    posix = normalize_plan_argv(
        (posix_root, f"{posix_root}/.booley_work/sim"),
        tmp_path,
    )

    assert native == posix == (".", ".booley_work/sim")


def test_inputs_are_partitioned_and_normalized_once(tmp_path: Path) -> None:
    inputs = (
        SimpleNamespace(path=str(tmp_path / "rtl/top.sv"), file_type="systemVerilogSource"),
        SimpleNamespace(path=str(tmp_path / "constraints/top.sdc"), file_type="SDC"),
    )

    assert normalize_plan_inputs(inputs, tmp_path) == (
        ("rtl/top.sv",),
        ("constraints/top.sdc",),
    )


@pytest.mark.parametrize("value", [nan, inf, -inf])
def test_non_finite_plan_values_are_rejected(value: float) -> None:
    with pytest.raises(ValueError, match="finite number"):
        _unit(parameters={"unsafe": value})


def test_sensitive_semantic_values_have_distinct_fingerprints() -> None:
    assert plan_value_fingerprint({"FLAVOR": "fast"}) != plan_value_fingerprint({"FLAVOR": "safe"})


def test_stable_unit_id_is_repeatable_and_scope_sensitive() -> None:
    first = stable_unit_id("sim", "alu", ("smoke",))

    assert first == stable_unit_id("sim", "alu", ("smoke",))
    assert first != stable_unit_id("sim", "alu", ("corner",))


def test_work_unit_timeout_must_be_positive() -> None:
    with pytest.raises(ValueError, match="must be positive"):
        _unit(timeout_ms=0)
