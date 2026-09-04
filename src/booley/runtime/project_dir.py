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
from pathlib import Path

from booley.runtime.checkout_role import require_project_checkout

logger = logging.getLogger(__name__)

# The project data directory's name inside the RTL repo. On the host the project
# dir IS <repo>/.booley_project; inside the Session Runtime it is bind-mounted at
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
                dir_val = cfg.get("project", {}).get("dir", "")
                if dir_val:
                    p = Path(dir_val)
                    if not p.is_absolute():
                        p = (parent / p).resolve()
                    if p.is_dir():
                        return p
            except (OSError, tomllib.TOMLDecodeError) as e:
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


def resolve_checkout_project_dir(project_root: Path) -> Path:
    """Resolve config for one explicitly selected checkout.

    A Booley-created linked worktree carries a local ``.booley_project``
    snapshot. Prefer that snapshot over the session-global environment and
    cache so config and design sources come from the same checkout. Projects
    without a local snapshot retain the normal resolution chain.
    """
    root = require_project_checkout(project_root)
    toml_result = _resolve_from_toml(root)
    if toml_result is not None:
        return toml_result
    local = root / PROJECT_DIR_NAME
    if local.is_dir():
        return local
    return resolve_project_dir(root)


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


def runtime_dir(start: Path | None = None) -> Path:
    """Return the transient runtime tree ``<project_dir>/.runtime``.

    Single source of truth for the git-ignored ``.runtime`` tree that holds
    generated state, locks, and Flow/MCP-tool reports. Code that resolves this tree
    against the *project data dir* should route through here instead of
    hand-joining the bare ``.runtime`` literal, so the location lives in one
    place. (Worktree-relative constructions that must cross into the sandbox at
    ``/work`` — e.g. the edalize work dirs — deliberately stay off this helper;
    they are keyed on an explicit worktree root, not cwd discovery.)

    Creates the directory (``parents=True, exist_ok=True``) as a side effect.

    Args:
        start: Directory to start walking up from. Defaults to cwd.
    """
    d = resolve_project_dir(start) / ".runtime"
    d.mkdir(parents=True, exist_ok=True)
    return d


def reset_cache() -> None:
    """Clear cached result — for tests only."""
    global _cache
    _cache = None
