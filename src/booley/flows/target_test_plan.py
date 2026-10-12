"""The Test Runs one simulation Target can run, and how each is seeded (ADR 0072).

A :class:`TargetTestPlan` is the validated per-Target view of ``tests.toml``:
the registered Tests, the durable skips, every Test's Variant table, the
Target's Default Seed and its seed mechanism. It turns what a request names
(``--test`` selectors and ``--seed``) into concrete :class:`TestRun` values
and renders the run-time arguments of one Test Run.

The checks here are the ones the ``tests.toml`` loader cannot make because
they need the resolved ``.core`` Target: which simulator integration launches
it and which plusargs it declares. Doctor, the Simulation Flow preflight and
every launch path build the plan through :func:`build_target_test_plan`, so
they cannot disagree.

Domain policy only: no CLI, process or Sandbox imports.
"""

from __future__ import annotations

from collections.abc import Callable, Collection, Mapping, Sequence
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Literal, cast

from booley.config.project_config import (
    DEFAULT_TEST_SELECT,
    load_test_configuration,
    lookup_target_section,
)
from booley.config.tests_toml_rules import (
    DEFAULT_SEED,
    VERILATOR_SEED_OPTION,
    SeedConfigurationError,
    TestVariantError,
    TestVariantTable,
    UnknownVariantError,
    argument_token_problem,
    option_key,
    parse_test_variants,
)
from booley.flows.test_runs import (
    RANDOM_SEED,
    SEED_WITH_SEEDED_SELECTOR,
    PinnedSeed,
    SeedCount,
    SeedRequest,
    TestRun,
    TestRunLimitError,
    TestRunNameError,
    TestSelector,
    UnpinnedSeed,
    UnseededTargetError,
    build_test_run,
    draw_seeds,
    parse_test_selector,
)

__all__ = [
    "MAX_TEST_RUNS_PER_REQUEST",
    "SeedMechanism",
    "TargetTestPlan",
    "TestRunLaunch",
    "UnknownTestError",
    "batch_test_runs",
    "build_target_test_plan",
    "load_target_test_plan",
    "seed_mechanism",
]

#: Most Test Runs one request may ask of one Target (the Simulation Campaign
#: work-item limit: an ordinary HDL Test Run is one work item).
MAX_TEST_RUNS_PER_REQUEST = 1000

#: Draws distinct seeds; :func:`booley.flows.test_runs.draw_seeds` by default.
SeedDraw = Callable[..., tuple[int, ...]]

_COCOTB = "cocotb"
_VERILATOR = "verilator"


class UnknownTestError(ValueError):
    """A selector names a Test the Target does not register."""


@dataclass(frozen=True)
class SeedMechanism:
    """How Booley gives a Test Run its seed on one Target.

    ``kind`` is ``verilator_native`` (``+verilator+seed+<seed>``),
    ``cocotb_env`` (the seed variable of the installed cocotb version),
    ``template`` (the Project's ``seed`` argument template) or ``none``.
    ``template`` holds the Project's template when one is declared; a
    Verilator Target with a template gets the native option and the rendered
    template from the one seed.
    """

    kind: Literal["verilator_native", "cocotb_env", "template", "none"]
    template: str | None = None

    @property
    def seeded(self) -> bool:
        """Whether the Target can apply a seed at all."""
        return self.kind != "none"

    def arguments(self, seed: int | None) -> tuple[str, ...]:
        """The simulator arguments that apply *seed*; empty for cocotb and unseeded."""
        if seed is None:
            return ()
        native = (f"{VERILATOR_SEED_OPTION}{seed}",) if self.kind == "verilator_native" else ()
        rendered = (self.template.format(seed=seed),) if self.template is not None else ()
        return (*native, *rendered)

    def describe(self) -> str:
        """One phrase for Doctor and reports: what applies the seed."""
        if self.kind == "verilator_native":
            extra = f" and seed template {self.template}" if self.template else ""
            return f"Verilator {VERILATOR_SEED_OPTION}<seed>{extra}"
        if self.kind == "cocotb_env":
            return "cocotb seed variable (COCOTB_RANDOM_SEED on 2.x, RANDOM_SEED on 1.x)"
        if self.kind == "template":
            return f"seed template {self.template}"
        return "unseeded"


