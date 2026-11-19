"""Console adapter for the host-owned Project Inventory."""

from __future__ import annotations

import argparse
import json
import sys
from collections.abc import Callable
from dataclasses import asdict
from pathlib import Path
from typing import Protocol

from booley.core.host_shell import quote_arg
from booley.projects import image_keepers, inventory


class KeeperOperations(Protocol):
    """Host-coordinated keeper operations supplied by the command composition layer."""

    def forget_project(
        self, project: Path, *, purge: bool = False
    ) -> tuple[Path, image_keepers.KeeperRelease, image_keepers.PurgeResult | None]: ...

    def project_leftovers(self, project: Path) -> tuple[image_keepers.Leftover, ...]: ...

    def prune_keepers(self, confirm: str | None = None) -> image_keepers.PruneResult: ...

    def prune_stamps(self, roots: tuple[str, ...]) -> image_keepers.StampPrune: ...


#: Renders the Docker resources a forgotten Project leaves behind; the harness supplies it.
LeftoversView = Callable[[tuple[image_keepers.Leftover, ...]], str]


def add_subparser(subparsers: argparse._SubParsersAction) -> None:
    """Register the Project Inventory command."""
    parser = subparsers.add_parser(
        "projects", help="List remembered Project paths and their Project Grants"
    )
    parser.add_argument("--json", action="store_true", help="Emit machine-readable JSON")
    actions = parser.add_subparsers(
        dest="projects_action", metavar="{discover,forget,prune-keepers,prune-stamps}"
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
        "--purge",
        action="store_true",
        help="Also remove the Project's unused images and state volumes (agent login and history)",
    )
    forget.add_argument(
        "--json",
        action="store_true",
        default=argparse.SUPPRESS,
        help="Emit machine-readable JSON",
    )

    prune = actions.add_parser("prune-keepers", help="Preview orphan issued image keepers")
    prune.add_argument("--confirm", metavar="DIGEST", help="Release exactly the confirmed preview")
    prune.add_argument("--json", action="store_true", default=argparse.SUPPRESS)

    stamps = actions.add_parser(
        "prune-stamps",
        help=(
            "Remove the Sandbox issuance stamps of the named deleted Projects that are absent "
            "from the Project Inventory; keep their images and state volumes; with no ROOT, list them"
        ),
    )
    stamps.add_argument("roots", nargs="*", metavar="ROOT")
    stamps.add_argument("--json", action="store_true", default=argparse.SUPPRESS)


def run(
    args: argparse.Namespace,
    *,
    leftovers_view: LeftoversView,
    keeper_operations: KeeperOperations | None = None,
) -> int:
    """Execute one Project Inventory operation."""
    try:
        action = getattr(args, "projects_action", None)
        if action == "discover":
            discovered = inventory.discover_projects(tuple(args.search_roots))
            return _render_discovered(discovered, json_output=getattr(args, "json", False))
        if action in {"forget", "prune-keepers", "prune-stamps"} and keeper_operations is None:
            raise inventory.ProjectInventoryError(
                "Project keeper access requires host lifecycle coordination"
            )
        if action == "forget":
            assert keeper_operations is not None
            return _run_forget(args, keeper_operations, leftovers_view)
        if action == "prune-keepers":
            assert keeper_operations is not None
            result = keeper_operations.prune_keepers(args.confirm)
            return _render_prune(result, json_output=getattr(args, "json", False))
        if action == "prune-stamps":
            assert keeper_operations is not None
            pruned = keeper_operations.prune_stamps(tuple(args.roots))
            return _render_pruned_stamps(
                pruned, named=bool(args.roots), json_output=getattr(args, "json", False)
            )
        entries = inventory.project_inventory()
    except inventory.ProjectInventoryError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 2
    if getattr(args, "json", False):
        print(json.dumps(_json_document(entries), indent=2))
    else:
        _print_human(entries)
    return 0


