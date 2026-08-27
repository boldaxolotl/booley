#!/usr/bin/env python3
"""Install a CI-owned Ticket fixture into a ticket-free demo checkout."""

from __future__ import annotations

import argparse
import re
import shutil
import sys
from pathlib import Path

_SAFE_SLUG_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,79}$")


class DemoTicketInstallError(RuntimeError):
    """The demo checkout is not a safe destination for the Ticket fixture."""


def install_ticket_fixture(project_dir: Path, fixture: Path, slug: str) -> Path:
    """Install *fixture* only when the pinned project has no queued Tickets."""
    if not project_dir.is_dir():
        raise DemoTicketInstallError(f"demo project directory is missing: {project_dir}")
    if not fixture.is_file():
        raise DemoTicketInstallError(f"Ticket fixture is missing: {fixture}")
    if not _SAFE_SLUG_RE.fullmatch(slug):
        raise DemoTicketInstallError(f"unsafe Ticket slug: {slug!r}")

    queue_dir = project_dir / "tickets" / "board" / "queue"
    destination = queue_dir / f"{slug}.md"
    if destination.exists():
        raise DemoTicketInstallError(f"Ticket destination already exists: {destination}")
    queued = sorted(queue_dir.glob("*.md")) if queue_dir.is_dir() else []
    if queued:
        names = ", ".join(path.name for path in queued)
        raise DemoTicketInstallError(f"demo checkout already contains queued Tickets: {names}")

    exclude = project_dir / ".git" / "info" / "exclude"
    if not exclude.parent.is_dir():
        raise DemoTicketInstallError(f"demo project Git metadata is missing: {exclude.parent}")
    queue_dir.mkdir(parents=True, exist_ok=True)
    with exclude.open("a", encoding="utf-8") as stream:
        stream.write(f"/tickets/board/queue/{slug}.md\n")
    shutil.copyfile(fixture, destination)
    destination.chmod(0o644)
    return destination


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--project-dir", required=True, type=Path)
    parser.add_argument("--fixture", required=True, type=Path)
    parser.add_argument("--slug", required=True)
    args = parser.parse_args(argv)
    try:
        install_ticket_fixture(args.project_dir, args.fixture, args.slug)
    except DemoTicketInstallError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
