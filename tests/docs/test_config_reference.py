"""Keep the configuration reference aligned with recognized project tables."""

from pathlib import Path

import pytest

from booley.audit.project_schema import KNOWN_BOOLEY_TOML_TABLES

CONFIG_REFERENCE = Path(__file__).resolve().parents[2] / "docs/user/CONFIG.md"


@pytest.mark.parametrize("table", sorted(KNOWN_BOOLEY_TOML_TABLES))
def test_config_reference_documents_known_booley_toml_table(table: str) -> None:
    reference = CONFIG_REFERENCE.read_text(encoding="utf-8")

    assert f"[{table}" in reference, f"booley.toml [{table}] missing from CONFIG.md"
