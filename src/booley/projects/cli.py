"""Console adapter for the host-owned Project Inventory."""

from __future__ import annotations

import argparse
import json
import sys
from dataclasses import asdict
from pathlib import Path
from typing import Protocol

from booley.projects import image_keepers, inventory


class KeeperOperations(Protocol):
    """Host-coordinated keeper operations supplied by the command composition layer."""

    def forget_project(self, project: Path) -> tuple[Path, image_keepers.KeeperRelease]: ...

    def prune_keepers(self, confirm: str | None = None) -> image_keepers.PruneResult: ...


def add_subparser(subparsers: argparse._SubParsersAction) -> None:
    """Register the Project Inventory command."""
    parser = subparsers.add_parser(
        "projects", help="List remembered Project paths and their Project Grants"
    )
    parser.add_argument("--json", action="store_true", help="Emit machine-readable JSON")
    actions = parser.add_subparsers(
        dest="projects_action", metavar="{discover,forget,prune-keepers}"
    )
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

    prune = actions.add_parser("prune-keepers", help="Preview orphan issued image keepers")
    prune.add_argument("--confirm", metavar="DIGEST", help="Release exactly the confirmed preview")
    prune.add_argument("--json", action="store_true", default=argparse.SUPPRESS)


def run(args: argparse.Namespace, *, keeper_operations: KeeperOperations | None = None) -> int:
    """Execute one Project Inventory operation."""
    try:
        action = getattr(args, "projects_action", None)
        if action == "discover":
            discovered = inventory.discover_projects(tuple(args.search_roots))
            return _render_discovered(discovered, json_output=getattr(args, "json", False))
        if action in {"forget", "prune-keepers"} and keeper_operations is None:
            raise inventory.ProjectInventoryError(
                "Project keeper access requires host lifecycle coordination"
            )
        if action == "forget":
            assert keeper_operations is not None
            forgotten, keeper = keeper_operations.forget_project(args.project)
            return _render_forgotten(forgotten, keeper, json_output=getattr(args, "json", False))
        if action == "prune-keepers":
            assert keeper_operations is not None
            result = keeper_operations.prune_keepers(args.confirm)
            return _render_prune(result, json_output=getattr(args, "json", False))
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


def _render_forgotten(
    forgotten: Path, keeper: image_keepers.KeeperRelease, *, json_output: bool
) -> int:
    if json_output:
        print(
            json.dumps(
                {"schema": 1, "forgotten": str(forgotten), "keeper": asdict(keeper)}, indent=2
            )
        )
    else:
        print(f"Forgot remembered Project path: {forgotten}")
        image = f" ({keeper.image_id})" if keeper.image_id is not None else ""
        print(f"Keeper {keeper.status}: {keeper.tag}{image}")
    return 0


def _render_prune(result: image_keepers.PruneResult, *, json_output: bool) -> int:
    if json_output:
        print(json.dumps(asdict(result), indent=2))
    else:
        for candidate in result.candidates:
            print(f"{candidate.tag} {candidate.image_id} [{candidate.reason}]")
        print(f"Preview digest: {result.digest}")
        print("Confirm with: booley projects prune-keepers --confirm " + result.digest)
        for tag in result.released:
            print(f"Keeper tag released: {tag}")
        for tag in result.retained:
            print(f"Keeper tag retained or unresolved: {tag}")
        for error in result.errors:
            print(f"ERROR: {error}", file=sys.stderr)
    return 2 if result.errors else 0


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
        print("  No remembered Project paths or Project Grants.")
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
