"""Goal Mode values: Goal arguments, translated Goals, and the Goal Record.

Three layers, each a frozen value:

- :data:`GoalArg` is what an agent passes when it enters Goal Mode: one shape
  per Goal family, each naming its Target (review Goals name a review
  instead). :func:`parse_goal_arg` is the trust boundary for that input and
  :func:`goal_arg_json_schema` is the schema the entry tool will publish, so
  the vocabulary is defined once here.
- :class:`GoalSpec` is one translated Goal. Its ``key`` is the Criterion key
  the Goal is judged by (ADR 0067 D4), so every Booley Flow and Specialist
  keeps publishing evidence under the key it already knows.
- :class:`GoalRecord` is the persisted state of one Goal Mode
  (``record.json``). :meth:`GoalRecord.from_json` validates every field read
  from disk and raises :class:`GoalRecordFormatError` instead of guessing.

Record schema evolution: ``record.json`` carries a ``schema`` version, and a
different version is refused. Within one version, fields may only be added,
and only as optional fields: a record that lacks one (written by an older
Booley) loads it as its default, while a record with a field this code does
not know (written by a newer Booley) is refused with that explanation rather
than having the field silently dropped on the next save. Required fields stay
required.

Nothing here touches the filesystem or Git; :mod:`booley.goals.store` owns
persistence and :mod:`booley.goals.translate` owns GoalArg-to-Goal translation.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import PurePosixPath, PureWindowsPath
from types import MappingProxyType
from typing import Any, Literal

from booley.core.boundary import (
    BoundaryError,
    require_bool_value,
    require_dict,
    require_int,
    require_list,
    require_sha256_digest,
    require_str_value,
    require_uuid4,
)
from booley.criteria.templates import FPGA_IMPL_OK_PARAMS, SYNTHESIS_OK_PARAMS
from booley.criteria.thresholds import CYCLE_COUNT_PARAMS
from booley.runtime.timefmt import parse_timestamp

# ---------------------------------------------------------------------------
# Enumerations
# ---------------------------------------------------------------------------


class GoalState(StrEnum):
    """Lifecycle state of one Goal Mode (ADR 0067 D15)."""

    ENTERING = "entering"
    ACTIVE = "active"
    FINISHING = "finishing"
    FINISHED = "finished"
    ABANDONED = "abandoned"
    FAILED = "failed"


# States in which a Goal Mode still owns its worktree. At most one record per
# worktree may be in one of these states. ``failed`` does not occupy the
# worktree: a failed entry is replaced by a new one (D15).
OCCUPYING_STATES: frozenset[GoalState] = frozenset(
    {GoalState.ENTERING, GoalState.ACTIVE, GoalState.FINISHING}
)

# Every state a saved record may move to from its current state. Terminal
# states have no exits, so a closed record never occupies its worktree again
# and the create-time uniqueness check stays sufficient. Each state may also
# stay put, which is an ordinary field update.
ALLOWED_TRANSITIONS: Mapping[GoalState, frozenset[GoalState]] = {
    GoalState.ENTERING: frozenset(
        {GoalState.ENTERING, GoalState.ACTIVE, GoalState.FAILED, GoalState.ABANDONED}
    ),
    GoalState.ACTIVE: frozenset(
        {GoalState.ACTIVE, GoalState.FINISHING, GoalState.ABANDONED, GoalState.FAILED}
    ),
    GoalState.FINISHING: frozenset(
        {
            GoalState.FINISHING,
            GoalState.ACTIVE,
            GoalState.FINISHED,
            GoalState.ABANDONED,
            GoalState.FAILED,
        }
    ),
    GoalState.FINISHED: frozenset(),
    GoalState.ABANDONED: frozenset(),
    GoalState.FAILED: frozenset(),
}


class GoalFamily(StrEnum):
    """The Goal families an agent may name at entry (ADR 0067 D10)."""

    LINT = "lint"
    SIM = "sim"
    ELAB = "elab"
    SYNTH = "synth"
    FPGA = "fpga"
    CYCLE_COUNT = "cycle_count"
    COVERAGE = "coverage"
    MUTATION = "mutation"
    REVIEW = "review"


# Origin of a Goal named directly rather than through a Goalset.
AD_HOC_ORIGIN = "ad-hoc"

# The review Goals a Goal may name: the base review Criteria
# (``data/criteria.toml``) without their ``review_`` prefix.
REVIEW_KINDS: tuple[str, ...] = (
    "rtl_spec",
    "rtl_bugs",
    "rtl_protocol",
    "rtl_security",
    "rtl_optimization",
    "rtl_code_style",
    "tb_quality",
)
# The one review kind that judges RTL against a spec file the Goal names.
SPEC_REVIEW_KIND = "rtl_spec"

ReviewVerdict = Literal["clean", "done"]
REVIEW_VERDICTS: tuple[ReviewVerdict, ...] = ("clean", "done")

MUTATION_PARAMS: frozenset[str] = frozenset({"scope", "min_detected", "total", "auto"})

# Coverage metrics a coverage Goal may set a floor for (Coverage Criterion).
COVERAGE_METRICS: tuple[str, ...] = ("line", "branch", "expression", "toggle", "cover_property")


PROJECT_SNAPSHOT_NAMES = ("booley.toml", "pipeline.toml", "tests.toml", ".gitignore")


class GoalArgError(ValueError):
    """One Goal argument does not match its family's shape."""


