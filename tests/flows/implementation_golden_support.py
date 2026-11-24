"""Shared rendering for the implementation Flow parity goldens."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from booley.flows.base import BuiltinFlow
from booley.runtime.endpoint_execution import EndpointOutcome
from tests.golden.conftest import normalize_work_dir

_TIME = "<TIME>"


def _mask_times(value: Any) -> Any:
    """Replace every ``timestamp`` / ``*_at`` value, whatever its depth."""
    if isinstance(value, dict):
        return {
            key: _TIME if key == "timestamp" or str(key).endswith("_at") else _mask_times(item)
            for key, item in value.items()
        }
    if isinstance(value, list):
        return [_mask_times(item) for item in value]
    return value


def _json_text(path: Path) -> str:
    data = json.loads(path.read_text(encoding="utf-8"))
    return json.dumps(_mask_times(data), indent=2, sort_keys=False)


def render_implementation_output(
    flow: BuiltinFlow[Any], report_name: str, outcome: EndpointOutcome, work_dir: Path
) -> str:
    """The stable Target report, progress record, ``RESULT:`` line and exit code."""
    result_line = next(
        line for line in reversed(outcome.report_text.splitlines()) if line.startswith("RESULT:")
    )
    text = "\n".join(
        [
            f"== {report_name} ==",
            _json_text(Path(flow.args.report_dir) / report_name),
            "== progress.json ==",
            _json_text(flow.reserve_invocation_dir() / "progress.json"),
            "== RESULT ==",
            result_line,
            "== exit code ==",
            str(outcome.exit_code),
        ]
    )
    return normalize_work_dir(text, work_dir)