def seed_mechanism(adapter: str, seed_template: str | None, *, where: str) -> SeedMechanism:
    """Choose the seed mechanism of a Target launched through *adapter*.

    *adapter* is ``cocotb`` for a Cocotb Target, otherwise the simulator
    (``verilator``, ``icarus``). Raises :class:`SeedConfigurationError` when a
    Cocotb Target declares a ``seed`` template: cocotb is seeded through its
    own variable, and a second mechanism would let the two disagree.
    """
    if adapter == _COCOTB:
        if seed_template is not None:
            raise SeedConfigurationError(
                f"{where}.seed is not allowed on a Cocotb Target: Booley seeds cocotb "
                "through its seed variable; remove the seed template"
            )
        return SeedMechanism("cocotb_env")
    if adapter == _VERILATOR:
        return SeedMechanism("verilator_native", seed_template)
    if seed_template is not None:
        return SeedMechanism("template", seed_template)
    return SeedMechanism("none")


@dataclass(frozen=True)
class TestRunLaunch:
    """The run-time inputs of one simulator process (never build inputs)."""

    __test__ = False

    #: The rendered ``select`` argument; ``None`` when the process selects no
    #: Test by argument (a Cocotb batch, the unnamed invocation).
    select: str | None
    #: The Test Variant's declared arguments, as written.
    variant_arguments: tuple[str, ...]
    #: The arguments that apply the seed; empty for cocotb and unseeded runs.
    seed_arguments: tuple[str, ...]
    #: The Test Variant's environment, ``(name, value)`` pairs sorted by name.
    environment: tuple[tuple[str, str], ...]
    #: The seed of every Test Run in the process; ``None`` when unseeded.
    seed: int | None

    @property
    def booley_arguments(self) -> tuple[str, ...]:
        """The arguments Booley renders itself: ``select`` and the seed's.

        Only these may replace a Target plusarg that sets the same option.
        """
        return (*((self.select,) if self.select is not None else ()), *self.seed_arguments)

    @property
    def arguments(self) -> tuple[str, ...]:
        """Every argument in simulator order: ``select``, Test Variant, seed."""
        select = (self.select,) if self.select is not None else ()
        return (*select, *self.variant_arguments, *self.seed_arguments)


