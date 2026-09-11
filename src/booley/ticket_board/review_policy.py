"""Resolve live Ticket review policy into dependency-neutral receipt values."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

from booley.core.boundary import as_dict, as_str_list
from booley.runtime.shared_infra import _load_rtl_config
from booley.targets.flow_names import config_section


def _digest(value: Any) -> str:
    encoded = json.dumps(value, sort_keys=True, separators=(",", ":")).encode()
    return hashlib.sha256(encoded).hexdigest()


def review_policy_digest(work_dir: Path, category: str) -> str:
    """Return the current policy identity used by one Reviewer invocation."""
    if category != "tb":
        return _digest({})
    try:
        cfg = _load_rtl_config(work_dir)
    except ImportError:
        cfg = None
    flows = as_dict((cfg or {}).get("flows"), default={}) or {}
    sim = config_section(flows, "sim")
    return _digest(
        {
            "pass_sentinels": as_str_list(sim.get("pass_sentinels")),
            "fail_sentinels": as_str_list(sim.get("fail_sentinels")),
            "trace_files": as_str_list(sim.get("trace_files")),
        }
    )
