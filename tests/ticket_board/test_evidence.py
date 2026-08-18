"""Tests for the current, authoritative ticket evidence bundle."""

from pathlib import Path
from types import SimpleNamespace

from booley.ticket_board.evidence import op_collect_evidence


def _ticket_io(tmp_path: Path, entry: dict | None) -> SimpleNamespace:
    return SimpleNamespace(
        logs_dir=tmp_path / "logs",
        tickets_dir=None,
        find_ticket=lambda _slug: entry,
    )


def test_collect_evidence_reports_only_authoritative_ticket_data(tmp_path: Path) -> None:
    tio = _ticket_io(
        tmp_path,
        {
            "type": "bugfix",
            "scope": ["rtl/core.sv"],
            "spec": "docs/core.md",
            "test": {"target": "smoke"},
        },
    )
    snapshot = tio.logs_dir / "fix-core" / "ticket.md"
    snapshot.parent.mkdir(parents=True)
    snapshot.write_text(
        "## Acceptance Criteria\n- smoke passes\n- lint is clean\n## Notes\ntext\n",
        encoding="utf-8",
    )

    evidence = op_collect_evidence(tio, "fix-core")

    assert evidence == {
        "ticket": {
            "type": "bugfix",
            "scope": ["rtl/core.sv"],
            "spec": "docs/core.md",
            "test": {"target": "smoke"},
            "acceptance_criteria": ["smoke passes", "lint is clean"],
        }
    }


def test_collect_evidence_returns_none_for_unknown_ticket(tmp_path: Path) -> None:
    assert op_collect_evidence(_ticket_io(tmp_path, None), "missing") is None
