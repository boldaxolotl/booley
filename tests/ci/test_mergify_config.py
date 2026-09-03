from __future__ import annotations

from pathlib import Path

MERGIFY_CONFIG = Path(__file__).parents[2] / ".mergify.yml"


def test_merge_queue_uses_mergify_integration_for_updates_and_merges() -> None:
    config = MERGIFY_CONFIG.read_text(encoding="utf-8")

    assert "update_bot_account:" not in config
    assert "merge_bot_account:" not in config
