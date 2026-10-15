"""Live Project inputs, schema caching, and atomic application publication.

Static/synthetic schema assembly remains owned by the server's definitions
provider. This module deliberately has no MCP SDK dependency.
"""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import logging
import os
import threading
from collections.abc import Callable
from pathlib import Path
from typing import Any

import anyio

from booley.mcp.application import McpApplication
from booley.mcp.catalog_worker import run_schema_worker
from booley.mcp.endpoint_config import read_endpoint_config
from booley.mcp.registry import McpToolInfo, discover_mcp_tools
from booley.runtime.booley_dir import resolve_booley_dir

logger = logging.getLogger(__name__)
Definitions = list[dict[str, Any]]


def selected_directory() -> Path | None:
    """Preserve built-in-only startup when no Project has been selected."""
    try:
        return resolve_booley_dir()
    except FileNotFoundError:
        return None


def custom_directory(directory: Path | None) -> Path | None:
    """Validate the retired directory boundary, including later installations."""
    if directory is None:
        return None
    current, retired = directory / "mcp_tools", directory / "tools"
    if retired.is_dir() and not current.is_dir():
        raise RuntimeError(
            f"{retired} is retired; rename it to {current} and migrate legacy `Tool` classes "
            "to McpTool, Specialist, or BooleyFlow"
        )
    return current if current.is_dir() else None


def _file_digest(path: Path) -> str:
    try:
        return hashlib.sha256(path.read_bytes()).hexdigest()
    except FileNotFoundError:
        return "missing"


def project_fingerprint(directory: Path | None) -> tuple:
    """Hash selected Project inputs and layout guards without importing Project code."""
    if directory is None:
        return ()
    root = directory
    selector = (
        ()
        if os.environ.get("BOOLEY_DIR")
        else tuple(
            (str(parent / "booley.toml"), _file_digest(parent / "booley.toml"))
            for parent in (Path.cwd(), *Path.cwd().parents)
            if (parent / "booley.toml").is_file()
        )
    )
    marker_paths = [root / name for name in ("tools", "mcp_tools", "pipeline.toml", "project")]
    marker_paths.append(root.parent / ".booley_project")
    markers = tuple((str(path), path.exists() or path.is_symlink()) for path in marker_paths)
    return (_file_digest(root / "booley.toml"), selector, markers, source_fingerprint(directory))


def source_fingerprint(directory: Path | None) -> tuple:
    """Fingerprint the custom Python input set, including sibling helpers."""
    if directory is None:
        return ()
    return tuple(
        (str(path), _file_digest(path)) for path in sorted((directory / "mcp_tools").glob("*.py"))
    )


def ensure_unique_names(candidates: list[McpToolInfo]) -> None:
    """Reject ambiguous metadata before a path index could discard a duplicate."""
    if len({item.name for item in candidates}) != len(candidates):
        raise ValueError("Duplicate MCP endpoint name in discovery")


