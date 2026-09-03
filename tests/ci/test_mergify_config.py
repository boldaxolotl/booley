from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml

from booley.core.boundary import require_dict, require_list

MERGIFY_CONFIG = Path(__file__).parents[2] / ".mergify.yml"


def _load_config() -> dict[Any, Any]:
    parsed = yaml.safe_load(MERGIFY_CONFIG.read_text(encoding="utf-8"))
    return require_dict(parsed, field=".mergify.yml")


def _priority_rule(config: dict[Any, Any], condition: str) -> dict[Any, Any]:
    rules = require_list(config.get("priority_rules"), field="priority_rules")
    for index, raw_rule in enumerate(rules):
        rule = require_dict(raw_rule, field=f"priority_rules[{index}]")
        conditions = require_list(
            rule.get("conditions"), field=f"priority_rules[{index}].conditions"
        )
        if conditions == [condition]:
            return rule
    raise AssertionError(f"missing priority rule for {condition!r}")


def test_merge_queue_uses_mergify_integration_for_updates_and_merges() -> None:
    config = MERGIFY_CONFIG.read_text(encoding="utf-8")

    assert "update_bot_account:" not in config
    assert "merge_bot_account:" not in config


def test_merge_queue_prioritizes_urgent_and_ci_fixes() -> None:
    config = _load_config()

    for label in ("urgent", "ci"):
        rule = _priority_rule(config, f"label = {label}")
        assert rule["priority"] == "high"
        assert rule["allow_checks_interruption"] is False
