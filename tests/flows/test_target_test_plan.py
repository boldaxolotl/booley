"""TargetTestPlan: which Test Runs a Target runs and how each is seeded (ADR 0072)."""

from __future__ import annotations

import random
from pathlib import Path

import pytest

from booley.config.project_config import normalize_tests_toml
from booley.config.tests_toml_rules import SeedConfigurationError, TestVariantError
from booley.flows.target_test_plan import (
    MAX_TEST_RUNS_PER_REQUEST,
    SeedMechanism,
    TargetTestPlan,
    UnknownTestError,
    batch_test_runs,
    build_target_test_plan,
    seed_mechanism,
)
from booley.flows.target_test_plan_loading import load_target_test_plan
from booley.flows.test_runs import (
    DEFAULT_SEED,
    MAX_SEED,
    TestRunLimitError,
    TestRunNameError,
    UnknownVariantError,
    UnseededTargetError,
    build_test_run,
)

_SECTION = {
    "sim": {
        "select": "+test={name}",
        "tests": ["reset", "basic", "stress"],
        "skip": ["basic"],
        "variants": {
            "stress": {
                "default": "short",
                "short": ["+iters=1000"],
                "soak": ["+iters=100000"],
                "fault": {"args": ["+err_rate=5"], "env": {"FAULT_MODE": "1"}},
            }
        },
    }
}


def _plan(adapter: str = "verilator", **extra: object) -> TargetTestPlan:
    section = normalize_tests_toml({"sim": {**_SECTION["sim"], **extra}})["sim"]
    return build_target_test_plan("sim", section, adapter=adapter)


def _fixed_draw(*seeds: int):
    """A draw that hands out *seeds* in order and records what it was asked."""
    pending = list(seeds)
    calls: list[tuple[int, frozenset[int]]] = []

    def draw(count: int, *, exclude=()) -> tuple[int, ...]:
        calls.append((count, frozenset(exclude)))
        return tuple(pending.pop(0) for _ in range(count))

    draw.calls = calls  # type: ignore[attr-defined]
    return draw


class TestSeedMechanism:
    def test_verilator_is_native_and_never_passes_seed_zero(self):
        mechanism = seed_mechanism("verilator", None, where="tests.toml [sim]")
        assert mechanism == SeedMechanism("verilator_native")
        assert mechanism.arguments(1) == ("+verilator+seed+1",)
        assert mechanism.arguments(MAX_SEED) == (f"+verilator+seed+{MAX_SEED}",)

    def test_verilator_with_template_gets_both_from_one_seed(self):
        mechanism = seed_mechanism("verilator", "+seed={seed}", where="tests.toml [sim]")
        assert mechanism.arguments(42) == ("+verilator+seed+42", "+seed=42")

    def test_icarus_is_seeded_only_through_a_template(self):
        assert seed_mechanism("icarus", None, where="w") == SeedMechanism("none")
        templated = seed_mechanism("icarus", "+seed={seed}", where="w")
        assert templated == SeedMechanism("template", "+seed={seed}")
        assert templated.arguments(7) == ("+seed=7",)

    def test_cocotb_is_seeded_through_its_environment(self):
        mechanism = seed_mechanism("cocotb", None, where="w")
        assert mechanism.seeded
        assert mechanism.arguments(7) == ()

    @pytest.mark.parametrize("adapter", ["vcs", "xcelium"])
    @pytest.mark.parametrize("template", [None, "+seed={seed}", "-svseed={seed}"])
    def test_a_simulator_booley_does_not_seed_is_unseeded_even_with_a_template(
        self, adapter: str, template: str | None
    ):
        """Regression: a VCS Target with a seed template reported itself seeded.

        The Simulation Flow renders a seed template for Icarus and Verilator
        only, so such a Target would have recorded seeds nothing applied.
        """
        mechanism = seed_mechanism(adapter, template, where="tests.toml [sim]")
        assert (mechanism.kind, mechanism.seeded) == ("none", False)
        assert mechanism.arguments(7) == ()

        section = {"tests": ["reset"], **({"seed": template} if template else {})}
        plan = build_target_test_plan(
            "sim", normalize_tests_toml({"sim": section})["sim"], adapter=adapter
        )
        assert plan.default_seed is None
        assert [run.name for run in plan.default_runs()] == ["reset"]
        assert plan.unseeded_cause() == f"Booley applies no seed to a {adapter} Test Run"
        for selectors, seed in (
            (None, 5),
            (None, "random"),
            (["reset@5"], None),
            (["reset@x2"], None),
        ):
            with pytest.raises(UnseededTargetError) as excinfo:
                plan.resolve(selectors, seed=seed)
            assert str(excinfo.value) == (
                f"Target 'sim' is unseeded: Booley applies no seed to a {adapter} Test "
                "Run, with or without a seed argument template; drop the seed"
            )
        with pytest.raises(UnseededTargetError, match="default_seed cannot apply"):
            build_target_test_plan(
                "sim",
                normalize_tests_toml({"sim": {**section, "default_seed": 3}})["sim"],
                adapter=adapter,
            )

    def test_cocotb_refuses_a_seed_template(self):
        with pytest.raises(SeedConfigurationError) as excinfo:
            seed_mechanism("cocotb", "+seed={seed}", where="tests.toml [sim]")
        assert str(excinfo.value) == (
            "tests.toml [sim].seed is not allowed on a Cocotb Target: Booley seeds "
            "cocotb through its seed variable; remove the seed template"
        )

    def test_unseeded_run_has_no_arguments(self):
        assert SeedMechanism("none").arguments(None) == ()
        assert SeedMechanism("verilator_native").arguments(None) == ()


