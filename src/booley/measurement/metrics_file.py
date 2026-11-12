"""Load ``metrics.toml``: the Target Metrics a Project declares, and each table's effective rules.

One table per Target and Flow, ``[<target>.<flow>]``, holds absolute bounds
(``<metric>_max`` / ``<metric>_min``), ``track``, ``off`` and ``notes``;
``[defaults.<flow>]`` adds bounds to every table of that Flow. A key is split on
its last dot; the prefix is a clock for a per-clock metric and a test selector
for a per-test metric, as the Flow declares (ADR 0092, issue #3045).

:func:`load_metrics_rules` reads the file of any checkout, collects *every*
load error, and raises them together so ``booley measure --validate`` can list
each one. Entries are compared by their resolved address (metric, limit, Test
Run or clock), never by key text, so ``smoke`` and ``smoke@1`` are one address.

:func:`parse_metrics_as_written` reads a file's text with no checkout, so a
commit's rules can be compared with another commit's from Git objects alone
(#3697): Target keys and test prefixes stay as spelled and the checks that need
the Target catalog or Test Run plan are skipped.
"""

from __future__ import annotations

import json
import math
import re
import tomllib
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any, Literal, cast

from booley.core.booley_dir import resolve_checkout_booley_dir
from booley.core.boundary import is_str_list, require_finite_number_value
from booley.flows.metric_declarations import MetricKind
from booley.flows.target_test_plan import TargetTestPlan
from booley.flows.target_test_plan_loading import load_checkout_target_test_plan
from booley.flows.test_runs import SeedCount, parse_test_selector
from booley.measurement.flow_metrics import flows_with_metric_declarations, metric_declarations
from booley.targets.catalog import TargetCatalog
from booley.targets.domain import FuseSocError

METRICS_FILE = "metrics.toml"
TABLE_FLOWS = flows_with_metric_declarations()

Limit = Literal["max", "min"]
_Address = tuple[str, Limit | None, str | None]  # (metric, limit, Test Run or clock)

_RESERVED_KEYS = ("track", "off", "notes")
_BARE_KEY = re.compile(r"[A-Za-z0-9_-]+")
# `<metric>_increase_at_most`, `<metric>_reduce_at_least`, ... (an optional unit follows).
_RELATIVE_BOUND = re.compile(
    r"(?P<metric>.+?)_(?P<change>increase|reduce)_at_(?P<how>most|least)(?:_[a-z]+)?"
)
_RELATIVE_LIMIT: dict[tuple[str, str], Limit] = {
    ("increase", "most"): "max",
    ("reduce", "least"): "max",
    ("increase", "least"): "min",
    ("reduce", "most"): "min",
}
_FAILING_LINE = re.compile(r"at line (\d+)")
_UNQUOTED_ASSIGNMENT = re.compile(r"\s*(?P<key>[^\"'=\s\[]+)\s*=\s*(?P<value>.+?)\s*")


@dataclass(frozen=True)
class Bound:
    """An enforced metric: ``<metric>_max`` (at most) or ``<metric>_min`` (at least)."""

    key: str  # as written, e.g. "clk_i.fmax_mhz_min"
    metric: str
    limit: Limit
    value: int | float
    prefix: str | None  # clock name or test selector, as written
    test_run: str | None  # the Test Run a per-test prefix resolves to
    note: str | None


@dataclass(frozen=True)
class TrackedMetric:
    """A metric listed in ``track``: reported, never a bound."""

    key: str
    metric: str
    prefix: str | None
    test_run: str | None


@dataclass(frozen=True)
class TableRules:
    """The effective rules of one ``[<target>.<flow>]`` table."""

    target: str  # the Target key as written
    identity: str  # ``vlnv#name``
    flow: str
    bounds: tuple[Bound, ...]
    tracked: tuple[TrackedMetric, ...]


@dataclass(frozen=True)
class MetricsRules:
    """What a checkout's ``metrics.toml`` declares; no tables when it declares nothing."""

    path: Path  # <Booley Directory>/metrics.toml; it may not exist
    tables: tuple[TableRules, ...]


