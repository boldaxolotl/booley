"""Checked-in Project fixtures keep every ignore pattern `booley init` writes."""

from __future__ import annotations

from pathlib import Path

import pytest

from booley.runtime.project_gitignore import missing_gitignore_patterns

FIXTURES = Path(__file__).parents[1] / "fixtures"


@pytest.mark.parametrize(
    "gitignore",
    sorted(FIXTURES.glob("*/.booley_project/.gitignore")),
    ids=lambda path: path.parents[1].name,
)
def test_fixture_project_gitignore_is_current(gitignore: Path) -> None:
    # A stale fixture lets board documents and state records be committed,
    # which the Ticket Board legacy-layout guard then refuses (ADR 0065).
    assert missing_gitignore_patterns(gitignore.read_text(encoding="utf-8")) == []
