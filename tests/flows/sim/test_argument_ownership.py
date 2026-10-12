"""Who owns a simulator option: one key at validation and at launch (ADR 0072).

A Test Variant argument may not set an option that ``select``, the seed or
the Target owns. Validation and launch rendering compare the same canonical
option key, so no spelling of an argument passes validation and then sets
another option at launch.
"""

from __future__ import annotations

import pytest

from booley.config.project_config import normalize_tests_toml
from booley.config.tests_toml_rules import (
    MAX_SEED,
    SeedConfigurationError,
    TestVariantError,
    adapter_argument,
    option_key,
    validate_seed_template,
)
from booley.flows.sim.execution.engine import simulation_plusargs
from booley.flows.target_test_plan import build_target_test_plan
from booley.flows.test_runs import build_test_run

_WIDTH = {"width": {"paramtype": "plusarg", "default": 8}}


def _section(args: list[str], **keys: object) -> dict[str, object]:
    section: dict[str, object] = {
        "tests": ["stress"],
        "select": "+test={name}",
        "variants": {"stress": {"default": "a", "a": args}},
        **keys,
    }
    return {"sim": section}


def _plan(args: list[str], *, plusargs: tuple[str, ...] = (), **keys: object):
    section = normalize_tests_toml(_section(args, **keys))["sim"]
    return build_target_test_plan("sim", section, adapter="verilator", target_plusargs=plusargs)


class TestOptionKey:
    @pytest.mark.parametrize(
        ("token", "key"),
        [
            ("+width", "+width"),
            ("+width=9", "+width"),
            ("width=9", "+width"),
            ("width", "+width"),
            ("--width", "--width"),
            ("--width=9", "--width"),
            ("-w9", "-w9"),
            ("+verilator+seed+5", "+verilator+seed+"),
            ("verilator+seed+5", "+verilator+seed+"),
        ],
    )
    def test_canonical_key(self, token: str, key: str):
        assert option_key(token) == key

    @pytest.mark.parametrize("token", ["+width=9", "+width", "--width=9", "-w"])
    def test_adapter_form_keeps_the_key(self, token: str):
        assert option_key(adapter_argument(token)) == option_key(token)


class TestLoaderRefusals:
    @pytest.mark.parametrize("argument", ["++width=99", "++width", "++seed=99", "+-width=9"])
    def test_doubled_prefix_is_refused(self, argument: str):
        with pytest.raises(TestVariantError, match="which the simulator reads as another option"):
            normalize_tests_toml(_section([argument]))

    @pytest.mark.parametrize("argument", ["+test", "+test=x", "+test=stress"])
    def test_select_option_is_refused_in_every_form(self, argument: str):
        with pytest.raises(ValueError, match="is not allowed: the select template sets it"):
            normalize_tests_toml(_section([argument]))

    @pytest.mark.parametrize(
        ("template", "argument"),
        [
            ("+seed={seed}", "+seed"),
            ("+seed={seed}", "+seed=99"),
            ("--seed={seed}", "--seed"),
            ("--seed={seed}", "--seed=99"),
        ],
    )
    def test_seed_template_option_is_refused_in_every_form(self, template: str, argument: str):
        with pytest.raises(ValueError, match="is not allowed: the seed template sets it"):
            normalize_tests_toml(_section([argument], seed=template))

    @pytest.mark.parametrize("argument", ["+verilator+seed+9", "+verilator+seed+"])
    def test_verilator_seed_option_is_refused(self, argument: str):
        with pytest.raises(ValueError, match="Booley passes the Verilator seed option itself"):
            normalize_tests_toml(_section([argument]))

    def test_getopt_argument_of_another_name_than_a_plusarg_template_loads(self):
        # ``--seed`` and ``+seed`` are different options of the simulator binary.
        normalize_tests_toml(_section(["--seed=5"], seed="+seed={seed}"))