class TestBuildPlan:
    def test_default_seed_is_the_booley_constant(self):
        assert _plan().default_seed == DEFAULT_SEED == 1

    def test_target_overrides_the_default_seed(self):
        assert _plan(default_seed=7).default_seed == 7

    def test_unseeded_target_has_no_default_seed(self):
        plan = _plan("icarus")
        assert plan.default_seed is None
        assert not plan.mechanism.seeded

    def test_unseeded_target_refuses_default_seed(self):
        with pytest.raises(UnseededTargetError) as excinfo:
            _plan("icarus", default_seed=7)
        message = str(excinfo.value)
        assert message.startswith("tests.toml [sim].default_seed cannot apply. Target 'sim'")
        assert 'seed = "+seed={seed}"' in message

    def test_cocotb_variant_with_arguments_is_refused(self):
        with pytest.raises(TestVariantError) as excinfo:
            _plan("cocotb")
        assert str(excinfo.value) == (
            "tests.toml [sim].variants.stress.short carries arguments, but a Cocotb "
            "Target's Test Variants carry environment only; move the values into env"
        )

    def test_variant_overriding_a_target_plusarg_is_refused(self):
        section = normalize_tests_toml(_SECTION)["sim"]
        with pytest.raises(TestVariantError) as excinfo:
            build_target_test_plan("sim", section, adapter="verilator", target_plusargs=("iters",))
        assert str(excinfo.value) == (
            "tests.toml [sim].variants.stress.short argument '+iters=1000' overrides a "
            "plusarg the Target declares; the Target owns its parameter values"
        )

    def test_target_without_a_section_runs_its_unnamed_invocation(self):
        plan = build_target_test_plan("sim", None, adapter="verilator")
        assert plan.tests == ()
        assert [run.name for run in plan.default_runs()] == ["<default>@1"]

    def test_load_reads_the_checkout_and_tolerates_a_qualified_target(self, tmp_path: Path):
        project = tmp_path / ".booley"
        project.mkdir()
        (project / "booley.toml").write_text('[project]\nname = "demo"\n', encoding="utf-8")
        (project / "tests.toml").write_text(
            '[sim]\ntests = ["reset"]\ndefault_seed = 9\n', encoding="utf-8"
        )
        plan = load_target_test_plan(
            tmp_path, "lib:ip:core:1#sim", identity="lib:ip:core:1#sim", adapter="verilator"
        )
        assert plan.tests == ("reset",)
        assert plan.default_seed == 9


class TestDefaultRuns:
    def test_default_suite_runs_default_variants_at_the_default_seed(self):
        runs = _plan().default_runs()
        assert [run.name for run in runs] == ["reset@1", "stress+short@1"]
        assert runs[1].args == ("+iters=1000",)

    def test_unseeded_names_carry_no_seed(self):
        runs = _plan("icarus").default_runs()
        assert [run.name for run in runs] == ["reset", "stress+short"]
        assert [run.seed for run in runs] == [None, None]

    def test_is_default_run(self):
        plan = _plan()
        assert plan.is_default_run(plan.run("stress"))
        assert not plan.is_default_run(plan.run("stress", "soak"))
        assert not plan.is_default_run(plan.run("stress", seed=2))
        assert not plan.is_default_run(build_test_run("gone", seed=1))


