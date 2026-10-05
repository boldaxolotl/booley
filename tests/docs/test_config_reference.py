"""Keep the configuration reference aligned with recognized project tables."""

import re
from pathlib import Path

import pytest

from booley.audit.project_schema import KNOWN_BOOLEY_TOML_TABLES

CONFIG_REFERENCE = Path(__file__).resolve().parents[2] / "docs/user/CONFIG.md"


# Project [interactive] recognizes migration inputs only: policy and app are
# rejected, and no field supplies a current setting. Recognition stays intact;
# tests/harness/test_doctor.py proves each field's user-visible replacement.
MIGRATION_ONLY_TABLES = {"interactive"}


@pytest.mark.parametrize("table", sorted(KNOWN_BOOLEY_TOML_TABLES - MIGRATION_ONLY_TABLES))
def test_config_reference_documents_known_booley_toml_table(table: str) -> None:
    reference = CONFIG_REFERENCE.read_text(encoding="utf-8")

    table_reference = rf"\[{re.escape(table)}(?:\.[^\]\n]+)?\]"
    assert re.search(table_reference, reference), f"booley.toml [{table}] missing from CONFIG.md"
