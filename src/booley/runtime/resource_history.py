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
from collections import Counter
from pathlib import Path
from typing import Any

from booley.core.boundary import as_dict
from booley.core.file_lock import release_file_lock, wait_for_file_lock
from booley.runtime.atomic_files import atomic_replace_bytes

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
