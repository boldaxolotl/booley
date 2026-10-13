"""Read-only discovery of source stashes left by older Booley versions."""

from __future__ import annotations

import json
import logging
import os
import stat
import tempfile
from pathlib import Path

logger = logging.getLogger(__name__)


def _belongs_to_project(manifest: Path, work_dir: Path) -> bool:
    """Inspect an owned regular sidecar without trusting its restoration paths."""
    metadata = manifest.lstat()
    if not stat.S_ISREG(metadata.st_mode) or metadata.st_size > 1024 * 1024:
        return False
    getuid = getattr(os, "getuid", None)
    if getuid is not None and metadata.st_uid != getuid():
        return False
    data = json.loads(manifest.read_text(encoding="utf-8"))
    return isinstance(data, dict) and data.get("work_dir") == str(work_dir)


def legacy_stash_manifests(work_dir: Path) -> list[Path]:
    """Find this Project's old sidecars, including retired recovery records.

    Never unlock, restore, delete, or modify a stash or its sidecar. Human
    inspection decides whether the record describes missing uncommitted work.
    """
    matches: list[Path] = []
    try:
        candidates = sorted(Path(tempfile.gettempdir()).glob("booley_*.manifest.json*"))
    except OSError as exc:
        logger.warning("Could not inspect legacy source stash records: %s", exc)
        return matches
    for manifest in candidates:
        if not manifest.name.startswith(("booley_isolation_", "booley_hide_files_")):
            continue
        if not manifest.name.endswith((".manifest.json", ".manifest.json.retired")):
            continue
        try:
            if _belongs_to_project(manifest, work_dir.resolve()):
                matches.append(manifest)
        except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
            logger.debug("Could not inspect legacy source stash record %s: %s", manifest, exc)
    return matches
