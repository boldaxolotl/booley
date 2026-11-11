"""`booley coverage`: read-only views of a finished coverage run's retained record."""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path
from typing import TYPE_CHECKING

from booley.core.exit_codes import EXIT_ERROR, EXIT_SUCCESS

if TYPE_CHECKING:
    from booley.flows.sim.coverage_campaign import CoverageCampaign, CoveragePoint
    from booley.flows.sim.coverage_point_query import UncoveredPoints

_RECORD_HINT = "Pass sim/<N>/targets/<target>/coverage.json of a retained coverage run."


def add_subparser(sub: argparse._SubParsersAction) -> None:
    parser = sub.add_parser(
        "coverage", help="List uncovered coverage points of a finished coverage run"
    )
    commands = parser.add_subparsers(dest="coverage_command", required=True, metavar="<command>")
    uncovered = commands.add_parser(
        "uncovered",
        help="List every uncovered scored point of one coverage record",
        description=(
            "List every point no Test Run hit, as `source:line  metric  detail  hierarchy`, "
            "sorted by source, line, and metric. Waived and unscored points are not listed."
        ),
    )
    uncovered.add_argument(
        "coverage_json",
        metavar="COVERAGE_JSON",
        type=Path,
        help="sim/<N>/targets/<target>/coverage.json of a retained coverage run",
    )
    uncovered.add_argument("--metric", help="List only this metric (exact match), e.g. branch")
    uncovered.add_argument(
        "--source", metavar="PATH", help="List only this Project-relative source (exact match)"
    )


def run(args: argparse.Namespace, project_root: Path) -> int:
    """List the uncovered points of one record; the Project is not consulted."""
    from booley.flows.sim.coverage_analysis_input import read_coverage_campaign
    from booley.flows.sim.coverage_point_query import uncovered_points

    # `..` is normalized lexically so a typed relative path works; the shared reader
    # still refuses every symlinked folder except the `.booley` entry.
    record = Path(os.path.normpath(Path.cwd() / args.coverage_json))
    try:
        campaign = read_coverage_campaign(record).campaign
    except (OSError, ValueError, RecursionError) as exc:
        reason = exc.__cause__ or exc
        print(
            f"ERROR: cannot read coverage record {args.coverage_json}: {reason}", file=sys.stderr
        )
        print(_RECORD_HINT, file=sys.stderr)
        return EXIT_ERROR
    problem = _collection_problem(campaign)
    if problem is not None:
        print(
            f"WARNING: coverage collection is {problem}; the list may be incomplete "
            "and is not a coverage result",
            file=sys.stderr,
        )
    selection = uncovered_points(campaign.points, metric=args.metric, source=args.source)
    for point in selection.points:
        print(_line(point))
    print(_summary(selection))
    return EXIT_SUCCESS


def _collection_problem(campaign: CoverageCampaign) -> str | None:
    """Name why the record may lack points, or None when collection is complete and compatible."""
    status = str(campaign.collection["status"])
    if status != "complete":
        return status.upper()
    if campaign.collector.native_format["compatibility"] != "compatible":
        return "INCOMPATIBLE"
    return None


def _line(point: CoveragePoint) -> str:
    """One listing line: `source:line  metric  detail  hierarchy`."""
    from booley.flows.sim.coverage_point_query import coverage_point_detail, coverage_point_line

    identity = point.identity
    where = f"{identity.source}:{coverage_point_line(point)}"
    return f"{where}  {identity.metric}  {coverage_point_detail(point)}  {identity.hierarchy}"


def _summary(selection: UncoveredPoints) -> str:
    """The closing count line; waived points are mentioned only when some were left out."""
    text = _counted(len(selection.points), "uncovered point")
    if selection.waived_omitted:
        text += f"; {_counted(selection.waived_omitted, 'waived point')} not listed"
    return text


def _counted(count: int, noun: str) -> str:
    return f"{count} {noun}" if count == 1 else f"{count} {noun}s"