@dataclass(frozen=True)
class WrittenRules:
    """The rules of a ``metrics.toml`` text read without a checkout (keys as spelled)."""

    tables: tuple[TableRules, ...]
    # Default bounds each table's ``off`` removed, by ``(target, flow)``: addresses as
    # written, so "turned off" can be told from "removed". ``TableRules`` stays as it is
    # because it is serialized into every stored Measurement Run.
    switched_off: dict[tuple[str, str], frozenset[_Address]]


@dataclass(frozen=True)
class MetricsLoadError:
    """One problem in the file, naming its table and key where it has them."""

    table: str  # "[sim_core.sim]"; "" for the file or a top-level key
    key: str | None
    message: str
    corrected_line: str | None = None

    def render(self) -> str:
        """The error as printed: ``[table] key: message`` and the corrected line, if any."""
        where = " ".join(part for part in (self.table, f"{self.key}:" if self.key else "") if part)
        text = f"{where} {self.message}" if where else self.message
        if self.corrected_line is not None:
            for line in self.corrected_line.splitlines():
                text += f"\n  write instead: {line}"
        return text


class MetricsFileError(ValueError):
    """``metrics.toml`` has load errors; ``errors`` holds every one."""

    def __init__(self, errors: tuple[MetricsLoadError, ...]) -> None:
        super().__init__(f"{METRICS_FILE} has {len(errors)} load error{'s' * (len(errors) != 1)}")
        self.errors = errors


@dataclass(frozen=True)
class _Ref:
    """A parsed key (bound, tracked metric, ``off`` entry or note): what it addresses."""

    key: str
    metric: str
    limit: Limit | None
    prefix: str | None
    test_run: str | None

    @property
    def address(self) -> _Address:
        """The resolved address two entries are compared by."""
        return (
            self.metric,
            self.limit,
            self.test_run if self.test_run is not None else self.prefix,
        )


@dataclass
class _Body:
    """The parsed content of one table."""

    bounds: list[Bound]
    tracked: list[TrackedMetric]
    off: list[_Ref]
    notes: list[tuple[_Ref, str]]


def toml_key(part: str) -> str:
    """*part* as a TOML key: bare when it can be, else quoted."""
    return part if _BARE_KEY.fullmatch(part) else json.dumps(part)


def table_header(*parts: str) -> str:
    """The TOML table header of dotted *parts*."""
    return "[" + ".".join(toml_key(part) for part in parts) + "]"


def _toml_value(value: object) -> str:
    """*value* written as TOML, or a ``<value>`` placeholder for a kind a bound cannot hold."""
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, int) or (isinstance(value, float) and math.isfinite(value)):
        return repr(value)
    if isinstance(value, str):
        return json.dumps(value)
    return "<value>"


def _flatten(path: str, value: object) -> list[tuple[str, object]]:
    """The ``(dotted path, leaf)`` pairs under a nested table."""
    if not isinstance(value, dict):
        return [(path, value)]
    leaves: list[tuple[str, object]] = []
    for key, child in cast(dict[str, Any], value).items():
        leaves.extend(_flatten(f"{path}.{key}", child))
    return leaves


def _syntax_error(text: str, failure: tomllib.TOMLDecodeError) -> MetricsLoadError:
    """The error for text TOML refused; recognizes an unquoted test selector key."""
    match = _FAILING_LINE.search(str(failure))
    lines = text.splitlines()
    if match and int(match[1]) <= len(lines):
        number = int(match[1])
        assignment = _UNQUOTED_ASSIGNMENT.fullmatch(lines[number - 1])
        headers = [
            header[0]
            for line in lines[: number - 1]
            if (header := re.match(r"\s*\[[^\]]*\]+", line))
        ]
        if assignment and headers and re.search(r"[+@]", assignment["key"]):
            key = assignment["key"]
            return MetricsLoadError(
                headers[-1].strip(),
                key,
                "a test selector with `+` or `@` is not a TOML bare key; quote the whole key",
                f'"{key}" = {assignment["value"]}',
            )
    return MetricsLoadError("", None, f"{METRICS_FILE} is not valid TOML: {failure}")


