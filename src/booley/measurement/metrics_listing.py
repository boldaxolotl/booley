"""List every Target's metrics as ``metrics.toml`` text (``booley cheat --metrics``).

One block per Target and runnable Flow: the ``[<target>.<flow>]`` header and, per
declared metric, an empty ``<key> =`` line with its unit and kind in a comment.
The user types a number after each ``=`` they keep and deletes the rest, so every
printed header and key is one :mod:`booley.measurement.metrics_file` accepts:

* the limit follows the metric's direction: ``_max`` when lower is better,
  ``_min`` when higher is better;
* a per-test key is prefixed with a selector that names exactly one Test Run: the
  bare Test name for its default Variant, ``test+variant`` for another;
* a per-clock key is prefixed with a clock name from the newest stored
  Measurement Run of the table, and is unprefixed until one exists.
"""

from __future__ import annotations

from pathlib import Path

from booley.core.booley_dir import main_booley_dir_of
from booley.flows.metric_declarations import Better, MetricDeclaration, MetricKind
from booley.flows.target_test_plan import TargetTestPlan
from booley.flows.target_test_plan_loading import load_checkout_target_test_plan
from booley.measurement.flow_metrics import metric_declarations
from booley.measurement.metrics_file import TABLE_FLOWS, table_header, toml_key
from booley.measurement.run_store import MeasurementStore
from booley.targets.catalog import TargetCatalog
from booley.targets.domain import FuseSocError, TargetHandle

NO_TARGET_FLOW = "# No Target runs a Flow that declares metrics."
CLOCK_FALLBACK = (
    "# Clock names appear after the first measurement; a single-clock design needs no prefix."
)

# A Test Run selector for a metric key: the prefix, and whether the default suite skips it.
_TestKey = tuple[str | None, bool]


def render_metrics_listing(project_root: Path) -> str:
    """Every Target's runnable Flows and their metrics as ``metrics.toml`` text.

    Raises ``FuseSocError`` when the Target catalog cannot be built, and
    ``OSError`` or ``ValueError`` when the Project's files cannot be read.
    """
    catalog = TargetCatalog.build(project_root)
    store = MeasurementStore(main_booley_dir_of(project_root))
    blocks: list[str] = []
    for handle in catalog.list():
        flows = _runnable_flows(handle)
        if not flows:
            continue
        selector = _table_selector(catalog, handle)
        if selector.startswith("# "):
            blocks.append(selector)
            continue
        tests = _test_keys(project_root, handle) if "sim" in flows else ()
        blocks.extend(_flow_block(store, handle, selector, flow, tests) for flow in flows)
    return "\n\n".join(blocks) if blocks else NO_TARGET_FLOW


def _runnable_flows(handle: TargetHandle) -> tuple[str, ...]:
    """The Flows with a ``metrics.toml`` table that *handle* can run, in table order.

    Coverage is not a Target-aware Flow of its own: a Verilator Simulation Target runs it.
    """
    coverage = "sim" in handle.drivable_by and handle.eda_tool == "verilator"
    return tuple(
        flow
        for flow in TABLE_FLOWS
        if flow in handle.drivable_by or (flow == "coverage" and coverage)
    )


def _table_selector(catalog: TargetCatalog, handle: TargetHandle) -> str:
    """The Target key a ``metrics.toml`` header uses for *handle*, or a ``# `` comment saying why none does.

    ``defaults`` is the Flow-wide table, so a Target of that name is written by
    identity. The loader must select the same Target back from the key.
    """
    key = handle.identity if handle.selector == "defaults" else handle.selector
    try:
        reached = catalog.select(key).identity
    except FuseSocError as exc:
        return _unreachable(handle, " ".join(str(exc).split()))
    if reached != handle.identity:
        return _unreachable(handle, f"it selects {reached}")
    return key


def _unreachable(handle: TargetHandle, reason: str) -> str:
    return f"# {handle.identity}: no metrics.toml selector reaches this Target: {reason}"


def _test_keys(project_root: Path, handle: TargetHandle) -> tuple[_TestKey, ...] | str:
    """The per-test key prefixes of *handle*, or why they cannot be listed.

    A Target without registered Tests has one unnamed Test Run, which takes no prefix.
    """
    try:
        plan = load_checkout_target_test_plan(project_root, handle.identity)
    except (FuseSocError, ValueError, OSError) as exc:
        return " ".join(str(exc).split())
    return _plan_keys(plan)


def _plan_keys(plan: TargetTestPlan) -> tuple[_TestKey, ...]:
    """A selector for each Test's default Variant and for each other Variant."""
    if not plan.tests:
        return ((None, False),)
    keys: list[_TestKey] = []
    for test in plan.tests:
        skipped = test in plan.skipped
        keys.append((test, skipped))
        table = plan.variants_of(test)
        if table is not None:
            keys.extend(
                (f"{test}+{variant.name}", skipped)
                for variant in table.variants
                if variant.name != table.default
            )
    return tuple(keys)


def _flow_block(
    store: MeasurementStore,
    handle: TargetHandle,
    selector: str,
    flow: str,
    tests: tuple[_TestKey, ...] | str,
) -> str:
    """The header and metric lines of one ``[<target>.<flow>]`` table."""
    lines = [table_header(selector, flow)]
    clocks = store.clock_names(handle.identity, flow)
    for declaration in metric_declarations(flow):
        if declaration.kind is MetricKind.PER_TEST:
            lines.extend(_per_test_lines(declaration, tests))
        elif declaration.kind is MetricKind.PER_CLOCK:
            lines.extend(_per_clock_lines(declaration, clocks, first=CLOCK_FALLBACK not in lines))
        else:
            lines.append(_key_line(None, declaration))
    return "\n".join(lines)


def _per_test_lines(
    declaration: MetricDeclaration, tests: tuple[_TestKey, ...] | str
) -> list[str]:
    if isinstance(tests, str):
        return [f"# {declaration.name} ({_unit_kind(declaration)}): no test selector: {tests}"]
    return [
        _key_line(prefix, declaration, "skipped by default" if skipped else "")
        for prefix, skipped in tests
    ]


def _per_clock_lines(
    declaration: MetricDeclaration, clocks: tuple[str, ...], *, first: bool
) -> list[str]:
    """One key per stored clock, else the unprefixed key under one explanatory comment."""
    if clocks:
        return [_key_line(clock, declaration) for clock in clocks]
    return [*([CLOCK_FALLBACK] if first else []), _key_line(None, declaration)]


def _unit_kind(declaration: MetricDeclaration) -> str:
    """``<unit>, <kind>``: what a key's comment says about its metric."""
    return f"{declaration.unit}, {declaration.kind.value.replace('_', '-')}"


def _key_line(prefix: str | None, declaration: MetricDeclaration, note: str = "") -> str:
    """``<key> =  # <unit>, <kind>``: a bound with no number yet."""
    limit = "max" if declaration.better is Better.LOWER else "min"
    name = f"{declaration.name}_{limit}"
    key = toml_key(name if prefix is None else f"{prefix}.{name}")
    comment = f"{_unit_kind(declaration)}, {note}" if note else _unit_kind(declaration)
    return f"{key} =  # {comment}"
