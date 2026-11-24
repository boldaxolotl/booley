"""The Declared Reviews of a ``reviews.toml``, read without a checkout (issue #3271).

The reviews a Target declares live in ``<Booley Directory>/reviews.toml``::

    [defaults.review]  # fills the keys a Target's table leaves out
    resolve = "minor"

    [sim_core.review]
    rtl = ["spec", "bugs"]
    spec = "docs/spec.md"
    exclude = ["rtl/gen/*.sv"]

    [[sim_core.review.paths]]  # adds reviews over the files its globs match
    paths = ["rtl/*.sv"]
    rtl = ["security"]

This module checks everything that needs no checkout; the checks against the
Target catalog and the files are in :mod:`booley.review.review_resolution`.
"""

from __future__ import annotations

import tomllib
from collections.abc import Iterator, Mapping
from dataclasses import dataclass, replace
from pathlib import Path, PurePosixPath
from typing import Any, Final, Literal, cast, get_args

from booley.core.boundary import is_str_list
from booley.measurement.base import resolve_rules_base
from booley.measurement.metrics_file import table_header
from booley.measurement.rules_diff import commit_file
from booley.measurement.run_key import KeyedCommit, booley_dir_at
from booley.runtime.commit_export import versioned_booley_dir
from booley.runtime.pinned_history import FileCommitError
from booley.specialists.reviewer import RTL_FOCUSES, TB_FOCUS_CATEGORIES

REVIEWS_FILE = "reviews.toml"

Role = Literal["rtl", "tb"]
Resolve = Literal["critical", "major", "minor"]

DEFAULT_RESOLVE: Final[Resolve] = "major"
_RESOLVES: Final[tuple[str, ...]] = get_args(Resolve)
_FOCUSES: Final[Mapping[str, tuple[str, ...]]] = {
    "rtl": RTL_FOCUSES,
    "tb": tuple(sorted(TB_FOCUS_CATEGORIES)),
}
_ROLE_PHRASE: Final[Mapping[str, str]] = {"rtl": "an RTL", "tb": "a testbench"}
_TABLE_KEYS: Final = ("rtl", "tb", "spec", "resolve", "exclude")
_ENTRY_KEYS: Final = ("paths", "rtl", "tb", "spec", "resolve")


@dataclass(frozen=True)
class PathReviews:
    """One ``[[<target>.review.paths]]`` entry, after it took ``spec`` and ``resolve``."""

    paths: tuple[str, ...]
    rtl: tuple[str, ...]
    tb: tuple[str, ...]
    spec: str | None
    resolve: Resolve
    inherited: frozenset[str]  # of "spec", "resolve": the keys taken from the Target's table


@dataclass(frozen=True)
class TargetReviews:
    """One ``[<target>.review]`` table after ``[defaults.review]`` filled its missing keys."""

    target: str  # the key as written
    rtl: tuple[str, ...]
    tb: tuple[str, ...]
    spec: str | None
    resolve: Resolve
    exclude: tuple[str, ...]
    paths: tuple[PathReviews, ...]
    inherited: frozenset[str]  # the keys filled from [defaults.review]


@dataclass(frozen=True)
class ReviewsLoadError:
    """One problem in the file, naming its table and key where it has them."""

    table: str  # "[sim_core.review]"; "" for the file or a top-level key
    key: str | None
    message: str

    def render(self) -> str:
        """The error as printed: ``[table] key: message``."""
        where = " ".join(part for part in (self.table, f"{self.key}:" if self.key else "") if part)
        return f"{where} {self.message}" if where else self.message


class ReviewsFileError(ValueError):
    """``reviews.toml`` has load errors; ``errors`` holds every one."""

    def __init__(self, errors: tuple[ReviewsLoadError, ...]) -> None:
        super().__init__(f"{REVIEWS_FILE} has {len(errors)} load error{'s' * (len(errors) != 1)}")
        self.errors = errors


def entry_header(target: str, number: int) -> str:
    """The name of the *number*-th (1-based) ``paths`` entry of *target* in an error."""
    return f"[{table_header(target, 'review', 'paths')}] #{number}"


def _focus_list(role: str) -> str:
    return ", ".join(_FOCUSES[role])