@dataclass(frozen=True)
class TargetTestPlan:
    """Every Test Run one Target can run. Build with :func:`build_target_test_plan`."""

    target: str
    adapter: str
    #: Every registered Test, in declaration order; empty when the Target
    #: registers none and has only its unnamed invocation.
    tests: tuple[str, ...]
    #: Registered Tests the default suite leaves out (``skip``).
    skipped: tuple[str, ...]
    #: The Target's effective ``select`` argument template.
    select: str
    variant_tables: tuple[TestVariantTable, ...]
    mechanism: SeedMechanism
    #: The seed of a Test Run that names none; ``None`` on an unseeded Target.
    default_seed: int | None
    #: Names of the plusarg parameters the resolved ``.core`` Target declares.
    target_plusargs: tuple[str, ...] = ()

    def owned_options(self) -> dict[str, str]:
        """Options no Test Variant argument may set here, each with its owner."""
        owned = {
            option_key(self.select.format(index=0, name="probe")): "the select template sets it",
            VERILATOR_SEED_OPTION: "Booley passes the Verilator seed option itself",
        }
        if self.mechanism.template is not None:
            seed_key = option_key(self.mechanism.template.format(seed=DEFAULT_SEED))
            owned[seed_key] = "the seed template sets it"
        for name in self.target_plusargs:
            owned[option_key(f"+{name}")] = (
                "it overrides a plusarg the Target declares; the Target owns its parameter values"
            )
        return owned

    def variants_of(self, test: str) -> TestVariantTable | None:
        """The Variant table *test* declares, or ``None``."""
        return next((table for table in self.variant_tables if table.test == test), None)

    def unseeded_reason(self) -> str:
        """Why this Target cannot apply a seed, and how to change that."""
        return (
            f"Target {self.target!r} is unseeded: its simulator ({self.adapter}) has no "
            "seed option that Booley uses, and tests.toml declares no seed argument "
            'template. Add one (for example seed = "+seed={seed}") that the testbench '
            "reads, or drop the seed"
        )

    def run(
        self, test: str | None, variant: str | None = None, seed: int | None = None
    ) -> TestRun:
        """The Test Run of *test* with *variant* (default when ``None``) and *seed*.

        *seed* ``None`` means the Default Seed. Raises :class:`UnknownTestError`,
        :class:`UnknownVariantError` or :class:`UnseededTargetError`.
        """
        if seed is not None and not self.mechanism.seeded:
            raise UnseededTargetError(self.unseeded_reason())
        resolved_seed = seed if seed is not None else self.default_seed
        if test is None:
            if self.tests:
                raise UnknownTestError(
                    f"Target {self.target!r} registers Tests; a Test Run must name one"
                )
            return build_test_run(None, seed=resolved_seed)
        if test not in self.tests:
            raise UnknownTestError(
                f"unknown Test {test!r} for Target {self.target!r}. "
                f"Declared Tests: {', '.join(self.tests) or 'none'}."
            )
        table = self.variants_of(test)
        if table is None:
            if variant is not None:
                raise UnknownVariantError(
                    f"Test {test!r} of Target {self.target!r} declares no Test Variants, "
                    f"so {test}+{variant} does not exist"
                )
            return build_test_run(test, seed=resolved_seed)
        declared = table.variant(variant or table.default)
        if declared is None:
            names = ", ".join(item.name for item in table.variants)
            raise UnknownVariantError(
                f"Test {test!r} of Target {self.target!r} has no Test Variant "
                f"{variant!r}. Declared Test Variants: {names}."
            )
        return build_test_run(
            test,
            variant=declared.name,
            seed=resolved_seed,
            args=declared.args,
            env=dict(declared.env),
        )

    def default_runs(self) -> tuple[TestRun, ...]:
        """The default suite: each non-skipped Test's default Variant at the Default Seed."""
        if not self.tests:
            return (self.run(None),)
        return tuple(self.run(test) for test in self.tests if test not in self.skipped)

    def is_default_run(self, run: TestRun) -> bool:
        """Whether *run* is its Test's default Variant at the Default Seed."""
        try:
            return run == self.run(run.test)
        except (UnknownTestError, UnknownVariantError):
            return False

    def resolve(
        self,
        selectors: Sequence[str] | None,
        *,
        seed: SeedRequest = None,
        draw: SeedDraw = draw_seeds,
    ) -> tuple[TestRun, ...]:
        """Turn a request into concrete Test Runs, in request order.

        *selectors* ``None`` selects the default suite. *seed* applies to every
        selected run; ``"random"`` draws one seed per Test Run. ``@xN`` draws N
        distinct seeds. Every seed is concrete in the result, so nothing is
        drawn again later.

        Raises :class:`TestRunNameError`, :class:`UnknownTestError`,
        :class:`UnknownVariantError`, :class:`UnseededTargetError` or
        :class:`TestRunLimitError`.
        """
        if selectors is None and not self.tests:
            return (self.unnamed_run(seed, draw=draw),)
        parsed = self._selected(selectors)
        if seed is not None and any(
            not isinstance(item.seed_form, UnpinnedSeed) for item in parsed
        ):
            raise TestRunNameError(SEED_WITH_SEEDED_SELECTOR)
        if seed is not None and not self.mechanism.seeded:
            raise UnseededTargetError(self.unseeded_reason())
        runs: list[TestRun] = []
        for item in parsed:
            runs.extend(self._runs_of(item, seed, draw, runs))
            if len(runs) > MAX_TEST_RUNS_PER_REQUEST:
                raise TestRunLimitError(
                    f"the request asks for more than {MAX_TEST_RUNS_PER_REQUEST} Test Runs "
                    f"of Target {self.target!r}; split it into several requests"
                )
        _refuse_duplicate_runs(runs)
        return tuple(runs)

    def _selected(self, selectors: Sequence[str] | None) -> list[TestSelector]:
        """Parse *selectors*, or select the default suite when there are none."""
        if selectors is not None:
            return [parse_test_selector(text) for text in selectors]
        return [
            TestSelector(test=test, variant=None, seed_form=UnpinnedSeed())
            for test in self.tests
            if test not in self.skipped
        ]

    def _runs_of(
        self,
        selector: TestSelector,
        seed: SeedRequest,
        draw: SeedDraw,
        earlier: Sequence[TestRun],
    ) -> list[TestRun]:
        """The Test Runs one selector stands for."""
        form = selector.seed_form
        base = self.run(selector.test, selector.variant)
        if isinstance(form, PinnedSeed):
            return [self.run(selector.test, selector.variant, form.seed)]
        if isinstance(form, SeedCount):
            if not self.mechanism.seeded:
                raise UnseededTargetError(self.unseeded_reason())
            taken = _seeds_of_variant(earlier, base)
            return [
                self.run(selector.test, selector.variant, drawn)
                for drawn in draw(form.count, exclude=taken)
            ]
        if seed == RANDOM_SEED:
            taken = _seeds_of_variant(earlier, base)
            return [self.run(selector.test, selector.variant, draw(1, exclude=taken)[0])]
        return [self.run(selector.test, selector.variant, seed)] if seed is not None else [base]

    def unnamed_run(self, seed: SeedRequest = None, *, draw: SeedDraw = draw_seeds) -> TestRun:
        """The unnamed invocation of a Target without registered Tests, at *seed*."""
        if seed is not None and not self.mechanism.seeded:
            raise UnseededTargetError(self.unseeded_reason())
        concrete = draw(1)[0] if seed == RANDOM_SEED else seed
        return self.run(None, seed=concrete)

    def default_runs_for(self, tests: Sequence[str]) -> tuple[TestRun, ...]:
        """Each of *tests* as its default Variant at the Default Seed.

        No *tests* means the unnamed invocation. A name outside the registry
        has no declared Variants to apply and runs bare at the Default Seed.
        """
        if not tests:
            return (build_test_run(None, seed=self.default_seed),)
        return tuple(
            self.run(test) if test in self.tests else build_test_run(test, seed=self.default_seed)
            for test in tests
        )

    def require_applicable(self, run: TestRun) -> None:
        """Refuse a Test Run resolved elsewhere that this Target cannot apply as recorded.

        A Test Run can be carried to another checkout of the Target: a
        resumed campaign, or the baseline revision of a Cycle Count
        comparison. If the Target is unseeded there, the recorded seed would
        be dropped silently; if it owns an option one of the run's arguments
        sets, the run would launch with other inputs than it records. Raises
        :class:`UnseededTargetError` or :class:`TestVariantError`.
        """
        if run.seed is not None and not self.mechanism.seeded:
            raise UnseededTargetError(
                f"Test Run {run.name!r} has a seed, but {self.unseeded_reason()}"
            )
        owned = self.owned_options()
        for argument in run.args:
            problem = argument_token_problem(argument)
            owner = owned.get(option_key(argument))
            if problem or owner:
                raise TestVariantError(
                    f"Test Run {run.name!r} of Target {self.target!r}: argument "
                    f"{argument!r} " + (problem or f"is not allowed: {owner}")
                )

    def launch(self, runs: Sequence[TestRun]) -> TestRunLaunch:
        """What one simulator process needs to run *runs*.

        A process that runs exactly one Test Run gets ``select``, the Test
        Variant's arguments and the seed arguments, in that order. A Cocotb
        batch of several selects by filter and gets none.

        Raises :class:`ValueError` when *runs* differ in seed or environment:
        such Test Runs never share a process.
        """
        if len({(run.seed, run.env) for run in runs}) != 1:
            raise ValueError(
                f"Test Runs of Target {self.target!r} that differ in seed or environment "
                "cannot share one simulator process"
            )
        first = runs[0]
        alone = len(runs) == 1
        return TestRunLaunch(
            select=self._select_argument(first) if alone else None,
            variant_arguments=first.args if alone else (),
            seed_arguments=self.mechanism.arguments(first.seed) if alone else (),
            environment=first.env,
            seed=first.seed,
        )

    def _select_argument(self, run: TestRun) -> str | None:
        """The rendered ``select`` argument of a registered *run*; else ``None``."""
        if run.test is None or run.test not in self.tests:
            return None
        return self.select.format(index=self.tests.index(run.test), name=run.test)


