#!/usr/bin/env python3
"""Synchronize, validate, extract, and cut Booley's canonical release notes."""

from __future__ import annotations

import argparse
import os
import re
import sys
from datetime import UTC, date, datetime
from pathlib import Path
from typing import NamedTuple

REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPOSITORY_ROOT / "src"))

from booley.runtime.changelog import (
    ChangelogError,
    StableVersion,
    parse_releases,
    parse_stable_version,
    release_entry,
)
from booley.runtime.timefmt import format_human_date

DEFAULT_ROOT = REPOSITORY_ROOT / "CHANGELOG.md"
DEFAULT_PACKAGED = REPOSITORY_ROOT / "src" / "booley" / "data" / "refs" / "CHANGELOG.md"
UNRELEASED_HEADING = "## Unreleased"
# Any second-level heading; CRLF files keep their "\r" inside the match.
SECOND_LEVEL_HEADING = re.compile(r"^## .*$", re.MULTILINE)

DEFAULT_FRAGMENTS_NAME = "changelog.d"
FRAGMENT_README = "README.md"
# Fragment kinds in the order of their changelog sections.
KINDS = (
    ("major", "Major features"),
    ("feature", "Regular features"),
    ("qol", "Quality of life"),
    ("fix", "Bug fixes"),
    ("internal", "Internal work"),
    ("upgrade", "Upgrade notes"),
)
# Fragments in this subdirectory wait for the next ``X.Y.0`` release; a patch cut skips them.
HELD_DIRECTORY = "next-minor"
HELD_ONLY_KINDS = frozenset({"major", "upgrade"})
# Exit status of ``cut`` when no fragment is selected; every failure is status 2.
EXIT_NOTHING_TO_CUT = 3
_KIND_NAMES = ", ".join(kind for kind, _ in KINDS)
_SECTIONS = dict(KINDS)
# ``<issue number>.<kind>.md`` or ``<slug>.<kind>.md``; a slug starts with a letter, so it
# can never be mistaken for an issue number.
_ISSUE_NUMBER = re.compile(r"[1-9][0-9]*")
_SLUG = re.compile(r"[a-z][a-z0-9-]*")
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

    Ordinary changes add fragments under ``changelog.d/`` that ``cut`` writes as a
    dated entry directly below this heading, so a release must keep the heading.
    """
    first = SECOND_LEVEL_HEADING.search(text)
    observed = first.group(0).rstrip("\r") if first else None
    if observed != UNRELEASED_HEADING:
        raise ChangelogError(
            f"the first changelog section must be {UNRELEASED_HEADING!r} (keep it, even empty, "
            f"above the newest release); found {observed!r}"
        )


class Fragment(NamedTuple):
    """One well-formed fragment: where it is, what it says, and where it sorts."""

    path: Path
    kind: str
    held: bool
    order: tuple[int, int, str]
    lines: list[str]


def _fragment_order(name: str) -> tuple[int, int, str]:
    """Sort key: issue numbers ascending, then slugs alphabetically."""
    prefix = name.split(".", maxsplit=1)[0]
    if _ISSUE_NUMBER.fullmatch(prefix):
        return (0, int(prefix), name)
    return (1, 0, name)


def _fragment_candidates(directory: Path) -> list[tuple[Path, bool]]:
    """List ``(path, held)`` for every fragment candidate, skipping README and hidden names."""
    if not directory.is_dir():
        return []
    held_directory = directory / HELD_DIRECTORY
    found = [
        (path, False)
        for path in directory.iterdir()
        if path.name != FRAGMENT_README
        and not path.name.startswith(".")
        and path != held_directory
    ]
    if held_directory.is_dir():
        found.extend(
            (path, True) for path in held_directory.iterdir() if not path.name.startswith(".")
        )
    return found


def _fragment_kind(path: Path, held: bool) -> str:
    """Return the kind in ``<issue number or slug>.<kind>.md`` or raise naming the rule."""
    rule = (
        "name it '<issue number>.<kind>.md' or a lowercase slug like 'fix-typo.fix.md', "
        f"with a kind of {_KIND_NAMES}"
    )
    parts = path.name.split(".")
    if parts[-1] != "md" or not 2 <= len(parts) <= 3:
        raise ChangelogError(f"{path}: invalid name; {rule}")
    if not (_ISSUE_NUMBER.fullmatch(parts[0]) or _SLUG.fullmatch(parts[0])):
        raise ChangelogError(f"{path}: invalid name; {rule}")
    if len(parts) == 2:
        raise ChangelogError(f"{path}: missing kind; {rule}")
    kind = parts[1]
    if kind not in _SECTIONS:
        raise ChangelogError(f"{path}: unknown kind {kind!r}; use one of {_KIND_NAMES}")
    if kind in HELD_ONLY_KINDS and not held:
        raise ChangelogError(
            f"{path}: kind {kind!r} is valid only in {HELD_DIRECTORY}/ "
            "(held for the next minor release)"
        )
    return kind


def _fragment_lines(path: Path) -> list[str]:
    """Return a fragment's bullet lines, or raise ``ChangelogError`` naming the first problem."""
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
) -> list[Fragment]:
    """Read every fragment, held ones too; one error lists every malformed file."""
    fragments: list[Fragment] = []
    problems: list[str] = []
    for path, held in _fragment_candidates(directory):
        try:
            if not path.is_file():
                raise ChangelogError(f"{path}: not a regular file")
            kind = _fragment_kind(path, held)
            fragments.append(
                Fragment(path, kind, held, _fragment_order(path.name), _fragment_lines(path))
            )
        except ChangelogError as exc:
            problems.append(str(exc))
    if problems:
        raise ChangelogError("malformed changelog fragments:\n  " + "\n  ".join(sorted(problems)))
    return sorted(fragments, key=lambda fragment: (fragment.order, fragment.held))


