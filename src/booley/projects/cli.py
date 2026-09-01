"""Console adapter for the host-owned Project Inventory."""

from __future__ import annotations

import argparse
import json
import sys
from dataclasses import asdict
from pathlib import Path

from booley.projects import inventory


def add_subparser(subparsers: argparse._SubParsersAction) -> None:
    """Register the Project Inventory command."""
    parser = subparsers.add_parser(
        "projects", help="List Remembered Project Roots and their Project Grants"
    )
    parser.add_argument("--json", action="store_true", help="Emit machine-readable JSON")
    actions = parser.add_subparsers(dest="projects_action", metavar="{discover,forget}")
    discover = actions.add_parser(
        "discover", help="Remember initialized Projects beneath explicit search roots"
    )
    discover.add_argument("search_roots", nargs="+", type=Path, metavar="ROOT")
    discover.add_argument(
        "--json",
        action="store_true",
        default=argparse.SUPPRESS,
        help="Emit machine-readable JSON",
    )
    forget = actions.add_parser("forget", help="Forget one root with no live Project Grants")
    forget.add_argument("project", type=Path)
    forget.add_argument(
        "--json",
        action="store_true",
        default=argparse.SUPPRESS,
        help="Emit machine-readable JSON",
    )


def run(args: argparse.Namespace) -> int:
    """Execute one Project Inventory operation."""
    try:
        action = getattr(args, "projects_action", None)
        if action == "discover":
            discovered = inventory.discover_projects(tuple(args.search_roots))
            return _render_discovered(discovered, json_output=getattr(args, "json", False))
        if action == "forget":
            forgotten = inventory.forget_project(args.project)
            return _render_forgotten(forgotten, json_output=getattr(args, "json", False))
        entries = inventory.project_inventory()
    except inventory.ProjectInventoryError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 2
    if getattr(args, "json", False):
        print(json.dumps(_json_document(entries), indent=2))
    else:
        _print_human(entries)
    return 0


def _render_discovered(discovered: tuple[Path, ...], *, json_output: bool) -> int:
    if json_output:
        print(
            json.dumps(
                {"schema": 1, "discovered": [str(project) for project in discovered]}, indent=2
            )
        )
        return 0
    print(f"Remembered {len(discovered)} Project root(s):")
    for project in discovered:
        print(f"  {project}")
    return 0


def _render_forgotten(forgotten: Path, *, json_output: bool) -> int:
    if json_output:
        print(json.dumps({"schema": 1, "forgotten": str(forgotten)}, indent=2))
    else:
        print(f"Forgot Remembered Project Root: {forgotten}")
    return 0


def _json_document(
    entries: tuple[inventory.ProjectInventoryEntry, ...],
) -> dict[str, object]:
    projects = []
    for entry in entries:
        projects.append(
            {
                "project_root": entry.project_root,
                "status": entry.status.value,
                "remembered": entry.remembered,
                "grants": [asdict(grant) for grant in entry.grants],
            }
        )
    return {"schema": 1, "projects": projects}


def _print_human(entries: tuple[inventory.ProjectInventoryEntry, ...]) -> None:
    print("Project Inventory")
    if not entries:
        print("  No Remembered Project Roots or Project Grants.")
        return
    for entry in entries:
        source = "" if entry.remembered else "; grant only"
        print(f"  {entry.project_root} [{entry.status.value}{source}]")
        if not entry.grants:
            print("    Grants: none")
        for grant in entry.grants:
            print(f"    {grant.kind}")
            print(f"      Installation: {grant.installation or 'none'}")
            print(f"      License Profile: {grant.license_profile or 'none'}")