class TestResolve:
    def test_selectors_keep_request_order_and_may_name_a_skipped_test(self):
        runs = _plan().resolve(["stress+soak", "basic", "reset@5"])
        assert [run.name for run in runs] == ["stress+soak@1", "basic@1", "reset@5"]

    def test_seed_applies_to_every_selected_run(self):
        assert [run.name for run in _plan().resolve(None, seed=9)] == [
            "reset@9",
            "stress+short@9",
        ]

    def test_random_draws_one_seed_per_test_run(self):
        draw = _fixed_draw(11, 22)
        runs = _plan().resolve(None, seed="random", draw=draw)
        assert [run.name for run in runs] == ["reset@11", "stress+short@22"]
        assert [count for count, _ in draw.calls] == [1, 1]

    def test_count_draws_distinct_seeds_excluding_pinned_ones(self):
        draw = _fixed_draw(3, 4, 5)
        runs = _plan().resolve(["stress+soak@9", "stress+soak@x3"], draw=draw)
        assert [run.name for run in runs] == [
            "stress+soak@9",
            "stress+soak@3",
            "stress+soak@4",
            "stress+soak@5",
        ]
        assert draw.calls == [(3, frozenset({9}))]

    def test_real_draw_gives_distinct_seeds_in_range(self):
        def draw(count: int, *, exclude=()):
            from booley.flows.test_runs import draw_seeds

            return draw_seeds(count, exclude=exclude, rng=random.Random(1233))

        runs = _plan().resolve(["reset@x50"], draw=draw)
        seeds = [run.seed for run in runs]
        assert len(set(seeds)) == 50
        assert all(seed is not None and 1 <= seed <= MAX_SEED for seed in seeds)

    def test_seed_with_an_at_form_is_refused(self):
        for selector in ("reset@5", "reset@x2"):
            with pytest.raises(TestRunNameError, match="--seed cannot be combined"):
                _plan().resolve([selector], seed=3)

    def test_same_test_run_twice_is_refused(self):
        with pytest.raises(TestRunNameError) as excinfo:
            _plan().resolve(["stress", "stress+short@1"])
        assert str(excinfo.value) == (
            "the request selects Test Run 'stress+short@1' more than once; "
            "each Test Run runs once per request"
        )

    def test_two_variants_of_one_test_are_two_runs(self):
        runs = _plan().resolve(["stress+short", "stress+soak"])
        assert [run.name for run in runs] == ["stress+short@1", "stress+soak@1"]
        assert runs[0].fingerprint != runs[1].fingerprint

    def test_unknown_test_and_variant(self):
        with pytest.raises(UnknownTestError, match="unknown Test 'nope' for Target 'sim'"):
            _plan().resolve(["nope"])
        with pytest.raises(UnknownVariantError, match="has no Test Variant 'nope'"):
            _plan().resolve(["stress+nope"])
        with pytest.raises(UnknownVariantError, match="declares no Test Variants"):
            _plan().resolve(["reset+short"])

    def test_more_than_the_limit_is_refused(self):
        draw = _fixed_draw(*range(1, 2002))
        selectors = ["reset@x1000", "stress@x1"]
        with pytest.raises(TestRunLimitError) as excinfo:
            _plan().resolve(selectors, draw=draw)
        assert str(excinfo.value) == (
            f"the request asks for more than {MAX_TEST_RUNS_PER_REQUEST} Test Runs of "
            "Target 'sim'; split it into several requests"
        )

    def test_exactly_the_limit_is_accepted(self):
        runs = _plan().resolve(["reset@x1000"], draw=_fixed_draw(*range(1, 1001)))
        assert len(runs) == MAX_TEST_RUNS_PER_REQUEST

    @pytest.mark.parametrize(
        ("selectors", "seed"),
        [(None, 5), (None, "random"), (["reset@5"], None), (["reset@x2"], None)],
    )
    def test_unseeded_target_refuses_every_seed_request(self, selectors, seed):
        with pytest.raises(UnseededTargetError) as excinfo:
            _plan("icarus").resolve(selectors, seed=seed)
        message = str(excinfo.value)
        assert message.startswith("Target 'sim' is unseeded: its simulator (icarus)")
        assert message.endswith("or drop the seed")

    def test_unnamed_invocation_takes_a_seed_but_no_name(self):
        plan = build_target_test_plan("sim", None, adapter="verilator")
        assert [run.name for run in plan.resolve(None, seed=4)] == ["<default>@4"]
        assert [run.name for run in plan.resolve(None, seed="random", draw=_fixed_draw(8))] == [
            "<default>@8"
        ]
        with pytest.raises(UnknownTestError):
            plan.resolve(["reset"])