class _Loader:
    """One load of one ``metrics.toml``; accumulates every error.

    With a *checkout* the Target keys and test prefixes are resolved against its
    catalog and Test Run plans; without one (``None``) they are kept as written.
    """

    def __init__(self, checkout: Path | None) -> None:
        self.checkout = checkout
        self.errors: list[MetricsLoadError] = []
        self.switched_off: dict[tuple[str, str], frozenset[_Address]] = {}
        self._catalog: TargetCatalog | None = None
        self._catalog_failed = False
        self._plans: dict[str, TargetTestPlan | str] = {}

    def error(
        self, table: str, key: str | None, message: str, corrected: str | None = None
    ) -> None:
        """Record a load error."""
        self.errors.append(MetricsLoadError(table, key, message, corrected))

    # --- file structure -------------------------------------------------------------

    def load(self, data: dict[str, Any]) -> tuple[TableRules, ...]:
        """Every table of the parsed file, in file order; errors accumulate in ``self``."""
        defaults = self._load_defaults(data.get("defaults", {}))
        tables: list[TableRules] = []
        identities: dict[tuple[str, str], str] = {}
        for target, sections in data.items():
            if target == "defaults":
                continue
            if not isinstance(sections, dict):
                self.error("", target, self._holds_only(target))
                continue
            for flow, body in cast(dict[str, Any], sections).items():
                if not isinstance(body, dict):
                    self.error(table_header(target), flow, self._holds_only(target))
                    continue
                table = self._load_table(
                    target, flow, cast(dict[str, Any], body), defaults, identities
                )
                if table is not None:
                    tables.append(table)
        return tuple(tables)

    @staticmethod
    def _holds_only(name: str) -> str:
        return f"`{name}` holds only `[{name}.<flow>]` tables, one per Flow"

    def _flow_known(self, header: str, flow: str) -> bool:
        if flow in TABLE_FLOWS:
            return True
        self.error(
            header, None, f"unknown Flow {flow!r}; Flows with a table: {', '.join(TABLE_FLOWS)}"
        )
        return False

    def _load_defaults(self, section: object) -> dict[str, list[Bound]]:
        """The ``[defaults.<flow>]`` bounds by Flow."""
        defaults: dict[str, list[Bound]] = {}
        if not isinstance(section, dict):
            self.error("", "defaults", self._holds_only("defaults"))
            return defaults
        for flow, body in cast(dict[str, Any], section).items():
            header = table_header("defaults", flow)
            if not self._flow_known(header, flow):
                continue
            if not isinstance(body, dict):
                self.error(table_header("defaults"), flow, self._holds_only("defaults"))
                continue
            defaults[flow] = self._parse_body(
                header, flow, cast(dict[str, Any], body), None
            ).bounds
        return defaults

    def _load_table(
        self,
        target: str,
        flow: str,
        body: dict[str, Any],
        defaults: dict[str, list[Bound]],
        identities: dict[tuple[str, str], str],
    ) -> TableRules | None:
        """One ``[<target>.<flow>]`` table's effective rules, or ``None`` after an error."""
        header = table_header(target, flow)
        if not self._flow_known(header, flow):
            return None
        identity = self._resolve_target(header, target)
        if identity is None:
            return None
        if (identity, flow) in identities:
            self.error(header, None, f"same Target and Flow as {identities[(identity, flow)]}")
            return None
        identities[(identity, flow)] = header
        before = len(self.errors)
        parsed = self._parse_body(header, flow, body, identity)
        if len(self.errors) != before:
            return None
        rules = self._effective(header, parsed, defaults.get(flow, []))
        if rules is None:
            return None
        self.switched_off[(target, flow)] = frozenset(ref.address for ref in parsed.off)
        return TableRules(target, identity, flow, rules[0], rules[1])

    def _resolve_target(self, header: str, target: str) -> str | None:
        """The identity (``vlnv#name``) a Target key selects, or ``None`` after an error."""
        if self.checkout is None:
            return target
        if self._catalog_failed:
            return None
        try:
            if self._catalog is None:
                self._catalog = TargetCatalog.build(self.checkout)
        except FuseSocError as exc:
            # One file-level error: every table would only repeat it.
            self._catalog_failed = True
            self.error("", None, str(exc))
            return None
        try:
            return self._catalog.select(target).identity
        except FuseSocError as exc:
            self.error(header, None, str(exc))
            return None

    # --- one table's keys -------------------------------------------------------------

    def _parse_body(
        self, header: str, flow: str, body: dict[str, Any], identity: str | None
    ) -> _Body:
        """Parse the keys of one table (*identity* ``None``: a defaults table)."""
        parsed = _Body([], [], [], [])
        for key, value in body.items():
            if key in _RESERVED_KEYS:
                if identity is None:
                    self.error(
                        header,
                        key,
                        "`[defaults.<flow>]` holds bounds only; put it in a Target table",
                    )
                else:
                    self._parse_reserved(header, flow, identity, key, value, parsed)
            elif isinstance(value, dict):
                self._nested_error(header, key, value)
            else:
                self._parse_bound(header, flow, identity, key, value, parsed)
        return parsed

    def _nested_error(self, header: str, key: str, value: object) -> None:
        """A table nested under the Flow table: an unquoted dotted key parses to one."""
        corrected = [
            f"{json.dumps(path)} = {_toml_value(leaf)}" for path, leaf in _flatten(key, value)
        ]
        self.error(
            header,
            key,
            "a table holds bounds at its top level, and an unquoted dotted key is a nested "
            "table; quote the key",
            "\n".join(corrected) or None,
        )

    def _parse_bound(
        self, header: str, flow: str, identity: str | None, key: str, value: object, out: _Body
    ) -> None:
        ref = self._parse_ref(header, flow, identity, key, bound=True)
        if ref is None:
            return
        try:
            number = require_finite_number_value(value, field=key)
        except ValueError as exc:
            self.error(header, key, str(exc))
            return
        assert ref.limit is not None  # a bound key always ends in _max or _min
        out.bounds.append(
            Bound(key, ref.metric, ref.limit, number, ref.prefix, ref.test_run, None)
        )

    def _parse_reserved(
        self, header: str, flow: str, identity: str, key: str, value: object, out: _Body
    ) -> None:
        """Parse ``track``, ``off`` or ``notes``."""
        if key == "notes":
            self._parse_notes(header, flow, identity, value, out)
        elif not is_str_list(value):
            self.error(header, key, f"`{key}` is a list of strings")
        else:
            for entry in cast(list[str], value):
                ref = self._parse_ref(header, flow, identity, entry, bound=key == "off")
                if ref is None:
                    continue
                if key == "off":
                    out.off.append(ref)
                else:
                    out.tracked.append(TrackedMetric(entry, ref.metric, ref.prefix, ref.test_run))

    def _parse_notes(
        self, header: str, flow: str, identity: str, value: object, out: _Body
    ) -> None:
        if not isinstance(value, dict):
            self.error(header, "notes", '`notes` is a table of `<bound> = "note"`')
            return
        for bound_key, note in cast(dict[str, Any], value).items():
            ref = self._parse_ref(header, flow, identity, bound_key, bound=True)
            if ref is None:
                continue
            if isinstance(note, str):
                out.notes.append((ref, note))
            else:
                self.error(header, bound_key, f"the note must be a string, got {note!r}")

    def _split_key(
        self, header: str, key: str, *, bound: bool
    ) -> tuple[str, Limit | None, str | None] | None:
        """Split a key on its last dot into ``(metric, limit, prefix)``; ``None`` after an error."""
        prefix, _, name = key.rpartition(".")
        prefix_text = prefix or None
        if not bound:
            return (name, None, prefix_text)
        if relative := _RELATIVE_BOUND.fullmatch(name):
            self._relative_error(header, key, prefix_text, relative)
            return None
        if not name.endswith(("_max", "_min")):
            self.error(header, key, "a bound ends in `_max` or `_min`")
            return None
        return (name[:-4], cast(Limit, name[-3:]), prefix_text)

    def _parse_ref(
        self, header: str, flow: str, identity: str | None, key: str, *, bound: bool
    ) -> _Ref | None:
        """Parse a key into the metric it addresses; ``None`` after recording the error."""
        split = self._split_key(header, key, bound=bound)
        if split is None:
            return None
        metric, limit, prefix = split
        declared = {item.name: item for item in metric_declarations(flow)}
        if metric not in declared:
            self.error(
                header, key, f"unknown metric {metric!r}; {flow} declares: {', '.join(declared)}"
            )
            return None
        kind = declared[metric].kind
        if kind is MetricKind.PLAIN and prefix is not None:
            self.error(header, key, f"{metric} takes no prefix")
            return None
        test_run = None
        if kind is MetricKind.PER_TEST:
            if self.checkout is None:
                if not self._check_written_test(header, key, metric, prefix, identity):
                    return None
            else:
                test_run = self._resolve_test_run(header, key, metric, prefix, identity)
                if test_run is None:
                    return None
        return _Ref(key, metric, limit, prefix, test_run)

    def _relative_error(
        self, header: str, key: str, prefix: str | None, relative: re.Match[str]
    ) -> None:
        limit = _RELATIVE_LIMIT[relative["change"], relative["how"]]
        absolute = f"{relative['metric']}_{limit}"
        corrected = f"{json.dumps(f'{prefix}.{absolute}')}" if prefix else absolute
        self.error(
            header,
            key,
            "only absolute bounds exist (`_max` is at most, `_min` is at least)",
            f"{corrected} = <absolute number>",
        )

    # --- test prefixes ----------------------------------------------------------------

    def _needs_target_error(self, header: str, key: str, metric: str) -> None:
        self.error(
            header,
            key,
            f"{metric} is per test and needs a test prefix, which a default cannot hold: "
            "Tests belong to one Target; bound it in the Target's own table",
        )

    def _seed_count_error(self, header: str, key: str, prefix: str) -> bool:
        """Record an error and return ``True`` when *prefix* asks for random seeds (``@xN``).

        Such a prefix draws N random seeds on every load: no stable Test Run to bind to.
        """
        if not isinstance(parse_test_selector(prefix).seed_form, SeedCount):
            return False
        self.error(
            header,
            key,
            f"test prefix {prefix!r} asks for random seeds; name one Test Run "
            "(`<test>` or `<test>@<seed>`)",
        )
        return True

    def _check_written_test(
        self, header: str, key: str, metric: str, prefix: str | None, identity: str | None
    ) -> bool:
        """The checks on a per-test key that need no Test Run plan; ``False`` after an error."""
        if identity is None:
            self._needs_target_error(header, key, metric)
            return False
        try:
            return prefix is None or not self._seed_count_error(header, key, prefix)
        except ValueError:
            return True  # whether the selector names a Test Run needs the plan: not judged

    def _plan(self, checkout: Path, identity: str) -> TargetTestPlan | str:
        """The Target's Test Run plan, or why it cannot be built (cached per load)."""
        if identity not in self._plans:
            try:
                self._plans[identity] = load_checkout_target_test_plan(checkout, identity)
            except (FuseSocError, ValueError, OSError) as exc:
                self._plans[identity] = str(exc)
        return self._plans[identity]

    def _resolve_test_run(
        self, header: str, key: str, metric: str, prefix: str | None, identity: str | None
    ) -> str | None:
        """The one Test Run a per-test key addresses, or ``None`` after recording why not."""
        checkout = self.checkout
        assert checkout is not None  # written keys never resolve a Test Run
        if identity is None:
            self._needs_target_error(header, key, metric)
            return None
        plan = self._plan(checkout, identity)
        if isinstance(plan, str):
            self.error(header, key, f"unknown test prefix {prefix!r}: {plan}")
            return None
        try:
            if prefix is not None and self._seed_count_error(header, key, prefix):
                return None
            runs = plan.default_runs() if prefix is None else plan.resolve([prefix])
        except (ValueError, OSError) as exc:
            self.error(header, key, f"unknown test prefix {prefix!r}: {exc}")
            return None
        if len(runs) == 1:
            return runs[0].name
        if prefix is None:
            names = ", ".join(run.name for run in runs)
            self.error(
                header,
                key,
                f"{metric} is per test; name a test (`<test>.{key}`): the Target runs {names}",
            )
        else:
            self.error(
                header, key, f"test prefix {prefix!r} matches {len(runs)} Test Runs; name one"
            )
        return None

    # --- effective rules --------------------------------------------------------------

    def _effective(
        self, header: str, parsed: _Body, defaults: list[Bound]
    ) -> tuple[tuple[Bound, ...], tuple[TrackedMetric, ...]] | None:
        """Defaults minus ``off`` and overridden bounds, plus the table's own, with notes."""
        before = len(self.errors)
        default_addresses = {_address_of(bound) for bound in defaults}
        for ref in parsed.off:
            if ref.address not in default_addresses:
                self.error(header, ref.key, "`off` names no default bound")
        own: dict[_Address, Bound] = {}
        for bound in parsed.bounds:
            first = own.setdefault(_address_of(bound), bound)
            if first is not bound and self.checkout is not None:
                self.error(header, bound.key, f"same address as `{first.key}`")
        switched_off = {ref.address for ref in parsed.off}
        inherited = [
            bound
            for bound in defaults
            if _address_of(bound) not in switched_off and _address_of(bound) not in own
        ]
        bounds = self._attach_notes(header, [*inherited, *own.values()], parsed.notes)
        slots = {(bound.metric, _where(bound)): bound for bound in bounds}
        seen: dict[tuple[str, str | None], str] = {}
        for tracked in parsed.tracked:
            slot = (tracked.metric, _where(tracked))
            twin = slots.get(slot)
            if self.checkout is None:
                continue  # twins and duplicates are judged by resolved address, not spelling
            if twin is not None:
                self.error(header, tracked.key, f"also bounded by `{twin.key}`")
            elif slot in seen:
                self.error(header, tracked.key, f"same address as `{seen[slot]}`")
            else:
                seen[slot] = tracked.key
        if not bounds and not parsed.tracked:
            self.error(
                header,
                None,
                "no reported metric: add a bound or `track` a metric (turning defaults `off` "
                "leaves nothing to report)",
            )
        if len(self.errors) != before:
            return None
        return tuple(bounds), tuple(parsed.tracked)

    def _attach_notes(
        self, header: str, bounds: list[Bound], notes: list[tuple[_Ref, str]]
    ) -> list[Bound]:
        """*bounds* with each note attached to the bound at its address."""
        index = {_address_of(bound): position for position, bound in enumerate(bounds)}
        noted: dict[_Address, str] = {}
        for ref, note in notes:
            position = index.get(ref.address)
            if position is None:
                if self.checkout is not None:  # as written, a respelled note is dropped
                    self.error(header, ref.key, "`notes` names no effective bound")
            elif ref.address in noted and self.checkout is not None:
                self.error(header, ref.key, f"same address as `{noted[ref.address]}`")
            else:
                noted[ref.address] = ref.key
                bounds[position] = replace(bounds[position], note=note)
        return bounds


