"""Test Run identity: selector grammar, name rules, Variants, seeds (ADR 0072)."""

from __future__ import annotations

import random
import shlex

import pytest

from booley.config import project_config, test_name_rules
from booley.flows.sim.campaign.planning import canonical_sha256
from booley.flows.test_runs import (
    DEFAULT_SEED,
    MAX_MULTI_SEED_COUNT,
    MAX_SEED,
    Count,
    Pinned,
    TestRunLimitError,
    TestRunNameError,
    TestSelector,
    TestVariant,
    TestVariantError,
    TestVariantTable,
    UnknownVariantError,
    Unpinned,
    UnseededTargetError,
    build_test_run,
    declared_inputs_fingerprint,
    draw_seeds,
    parse_test_selector,
    parse_test_variants,
    rerun_command,
    validate_test_run_name_part,
)


def test_constants_match_the_settled_values() -> None:
    assert DEFAULT_SEED == 1
    assert MAX_SEED == 2**31 - 1
    assert MAX_MULTI_SEED_COUNT == 1000


def test_every_typed_error_is_a_value_error() -> None:
    for error in (
        TestRunNameError,
        TestVariantError,
        UnknownVariantError,
        UnseededTargetError,
        TestRunLimitError,
    ):
        assert issubclass(error, ValueError)


def test_name_rules_are_the_ones_the_tests_toml_loader_applies() -> None:
    # One definition: the loader and the selector grammar cannot drift apart.
    assert validate_test_run_name_part is test_name_rules.validate_test_run_name_part
    assert TestRunNameError is test_name_rules.TestRunNameError
    assert project_config.validate_test_run_name_part is validate_test_run_name_part


# ---------------------------------------------------------------------------
# Selector grammar
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("stress", TestSelector("stress", None, Unpinned())),
        ("stress+soak", TestSelector("stress", "soak", Unpinned())),
        ("stress@1", TestSelector("stress", None, Pinned(1))),
        ("stress@42", TestSelector("stress", None, Pinned(42))),
        (f"stress@{MAX_SEED}", TestSelector("stress", None, Pinned(MAX_SEED))),
        ("stress+soak@7", TestSelector("stress", "soak", Pinned(7))),
        ("stress@x1", TestSelector("stress", None, Count(1))),
        ("stress@x1000", TestSelector("stress", None, Count(1000))),
        ("stress+soak@x25", TestSelector("stress", "soak", Count(25))),
        # Names keep every character the grammar does not own.
        ("slow-first", TestSelector("slow-first", None, Unpinned())),
        ("test_reset.v2", TestSelector("test_reset.v2", None, Unpinned())),
        ("x5", TestSelector("x5", None, Unpinned())),
        ("x5+x6@x7", TestSelector("x5", "x6", Count(7))),
        ("tb/smoke:1", TestSelector("tb/smoke:1", None, Unpinned())),
    ],
)
def test_valid_selectors_parse(text: str, expected: TestSelector) -> None:
    assert parse_test_selector(text) == expected


