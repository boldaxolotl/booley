"""Keep the configuration reference aligned with recognized project tables."""

import re
from pathlib import Path

import pytest
from tests.config_docs_support import CONFIG_PAGES

from booley.audit.project_schema import KNOWN_BOOLEY_TOML_TABLES

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


def test_config_reference_lists_pinnable_roles_exactly() -> None:
    from booley.config.agent import KNOWN_ROLES

    reference = " ".join(CONFIG_REFERENCE.read_text(encoding="utf-8").split())
    sentence = re.search(r"Specialist roles are (.+?)\.", reference)
    assert sentence, "CONFIG.md must say 'Specialist roles are ...' for [models.roles]"
    assert set(re.findall(r"`([a-z_]+)`", sentence.group(1))) == KNOWN_ROLES


def test_tier_table_matches_provider_defaults(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The `[models]` tier table shows the defaults a project gets without pins."""
    from booley.config import agent as agent_config

    documented: dict[str, dict[str, str]] = {"claude": {}, "codex": {}}
    for line in CONFIG_REFERENCE.read_text(encoding="utf-8").splitlines():
        cells = [cell.strip().strip("`") for cell in line.strip().strip("|").split("|")]
        if len(cells) == 3 and cells[0] in {"heavy", "standard", "light"}:
            documented["claude"][cells[0]] = cells[1]
            documented["codex"][cells[0]] = cells[2]
    assert len(documented["claude"]) == 3, "CONFIG.md tier table not found"

    for var in ("BOOLEY_PRIMARY_PROVIDER", "BOOLEY_PRIMARY_AUTH", "BOOLEY_DIR"):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setattr(agent_config, "_inside_container", lambda: False)
    project_data = tmp_path / ".booley"
    project_data.mkdir()
    for provider, rows in documented.items():
        monkeypatch.setattr(agent_config, "_agent_settings", None)
        (project_data / "booley.toml").write_text(f'[agent]\nprovider = "{provider}"\n')
        agent_config.load_agent_settings(tmp_path)
        settings = agent_config.get_agent_settings()
        assert rows == {tier: settings.model_for_tier(tier) for tier in rows}, provider