class _Reader:
    """Validates a parsed ``reviews.toml`` and collects every load error."""

    def __init__(self) -> None:
        self.errors: list[ReviewsLoadError] = []

    def error(self, table: str, key: str | None, message: str) -> None:
        self.errors.append(ReviewsLoadError(table, key, message))

    def _strings(self, header: str, key: str, value: object) -> tuple[str, ...] | None:
        if not is_str_list(value):
            self.error(header, key, "must be a list of strings")
            return None
        return tuple(dict.fromkeys(value))

    def _focuses(self, header: str, role: str, value: object) -> tuple[str, ...] | None:
        items = self._strings(header, role, value)
        if items is None:
            return None
        other = "tb" if role == "rtl" else "rtl"
        valid: list[str] = []
        for item in items:
            if item in _FOCUSES[role]:
                valid.append(item)
            elif item in _FOCUSES[other]:
                self.error(
                    header,
                    role,
                    f"{item!r} is {_ROLE_PHRASE[other]} focus, not {_ROLE_PHRASE[role]} focus; "
                    f"use one of: {_focus_list(role)}",
                )
            else:
                self.error(
                    header, role, f"unknown focus {item!r}; use one of: {_focus_list(role)}"
                )
        return tuple(valid)

    def _value(self, header: str, key: str, value: object) -> object | None:
        """The checked *value* of *key*, or ``None`` after an error."""
        if key in ("rtl", "tb"):
            return self._focuses(header, key, value)
        if key == "spec":
            if isinstance(value, str) and value.strip():
                return value
            self.error(header, key, "must be a non-empty path string")
        elif key == "resolve":
            if value in _RESOLVES:
                return value
            self.error(header, key, f"must be one of: {', '.join(_RESOLVES)}")
        else:  # exclude, paths
            return self._strings(header, key, value)
        return None

    def values(
        self, header: str, raw: Mapping[str, Any], allowed: tuple[str, ...]
    ) -> dict[str, Any]:
        """The valid values of the *allowed* keys in *raw*; each problem is an error."""
        checked: dict[str, Any] = {}
        for key, value in raw.items():
            if key not in allowed:  # :meth:`table` reported it
                continue
            result = self._value(header, key, value)
            if result is not None:
                checked[key] = result
        return checked

    def table(self, header: str, value: object, allowed: tuple[str, ...]) -> dict[str, Any] | None:
        """*value* as a table whose only keys are *allowed*, else ``None`` after an error."""
        if not isinstance(value, dict):
            self.error(header, None, "must be a table")
            return None
        table = cast(dict[str, Any], value)
        for key in table:
            if key not in allowed:
                self.error(header, key, f"unknown key; use one of: {', '.join(allowed)}")
        return table

    def read(self, data: Mapping[str, Any]) -> tuple[TargetReviews, ...]:
        defaults = self._defaults(data.get("defaults"))
        tables: list[TargetReviews] = []
        for key, value in data.items():
            if key == "defaults":
                continue
            table = self._target(key, value, defaults)
            if table is not None:
                tables.append(table)
        return tuple(tables)

    def _defaults(self, value: object) -> dict[str, Any]:
        if value is None:
            return {}
        table = self.table("[defaults]", value, ("review",))
        if table is None or "review" not in table:
            return {}
        header = "[defaults.review]"
        review = self.table(header, table["review"], _TABLE_KEYS)
        return {} if review is None else self.values(header, review, _TABLE_KEYS)

    def _target(
        self, key: str, value: object, defaults: Mapping[str, Any]
    ) -> TargetReviews | None:
        table = self.table(table_header(key), value, ("review",))
        if table is None or "review" not in table:
            return None
        header = table_header(key, "review")
        review = self.table(header, table["review"], (*_TABLE_KEYS, "paths"))
        if review is None:
            return None
        before = len(self.errors)
        own = self.values(header, {k: v for k, v in review.items() if k != "paths"}, _TABLE_KEYS)
        effective = {**_BUILT_IN}
        inherited = {name for name in _TABLE_KEYS if name not in own and name in defaults}
        effective.update({name: defaults[name] for name in inherited})
        effective.update(own)
        self._check_focus(header, effective, before)
        entries = self._entries(key, review.get("paths", []), effective)
        return TargetReviews(
            key,
            effective["rtl"],
            effective["tb"],
            effective["spec"],
            effective["resolve"],
            effective["exclude"],
            entries,
            frozenset(inherited),
        )

    def _check_focus(self, header: str, effective: Mapping[str, Any], before: int) -> None:
        """Report a ``spec`` focus without a spec, or no focus at all, once."""
        if "spec" in effective["rtl"] and effective["spec"] is None:
            self.error(header, "spec", "the 'spec' focus needs a spec path; set spec")
        if not effective["rtl"] and not effective["tb"] and len(self.errors) == before:
            self.error(header, "rtl", "declares no focus; set rtl or tb")

    def _entries(
        self, key: str, raw: object, target: Mapping[str, Any]
    ) -> tuple[PathReviews, ...]:
        if not isinstance(raw, list):
            self.error(table_header(key, "review"), "paths", "must be an array of tables")
            return ()
        entries: list[PathReviews] = []
        for number, item in enumerate(cast(list[object], raw), 1):
            entry = self._entry(entry_header(key, number), item, target)
            if entry is not None:
                entries.append(entry)
        return tuple(entries)

    def _entry(self, header: str, item: object, target: Mapping[str, Any]) -> PathReviews | None:
        before = len(self.errors)
        table = self.table(header, item, _ENTRY_KEYS)
        if table is None:
            return None
        own = self.values(header, table, _ENTRY_KEYS)
        if "paths" not in table:
            self.error(header, "paths", "is required: list the globs the entry covers")
        elif "paths" in own and not own["paths"]:
            self.error(header, "paths", "must not be empty")
        effective = {
            "rtl": own.get("rtl", ()),
            "tb": own.get("tb", ()),
            "spec": own.get("spec", target["spec"]),
            "resolve": own.get("resolve", target["resolve"]),
        }
        self._check_focus(header, effective, before)
        if len(self.errors) != before:
            return None
        inherited = frozenset(name for name in ("spec", "resolve") if name not in own)
        return PathReviews(
            own["paths"],
            effective["rtl"],
            effective["tb"],
            effective["spec"],
            effective["resolve"],
            inherited,
        )


