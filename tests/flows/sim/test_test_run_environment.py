"""Pre-Sim variables, evidence paths, watchdog sizing and build identity (ADR 0072)."""

from __future__ import annotations

import dataclasses
from pathlib import Path, PurePosixPath, PureWindowsPath
from unittest.mock import MagicMock, patch

import pytest

from booley.flows import artifacts as flow_artifacts
from booley.flows.sim.execution import SimulationOptions
from booley.flows.sim.execution.artifacts import artifact_path_component
from booley.flows.sim.execution.engine import (
    _adapter_identity,
    prepare_simulation_work,
    simulation_target_environment,
    target_test_plan,
)
from booley.flows.sim.execution.pre_sim import (
    TEST_RUN_VARIABLES,
    _pre_sim_environment,
    test_run_environment,
)
from booley.flows.sim.flow import _resolve_sim_campaign_work_units
from booley.flows.test_runs import build_test_run
from booley.targets.catalog import TargetCatalog
from tests.flows.sim.test_execution_engine import _handle, _prepared
from tests.flows.sim.test_test_run_launch import _VARIANTS, _project
from tests.target_test_support import write_two_core_sim_project

_SOAK = build_test_run("stress", variant="soak", seed=42, args=("+iters=9",))


class TestPreSimVariables:
    def test_single_run_names_its_variant_and_seed(self):
        assert test_run_environment((_SOAK,)) == {
            "BOOLEY_TEST_RUN_NAMES": "stress+soak@42",
            "BOOLEY_TEST_RUN_NAME": "stress+soak@42",
            "BOOLEY_TEST_VARIANT": "soak",
            "BOOLEY_TEST_SEED": "42",
        }

    def test_run_without_variant_or_seed_sets_neither(self):
        assert test_run_environment((build_test_run("reset"),)) == {
            "BOOLEY_TEST_RUN_NAMES": "reset",
            "BOOLEY_TEST_RUN_NAME": "reset",
        }

    def test_unnamed_invocation_is_named_by_its_display_name(self):
        assert test_run_environment((build_test_run(None, seed=1),)) == {
            "BOOLEY_TEST_RUN_NAMES": "<default>@1",
            "BOOLEY_TEST_RUN_NAME": "<default>@1",
            "BOOLEY_TEST_SEED": "1",
        }

    def test_cocotb_batch_lists_every_run_and_no_single_run_variable(self):
        batch = (build_test_run("test_a", seed=5), build_test_run("test_b", seed=5))
        assert test_run_environment(batch) == {"BOOLEY_TEST_RUN_NAMES": "test_a@5 test_b@5"}

    def test_no_resolved_runs_set_nothing(self):
        assert test_run_environment(()) == {}

    def _environment(self, tmp_path: Path, monkeypatch, names, runs):
        for name in TEST_RUN_VARIABLES:
            monkeypatch.setenv(name, "stale")
        return _pre_sim_environment(
            _handle(tmp_path),
            test_names=names,
            build_root=tmp_path / "build",
            eda_tool="verilator",
            simulator_environment={"FLAVOR": "a"},
            run_cwd=".",
            expose_build_root=True,
            test_runs=runs,
        )

    def test_hook_keeps_bare_test_names_beside_the_run_names(self, tmp_path, monkeypatch):
        env = self._environment(tmp_path, monkeypatch, ("stress",), (_SOAK,))
        assert env["BOOLEY_TEST_NAME"] == env["BOOLEY_TEST_NAMES"] == "stress"
        assert env["BOOLEY_TEST_RUN_NAME"] == env["BOOLEY_TEST_RUN_NAMES"] == "stress+soak@42"
        assert (env["BOOLEY_TEST_VARIANT"], env["BOOLEY_TEST_SEED"]) == ("soak", "42")
        assert env["FLAVOR"] == "a"

    def test_each_hdl_run_of_a_multi_run_request_gets_its_own_values(self, tmp_path, monkeypatch):
        runs = (build_test_run("stress", seed=3), build_test_run("stress", seed=4))
        seen = [
            self._environment(tmp_path, monkeypatch, ("stress",), (run,))["BOOLEY_TEST_RUN_NAME"]
            for run in runs
        ]
        assert seen == ["stress@3", "stress@4"]

    def test_inherited_values_never_reach_a_hook(self, tmp_path, monkeypatch):
        env = self._environment(tmp_path, monkeypatch, ("reset",), (build_test_run("reset"),))
        assert "BOOLEY_TEST_VARIANT" not in env
        assert "BOOLEY_TEST_SEED" not in env
        batch = (build_test_run("a", seed=5), build_test_run("b", seed=5))
        env = self._environment(tmp_path, monkeypatch, ("a", "b"), batch)
        assert "BOOLEY_TEST_RUN_NAME" not in env
        assert env["BOOLEY_TEST_RUN_NAMES"] == "a@5 b@5"
        env = self._environment(tmp_path, monkeypatch, ("a",), ())
        assert not set(TEST_RUN_VARIABLES) & set(env)


