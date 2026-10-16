"""Bounded, fresh-process discovery of Project-owned Python endpoint schemas."""

from __future__ import annotations

import json
import os
import subprocess
import sys
import sysconfig
import tempfile
import threading
import time
from pathlib import Path
from typing import Any

from pydantic import TypeAdapter

from booley.core.boundary import require_dict
from booley.runtime.process_group import (
    capture_process_group,
    new_group_kwargs,
    terminate_process_group,
)

DISCOVERY_TIMEOUT_SECONDS = 15.0
_MAX_OUTPUT_BYTES = 4 * 1024 * 1024
_DIAGNOSTIC_BYTES = 4096


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
        environment, search_path = _worker_environment(paths)
        request.write_text(
            json.dumps(
                {"paths": [str(path) for path in paths.values()], "search_path": search_path}
            )
        )
        command = [
            sys.executable,
            "-B",
            "-P",
            "-m",
            "booley.mcp.catalog_worker",
            str(request),
            str(result),
        ]
        with (directory / "worker.log").open("wb") as output:
            process = subprocess.Popen(
                command, stdout=output, stderr=output, env=environment, **new_group_kwargs()
            )
            group = capture_process_group(process)
            try:
                _wait_for_worker(process, output, result, cancel)
            except RuntimeError as exc:
                raise RuntimeError(f"{exc}\n{_diagnostic_tail(directory / 'worker.log')}") from exc
            finally:
                # Also terminate descendants that outlive a successfully exited importer.
                terminate_process_group(process, group, grace_seconds=0.2)
        return _read_result(result)


def _worker_environment(paths: dict[str, Path]) -> tuple[dict[str, str], list[str]]:
    """Keep Project imports out of the framework preload's shared bytecode cache."""
    environment = dict(os.environ)
    directory = next(iter(paths.values())).parent.parent
    # Retain the server's selection even after a Project Root selector edit.
    environment["BOOLEY_DIR"] = str(directory)
    source_root = str(Path(__file__).resolve().parents[2])
    search_path = [source_root, *sys.path, *environment.get("PYTHONPATH", "").split(os.pathsep)]
    project_roots = {Path.cwd().resolve(), directory.resolve()}
    project_roots.update(path.parent.resolve() for path in paths.values())
    installed = {Path(value).resolve() for value in sysconfig.get_paths().values()}
    bootstrap = []
    for raw_path in search_path:
        path = Path(raw_path).resolve()
        trusted = str(path) == source_root or path in installed
        if trusted or not any(path.is_relative_to(root) for root in project_roots):
            bootstrap.append(raw_path)
    environment["PYTHONPATH"] = os.pathsep.join(bootstrap)
    return environment, search_path


def _diagnostic_tail(path: Path) -> str:
    """Retain a bounded importer diagnostic before its temporary log is removed."""
    with path.open("rb") as stream:
        stream.seek(max(0, path.stat().st_size - _DIAGNOSTIC_BYTES))
        return stream.read(_DIAGNOSTIC_BYTES).decode(errors="replace").strip()


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

    data = require_dict(json.loads(request.read_text()), field="catalog worker request")
    # Only trusted framework/dependency paths were visible during preload.
    # Project modules and sibling helpers must never reuse timestamp-based pyc.
    sys.pycache_prefix = str(result.parent / "pycache")
    sys.path[:] = data["search_path"]
    definitions, errors = [], []
    adapter = TypeAdapter(dict[str, Any])
    for raw_path in data["paths"]:
        path = Path(raw_path)
        sys.path.insert(0, str(path.parent))
        try:
            definition, error = _import_and_extract_file(path)
        finally:
            sys.path.pop(0)
        if definition is not None:
            try:
                # Match the SDK's JSON serialization of Path/Enum defaults,
                # while isolating an unsupported value to its own endpoint.
                definitions.append(adapter.dump_python(definition, mode="json"))
            except (TypeError, ValueError) as exc:
                errors.append(f"INVALID CUSTOM MCP SCHEMA: {definition['name']}: {exc}")
        if error is not None:
            errors.append(error)
    result.write_text(json.dumps({"definitions": definitions, "errors": errors}))


if __name__ == "__main__":
    _main(Path(sys.argv[1]), Path(sys.argv[2]))