def _seeds_of_variant(runs: Sequence[TestRun], base: TestRun) -> Collection[int]:
    """Seeds *runs* already use for the Test Variant of *base*."""
    return {
        run.seed
        for run in runs
        if run.seed is not None and (run.test, run.variant) == (base.test, base.variant)
    }


def _refuse_duplicate_runs(runs: Sequence[TestRun]) -> None:
    """Refuse a request that names one Test Run twice."""
    seen: set[str] = set()
    for run in runs:
        if run.name in seen:
            raise TestRunNameError(
                f"the request selects Test Run {run.name!r} more than once; "
                "each Test Run runs once per request"
            )
        seen.add(run.name)


def _variant_tables(section: Mapping[str, object], where: str) -> tuple[TestVariantTable, ...]:
    """Rebuild the typed Variant tables from a normalized ``tests.toml`` section."""
    raw = section.get("variants")
    if raw is None:
        return ()
    assert isinstance(raw, Mapping), "the loader validated the variants table"
    tables = cast(Mapping[str, object], raw)
    return tuple(
        parse_test_variants(test, table, where=f"{where}.variants.{test}")
        for test, table in tables.items()
    )


def _check_variants_against_target(
    tables: Sequence[TestVariantTable],
    adapter: str,
    target_plusargs: Collection[str],
    where: str,
) -> None:
    """Refuse Variant arguments the resolved Target cannot take."""
    owned = {option_key(f"+{name}") for name in target_plusargs}
    for table in tables:
        for variant in table.variants:
            spot = f"{where}.variants.{table.test}.{variant.name}"
            if adapter == _COCOTB and variant.args:
                raise TestVariantError(
                    f"{spot} carries arguments, but a Cocotb Target's Test Variants "
                    "carry environment only; move the values into env"
                )
            for argument in variant.args:
                if option_key(argument) in owned:
                    raise TestVariantError(
                        f"{spot} argument {argument!r} overrides a plusarg the Target "
                        "declares; the Target owns its parameter values"
                    )


