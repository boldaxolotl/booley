"""Test Run identity: selector grammar, Test Variants, seeds, rerun command (ADR 0072)."""

from __future__ import annotations

import random
import shlex

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from booley.config.tests_toml_rules import TestRunNameError
from booley.flows.builtin_cli import parse_request
from booley.flows.sim.campaign.planning import canonical_sha256
from booley.flows.sim.flow import SimulateFlow
from booley.flows.test_runs import (
    DEFAULT_SEED,
    MAX_MULTI_SEED_COUNT,
    MAX_SEED,
    UNNAMED_TEST_DISPLAY,
    PinnedSeed,
    SeedCount,
    TestRun,
    TestRunLimitError,
    TestSelector,
    TestVariant,
    TestVariantError,
    TestVariantTable,
    UnknownVariantError,
    UnpinnedSeed,
    UnseededTargetError,
    build_test_run,
    declared_inputs_fingerprint,
    draw_seeds,
    parse_test_selector,
    parse_test_variants,
    rerun_command,
    test_run_document,
    test_run_from_document,
)

# Deterministic on every run and machine; no example database is written.
PROPERTY_SETTINGS = settings(derandomize=True, database=None, max_examples=300, deadline=None)


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


# ---------------------------------------------------------------------------
# Selector grammar
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("stress", TestSelector("stress", None, UnpinnedSeed())),
        ("stress+soak", TestSelector("stress", "soak", UnpinnedSeed())),
        ("stress@1", TestSelector("stress", None, PinnedSeed(1))),
        ("stress@42", TestSelector("stress", None, PinnedSeed(42))),
        (f"stress@{MAX_SEED}", TestSelector("stress", None, PinnedSeed(MAX_SEED))),
        ("stress+soak@7", TestSelector("stress", "soak", PinnedSeed(7))),
        ("stress@x1", TestSelector("stress", None, SeedCount(1))),
        ("stress@x1000", TestSelector("stress", None, SeedCount(1000))),
        ("stress+soak@x25", TestSelector("stress", "soak", SeedCount(25))),
        # Names keep every character the grammar does not own.
        ("slow-first", TestSelector("slow-first", None, UnpinnedSeed())),
        ("test_reset.v2", TestSelector("test_reset.v2", None, UnpinnedSeed())),
        ("x5", TestSelector("x5", None, UnpinnedSeed())),
        ("x5+x6@x7", TestSelector("x5", "x6", SeedCount(7))),
        ("tb/smoke:1", TestSelector("tb/smoke:1", None, UnpinnedSeed())),
    ],
)
def test_valid_selectors_parse(text: str, expected: TestSelector) -> None:
    assert parse_test_selector(text) == expected