class GoalRecordFormatError(ValueError):
    """A persisted Goal Record value does not match the Goal Record format."""


# ---------------------------------------------------------------------------
# Goal arguments (the entry tool's input)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class TargetGoalArg:
    """A lint, sim, or elab Goal: the named Target passes that Booley Flow."""

    family: Literal[GoalFamily.LINT, GoalFamily.SIM, GoalFamily.ELAB]
    target: str
    origin: str = AD_HOC_ORIGIN


@dataclass(frozen=True)
class ImplementationGoalArg:
    """A synth or fpga Goal with optional QoR thresholds.

    ``baseline`` names the Target a baseline-relative threshold compares
    against at the base commit; ``None`` means the candidate's own name.
    ``thresholds`` keep their authored spelling (percentages as ``"8%"``).
    """

    family: Literal[GoalFamily.SYNTH, GoalFamily.FPGA]
    target: str
    thresholds: Mapping[str, Any] = field(default_factory=dict[str, Any])
    baseline: str | None = None
    origin: str = AD_HOC_ORIGIN


@dataclass(frozen=True)
class CycleCountGoalArg:
    """A Cycle Count Goal: one named test of one Target meets every threshold."""

    target: str
    test: str
    thresholds: Mapping[str, Any]
    origin: str = AD_HOC_ORIGIN
    family: Literal[GoalFamily.CYCLE_COUNT] = GoalFamily.CYCLE_COUNT


@dataclass(frozen=True)
class CoverageGoalArg:
    """A coverage Goal: per-metric floors (percent) over ``tests`` of one Target."""

    target: str
    metrics: Mapping[str, Any]
    tests: Literal["all"] | tuple[str, ...]
    origin: str = AD_HOC_ORIGIN
    family: Literal[GoalFamily.COVERAGE] = GoalFamily.COVERAGE


@dataclass(frozen=True)
class MutationGoalArg:
    """A mutation Goal: the mutation score of one Target meets ``params``."""

    target: str
    params: Mapping[str, Any]
    origin: str = AD_HOC_ORIGIN
    family: Literal[GoalFamily.MUTATION] = GoalFamily.MUTATION


@dataclass(frozen=True)
class ReviewGoalArg:
    """A review Goal: ``clean`` (no findings) or ``done`` (terminal advisory review).

    A spec review names the spec file it judges the RTL against (ADR 0067):
    a path relative to the Goal worktree, without ``..``. Parsing rejects
    absolute and escaping paths; whether the file exists is checked at entry,
    against the worktree (a later phase).
    """

    review: str
    verdict: ReviewVerdict
    spec: str | None = None
    origin: str = AD_HOC_ORIGIN
    family: Literal[GoalFamily.REVIEW] = GoalFamily.REVIEW


# The discriminated union the entry tool accepts; ``family`` is the tag.
GoalArg = (
    TargetGoalArg
    | ImplementationGoalArg
    | CycleCountGoalArg
    | CoverageGoalArg
    | MutationGoalArg
    | ReviewGoalArg
)


# Characters that would make the Criterion grammar read a Target name as a
# structured simulation entry (``tb @ target -> pass``) instead of a name.
_TARGET_NAME = re.compile(r"(?!.*->)[^\s@]+")


def _required_text(raw: Mapping[str, Any], key: str, where: str) -> str:
    """A required non-empty string field, stripped."""
    if key not in raw:
        raise GoalArgError(f"{where}.{key} is required")
    try:
        value = require_str_value(raw[key], field=f"{where}.{key}").strip()
    except BoundaryError as exc:
        raise GoalArgError(str(exc)) from None
    if not value:
        raise GoalArgError(f"{where}.{key} must be a non-empty string")
    return value


def _target_name(raw: Mapping[str, Any], key: str, where: str) -> str:
    """A required Target name that the Criterion grammar reads as one name."""
    name = _required_text(raw, key, where)
    if not _TARGET_NAME.fullmatch(name):
        raise GoalArgError(f"{where}.{key} {name!r} must not contain whitespace, '@', or '->'")
    return name


def _mapping(raw: Mapping[str, Any], key: str, where: str, *, required: bool) -> dict[str, Any]:
    """An optional or required JSON object field."""
    if key not in raw:
        if required:
            raise GoalArgError(f"{where}.{key} is required")
        return {}
    try:
        value = require_dict(raw[key], field=f"{where}.{key}")
    except BoundaryError as exc:
        raise GoalArgError(str(exc)) from None
    if not all(isinstance(name, str) for name in value):
        raise GoalArgError(f"{where}.{key} keys must be strings")
    return value


def _reject_unknown(raw: Mapping[str, Any], allowed: frozenset[str], where: str) -> None:
    unknown = sorted(str(key) for key in raw if key not in allowed)
    if unknown:
        raise GoalArgError(f"{where} has unknown fields: {unknown}")


