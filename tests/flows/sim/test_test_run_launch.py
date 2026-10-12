"""The simulator command and environment Booley builds for a Test Run (ADR 0072).

These tests use no simulator: they prove what reaches the adapter for each
seed mechanism. The real-simulator proofs (same seed repeats, another seed
differs) live in ``tests/docker``.
"""

from __future__ import annotations

import re
from pathlib import Path
from types import SimpleNamespace
from typing import cast
from unittest.mock import MagicMock, patch

import pytest

from booley.flows.sim.execution import (
    DefaultSelection,
    NamedTests,
    SelectedTestRuns,
    SimulationExecution,
    SimulationOptions,
)
from booley.flows.sim.execution.engine import (
    _preview_work,
    launch_environment,
    target_test_plan,
)
from booley.targets.domain import TargetHandle

_VARIANTS = """
[sim.variants.stress]
default = "short"
short = ["+iters=1000"]
soak = ["+iters=100000"]
fault = { args = ["+err_rate=5"], env = { FAULT_MODE = "1" } }
"""


def _project(root: Path, tests_toml: str) -> Path:
    project = root / ".booley_project"
    project.mkdir(parents=True)
    (project / "booley.toml").write_text('[project]\nname = "demo"\n', encoding="utf-8")
    (project / "tests.toml").write_text(tests_toml, encoding="utf-8")
    return root


def _handle(root: Path, selector: str = "sim") -> TargetHandle:
    return cast(
        TargetHandle,
        SimpleNamespace(
            project_root=root.resolve(),
            selector=selector,
            identity=f"acme:lib:demo:1#{selector}",
            vlnv="acme:lib:demo:1",
            eda_tool="verilator",
            snapshot_id="",
        ),
    )


def _inspection(eda_tool: str, *, cocotb: bool = False, parameters=None) -> SimpleNamespace:
    inspection = SimpleNamespace(
        toplevel="tb_demo",
        eda_tool=eda_tool,
        parameters=parameters or {},
        flow_options={"cocotb_module": "test_demo"} if cocotb else {},
        tool_options={},
        inputs=(),
    )
    inspection.inspect = lambda _handle: inspection
    return inspection


def _preview(root: Path, inspection: SimpleNamespace, selection):
    execution = SimulationExecution(invoke=MagicMock(), options=SimulationOptions())
    with (
        patch("booley.flows.sim.execution.engine.TargetCatalog.build", return_value=inspection),
        patch(
            "booley.flows.sim.execution.engine.fusesoc_registry.setup_command_for_handle",
            return_value=["setup"],
        ),
    ):
        return execution.preview(_handle(root), selection)


def _work(root: Path, inspection: SimpleNamespace, names: tuple[str, ...], runs=None):
    execution = SimulationExecution(invoke=MagicMock(), options=SimulationOptions())
    cocotb = bool(inspection.flow_options)
    return _preview_work(execution, _handle(root), inspection, names, cocotb, "build", runs)


def _plusargs(command: tuple[str, ...]) -> list[str]:
    """The ``--plusarg=`` values of the adapter invocation, in order."""
    words = command[-1].split(" && ")[-1].split()
    return [word.removeprefix("--plusarg=") for word in words if word.startswith("--plusarg=")]


class TestVerilatorNativeSeed:
    def test_default_run_gets_the_default_seed_after_select(self, tmp_path: Path):
        root = _project(tmp_path, '[sim]\ntests = ["reset", "stress"]\nselect = "+test={name}"\n')
        preview = _preview(root, _inspection("verilator"), NamedTests(("reset", "stress")))
        assert [_plusargs(command) for command in preview.commands] == [
            ["test=reset", "verilator+seed+1"],
            ["test=stress", "verilator+seed+1"],
        ]
        assert [[run.name for run in group] for group in preview.run_groups] == [
            ["reset@1"],
            ["stress@1"],
        ]

    def test_argument_order_is_target_select_variant_seed(self, tmp_path: Path):
        root = _project(
            tmp_path,
            '[sim]\ntests = ["reset", "stress"]\nselect = "+test={name}"\ndefault_seed = 7\n'
            + _VARIANTS,
        )
        parameters = {"depth": {"paramtype": "plusarg", "default": 4}}
        preview = _preview(
            root, _inspection("verilator", parameters=parameters), NamedTests(("stress",))
        )
        assert _plusargs(preview.commands[0]) == [
            "depth=4",
            "test=stress",
            "iters=1000",
            "verilator+seed+7",
        ]

    def test_seed_template_is_passed_in_addition_to_the_native_option(self, tmp_path: Path):
        root = _project(tmp_path, '[sim]\ntests = ["reset"]\nseed = "+seed={seed}"\n')
        preview = _preview(root, _inspection("verilator"), NamedTests(("reset",)))
        assert _plusargs(preview.commands[0]) == ["test_id=0", "verilator+seed+1", "seed=1"]

    def test_seed_template_replaces_a_target_plusarg_of_the_same_name(self, tmp_path: Path):
        root = _project(tmp_path, '[sim]\ntests = ["reset"]\nseed = "+seed={seed}"\n')
        parameters = {"seed": {"paramtype": "plusarg", "default": 99}}
        preview = _preview(
            root, _inspection("verilator", parameters=parameters), NamedTests(("reset",))
        )
        assert _plusargs(preview.commands[0]) == ["test_id=0", "verilator+seed+1", "seed=1"]

    def test_unnamed_invocation_is_seeded_too(self, tmp_path: Path):
        preview = _preview(tmp_path, _inspection("verilator"), DefaultSelection())
        assert _plusargs(preview.commands[0]) == ["verilator+seed+1"]
        assert [[run.name for run in group] for group in preview.run_groups] == [["<default>@1"]]

    def test_named_test_runs_carry_their_variant_and_seed(self, tmp_path: Path):
        root = _project(
            tmp_path, '[sim]\ntests = ["reset", "stress"]\nselect = "+test={name}"\n' + _VARIANTS
        )
        inspection = _inspection("verilator")
        plan = target_test_plan(_handle(root), "verilator", {})
        runs = (plan.run("stress", "soak", 42), plan.run("stress", "fault", 2**31 - 1))
        preview = _preview(root, inspection, SelectedTestRuns(runs))
        assert preview.groups == (("stress",), ("stress",))
        assert [_plusargs(command) for command in preview.commands] == [
            ["test=stress", "iters=100000", "verilator+seed+42"],
            ["test=stress", "err_rate=5", "verilator+seed+2147483647"],
        ]