@pytest.mark.parametrize(
    ("text", "reason"),
    [
        ("", "Test name '' is invalid: it is empty"),
        ("+soak", "Test name '' is invalid: it is empty"),
        ("stress+", "Test Variant name '' is invalid: it is empty"),
        ("@5", "Test name '' is invalid: it is empty"),
        ("stress@", "must be followed by a seed or xN"),
        ("stress+@5", "Test Variant name '' is invalid: it is empty"),
        ("a+b+c", "more than one '+'; a Test Run names one Test Variant"),
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
        ("stress@\u0663", "decimal number without a sign"),
        ("stress@x", "decimal number without a sign"),
        ("stress@x0", "at least 1"),
        ("stress@x01", "leading zeros"),
        ("stress@x-1", "decimal number without a sign"),
        ("stress@X5", "decimal number without a sign"),
        ("stress@xx5", "decimal number without a sign"),
        ("default", "'default' is reserved"),
        ("stress+default", "Test Variant name 'default' is invalid"),
        ("-foo", "it starts with '-'"),
        ("-foo@1", "it starts with '-'"),
        ("stress+-v", "Test Variant name '-v' is invalid: it starts with '-'"),
        ("<default>", "it contains '<'"),
        ("<default>@1", "it contains '<'"),
        ("stress+a>b", "it contains '>'"),
        ("my test", "it contains whitespace"),
        ("stress+so ak", "it contains whitespace"),
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
        (["+a=1"], TestVariantError, "must be a table of Test Variants"),
        ({"short": []}, TestVariantError, "must name its default Test Variant"),
        (
            {"default": 1, "short": []},
            TestVariantError,
            "default must be the name of a Test Variant",
        ),
        (
            {"default": "short"},
            UnknownVariantError,
            "not a declared Test Variant (declared: none)",
        ),
        (
            {"default": "long", "short": []},
            UnknownVariantError,
            "names 'long', which is not a declared Test Variant (declared: short)",
        ),
        ({"default": "a", "a": [], "b+c": []}, TestRunNameError, "Test Variant name 'b+c'"),
        ({"default": "a", "a": [], "b c": []}, TestRunNameError, "it contains whitespace"),
        ({"default": "a", "a": [], "-b": []}, TestRunNameError, "it starts with '-'"),
        ({"default": "a", "a": [], "<b>": []}, TestRunNameError, "it contains '<'"),
        ({"default": "a", "a": "+x=1"}, TestVariantError, "must be a list of arguments"),
        ({"default": "a", "a": 5}, TestVariantError, "got int"),
        ({"default": "a", "a": [1]}, TestVariantError, "must be a list of strings"),
        ({"default": "a", "a": ["iters=1"]}, TestVariantError, "must start with '+' or '-'"),
        ({"default": "a", "a": [""]}, TestVariantError, "must start with '+' or '-'"),
        ({"default": "a", "a": ["+a=1 +b=2"]}, TestVariantError, "one token"),
        ({"default": "a", "a": ["+a=\t1"]}, TestVariantError, "one token"),
        ({"default": "a", "a": ["+a=\x001"]}, TestVariantError, "one token"),
        ({"default": "a", "a": {"args": "+x=1"}}, TestVariantError, "list of strings"),
        (
            {"default": "a", "a": {"plusargs": []}},
            TestVariantError,
            "unknown key 'plusargs'; a Test Variant table holds only args and env",
        ),
        ({"default": "a", "a": {"args": [], "seed": 1}}, TestVariantError, "unknown key 'seed'"),
        ({"default": "a", "a": {"env": ["X=1"]}}, TestVariantError, ".a.env must be a table"),
        ({"default": "a", "a": {"env": {"MY-VAR": "1"}}}, TestVariantError, "'MY-VAR'"),
        ({"default": "a", "a": {"env": {"1X": "1"}}}, TestVariantError, "'1X'"),
        # A trailing newline is not part of an identifier.
        ({"default": "a", "a": {"env": {"X\n": "1"}}}, TestVariantError, ".a.env key 'X\\n'"),
        ({"default": "a", "a": {"env": {"N": 1}}}, TestVariantError, ".a.env.N must be a string"),
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
    assert run == build_test_run("stress", args=("+b=2", "+a=1"), env={"A": "2", "Z": "1"})
    assert hash(run) == hash(
        build_test_run("stress", args=("+b=2", "+a=1"), env={"A": "2", "Z": "1"})
    )


@pytest.mark.parametrize(
    ("kwargs", "reason"),
    [
        ({"test": None, "variant": "soak"}, "the unnamed invocation has no Test Variants"),
        ({"test": "default"}, "reserved"),
        ({"test": "a+b"}, "'+'"),
        ({"test": "-foo"}, "it starts with '-'"),
        ({"test": "<default>"}, "it contains '<'"),
        ({"test": "a", "variant": "b@c"}, "Test Variant name 'b@c'"),
        ({"test": "a", "variant": "-b"}, "Test Variant name '-b'"),
        ({"test": "a", "seed": 0}, "seed 0 is out of range"),
        ({"test": "a", "seed": MAX_SEED + 1}, "out of range"),
        ({"test": "a", "seed": -1}, "seed -1 is out of range"),
        # Only a real integer is a seed: no bool, float or numeric string.
        ({"test": "a", "seed": True}, "seed must be an integer, got True"),
        ({"test": "a", "seed": False}, "seed must be an integer, got False"),
        ({"test": "a", "seed": 1.5}, "seed must be an integer, got 1.5"),
        ({"test": "a", "seed": 1.0}, "seed must be an integer, got 1.0"),
        ({"test": "a", "seed": float("nan")}, "seed must be an integer"),
        ({"test": "a", "seed": "1"}, "seed must be an integer, got '1'"),
        ({"test": None, "seed": "42"}, "seed must be an integer, got '42'"),
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


# ---------------------------------------------------------------------------
# Properties: names round-trip, the unnamed invocation is never a Test
# ---------------------------------------------------------------------------

_FORBIDDEN_CHARACTERS = "+@<>'\"\\"
_VALID_NAMES = st.text(
    alphabet=st.characters(
        # No surrogates, controls, invisible format characters or separators.
        blacklist_categories=("Cs", "Cc", "Cf", "Zs", "Zl", "Zp"),
        blacklist_characters=_FORBIDDEN_CHARACTERS,
    ),
    min_size=1,
    max_size=24,
).filter(lambda name: not name.startswith("-") and name != "default")
_SEEDS = st.integers(min_value=1, max_value=MAX_SEED)
_OPTIONAL_SEEDS = st.none() | _SEEDS


@st.composite
def _malformed_names(draw: st.DrawFn) -> str:
    """A valid name broken in exactly one way the rule refuses."""
    name = draw(_VALID_NAMES)
    breakage = draw(st.sampled_from(["empty", "reserved", "dash", "character"]))
    if breakage == "empty":
        return ""
    if breakage == "reserved":
        return "default"
    if breakage == "dash":
        return "-" + name
    bad = draw(
        st.sampled_from([*_FORBIDDEN_CHARACTERS, " ", "\t", "\n", "\x00", "\x1b", "\u200b"])
    )
    position = draw(st.integers(min_value=0, max_value=len(name)))
    return name[:position] + bad + name[position:]


_NOT_SEEDS = st.one_of(
    st.integers(max_value=0),
    st.integers(min_value=MAX_SEED + 1),
    st.booleans(),
    st.floats(),
    _SEEDS.map(str),
    _SEEDS.map(float),
)


@PROPERTY_SETTINGS
@given(test=_VALID_NAMES, variant=st.none() | _VALID_NAMES, seed=_OPTIONAL_SEEDS)
def test_a_rendered_name_parses_back_to_the_same_test_run(
    test: str, variant: str | None, seed: int | None
) -> None:
    run = build_test_run(test, variant=variant, seed=seed)
    seed_form = UnpinnedSeed() if seed is None else PinnedSeed(seed)
    assert parse_test_selector(run.name) == TestSelector(test, variant, seed_form)


@PROPERTY_SETTINGS
@given(seed=_OPTIONAL_SEEDS)
def test_the_unnamed_invocation_never_parses_as_a_selector(seed: int | None) -> None:
    run = build_test_run(None, seed=seed)
    assert run.name.startswith(UNNAMED_TEST_DISPLAY)
    with pytest.raises(TestRunNameError):
        parse_test_selector(run.name)


@PROPERTY_SETTINGS
@given(
    test=_VALID_NAMES,
    variant=st.none() | _VALID_NAMES,
    seed=_OPTIONAL_SEEDS,
    unnamed_seed=_OPTIONAL_SEEDS,
)
def test_a_declared_name_never_renders_like_the_unnamed_invocation(
    test: str, variant: str | None, seed: int | None, unnamed_seed: int | None
) -> None:
    named = build_test_run(test, variant=variant, seed=seed)
    assert named.name != build_test_run(None, seed=unnamed_seed).name
    assert UNNAMED_TEST_DISPLAY not in named.name


def test_the_unnamed_display_itself_is_not_a_declarable_name() -> None:
    # Regression: "<default>" used to be a legal Test name and rendered
    # exactly like the unnamed invocation.
    with pytest.raises(TestRunNameError, match="it contains '<'"):
        build_test_run(UNNAMED_TEST_DISPLAY, seed=1)


@PROPERTY_SETTINGS
@given(bad=_malformed_names(), good=_VALID_NAMES, seed=_OPTIONAL_SEEDS)
def test_the_builder_and_the_parser_refuse_malformed_names(
    bad: str, good: str, seed: int | None
) -> None:
    with pytest.raises(TestRunNameError):
        build_test_run(bad, seed=seed)
    with pytest.raises(TestRunNameError):
        build_test_run(good, variant=bad, seed=seed)
    with pytest.raises(TestRunNameError):
        parse_test_selector(bad)


@PROPERTY_SETTINGS
@given(test=st.none() | _VALID_NAMES, seed=_NOT_SEEDS)
def test_the_builder_refuses_everything_that_is_not_a_seed(test: str | None, seed: object) -> None:
    with pytest.raises(TestRunNameError):
        build_test_run(test, seed=seed)  # type: ignore[arg-type]


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


def _unvalidated_run(test: str, seed: int | None) -> TestRun:
    """A run whose name skipped validation, to test the renderer on its own."""
    return TestRun(test, None, seed, (), (), declared_inputs_fingerprint((), {}))


_RERUN_CASES = [
    ("sim", build_test_run("stress", seed=42), [*_SIM, "sim", "--test=stress@42"]),
    (
        "sim",
        build_test_run("stress", variant="soak", seed=7),
        [*_SIM, "sim", "--test=stress+soak@7"],
    ),
    # An unseeded Target: no @seed and no --seed.
    ("sim_icarus", build_test_run("stress"), [*_SIM, "sim_icarus", "--test=stress"]),
    (
        "sim_icarus",
        build_test_run("stress", variant="soak"),
        [*_SIM, "sim_icarus", "--test=stress+soak"],
    ),
    # The unnamed invocation cannot be named by --test.
    ("sim", build_test_run(None, seed=1), [*_SIM, "sim", "--seed", "1"]),
    ("sim", build_test_run(None), [*_SIM, "sim"]),
    # A VLNV-qualified Target and shell-active name characters stay one token each.
    (
        "booley:ibex:port#sim",
        build_test_run("tb/smoke$1;x", seed=3),
        [*_SIM, "booley:ibex:port#sim", "--test=tb/smoke$1;x@3"],
    ),
    ("my sim", build_test_run("a|b"), [*_SIM, "my sim", "--test=a|b"]),
    ("sim", build_test_run("a=b", seed=2), [*_SIM, "sim", "--test=a=b@2"]),
    # Regression: `--test -foo@1` (two tokens) is refused by argparse. Names
    # can no longer start with '-', and the renderer does not rely on that.
    ("sim", _unvalidated_run("-foo", 1), [*_SIM, "sim", "--test=-foo@1"]),
    ("sim", _unvalidated_run("--trace", None), [*_SIM, "sim", "--test=--trace"]),
]


@pytest.mark.parametrize(("target", "run", "argv"), _RERUN_CASES)
def test_rerun_command_splits_into_the_intended_arguments(
    target: str, run: TestRun, argv: list[str]
) -> None:
    assert shlex.split(rerun_command(target, run)) == argv


@pytest.mark.parametrize(("target", "run", "_argv"), _RERUN_CASES)
def test_rerun_command_is_accepted_by_the_real_sim_parser(
    target: str, run: TestRun, _argv: list[str]
) -> None:
    flow = SimulateFlow()
    argv = shlex.split(rerun_command(target, run))
    assert argv[:3] == ["booley", "flow", flow.name]
    if run.test is not None and run.test.startswith("-"):
        # The renderer keeps such a name one token, and the parser then
        # refuses it by the name rule instead of reading it as an option.
        with pytest.raises(SystemExit):
            parse_request(flow, argv[3:])
        return
    request = parse_request(flow, argv[3:])
    assert request.target == target
    assert request.test == (None if run.test is None else (run.name,))
    assert request.seed == (run.seed if run.test is None else None)


def test_rerun_command_is_plain_text_for_ordinary_names() -> None:
    run = build_test_run("stress", variant="soak", seed=42)
    assert rerun_command("sim", run) == "booley flow sim --target sim --test=stress+soak@42"


def test_run_from_document_recomputes_the_fingerprint_unless_told_to_trust_it() -> None:
    stale = {
        **test_run_document(build_test_run("stress", seed=7)),
        "fingerprint": "sha256:" + "0" * 64,
    }

    with pytest.raises(TestRunNameError, match="disagrees"):
        test_run_from_document(stale)
    assert (
        test_run_from_document(stale, trust_fingerprint=True).fingerprint == stale["fingerprint"]
    )
    with pytest.raises(TestRunNameError, match="sha256 digest"):
        test_run_from_document({**stale, "fingerprint": "sha256:x"}, trust_fingerprint=True)
