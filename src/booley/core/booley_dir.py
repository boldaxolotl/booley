"""Booley Directory selection.

Runtime lookups route through :func:`resolve_booley_dir` with 4-step discovery:
  1. $BOOLEY_DIR env var (CI, Docker, tests)
  2. Walk up from start dir to find booley.toml [project] dir override
  3. Walk up from start dir looking for .booley/
  4. Raise with actionable error

Full initialization instead uses :func:`booley_dir_for_init` because it owns
the prospective directory in the explicitly selected checkout.

Stdlib-only (tomllib). Module-level cache with reset_cache() for tests.
"""

from __future__ import annotations

import logging
import os
import tomllib
from collections.abc import Generator
from contextlib import contextmanager
from contextvars import ContextVar
from pathlib import Path

from booley.core.booley_dir_links import lexical_booley_path
from booley.core.booley_dir_pin import require_pinned_entry
from booley.core.booley_layout import (
    BOOLEY_DIR_NAME,
    require_current_layout,
    validate_environment,
    validate_selection,
)
from booley.core.boundary import as_dict, as_str
from booley.core.checkout_role import require_project_checkout

logger = logging.getLogger(__name__)

_cache: Path | None = None


def booley_dir_for_init(project_root: Path) -> Path:
    """Return the checkout-local Booley Directory owned by full initialization.

    Unlike runtime and seed resolution, full initialization must not inherit an
    ancestor Project, an environment override, or a cached result. The directory
    may not exist yet; this is the path initialization will create.
    """
    validate_environment()
    return require_project_checkout(project_root) / BOOLEY_DIR_NAME


@contextmanager
def init_booley_dir_scope(project_root: Path) -> Generator[Path]:
    """Keep every full-init lookup bound to the selected checkout.

    Init calls helpers that use the normal runtime resolver. Temporarily publish
    its checkout-local directory through that resolver's trusted environment
    boundary, then restore the caller's selection and invalidate both cached
    views. ``booley init --seed`` deliberately does not use this scope.
    """
    target = booley_dir_for_init(project_root)
    env_name = "BOOLEY_DIR"
    had_original = env_name in os.environ
    original = os.environ.get(env_name, "")
    os.environ[env_name] = str(target)
    reset_cache()
    try:
        yield target
    finally:
        if had_original:
            os.environ[env_name] = original
        else:
            os.environ.pop(env_name, None)
        reset_cache()


def _resolve_from_toml(current: Path) -> Path | None:
    """Walk up from *current* looking for booley.toml [project].dir override."""
    for parent in [current, *current.parents]:
        toml_path = parent / "booley.toml"
        if toml_path.is_file():
            try:
                with toml_path.open("rb") as f:
                    cfg = tomllib.load(f)
                project = as_dict(cfg.get("project")) or {}
                dir_val = as_str(project.get("dir"), default="")
                if dir_val and "\x00" not in dir_val:
                    p = Path(dir_val)
                    if not p.is_absolute():
                        p = (parent / p).resolve()
                    validate_selection(p)
                    if p.is_dir():
                        return p
            except (OSError, UnicodeDecodeError, tomllib.TOMLDecodeError) as e:
                logger.warning("Failed to read %s: %s", toml_path, e)
            # booley.toml found but no override — fall through
            break
    return None


def _validate_walkup_layout(current: Path) -> None:
    """Local retired layouts outrank ancestor selections, including warmed caches."""
    for parent in (current, *current.parents):
        require_current_layout(parent)
        if (parent / BOOLEY_DIR_NAME).is_dir() or (parent / "booley.toml").is_file():
            break


def _environment_selection() -> Path | None:
    """Resolve the trusted CI/Docker selection, keeping the existing missing-path warning."""
    env = os.environ.get("BOOLEY_DIR")
    if env:
        validate_selection(Path(env))
        p = require_project_checkout(Path(env))
        if not p.is_dir():
            import warnings

            warnings.warn(
                f"BOOLEY_DIR={env!r} does not exist; using anyway",
                stacklevel=2,
            )
        return p

    return None