class TestIcarus:
    def test_without_a_template_the_run_is_unseeded(self, tmp_path: Path):
        root = _project(tmp_path, '[sim]\ntests = ["reset"]\n')
        preview = _preview(root, _inspection("icarus"), NamedTests(("reset",)))
        assert _plusargs(preview.commands[0]) == ["test_id=0"]
        assert [[run.name for run in group] for group in preview.run_groups] == [["reset"]]

    def test_iverilog_spelling_is_the_same_family(self, tmp_path: Path):
        root = _project(tmp_path, '[sim]\ntests = ["reset"]\nseed = "+seed={seed}"\n')
        preview = _preview(root, _inspection("iverilog"), NamedTests(("reset",)))
        assert _plusargs(preview.commands[0]) == ["test_id=0", "seed=1"]

    def test_getopt_seed_template_is_forwarded_verbatim(self, tmp_path: Path):
        root = _project(tmp_path, '[sim]\ntests = ["reset"]\nseed = "--seed={seed}"\n')
        work = _work(root, _inspection("icarus"), ("reset",))
        assert work.plusargs == ("test_id=0", "--seed=1")


class TestVariantEnvironment:
    def test_variant_environment_reaches_the_launch_only(self, tmp_path: Path):
        root = _project(
            tmp_path,
            '[sim]\ntests = ["stress"]\nenv = { FLAVOR = "a" }\n' + _VARIANTS,
        )
        inspection = _inspection("verilator")
        plan = target_test_plan(_handle(root), "verilator", {})
        work = _work(root, inspection, ("stress",), (plan.run("stress", "fault"),))
        assert work.environment == (("FAULT_MODE", "1"),)
        assert launch_environment({"FLAVOR": "a"}, work) == {"FLAVOR": "a", "FAULT_MODE": "1"}
        assert work.cocotb_seed is None

    def test_default_variant_has_no_extra_environment(self, tmp_path: Path):
        root = _project(tmp_path, '[sim]\ntests = ["stress"]\n' + _VARIANTS)
        assert _work(root, _inspection("verilator"), ("stress",)).environment == ()


_COCOTB_TESTS = """
[sim]
tests = ["test_a", "test_b", "test_c"]

[sim.variants.test_c]
default = "plain"
plain = []
slow = { env = { SPEED = "slow" } }
"""


