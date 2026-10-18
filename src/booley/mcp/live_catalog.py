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
from booley.mcp.registry import McpToolInfo, discover_mcp_inventory, discover_mcp_tools
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
        return (installed_fingerprint(),)
    root = directory
    selector = (
        ()
        if os.environ.get("BOOLEY_DIR")
        else tuple(
            (directory_identity(parent / "booley.toml"), _file_digest(parent / "booley.toml"))
            for parent in (Path.cwd(), *Path.cwd().parents)
            if (parent / "booley.toml").is_file()
        )
    )
    marker_paths = [root / name for name in ("tools", "mcp_tools", "pipeline.toml", "project")]
    marker_paths.append(root.parent / ".booley_project")
    markers = tuple(directory_identity(path) for path in marker_paths)
    return (
        directory_identity(root),
        _file_digest(root / "booley.toml"),
        selector,
        markers,
        directory_identity(root / "mcp_tools"),
        source_fingerprint(directory),
        installed_fingerprint(),
    )


def directory_identity(path: Path | None) -> tuple:
    """Include lexical selection, symlink target, type and replacement identity."""
    if path is None:
        return ()
    lexical = str(path.absolute())
    resolved = str(path.resolve())
    try:
        link = path.lstat()
    except FileNotFoundError:
        return (lexical, resolved, "missing")
    link_identity = (link.st_dev, link.st_ino, link.st_mode)
    try:
        stat = path.stat()
    except FileNotFoundError:
        return (lexical, resolved, link_identity, "missing-target")
    return (lexical, resolved, link_identity, stat.st_dev, stat.st_ino, stat.st_mode)


def installed_fingerprint() -> tuple:
    """Identify installed metadata inputs by contents, including helper edits."""
    root = Path(__file__).resolve().parent.parent
    paths = sorted(
        path
        for package in ("mcp", "specialists", "flows")
        for path in (root / package).rglob("*.py")
    )
    return (directory_identity(root), tuple((str(path), _file_digest(path)) for path in paths))


def _walk_error(error: OSError) -> None:
    raise error


def source_fingerprint(directory: Path | None) -> tuple:
    """Hash all custom Python inputs within the selected source scope."""
    if directory is None:
        return ()
    root = directory / "mcp_tools"
    if not root.is_dir():
        return (("directory", directory_identity(root)),)
    scope = root.resolve()
    visited: set[Path] = set()
    inputs: list[tuple] = [("directory", directory_identity(root))]
    for current, directories, files in os.walk(root, followlinks=True, onerror=_walk_error):
        parent = Path(current)
        resolved = parent.resolve()
        if resolved in visited:
            directories.clear()
            continue
        visited.add(resolved)
        for name in sorted(directories):
            if name != "__pycache__":
                inputs.append(("directory", directory_identity(parent / name)))
        directories[:] = sorted(
            name
            for name in directories
            if name != "__pycache__" and (parent / name).resolve().is_relative_to(scope)
        )
        inputs.extend(
            (str(parent / name), directory_identity(parent / name), _file_digest(parent / name))
            for name in sorted(files)
            if name.endswith(".py")
        )
    return tuple(inputs)


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
        self._inventory_key: tuple | None = None
        self._inventory: list[McpToolInfo] = []
        self._published = self.fingerprint()
        self._notified = self._published
        self._builtin_cache = {
            item["name"]: item for item in endpoints if not item.get("is_custom")
        }
        self._custom_cache = [item for item in endpoints if item.get("is_custom")]
        self._custom_key = self._custom_fingerprint(self._candidates())
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

    def _candidates(self) -> list[McpToolInfo]:
        if self.directory is not None:
            resolve_booley_dir()
        custom = custom_directory(self.directory)
        project = project_fingerprint(self.directory)
        key = (project[:1], project[2:])
        if key != self._inventory_key:
            inventory = discover_mcp_inventory(project_mcp_tools_dir=custom)
            self._inventory, self._inventory_key = inventory, key
        specialists, flows = (
            ({}, {})
            if self.directory is None
            else read_endpoint_config(
                self.directory / "booley.toml",
                specialist_names={
                    item.name for item in self._inventory if item.kind == "specialist"
                },
            )
        )
        candidates = discover_mcp_tools(
            inventory=self._inventory, specialist_config=specialists, flow_config=flows
        )
        ensure_unique_names(candidates)
        return candidates

    def _custom_fingerprint(self, candidates: list[Any]) -> tuple:
        return (
            self.fingerprint(),
            tuple((item.name, item.path) for item in candidates if Path(item.path).is_absolute()),
        )

    def needs_refresh(self, name: str) -> bool:
        """Independent protocol utilities remain usable during failed/slow refresh."""
        return name not in self._stable

    async def refresh(self) -> None:
        """Read current config on every request; replace only complete generations."""
        async with self._lock:
            fingerprint = self.fingerprint()
            if fingerprint == self._published:
                if self.directory is not None:
                    resolve_booley_dir()
                    custom_directory(self.directory)
                return
            candidates = self._candidates()
            if fingerprint != self.fingerprint():
                raise RuntimeError("MCP catalog inputs changed while reading; retry the request")
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
        key = self._custom_fingerprint(candidates)
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
            # The runner already bounds discovery and process-tree cleanup.
            # A shorter outer timeout could abandon Windows taskkill/reaping.
            with contextlib.suppress(Exception):
                await asyncio.shield(task)
        raise
