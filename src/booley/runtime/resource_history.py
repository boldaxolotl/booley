"""Per-Project Job resource history: bounded, atomic and ignored by Git.

The history lives at ``<Booley Directory>/runtime/resources/history.json``
(``runtime/`` is Git-ignored). Writers serialize on a sibling lock file and
publish the whole document with one atomic replacement, so concurrent Jobs
never lose or corrupt a record. Each history key keeps its newest samples;
older ones roll off.
"""

from __future__ import annotations

import json
import logging
import statistics
from collections import Counter
from pathlib import Path
from typing import Any

from booley.core.boundary import as_dict
from booley.core.file_lock import release_file_lock, wait_for_file_lock
from booley.runtime.atomic_files import atomic_replace_bytes
from booley.runtime.timefmt import format_duration

logger = logging.getLogger(__name__)

SCHEMA = "booley.job-resource-history/v1"
MAX_SAMPLES_PER_KEY = 20
MAX_RECORDS = 2000
_LOCK_TIMEOUT_S = 10.0


def history_path(booley_dir: Path) -> Path:
    return booley_dir / "runtime" / "resources" / "history.json"


def _key(record: dict[str, Any]) -> tuple[str, str, tuple[str, ...], str]:
    key = record["key"]
    return key["target"], key["phase"], tuple(key["flags"]), key["test_run"]


def load_records(booley_dir: Path) -> list[dict[str, Any]]:
    """Read the history; a missing file is an empty history."""
    path = history_path(booley_dir)
    try:
        document = as_dict(json.loads(path.read_text(encoding="utf-8")))
    except FileNotFoundError:
        return []
    if document is None or document.get("$schema") != SCHEMA:
        raise ValueError(f"{path}: not a {SCHEMA} document")
    return list(document["records"])


def _bounded(records: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Keep the newest samples of each key, then the newest records overall."""
    per_key: Counter[tuple[str, str, tuple[str, ...], str]] = Counter()
    kept: list[dict[str, Any]] = []
    for record in reversed(records):
        key = _key(record)
        if per_key[key] < MAX_SAMPLES_PER_KEY:
            per_key[key] += 1
            kept.append(record)
    kept.reverse()
    return kept[-MAX_RECORDS:]


def append_records(booley_dir: Path, records: list[dict[str, Any]]) -> None:
    """Add *records* under the history lock and publish the bounded result."""
    path = history_path(booley_dir)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.with_suffix(".lock").open("a+", encoding="utf-8") as handle:
        wait_for_file_lock(handle, timeout_s=_LOCK_TIMEOUT_S)
        try:
            try:
                existing = load_records(booley_dir)
            except ValueError as exc:
                logger.warning("Starting a new Job resource history: %s", exc)
                existing = []
            document = {"$schema": SCHEMA, "records": _bounded([*existing, *records])}
            atomic_replace_bytes(path, json.dumps(document, separators=(",", ":")).encode("utf-8"))
        finally:
            release_file_lock(handle)


def _spread(values: list[float | None]) -> dict[str, float | None] | None:
    """Min, median, max and max/median (how far the worst run strays) of measured values."""
    measured = [value for value in values if value is not None]
    if not measured:
        return None
    median = statistics.median(measured)
    top = max(measured)
    return {
        "min": min(measured),
        "median": median,
        "max": top,
        "ratio": top / median if median else None,
    }


def summarize(records: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Statistics per history key, in key order."""
    groups: dict[tuple[str, str, tuple[str, ...], str], list[dict[str, Any]]] = {}
    for record in records:
        groups.setdefault(_key(record), []).append(record)
    summaries = []
    for _, group in sorted(groups.items()):
        solo = [
            r
            for r in group
            if r["solo"] and None not in (r["container_peak_delta_mb"], r["peak_memory_mb"])
        ]
        summaries.append(
            {
                "key": group[0]["key"],
                "runs": len(group),
                "peak_memory_mb": _spread([r["peak_memory_mb"] for r in group]),
                "wall_s": _spread([r["wall_s"] for r in group]),
                "solo_runs": len(solo),
                # Paired per run: a max of each column could come from different runs.
                "solo_container_over_rss": _spread(
                    [
                        r["container_peak_delta_mb"] / r["peak_memory_mb"]
                        for r in solo
                        if r["peak_memory_mb"]
                    ]
                ),
            }
        )
    return summaries


def _line(summary: dict[str, Any]) -> str:
    key = summary["key"]
    flags = f" [{','.join(key['flags'])}]" if key["flags"] else ""
    tests = f" tests={key['test_run']}" if key["test_run"] else ""
    parts = [
        f"{key['target'] or '(no Target)'} {key['phase']}{flags}{tests}: {summary['runs']} run{'' if summary['runs'] == 1 else 's'}"
    ]
    for label, field, unit in (("peak memory", "peak_memory_mb", "MiB"), ("wall", "wall_s", "")):
        spread = summary[field]
        if spread is None:
            parts.append(f"{label} unmeasured")
            continue
        shown = [
            f"{spread[n]:.0f} MiB" if unit else format_duration(spread[n])
            for n in ("min", "median", "max")
        ]
        ratio = "-" if spread["ratio"] is None else f"{spread['ratio']:.2f}"
        parts.append(f"{label} {' / '.join(shown)} (max/median {ratio})")
    if summary["solo_runs"]:
        ratio = summary["solo_container_over_rss"]
        shown = (
            "unmeasured"
            if ratio is None
            else " / ".join(f"{ratio[n]:.2f}" for n in ("min", "median", "max"))
        )
        parts.append(f"solo runs {summary['solo_runs']}: container peak / (RSS+TMPDIR) {shown}")
    return "; ".join(parts)


def render_report(summaries: list[dict[str, Any]]) -> str:
    """One line per history key: min / median / max, max/median and solo calibration."""
    if not summaries:
        return "No Job resource history yet: run a Booley Flow first."
    return "\n".join(
        [
            *(_line(summary) for summary in summaries),
            "Peak memory = process-tree RSS + the Job's own TMPDIR files; "
            "files a tool writes outside TMPDIR are not counted.",
        ]
    )