@pytest.mark.parametrize(
    ("text", "reason"),
    [
        ("", "must not be empty"),
        ("+soak", "must not be empty"),
        ("stress+", "must not be empty"),
        ("@5", "must not be empty"),
        ("stress@", "must be followed by a seed or xN"),
        ("stress+@5", "must not be empty"),
        ("a+b+c", "more than one '+'"),
        ("stress@x1@2", "more than one '@'"),
        ("stress@1@2", "more than one '@'"),
        ("stress@0", "out of range"),
        (f"stress@{2**31}", "out of range"),
        ("stress@99999999999999999999", "out of range"),
        ("stress@" + "9" * 5000, "out of range"),
        ("stress@01", "leading zeros"),
        ("stress@00", "leading zeros"),
        ("stress@-1", "decimal number without a sign"),
        ("stress@+1", "decimal number without a sign"),
        ("stress@1.5", "decimal number without a sign"),
        ("stress@0x10", "decimal number without a sign"),
        ("stress@seed", "decimal number without a sign"),
        ("stress@ 5", "decimal number without a sign"),
        ("stress@٣", "decimal number without a sign"),
        ("stress@x", "decimal number without a sign"),
        ("stress@x0", "at least 1"),
        ("stress@x01", "leading zeros"),
        ("stress@x-1", "decimal number without a sign"),
        ("stress@X5", "decimal number without a sign"),
        ("stress@xx5", "decimal number without a sign"),
        ("default", "reserved"),
        ("stress+default", "reserved"),
        ("my test", "whitespace"),
        ("stress+so ak", "whitespace"),
        ("it's", "single quote"),
        ('say"hi', "double quote"),
        ("back\\slash", "backslash"),
        ("bell\x07", "control character U+0007"),
    ],
)
def test_invalid_selectors_name_the_reason(text: str, reason: str) -> None:
    with pytest.raises(TestRunNameError) as excinfo:
        parse_test_selector(text)
    assert reason in str(excinfo.value)


@pytest.mark.parametrize("text", ["stress@x1001", "stress+soak@x99999999999999"])
def test_seed_count_above_the_limit_is_a_limit_error(text: str) -> None:
    with pytest.raises(TestRunLimitError, match="exceeds the limit of 1000"):
        parse_test_selector(text)


# ---------------------------------------------------------------------------
# Name rules
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "name",
    ["reset", "test_reset", "slow-first", "a.b", "tb/smoke", "x1", "Default", "DEFAULT", "é"],
)
def test_valid_names_are_returned_unchanged(name: str) -> None:
    assert validate_test_run_name_part(name) == name


@pytest.mark.parametrize(
    ("name", "reason"),
    [
        ("", "must not be empty"),
        ("default", "reserved"),
        ("a b", "whitespace"),
        ("a\tb", "whitespace"),
        ("a\nb", "whitespace"),
        ("a\u00a0b", "whitespace"),
        (" lead", "whitespace"),
        ("a+b", "'+'"),
        ("a@b", "'@'"),
        ("a'b", "single quote"),
        ('a"b', "double quote"),
        ("a\\b", "backslash"),
        ("a\x00b", "control character U+0000"),
        ("a\x1bb", "control character U+001B"),
        ("a\x7fb", "control character U+007F"),
        ("a\u200bb", "control character U+200B"),
        (7, "must be a string"),
        (None, "must be a string"),
    ],
)
def test_invalid_names_name_the_reason(name: object, reason: str) -> None:
    with pytest.raises(TestRunNameError) as excinfo:
        validate_test_run_name_part(name, kind="Variant")
    message = str(excinfo.value)
    assert reason in message
    assert message.startswith("Variant name ")


# ---------------------------------------------------------------------------
# Variant table
# ---------------------------------------------------------------------------


def test_variant_table_accepts_lists_and_tables() -> None:
    table = parse_test_variants(
        "stress",
        {
            "default": "short",
            "short": ["+iters=1000"],
            "soak": ["+iters=100000", "--trace-depth=2"],
            "fault": {"args": ["+err_rate=5"], "env": {"FAULT_MODE": "1", "A": ""}},
            "env_only": {"env": {"FLAVOR": "x y"}},
            "legacy": [],
            "empty_table": {},
        },
    )
    assert table == TestVariantTable(
        test="stress",
        default="short",
        variants=(
            TestVariant("short", ("+iters=1000",)),
            TestVariant("soak", ("+iters=100000", "--trace-depth=2")),
            TestVariant("fault", ("+err_rate=5",), (("A", ""), ("FAULT_MODE", "1"))),
            TestVariant("env_only", (), (("FLAVOR", "x y"),)),
            TestVariant("legacy"),
            TestVariant("empty_table"),
        ),
    )