def resolve_booley_dir(start: Path | None = None, *, cache_result: bool = True) -> Path:
    """Resolve the Booley Directory via 4-step discovery.

    Args:
        start: Directory to start walking up from. Defaults to cwd.
        cache_result: Cache a newly discovered selection; existing cache reads remain valid.
    """
    global _cache
    validate_environment()
    # An explicit start selects a checkout and must never reinterpret Booley's
    # own source as a Project.  With the implicit cwd, however, an explicit
    # environment override may select a separate Project (CI, Docker, tests,
    # and source-checkout development all rely on that supported boundary).
    current = Path(start or Path.cwd()).resolve()
    if start is not None:
        current = require_project_checkout(current)
    if not os.environ.get("BOOLEY_DIR"):
        current = require_project_checkout(current)
        _validate_walkup_layout(current)
    if _cache is not None:
        validate_selection(_cache)
        return _cache

    explicit = _environment_selection()
    if explicit is not None:
        if cache_result:
            _cache = explicit
        return explicit

    current = require_project_checkout(current)

    # 2. Walk up to find booley.toml [project] dir override
    toml_result = _resolve_from_toml(current)
    if toml_result is not None:
        if cache_result:
            _cache = toml_result
        return toml_result

    # 3. Walk up from start looking for .booley/
    for parent in [current, *current.parents]:
        candidate = parent / ".booley"
        if candidate.is_dir() and (
            candidate != Path.home() / BOOLEY_DIR_NAME
            or any((candidate / name).is_file() for name in ("booley.toml", "pipeline.toml"))
        ):
            require_pinned_entry(parent)
            if cache_result:
                _cache = candidate
            return candidate

    raise FileNotFoundError("No .booley/ found. Run 'booley init' to set up the project.")


def _resolve_explicit_booley_dir(root: Path) -> Path | None:
    """Resolve a configured or co-located Booley Directory for one root."""
    require_current_layout(root)
    toml_result = _resolve_from_toml(root)
    if toml_result is not None:
        return toml_result
    local = root / BOOLEY_DIR_NAME
    if not local.is_dir():
        return None
    require_pinned_entry(root)
    return local


_input_projects: ContextVar[dict[Path, Path] | None] = ContextVar(
    "booley_input_projects", default=None
)


@contextmanager
def committed_project_scope(root: Path, project: Path) -> Generator[None]:
    """Bind an explicit immutable input view without process-global selection changes.

    The materialization owner proves topology and captures every permitted
    non-versioned input before entering this scope. Only this exact root is
    rebound; other callers retain their ordinary checkout resolution.
    """
    token = _input_projects.set(
        {**(_input_projects.get() or {}), root.resolve(): project.resolve()}
    )
    try:
        yield
    finally:
        _input_projects.reset(token)


def resolve_checkout_booley_dir(project_root: Path) -> Path:
    """Resolve config for one explicitly selected checkout.

    A Booley-created linked worktree carries a local ``.booley``
    snapshot. Prefer that snapshot over the session-global environment and
    cache so config and design sources come from the same checkout. Projects
    without a local snapshot retain the normal resolution chain.
    """
    validate_environment()
    root = require_project_checkout(project_root)
    captured = (_input_projects.get() or {}).get(root.resolve())
    if captured is not None:
        return captured
    explicit = _resolve_explicit_booley_dir(root)
    if explicit is not None:
        return explicit
    return resolve_booley_dir(root)


def resolve_authoritative_booley_dir(project_root: Path) -> Path:
    """Resolve the Booley Directory owned by an authority-bearing root.

    This resolver never consults ambient Project selection or the process cache.
    Use it only when the caller's explicit root is itself the authority boundary.
    """
    validate_environment()
    root = require_project_checkout(project_root)
    explicit = _resolve_explicit_booley_dir(root)
    if explicit is not None:
        return explicit
    return root / BOOLEY_DIR_NAME


def checkout_booley_dir_relative_to(project_root: Path) -> Path:
    """Return the selected checkout's Booley Directory as a safe relative path."""
    root = project_root.resolve()
    # A symlinked ``.booley`` entry is reported by its lexical name, not its target.
    booley_dir = lexical_booley_path(
        root / BOOLEY_DIR_NAME, resolve_checkout_booley_dir(root).resolve()
    )
    try:
        relative = booley_dir.relative_to(root)
    except ValueError as exc:
        raise ValueError(f"Booley Directory {booley_dir} is outside checkout {root}") from exc
    if relative == Path():
        raise ValueError("Booley Directory cannot be the checkout root")
    return relative


def reset_cache() -> None:
    """Clear cached result — for tests only."""
    global _cache
    _cache = None
