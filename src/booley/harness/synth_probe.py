"""Persistent peak-memory calibration from Doctor's heaviest synthesis run."""

from __future__ import annotations

import json
import os
from collections.abc import Collection
from pathlib import Path
from typing import Any

from booley.runtime.timefmt import utc_now_rfc3339

PROBE_FILENAME = "synth_probe.json"


def probe_path(project_dir: Path) -> Path:
    return project_dir / "runtime" / PROBE_FILENAME


def load_measurement(project_dir: Path) -> dict[str, Any] | None:
    try:
        data = json.loads(probe_path(project_dir).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    if not isinstance(data, dict):
        return None
    peak = data.get("peak_rss_bytes")
    target = data.get("target")
    if isinstance(peak, bool) or not isinstance(peak, int) or peak <= 0:
        return None
    if not isinstance(target, str) or not target:
        return None
    return data


def record_measurement(
    project_dir: Path,
    target: str,
    peak_rss_mb: float,
    *,
    selected_targets: Collection[str] | None = None,
) -> Path:
    """Atomically retain the largest selected synthesis measurement."""
    path = probe_path(project_dir)
    path.parent.mkdir(parents=True, exist_ok=True)
    peak_bytes = max(1, int(peak_rss_mb * 1024 * 1024))
    previous = load_measurement(project_dir)
    previous_is_selected = selected_targets is None or (
        previous is not None and previous.get("target") in selected_targets
    )
    if previous and previous_is_selected and int(previous["peak_rss_bytes"]) > peak_bytes:
        peak_bytes = int(previous["peak_rss_bytes"])
        target = str(previous["target"])
    payload = {
        "target": target,
        "peak_rss_bytes": peak_bytes,
        "measured_at": utc_now_rfc3339(),
    }
    tmp = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    tmp.write_text(json.dumps(payload) + "\n", encoding="utf-8")
    tmp.replace(path)
    return path