@pytest.mark.parametrize(
    ("raw", "error", "reason"),
    [
        (["+a=1"], TestVariantError, "must be a table of Variants"),
        ({"short": []}, TestVariantError, "must name its default Variant"),
        ({"default": 1, "short": []}, TestVariantError, "default must be the name"),
        ({"default": "short"}, UnknownVariantError, "not a declared Variant (declared: none)"),
        (
            {"default": "long", "short": []},
            UnknownVariantError,
            "names 'long', which is not a declared Variant (declared: short)",
        ),
        ({"default": "a", "a": [], "b+c": []}, TestRunNameError, "'+'"),
        ({"default": "a", "a": [], "b c": []}, TestRunNameError, "whitespace"),
        ({"default": "a", "a": "+x=1"}, TestVariantError, "must be a list of arguments"),
        ({"default": "a", "a": 5}, TestVariantError, "got int"),
        ({"default": "a", "a": [1]}, TestVariantError, "must be a list of strings"),
        ({"default": "a", "a": ["iters=1"]}, TestVariantError, "must start with '+' or '-'"),
        ({"default": "a", "a": [""]}, TestVariantError, "must start with '+' or '-'"),
        ({"default": "a", "a": ["+a=1 +b=2"]}, TestVariantError, "one token"),
        ({"default": "a", "a": ["+a=\t1"]}, TestVariantError, "one token"),
        ({"default": "a", "a": ["+a=\x001"]}, TestVariantError, "one token"),
        ({"default": "a", "a": {"args": "+x=1"}}, TestVariantError, "list of strings"),
        ({"default": "a", "a": {"plusargs": []}}, TestVariantError, "unknown key 'plusargs'"),
        ({"default": "a", "a": {"args": [], "seed": 1}}, TestVariantError, "unknown key 'seed'"),
        ({"default": "a", "a": {"env": ["X=1"]}}, TestVariantError, "env must be a table"),
        ({"default": "a", "a": {"env": {"MY-VAR": "1"}}}, TestVariantError, "'MY-VAR'"),
        ({"default": "a", "a": {"env": {"1X": "1"}}}, TestVariantError, "'1X'"),
        ({"default": "a", "a": {"env": {"N": 1}}}, TestVariantError, "env.N must be a string"),
    ],
)
def test_variant_table_refusals(raw: object, error: type[ValueError], reason: str) -> None:
    with pytest.raises(error) as excinfo:
        parse_test_variants("stress", raw, where="tests.toml [sim].variants.stress")
    message = str(excinfo.value)
    assert reason in message
    assert message.startswith("tests.toml [sim].variants.stress")


def test_variant_table_default_location_names_the_test() -> None:
    with pytest.raises(TestVariantError, match=r"^variants\.stress must be a table"):
        parse_test_variants("stress", None)


# ---------------------------------------------------------------------------
# Test Run and fingerprint
# ---------------------------------------------------------------------------


def test_run_name_renders_every_form() -> None:
    assert build_test_run("stress").name == "stress"
    assert build_test_run("stress", seed=1).name == "stress@1"
    assert build_test_run("stress", variant="soak").name == "stress+soak"
    assert (
        build_test_run("stress", variant="soak", seed=MAX_SEED).name == f"stress+soak@{MAX_SEED}"
    )
    assert build_test_run(None).name == "<default>"
    assert build_test_run(None, seed=1).name == "<default>@1"


def test_build_test_run_normalizes_env_and_keeps_args_order() -> None:
    run = build_test_run("stress", args=("+b=2", "+a=1"), env={"Z": "1", "A": "2"})
    assert run.args == ("+b=2", "+a=1")
    assert run.env == (("A", "2"), ("Z", "1"))
    assert run.fingerprint == declared_inputs_fingerprint(("+b=2", "+a=1"), {"A": "2", "Z": "1"})
    assert hash(run) == hash(
        build_test_run("stress", args=("+b=2", "+a=1"), env={"A": "2", "Z": "1"})
    )