class TestEvidencePaths:
    @pytest.mark.parametrize("name", ["stress+soak@42", "reset@1", "batch+0a1b2c3d@7", "plain"])
    def test_test_run_names_are_directory_names_as_written(self, name: str):
        assert artifact_path_component(name) == name
        assert flow_artifacts.path_component(name) == name

    @pytest.mark.parametrize("name", ["<default>@1", "a/b@1", "..", "+lead", "@lead", "a b"])
    def test_other_names_are_still_hashed(self, name: str):
        assert artifact_path_component(name).startswith("~sha256-")

    def test_run_directory_is_one_legal_component_on_windows_and_posix(self):
        name = artifact_path_component("stress+soak@42")
        for flavour in (PureWindowsPath, PurePosixPath):
            path = flavour("reports", "sim", "1", "tests", name, "run.log")
            assert path.parts[-2] == "stress+soak@42"
        # None of the characters Windows reserves in a file name.
        assert not set(name) & set('<>:"/\\|?*')

    def test_published_evidence_lands_in_the_run_directory(self, tmp_path: Path):
        relative = flow_artifacts.publish_bytes(
            tmp_path, ("sim_core", "tests", "stress+soak@42", "run.log"), b"log", work_dir=tmp_path
        )
        assert relative == "sim_core/tests/stress+soak@42/run.log"
        assert (tmp_path / relative).read_bytes() == b"log"


def _work(root: Path, run):
    handle = _handle(root, eda_tool="verilator")
    prepared = dataclasses.replace(_prepared(handle, cocotb=False), eda_tool="verilator")
    identity = _adapter_identity(handle, prepared, ("stress",), "verilator")
    return prepared, prepare_simulation_work(
        handle,
        prepared,
        identity,
        SimulationOptions(),
        trace=False,
        trace_mode="vcd_fifo",
        test_runs=None if run is None else (run,),
    )


