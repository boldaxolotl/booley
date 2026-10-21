"""CLI adapter for marker-guarded Project Setup run-root cleanup."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from booley.flows.cli_arguments import add_project_root_alias
from booley.harness.setup import run_root_cleanup

_OPERATIONS = {
    "prepare": ("create a marked run root", run_root_cleanup.prepare),
    "preview": ("list what apply would remove", run_root_cleanup.preview),
    "apply": ("snapshot Findings attachments, then remove the run root", run_root_cleanup.apply),
}


def add_subparser(sub: argparse._SubParsersAction) -> None:
    """Register ``booley cleanup``; the root command makes its alias a Project selector."""
    parser = sub.add_parser(
        "cleanup", help="Prepare, preview or remove one Project Setup run root"
    )
    add_project_root_alias(parser, "_cli_project_cleanup")
    commands = parser.add_subparsers(dest="cleanup_command", required=True)
    for name, (help_text, _) in _OPERATIONS.items():
        command = commands.add_parser(name, help=help_text)
        command.add_argument("--run-id", required=name != "prepare")
        command.add_argument("--json", action="store_true")


def _text(report: dict) -> str:
    """Human rendering: a header, one line per file, one line per kept reason."""
    rows = [f"{report['status']}: {report['run_root']} ({report['bytes']} bytes)"]
    for item in report["files"]:
        rows.append(f"  {item['path']} ({item['bytes']} bytes){' [attached]' * item['attached']}")
    return "\n".join(rows + [f"kept: {k['reason']}: {k['path']}" for k in report["kept"]])


def run(args: argparse.Namespace, project_root: Path) -> int:
    """Run one operation: exit 0 done, 1 run root kept, 2 refused (nothing deleted)."""
    root = Path(args.project_root).resolve() if args.project_root else project_root
    try:
        report = _OPERATIONS[args.cleanup_command][1](root, args.run_id)
    except (run_root_cleanup.CleanupRefusedError, OSError, ValueError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 2
    print(json.dumps(report, indent=2, sort_keys=True) if args.json else _text(report))
    return 1 if report["kept"] else 0
