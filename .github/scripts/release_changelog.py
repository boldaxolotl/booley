#!/usr/bin/env python3
"""Synchronize, validate, extract, and assemble Booley's canonical release notes."""

from __future__ import annotations

import argparse
import os
import re
import sys
from pathlib import Path

REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPOSITORY_ROOT / "src"))

from booley.runtime.changelog import (
    ChangelogError,
    parse_releases,
    parse_stable_version,
    release_entry,
)

DEFAULT_ROOT = REPOSITORY_ROOT / "CHANGELOG.md"
DEFAULT_PACKAGED = REPOSITORY_ROOT / "src" / "booley" / "data" / "refs" / "CHANGELOG.md"
UNRELEASED_HEADING = "## Unreleased"
# Any second-level heading; CRLF files keep their "\r" inside the match.
SECOND_LEVEL_HEADING = re.compile(r"^## .*$", re.MULTILINE)

DEFAULT_FRAGMENTS_NAME = "changelog.d"
FRAGMENT_README = "README.md"
# ``<issue number>.md`` or a lowercase slug that starts with a letter, so a slug
# can never be mistaken for an issue number.
_NUMBERED_FRAGMENT = re.compile(r"^([1-9][0-9]*)\.md$")
_SLUG_FRAGMENT = re.compile(r"^[a-z][a-z0-9-]*\.md$")
# A Markdown ATX heading, also directly after a bullet marker (``- ## x``).
_HEADING_LINE = re.compile(r"^\s*(?:[-*+]\s+)?#{1,6}(?:\s|$)")
_BULLET_LINE = re.compile(r"^[-*+] ")
# A column-0 setext underline or rule would turn the line above into a heading.
_SETEXT_UNDERLINE = re.compile(r"^(?:=+|-+)\s*$")


def synchronize(root: Path = DEFAULT_ROOT, packaged: Path = DEFAULT_PACKAGED) -> None:
    """Make the packaged mirror byte-identical to the canonical changelog."""
    packaged.parent.mkdir(parents=True, exist_ok=True)
    packaged.write_bytes(root.read_bytes())


def _require_unreleased_first(text: str) -> None:
    """Require ``## Unreleased`` as the first section, empty or not.

    Ordinary changes add fragments under ``changelog.d/`` that ``assemble``
    appends here, and release prep drafts the dated entry directly below it, so
    a release must never remove the heading.
    """
    first = SECOND_LEVEL_HEADING.search(text)
    observed = first.group(0).rstrip("\r") if first else None
    if observed != UNRELEASED_HEADING:
        raise ChangelogError(
            f"the first changelog section must be {UNRELEASED_HEADING!r} (keep it, even empty, "
            f"above the newest release); found {observed!r}"
        )


def _fragment_order(path: Path) -> tuple[int, int, str]:
    """Sort key: numeric names ascending, then slugs alphabetically, then anything else."""
    numbered = _NUMBERED_FRAGMENT.match(path.name)
    if numbered:
        return (0, int(numbered.group(1)), path.name)
    return (1 if _SLUG_FRAGMENT.match(path.name) else 2, 0, path.name)


def _fragment_paths(directory: Path) -> list[Path]:
    """List fragment candidates in assembly order, skipping the README and hidden files."""
    if not directory.is_dir():
        return []
    candidates = [
        path
        for path in directory.iterdir()
        if path.name != FRAGMENT_README and not path.name.startswith(".")
    ]
    return sorted(candidates, key=_fragment_order)


def _fragment_lines(path: Path) -> list[str]:
    """Return a fragment's bullet lines, or raise ``ChangelogError`` naming the first problem."""
    if not path.is_file():
        raise ChangelogError(f"{path}: not a regular file")
    if not (_NUMBERED_FRAGMENT.match(path.name) or _SLUG_FRAGMENT.match(path.name)):
        raise ChangelogError(
            f"{path}: name must be '<issue number>.md' or a lowercase slug like 'fix-typo.md'"
        )
    try:
        text = path.read_bytes().decode("utf-8")
    except UnicodeDecodeError as exc:
        raise ChangelogError(f"{path}: not valid UTF-8 ({exc.reason})") from exc
    lines = text.splitlines()
    while lines and not lines[0].strip():
        lines.pop(0)
    while lines and not lines[-1].strip():
        lines.pop()
    if not lines:
        raise ChangelogError(f"{path}: empty (no non-blank line)")
    for number, line in enumerate(lines, start=1):
        if _HEADING_LINE.match(line):
            raise ChangelogError(f"{path}: line {number} is a Markdown heading: {line.strip()!r}")
        if not line.strip() or line[0] in " \t":
            continue  # blank lines and indented continuations belong to the bullet above
        if _SETEXT_UNDERLINE.match(line):
            raise ChangelogError(f"{path}: line {number} is a heading underline or rule")
        if _BULLET_LINE.match(line):
            continue
        # Column-0 text continues the bullet only directly after a non-blank line.
        if number == 1 or not lines[number - 2].strip():
            raise ChangelogError(
                f"{path}: line {number} is top-level text that is not a bullet or its "
                f"continuation: {line.strip()!r}"
            )
    return lines


def read_fragments(
    directory: Path = REPOSITORY_ROOT / DEFAULT_FRAGMENTS_NAME,
) -> list[tuple[Path, list[str]]]:
    """Read every fragment in assembly order; one error lists every malformed file."""
    fragments: list[tuple[Path, list[str]]] = []
    problems: list[str] = []
    for path in _fragment_paths(directory):
        try:
            fragments.append((path, _fragment_lines(path)))
        except ChangelogError as exc:
            problems.append(str(exc))
    if problems:
        raise ChangelogError("malformed changelog fragments:\n  " + "\n  ".join(problems))
    return fragments


