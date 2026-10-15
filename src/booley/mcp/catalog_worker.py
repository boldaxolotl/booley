"""Bounded, fresh-process discovery of Project-owned Python endpoint schemas."""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path
from typing import Any

from booley.core.boundary import require_dict
from booley.runtime.process_group import (
    capture_process_group,
    new_group_kwargs,
    terminate_process_group,
)

DISCOVERY_TIMEOUT_SECONDS = 15.0
_MAX_OUTPUT_BYTES = 4 * 1024 * 1024


def run_schema_worker(
    paths: dict[str, Path], cancel: threading.Event | None = None
) -> tuple[list[dict[str, Any]], list[str]]:
    """Extract schemas without executing Project imports in the serving process."""
    if not paths:
        return [], []
    cancel = cancel or threading.Event()
    with tempfile.TemporaryDirectory(prefix="booley-catalog-") as temporary:
        directory = Path(temporary)
        request = directory / "request.json"
        result = directory / "result.json"
        request.write_text(json.dumps([str(path) for path in paths.values()]))
        command = [
            sys.executable,
            "-B",
            "-X",
            f"pycache_prefix={directory / 'pycache'}",
            "-m",
            "booley.mcp.catalog_worker",
            str(request),
            str(result),
        ]
        environment = dict(os.environ)
        # Schema hooks must retain the running server's selected root even if
        # the Project Root selector was edited after server startup.
        environment["BOOLEY_DIR"] = str(next(iter(paths.values())).parent.parent)
        source_root = str(Path(__file__).resolve().parents[2])
        environment["PYTHONPATH"] = os.pathsep.join(
            [source_root, *sys.path, environment.get("PYTHONPATH", "")]
        )
        with (directory / "worker.log").open("wb") as output:
            process = subprocess.Popen(
                command, stdout=output, stderr=output, env=environment, **new_group_kwargs()
            )
            group = capture_process_group(process)
            try:
                _wait_for_worker(process, output, result, cancel)
            finally:
                # Also terminate descendants that outlive a successfully exited importer.
                terminate_process_group(process, group, grace_seconds=0.2)
        return _read_result(result)


def _wait_for_worker(process, output, result: Path, cancel: threading.Event) -> None:
    deadline = time.monotonic() + DISCOVERY_TIMEOUT_SECONDS
    while process.poll() is None and time.monotonic() < deadline:
        if cancel.wait(0.05):
            raise RuntimeError("Custom MCP schema discovery cancelled")
        if output.tell() > _MAX_OUTPUT_BYTES or (
            result.exists() and result.stat().st_size > _MAX_OUTPUT_BYTES
        ):
            raise RuntimeError("Custom MCP schema discovery exceeded output limit")
    if output.tell() > _MAX_OUTPUT_BYTES:
        raise RuntimeError("Custom MCP schema discovery exceeded output limit")
    if process.poll() is None:
        raise RuntimeError("Custom MCP schema discovery timed out")
    if process.returncode != 0:
        raise RuntimeError(f"Custom MCP schema discovery exited {process.returncode}")


def _read_result(path: Path) -> tuple[list[dict[str, Any]], list[str]]:
    if path.stat().st_size > _MAX_OUTPUT_BYTES:
        raise RuntimeError("Custom MCP schema discovery exceeded output limit")
    data = require_dict(json.loads(path.read_text()), field="catalog worker result")
    definitions = data.get("definitions")
    errors = data.get("errors")
    if not isinstance(definitions, list) or not isinstance(errors, list):
        raise ValueError("Invalid catalog worker result")
    return (
        [require_dict(item, field="catalog worker definition") for item in definitions],
        [str(item) for item in errors],
    )


def _main(request: Path, result: Path) -> None:
    from booley.mcp.server import _import_and_extract_file

    definitions, errors = [], []
    for raw_path in json.loads(request.read_text()):
        path = Path(raw_path)
        sys.path.insert(0, str(path.parent))
        try:
            definition, error = _import_and_extract_file(path)
        finally:
            sys.path.pop(0)
        if definition is not None:
            definitions.append(definition)
        if error is not None:
            errors.append(error)
    result.write_text(json.dumps({"definitions": definitions, "errors": errors}))


if __name__ == "__main__":
    _main(Path(sys.argv[1]), Path(sys.argv[2]))
