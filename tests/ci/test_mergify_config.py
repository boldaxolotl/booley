from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from typing import Any

import yaml

from booley.core.boundary import require_dict, require_list

MERGIFY_CONFIG = Path(__file__).parents[2] / ".mergify.yml"
WATCH_PR_SCRIPT = Path(__file__).parents[2] / ".github/scripts/watch_pr.py"


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


def _default_rule(config: dict[Any, Any]) -> dict[Any, Any]:
    queue_rules = require_list(config.get("queue_rules"), field="queue_rules")
    return require_dict(queue_rules[0], field="queue_rules[0]")


def _load_watch_pr() -> Any:
    spec = importlib.util.spec_from_file_location("watch_pr", WATCH_PR_SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def test_merge_queue_checks_four_serial_batches_of_up_to_three_prs() -> None:
    """Four cumulative candidates run at once, each adding up to three PRs.

    docs/internals/agents/merge-queue.md documents this contract for agents that
    monitor and recover queued PRs.
    """
    config = _load_config()
    merge_queue = require_dict(config.get("merge_queue"), field="merge_queue")
    default_rule = _default_rule(config)

    assert merge_queue["mode"] == "serial"
    assert merge_queue["max_parallel_checks"] == 4
    assert default_rule["batch_size"] == 3


def test_partial_batch_starts_within_thirty_seconds() -> None:
    """A lone queued PR waits at most 30 seconds for batchmates.

    docs/internals/agents/merge-queue.md documents this bound.
    """
    default_rule = _default_rule(_load_config())

    assert default_rule["batch_max_wait_time"] == "30 seconds"


def test_failed_batch_is_split_until_its_culprit_is_found() -> None:
    """Leaving the resolution cap unset makes Mergify split a failed batch.

    Mergify then dequeues only the culprit and merges the other PRs; a cap of 0
    would dequeue every batchmate.
    """
    default_rule = _default_rule(_load_config())

    assert "batch_max_failure_resolution_attempts" not in default_rule


def test_merge_queue_keeps_required_checks_retries_and_labels() -> None:
    config = _load_config()
    merge_queue = require_dict(config.get("merge_queue"), field="merge_queue")
    default_rule = _default_rule(config)
    watch_pr = _load_watch_pr()
    required_conditions = [
        "base = main",
        "check-success = @github-actions/ci-required",
        "check-success = @github-actions/confidential-content",
    ]

    assert default_rule["queue_conditions"] == required_conditions
    assert default_rule["merge_conditions"] == required_conditions
    assert default_rule["max_checks_retries"] == 0
    assert merge_queue["dequeued_label"] == watch_pr.DEQUEUED_LABEL
    assert {merge_queue["queued_label"], merge_queue["checking_label"]} == set(
        watch_pr.ACTIVE_QUEUE_LABELS
    )


def test_merge_queue_timeout_covers_cold_runtime_build() -> None:
    config = _load_config()
    queue_rules = require_list(config.get("queue_rules"), field="queue_rules")
    default_rule = require_dict(queue_rules[0], field="queue_rules[0]")

    assert default_rule["name"] == "default"
    assert default_rule["checks_timeout"] == "90 minutes"


def test_merge_queue_prioritizes_urgent_and_ci_fixes() -> None:
    config = _load_config()

    for label in ("urgent", "ci"):
        rule = _priority_rule(config, f"label = {label}")
        assert rule["priority"] == "high"
        assert rule["allow_checks_interruption"] is False