class LiveCatalog:
    """One server's selected Project and cached, fully validated generations."""

    def __init__(
        self,
        application: McpApplication,
        endpoints: Definitions,
        *,
        definitions_provider: Callable[[Definitions], Definitions],
        builtin_provider: Callable[[list[Any]], tuple[Definitions, list[str]]],
        visible: Callable[[str], bool],
        on_publish: Callable[[Definitions], None],
    ) -> None:
        self.application: McpApplication = application
        self.directory: Path | None = selected_directory()
        self._definitions_provider = definitions_provider
        self._builtin_provider = builtin_provider
        self._visible = visible
        self._on_publish = on_publish
        self._lock = asyncio.Lock()
        self._published = self.fingerprint()
        self._notified = self._published
        self._builtin_cache = {
            item["name"]: item for item in endpoints if not item.get("is_custom")
        }
        self._custom_cache = [item for item in endpoints if item.get("is_custom")]
        self._custom_key = self._custom_fingerprint()
        self._failed: tuple[object, Exception] | None = None
        self._stable = {
            item.name
            for item in application.list_tools()
            if item.name
            not in {
                endpoint["name"]
                for endpoint in endpoints
                if endpoint.get("is_flow")
                or endpoint.get("is_specialist")
                or endpoint.get("is_custom")
            }
        }

    def fingerprint(self) -> tuple:
        """Hash bytes rather than timestamps, including missing input paths."""
        return project_fingerprint(self.directory)

    def _source_fingerprint(self) -> tuple:
        return source_fingerprint(self.directory)

    def _candidates(self) -> list[Any]:
        if self.directory is None:
            specialists, flows = {}, {}
        else:
            # The running server retains its startup identity, while the resolver
            # still enforces newly introduced retired-layout markers.
            resolve_booley_dir()
            specialists, flows = read_endpoint_config(self.directory / "booley.toml")
        candidates = discover_mcp_tools(
            project_mcp_tools_dir=custom_directory(self.directory),
            specialist_config=specialists,
            flow_config=flows,
        )
        ensure_unique_names(candidates)
        return candidates

    def _custom_fingerprint(self) -> tuple:
        return (
            self._source_fingerprint(),
            tuple(
                (item.name, item.path)
                for item in self._candidates()
                if Path(item.path).is_absolute()
            ),
        )

    def needs_refresh(self, name: str) -> bool:
        """Independent protocol utilities remain usable during failed/slow refresh."""
        return name not in self._stable

    async def refresh(self) -> None:
        """Read current config on every request; replace only complete generations."""
        async with self._lock:
            fingerprint = self.fingerprint()
            candidates = self._candidates()
            if fingerprint != self.fingerprint():
                raise RuntimeError("MCP catalog inputs changed while reading; retry the request")
            if fingerprint == self._published:
                return
            if self._failed is not None and self._failed[0] == fingerprint:
                raise self._failed[1]
            try:
                endpoints = await self._discover(candidates)
                if fingerprint != self.fingerprint():
                    raise RuntimeError(
                        "MCP catalog inputs changed during discovery; retry the request"
                    )
                self.application.replace_tools(self._definitions_provider(endpoints))
            except Exception as exc:
                self._failed = (fingerprint, exc)
                raise
            self._on_publish(endpoints)
            self._published, self._failed = fingerprint, None

    async def _discover(self, candidates: list[Any]) -> Definitions:
        builtins = [item for item in candidates if not Path(item.path).is_absolute()]
        missing = [item for item in builtins if item.name not in self._builtin_cache]
        definitions, errors = self._builtin_provider(missing)
        self._builtin_cache.update((item["name"], item) for item in definitions)
        custom = {
            item.name: Path(item.path) for item in candidates if Path(item.path).is_absolute()
        }
        key = (
            self._source_fingerprint(),
            tuple((name, str(path)) for name, path in custom.items()),
        )
        if key != self._custom_key:
            definitions, custom_errors = await _async_schemas(custom)
            self._custom_cache, self._custom_key = definitions, key
            errors.extend(custom_errors)
        for error in errors:
            logger.warning("MCP discovery: %s", error)
        return [
            self._builtin_cache[item.name] for item in builtins if item.name in self._builtin_cache
        ] + [item for item in self._custom_cache if self._visible(item["name"])]

    async def observe_change(self) -> bool:
        """Track client invalidation independently of request-side publication."""
        fingerprint = self.fingerprint()
        if fingerprint == self._notified:
            return False
        self._notified = fingerprint
        return True


async def _async_schemas(paths: dict[str, Path]) -> tuple[Definitions, list[str]]:
    cancel = threading.Event()
    task = asyncio.create_task(asyncio.to_thread(run_schema_worker, paths, cancel))
    try:
        return await asyncio.shield(task)
    except asyncio.CancelledError:
        cancel.set()
        # AnyIO cancellation is level-triggered; shield the reaping join from
        # the SDK's cancelled task group as well as from asyncio cancellation.
        with anyio.CancelScope(shield=True):
            with contextlib.suppress(RuntimeError, TimeoutError):
                await asyncio.wait_for(asyncio.shield(task), 8)
        raise