class TestLaunch:
    def test_argument_order_is_select_variant_seed(self):
        plan = _plan(seed="+seed={seed}")
        launch = plan.launch((plan.run("stress", "fault", 42),))
        assert launch.arguments == (
            "+test=stress",
            "+err_rate=5",
            "+verilator+seed+42",
            "+seed=42",
        )
        assert launch.environment == (("FAULT_MODE", "1"),)
        assert launch.seed == 42

    def test_select_uses_the_registry_index(self):
        plan = _plan(select="+test_id={index}")
        assert plan.launch((plan.run("stress"),)).arguments[0] == "+test_id=2"

    def test_unseeded_launch_has_no_seed_argument(self):
        plan = _plan("icarus")
        launch = plan.launch((plan.run("stress", "soak"),))
        assert launch.arguments == ("+test=stress", "+iters=100000")
        assert launch.seed is None

    def test_unnamed_invocation_gets_only_the_seed(self):
        plan = build_target_test_plan("sim", None, adapter="verilator")
        assert plan.launch(plan.default_runs_for(())).arguments == ("+verilator+seed+1",)

    def test_batch_of_several_gets_no_arguments(self):
        section = normalize_tests_toml({"sim": {"tests": ["a", "b"]}})["sim"]
        plan = build_target_test_plan("sim", section, adapter="cocotb")
        launch = plan.launch(plan.default_runs_for(("a", "b")))
        assert launch.arguments == ()
        assert launch.seed == 1

    def test_runs_that_differ_never_share_a_process(self):
        plan = _plan()
        with pytest.raises(ValueError, match="cannot share one simulator process"):
            plan.launch((plan.run("reset", seed=1), plan.run("stress", seed=2)))
        with pytest.raises(ValueError, match="cannot share one simulator process"):
            plan.launch((plan.run("reset"), plan.run("stress", "fault")))


class TestBatching:
    def _cocotb_plan(self) -> TargetTestPlan:
        section = normalize_tests_toml(
            {
                "sim": {
                    "tests": ["a", "b", "c"],
                    "variants": {
                        "c": {
                            "default": "plain",
                            "plain": [],
                            "slow": {"env": {"SPEED": "slow"}},
                        }
                    },
                }
            }
        )["sim"]
        return build_target_test_plan("sim", section, adapter="cocotb")

    def test_hdl_runs_get_one_process_each(self):
        plan = _plan()
        runs = plan.resolve(["stress+short", "stress+soak", "reset@7"])
        assert batch_test_runs(runs, one_process_per_run=True) == tuple((run,) for run in runs)

    def test_cocotb_default_suite_is_one_batch(self):
        plan = self._cocotb_plan()
        batches = batch_test_runs(plan.default_runs(), one_process_per_run=False)
        assert [[run.name for run in batch] for batch in batches] == [["a@1", "b@1", "c+plain@1"]]

    def test_cocotb_batches_split_by_environment_and_seed(self):
        plan = self._cocotb_plan()
        runs = plan.resolve(["a", "c+slow", "b@5", "c+plain", "a@5"])
        batches = batch_test_runs(runs, one_process_per_run=False)
        assert [[run.name for run in batch] for batch in batches] == [
            ["a@1", "c+plain@1"],
            ["c+slow@1"],
            ["b@5", "a@5"],
        ]

    def test_a_test_appears_once_per_cocotb_batch(self):
        plan = self._cocotb_plan()
        runs = (plan.run("c", "plain"), build_test_run("c", variant="twin", seed=1))
        batches = batch_test_runs(runs, one_process_per_run=False)
        assert [len(batch) for batch in batches] == [1, 1]


def test_the_plan_module_reads_no_file_and_only_its_loader_does():
    """The module contract: pure policy here, ``tests.toml`` I/O in the loader."""
    import ast
    import inspect

    from booley.flows import target_test_plan, target_test_plan_loading

    def imported(module) -> set[str]:
        names: set[str] = set()
        for node in ast.walk(ast.parse(inspect.getsource(module))):
            if isinstance(node, ast.ImportFrom):
                names.update(alias.name for alias in node.names)
                names.add(node.module or "")
            elif isinstance(node, ast.Import):
                names.update(alias.name for alias in node.names)
        return names

    pure = imported(target_test_plan)
    assert not pure & {"load_test_configuration", "pathlib", "Path", "tomllib", "os", "subprocess"}
    assert "open(" not in inspect.getsource(target_test_plan)
    assert "load_test_configuration" in imported(target_test_plan_loading)