# A clock name in a clock-scoped threshold (``<clock>.<name>``): Criteria split
# at the first dot and accept any clock spelling (``clk-out``, ``clk[0]``), so
# only dots and whitespace are excluded here.
_CLOCK_NAME = re.compile(r"[^.\s]+")


def _require_threshold_names(
    thresholds: Mapping[str, Any], params: frozenset[str], where: str, *, clock_scoped: bool
) -> None:
    """Refuse every threshold name that is not a known parameter of the family.

    Thresholds are spread into the Criterion authoring entry next to the
    fields that bind the Goal to its Target, so a structural name such as
    ``targets`` or ``test`` must never get through. A per-clock name is
    ``<clock>.<parameter>``, split at the first dot as Criteria do; whether that
    parameter may be clock-scoped is checked by the Criteria rules.
    """
    for name in thresholds:
        clock, dot, base = name.partition(".")
        if not dot:
            clock, base = "", name
        scoped_ok = clock_scoped and bool(_CLOCK_NAME.fullmatch(clock))
        if base not in params or (dot and not scoped_ok):
            raise GoalArgError(
                f"{where} has unknown threshold {name!r}; valid names: {sorted(params)}"
                + (" (optionally '<clock>.<name>')" if clock_scoped else "")
            )


def _origin(raw: Mapping[str, Any], where: str) -> str:
    return _required_text(raw, "origin", where) if "origin" in raw else AD_HOC_ORIGIN


def _parse_target_goal(raw: Mapping[str, Any], family: GoalFamily, where: str) -> TargetGoalArg:
    _reject_unknown(raw, frozenset({"family", "target", "origin"}), where)
    assert family in (GoalFamily.LINT, GoalFamily.SIM, GoalFamily.ELAB)
    return TargetGoalArg(family, _target_name(raw, "target", where), _origin(raw, where))


def _parse_implementation_goal(
    raw: Mapping[str, Any], family: GoalFamily, where: str
) -> ImplementationGoalArg:
    _reject_unknown(
        raw, frozenset({"family", "target", "baseline", "thresholds", "origin"}), where
    )
    assert family in (GoalFamily.SYNTH, GoalFamily.FPGA)
    target = _target_name(raw, "target", where)
    baseline = _target_name(raw, "baseline", where) if "baseline" in raw else None
    params = SYNTHESIS_OK_PARAMS if family is GoalFamily.SYNTH else FPGA_IMPL_OK_PARAMS
    thresholds = _mapping(raw, "thresholds", where, required=False)
    _require_threshold_names(thresholds, params, f"{where}.thresholds", clock_scoped=True)
    return ImplementationGoalArg(
        family,
        target,
        thresholds,
        None if baseline == target else baseline,
        _origin(raw, where),
    )


def _parse_cycle_count_goal(raw: Mapping[str, Any], where: str) -> CycleCountGoalArg:
    _reject_unknown(raw, frozenset({"family", "target", "test", "thresholds", "origin"}), where)
    thresholds = _mapping(raw, "thresholds", where, required=True)
    _require_threshold_names(
        thresholds, CYCLE_COUNT_PARAMS, f"{where}.thresholds", clock_scoped=False
    )
    return CycleCountGoalArg(
        _target_name(raw, "target", where),
        _required_text(raw, "test", where),
        thresholds,
        _origin(raw, where),
    )


def _coverage_tests(raw: Mapping[str, Any], where: str) -> Literal["all"] | tuple[str, ...]:
    if "tests" not in raw:
        raise GoalArgError(f"{where}.tests is required")
    tests = raw["tests"]
    if tests == "all":
        return "all"
    try:
        items = require_list(tests, field=f"{where}.tests")
    except BoundaryError:
        raise GoalArgError(f"{where}.tests must be 'all' or a list of test names") from None
    if not items or not all(isinstance(item, str) and item.strip() for item in items):
        raise GoalArgError(f"{where}.tests must be 'all' or a non-empty list of test names")
    return tuple(str(item).strip() for item in items)


def _parse_coverage_goal(raw: Mapping[str, Any], where: str) -> CoverageGoalArg:
    _reject_unknown(raw, frozenset({"family", "target", "metrics", "tests", "origin"}), where)
    metrics = _mapping(raw, "metrics", where, required=True)
    unknown = sorted(name for name in metrics if name not in COVERAGE_METRICS)
    if unknown:
        raise GoalArgError(f"{where}.metrics has unknown metrics {unknown}")
    return CoverageGoalArg(
        _target_name(raw, "target", where),
        metrics,
        _coverage_tests(raw, where),
        _origin(raw, where),
    )


def _parse_mutation_goal(raw: Mapping[str, Any], where: str) -> MutationGoalArg:
    _reject_unknown(raw, frozenset({"family", "target", "origin"}) | MUTATION_PARAMS, where)
    params = {name: raw[name] for name in sorted(MUTATION_PARAMS) if name in raw}
    return MutationGoalArg(_target_name(raw, "target", where), params, _origin(raw, where))


