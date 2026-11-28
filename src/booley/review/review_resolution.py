"""Resolve the Declared Reviews of a checkout to the files each one reviews (issue #3271).

:func:`load_declared_reviews` reads ``<Booley Directory>/reviews.toml``, checks it
against the Target catalog and the checkout's files, and returns, for each Target,
the (role, focus) reviews with the files they cover.
"""

from __future__ import annotations

import os
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from pathlib import Path

from booley.config.protected_inputs import declaration_matches
from booley.core.booley_dir import resolve_checkout_booley_dir
from booley.measurement.metrics_file import table_header
from booley.review.declared_reviews import (
    REVIEWS_FILE,
    PathReviews,
    Resolve,
    ReviewsFileError,
    ReviewsLoadError,
    Role,
    TargetReviews,
    entry_header,
    parse_reviews_as_written,
)
from booley.targets.catalog import TargetCatalog
from booley.targets.domain import FuseSocError, TargetInspection

_ROLES: tuple[Role, ...] = ("rtl", "tb")
_ROLE_NAME = {"rtl": "RTL", "tb": "testbench"}
_DEFAULTS = "[defaults.review]"
_SHOWN = 3  # paths a glob error names


@dataclass(frozen=True)
class DeclaredReview:
    """One review of one focus over some files of a Target."""

    role: Role
    focus: str
    spec: str | None
    resolve: Resolve
    files: frozenset[str]  # Project-relative POSIX paths


@dataclass(frozen=True)
class DeclaredReviews:
    """What a checkout's ``reviews.toml`` declares; nothing when it declares nothing."""

    path: Path  # <Booley Directory>/reviews.toml; it may not exist
    tables: tuple[TargetReviews, ...]  # the effective declarations, defaults applied
    reviews: Mapping[str, frozenset[DeclaredReview]]  # by Target identity (vlnv#name)


def _checkout_files(checkout: Path) -> frozenset[str]:
    """Every file of *checkout* as a POSIX path, skipping ``.git``."""
    found: set[str] = set()
    for directory, names, files in os.walk(checkout):
        names[:] = [name for name in names if name != ".git"]
        base = Path(directory).relative_to(checkout)
        found.update((base / name).as_posix() for name in files)
    return frozenset(found)


def _matches(globs: Iterable[str], paths: Iterable[str]) -> frozenset[str]:
    """The *paths* any of *globs* matches."""
    listed = tuple(globs)
    return frozenset(
        path for path in paths if any(declaration_matches(glob, path) for glob in listed)
    )


class _Checker:
    """Checks the declarations against a checkout and collects every error."""

    def __init__(self, checkout: Path, catalog: TargetCatalog) -> None:
        self.checkout = checkout.resolve()
        self.catalog = catalog
        self.files = _checkout_files(checkout)
        self.errors: list[ReviewsLoadError] = []
        self._specs: set[tuple[str, str]] = set()

    def error(self, table: str, key: str | None, message: str) -> None:
        self.errors.append(ReviewsLoadError(table, key, message))

    def glob_problem(
        self, glob: str, inputs: frozenset[str], wanted: Mapping[Role, frozenset[str]]
    ) -> str | None:
        """Why *glob* does not fit a Target with *inputs*, or ``None`` if it does.

        *wanted* maps each role the glob must reach to that role's files; empty means any.
        """
        matched = _matches((glob,), self.files | inputs)
        if outside := sorted(matched - inputs):
            more = f" and {len(outside) - _SHOWN} more" if len(outside) > _SHOWN else ""
            shown = ", ".join(outside[:_SHOWN])
            return f"glob {glob!r} matches files outside the Target: {shown}{more}"
        if not matched:
            return f"glob {glob!r} matches no file of the Target"
        if wanted and not any(matched & files for files in wanted.values()):
            names = " or ".join(_ROLE_NAME[role] for role in wanted)
            return f"glob {glob!r} matches no {names} file of the Target"
        return None

    def spec(self, header: str, spec: str | None) -> None:
        """Report a *spec* path that is not a file of the checkout, once per written value."""
        if spec is None or (header, spec) in self._specs:
            return
        self._specs.add((header, spec))
        try:
            path = (self.checkout / spec).resolve()
            found = path.is_file() and path.is_relative_to(self.checkout)
        except ValueError:  # an embedded NUL
            found = False
        if not found:
            self.error(header, "spec", f"{spec!r} is not a file of the Project")

    def table(self, table: TargetReviews) -> tuple[TargetInspection, frozenset[str]] | None:
        """Check *table*; return the Target's inspection and the inputs it excludes."""
        header = table_header(table.target, "review")
        try:
            inspection = self.catalog.inspect(self.catalog.select(table.target))
        except FuseSocError as exc:
            self.error(header, None, str(exc))
            return None
        inputs = _inputs(inspection)
        self.spec(_DEFAULTS if "spec" in table.inherited else header, table.spec)
        suffix = f" (from {_DEFAULTS})" if "exclude" in table.inherited else ""
        for glob in table.exclude:
            if problem := self.glob_problem(glob, inputs, {}):
                self.error(header, "exclude", problem + suffix)
        for number, entry in enumerate(table.paths, 1):
            self.entry(entry_header(table.target, number), entry, inputs, inspection)
        return inspection, _matches(table.exclude, inputs)

    def entry(
        self, header: str, entry: PathReviews, inputs: frozenset[str], inspection: TargetInspection
    ) -> None:
        """Check one ``paths`` entry of *table*."""
        if "spec" not in entry.inherited:  # an inherited one was checked with its table
            self.spec(header, entry.spec)
        files = _role_files(inspection)
        wanted: dict[Role, frozenset[str]] = {
            role: files[role] for role in _ROLES if _focuses(entry, role)
        }
        before = len(self.errors)
        for glob in entry.paths:
            if problem := self.glob_problem(glob, inputs, wanted):
                self.error(header, "paths", problem)
        if len(self.errors) != before:
            return
        reached = _matches(entry.paths, inputs)
        for role, role_files in wanted.items():
            if not reached & role_files:
                self.error(
                    header,
                    "paths",
                    f"no glob matches a {_ROLE_NAME[role]} file of the Target, "
                    f"but the entry carries {role} focuses",
                )