class TestTargetPlusargOwnership:
    @pytest.mark.parametrize("argument", ["+width", "+width=99"])
    def test_plan_refuses_a_target_plusarg_in_every_form(self, argument: str):
        with pytest.raises(TestVariantError, match="overrides a plusarg the Target declares"):
            _plan([argument], plusargs=("width",))

    def test_getopt_argument_with_a_plusarg_name_is_another_option(self):
        plan = _plan(["--width=99"], plusargs=("width",))
        launch = plan.launch((plan.run("stress"),))
        assert simulation_plusargs(launch, _WIDTH) == [
            "width=8",
            "test=stress",
            "--width=99",
            "verilator+seed+1",
        ]

    @pytest.mark.parametrize("argument", ["+width", "+width=99"])
    def test_launch_never_replaces_a_target_plusarg_with_a_variant_argument(self, argument: str):
        # The plan was built without the Target's plusargs (another checkout,
        # an older caller): the launch still refuses instead of dropping width=8.
        plan = _plan([argument])
        launch = plan.launch((plan.run("stress"),))
        with pytest.raises(TestVariantError, match="the launch is refused"):
            simulation_plusargs(launch, _WIDTH)

    def test_launch_refuses_a_carried_argument_that_sets_the_seed_option(self):
        plan = _plan([], seed="+seed={seed}")
        carried = build_test_run("stress", variant="a", seed=3, args=("+seed=99",))
        with pytest.raises(TestVariantError, match="the launch is refused"):
            simulation_plusargs(plan.launch((carried,)), {})

    def test_select_and_seed_replace_a_target_plusarg_of_the_same_option(self):
        plan = _plan([], seed="+seed={seed}")
        parameters = {
            "test": {"paramtype": "plusarg", "default": "none"},
            "seed": {"paramtype": "plusarg", "default": 0},
            **_WIDTH,
        }
        launch = plan.launch((plan.run("stress", seed=5),))
        assert simulation_plusargs(launch, parameters) == [
            "width=8",
            "test=stress",
            "verilator+seed+5",
            "seed=5",
        ]

    @pytest.mark.parametrize(
        ("argument", "reason"),
        [
            ("+width=99", "overrides a plusarg the Target declares"),
            ("+test=other", "the select template sets it"),
            ("+seed=99", "the seed template sets it"),
            ("++width=99", "which the simulator reads as another option"),
        ],
    )
    def test_carried_test_run_is_refused_where_its_argument_is_owned(
        self, argument: str, reason: str
    ):
        plan = _plan([], plusargs=("width",), seed="+seed={seed}")
        carried = build_test_run("stress", variant="a", seed=3, args=(argument,))
        with pytest.raises(TestVariantError, match=reason):
            plan.require_applicable(carried)


class TestSeedTemplate:
    @pytest.mark.parametrize(
        "template",
        [
            "+seed={{seed}}",
            "+seed={{seed}}{{seed}}",
            "+seed={seed!r}",
            "+seed={seed:x}",
            "+seed={seed:>12}",
            "+seed={seed}{seed}",
            "+seed={0}",
            "+seed={}",
            "+seed={seed.real}",
            "+seed={seed",
        ],
    )
    def test_only_the_plain_seed_field_is_accepted(self, template: str):
        with pytest.raises(SeedConfigurationError):
            validate_seed_template(template, where="tests.toml [sim].seed")

    @pytest.mark.parametrize("template", ["+seed={seed}", "--seed={seed}", "+s{{x}}={seed}"])
    @pytest.mark.parametrize("seed", [1, 42, MAX_SEED])
    def test_rendering_always_holds_the_decimal_seed(self, template: str, seed: int):
        section = normalize_tests_toml({"sim": {"tests": ["a"], "seed": template}})["sim"]
        plan = build_target_test_plan("sim", section, adapter="icarus")
        launch = plan.launch((plan.run("a", seed=seed),))
        assert len(launch.seed_arguments) == 1
        assert str(seed) in launch.seed_arguments[0]
        assert "{seed}" not in launch.seed_arguments[0]

    @pytest.mark.parametrize("template", ["++seed={seed}", "+-seed={seed}"])
    def test_doubled_prefix_is_refused(self, template: str):
        with pytest.raises(SeedConfigurationError, match="reads as another option"):
            validate_seed_template(template, where="tests.toml [sim].seed")


class TestSelectTemplate:
    @pytest.mark.parametrize("template", ["+test={{name}}", "+test={{index}}", "+run_all"])
    def test_a_template_that_selects_no_test_is_refused(self, template: str):
        with pytest.raises(ValueError, match="selects no Test"):
            normalize_tests_toml({"sim": {"select": template}})

    @pytest.mark.parametrize("template", ["+test={name!r}", "+test={seed}", "+test={0}"])
    def test_other_fields_and_conversions_are_refused(self, template: str):
        with pytest.raises(ValueError, match="unknown field"):
            normalize_tests_toml({"sim": {"select": template}})

    @pytest.mark.parametrize("template", ["++test={name}", "+-t={index}"])
    def test_doubled_prefix_is_refused(self, template: str):
        with pytest.raises(ValueError, match="reads as another option"):
            normalize_tests_toml({"sim": {"select": template}})