def _parse_review_goal(raw: Mapping[str, Any], where: str) -> ReviewGoalArg:
    _reject_unknown(raw, frozenset({"family", "review", "verdict", "spec", "origin"}), where)
    review = _required_text(raw, "review", where)
    if review not in REVIEW_KINDS:
        raise GoalArgError(f"{where}.review must be one of {list(REVIEW_KINDS)}, got {review!r}")
    verdict = _required_text(raw, "verdict", where)
    if verdict not in REVIEW_VERDICTS:
        raise GoalArgError(f"{where}.verdict must be 'clean' or 'done', got {verdict!r}")
    spec = _spec_path(raw, where) if "spec" in raw else None
    if (review == SPEC_REVIEW_KIND) != (spec is not None):
        raise GoalArgError(
            f"{where}.spec is required for review {SPEC_REVIEW_KIND!r} and "
            "accepted for no other review"
        )
    return ReviewGoalArg(
        review, "clean" if verdict == "clean" else "done", spec, _origin(raw, where)
    )


def _spec_path(raw: Mapping[str, Any], where: str) -> str:
    """A spec file path relative to the worktree that cannot escape it."""
    spec = _required_text(raw, "spec", where)
    escapes = ".." in re.split(r"[\\/]", spec)
    windows = PureWindowsPath(spec)
    rooted = spec.startswith(("/", "\\")) or bool(windows.drive) or bool(windows.root)
    if rooted or PurePosixPath(spec).is_absolute() or escapes:
        raise GoalArgError(f"{where}.spec {spec!r} must be relative to the worktree, without '..'")
    return spec


def _family(raw: Mapping[str, Any], where: str) -> GoalFamily:
    name = _required_text(raw, "family", where)
    try:
        return GoalFamily(name)
    except ValueError:
        families = [family.value for family in GoalFamily]
        raise GoalArgError(f"{where}.family must be one of {families}, got {name!r}") from None


def parse_goal_arg(raw: object, *, where: str = "goal") -> GoalArg:
    """Validate one untrusted Goal argument and return its typed shape.

    Raises :class:`GoalArgError` naming *where* for an unknown family, a
    missing Target, an unknown field, or a malformed value. Threshold values
    are validated later, by translation, against the Criteria rules.
    """
    try:
        mapping = require_dict(raw, field=where)
    except BoundaryError as exc:
        raise GoalArgError(str(exc)) from None
    family = _family(mapping, where)
    if family in (GoalFamily.LINT, GoalFamily.SIM, GoalFamily.ELAB):
        return _parse_target_goal(mapping, family, where)
    if family in (GoalFamily.SYNTH, GoalFamily.FPGA):
        return _parse_implementation_goal(mapping, family, where)
    if family is GoalFamily.CYCLE_COUNT:
        return _parse_cycle_count_goal(mapping, where)
    if family is GoalFamily.COVERAGE:
        return _parse_coverage_goal(mapping, where)
    if family is GoalFamily.MUTATION:
        return _parse_mutation_goal(mapping, where)
    return _parse_review_goal(mapping, where)


def parse_goal_args(raw: object, *, where: str = "goals") -> tuple[GoalArg, ...]:
    """Validate an untrusted list of Goal arguments, naming each by index."""
    try:
        items = require_list(raw, field=where)
    except BoundaryError as exc:
        raise GoalArgError(str(exc)) from None
    return tuple(
        parse_goal_arg(item, where=f"{where}[{index}]") for index, item in enumerate(items)
    )


# ---------------------------------------------------------------------------
# Entry-tool JSON schema
# ---------------------------------------------------------------------------

_STRING = {"type": "string", "minLength": 1}
_ORIGIN = {
    "type": "string",
    "minLength": 1,
    "description": f"Goalset the Goal came from, or {AD_HOC_ORIGIN!r} (default).",
}
_THRESHOLD_VALUE = {"type": ["number", "string"]}


def _object(family: GoalFamily, properties: dict[str, Any], required: list[str]) -> dict[str, Any]:
    return {
        "type": "object",
        "properties": {"family": {"const": family.value}, **properties, "origin": _ORIGIN},
        "required": ["family", *required],
        "additionalProperties": False,
    }


def _thresholds(params: frozenset[str], *, clock_scoped: bool) -> dict[str, Any]:
    names = "|".join(sorted(params))
    clock = f"(?:{_CLOCK_NAME.pattern}\\.)?" if clock_scoped else ""
    scoping = (
        " Per-clock timing thresholds may be scoped as '<clock>.<name>'." if clock_scoped else ""
    )
    return {
        "type": "object",
        "propertyNames": {"pattern": f"^{clock}(?:{names})$"},
        "additionalProperties": _THRESHOLD_VALUE,
        "description": (
            "Threshold name to value; percentages as '8%'. Names: "
            + ", ".join(sorted(params))
            + "."
            + scoping
        ),
    }


