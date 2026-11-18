"""Synchronize the packaged CHANGELOG mirror from the repository.

The packaged user docs are not synchronized here: they are Git-ignored build output
that every wheel and image builder renders from ``SHIPPED_DOCS`` through
``booley.runtime.build_stamp`` (allowlist, link-rewriting rules and renderer in
``booley.runtime.packaged_docs``). Only ``CHANGELOG.md`` is still a committed copy,
mirrored byte for byte (no link rewriting) to ``src/booley/data/refs/CHANGELOG.md``.

    python -m booley.dev_support.reference_docs          # regenerate the mirror
    python -m booley.dev_support.reference_docs --check  # fail on a stale mirror
    python3 .github/scripts/sync_packaged_copies.py [--check]  # same, without installing Booley

An ordinary change does not edit ``CHANGELOG.md`` or its mirror: it adds a fragment
under ``changelog.d/`` that the release assembles
(``.github/scripts/release_changelog.py assemble``). The mirror is generated; never
edit it.
"""

from __future__ import annotations

import argparse
from pathlib import Path

from booley.runtime.packaged_docs import atomic_write

_REPOSITORY_ROOT = Path(__file__).resolve().parents[3]
CHANGELOG_SOURCE = "CHANGELOG.md"
PACKAGED_CHANGELOG_RELATIVE_PATH = "src/booley/data/refs/CHANGELOG.md"

_SYNC_COMMAND = "python -m booley.dev_support.reference_docs"


def packaged_changelog_problems(root: Path = _REPOSITORY_ROOT) -> list[str]:
    """Return one line when the packaged CHANGELOG mirror is missing or differs from the source."""
    mirror = root / PACKAGED_CHANGELOG_RELATIVE_PATH
    if not mirror.is_file():
        return [f"{PACKAGED_CHANGELOG_RELATIVE_PATH}: packaged copy is missing"]
    if mirror.read_bytes() != (root / CHANGELOG_SOURCE).read_bytes():
        return [f"{PACKAGED_CHANGELOG_RELATIVE_PATH}: packaged copy is stale"]
    return []


def sync_packaged_changelog(root: Path = _REPOSITORY_ROOT) -> list[str]:
    """Copy ``CHANGELOG.md`` byte for byte to its packaged mirror; return what changed."""
    if not packaged_changelog_problems(root):
        return []
    mirror = root / PACKAGED_CHANGELOG_RELATIVE_PATH
    mirror.parent.mkdir(parents=True, exist_ok=True)
    atomic_write(mirror, (root / CHANGELOG_SOURCE).read_bytes())
    return [PACKAGED_CHANGELOG_RELATIVE_PATH]


def main(
    argv: list[str] | None = None,
    root: Path = _REPOSITORY_ROOT,
    sync_command: str = _SYNC_COMMAND,
) -> int:
    """Synchronize the packaged CHANGELOG mirror, or check it without writing.

    ``sync_command`` is the fix command ``--check`` prints; each entry point names itself.
    """
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--check",
        action="store_true",
        help="fail when the packaged CHANGELOG mirror is stale",
    )
    args = parser.parse_args(argv)
    if args.check:
        problems = packaged_changelog_problems(root)
        for problem in problems:
            print(problem)
        if problems:
            print(f"packaged CHANGELOG mirror is out of date; run {sync_command}")
        return 1 if problems else 0
    changed = sync_packaged_changelog(root)
    print(
        f"updated packaged CHANGELOG mirror: {', '.join(changed)}"
        if changed
        else "packaged CHANGELOG mirror is current"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