def build_target_test_plan(
    target: str,
    section: Mapping[str, object] | None,
    *,
    adapter: str,
    target_plusargs: Collection[str] = (),
) -> TargetTestPlan:
    """Build the plan of *target* from its normalized ``tests.toml`` section.

    *adapter* is ``cocotb`` for a Cocotb Target, otherwise the simulator.
    *target_plusargs* are the names of the plusarg parameters the ``.core``
    Target declares.

    Raises :class:`TestVariantError`, :class:`SeedConfigurationError` or
    :class:`UnseededTargetError` for a declaration this Target cannot apply.
    """
    section = section or {}
    where = f"tests.toml [{target}]"
    template = section.get("seed")
    assert template is None or isinstance(template, str)
    mechanism = seed_mechanism(adapter, template, where=where)
    tables = _variant_tables(section, where)
    _check_variants_against_target(tables, adapter, target_plusargs, where)
    declared_default = section.get("default_seed")
    plan = TargetTestPlan(
        target=target,
        adapter=adapter,
        tests=tuple(str(name) for name in _names(section.get("tests"))),
        skipped=tuple(str(name) for name in _names(section.get("skip"))),
        select=str(section.get("select") or DEFAULT_TEST_SELECT),
        variant_tables=tables,
        mechanism=mechanism,
        default_seed=None,
        target_plusargs=tuple(target_plusargs),
    )
    if not mechanism.seeded:
        if declared_default is not None:
            raise UnseededTargetError(
                f"{where}.default_seed cannot apply. {plan.unseeded_reason()}"
            )
        return plan
    assert declared_default is None or isinstance(declared_default, int)
    return replace(plan, default_seed=declared_default or DEFAULT_SEED)


def _names(raw: object) -> Sequence[object]:
    """A normalized name list, or empty when the section has none."""
    return cast(list[object], raw) if isinstance(raw, list) else ()


def load_target_test_plan(
    work_dir: Path | str,
    target: str,
    *,
    adapter: str,
    target_plusargs: Collection[str] = (),
) -> TargetTestPlan:
    """Read ``tests.toml`` of the checkout at *work_dir* and build *target*'s plan."""
    sections = load_test_configuration(work_dir)
    section = lookup_target_section(sections, target)
    return build_target_test_plan(
        target, section, adapter=adapter, target_plusargs=target_plusargs
    )


def batch_test_runs(
    runs: Sequence[TestRun], *, one_process_per_run: bool
) -> tuple[tuple[TestRun, ...], ...]:
    """Group *runs* into simulator processes, in order of first appearance.

    Test Runs that differ in arguments, environment or seed never share a
    process. An HDL Target (*one_process_per_run*) launches one process per
    Test Run. A Cocotb Target batches Test Runs with the same environment and
    seed, and a Test appears at most once in a batch because the batch selects
    by Test name.
    """
    if one_process_per_run:
        return tuple((run,) for run in runs)
    batches: list[list[TestRun]] = []
    for run in runs:
        home = next(
            (
                batch
                for batch in batches
                if (batch[0].env, batch[0].seed) == (run.env, run.seed)
                and all(other.test != run.test for other in batch)
            ),
            None,
        )
        if home is None:
            batches.append([run])
        else:
            home.append(run)
    return tuple(tuple(batch) for batch in batches)