class TestRunTimeInputsAreNotBuildInputs:
    def test_two_variants_and_two_seeds_share_one_prepared_build(self, tmp_path: Path):
        root = _project(
            tmp_path, '[sim]\ntests = ["stress"]\nselect = "+test={name}"\n' + _VARIANTS
        )
        plan = target_test_plan(_handle(root, eda_tool="verilator"), "verilator", {})
        works = {}
        builds = []
        for run in (None, plan.run("stress", "soak"), plan.run("stress", "fault", 7)):
            prepared, work = _work(root, run)
            builds.append(prepared)
            works[work.test_runs[0].name] = work
        assert list(works) == ["stress+short@1", "stress+soak@1", "stress+fault@7"]
        # The build description is identical; only run-time fields differ.
        assert builds[0] == builds[1] == builds[2]
        run_time = {"plusargs", "environment", "test_runs", "artifact_group"}
        transport = {"adapter_result_path", "attempt_token"}
        baseline = dataclasses.asdict(works["stress+short@1"])
        for name, work in works.items():
            changed = {
                key for key, value in dataclasses.asdict(work).items() if value != baseline[key]
            }
            assert changed <= run_time | transport, name
        assert works["stress+fault@7"].environment == (("FAULT_MODE", "1"),)
        assert works["stress+soak@1"].artifact_group == "stress+soak@1"

    def test_two_variants_and_two_seeds_have_one_real_reuse_key(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ):
        """The Simulator Bundle key itself is compared, not only the build description.

        The key is the real ``SimulationBuildSession.reusable_key`` of the
        ordinary Icarus recipe (only the installed-tool probes are pinned).
        It is the same for the default Test Run, another Test Variant, a
        Test Variant with its own environment and other seeds, and it does
        change with the Target's own environment, so equality is not vacuous.
        """
        from booley.flows.sim import build_session
        from booley.flows.sim.build_session import SimulationBuildSession

        root = _project(
            tmp_path,
            '[sim]\ntests = ["stress"]\nselect = "+test={name}"\nseed = "+seed={seed}"\n'
            + _VARIANTS,
        )
        core = root / "demo.core"
        core.write_text("CAPI=2:\nname: acme:lib:demo:1\n", encoding="utf-8")
        monkeypatch.setattr(build_session, "_eligible_core_file", lambda *_args: core)
        monkeypatch.setattr(build_session, "_icarus_tool_identity", lambda: {"iverilog": "12"})
        monkeypatch.setattr(build_session, "_python_recipe_identity", lambda: {"recipe": "1"})
        handle = _handle(root, eda_tool="icarus")
        plan = target_test_plan(handle, "icarus", {})
        runs = (
            None,
            plan.run("stress", "soak"),
            plan.run("stress", "fault", 7),
            plan.run("stress", seed=9),
        )
        inputs = {"rtl/demo.sv": "sha256:" + "1" * 64}
        keys: dict[str, str | None] = {}
        with SimulationBuildSession(handle) as session:
            for run in runs:
                prepared = _prepared(handle, cocotb=False)
                identity = _adapter_identity(handle, prepared, ("stress",), "icarus")
                work = prepare_simulation_work(
                    handle,
                    prepared,
                    identity,
                    SimulationOptions(),
                    trace=False,
                    trace_mode="vcd_fifo",
                    test_runs=None if run is None else (run,),
                )
                keys[work.test_runs[0].name] = session.reusable_key(prepared, inputs, hooks=False)
            flavored = dataclasses.replace(
                _prepared(handle, cocotb=False), environment={"FLAVOR": "wide"}
            )
            other = session.reusable_key(flavored, inputs, hooks=False)

        assert list(keys) == [
            "stress+short@1",
            "stress+soak@1",
            "stress+fault@7",
            "stress+short@9",
        ]
        (key,) = set(keys.values())
        assert key is not None and len(key) == 64
        assert other is not None and other != key

    def test_the_build_is_prepared_before_any_test_run_is_known(self, tmp_path: Path):
        """``prepare_simulation_build`` gets the Target's environment, never a Variant's."""
        from booley.flows.sim.execution import SimulationExecution, engine

        root = _project(
            tmp_path, '[sim]\ntests = ["stress"]\nenv = { FLAVOR = "a" }\n' + _VARIANTS
        )
        handle = _handle(root, eda_tool="verilator")
        execution = SimulationExecution(invoke=MagicMock(), options=SimulationOptions())
        with patch.object(engine, "prepare_simulation_build") as prepare:
            prepare.return_value = _prepared(handle, cocotb=False)
            execution._build_session = MagicMock()
            execution._prepare_build(handle)
        assert prepare.call_args.kwargs["environment"] == {"FLAVOR": "a"}
        assert "test_runs" not in prepare.call_args.kwargs


