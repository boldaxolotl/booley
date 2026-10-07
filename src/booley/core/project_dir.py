"""Project-specific data-directory selection.

Runtime lookups route through :func:`resolve_project_dir` with 4-step discovery:
  1. $BOOLEY_PROJECT_DIR env var (CI, Docker, tests)
  2. Walk up from start dir to find booley.toml [project] dir override
  3. Walk up from start dir looking for .booley_project/
  4. Raise with actionable error

Full initialization instead uses :func:`project_dir_for_init` because it owns
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

from booley.core.boundary import as_dict, as_str
from booley.core.checkout_role import require_project_checkout

logger = logging.getLogger(__name__)

# The project data directory's name inside the RTL repo. On the host the project
# dir IS <repo>/.booley_project; inside the Sandbox it is bind-mounted at
# /booley-project, but the same files stay reachable through the workspace mount
# at <repo>/.booley_project. Code that must name a path valid on both sides of
# (e.g. guidance_links) builds it from this.
PROJECT_DIR_NAME = ".booley_project"

_cache: Path | None = None


def project_dir_for_init(project_root: Path) -> Path:
    """Return the checkout-local Project directory owned by full initialization.

    Unlike runtime and seed resolution, full initialization must not inherit an
    ancestor Project, an environment override, or a cached result. The directory
    may not exist yet; this is the path initialization will create.
    """
    return require_project_checkout(project_root) / PROJECT_DIR_NAME


@contextmanager
def init_project_dir_scope(project_root: Path) -> Generator[Path]:
    """Keep every full-init lookup bound to the selected checkout.

    Init calls helpers that use the normal runtime resolver. Temporarily publish
    its checkout-local directory through that resolver's trusted environment
    boundary, then restore the caller's selection and invalidate both cached
    views. ``booley init --seed`` deliberately does not use this scope.
    """
    target = project_dir_for_init(project_root)
    env_name = "BOOLEY_PROJECT_DIR"
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
                    if p.is_dir():
                        return p
            except (OSError, UnicodeDecodeError, tomllib.TOMLDecodeError) as e:
                logger.warning("Failed to read %s: %s", toml_path, e)
            # booley.toml found but no override — fall through
            break
    return None


def resolve_project_dir(start: Path | None = None) -> Path:
    """Resolve the project data directory via 4-step discovery.

    Args:
        start: Directory to start walking up from. Defaults to cwd.
    """
    global _cache
    # An explicit start selects a checkout and must never reinterpret Booley's
    # own source as a Project.  With the implicit cwd, however, an explicit
    # environment override may select a separate Project (CI, Docker, tests,
    # and source-checkout development all rely on that supported boundary).
    current = Path(start or Path.cwd()).resolve()
    if start is not None:
        current = require_project_checkout(current)
    if _cache is not None:
        return _cache

    # 1. Env var override (trusted — CI/Docker/tests set this explicitly)
    env = os.environ.get("BOOLEY_PROJECT_DIR")
    if env:
        p = require_project_checkout(Path(env))
        if not p.is_dir():
            import warnings

            warnings.warn(
                f"BOOLEY_PROJECT_DIR={env!r} does not exist; using anyway",
                stacklevel=2,
            )
        _cache = p
        return _cache

    current = require_project_checkout(current)

    # 2. Walk up to find booley.toml [project] dir override
    toml_result = _resolve_from_toml(current)
    if toml_result is not None:
        _cache = toml_result
        return _cache

    # 3. Walk up from start looking for .booley_project/
    for parent in [current, *current.parents]:
        candidate = parent / ".booley_project"
        if candidate.is_dir():
            _cache = candidate
            return _cache

    raise FileNotFoundError("No .booley_project/ found. Run 'booley init' to set up the project.")


def _resolve_explicit_project_dir(root: Path) -> Path | None:
    """Resolve a configured or co-located Project directory for one root."""
    toml_result = _resolve_from_toml(root)
    if toml_result is not None:
        return toml_result
    local = root / PROJECT_DIR_NAME
    return local if local.is_dir() else None


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


def resolve_checkout_project_dir(project_root: Path) -> Path:
    """Resolve config for one explicitly selected checkout.

    A Booley-created linked worktree carries a local ``.booley_project``
    snapshot. Prefer that snapshot over the session-global environment and
    cache so config and design sources come from the same checkout. Projects
    without a local snapshot retain the normal resolution chain.
    """
    root = require_project_checkout(project_root)
    captured = (_input_projects.get() or {}).get(root.resolve())
    if captured is not None:
        return captured
    explicit = _resolve_explicit_project_dir(root)
    if explicit is not None:
        return explicit
    return resolve_project_dir(root)


def resolve_authoritative_project_dir(project_root: Path) -> Path:
    """Resolve the Project directory owned by an authority-bearing root.

    This resolver never consults ambient Project selection or the process cache.
    Use it only when the caller's explicit root is itself the authority boundary.
    """
    root = require_project_checkout(project_root)
    explicit = _resolve_explicit_project_dir(root)
    if explicit is not None:
        return explicit
    return root / ".booley" / "project"


def checkout_project_dir_relative_to(project_root: Path) -> Path:
    """Return the selected checkout's project directory as a safe relative path."""
    root = project_root.resolve()
    project_dir = resolve_checkout_project_dir(root).resolve()
    try:
        relative = project_dir.relative_to(root)
    except ValueError as exc:
        raise ValueError(f"project directory {project_dir} is outside checkout {root}") from exc
    if relative == Path():
        raise ValueError("project directory cannot be the checkout root")
    return relative


def reset_cache() -> None:
    """Clear cached result — for tests only."""
    global _cache
    _cache = None