def goal_arg_json_schema() -> dict[str, Any]:
    """The JSON schema of one Goal argument: a ``oneOf`` tagged by ``family``."""
    target = {**_STRING, "description": "Target the Goal binds to; it may not exist yet."}
    variants = [
        _object(family, {"target": target}, ["target"])
        for family in (GoalFamily.LINT, GoalFamily.SIM, GoalFamily.ELAB)
    ]
    for family, params in (
        (GoalFamily.SYNTH, SYNTHESIS_OK_PARAMS),
        (GoalFamily.FPGA, FPGA_IMPL_OK_PARAMS),
    ):
        baseline = {**_STRING, "description": "Baseline Target at the base commit."}
        properties = {
            "target": target,
            "baseline": baseline,
            "thresholds": _thresholds(params, clock_scoped=True),
        }
        variants.append(_object(family, properties, ["target"]))
    cycle = {
        "target": target,
        "test": _STRING,
        "thresholds": _thresholds(CYCLE_COUNT_PARAMS, clock_scoped=False),
    }
    variants.append(_object(GoalFamily.CYCLE_COUNT, cycle, ["target", "test", "thresholds"]))
    metrics = {
        "type": "object",
        "properties": {metric: {"type": "number"} for metric in COVERAGE_METRICS},
        "additionalProperties": False,
        "minProperties": 1,
        "description": "Coverage metric to minimum percent in (0, 100].",
    }
    tests = {"oneOf": [{"const": "all"}, {"type": "array", "items": _STRING, "minItems": 1}]}
    coverage = {"target": target, "metrics": metrics, "tests": tests}
    variants.append(_object(GoalFamily.COVERAGE, coverage, ["target", "metrics", "tests"]))
    mutation = {
        "target": target,
        "scope": {"type": "array", "items": _STRING, "minItems": 1},
        "min_detected": {"type": "integer", "minimum": 1},
        "total": {"type": "integer", "minimum": 1},
        "auto": {"const": True},
    }
    variants.append(_object(GoalFamily.MUTATION, mutation, ["target"]))
    review = {
        "review": {"enum": list(REVIEW_KINDS)},
        "verdict": {"enum": list(REVIEW_VERDICTS)},
        "spec": {**_STRING, "description": f"Spec file; required for {SPEC_REVIEW_KIND!r}."},
    }
    variants.append(_object(GoalFamily.REVIEW, review, ["review", "verdict"]))
    return {"oneOf": variants}


# ---------------------------------------------------------------------------
# Translated Goals and the Goal Record
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class GoalSpec:
    """One translated Goal, judged by the Criterion named ``key``.

    ``target`` is the candidate Target (``None`` for review Goals). ``params``
    are the normalized Criterion parameters. ``origins`` lists every Goalset
    (or :data:`AD_HOC_ORIGIN`) that asked for this Goal; more than one means
    entry merged them (D11).
    """

    key: str
    family: GoalFamily
    target: str | None
    params: dict[str, Any]
    origins: tuple[str, ...]

    def to_json(self) -> dict[str, Any]:
        """The JSON form stored in ``record.json`` and the change log."""
        return {
            "key": self.key,
            "family": self.family.value,
            "target": self.target,
            "params": self.params,
            "origins": list(self.origins),
        }

    @classmethod
    def from_json(cls, raw: object, *, where: str) -> GoalSpec:
        """Parse a stored Goal, raising :class:`GoalRecordFormatError`."""
        mapping = _record_mapping(raw, where)
        _require_keys(mapping, {"key", "family", "target", "params", "origins"}, where)
        family_name = _record_str(mapping["family"], f"{where}.family")
        try:
            family = GoalFamily(family_name)
        except ValueError:
            raise GoalRecordFormatError(f"{where}.family {family_name!r} is unknown") from None
        origins = _record_str_list(mapping["origins"], f"{where}.origins")
        if not origins:
            raise GoalRecordFormatError(f"{where}.origins must not be empty")
        return cls(
            key=_record_str(mapping["key"], f"{where}.key"),
            family=family,
            target=_record_opt_str(mapping["target"], f"{where}.target"),
            params=_record_mapping(mapping["params"], f"{where}.params"),
            origins=tuple(origins),
        )


@dataclass(frozen=True)
class RecordedGoal:
    """A Goal as held by a Goal Record, with the revision that last changed it.

    A run is stamped with ``spec_revision`` when it starts; its evidence is
    dropped if the Goal changed before it finished (D15 fence).
    """

    spec: GoalSpec
    spec_revision: int

    def to_json(self) -> dict[str, Any]:
        """The JSON form stored in ``record.json``."""
        return {**self.spec.to_json(), "spec_revision": self.spec_revision}

    @classmethod
    def from_json(cls, raw: object, *, where: str) -> RecordedGoal:
        """Parse a stored Goal and its revision."""
        mapping = _record_mapping(raw, where)
        _require_keys(mapping, {"spec_revision"}, where, exact=False)
        revision = _record_positive_int(mapping.pop("spec_revision"), f"{where}.spec_revision")
        return cls(GoalSpec.from_json(mapping, where=where), revision)


