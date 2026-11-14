"""Keep the configuration reference aligned with recognized project tables."""

import re
from pathlib import Path

import pytest
from tests.config_docs_support import CONFIG_PAGES

from booley.audit.project_schema import KNOWN_BOOLEY_TOML_TABLES
from booley.config import agent as bc

USER_DOCS = Path(__file__).resolve().parents[2] / "docs/user"
CONFIG_REFERENCE = USER_DOCS / "CONFIG.md"


def _config_pages_text() -> str:
    return "\n".join((USER_DOCS / name).read_text(encoding="utf-8") for name in CONFIG_PAGES)


# Project [interactive] recognizes migration inputs only: policy and app are
# rejected, and no field supplies a current setting. Recognition stays intact;
# tests/harness/test_doctor.py proves each field's user-visible replacement.
MIGRATION_ONLY_TABLES = {"interactive"}


@pytest.mark.parametrize("table", sorted(KNOWN_BOOLEY_TOML_TABLES - MIGRATION_ONLY_TABLES))
def test_config_reference_documents_known_booley_toml_table(table: str) -> None:
    reference = _config_pages_text()

    table_reference = rf"\[{re.escape(table)}(?:\.[^\]\n]+)?\]"
    assert re.search(table_reference, reference), (
        f"booley.toml [{table}] missing from the configuration pages"
    )


def test_config_reference_lists_specialist_model_keys_exactly() -> None:
    """CONFIG.md names exactly the Specialists a `[models]` key may pin."""
    reference = " ".join(CONFIG_REFERENCE.read_text(encoding="utf-8").split())
    sentence = re.search(r"The `\[models\]` keys are (.+?)\.", reference)
    assert sentence, "CONFIG.md must say 'The `[models]` keys are ...'"
    documented = re.findall(r"`([a-z_]+)`", sentence.group(1))
    assert sorted(documented) == sorted(bc.SPECIALISTS)


def test_default_model_table_matches_builtin_defaults() -> None:
    """The default-model table shows each Agent CLI's built-in Specialist model."""
    rows = {
        "Specialists": "_DEFAULT_SPECIALIST_MODEL",
    }
    documented: dict[str, dict[str, str]] = {}
    for line in CONFIG_REFERENCE.read_text(encoding="utf-8").splitlines():
        cells = [cell.strip().strip("`") for cell in line.strip().strip("|").split("|")]
        if len(cells) == 3 and cells[0] in rows:
            documented[rows[cells[0]]] = {"claude": cells[1], "codex": cells[2]}
    assert set(documented) == set(rows.values()), "CONFIG.md default-model table not found"
    for attribute, models in documented.items():
        assert models == getattr(bc, attribute), attribute


def test_worktrees_section_documents_every_worktree_key() -> None:
    text = CONFIG_REFERENCE.read_text(encoding="utf-8")
    section = text.split("\n## Worktrees\n", 1)[1].split("\n## ", 1)[0]

    assert "[worktree]\nchat = " in section
    assert "gc_older_than = " in section
