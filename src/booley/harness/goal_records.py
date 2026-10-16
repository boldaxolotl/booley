"""Human rendering and CLI dispatch for observational retained-record services."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

from booley.goals.inventory import list_records, show_record
from booley.goals.prune import prune_record
from booley.goals.store import GoalStore, GoalStoreError
from booley.runtime.booley_dir import resolve_booley_dir
from booley.runtime.timefmt import format_human_datetime_safe


def render_inventory(value: dict[str, Any]) -> str:
    """Render nullable metadata explicitly; machine envelopes retain RFC3339 values."""
    rows = []
    for row in value["records"]:
        rows.append(
            f"{row['record_id']}  {row['state']}\n"
            f"  entered {format_human_datetime_safe(row['entered_at'])}; ended {format_human_datetime_safe(row['ended_at']) if row['ended_at'] else 'unavailable'}\n"
            f"  branch {row['branch']}: {row['branch_presence']}; worktree {row['worktree_path']}: {row['worktree_presence']}\n"
            f"  logical bytes: {row['size_bytes'] if row['size_bytes'] is not None else 'unavailable/incomplete'}"
        )
    return "\n".join([*(rows or ["No retained Goal Records."]), *value["diagnostics"]])


def render_record(value: dict[str, Any]) -> str:
    """Show retained outcomes, their operation and record-owned artifact locations."""
    rows = [render_inventory({"records": [value["record"]], "diagnostics": value["diagnostics"]})]
    for goal in value["goals"]:
        rows.append(f"{goal['name']}: {goal['status']}")
    operation = value["operation"]
    rows.append(
        "Operation: unavailable"
        if operation is None
        else f"Operation: {operation['kind']} {operation['operation_id']} at {operation['path']}"
    )
    rows.extend(
        f"{name}: {row['path']} ({'present' if row['exists'] else 'missing'})"
        for name, row in value["artifacts"].items()
    )
    return "\n".join(rows)


def run(args: argparse.Namespace, project_root: Path) -> int:
    """Dispatch exact-ID reads/cleanup without entering the live status workflow."""
    try:
        store = GoalStore(resolve_booley_dir(project_root))
        if args.goal_command == "list":
            print(render_inventory(list_records(store, project_root)))
        elif args.goal_command == "show":
            print(render_record(show_record(store, project_root, args.record_id)))
        else:
            if args.confirm:
                preview = prune_record(store, project_root, args.record_id)
                print(json.dumps(preview, indent=2), flush=True)
            result = prune_record(store, project_root, args.record_id, confirm=args.confirm)
            print(json.dumps(result, indent=2))
            if not args.confirm:
                print(
                    f"Preview only. Remove this record with booley goal prune {args.record_id} --confirm"
                )
        return 0
    except (GoalStoreError, OSError, ValueError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 2
