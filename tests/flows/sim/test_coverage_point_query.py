"""The one rule for which Coverage Points count as uncovered, and the point filters."""

from __future__ import annotations

from collections.abc import Iterable

import pytest

from booley.flows.sim.coverage_campaign import (
    CoveragePoint,
    CoveragePointIdentity,
    encode_coverage_point_id,
)
from booley.flows.sim.coverage_json import freeze_coverage_mapping


def _point(
    source: str,
    line: int,
    metric: str = "line",
    *,
    subject: dict[str, object] | None = None,
    hits: dict[str, int] | None = None,
    kind: str = "eligible",
    hierarchy: str = "TOP.counter",
) -> CoveragePoint:
    """Build one in-memory point; ``hits`` empty means never hit."""
    identity = {
        "metric": metric,
        "location": {
            "source": source,
            "start": {"line": line, "column": 1},
            "end": {"line": line, "column": 9},
        },
        "hierarchy": hierarchy,
        "subject": {"basic_block": 0} if subject is None else subject,
        "collector": {"record_type": f"v_{metric}", "native_key": f"{line}:{metric}"},
    }
    return CoveragePoint(
        id=encode_coverage_point_id(identity),
        identity=CoveragePointIdentity(
            metric=metric,
            location=freeze_coverage_mapping(identity["location"]),
            hierarchy=hierarchy,
            subject=freeze_coverage_mapping(identity["subject"]),
            collector=freeze_coverage_mapping(identity["collector"]),
        ),
        hits_by_run=hits or {},
        disposition=freeze_coverage_mapping({"kind": kind}),
    )


def _ids(points: Iterable[CoveragePoint]) -> list[str]:
    return [point.id for point in points]


def test_uncovered_points_are_eligible_never_hit_sorted_by_source_line_metric() -> None:
    from booley.flows.sim.coverage_point_query import uncovered_points

    zeta = _point("rtl/zeta.sv", 3)
    late = _point("rtl/alpha.sv", 20)
    early_toggle = _point("rtl/alpha.sv", 4, "toggle", subject={"bit": "q[0]:0->1"})
    early_branch = _point("rtl/alpha.sv", 4, "branch", subject={"outcome": "else"})
    early_line = _point("rtl/alpha.sv", 4)
    hit = _point("rtl/alpha.sv", 1, hits={"run:a": 2})
    # Lines compare as numbers, not text: 9 sorts before 10.
    nine = _point("rtl/beta.sv", 9)
    ten = _point("rtl/beta.sv", 10)

    selection = uncovered_points(
        [zeta, ten, late, hit, early_toggle, nine, early_line, early_branch]
    )

    assert _ids(selection.points) == _ids(
        [early_branch, early_line, early_toggle, late, nine, ten, zeta]
    )
    assert selection.waived_omitted == 0


def test_equal_source_line_metric_ties_break_deterministically() -> None:
    from booley.flows.sim.coverage_point_query import uncovered_points

    inner = _point("rtl/a.sv", 5, hierarchy="TOP.a.inner", subject={"basic_block": 0})
    outer_two = _point("rtl/a.sv", 5, hierarchy="TOP.a", subject={"basic_block": 2})
    outer_one = _point("rtl/a.sv", 5, hierarchy="TOP.a", subject={"basic_block": 1})

    forward = uncovered_points([inner, outer_two, outer_one])
    backward = uncovered_points([outer_one, outer_two, inner])

    assert _ids(forward.points) == _ids([outer_one, outer_two, inner])
    assert _ids(backward.points) == _ids(forward.points)


def test_waived_and_unscored_points_are_left_out_and_waived_counted() -> None:
    from booley.flows.sim.coverage_point_query import uncovered_points

    listed = _point("rtl/a.sv", 1)
    waived = _point("rtl/a.sv", 2, kind="waived")
    waived_too = _point("rtl/a.sv", 3, "branch", kind="waived")
    # A waived point that was hit would never have been listed, so it is not counted.
    waived_hit = _point("rtl/a.sv", 4, kind="waived", hits={"run:a": 1})
    unscored = _point("tb/tb.sv", 7, kind="unscored")

    selection = uncovered_points([waived, listed, waived_too, waived_hit, unscored])

    assert _ids(selection.points) == _ids([listed])
    assert selection.waived_omitted == 2