@pytest.mark.parametrize(
    ("kwargs", "reason"),
    [
        ({"test": None, "variant": "soak"}, "unnamed invocation has no Variants"),
        ({"test": "default"}, "reserved"),
        ({"test": "a+b"}, "'+'"),
        ({"test": "a", "variant": "b@c"}, "'@'"),
        ({"test": "a", "seed": 0}, "out of range"),
        ({"test": "a", "seed": MAX_SEED + 1}, "out of range"),
        ({"test": "a", "seed": -1}, "out of range"),
        ({"test": "a", "seed": True}, "out of range"),
    ],
)
def test_build_test_run_refusals(kwargs: dict, reason: str) -> None:
    test = kwargs.pop("test")
    with pytest.raises(TestRunNameError) as excinfo:
        build_test_run(test, **kwargs)
    assert reason in str(excinfo.value)


def test_fingerprint_is_stable_and_matches_the_campaign_hash() -> None:
    fingerprint = declared_inputs_fingerprint(("+iters=1000",), {"FAULT_MODE": "1"})
    # Pinned literal: a silent change of the encoding would orphan frozen Goals.
    assert fingerprint == (
        "sha256:8b3ad4350562ee7758fdfd17286ec271131b4915bbd2d076291e34c0181e6e63"
    )
    assert fingerprint == canonical_sha256({"args": ["+iters=1000"], "env": {"FAULT_MODE": "1"}})
    unicode_inputs = (("+name=é",), {"NAME": "ü"})
    assert declared_inputs_fingerprint(*unicode_inputs) == canonical_sha256(
        {"args": list(unicode_inputs[0]), "env": unicode_inputs[1]}
    )


def test_fingerprint_sensitivity() -> None:
    base = declared_inputs_fingerprint(("+a=1", "+b=2"), {"X": "1", "Y": "2"})
    # Environment key order is not significant.
    assert declared_inputs_fingerprint(("+a=1", "+b=2"), {"Y": "2", "X": "1"}) == base
    # Argument order is.
    assert declared_inputs_fingerprint(("+b=2", "+a=1"), {"X": "1", "Y": "2"}) != base
    assert declared_inputs_fingerprint(("+a=1",), {"X": "1", "Y": "2"}) != base
    assert declared_inputs_fingerprint(("+a=1", "+b=2"), {"X": "1", "Y": "3"}) != base
    assert declared_inputs_fingerprint(("+a=1", "+b=2"), {"X": "1"}) != base
    # Arguments and environment cannot be confused with each other.
    assert declared_inputs_fingerprint(("X",), {}) != declared_inputs_fingerprint((), {"X": ""})


def test_fingerprint_ignores_name_and_seed() -> None:
    soak = build_test_run("stress", variant="soak", seed=1, args=("+iters=9",))
    other = build_test_run("smoke", seed=77, args=("+iters=9",))
    assert soak.fingerprint == other.fingerprint
    assert build_test_run("stress").fingerprint == declared_inputs_fingerprint((), {})


@pytest.mark.parametrize("test", ["stress", "slow-first", "x5", "tb/smoke:1"])
@pytest.mark.parametrize("variant", [None, "soak", "x9"])
@pytest.mark.parametrize("seed", [None, 1, 42, MAX_SEED])
def test_parsing_a_rendered_name_round_trips(
    test: str, variant: str | None, seed: int | None
) -> None:
    run = build_test_run(test, variant=variant, seed=seed)
    expected_seed = Unpinned() if seed is None else Pinned(seed)
    assert parse_test_selector(run.name) == TestSelector(test, variant, expected_seed)


# ---------------------------------------------------------------------------
# Seed draws
# ---------------------------------------------------------------------------