@dataclass(frozen=True)
class WorktreeIdentity:
    """A worktree as Git knows it, the same from every path spelling (D3).

    ``repository`` identifies the Git common directory: a UUID Booley stores
    inside it, so two mount spellings of one repository agree. ``checkout`` is
    ``main`` for the primary checkout and ``worktrees/<admin name>`` for a
    linked worktree, the Git directory relative to the common directory.
    """

    repository: str
    checkout: str

    @property
    def key(self) -> str:
        """One string naming this worktree, used to name its lock."""
        return f"{self.repository}/{self.checkout}"

    def to_json(self) -> dict[str, str]:
        """The JSON form stored in ``record.json``."""
        return {"repository": self.repository, "checkout": self.checkout}

    @classmethod
    def from_json(cls, raw: object, *, where: str) -> WorktreeIdentity:
        """Parse a stored worktree identity."""
        mapping = _record_mapping(raw, where)
        _require_keys(mapping, {"repository", "checkout"}, where)
        try:
            repository = require_uuid4(mapping["repository"], field=f"{where}.repository")
        except BoundaryError as exc:
            raise GoalRecordFormatError(str(exc)) from None
        checkout = _record_str(mapping["checkout"], f"{where}.checkout")
        if not _CHECKOUT.fullmatch(checkout):
            raise GoalRecordFormatError(f"{where}.checkout {checkout!r} is not a checkout name")
        return cls(repository, checkout)


