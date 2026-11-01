"""Project-scoped Verilator compiler cache policy and execution preparation."""

from __future__ import annotations

import logging
import shlex
import shutil
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path

from booley.core.boundary import BoundaryError
from booley.flows.sim.config import resolve_compiler_cache_settings
from booley.runtime.compiler_cache import COMPILER_CACHE_ROOT_ENV

logger = logging.getLogger(__name__)
# Build variables Booley manages; Verilator build reuse keeps them out of its key.
_CCACHE = ("CCACHE_DIR", "CCACHE_MAXSIZE", "CCACHE_COMPILERCHECK", "CCACHE_BASEDIR")
RESERVED = frozenset({"OBJCACHE", "USER_CPPFLAGS", COMPILER_CACHE_ROOT_ENV, *_CCACHE})


class CompilerCacheConfigurationError(ValueError):
    """Invalid explicit compiler-cache configuration."""


@dataclass(frozen=True)
class CompilerCachePolicy:
    """Settings plus the issued root (``None`` when unset); resolving has no side effects."""

    enabled: bool
    root: Path | None
    max_size: str

    @property
    def active(self) -> bool:
        """Whether builds should compile through the Project cache."""
        return self.enabled and self.root is not None

    def environment(self) -> dict[str, str]:
        """Managed build variables; inactive policies only clear ``OBJCACHE``."""
        if not self.active:
            return {"OBJCACHE": ""}
        return {
            "OBJCACHE": "ccache",
            "CCACHE_DIR": str(self.root),
            "CCACHE_MAXSIZE": self.max_size,
            "CCACHE_COMPILERCHECK": "content",
        }


def resolve_policy(project_root: Path, environ: Mapping[str, str]) -> CompilerCachePolicy:
    """Read the Project's cache settings and the issued root from *environ*."""
    try:
        enabled, max_size = resolve_compiler_cache_settings(project_root)
    except BoundaryError as exc:
        raise CompilerCacheConfigurationError(str(exc)) from exc
    root = environ.get(COMPILER_CACHE_ROOT_ENV)
    return CompilerCachePolicy(enabled, Path(root) if root else None, max_size)


def compose_environment(
    policy: CompilerCachePolicy,
    target: Mapping[str, str],
    *,
    ambient: Mapping[str, str],
    build_root: Path | None = None,
) -> dict[str, str]:
    """Managed values win over *ambient* and Target values; active builds also map
    the generated build directory so ``-g`` objects hit across build generations."""
    managed = policy.environment()
    if policy.active and build_root is not None:
        generated_root = str(build_root.absolute())
        managed["CCACHE_BASEDIR"] = generated_root
        prefix_map = shlex.quote(f"-fdebug-prefix-map={generated_root}=.").replace("$", "$$")
        user_flags = target.get("USER_CPPFLAGS", ambient.get("USER_CPPFLAGS", ""))
        managed["USER_CPPFLAGS"] = f"{user_flags} {prefix_map}".strip()
    for name in (RESERVED - {"USER_CPPFLAGS"}) & target.keys():
        if target[name] != managed.get(name, ambient.get(name, "")):
            logger.warning("Target compiler-cache setting %s yields to Booley's value", name)
    return {**target, **managed}


def _cache_blocker(policy: CompilerCachePolicy, search_path: str | None) -> str:
    """Return why an enabled cache cannot serve this build, creating its root if it can."""
    if policy.root is None:
        return f"{COMPILER_CACHE_ROOT_ENV} is unset; run booley session refresh on the host"
    if shutil.which("ccache", path=search_path) is None:
        return "ccache is missing from the build PATH"
    try:
        policy.root.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        return f"cannot create cache storage: {exc}"
    return ""


def execution_environment(
    policy: CompilerCachePolicy, environment: Mapping[str, str], *, ambient: Mapping[str, str]
) -> dict[str, str]:
    """Finalize availability only when a build will execute; may create cache storage."""
    result = dict(environment)
    if not policy.enabled:
        return result
    reason = _cache_blocker(policy, {**ambient, **result}.get("PATH"))
    if reason:
        logger.warning("Verilator compiler cache unavailable: %s; compiling uncached", reason)
        result["OBJCACHE"] = ""
    else:
        logger.info("Verilator compiler cache enabled; size target %s", policy.max_size)
    return result