def _replace_file(path: Path, data: bytes) -> None:
    """Write ``data`` through a same-directory temporary file and ``os.replace``."""
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f"{path.name}.tmp")
    try:
        temporary.write_bytes(data)
        os.replace(temporary, path)  # noqa: PTH105 (atomic; tests patch os.replace)
    finally:
        temporary.unlink(missing_ok=True)


def _today() -> date:
    return datetime.now(UTC).date()


def _check_cut(root: Path, directory: Path, target: StableVersion) -> tuple[str, list[Fragment]]:
    """Validate the target, ``## Unreleased`` and every fragment; return the text and selection."""
    version = parse_stable_version((root.parent / "VERSION").read_text(encoding="utf-8").strip())
    if target <= version:
        raise ChangelogError(f"target {target} is not greater than VERSION {version}")
    text = root.read_bytes().decode("utf-8")
    _require_unreleased_first(text)
    heading = SECOND_LEVEL_HEADING.search(text)
    assert heading is not None  # _require_unreleased_first ran before
    following = SECOND_LEVEL_HEADING.search(text, heading.end())
    if text[heading.end() : following.start() if following else len(text)].strip():
        raise ChangelogError(
            f"'{UNRELEASED_HEADING}' has content; cut writes the entry from fragments only: "
            "move that text into fragments"
        )
    fragments = read_fragments(directory)
    return text, [item for item in fragments if target.patch == 0 or not item.held]


def _render_entry(target: StableVersion, selected: list[Fragment], released: date, nl: str) -> str:
    """Render the dated entry: one section per kind that has fragments, in section order."""
    entry = [f"## {target} - {format_human_date(released)}"]
    for kind, section in KINDS:
        lines = [line for f in selected if f.kind == kind for line in f.lines]
        if lines:
            entry += ["", f"### {section}", "", *lines]
    return nl.join(entry) + nl


def _newline(text: str) -> str:
    """The changelog's line ending, read from its ``## Unreleased`` heading."""
    heading = SECOND_LEVEL_HEADING.search(text)
    assert heading is not None  # _require_unreleased_first ran before
    # CRLF files keep the "\r" inside the heading match.
    return "\r\n" if heading.group(0).endswith("\r") else "\n"


