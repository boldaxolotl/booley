"""The one rule for which Coverage Points are uncovered, and the point filters.

The Coverage Analyst's evidence tool and ``booley coverage uncovered`` both read
points through this module, so they never disagree about what "covered" means.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass

from .coverage_campaign import CoveragePoint


def coverage_point_covered(point: CoveragePoint) -> bool:
    """A point is covered when any Test Run hit it."""
    return sum(point.hits_by_run.values()) > 0


def coverage_point_matches(
    point: CoveragePoint,
    *,
    metric: str | None = None,
    source: str | None = None,
    covered: bool | None = None,
    disposition: str | None = None,
) -> bool:
    """Exact match on every filter that is not ``None``."""
    return (
        (metric is None or point.identity.metric == metric)
        and (source is None or point.identity.source == source)
        and (covered is None or coverage_point_covered(point) is covered)
        and (disposition is None or point.disposition["kind"] == disposition)
    )


@dataclass(frozen=True)
class UncoveredPoints:
    """Eligible never-hit points, in listing order, and the waived ones left out."""

    points: tuple[CoveragePoint, ...]
    waived_omitted: int


def uncovered_points(
    points: Iterable[CoveragePoint], *, metric: str | None = None, source: str | None = None
) -> UncoveredPoints:
    """Select eligible never-hit points sorted by source, line, then metric.

    Hierarchy, detail, and point id break ties so the order is deterministic.
    ``waived_omitted`` counts the waived never-hit points the same filters match:
    the ones the listing would otherwise have shown.
    """
    eligible: list[CoveragePoint] = []
    waived_omitted = 0
    for point in points:
        if not coverage_point_matches(point, metric=metric, source=source, covered=False):
            continue
        if point.disposition["kind"] == "eligible":
            eligible.append(point)
        elif point.disposition["kind"] == "waived":
            waived_omitted += 1
    eligible.sort(key=_listing_order)
    return UncoveredPoints(points=tuple(eligible), waived_omitted=waived_omitted)


def coverage_point_detail(point: CoveragePoint) -> str:
    """The point's subject value as text, such as ``if``, ``wr_ptr[2]:1->0`` or ``0``.

    A valid point's subject always has at least one entry, but its value may be
    empty; ``-`` stands in so a listing line keeps all four columns.
    """
    return str(next(iter(point.identity.subject.values()))) or "-"


def coverage_point_line(point: CoveragePoint) -> int:
    """The first source line of a point; a loaded Campaign guarantees it is an integer."""
    start = point.identity.location["start"]
    assert isinstance(start, Mapping)
    line = start["line"]
    assert isinstance(line, int)
    return line


def _listing_order(point: CoveragePoint) -> tuple[str, int, str, str, str, str]:
    identity = point.identity
    return (
        identity.source,
        coverage_point_line(point),
        identity.metric,
        identity.hierarchy,
        coverage_point_detail(point),
        point.id,
    )