@pytest.mark.parametrize(
    ("filters", "expected"),
    [
        ({}, ["a-branch", "a-line", "b-branch", "b-line"]),
        ({"metric": "branch"}, ["a-branch", "b-branch"]),
        ({"source": "rtl/b.sv"}, ["b-branch", "b-line"]),
        ({"metric": "branch", "source": "rtl/b.sv"}, ["b-branch"]),
        ({"source": "rtl/b.s"}, []),
        ({"metric": "Branch"}, []),
        ({"metric": "toggle", "source": "rtl/a.sv"}, []),
    ],
    ids=["none", "metric", "source", "both", "source-near-miss", "metric-case", "disjoint"],
)
def test_metric_and_source_filters_match_exactly_and_combine(
    filters: dict[str, str], expected: list[str]
) -> None:
    from booley.flows.sim.coverage_point_query import uncovered_points

    named = {
        "a-line": _point("rtl/a.sv", 1),
        "a-branch": _point("rtl/a.sv", 1, "branch", subject={"outcome": "if"}),
        "b-line": _point("rtl/b.sv", 1),
        "b-branch": _point("rtl/b.sv", 1, "branch", subject={"outcome": "if"}),
    }

    selection = uncovered_points(named.values(), **filters)

    assert _ids(selection.points) == _ids([named[name] for name in expected])


def test_filters_also_narrow_the_waived_count() -> None:
    from booley.flows.sim.coverage_point_query import uncovered_points

    points = [
        _point("rtl/a.sv", 1, kind="waived"),
        _point("rtl/a.sv", 2, "branch", kind="waived"),
        _point("rtl/b.sv", 3, kind="waived"),
    ]

    assert uncovered_points(points, metric="line").waived_omitted == 2
    assert uncovered_points(points, source="rtl/a.sv").waived_omitted == 2
    assert uncovered_points(points, metric="line", source="rtl/a.sv").waived_omitted == 1
    assert uncovered_points(points, metric="toggle").waived_omitted == 0


def test_no_uncovered_points_gives_an_empty_selection() -> None:
    from booley.flows.sim.coverage_point_query import uncovered_points

    selection = uncovered_points([_point("rtl/a.sv", 1, hits={"run:a": 3})])

    assert selection.points == ()
    assert selection.waived_omitted == 0
    assert uncovered_points([]).points == ()


def test_covered_means_any_run_hit_it() -> None:
    from booley.flows.sim.coverage_point_query import coverage_point_covered

    assert coverage_point_covered(_point("rtl/a.sv", 1, hits={"run:a": 1, "run:b": 4}))
    assert not coverage_point_covered(_point("rtl/a.sv", 1))


def test_matches_ignores_filters_left_as_none_and_checks_each_given_one() -> None:
    from booley.flows.sim.coverage_point_query import coverage_point_matches

    point = _point("rtl/a.sv", 1, kind="waived")

    assert coverage_point_matches(point)
    assert coverage_point_matches(point, covered=False, disposition="waived")
    assert not coverage_point_matches(point, covered=True)
    assert not coverage_point_matches(point, disposition="eligible")
    assert not coverage_point_matches(point, metric="branch")
    assert not coverage_point_matches(point, source="rtl/b.sv")


@pytest.mark.parametrize(
    ("subject", "detail"),
    [
        ({"outcome": "if"}, "if"),
        ({"bit": "wr_ptr[2]:1->0"}, "wr_ptr[2]:1->0"),
        ({"basic_block": 0}, "0"),
    ],
    ids=["text", "bit", "number"],
)
def test_point_detail_is_the_subject_value_as_text(
    subject: dict[str, object], detail: str
) -> None:
    from booley.flows.sim.coverage_point_query import coverage_point_detail

    assert coverage_point_detail(_point("rtl/a.sv", 1, subject=subject)) == detail