def _insert_entry(text: str, entry: str) -> str:
    """Put ``entry`` directly below the (blank) ``## Unreleased`` section."""
    heading = SECOND_LEVEL_HEADING.search(text)
    assert heading is not None  # _check_cut ran before
    newline = _newline(text)
    # CRLF files keep the "\r" inside the heading match; step back before it.
    heading_end = heading.end() - (len(newline) - 1)
    following = SECOND_LEVEL_HEADING.search(text, heading.end())
    tail = newline + text[following.start() :] if following else ""
    return f"{text[:heading_end]}{newline}{newline}{entry}{tail}"


def cut(
    root: Path = DEFAULT_ROOT,
    packaged: Path = DEFAULT_PACKAGED,
    fragments: Path | None = None,
    *,
    target: str,
    released: date | None = None,
) -> list[Path]:
    """Write the dated ``target`` entry from its fragments and delete them.

    A patch target uses the fragments outside ``next-minor/``; an ``X.Y.0`` target also
    uses the held ones. The fragment directory defaults to ``changelog.d`` next to ``root``.
    Everything is checked before the first write, so a failure changes nothing. Returns
    the used fragment paths; with none selected no file is touched.
    """
    directory = fragments if fragments is not None else root.parent / DEFAULT_FRAGMENTS_NAME
    version = parse_stable_version(target)
    text, selected = _check_cut(root, directory, version)
    if not selected:
        return []
    entry = _render_entry(version, selected, released or _today(), _newline(text))
    updated = _insert_entry(text, entry)
    parse_releases(updated)  # rejects a second entry for the same version
    _replace_file(root, updated.encode("utf-8"))
    try:
        _replace_file(packaged, updated.encode("utf-8"))
        for fragment in selected:
            fragment.path.unlink()
    except OSError as exc:
        raise ChangelogError(
            f"{exc}; {root} was already updated: restore it, the mirror and "
            "the fragments (git checkout -- <both files> changelog.d) before cutting again, "
            "or the entry would be written twice"
        ) from exc
    return [fragment.path for fragment in selected]


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
    """Validate the changelog contract; with ``target``, also require no fragment ``cut`` would use."""
    _validate_contract(root, packaged, target=target, notes_file=notes_file)
    if target is None:
        return
    directory = fragments if fragments is not None else root.parent / DEFAULT_FRAGMENTS_NAME
    # A patch release leaves the fragments held for the next minor in place.
    held_too = parse_stable_version(target).patch == 0
    remaining = sorted(
        (path for path, held in _fragment_candidates(directory) if held_too or not held),
        key=lambda path: _fragment_order(path.name),
    )
    if remaining:
        names = ", ".join(path.relative_to(directory).as_posix() for path in remaining)
        raise ChangelogError(
            f"changelog fragments remain: {names}; run "
            f"'release_changelog.py cut --target {target}' before bumping VERSION"
        )


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=DEFAULT_ROOT)
    parser.add_argument("--packaged", type=Path, default=DEFAULT_PACKAGED)
    parser.add_argument(
        "--fragments", type=Path, help="fragment directory (default: changelog.d next to --root)"
    )
    commands = parser.add_subparsers(dest="command", required=True)
    cut_parser = commands.add_parser(
        "cut",
        help="write the dated entry for --target from the fragments and delete them",
        description=(
            "Exit status 0 when an entry is written, 2 on any failure (including a usage "
            f"error) and {EXIT_NOTHING_TO_CUT} when no fragment is selected."
        ),
    )
    cut_parser.add_argument("--target", required=True)
    cut_parser.add_argument("--date", type=date.fromisoformat, help="release date, YYYY-MM-DD")
    commands.add_parser("sync", help="copy the canonical changelog to the package mirror")
    validate_parser = commands.add_parser("validate", help="validate the changelog contract")
    validate_parser.add_argument("--target")
    validate_parser.add_argument("--notes-file", type=Path)
    extract = commands.add_parser("extract", help="extract one release body verbatim")
    extract.add_argument("--target", required=True)
    extract.add_argument("--output", type=Path)
    return parser


def _run_cut(args: argparse.Namespace) -> int:
    used = cut(args.root, args.packaged, args.fragments, target=args.target, released=args.date)
    for path in used:
        print(f"cut {path}")
    if not used:
        print(f"no changelog fragments selected for {args.target}")
        return EXIT_NOTHING_TO_CUT
    return 0


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
        elif args.command == "cut":
            return _run_cut(args)
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
