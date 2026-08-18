"""Shared fixtures for ticket_board unit tests."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

# Ensure package is importable (fallback when not installed via pip install -e .)
sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent / "src"))

from booley.ticket_board.io import TicketIO


def _make_tio(tmp_path: Path) -> TicketIO:
    """Create a tickets dir with all subdirectories and return a TicketIO."""
    tickets_dir = tmp_path / "tickets"
    for d in [
        "board/drafts",
        "board/queue",
        "board/waiting",
        "board/active",
        "board/blocked",
        "board/review",
        "board/done",
        "board/archived",
    ]:
        (tickets_dir / d).mkdir(parents=True, exist_ok=True)
    (tickets_dir / "logs").mkdir(parents=True, exist_ok=True)
    # Pin project_root: the bare tmp/tickets layout matches neither supported
    # convention, so TicketIO's inference would walk up to the SHARED pytest
    # tmp base — where stale .core files from other tests' retained runs leak
    # into .core-derived validation (tb_source_prefixes rglob).
    return TicketIO(tickets_dir, project_root=tmp_path)


@pytest.fixture
def tio(tmp_path: Path) -> TicketIO:
    """TicketIO instance backed by a temporary directory."""
    return _make_tio(tmp_path)


def make_ticket_file(
    tio: TicketIO,
    subdir: str,
    slug: str,
    extra_fields: dict | None = None,
    body: str = "## Description\nSome work.\n",
) -> Path:
    """Create a ticket .md file with frontmatter in the specified directory."""
    fields = {
        "summary": slug.replace("-", " "),
        "type": "feature",
        "branch": "master",
        "scope": ["rtl/foo.sv"],
        "criteria": {
            "mandatory": {
                "sim_pass": ["tb/foo_tb.sv @ default @ all @ pass -> pass"],
            },
        },
    }
    if extra_fields:
        fields.update(extra_fields)

    from booley.ticket_board.frontmatter import format_frontmatter

    content = format_frontmatter(fields, body)

    if not subdir.startswith("board/"):
        subdir = f"board/{subdir}"
    d = tio.tickets_dir / subdir
    d.mkdir(parents=True, exist_ok=True)
    p = d / f"{slug}.md"
    p.write_text(content, encoding="utf-8")
    return p


@pytest.fixture(autouse=True)
def _no_ntfy(monkeypatch):
    """Silence all ntfy.sh notifications during tests."""
    monkeypatch.setattr("booley.ticket_board.operations.ntfy_send", lambda *a, **kw: None)


@pytest.fixture(autouse=True)
def _set_project_dir(tmp_path, monkeypatch):
    """Prevent resolve_project_dir() from failing in ticket_board tests."""
    from booley.runtime.project_dir import reset_cache

    reset_cache()
    monkeypatch.setenv("BOOLEY_PROJECT_DIR", str(tmp_path))
