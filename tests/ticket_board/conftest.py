"""Shared fixtures for ticket_board unit tests."""

from __future__ import annotations

import os
import subprocess
import sys
from collections.abc import Callable, Collection
from pathlib import Path

import pytest

# Ensure package is importable (fallback when not installed via pip install -e .)
sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent / "src"))

from booley.ticket_board.io import TicketIO


def make_paired_repository(root: Path) -> Path:
    """Create committed outer and nested Project repositories for tests."""

    def git(repository: Path, *args: str) -> str:
        result = subprocess.run(
            ["git", *args],
            cwd=repository,
            capture_output=True,
            text=True,
            check=True,
            timeout=30,
        )
        return result.stdout.strip()

    root.mkdir()
    git(root, "init", "-q", "-b", "main")
    git(root, "config", "user.name", "Test")
    git(root, "config", "user.email", "test@example.invalid")
    (root / "README.md").write_text("outer\n", encoding="utf-8")
    git(root, "add", "README.md")
    git(root, "commit", "-qm", "initial outer")
    (root / ".git/info/exclude").write_text("/.booley_project\n", encoding="utf-8")

    project = root / ".booley_project"
    project.mkdir()
    git(project, "init", "-q", "-b", "main")
    git(project, "config", "user.name", "Test")
    git(project, "config", "user.email", "test@example.invalid")
    (project / "booley.toml").write_text("[flows]\n", encoding="utf-8")
    git(project, "add", ".")
    git(project, "commit", "-qm", "initial project")
    return project


def _make_tio(tmp_path: Path) -> TicketIO:
    """Create a tickets dir with its board, state, and logs dirs; return a TicketIO."""
    tickets_dir = tmp_path / "tickets"
    for d in ["board", "state", "logs"]:
        (tickets_dir / d).mkdir(parents=True, exist_ok=True)
    # Pin project_root: the bare tmp/tickets layout matches neither supported
    # convention, so TicketIO's inference would walk up to the SHARED pytest
    # tmp base — where stale .core files from other tests' retained runs leak
    # into .core-derived validation (tb_source_prefixes rglob).
    return TicketIO(tickets_dir, project_root=tmp_path)


@pytest.fixture
def fake_bind_mounts(
    monkeypatch: pytest.MonkeyPatch,
) -> Callable[[Path, Collection[Path]], None]:
    """Make selected paths report the same filesystem identity as a root."""
    real_samefile = Path.samefile

    def install(root: Path, aliases: Collection[Path]) -> None:
        identities = {frozenset((root, alias)) for alias in aliases}

        def samefile(left: Path, right: Path) -> bool:
            if frozenset((Path(left), Path(right))) in identities:
                return True
            return real_samefile(left, right)

        monkeypatch.setattr(Path, "samefile", samefile)

    return install


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
    """Create a ticket .md file with frontmatter in the state named by *subdir*.

    *subdir* is a board name such as ``queue`` or ``board/active``; the document
    lands at ``board/<slug>.md`` and, unless it is a draft, gets a state record.
    """
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

    return place_ticket(tio.tickets_dir, slug, subdir, content)


def place_ticket(tickets_dir: Path, slug: str, board_name: str, content: str) -> Path:
    """Write *content* as the board document of *slug* in the state *board_name*."""
    from booley.ticket_board.board_layout import (
        StateRecord,
        delete_state_record,
        ticket_document_path,
        write_state_record,
    )
    from booley.ticket_board.lifecycle import TicketState, parse_board_target

    state = parse_board_target(board_name)
    assert state is not None, board_name
    path = ticket_document_path(tickets_dir, slug)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")
    if state is TicketState.DRAFT:
        delete_state_record(tickets_dir, slug)
    else:
        write_state_record(tickets_dir, slug, StateRecord.fresh(state))
    return path


def place_closed_ticket(
    tickets_dir: Path,
    slug: str,
    content: str,
    outcome: str = "done",
    generation: str = "",
) -> Path:
    """Write *content* as the Ticket History document of a Closed Ticket.

    Closed Tickets live only in ``history/<slug>.md`` (ADR 0065): no board
    document and no state record. Only outcome ``done`` satisfies dependencies.
    """
    from booley.ticket_board.board_layout import history_document_path
    from booley.ticket_board.lifecycle import STATE_BY_STATUS
    from booley.ticket_board.ticket_history import ClosedBlock, with_closed_block

    path = history_document_path(tickets_dir, slug)
    path.parent.mkdir(parents=True, exist_ok=True)
    block = ClosedBlock.now(STATE_BY_STATUS[outcome], generation)
    path.write_text(with_closed_block(content, block), encoding="utf-8")
    return path


def make_closed_ticket(
    tio: TicketIO,
    slug: str,
    outcome: str = "done",
    extra_fields: dict | None = None,
    body: str = "## Description\nSome work.\n",
) -> Path:
    """Create a Closed Ticket in Ticket History with *outcome* (done or archived)."""
    from booley.ticket_board.frontmatter import format_frontmatter

    fields = {"summary": slug.replace("-", " "), "type": "feature", "branch": "master"}
    fields.update(extra_fields or {})
    return place_closed_ticket(tio.tickets_dir, slug, format_frontmatter(fields, body), outcome)


@pytest.fixture(autouse=True)
def _isolate_host_git_config(monkeypatch: pytest.MonkeyPatch) -> None:
    """Hide the developer's global Git config, which CI runners do not have.

    A host-global ``core.excludesFile`` ignoring ``.booley_project/`` silently
    turns Ticket History commits into no-ops, masking failures CI then reports.
    Tests that need an identity configure it in their repositories. System
    config stays visible: Git for Windows keeps its platform defaults there.
    """
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", os.devnull)


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


def publish_handoff_snapshot(tio: TicketIO, slug: str, *_args) -> bool:
    """Satisfy handoff's durable precondition in tests of unrelated policy."""
    from booley.criteria.state import DevelopmentState
    from booley.ticket_board.acceptance_ledger import freeze_acceptance

    freeze_acceptance(
        tio.logs_dir / slug,
        DevelopmentState(slug=slug),
        execution_id="fixture",
        ticket_identity=None,
        participant_heads={"outer": "a" * 40},
    )
    return True