_BUILT_IN: Final[Mapping[str, Any]] = {
    "rtl": (),
    "tb": (),
    "spec": None,
    "resolve": DEFAULT_RESOLVE,
    "exclude": (),
}


def parse_reviews_as_written(raw: bytes) -> tuple[TargetReviews, ...]:
    """The effective Declared Reviews of ``reviews.toml`` content *raw*, read without a checkout.

    ``[defaults.review]`` fills each key a Target's table leaves out, key by key
    (the Target's value replaces the default whole; lists are not merged), and
    a Target without a table gets none. Target keys stay as written, and the
    checks that need the Target catalog or the files are skipped.
    Raises :class:`ReviewsFileError` holding every load error.
    """
    try:
        data = tomllib.loads(raw.decode("utf-8"))
    except UnicodeDecodeError as exc:
        raise ReviewsFileError((ReviewsLoadError("", None, f"is not UTF-8: {exc}"),)) from exc
    except tomllib.TOMLDecodeError as exc:
        raise ReviewsFileError((ReviewsLoadError("", None, f"is not valid TOML: {exc}"),)) from exc
    reader = _Reader()
    tables = reader.read(data)
    if reader.errors:
        raise ReviewsFileError(tuple(reader.errors))
    return tables


def declared_reviews_at(repository: Path, keyed: KeyedCommit) -> tuple[TargetReviews, ...]:
    """The effective Declared Reviews of the commit *keyed*, read from Git objects.

    Reads ``reviews.toml`` where :func:`booley.measurement.rules_diff.rules_diff` reads
    ``metrics.toml``: in the measured tree at its Booley Directory, or, when
    ``keyed.booley_commit`` is set, at the root of the Versioned Booley Directory at
    that commit. A missing file declares nothing. No review runs and no checkout is made.

    Raises:
        ReviewsFileError: the file has load errors.
        ValueError: ``keyed.booley_commit`` is set but *repository* has no Versioned
            Booley Directory.
        BooleyDirOutsideCheckoutError: the commit's Booley Directory escapes the checkout.
    """
    if keyed.booley_commit is None:
        source, commit = repository, keyed.commit
        place = booley_dir_at(repository, commit)
    else:
        paired = versioned_booley_dir(repository)
        if paired is None:
            raise ValueError(
                "the measured commit's reviews come from a Versioned Booley Directory, "
                f"but {repository} has none"
            )
        source, commit, place = paired, keyed.booley_commit, PurePosixPath()
    raw = commit_file(source, commit, (place / REVIEWS_FILE).as_posix())
    return () if raw is None else parse_reviews_as_written(raw)


