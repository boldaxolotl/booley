#!/usr/bin/env python3
"""Synchronize, validate, and extract Booley's canonical release notes."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPOSITORY_ROOT / "src"))

from booley.runtime.changelog import ChangelogError, parse_releases, release_entry

DEFAULT_ROOT = REPOSITORY_ROOT / "CHANGELOG.md"
DEFAULT_PACKAGED = REPOSITORY_ROOT / "src" / "booley" / "data" / "refs" / "CHANGELOG.md"


def synchronize(root: Path = DEFAULT_ROOT, packaged: Path = DEFAULT_PACKAGED) -> None:
    """Make the packaged mirror byte-identical to the canonical changelog."""
    packaged.parent.mkdir(parents=True, exist_ok=True)
    packaged.write_bytes(root.read_bytes())


def validate(
    root: Path = DEFAULT_ROOT,
    packaged: Path = DEFAULT_PACKAGED,
    *,
    target: str | None = None,
    notes_file: Path | None = None,
) -> None:
    """Validate ordering, mirror equality, target presence, and optional notes."""
    root_bytes = root.read_bytes()
    packaged_bytes = packaged.read_bytes()
    if root_bytes != packaged_bytes:
        raise ChangelogError(f"packaged changelog {packaged} differs from {root}")
    text = root_bytes.decode("utf-8")
    parse_releases(text)
    if target is None:
        if notes_file is not None:
            raise ChangelogError("--notes-file requires --target")
        return
    body = release_entry(text, target).body.encode("utf-8")
    if notes_file is not None and notes_file.read_bytes() != body:
        raise ChangelogError(f"release notes {notes_file} differ from changelog entry {target}")


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=DEFAULT_ROOT)
    parser.add_argument("--packaged", type=Path, default=DEFAULT_PACKAGED)
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("sync", help="copy the canonical changelog to the package mirror")
    validate_parser = commands.add_parser("validate", help="validate the changelog contract")
    validate_parser.add_argument("--target")
    validate_parser.add_argument("--notes-file", type=Path)
    extract = commands.add_parser("extract", help="extract one release body verbatim")
    extract.add_argument("--target", required=True)
    extract.add_argument("--output", type=Path)
    return parser


def main() -> int:
    args = _parser().parse_args()
    try:
        if args.command == "sync":
            synchronize(args.root, args.packaged)
        elif args.command == "validate":
            validate(
                args.root,
                args.packaged,
                target=args.target,
                notes_file=args.notes_file,
            )
        else:
            validate(args.root, args.packaged, target=args.target)
            body = release_entry(args.root.read_bytes().decode("utf-8"), args.target).body
            if args.output is None:
                sys.stdout.write(body)
            else:
                args.output.write_bytes(body.encode("utf-8"))
    except (ChangelogError, OSError, UnicodeError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