def _append_to_unreleased(text: str, lines: list[str]) -> str:
    """Append ``lines`` after the last non-blank line of the ``## Unreleased`` section."""
    heading = SECOND_LEVEL_HEADING.search(text)
    assert heading is not None  # _require_unreleased_first ran before
    # CRLF files keep the "\r" inside the heading match; step back before it.
    newline = "\r\n" if heading.group(0).endswith("\r") else "\n"
    heading_end = heading.end() - (len(newline) - 1)
    following = SECOND_LEVEL_HEADING.search(text, heading.end())
    end = following.start() if following else len(text)
    existing = text[heading.end() : end].splitlines()
    while existing and not existing[0].strip():
        existing.pop(0)
    while existing and not existing[-1].strip():
        existing.pop()
    section = newline.join([*existing, *lines])
    tail = newline + text[end:] if following else ""
    return f"{text[:heading_end]}{newline}{newline}{section}{newline}{tail}"


def _replace_file(path: Path, data: bytes) -> None:
    """Write ``data`` through a same-directory temporary file and ``os.replace``."""
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f"{path.name}.tmp")
    try:
        temporary.write_bytes(data)
        os.replace(temporary, path)  # noqa: PTH105 (atomic; tests patch os.replace)
    finally:
        temporary.unlink(missing_ok=True)


def assemble(
    root: Path = DEFAULT_ROOT,
    packaged: Path = DEFAULT_PACKAGED,
    fragments: Path | None = None,
) -> list[Path]:
    """Move every fragment's bullets to the end of ``## Unreleased`` and delete the fragments.

    The fragment directory defaults to ``changelog.d`` next to ``root``. Everything is
    read and checked before the first write, so a malformed fragment changes nothing.
    Returns the assembled fragment paths; with no fragments no file is touched.
    """
    directory = fragments if fragments is not None else root.parent / DEFAULT_FRAGMENTS_NAME
    pending = read_fragments(directory)
    if not pending:
        return []
    text = root.read_bytes().decode("utf-8")
    _require_unreleased_first(text)
    updated = _append_to_unreleased(text, [line for _, lines in pending for line in lines])
    _replace_file(root, updated.encode("utf-8"))
    try:
        _replace_file(packaged, updated.encode("utf-8"))
        for path, _ in pending:
            path.unlink()
    except OSError as exc:
        raise ChangelogError(
            f"{exc}; {root} was already updated: restore it and the mirror "
            "(git checkout -- <both files>) before running assemble again, "
            "or its bullets would be appended twice"
        ) from exc
    return [path for path, _ in pending]


def _validate_contract(
    root: Path,
    packaged: Path,
    *,
    target: str | None = None,
    notes_file: Path | None = None,
) -> None:
    """Validate mirror equality, Unreleased heading, ordering, VERSION bound, target, and notes.

    ``VERSION`` is read from the directory holding ``root``.
    """
    root_bytes = root.read_bytes()
    packaged_bytes = packaged.read_bytes()
    if root_bytes != packaged_bytes:
        raise ChangelogError(f"packaged changelog {packaged} differs from {root}")
    text = root_bytes.decode("utf-8")
    _require_unreleased_first(text)
    releases = parse_releases(text)
    version = parse_stable_version((root.parent / "VERSION").read_text(encoding="utf-8").strip())
    # parse_releases enforces newest-first order, so the first entry is the newest.
    if releases and releases[0].version > version:
        raise ChangelogError(
            f"changelog heading {releases[0].heading!r} is newer than VERSION {version}; "
            "only '## Unreleased' may describe changes above VERSION"
        )
    if target is None:
        if notes_file is not None:
            raise ChangelogError("--notes-file requires --target")
        return
    body = release_entry(text, target).body.encode("utf-8")
    if notes_file is not None and notes_file.read_bytes() != body:
        raise ChangelogError(f"release notes {notes_file} differ from changelog entry {target}")


def validate(
    root: Path = DEFAULT_ROOT,
    packaged: Path = DEFAULT_PACKAGED,
    *,
    target: str | None = None,
    notes_file: Path | None = None,
    fragments: Path | None = None,
) -> None:
    """Validate the changelog contract; with ``target``, also require no pending fragments."""
    _validate_contract(root, packaged, target=target, notes_file=notes_file)
    if target is None:
        return
    directory = fragments if fragments is not None else root.parent / DEFAULT_FRAGMENTS_NAME
    remaining = _fragment_paths(directory)
    if remaining:
        names = ", ".join(path.name for path in remaining)
        raise ChangelogError(
            f"changelog fragments remain: {names}; run 'release_changelog.py assemble' "
            "before tagging"
        )


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=DEFAULT_ROOT)
    parser.add_argument("--packaged", type=Path, default=DEFAULT_PACKAGED)
    parser.add_argument(
        "--fragments", type=Path, help="fragment directory (default: changelog.d next to --root)"
    )
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser(
        "assemble", help="append changelog.d fragments to Unreleased and delete them"
    )
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
                fragments=args.fragments,
            )
        elif args.command == "assemble":
            assembled = assemble(args.root, args.packaged, args.fragments)
            for path in assembled:
                print(f"assembled {path}")
            if not assembled:
                print("no changelog fragments")
        else:
            # A published release's notes stay readable while new fragments are pending.
            _validate_contract(args.root, args.packaged)
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