def _role_files(inspection: TargetInspection) -> dict[Role, frozenset[str]]:
    """The inputs of each role; include headers are RTL, as in the Reviewer's file classes."""
    tb = frozenset(item.path for item in inspection.inputs if "tb" in item.tags)
    return {"rtl": _inputs(inspection) - tb, "tb": tb}


def _focuses(declared: TargetReviews | PathReviews, role: Role) -> tuple[str, ...]:
    return declared.rtl if role == "rtl" else declared.tb


def _target_reviews(
    table: TargetReviews, inspection: TargetInspection, excluded: frozenset[str]
) -> set[DeclaredReview]:
    """The reviews *table* declares for the Target of *inspection*, files *excluded* left out."""
    files = {role: names - excluded for role, names in _role_files(inspection).items()}
    scopes: list[tuple[TargetReviews | PathReviews, frozenset[str] | None]] = [(table, None)]
    scopes += [(entry, _matches(entry.paths, _inputs(inspection))) for entry in table.paths]
    reviews: set[DeclaredReview] = set()
    for declared, within in scopes:
        for role in _ROLES:
            covered = files[role] if within is None else files[role] & within
            if covered:
                reviews.update(
                    DeclaredReview(role, focus, declared.spec, declared.resolve, covered)
                    for focus in _focuses(declared, role)
                )
    return reviews


def _inputs(inspection: TargetInspection) -> frozenset[str]:
    """The Project-relative paths of every input of the Target."""
    return frozenset(item.path for item in inspection.inputs)


def load_declared_reviews(checkout: Path) -> DeclaredReviews:
    """Read the ``reviews.toml`` of the Booley Directory of *checkout* and resolve it.

    A missing file declares nothing. Each Target with a ``[<target>.review]`` table
    gets its table's reviews and its ``paths`` entries' reviews over the files they
    cover, with excluded files left out; an entry only adds reviews.

    Raises:
        ReviewsFileError: holding every load error, including those that need the
            Target catalog or the checkout's files.
        OSError: the file exists but cannot be read.
        StaleTargetCatalogError: the Project's Targets changed while the catalog was built.
    """
    path = resolve_checkout_booley_dir(checkout) / REVIEWS_FILE
    try:
        raw = path.read_bytes()
    except FileNotFoundError:
        return DeclaredReviews(path, (), {})
    tables = parse_reviews_as_written(raw)
    checker = _Checker(checkout, TargetCatalog.build(checkout))
    reviews: dict[str, frozenset[DeclaredReview]] = {}
    for table in tables:
        checked = checker.table(table)
        if checked is None:
            continue
        inspection, excluded = checked
        identity = inspection.handle.identity
        reviews[identity] = reviews.get(identity, frozenset()) | _target_reviews(
            table, inspection, excluded
        )
    if checker.errors:
        raise ReviewsFileError(tuple(checker.errors))
    return DeclaredReviews(path, tables, reviews)