_CHECKOUT = re.compile(r"main|worktrees/[^/\x00]+")
_COMMIT = re.compile(r"[0-9a-f]{40}|[0-9a-f]{64}")
_TIMESTAMP = re.compile(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z")
RECORD_SCHEMA_VERSION = 1


@dataclass(frozen=True)
class GoalRecord:
    """The persisted state of one Goal Mode (``record.json``, D9 and D15).

    ``revision`` is owned by the store: it starts at 1 when the record is
    created and every save increments it, refusing a stale one. A record not
    yet created has revision 0. ``worktree`` is the identity the record binds
    to and ``worktree_path`` only the path it was last seen at.

    Entry fields (D15): ``original_ref`` is the branch or detached commit
    HEAD was on, ``branch`` the Goal Branch, ``branch_created`` whether entry
    created it. Finish fields: ``validated_head`` and ``package_digest``.
    ``protected_paths``, ``protected_digest`` (working view), and
    ``protected_head_digest`` (HEAD view) are the D7 snapshot.
    ``session_key`` is the entering session, an audit field only (D3).
    ``failure`` explains a ``failed`` record, for example a branch left behind.
    ``paired_project_base_sha`` is the commit a paired Project repository
    (Stealth's separate ``.booley_project`` repository, checked out inside the
    worktree) was on at entry; baseline checkouts use it instead of guessing
    a fork point. ``None`` when the worktree has no paired Project repository.
    """

    id: str
    state: GoalState
    worktree: WorktreeIdentity
    worktree_path: str
    branch: str
    original_ref: str
    base_sha: str
    entered_at: str
    revision: int = 0
    branch_created: bool = False
    session_key: str | None = None
    ended_at: str | None = None
    goals: tuple[RecordedGoal, ...] = ()
    goalsets_used: tuple[str, ...] = ()
    default_skipped: bool = False
    skip_reason: str | None = None
    protected_digest: str | None = None
    protected_paths: tuple[str, ...] = ()
    protected_head_digest: str | None = None
    validated_head: str | None = None
    package_digest: str | None = None
    finish_attempt_digest: str | None = None
    failure: str | None = None
    paired_project_base_sha: str | None = None
    input_topology_digest: str | None = None
    input_paths: Mapping[str, Any] | None = None
    project_snapshot: Mapping[str, str | None] | None = None
    publication_floor: int = 0
    finish_operation: str | None = None
    end_instruction_quote: str | None = None
    abandon_operation: str | None = None

    def to_json(self) -> dict[str, Any]:
        """The JSON form stored in ``record.json``."""
        return {
            "schema": RECORD_SCHEMA_VERSION,
            "id": self.id,
            "revision": self.revision,
            "state": self.state.value,
            "worktree": self.worktree.to_json(),
            "worktree_path": self.worktree_path,
            "branch": self.branch,
            "original_ref": self.original_ref,
            "base_sha": self.base_sha,
            "branch_created": self.branch_created,
            "session_key": self.session_key,
            "entered_at": self.entered_at,
            "ended_at": self.ended_at,
            "goals": [goal.to_json() for goal in self.goals],
            "goalsets_used": list(self.goalsets_used),
            "default_skipped": self.default_skipped,
            "skip_reason": self.skip_reason,
            "protected_digest": self.protected_digest,
            "protected_paths": list(self.protected_paths),
            "protected_head_digest": self.protected_head_digest,
            "validated_head": self.validated_head,
            "package_digest": self.package_digest,
            "finish_attempt_digest": self.finish_attempt_digest,
            "failure": self.failure,
            "paired_project_base_sha": self.paired_project_base_sha,
            "input_topology_digest": self.input_topology_digest,
            "input_paths": None if self.input_paths is None else dict(self.input_paths),
            "project_snapshot": None
            if self.project_snapshot is None
            else dict(self.project_snapshot),
            "publication_floor": self.publication_floor,
            "finish_operation": self.finish_operation,
            "end_instruction_quote": self.end_instruction_quote,
            "abandon_operation": self.abandon_operation,
        }

    @classmethod
    def from_json(cls, raw: object) -> GoalRecord:
        """Parse ``record.json`` content, raising :class:`GoalRecordFormatError`."""
        mapping = _record_mapping(raw, "record")
        unknown = sorted(set(mapping) - set(_RECORD_KEYS) - set(_RECORD_DEFAULTS))
        if unknown:
            raise GoalRecordFormatError(
                f"record has unknown fields {unknown}; it was written by a newer Booley"
            )
        mapping = {**_RECORD_DEFAULTS, **mapping}
        _require_keys(mapping, set(_RECORD_KEYS) | set(_RECORD_DEFAULTS), "record")
        schema = _record_positive_int(mapping["schema"], "record.schema")
        if schema != RECORD_SCHEMA_VERSION:
            raise GoalRecordFormatError(f"record.schema {schema} is not supported")
        record = cls(**_identity_fields(mapping), **_lifecycle_fields(mapping))
        if not 0 <= record.publication_floor <= record.revision:
            raise GoalRecordFormatError("publication_floor must be within record revisions")
        stale = [goal.spec.key for goal in record.goals if goal.spec_revision > record.revision]
        if stale:
            raise GoalRecordFormatError(f"record.goals {stale} are newer than the record")
        return record


# Fields every ``record.json`` of this schema version carries.
_RECORD_KEYS = (
    "schema",
    "id",
    "revision",
    "state",
    "worktree",
    "worktree_path",
    "branch",
    "original_ref",
    "base_sha",
    "entered_at",
)
# Optional fields and the JSON value an older record without them loads as.
_RECORD_DEFAULTS: Mapping[str, Any] = MappingProxyType(
    {
        "branch_created": False,
        "session_key": None,
        "ended_at": None,
        "goals": [],
        "goalsets_used": [],
        "default_skipped": False,
        "skip_reason": None,
        "protected_digest": None,
        "protected_paths": [],
        "protected_head_digest": None,
        "validated_head": None,
        "package_digest": None,
        "finish_attempt_digest": None,
        "failure": None,
        "paired_project_base_sha": None,
        "input_topology_digest": None,
        "input_paths": None,
        "project_snapshot": None,
        "publication_floor": 0,
        "finish_operation": None,
        "end_instruction_quote": None,
        "abandon_operation": None,
    }
)


def _identity_fields(mapping: dict[str, Any]) -> dict[str, Any]:
    """Fields fixed when the record is created."""
    state_name = _record_str(mapping["state"], "record.state")
    try:
        state = GoalState(state_name)
    except ValueError:
        raise GoalRecordFormatError(f"record.state {state_name!r} is unknown") from None
    return {
        "id": _record_str(mapping["id"], "record.id"),
        "revision": _record_positive_int(mapping["revision"], "record.revision"),
        "state": state,
        "worktree": WorktreeIdentity.from_json(mapping["worktree"], where="record.worktree"),
        "worktree_path": _record_str(mapping["worktree_path"], "record.worktree_path"),
        "branch": _record_str(mapping["branch"], "record.branch"),
        "original_ref": _record_str(mapping["original_ref"], "record.original_ref"),
        "base_sha": _record_commit(mapping["base_sha"], "record.base_sha"),
        "branch_created": _record_bool(mapping["branch_created"], "record.branch_created"),
        "session_key": _record_opt_str(mapping["session_key"], "record.session_key"),
        "entered_at": record_timestamp(mapping["entered_at"], "record.entered_at"),
        "paired_project_base_sha": _record_opt_commit(
            mapping["paired_project_base_sha"], "record.paired_project_base_sha"
        ),
    }


def _lifecycle_fields(mapping: dict[str, Any]) -> dict[str, Any]:
    """Fields that entry, Goal changes, and Finish update."""
    goals = _record_list(mapping["goals"], "record.goals")
    ended_at = mapping["ended_at"]
    validated_head = mapping["validated_head"]
    return {
        "ended_at": None if ended_at is None else record_timestamp(ended_at, "record.ended_at"),
        "goals": tuple(
            RecordedGoal.from_json(goal, where=f"record.goals[{index}]")
            for index, goal in enumerate(goals)
        ),
        "goalsets_used": tuple(_record_str_list(mapping["goalsets_used"], "record.goalsets_used")),
        "default_skipped": _record_bool(mapping["default_skipped"], "record.default_skipped"),
        "skip_reason": _record_opt_str(mapping["skip_reason"], "record.skip_reason"),
        "protected_digest": _record_opt_digest(
            mapping["protected_digest"], "record.protected_digest"
        ),
        "protected_paths": tuple(
            _record_str_list(mapping["protected_paths"], "record.protected_paths")
        ),
        "protected_head_digest": _record_opt_digest(
            mapping["protected_head_digest"], "record.protected_head_digest"
        ),
        "validated_head": (
            None
            if validated_head is None
            else _record_commit(validated_head, "record.validated_head")
        ),
        "package_digest": _record_opt_digest(mapping["package_digest"], "record.package_digest"),
        "finish_attempt_digest": _record_opt_digest(
            mapping["finish_attempt_digest"], "record.finish_attempt_digest"
        ),
        "failure": _record_opt_str(mapping["failure"], "record.failure"),
        "input_topology_digest": _record_opt_digest(
            mapping["input_topology_digest"], "input_topology_digest"
        ),
        "input_paths": _record_input_paths(mapping["input_paths"]),
        "project_snapshot": _record_project_snapshot(mapping["project_snapshot"]),
        "publication_floor": _record_nonnegative_int(
            mapping["publication_floor"], "publication_floor"
        ),
        "finish_operation": _record_opt_str(mapping["finish_operation"], "finish_operation"),
        "abandon_operation": None
        if mapping["abandon_operation"] is None
        else require_uuid4(mapping["abandon_operation"], field="abandon_operation"),
        "end_instruction_quote": _record_opt_str(
            mapping["end_instruction_quote"], "end_instruction_quote"
        ),
    }


# ---------------------------------------------------------------------------
# Strict readers for persisted values
# ---------------------------------------------------------------------------


def _record_mapping(raw: object, where: str) -> dict[str, Any]:
    try:
        mapping = require_dict(raw, field=where)
    except BoundaryError as exc:
        raise GoalRecordFormatError(str(exc)) from None
    if not all(isinstance(key, str) for key in mapping):
        raise GoalRecordFormatError(f"{where} keys must be strings")
    return mapping


def _require_keys(
    mapping: Mapping[str, Any], keys: set[str], where: str, *, exact: bool = True
) -> None:
    missing = sorted(keys - set(mapping))
    if missing:
        raise GoalRecordFormatError(f"{where} is missing {missing}")
    unknown = sorted(set(mapping) - keys)
    if exact and unknown:
        raise GoalRecordFormatError(f"{where} has unknown fields {unknown}")


def _record_list(raw: object, where: str) -> list[Any]:
    try:
        return require_list(raw, field=where)
    except BoundaryError as exc:
        raise GoalRecordFormatError(str(exc)) from None


def _record_str(raw: object, where: str) -> str:
    try:
        return require_str_value(raw, field=where)
    except BoundaryError as exc:
        raise GoalRecordFormatError(str(exc)) from None


def _record_opt_str(raw: object, where: str) -> str | None:
    return None if raw is None else _record_str(raw, where)


def _record_str_list(raw: object, where: str) -> list[str]:
    return [
        _record_str(item, f"{where}[{index}]")
        for index, item in enumerate(_record_list(raw, where))
    ]


def _record_bool(raw: object, where: str) -> bool:
    try:
        return require_bool_value(raw, field=where)
    except BoundaryError as exc:
        raise GoalRecordFormatError(str(exc)) from None


def _record_positive_int(raw: object, where: str) -> int:
    try:
        value = require_int(raw, field=where)
    except BoundaryError as exc:
        raise GoalRecordFormatError(str(exc)) from None
    if value < 1:
        raise GoalRecordFormatError(f"{where} must be a positive integer, got {value}")
    return value


def _record_nonnegative_int(raw: object, where: str) -> int:
    try:
        value = require_int(raw, field=where)
    except BoundaryError as exc:
        raise GoalRecordFormatError(str(exc)) from None
    if value < 0:
        raise GoalRecordFormatError(f"{where} must be a nonnegative integer")
    return value


def _record_input_paths(raw: object) -> dict[str, Any] | None:
    from booley.goals.input_identity import parse_bindings

    if raw is None:
        return None
    try:
        return parse_bindings(raw)
    except ValueError as exc:
        raise GoalRecordFormatError(str(exc)) from exc


def _record_project_snapshot(raw: object) -> dict[str, str | None] | None:
    if raw is None:
        return None
    snapshot = _record_mapping(raw, "project_snapshot")
    if set(snapshot) != set(PROJECT_SNAPSHOT_NAMES):
        raise GoalRecordFormatError("project_snapshot has unexpected configuration names")
    result: dict[str, str | None] = {}
    for name, value in snapshot.items():
        if value is not None and not isinstance(value, str):
            raise GoalRecordFormatError("project_snapshot must contain hex strings or null")
        text = value
        if text is not None:
            try:
                bytes.fromhex(text)
            except ValueError as exc:
                raise GoalRecordFormatError("project_snapshot must contain hex bytes") from exc
        result[name] = text
    return result


def _record_commit(raw: object, where: str) -> str:
    value = _record_str(raw, where)
    if not _COMMIT.fullmatch(value):
        raise GoalRecordFormatError(f"{where} must be a full commit id, got {value!r}")
    return value


def _record_opt_commit(raw: object, where: str) -> str | None:
    return None if raw is None else _record_commit(raw, where)


def _record_opt_digest(raw: object, where: str) -> str | None:
    if raw is None:
        return None
    try:
        return require_sha256_digest(raw, field=where)
    except BoundaryError as exc:
        raise GoalRecordFormatError(str(exc)) from None


def record_timestamp(raw: object, where: str) -> str:
    """A canonical second-resolution UTC RFC 3339 timestamp, or :class:`GoalRecordFormatError`."""
    value = _record_str(raw, where)
    if not _TIMESTAMP.fullmatch(value):
        raise GoalRecordFormatError(f"{where} must be a UTC RFC 3339 timestamp, got {value!r}")
    try:
        parse_timestamp(value)
    except ValueError:
        raise GoalRecordFormatError(f"{where} is not a valid timestamp: {value!r}") from None
    return value
