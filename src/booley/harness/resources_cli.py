"""`booley resources`: read-only report of the Project's Job resource history."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from booley.runtime import resource_history
from booley.runtime.booley_dir import resolve_booley_dir


def add_subparser(sub: argparse._SubParsersAction) -> None:
    parser = sub.add_parser(
        "resources",
        help="Show measured peak memory and run time per Target and Job phase",
    )
    parser.add_argument("--json", action="store_true", help="Machine-readable statistics")


def run(args: argparse.Namespace, project_root: Path) -> int:
    """Print the history statistics of the selected Project."""
    try:
        records = resource_history.load_records(resolve_booley_dir(project_root))
    except (OSError, ValueError) as exc:
        print(f"ERROR: cannot read the Job resource history: {exc}", file=sys.stderr)
        return 2
    summaries = resource_history.summarize(records)
    if args.json:
        print(json.dumps(summaries, indent=2))
    else:
        print(resource_history.render_report(summaries))
    return 0
