"""Canonical Simulation invocation paths and atomic JSON publication."""

import json
import os
import tempfile
from pathlib import Path
from urllib.parse import quote


def target_report_directory(invocation: Path, selector: str) -> Path:
    """Encode qualified selectors as one portable, collision-free path component."""
    return invocation / "targets" / quote(selector, safe="")


def write_campaign_json(path: Path, document: dict[str, object]) -> None:
    """Publish a complete JSON document, never a partially written report."""
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary = tempfile.mkstemp(prefix=".coverage-", dir=path.parent)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            json.dump(document, stream, sort_keys=True, indent=2, allow_nan=False)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        Path(temporary).replace(path)
    finally:
        Path(temporary).unlink(missing_ok=True)
