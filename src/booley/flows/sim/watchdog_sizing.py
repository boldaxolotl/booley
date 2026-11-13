"""MCP Simulation capacity sizing: Work Units a Simulation request will run."""

from __future__ import annotations

from collections.abc import Collection, Mapping, Sequence
from dataclasses import replace
from pathlib import Path

from booley.config.project_config import (
    lookup_target_section,
)
from booley.flows.target_test_plan import (
    batch_test_runs,
)
from booley.flows.target_test_plan_loading import load_target_test_plan
from booley.flows.test_runs import (
    SeedCount,
    SeedRequest,
    parse_seed_request,
    parse_test_selector,
)
from booley.fusesoc import fusesoc_registry
from booley.targets.catalog import PreparedTargetSelection, TargetCatalog
from booley.targets.selection import TargetSectionConflictError

from .mode import SimulationMode, normalize_simulation_mode
from .test_selection import _get_test_names, _get_test_skips


def _target_is_cocotb(work_dir: Path, target: str) -> bool:
    """Read Cocotb identity through the Target catalog, failing soft for sizing."""
    try:
        return bool(TargetCatalog.build(work_dir).select(target).cocotb_module)
    except Exception:  # noqa: BLE001 — watchdog sizing degrades to native-HDL counting
        return False


def _sizing_identity(work_dir: Path, target: str) -> str:
    """Identity of *target* for sizing; an unresolvable token keeps its bare name.

    The child Flow reports the resolution error, so sizing falls back to the
    bare-name section lookup instead of raising.
    """
    try:
        return TargetCatalog.build(work_dir).select(target, for_flow="sim").identity
    except (fusesoc_registry.FuseSocError, OSError, ValueError):
        return target.rsplit("#", 1)[-1]


def _selected_test_work_units(
    identity: str,
    test_names: Mapping[str, list[str]],
    configured_skips: Mapping[str, list[str]],
    test_selector: Sequence[str] | str | None,
    skip_arg: str | None,
) -> int:
    """Simulator processes a non-cocotb Target will launch, at least one."""
    available = list(lookup_target_section(test_names, identity) or [])
    skips = set(lookup_target_section(configured_skips, identity) or [])
    if skip_arg:
        skips.update(item.strip() for item in skip_arg.split(",") if item.strip())
    if test_selector:
        return max(1, _selector_test_run_count(available, test_selector))
    if not available:
        return 1
    selected = [test for test in available if test not in skips]
    return len(selected or available)


def _selector_test_run_count(available: Sequence[str], test_selector: Sequence[str] | str) -> int:
    """How many Test Runs *test_selector* names among the registered Tests.

    ``test@xN`` counts N. A selector that does not parse, or names an
    unregistered Test, counts nothing: the Flow itself refuses it.
    """
    requested = (test_selector,) if isinstance(test_selector, str) else tuple(test_selector)
    count = 0
    for text in requested:
        try:
            selector = parse_test_selector(text)
        except ValueError:
            continue
        if selector.test in available:
            form = selector.seed_form
            count += form.count if isinstance(form, SeedCount) else 1
    return count


def _resolved_work_units(
    work_dir: Path,
    target: str,
    identity: str,
    registered: Sequence[str],
    skips: Collection[str],
    test_selector: Sequence[str] | None,
    seed: SeedRequest,
) -> int:
    """Simulator processes *target* launches for this request, from its resolved Test Runs.

    The request is resolved the way the Flow resolves it, then grouped into
    processes the way the Flow groups it: one per Test Run on an HDL Target,
    one per environment and seed on a Cocotb Target. A seed Booley would draw
    (``--seed random``, ``test@xN``) is unknown here, so each such Test Run
    counts as its own process, which is the most it can need. Raises
    ``ValueError`` for a request the Flow itself refuses.
    """
    cocotb = _target_is_cocotb(work_dir, target)
    # Sizing only: every HDL simulator launches one process per Test Run, and
    # the seeded one accepts each seed form the real Target might accept.
    plan = load_target_test_plan(
        work_dir, target, identity=identity, adapter="cocotb" if cocotb else "verilator"
    )
    plan = replace(
        plan,
        tests=tuple(registered),
        skipped=tuple(name for name in registered if name in skips),
    )
    drawn: set[int] = set()

    def draw(count: int, *, exclude: Collection[int] = ()) -> tuple[int, ...]:
        taken = drawn | set(exclude)
        seeds = [seed for seed in range(1, count + len(taken) + 1) if seed not in taken][:count]
        drawn.update(seeds)
        return tuple(seeds)

    runs = plan.resolve(test_selector, seed=seed, draw=draw)
    fixed = [run for run in runs if run.seed not in drawn]
    batches = batch_test_runs(fixed, one_process_per_run=not cocotb)
    return len(batches) + len(runs) - len(fixed)


def _sizing_seed(seed: object) -> SeedRequest:
    """The request's ``--seed`` for sizing; a value the Flow refuses counts as none."""
    if seed is None:
        return None
    try:
        return parse_seed_request(seed)
    except ValueError:
        return None


def resolve_sim_campaign_work_units(
    work_dir: Path,
    target_arg: str,
    test_selector: Sequence[str] | str | None = None,
    skip_arg: str | None = None,
    mode: SimulationMode | str = SimulationMode.SIMULATE,
    seed: object = None,
) -> int:
    """Count sequential simulator processes selected by one sim invocation.

    Counts the work items the request resolves to: its Test Runs, grouped
    into processes as the Flow groups them (``test@xN`` counts N; Cocotb
    batches split by environment and seed, also for a default suite). This
    is a preflight read used by the outer MCP watchdog; validation remains
    the child Flow's responsibility, so a request that does not resolve is
    counted from the Test registry alone.
    """
    targets = [item.strip() for item in target_arg.split(",") if item.strip()]
    if not targets:
        try:
            selection = PreparedTargetSelection.resolve(
                work_dir, "", for_flow="sim", select_omitted=True
            )
        except (fusesoc_registry.FuseSocError, OSError, ValueError):
            return 1
        targets = [handle.selector for handle in selection.handles]
    selected_mode = normalize_simulation_mode(mode)
    if selected_mode is SimulationMode.ELAB_ONLY:
        return len(targets)
    if selected_mode is SimulationMode.ELAB_ONLY_STANDALONE:
        return len(targets) + 1
    test_names = _get_test_names(work_dir)
    configured_skips = _get_test_skips(work_dir)
    selectors = (test_selector,) if isinstance(test_selector, str) else test_selector
    units = 0
    for target in targets:
        identity = _sizing_identity(work_dir, target)
        try:
            registered = list(lookup_target_section(test_names, identity) or [])
            skips = set(lookup_target_section(configured_skips, identity) or [])
        except TargetSectionConflictError:
            units += 1  # the child Flow refuses duplicate section keys
            continue
        skips.update(item.strip() for item in (skip_arg or "").split(",") if item.strip())
        try:
            resolved = _resolved_work_units(
                work_dir,
                target,
                identity,
                registered,
                skips,
                tuple(selectors) if selectors else None,
                _sizing_seed(seed),
            )
        except ValueError:
            resolved = 0
        units += resolved or _selected_test_work_units(
            identity, test_names, configured_skips, test_selector, skip_arg
        )
    return max(1, units)
