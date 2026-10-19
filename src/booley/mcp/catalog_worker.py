"""Bounded, fresh-process discovery of Project-owned Python endpoint schemas."""

from __future__ import annotations

import importlib
import json
import logging
import os
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path
from typing import Any, BinaryIO

from pydantic import TypeAdapter

from booley.core.boundary import require_dict
from booley.mcp import catalog
from booley.runtime.process_group import (
    capture_process_group,
    new_group_kwargs,
    terminate_process_group,
)

logger = logging.getLogger(__name__)

DISCOVERY_TIMEOUT_SECONDS = 15.0
_DIAGNOSTIC_BYTES = 4096


def run_schema_worker(
    paths: dict[str, Path], cancel: threading.Event | None = None
) -> tuple[list[dict[str, Any]], list[str]]:
    """Extract schemas without executing Project imports in the serving process."""
    if not paths:
        return [], []
    cancel = cancel or threading.Event()
    # Descendants inherit stdout; keep its backing file outside the request tree
    # so Windows handle teardown cannot prevent that tree's cleanup.
    with (
        tempfile.TemporaryDirectory(prefix="booley-catalog-") as temporary,
        tempfile.TemporaryFile() as output,
    ):
        directory = Path(temporary)
        request = directory / "request.json"
        result = directory / "result.json"
        project_directory = next(iter(paths.values())).parent.parent
        environment = {**os.environ, "BOOLEY_DIR": str(project_directory)}
        environment["PYTHONPATH"] = os.pathsep.join(
            (str(Path(__file__).resolve().parents[2]), environment.get("PYTHONPATH", ""))
        )
        request.write_text(json.dumps({"paths": [str(path) for path in paths.values()]}))
        command = [
            sys.executable,
            "-B",
            "-P",
            "-m",
            "booley.mcp.catalog_worker",
            str(request),
            str(result),
        ]
        process = subprocess.Popen(
            command, stdout=output, stderr=output, env=environment, **new_group_kwargs()
        )
        group = capture_process_group(process)
        try:
            _wait_for_worker(process, cancel)
        except RuntimeError as exc:
            raise RuntimeError(f"{exc}\n{_diagnostic_tail(output)}") from exc
        finally:
            # Also terminate descendants that outlive a successfully exited importer.
            terminate_process_group(process, group, grace_seconds=0.2)
        return _read_result(result)


def _diagnostic_tail(output: BinaryIO) -> str:
    """Retain a bounded importer diagnostic before its temporary log is removed."""
    output.seek(0, os.SEEK_END)
    output.seek(max(0, output.tell() - _DIAGNOSTIC_BYTES))
    return output.read(_DIAGNOSTIC_BYTES).decode(errors="replace").strip()


def _wait_for_worker(process, cancel: threading.Event) -> None:
    deadline = time.monotonic() + DISCOVERY_TIMEOUT_SECONDS
    while process.poll() is None and time.monotonic() < deadline:
        if cancel.wait(0.05):
            raise RuntimeError("Custom MCP schema discovery cancelled")
    if process.poll() is None:
        raise RuntimeError("Custom MCP schema discovery timed out")
    if process.returncode != 0:
        raise RuntimeError(f"Custom MCP schema discovery exited {process.returncode}")


def _read_result(path: Path) -> tuple[list[dict[str, Any]], list[str]]:
    data = require_dict(json.loads(path.read_text()), field="catalog worker result")
    definitions = data.get("definitions")
    errors = data.get("errors")
    if not isinstance(definitions, list) or not isinstance(errors, list):
        raise ValueError("Invalid catalog worker result")
    return (
        [require_dict(item, field="catalog worker definition") for item in definitions],
        [str(item) for item in errors],
    )


def _import_file_safe(
    py_path: Path,
) -> tuple[object | None, str | None]:
    """Import a Python file as a module. Returns (module, None) or (None, error_msg)."""
    import importlib.util
    import traceback

    module_name = py_path.stem
    try:
        spec = importlib.util.spec_from_file_location(
            f"booley_custom_mcp_tools.{module_name}",
            py_path,
        )
        if spec is None or spec.loader is None:
            return None, f"Could not create module spec for {py_path}"
        mod = importlib.util.module_from_spec(spec)
        sys.modules[mod.__name__] = mod
        spec.loader.exec_module(mod)
        return mod, None
    except Exception as exc:
        msg = f"IMPORT FAILED: custom MCP tool {py_path}: {exc}"
        logger.error(msg, exc_info=True)
        print(f"[mcp] ERROR {msg}", file=sys.stderr, flush=True)
        tb = traceback.format_exc()
        print(f"[mcp] TRACEBACK:\n{tb}", file=sys.stderr, flush=True)
        return None, msg


def _import_and_extract_file(
    py_path: Path,
) -> tuple[dict[str, Any] | None, str | None]:
    """Import a custom MCP tool from a file path and extract its schema."""
    mod, err = _import_file_safe(py_path)
    if err is not None:
        return None, err

    found = catalog._find_mcp_tool_class_in_module(mod, str(py_path))
    if found is None:
        msg = f"NO MCP ENDPOINT CLASS FOUND: custom MCP tool {py_path} has no recognized MCP endpoint subclass"
        logger.warning(msg)
        return None, msg

    cls, _instance, schema = found
    return catalog._mcp_tool_def_from_class(
        cls,
        schema,
        py_path.stem,
        is_custom=True,
        custom_path=str(py_path),
    ), None


def _preload_schema_modules() -> None:
    """Warm neutral extraction dependencies before enabling Project imports."""
    for module in ("booley.flows.base", "booley.mcp.base", "booley.mcp.flow_adapter"):
        importlib.import_module(module)


def _main(request: Path, result: Path) -> None:
    _preload_schema_modules()

    data = require_dict(json.loads(request.read_text()), field="catalog worker request")
    # Project modules and sibling helpers must never reuse timestamp-based pyc.
    sys.pycache_prefix = str(result.parent / "pycache")
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
