#!/usr/bin/env python3
"""Install a CI-owned Ticket fixture into a ticket-free demo checkout."""

from __future__ import annotations

import argparse
import re
import shutil
import sys
from pathlib import Path

from booley.ticket_board.board_layout import (
    documents_in_state,
    state_record_path,
    ticket_document_path,
)
from booley.ticket_board.io import TicketIO
from booley.ticket_board.lifecycle import TicketState

_SAFE_SLUG_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,79}$")


class DemoTicketInstallError(RuntimeError):
    """The demo checkout is not a safe destination for the Ticket fixture."""


def install_ticket_fixture(project_dir: Path, fixture: Path, slug: str) -> Path:
    """Install *fixture* only when the pinned project has no queued Tickets.

    The fixture is written as a draft at its board path and enqueued there,
    which adds its state record (ADR 0065).
    """
    if not project_dir.is_dir():
        raise DemoTicketInstallError(f"demo project directory is missing: {project_dir}")
    if not fixture.is_file():
        raise DemoTicketInstallError(f"Ticket fixture is missing: {fixture}")
    if not _SAFE_SLUG_RE.fullmatch(slug):
        raise DemoTicketInstallError(f"unsafe Ticket slug: {slug!r}")

    tickets_dir = project_dir / "tickets"
    destination = ticket_document_path(tickets_dir, slug)
    if destination.exists():
        raise DemoTicketInstallError(f"Ticket destination already exists: {destination}")
    queued = documents_in_state(tickets_dir, TicketState.QUEUED)
    if queued:
        names = ", ".join(path.name for path in queued)
        raise DemoTicketInstallError(f"demo checkout already contains queued Tickets: {names}")

    exclude = project_dir / ".git" / "info" / "exclude"
    if not exclude.parent.is_dir():
        raise DemoTicketInstallError(f"demo project Git metadata is missing: {exclude.parent}")
    destination.parent.mkdir(parents=True, exist_ok=True)
    with exclude.open("a", encoding="utf-8") as stream:
        for path in (destination, state_record_path(tickets_dir, slug)):
            stream.write(f"/{path.relative_to(project_dir).as_posix()}\n")
    shutil.copyfile(fixture, destination)
    destination.chmod(0o644)
    try:
        published = TicketIO(
            project_dir / "tickets", project_root=project_dir.parent
        ).enqueue_ticket(slug)
        if not published:
            raise DemoTicketInstallError("could not publish Ticket baseline")
        destination.chmod(0o644)
    except (OSError, RuntimeError, ValueError) as exc:
        raise DemoTicketInstallError(f"could not publish Ticket baseline: {exc}") from exc
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