class TestCocotb:
    def test_batch_is_seeded_through_the_adapter_not_a_plusarg(self, tmp_path: Path):
        root = _project(tmp_path, _COCOTB_TESTS)
        preview = _preview(
            root, _inspection("icarus", cocotb=True), NamedTests(("test_a", "test_b", "test_c"))
        )
        assert preview.groups == (("test_a", "test_b", "test_c"),)
        command = preview.commands[0][-1]
        assert "--seed 1" in command
        assert "verilator+seed" not in command

    def test_default_variants_that_differ_in_environment_split_the_batch(self, tmp_path: Path):
        root = _project(tmp_path, _COCOTB_TESTS.replace('default = "plain"', 'default = "slow"'))
        preview = _preview(
            root, _inspection("icarus", cocotb=True), NamedTests(("test_a", "test_b", "test_c"))
        )
        assert preview.groups == (("test_a", "test_b"), ("test_c",))

    def test_named_runs_batch_by_environment_and_seed(self, tmp_path: Path):
        root = _project(tmp_path, _COCOTB_TESTS)
        inspection = _inspection("icarus", cocotb=True)
        plan = target_test_plan(_handle(root), "cocotb", {})
        runs = (
            plan.run("test_a"),
            plan.run("test_c", "slow"),
            plan.run("test_b", seed=5),
            plan.run("test_c"),
        )
        preview = _preview(root, inspection, SelectedTestRuns(runs))
        assert preview.groups == (("test_a", "test_c"), ("test_c",), ("test_b",))
        assert ["--seed 5" in command[-1] for command in preview.commands] == [False, False, True]

    def test_work_carries_the_seed_and_the_variant_environment(self, tmp_path: Path):
        root = _project(tmp_path, _COCOTB_TESTS)
        inspection = _inspection("icarus", cocotb=True)
        plan = target_test_plan(_handle(root), "cocotb", {})
        work = _work(root, inspection, ("test_c",), (plan.run("test_c", "slow", 9),))
        assert work.cocotb_seed == 9
        assert work.environment == (("SPEED", "slow"),)


class TestDeclarationsTheTargetCannotApply:
    def test_cocotb_variant_arguments_stop_the_launch(self, tmp_path: Path):
        root = _project(tmp_path, '[sim]\ntests = ["stress"]\n' + _VARIANTS)
        with pytest.raises(ValueError, match="carry environment only"):
            _work(root, _inspection("icarus", cocotb=True), ("stress",))

    def test_runs_of_one_process_must_agree(self, tmp_path: Path):
        root = _project(tmp_path, _COCOTB_TESTS)
        plan = target_test_plan(_handle(root), "cocotb", {})
        with pytest.raises(ValueError, match="cannot share one simulator process"):
            _work(
                root,
                _inspection("icarus", cocotb=True),
                ("test_a", "test_b"),
                (plan.run("test_a"), plan.run("test_b", seed=5)),
            )


class TestEvidenceGroups:
    def test_each_hdl_test_run_has_its_own_evidence_directory(self, tmp_path: Path):
        root = _project(tmp_path, '[sim]\ntests = ["stress"]\n' + _VARIANTS)
        plan = target_test_plan(_handle(root), "verilator", {})
        inspection = _inspection("verilator")
        groups = [
            _work(root, inspection, ("stress",), None if run is None else (run,)).artifact_group
            for run in (None, plan.run("stress", "soak"), plan.run("stress", seed=7))
        ]
        assert groups == ["stress+short@1", "stress+soak@1", "stress+short@7"]

    def test_unseeded_default_run_keeps_its_bare_test_directory(self, tmp_path: Path):
        root = _project(tmp_path, '[sim]\ntests = ["reset"]\n')
        assert _work(root, _inspection("icarus"), ("reset",)).artifact_group == "reset"

    def test_unnamed_invocation_has_no_group_of_its_own(self, tmp_path: Path):
        assert _work(tmp_path, _inspection("verilator"), ()).artifact_group == ""

    def test_cocotb_batches_never_share_an_evidence_directory(self, tmp_path: Path):
        root = _project(tmp_path, _COCOTB_TESTS)
        inspection = _inspection("icarus", cocotb=True)
        plan = target_test_plan(_handle(root), "cocotb", {})
        slow = plan.run("test_c", "slow")
        groups = [
            _work(root, inspection, names, runs).artifact_group
            for names, runs in (
                (("test_a", "test_b"), None),
                (("test_b",), (plan.run("test_b", seed=5),)),
                (("test_c",), (slow,)),
                (("test_c",), (plan.run("test_c", "slow", 5),)),
            )
        ]
        assert groups[0] == "batch"
        assert len(set(groups)) == 4
        assert all(re.fullmatch(r"batch-[0-9a-f]{12}", group) for group in groups[1:])

    def test_two_variants_of_one_test_with_one_environment_get_two_directories(
        self, tmp_path: Path
    ):
        """They need two batches (a batch selects by Test name), so two directories."""
        root = _project(
            tmp_path,
            '[sim]\ntests = ["test_a"]\n'
            '[sim.variants.test_a]\ndefault = "one"\none = []\ntwo = []\n'
            'three = { env = { MODE = "x" } }\nfour = { env = { MODE = "x" } }\n',
        )
        inspection = _inspection("icarus", cocotb=True)
        plan = target_test_plan(_handle(root), "cocotb", {})
        runs = [plan.run("test_a", variant) for variant in ("one", "two", "three", "four")]
        assert {(run.env, run.seed) for run in runs[:2]} == {((), 1)}

        def groups() -> list[str]:
            return [_work(root, inspection, ("test_a",), (run,)).artifact_group for run in runs]

        assert groups()[0] == "batch"
        assert len(set(groups())) == 4
        # Deterministic: the same batch is always the same directory.
        assert groups() == groups()