def _run_forget(
    args: argparse.Namespace, keeper_operations: KeeperOperations, leftovers_view: LeftoversView
) -> int:
    forgotten, keeper, purged = keeper_operations.forget_project(args.project, purge=args.purge)
    try:
        left = keeper_operations.project_leftovers(forgotten)
    except inventory.ProjectInventoryError as exc:
        raise inventory.ProjectInventoryError(
            f"{exc} (keeper {keeper.status}: {keeper.tag})"
        ) from exc
    return _render_forgotten(
        forgotten,
        keeper,
        purged,
        left,
        json_output=getattr(args, "json", False),
        leftovers_view=leftovers_view,
    )


def _render_discovered(discovered: tuple[Path, ...], *, json_output: bool) -> int:
    if json_output:
        print(
            json.dumps(
                {"schema": 1, "discovered": [str(project) for project in discovered]}, indent=2
            )
        )
        return 0
    print(f"Remembered {len(discovered)} Project Root(s):")
    for project in discovered:
        print(f"  {project}")
    return 0


def _render_forgotten(
    forgotten: Path,
    keeper: image_keepers.KeeperRelease,
    purged: image_keepers.PurgeResult | None,
    left: tuple[image_keepers.Leftover, ...],
    *,
    json_output: bool,
    leftovers_view: LeftoversView,
) -> int:
    if json_output:
        document = {
            "schema": 1,
            "forgotten": str(forgotten),
            "keeper": asdict(keeper),
            "purge": asdict(purged) if purged is not None else None,
            "leftovers": [asdict(item) for item in left],
        }
        print(json.dumps(document, indent=2))
        return 0
    print(f"Forgot Project path: {forgotten}")
    image = f" ({keeper.image_id})" if keeper.image_id is not None else ""
    print(f"Keeper {keeper.status}: {keeper.tag}{image}")
    for name in purged.removed if purged else ():
        print(f"Removed: {name}")
    for line in purged.kept if purged else ():
        print(f"Kept: {line}")
    if not left:
        print("No Docker resources left behind for this Project.")
        return 0
    print("Left behind for this Project:")
    print(leftovers_view(left))
    if any(item.reason == "unrecorded" for item in left):
        print(
            "unrecorded: kept because Booley cannot tell which Project it belongs "
            "to; remove it with `docker image rm <id>` once no other Project uses it"
        )
    if purged is None and any(item.reason == "unused" for item in left):
        print(
            f"Remove the unused ones with: booley projects forget --purge {quote_arg(str(forgotten))}"
        )
    return 0


def _render_prune(result: image_keepers.PruneResult, *, json_output: bool) -> int:
    if json_output:
        print(json.dumps(asdict(result), indent=2))
    else:
        for candidate in result.candidates:
            print(f"{candidate.tag} {candidate.image_id} [{candidate.reason}]")
        if result.digest is None:
            print("Nothing to release.")
        else:
            print(f"Preview digest: {result.digest}")
            print("Confirm with: booley projects prune-keepers --confirm " + result.digest)
        for tag in result.released:
            print(f"Keeper tag released: {tag}")
        for tag in result.retained:
            print(f"Keeper tag retained or unresolved: {tag}")
        for error in result.errors:
            print(f"ERROR: {error}", file=sys.stderr)
    return 2 if result.errors else 0


def _render_pruned_stamps(
    pruned: image_keepers.StampPrune, *, named: bool, json_output: bool
) -> int:
    if json_output:
        document = {
            "schema": 1,
            "candidates": list(pruned.candidates),
            "withdrawn": list(pruned.withdrawn),
            "kept": list(pruned.kept),
        }
        print(json.dumps(document, indent=2))
    elif not named:
        for root in pruned.candidates:
            print(f"Removable: {root}")
        if pruned.candidates:
            command = " ".join(quote_arg(root) for root in pruned.candidates)
            print(f"Remove them with: booley projects prune-stamps {command}")
        else:
            print("No Sandbox issuance stamps of deleted, forgotten Projects.")
    else:
        for root in pruned.withdrawn:
            print(f"Removed Sandbox issuance stamp: {root}")
        for root in pruned.kept:
            print(f"Kept (not a deleted, forgotten Project): {root}")
        if pruned.withdrawn:
            print(
                "Their images and state volumes were kept; remove them with "
                "booley projects forget --purge <root>. If one of these folders comes back "
                "(an unmounted drive), run booley init there to reissue its Sandbox."
            )
    return 2 if pruned.kept else 0


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
