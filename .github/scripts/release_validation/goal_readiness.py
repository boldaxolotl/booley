"""Require the public demo's readiness proof before recording a release check.

Readiness proves Goal entry, status inspection and Target resolution. Goals
may still be unmet: this document is not evidence of Goal Mode completion.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).parents[3] / "src"))

from booley.core.boundary import require_dict, require_int, require_str_value
from booley.dev_support.demo_contract_codec import load_contract
from booley.goals.translate import translate_goals
from booley.targets.selection import split_selector, vlnv_matches


def _read_document(path: Path) -> dict[str, Any]:
    try:
        contents = path.read_text(encoding="utf-8")
    except (OSError, UnicodeError) as exc:
        raise ValueError(f"cannot read Goal readiness evidence {path}: {exc}") from exc
    try:
        return require_dict(json.loads(contents), field="Goal readiness evidence")
    except json.JSONDecodeError as exc:
        raise ValueError(f"Goal readiness evidence {path}: malformed JSON: {exc}") from exc


def _require_operations(document: dict[str, Any]) -> None:
    record_id = require_str_value(document.get("record_id"), field="record_id")
    if not record_id.strip():
        raise ValueError("Goal readiness record_id must not be blank")
    steps = require_dict(document.get("steps"), field="steps")
    for name in ("goal_enter", "goal_status"):
        step = require_dict(steps.get(name), field=name)
        if step.get("status") != "pass":
            raise ValueError(f"Goal readiness {name} status must be pass")
        response = require_str_value(step.get("response"), field=f"{name}.response")
        if not response.strip():
            raise ValueError(f"Goal readiness {name}.response must not be blank")
        if name == "goal_enter" and step.get("record_id") != record_id:
            raise ValueError("Goal readiness goal_enter.record_id differs from record_id")


def _require_target(value: object, *, key: str, selector: str) -> None:
    identity = require_str_value(value, field=f"Goal {key} resolved Target")
    core, name = split_selector(identity)
    qualifier, expected_name = split_selector(selector)
    if (
        identity != identity.strip()
        or identity.count("#") != 1
        or core is None
        or len(core.split(":")) != 4
        or not core.split(":")[2]
        or not name
    ):
        raise ValueError(f"Goal {key} has malformed resolved Target identity {identity!r}")
    if name != expected_name or (qualifier is not None and not vlnv_matches(qualifier, core)):
        raise ValueError(f"Goal {key} resolved Target {identity!r} does not match {selector!r}")


def validate(contract: Path, readiness: Path) -> None:
    """Read existing evidence and require every Goal from the single demo contract."""
    expected = translate_goals(load_contract(contract).required_goals).goals
    document = _read_document(readiness)
    if require_int(document.get("schema"), field="schema") != 1:
        raise ValueError("Goal readiness schema must be 1")
    if document.get("state") != "active":
        raise ValueError("Goal readiness Goal State must be active")
    _require_operations(document)
    goals = require_dict(document.get("goals"), field="goals")
    targets = require_dict(document.get("resolved_targets"), field="resolved_targets")
    for goal in expected:
        if goal.key not in goals:
            raise ValueError(f"Goal readiness is missing contract Goal {goal.key}")
        status = require_str_value(goals[goal.key], field=f"Goal {goal.key} status")
        if status not in {"met", "unmet", "stale"}:
            raise ValueError(f"Goal {goal.key} has invalid readiness status {status!r}")
        assert goal.target is not None  # The demo contract requires every Goal's Target.
        _require_target(targets.get(goal.key), key=goal.key, selector=goal.target)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--contract", required=True, type=Path)
    parser.add_argument("--readiness", required=True, type=Path)
    args = parser.parse_args(argv)
    try:
        validate(args.contract, args.readiness)
    except ValueError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 2
    print("Goal readiness evidence passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
