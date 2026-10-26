"""Keep the configuration reference aligned with recognized project tables."""

import re
from pathlib import Path

import pytest

from booley.audit.project_schema import KNOWN_BOOLEY_TOML_TABLES

USER_DOCS = Path(__file__).resolve().parents[2] / "docs/user"
CONFIG_REFERENCE = USER_DOCS / "CONFIG.md"
# CONFIG.md is the hub; these pages document the booley.toml tables it links to.
CONFIG_PAGES = (
    "CONFIG.md",
    "TARGETS.md",
    "TESTS.md",
    "SANDBOX.md",
    "STEALTH.md",
    "FLOW-SIM.md",
    "COVERAGE.md",
    "FLOW-LINT.md",
    "FLOW-SYNTH.md",
    "FLOW-FPGA.md",
)


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


def test_config_reference_lists_pinnable_roles_exactly() -> None:
    from booley.config.agent import KNOWN_ROLES

    reference = " ".join(CONFIG_REFERENCE.read_text(encoding="utf-8").split())
    sentence = re.search(r"Specialist roles are (.+?)\.", reference)
    assert sentence, "CONFIG.md must say 'Specialist roles are ...' for [models.roles]"
    assert set(re.findall(r"`([a-z_]+)`", sentence.group(1))) == KNOWN_ROLES