class _ScriptedRandom(random.Random):
    """A random source that replays fixed values, then repeats the last one."""

    def __init__(self, values: list[int]) -> None:
        super().__init__(0)
        self._values = values
        self.calls = 0

    def randint(self, a: int, b: int) -> int:
        assert (a, b) == (1, MAX_SEED)
        value = self._values[min(self.calls, len(self._values) - 1)]
        self.calls += 1
        return value


def test_draw_seeds_returns_distinct_seeds_in_range() -> None:
    seeds = draw_seeds(MAX_MULTI_SEED_COUNT)
    assert len(seeds) == len(set(seeds)) == MAX_MULTI_SEED_COUNT
    assert all(1 <= seed <= MAX_SEED for seed in seeds)
    assert draw_seeds(0) == ()


def test_draw_seeds_is_deterministic_with_an_injected_source() -> None:
    first = draw_seeds(5, rng=random.Random(1233))
    assert first == draw_seeds(5, rng=random.Random(1233))
    assert first != draw_seeds(5, rng=random.Random(1234))


def test_draw_seeds_skips_repeats_and_excluded_seeds() -> None:
    source = _ScriptedRandom([7, 7, 8, 9, 9, 10, 11])
    assert draw_seeds(3, exclude={8, 10}, rng=source) == (7, 9, 11)
    assert source.calls == 7


def test_draw_seeds_stops_at_a_bound_when_the_source_is_stuck() -> None:
    source = _ScriptedRandom([5])
    with pytest.raises(TestRunLimitError, match="could not draw 2 distinct seeds"):
        draw_seeds(2, rng=source)
    assert source.calls < 1000
    with pytest.raises(TestRunLimitError, match="could not draw 1 distinct seeds"):
        draw_seeds(1, exclude=[5], rng=_ScriptedRandom([5]))


@pytest.mark.parametrize("count", [-1, MAX_MULTI_SEED_COUNT + 1])
def test_draw_seeds_refuses_an_impossible_count(count: int) -> None:
    with pytest.raises(TestRunLimitError, match=r"the count must be 0\.\.1000"):
        draw_seeds(count)


# ---------------------------------------------------------------------------
# Rerun command
# ---------------------------------------------------------------------------

_SIM = ["booley", "flow", "sim", "--target"]


@pytest.mark.parametrize(
    ("target", "run", "argv"),
    [
        ("sim", build_test_run("stress", seed=42), [*_SIM, "sim", "--test", "stress@42"]),
        (
            "sim",
            build_test_run("stress", variant="soak", seed=7),
            [*_SIM, "sim", "--test", "stress+soak@7"],
        ),
        # An unseeded Target: no @seed and no --seed.
        ("sim_icarus", build_test_run("stress"), [*_SIM, "sim_icarus", "--test", "stress"]),
        (
            "sim_icarus",
            build_test_run("stress", variant="soak"),
            [*_SIM, "sim_icarus", "--test", "stress+soak"],
        ),
        # The unnamed invocation cannot be named by --test.
        ("sim", build_test_run(None, seed=1), [*_SIM, "sim", "--seed", "1"]),
        ("sim", build_test_run(None), [*_SIM, "sim"]),
        # A VLNV-qualified Target and shell-active name characters stay one token each.
        (
            "booley:ibex:port#sim",
            build_test_run("tb/smoke$1;x", seed=3),
            [*_SIM, "booley:ibex:port#sim", "--test", "tb/smoke$1;x@3"],
        ),
        ("my sim", build_test_run("a|b"), [*_SIM, "my sim", "--test", "a|b"]),
    ],
)
def test_rerun_command_parses_back_to_the_intended_arguments(
    target: str, run, argv: list[str]
) -> None:
    command = rerun_command(target, run)
    assert shlex.split(command) == argv


def test_rerun_command_is_plain_text_for_ordinary_names() -> None:
    run = build_test_run("stress", variant="soak", seed=42)
    assert rerun_command("sim", run) == "booley flow sim --target sim --test stress+soak@42"
