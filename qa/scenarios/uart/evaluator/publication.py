"""Publish complete operator manifests without replacing previously frozen evidence."""

import json
import os
import tempfile
from pathlib import Path


def publish_new(path: Path, value: dict) -> None:
    """Atomically link complete bytes; concurrent publication never overwrites."""
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w", encoding="utf-8", dir=path.parent, prefix=".qa-manifest-", delete=False
        ) as stream:
            temporary = Path(stream.name)
            json.dump(value, stream, indent=2, sort_keys=True)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.link(temporary, path)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)