def _spec_paths(declared: tuple[TargetReviews, ...]) -> Iterator[str]:
    for table in declared:
        if table.spec:
            yield table.spec
        yield from (entry.spec for entry in table.paths if entry.spec)


def _spec_changed(
    repository: Path, keyed: KeyedCommit, base: str, rules_base: str | None, spec: str
) -> bool:
    """Whether *spec* differs between the Base and the commit, in the repository that owns it.

    A path under the commit's Booley Directory placement belongs to the Versioned Booley
    Directory when the commit has one: it is compared there, relative to the placement,
    between its rules Base and ``keyed.booley_commit``. Any other path is compared in
    *repository* between *base* and ``keyed.commit``. What cannot be read counts as changed.
    """
    path = PurePosixPath(spec)
    try:
        place = booley_dir_at(repository, keyed.commit)
        if keyed.booley_commit is None or not path.is_relative_to(place):
            return commit_file(repository, base, spec) != commit_file(
                repository, keyed.commit, spec
            )
        paired = versioned_booley_dir(repository)
        if paired is None or rules_base is None:
            return False
        inside = path.relative_to(place).as_posix()
        return commit_file(paired, rules_base, inside) != commit_file(
            paired, keyed.booley_commit, inside
        )
    except (FileCommitError, ValueError):
        return True


def _rules_base_of(repository: Path, keyed: KeyedCommit) -> str | None:
    """The commit of the Versioned Booley Directory the rules are compared with, if any."""
    paired = versioned_booley_dir(repository)
    if keyed.booley_commit is None or paired is None:
        return None
    return resolve_rules_base(paired, keyed.booley_commit).commit


def review_rules_changes(
    repository: Path, keyed: KeyedCommit, base: str, checkout: Path
) -> tuple[str, ...]:
    """What the commit changed in its review rules since *base*: the files, as declared.

    ``reviews.toml`` when the effective Declared Reviews differ (read with
    :func:`declared_reviews_at`; a Base side that cannot be read counts as different), then
    each declared spec path, from either side, whose content differs. A spec that is a
    symlink in *checkout* (the export of the commit) is compared as the link and as its
    target inside the checkout, because the Reviewer reads the target. With a Versioned
    Booley Directory the Base side is its own rules Base; with none, nothing of it
    changed. The commit's own declarations are read as :func:`declared_reviews_at` does and
    their errors propagate.
    """
    after = declared_reviews_at(repository, keyed)
    rules_base = _rules_base_of(repository, keyed)
    before: tuple[TargetReviews, ...] | None = after  # no rules Base: nothing changed there
    if keyed.booley_commit is None or rules_base is not None:
        try:
            before = declared_reviews_at(
                repository, replace(keyed, commit=base, booley_commit=rules_base)
            )
        except (ReviewsFileError, ValueError, FileCommitError):
            before = None
    changed: list[str] = []
    if before is None or frozenset(before) != frozenset(after):
        changed.append(REVIEWS_FILE)
    specs = sorted({*_spec_paths(after), *_spec_paths(before or ())})
    for spec in specs:
        link = checkout / spec
        targets = [spec]
        if link.is_symlink():
            root = checkout.resolve()
            target = link.resolve()
            if target.is_relative_to(root):
                targets.append(target.relative_to(root).as_posix())
        if any(_spec_changed(repository, keyed, base, rules_base, path) for path in targets):
            changed.append(spec)
    return tuple(changed)