def _where(item: Bound | TrackedMetric) -> str | None:
    """The Test Run or clock an entry addresses (``None``: a plain metric)."""
    return item.test_run if item.test_run is not None else item.prefix


def _address_of(bound: Bound) -> _Address:
    """The resolved address a bound is compared by."""
    return (bound.metric, bound.limit, _where(bound))


def _parse_bytes(raw: bytes, loader: _Loader) -> tuple[TableRules, ...]:
    """Decode and load a ``metrics.toml``; raises :class:`MetricsFileError` with every error."""
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise MetricsFileError(
            (MetricsLoadError("", None, f"{METRICS_FILE} is not UTF-8: {exc}"),)
        ) from exc
    try:
        data = tomllib.loads(text)
    except tomllib.TOMLDecodeError as exc:
        raise MetricsFileError((_syntax_error(text, exc),)) from exc
    tables = loader.load(data)
    if loader.errors:
        raise MetricsFileError(tuple(loader.errors))
    return tables


def load_metrics_rules(checkout: Path) -> MetricsRules:
    """Read the ``metrics.toml`` of the Booley Directory of *checkout* and return its rules.

    A missing file declares nothing. Raises :class:`MetricsFileError` holding
    every load error, and ``OSError`` when the file exists but cannot be read.
    """
    path = resolve_checkout_booley_dir(checkout) / METRICS_FILE
    try:
        raw = path.read_bytes()
    except FileNotFoundError:
        return MetricsRules(path, ())
    return MetricsRules(path, _parse_bytes(raw, _Loader(checkout)))


def parse_metrics_as_written(raw: bytes) -> WrittenRules:
    """The rules of ``metrics.toml`` content *raw*, read without a checkout.

    Target keys and test prefixes stay as written, and the checks that need the
    Target catalog or a Test Run plan (unknown Target, unknown test prefix, two
    spellings of one Test Run) are skipped, so a file that :func:`load_metrics_rules`
    accepts always parses. A note whose spelling names no bound is dropped.
    Raises :class:`MetricsFileError` holding every load error.
    """
    loader = _Loader(None)
    return WrittenRules(_parse_bytes(raw, loader), loader.switched_off)