class TestWatchdogSizing:
    @pytest.fixture
    def project(self, tmp_path: Path) -> Path:
        return _project(tmp_path, '[sim]\ntests = ["reset", "stress"]\n' + _VARIANTS)

    @pytest.mark.parametrize(
        ("selectors", "units"),
        [
            (None, 2),
            (["stress"], 1),
            (["stress+soak@7", "reset"], 2),
            (["stress@x40"], 40),
            (["stress@x3", "stress+soak@x2", "reset@9"], 6),
            (["unknown@x5"], 1),
            (["stress@x1@2"], 1),
        ],
    )
    def test_hdl_target_counts_one_process_per_test_run(self, project, selectors, units):
        with patch("booley.flows.sim.flow._target_is_cocotb", return_value=False):
            assert _resolve_sim_campaign_work_units(project, "sim", selectors) == units

    @pytest.fixture
    def cocotb_project(self, tmp_path: Path) -> Path:
        """Cocotb Test Variants carry environment only; ``stress`` defaults to none."""
        return _project(
            tmp_path,
            '[sim]\ntests = ["reset", "stress", "idle"]\n'
            "[sim.variants.stress]\n"
            'default = "short"\nshort = []\nsoak = { env = { MODE = "soak" } }\n',
        )

    @pytest.mark.parametrize(
        ("selectors", "seed", "units"),
        [
            (None, None, 1),
            (["reset", "stress"], None, 1),
            (["stress@x4"], None, 4),
            (["reset@3", "stress+soak"], None, 2),
            # One seed for every run keeps one batch; a drawn seed per run does not.
            (None, 5, 1),
            (None, "random", 3),
            (["reset", "stress"], "random", 2),
            (["stress+soak", "reset", "idle"], None, 2),
            (["stress@x2", "reset@x2", "idle"], None, 5),
        ],
    )
    def test_cocotb_target_counts_its_resolved_batches(
        self, cocotb_project, selectors, seed, units
    ):
        with patch("booley.flows.sim.flow._target_is_cocotb", return_value=True):
            counted = _resolve_sim_campaign_work_units(cocotb_project, "sim", selectors, seed=seed)
        assert counted == units

    def test_cocotb_default_suite_whose_default_variants_differ_in_env_needs_several(
        self, tmp_path: Path
    ):
        """No selector at all: the default suite already splits by environment."""
        project = _project(
            tmp_path,
            '[sim]\ntests = ["a", "b", "c"]\n'
            '[sim.variants.a]\ndefault = "x"\nx = { env = { MODE = "A" } }\n'
            '[sim.variants.b]\ndefault = "y"\ny = { env = { MODE = "B" } }\n',
        )
        with patch("booley.flows.sim.flow._target_is_cocotb", return_value=True):
            assert _resolve_sim_campaign_work_units(project, "sim") == 3
            assert _resolve_sim_campaign_work_units(project, "sim", ["a", "b"]) == 2
            assert _resolve_sim_campaign_work_units(project, "sim", ["c"]) == 1

    @pytest.mark.parametrize(("seed", "units"), [("random", 2), (9, 2), ("bogus", 2), (0, 2)])
    def test_hdl_target_counts_do_not_depend_on_the_seed(self, project, seed, units):
        with patch("booley.flows.sim.flow._target_is_cocotb", return_value=False):
            assert _resolve_sim_campaign_work_units(project, "sim", seed=seed) == units

    def test_a_request_the_flow_refuses_is_counted_from_the_registry(self, cocotb_project):
        with patch("booley.flows.sim.flow._target_is_cocotb", return_value=True):
            assert _resolve_sim_campaign_work_units(cocotb_project, "sim", ["nope+x@x3"]) == 1
            assert _resolve_sim_campaign_work_units(cocotb_project, "sim", ["stress+nope"]) == 1


def test_target_environment_is_never_another_cores_env(tmp_path):
    write_two_core_sim_project(
        tmp_path,
        '["vendor:a:x#sim"]\nenv = { WHO = "a" }\n["vendor:b:y#sim"]\nenv = { WHO = "b" }\n',
    )
    catalog = TargetCatalog.build(tmp_path)
    assert simulation_target_environment(catalog.select("x#sim")) == {"WHO": "a"}
    assert simulation_target_environment(catalog.select("y#sim")) == {"WHO": "b"}
