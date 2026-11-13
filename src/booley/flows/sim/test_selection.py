"""Simulation Flow test-selection validation and resolution."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import replace
from pathlib import Path
from typing import TYPE_CHECKING, cast

from booley.config.project_config import (
    load_test_configuration_field,
    lookup_target_section,
)
from booley.flows.execution_persistence import (
    execution_goal_name,
    frozen_test_runs_for,
)
from booley.flows.frozen_test_runs import FrozenTestRuns, resolve_goal_test_runs
from booley.flows.target_test_plan import (
    TargetTestPlan,
)
from booley.flows.target_test_suite import (
    NoRunnableTestsError,
    require_runnable_target_test_suite,
    resolve_target_test_suite,
)
from booley.flows.test_runs import (
    TestRun,
    parse_test_selector,
)
from booley.fusesoc import fusesoc_registry
from booley.runtime.endpoint_execution import (
    EXIT_ERROR,
    EndpointOutcome,
)
from booley.targets.catalog import TargetCatalog

from .campaign_presentation import (
    _diagnostic_test_run_notes,
    _diagnostic_test_runs,
    _with_frozen_runs_of_a_diagnostic_run,
)
from .execution import (
    DefaultSelection,
    NamedTests,
    SelectedTestRuns,
)
from .execution.engine import simulation_adapter_kind, target_test_plan

if TYPE_CHECKING:
    from booley.flows.execution_persistence import FlowExecutionAdapter
    from booley.flows.sim.request import SimRequest
    from booley.flows.sim.test_run_invocation import TestRunInvocation
    from booley.targets.domain import TargetHandle


def _get_test_names(work_dir: Path | str) -> dict[str, list[str]]:
    """Load the per-target test names (tests.toml ``tests``)."""
    return load_test_configuration_field(work_dir, "tests")


def _get_test_skips(work_dir: Path | str) -> dict[str, list[str]]:
    """Load per-target known-hang skip lists (tests.toml ``skip``)."""
    return load_test_configuration_field(work_dir, "skip")


def _get_test_envs(work_dir: Path | str) -> dict[str, dict[str, str]]:
    """Load per-Target simulator environment (tests.toml ``env``)."""
    return load_test_configuration_field(work_dir, "env")


def _get_test_selects(work_dir: Path | str) -> dict[str, str]:
    """Load per-target explicit ``select`` templates (tests.toml ``select``).

    Only *explicitly declared* templates appear here (the rendering default,
    ``DEFAULT_TEST_SELECT``, does not) — exactly the set A2 must reject on a
    Cocotb Target, where selection is the ``COCOTB_TEST_FILTER`` env var and a
    plusarg template is a config contradiction.
    """
    return load_test_configuration_field(work_dir, "select")


def _filter_tests(tests: list[str], exact_names: str | Sequence[str]) -> list[str]:
    """Select registered tests by exact name while preserving request order."""
    requested = (exact_names,) if isinstance(exact_names, str) else tuple(exact_names)
    available = set(tests)
    return [name for name in requested if name in available]


class SimulationTestSelectionMixin:
    """Test-selection validation and resolution for `SimulateFlow`."""

    if TYPE_CHECKING:
        # Host members SimulateFlow provides; Pyright checks them against the host.
        execution_adapter: FlowExecutionAdapter
        _test_runs: TestRunInvocation

        @property
        def args(self) -> SimRequest: ...
        def is_cocotb_target(self, target: str) -> bool: ...
        def _target_identity(self, target: str) -> str: ...
        def _target_handle(self, target: str) -> TargetHandle: ...

    def _validate_prepared_selection(
        self, targets: list[str], test_names_map: dict[str, list[str]]
    ) -> EndpointOutcome | None:
        """Reject all selection/configuration errors before admission."""
        if (error := self._validate_test_selector(targets, test_names_map)) is not None:
            return error
        if (error := self._validate_cocotb_targets(targets)) is not None:
            return error
        if (error := self._validate_test_plans(targets, test_names_map)) is not None:
            return error
        return self._validate_runnable_tests(targets, test_names_map)

    def _effective_skips(self, target: str) -> set[str]:
        """Return Project-configured exact names excluded by default."""
        return set(
            lookup_target_section(
                _get_test_skips(self.args.work_dir), self._target_identity(target)
            )
            or []
        )

    def _validate_test_selector(
        self,
        targets: list[str],
        test_names_map: dict[str, list[str]],
    ) -> EndpointOutcome | None:
        """Reject a ``--test`` that names no registered Test before any sim runs.

        Exact named selection needs a registered test catalog (tests.toml
        ``tests``). A target without one refuses any ``--test``; the value is
        never passed through raw. A target with one refuses a selector whose
        parsed Test is not in the catalog (an exact match, not a substring):
        running it would select nothing and could report a false PASS.
        Mirrors the ``UnknownTargetError`` contract ``--target`` already gets.

        Returns an ``EXIT_ERROR`` EndpointOutcome on the first offending
        target, else ``None``.
        """
        selectors = self.args.test
        if not selectors:
            return None
        for target in targets:
            available = lookup_target_section(test_names_map, self._target_identity(target)) or []
            if not available:
                return EndpointOutcome(
                    exit_code=EXIT_ERROR,
                    report_text=(
                        f"sim: exact named selection requires target {target!r} "
                        "to have a registered test catalog"
                    ),
                )
            unknown = [
                name for name in selectors if parse_test_selector(name).test not in available
            ]
            if unknown:
                return EndpointOutcome(
                    exit_code=EXIT_ERROR,
                    report_text=(
                        f"sim: unknown exact test(s) for target {target!r}: "
                        f"{', '.join(unknown)}. Declared tests: {', '.join(available)}."
                    ),
                )
        return None

    def _validate_cocotb_targets(self, targets: list[str]) -> EndpointOutcome | None:
        """Reject a plusarg ``select`` template on a Cocotb Target (A2).

        On a Cocotb Target test selection is the ``COCOTB_TEST_FILTER`` env var
        Booley builds from the ``tests`` list — a plusarg ``select`` template is
        a config contradiction, rejected up front (a setup-time error, ADR 0034
        decision 5) rather than silently ignored. ``skip`` works unchanged.
        Returns an ``EXIT_ERROR`` EndpointOutcome on the first offending target,
        else ``None``.
        """
        selects = _get_test_selects(self.args.work_dir)
        for target in targets:
            if lookup_target_section(
                selects, self._target_identity(target)
            ) is not None and self.is_cocotb_target(target):
                return EndpointOutcome(
                    exit_code=EXIT_ERROR,
                    report_text=(
                        f"sim: tests.toml [{target}] declares a `select` "
                        f"plusarg template, but {target!r} is a Cocotb Target "
                        "(its .core flow options declare a cocotb_module). "
                        "Cocotb test selection is driven by the "
                        "COCOTB_TEST_FILTER environment variable built from "
                        "the `tests` list — remove the `select` key."
                    ),
                )
        return None

    def _target_test_plan(self, target: str) -> TargetTestPlan:
        """The Test Run plan of *target*, checked against its resolved ``.core``."""
        handle = self._target_handle(target)
        inspection = TargetCatalog.build(handle.project_root).inspect(handle)
        adapter = simulation_adapter_kind(inspection.eda_tool, inspection.flow_options)
        return target_test_plan(handle, adapter, inspection.parameters)

    def _validate_test_plans(
        self, targets: list[str], test_names_map: Mapping[str, list[str]] | None = None
    ) -> EndpointOutcome | None:
        """Reject a tests.toml declaration its resolved Target cannot apply.

        The loader cannot see the ``.core`` Target, so the checks that need it
        (ADR 0072) run here before anything is built: a Test Variant that
        overrides a Target plusarg, arguments or a ``seed`` template on a
        Cocotb Target, and ``default_seed`` on a Target that cannot be seeded.
        """
        if test_names_map is None:
            test_names_map = _get_test_names(self.args.work_dir)
        for target in targets:
            try:
                self._resolve_test_runs(target, test_names_map)
            except fusesoc_registry.FuseSocError:
                # Target resolution reports its own error on the run path.
                continue
            except ValueError as exc:
                return EndpointOutcome(exit_code=EXIT_ERROR, report_text=f"sim: {exc}")
        return None

    def _frozen_test_runs(self) -> FrozenTestRuns | None:
        """The Test Runs this run's Goals are frozen to; ``None`` outside Goal Mode.

        Read once per invocation: what the invocation runs and what its
        report calls diagnostic come from one reading.
        """
        if not self._test_runs.frozen:
            self._test_runs.frozen = (frozen_test_runs_for(self.execution_adapter),)
        return self._test_runs.frozen[0]

    def _with_diagnostic_test_run_note(self, result: EndpointOutcome) -> EndpointOutcome:
        """Name the Test Runs of this invocation that are evidence for no Goal (ADR 0072).

        Inside Goal Mode a Test Run is evidence only by matching a frozen Test
        Run of a Goal. Every other Test Run is diagnostic: it is reported, and
        a failing one prominently, but it blocks no Goal.
        """
        frozen = self._frozen_test_runs()
        if frozen is None:
            return result
        if self.args.diagnostic:
            adapter = self.execution_adapter
            return _with_frozen_runs_of_a_diagnostic_run(
                result, frozen, self._target_names, lambda key: execution_goal_name(adapter, key)
            )
        fresh = [
            f"{target}: {selector}"
            for target, selectors in self._test_runs.fresh_seed_selectors.items()
            for selector in selectors
        ]
        diagnostic, failures = _diagnostic_test_runs(result.detail, frozen, self._target_names)
        if not (fresh or diagnostic):
            return result
        result.detail["diagnostic_test_runs"] = {
            "test_runs": diagnostic,
            "failures": failures,
            "fresh_seed_selectors": fresh,
        }
        note = "\n".join(_diagnostic_test_run_notes(fresh, diagnostic, failures))
        result.report_text = f"{result.report_text}\n{note}" if result.report_text else note
        return result

    def _validate_runnable_tests(
        self,
        targets: list[str],
        test_names_map: dict[str, list[str]],
    ) -> EndpointOutcome | None:
        """Reject Targets whose skip policy excludes every declared test."""
        if self.args.test:
            return None  # an explicit selector deliberately overrides skips
        for target in targets:
            available = list(
                lookup_target_section(test_names_map, self._target_identity(target)) or []
            )
            try:
                require_runnable_target_test_suite(
                    target,
                    identity=self._target_identity(target),
                    test_names={target: available},
                    test_skips={target: list(self._effective_skips(target))},
                )
            except NoRunnableTestsError as exc:
                return EndpointOutcome(
                    exit_code=EXIT_ERROR,
                    report_text=(
                        f"sim: {exc}. Tests are excluded by tests.toml `skip`. "
                        "Remove a configured skip or explicitly select a test "
                        "with --test."
                    ),
                )
        return None

    def _resolve_tests_to_run(
        self,
        target: str,
        test_names_map: dict[str, list[str]],
    ) -> list[str | None]:
        """Resolve the list of tests to run for a target, minus skip list.

        Project-configured skips prune known-hanging tests so
        they don't each burn the full per-test wall-clock budget. An explicit
        ``--test`` selector that matches *only* skipped tests still runs them —
        naming a test by hand is a clear override of the skip list. When every
        known test is skipped, pre-run validation rejects the Target rather
        than passing vacuously or executing known-hanging tests.
        """
        try:
            runs = self._resolve_test_runs(target, test_names_map)
        except fusesoc_registry.FuseSocError:
            # The Target does not resolve; the run path reports that. Name the
            # registered Tests so plans and labels still describe the request.
            return self._registered_tests_to_run(target, test_names_map)
        return list(dict.fromkeys(run.test for run in runs))

    def _registered_tests_to_run(
        self, target: str, test_names_map: Mapping[str, list[str]]
    ) -> list[str | None]:
        """Selected Test names from the registry alone, without the ``.core`` Target."""
        if self.args.test:
            available = lookup_target_section(test_names_map, self._target_identity(target)) or []
            wanted = [parse_test_selector(text).test for text in self.args.test]
            return cast(list[str | None], _filter_tests(available, wanted))
        suite = resolve_target_test_suite(
            target,
            identity=self._target_identity(target),
            test_names=test_names_map,
            test_skips=_get_test_skips(self.args.work_dir),
        )
        return list(suite.tests)

    def _resolve_test_runs(
        self,
        target: str,
        test_names_map: Mapping[str, list[str]],
    ) -> tuple[TestRun, ...]:
        """The concrete Test Runs this invocation runs on *target* (ADR 0072).

        Resolved once per checkout and Target, before any plan or manifest is
        built: a seed drawn for ``--seed random`` or ``test@xN`` is concrete
        from then on and is never drawn again. Raises ``ValueError`` (the Test
        Run error types) for a request this Target cannot run.
        """
        cache = self._test_runs.resolved
        key = (str(Path(self.args.work_dir)), target)
        if key not in cache:
            plan = self._registered_test_plan(target, test_names_map)
            self._test_runs.default_seeds.setdefault(target, plan.default_seed)
            cache[key] = self._plan_test_runs(plan, target)
        return cache[key]

    def _plan_test_runs(self, plan: TargetTestPlan, target: str) -> tuple[TestRun, ...]:
        """Resolve this request on *plan*; inside Goal Mode from the frozen Test Runs.

        A request with ``--seed`` names its own seeds and always resolves from
        the current ``tests.toml``. Without it, a Target run without ``--test``
        runs the frozen Test Runs of its Target Goal, and ``test@xN`` runs the
        frozen seeds of the Goal with that Target, Test Variant and N; a
        ``test@xN`` no Goal names draws new seeds and is reported diagnostic.
        """
        selectors = tuple(self.args.test) if self.args.test else None
        seed = getattr(self.args, "seed", None)
        frozen = self._frozen_test_runs()
        if frozen is None or seed is not None:
            return plan.resolve(selectors, seed=seed)
        runs, fresh = resolve_goal_test_runs(plan, selectors, frozen, self._target_names(target))
        if fresh:
            self._test_runs.fresh_seed_selectors[target] = fresh
        return runs

    def _target_names(self, target: str) -> set[str]:
        """What a Goal may call *target*: its selector, and its identity when it resolves."""
        try:
            return {target, self._target_handle(target).identity}
        except fusesoc_registry.FuseSocError:
            return {target}

    def _registered_test_plan(
        self, target: str, test_names_map: Mapping[str, list[str]]
    ) -> TargetTestPlan:
        """*target*'s Test Run plan over the registry this invocation resolved."""
        plan = self._target_test_plan(target)
        registered = tuple(
            lookup_target_section(test_names_map, self._target_identity(target)) or ()
        )
        skipped = tuple(name for name in registered if name in self._effective_skips(target))
        if (registered, skipped) == (plan.tests, plan.skipped):
            return plan
        return replace(plan, tests=registered, skipped=skipped)

    def _test_run_selection(
        self, target: str, test_names_map: Mapping[str, list[str]]
    ) -> SelectedTestRuns | NamedTests | DefaultSelection:
        """The execution selection of *target*: its resolved Test Runs.

        Raises ``FuseSocError`` when the Target does not resolve; planning
        reports it by Target. The Tests' defaults are never selected instead.
        """
        runs = self._resolve_test_runs(target, test_names_map)
        if not runs:
            return DefaultSelection()
        return SelectedTestRuns(runs)
