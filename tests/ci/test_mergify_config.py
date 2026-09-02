from __future__ import annotations

from pathlib import Path

MERGIFY_CONFIG = Path(__file__).parents[2] / ".mergify.yml"


def test_merge_queue_impersonates_pull_request_author() -> None:
    config = MERGIFY_CONFIG.read_text(encoding="utf-8")

    assert 'update_bot_account: "{{ author }}"' in config
    assert 'merge_bot_account: "{{ author }}"' in config
