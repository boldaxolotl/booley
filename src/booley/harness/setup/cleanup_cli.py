"""CLI adapter for the Project Setup cleanup transaction."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from booley.harness.setup import cleanup


def add_subparser(sub: argparse._SubParsersAction) -> None:
    """Register ``booley cleanup`` and its preview/apply operations."""
    parser = sub.add_parser(
        "cleanup",
        help="Preview or apply manifest-owned Project Setup cleanup",
        description=(
            "Bounded Project Setup cleanup. Legacy plans without an ownership "
            "manifest are inventory-only."
        ),
    )
    parser.add_argument("--project-root", default="", help=argparse.SUPPRESS)
    commands = parser.add_subparsers(dest="cleanup_command", required=True)
    _add_prepare(commands)
    _add_record(commands)
    _add_preview(commands)
    _add_apply(commands)


def _add_prepare(commands: argparse._SubParsersAction) -> None:
    """Add the run allocation operation used before setup execution."""
    parser = commands.add_parser("prepare", help="allocate a setup-owned scratch root")
    parser.add_argument("--run-id", default=None)
    parser.add_argument("--json", action="store_true")


def _add_record(commands: argparse._SubParsersAction) -> None:
    """Add the manifest registration operation used by setup steps."""
    parser = commands.add_parser("record", help="record one setup-created artifact")
    parser.add_argument("path")
    parser.add_argument("--run-id", required=True)
    parser.add_argument("--producer", required=True)
    parser.add_argument("--class", dest="artifact_class", required=True)
    parser.add_argument("--disposition", choices=("preserve", "remove"), default="remove")
    parser.add_argument("--dependency", action="append", default=[])
    parser.add_argument("--active", action="store_true")


def _add_preview(commands: argparse._SubParsersAction) -> None:
    """Add the immutable planning operation."""
    parser = commands.add_parser("preview", help="build a cleanup plan without mutation")
    _add_plan_options(parser)


def _add_apply(commands: argparse._SubParsersAction) -> None:
    """Add the digest-guarded mutation operation."""
    parser = commands.add_parser("apply", help="apply one unchanged cleanup preview")
    _add_plan_options(parser)
    parser.add_argument("--digest", required=True)


def _add_plan_options(parser: argparse.ArgumentParser) -> None:
    """Add shared retention and cache policy options."""
    parser.add_argument("--run-id", default=None)
    parser.add_argument("--retention", choices=("minimal", "diagnostic"), default="minimal")
    parser.add_argument(
        "--cache-disposition",
        choices=("preserve", "evict-setup-touched"),
        default="preserve",
    )
    parser.add_argument("--json", action="store_true")


def _root(args: argparse.Namespace, project_root: Path) -> Path:
    """Prefer the explicit CLI root while keeping direct command use simple."""
    return Path(args.project_root).resolve() if args.project_root else project_root


def _print(value: object, as_json: bool) -> None:
    """Render one structured response."""
    if as_json:
        print(json.dumps(value, indent=2, sort_keys=True))
    else:
        print(value if isinstance(value, str) else json.dumps(value, indent=2, sort_keys=True))


def _run_prepare(args: argparse.Namespace, root: Path) -> int:
    """Allocate and report a run root."""
    path = cleanup.prepare_run(root, args.run_id)
    _print({"scratch_root": str(path), "manifest": str(path / "manifest.json")}, args.json)
    return 0


def _run_record(args: argparse.Namespace, root: Path) -> int:
    """Record one path before it becomes eligible for deletion."""
    entry = cleanup.record_artifact(
        root,
        args.run_id,
        Path(args.path),
        producer=args.producer,
        artifact_class=args.artifact_class,
        disposition=args.disposition,
        dependencies=args.dependency,
        active=args.active,
    )
    _print(entry.as_dict(), False)
    return 0


def _run_preview(args: argparse.Namespace, root: Path) -> int:
    """Build and print an immutable cleanup plan."""
    plan = cleanup.preview_cleanup(
        root,
        run_id=args.run_id,
        retention_mode=args.retention,
        cache_disposition=args.cache_disposition,
    )
    _print(plan.as_dict() if args.json else cleanup.format_summary(plan), args.json)
    return 0


def _run_apply(args: argparse.Namespace, root: Path) -> int:
    """Apply the caller-approved digest, with no raw delete surface."""
    plan = cleanup.preview_cleanup(
        root,
        run_id=args.run_id,
        retention_mode=args.retention,
        cache_disposition=args.cache_disposition,
    )
    if plan.digest != args.digest:
        raise cleanup.CleanupBlockedError("the supplied preview digest is stale")
    result = cleanup.apply_cleanup(plan)
    _print(result.as_dict() if args.json else cleanup.format_summary(plan, result), args.json)
    return 0 if not result.unresolved else 1


def run(args: argparse.Namespace, project_root: Path) -> int:
    """Dispatch the narrow cleanup command and convert failures to CLI errors."""
    try:
        root = _root(args, project_root)
        if args.cleanup_command == "prepare":
            return _run_prepare(args, root)
        if args.cleanup_command == "record":
            return _run_record(args, root)
        if args.cleanup_command == "preview":
            return _run_preview(args, root)
        if args.cleanup_command == "apply":
            return _run_apply(args, root)
        raise cleanup.CleanupError("a cleanup operation is required")
    except cleanup.CleanupError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 2

