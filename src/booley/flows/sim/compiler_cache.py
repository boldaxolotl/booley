"""Project-scoped Verilator compiler cache policy and execution preparation."""

from __future__ import annotations

import contextlib
import logging
import os
import re
import secrets
import shlex
import shutil
import stat
import tempfile
import tomllib
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path

from booley.core.boundary import as_str_list, require_dict
from booley.core.config_paths import resolve_toml
from booley.runtime.compiler_cache import (
    COMPILER_CACHE_RELATIVE,
    COMPILER_CACHE_ROOT_ENV,
    COMPILER_CACHE_SEGMENTS,
    ISSUED_COMPILER_CACHE_ROOT,
    IssuedCacheIdentity,
)
from booley.runtime.project_dir import resolve_checkout_project_dir, resolve_project_dir
from booley.targets.domain import TargetInspection

logger = logging.getLogger(__name__)
RESERVED = frozenset(
    {
        "OBJCACHE",
        "CCACHE_DIR",
        "CCACHE_MAXSIZE",
        "CCACHE_COMPILERCHECK",
        "CCACHE_BASEDIR",
        "USER_CPPFLAGS",
        COMPILER_CACHE_ROOT_ENV,
    }
)
_ASSIGNMENT = re.compile(
    r"^[ \t]*(?:(?:override|export|private)\s+)*("
    + "|".join(sorted(RESERVED))
    + r")\s*(?:[:]{1,3}=|\?=|\+=|!=|=)",
    re.MULTILINE,
)
_EVAL_VARIABLE = re.compile(r"(?<![\w])(" + "|".join(sorted(RESERVED)) + r")(?![\w])")
_SHORT_EVAL = re.compile(r"^-[bBdeiknpqrRsStvw]*E(.*)$")


class CompilerCacheConfigurationError(ValueError):
    """Invalid explicit compiler-cache configuration or Make override."""


_MISSING_SANDBOX_IDENTITY = (
    "Sandbox lacks the shared compiler-cache identity; run booley session refresh on the host"
)
_FOREIGN_ISSUED_ROOT = (
    "compiler-cache root differs from the authorized Project mount; refresh the Sandbox"
)


@dataclass(frozen=True)
class CompilerCachePolicy:
    """Resolved policy; resolution does not create storage or invoke tools.

    ``root`` is ``None`` when no trustworthy shared location exists (for
    example, a Sandbox issued before the cache); ``unavailable_reason`` then
    says why, and builds compile uncached.
    """

    enabled: bool
    root: Path | None
    max_size: str
    issued_root: str = ""
    unavailable_reason: str = ""

    @property
    def active(self) -> bool:
        """Whether builds should compile through the Project cache."""
        return self.enabled and self.root is not None

    def environment(self) -> dict[str, str]:
        """Managed build variables; inactive policies only clear ``OBJCACHE``."""
        managed = {"OBJCACHE": "", COMPILER_CACHE_ROOT_ENV: self.issued_root}
        if self.active:
            managed.update(
                {
                    "OBJCACHE": "ccache",
                    "CCACHE_DIR": str(self.root),
                    "CCACHE_MAXSIZE": self.max_size,
                    "CCACHE_COMPILERCHECK": "content",
                }
            )
        return managed


def _read_compiler_cache_section(project_root: Path) -> dict:
    """Return the selected checkout's ``[flows.sim.compiler_cache]`` table."""
    directory = resolve_checkout_project_dir(project_root)
    booley_toml = directory / "booley.toml"
    if booley_toml.is_symlink() and not booley_toml.exists():
        raise CompilerCacheConfigurationError(
            "Selected booley.toml is a broken link; repair the configuration"
        )
    path = resolve_toml(directory)
    if path.is_symlink() and not path.exists():
        raise CompilerCacheConfigurationError(
            "Selected compiler-cache config is a broken link; repair the configuration"
        )
    try:
        with path.open("rb") as stream:
            config = tomllib.load(stream)
    except FileNotFoundError:
        return {}
    except (OSError, tomllib.TOMLDecodeError) as exc:
        raise CompilerCacheConfigurationError(
            "Cannot read selected compiler-cache configuration; repair booley.toml/pipeline.toml"
        ) from exc
    try:
        for name in ("flows", "sim", "compiler_cache"):
            config = require_dict(config.get(name, {}), field=name)
    except ValueError as exc:
        raise CompilerCacheConfigurationError(str(exc)) from exc
    return config


def validate_make_assignments(
    inspection: TargetInspection, environment: Mapping[str, str]
) -> None:
    """Reject Make precedence escapes without altering unrelated Make options."""
    values = [
        environment.get(name, "")
        for name in ("MAKEFLAGS", "GNUMAKEFLAGS", "MAKEOVERRIDES", "MFLAGS")
    ]
    for options in (inspection.flow_options, inspection.tool_options):
        authored = options.get("make_options", ())
        values.extend(as_str_list(list(authored) if isinstance(authored, tuple) else authored))
    for raw in values:
        try:
            tokens = shlex.split(raw)
        except ValueError:
            tokens = [raw]
        for index, token in enumerate(tokens):
            is_eval = True
            option, separator, argument = token.partition("=")
            short_eval = _SHORT_EVAL.match(token)
            if option in {"--ev", "--eva", "--eval"} and separator:
                expression = argument
            elif token in {"--ev", "--eva", "--eval", "-E"}:
                expression = tokens[index + 1] if index + 1 < len(tokens) else ""
            elif short_eval:
                expression = short_eval[1] or (
                    tokens[index + 1] if index + 1 < len(tokens) else ""
                )
            elif token in RESERVED:
                expression = " ".join(tokens[index:])
                is_eval = False
            else:
                expression = token
                is_eval = False
            # Eval accepts arbitrary Make programs, including computed names and
            # nested eval/define. Do not attempt to interpret that language.
            match = (_EVAL_VARIABLE if is_eval else _ASSIGNMENT).search(expression)
            if match:
                raise CompilerCacheConfigurationError(
                    f"{match[1]} is managed by Booley; remove its Make assignment and configure flows.sim.compiler_cache instead"
                )


def resolve_policy(
    project_root: Path, *, issued: IssuedCacheIdentity, owner: Path | None = None
) -> CompilerCachePolicy:
    """Read selected-checkout settings and preserve shared operational ownership.

    Settings errors fail preparation. A missing or foreign issued identity
    only makes the policy inactive: the build still runs, uncached.
    """
    enabled, size = _validated_settings(project_root)
    if issued.root is None:
        if issued.in_sandbox:
            return CompilerCachePolicy(
                enabled, None, size, unavailable_reason=_MISSING_SANDBOX_IDENTITY
            )
        owner_root = (owner or resolve_project_dir(project_root)).resolve()
        return CompilerCachePolicy(enabled, owner_root / COMPILER_CACHE_RELATIVE, size)
    if issued.root != ISSUED_COMPILER_CACHE_ROOT:
        return CompilerCachePolicy(
            enabled, None, size, issued_root=issued.root, unavailable_reason=_FOREIGN_ISSUED_ROOT
        )
    return CompilerCachePolicy(enabled, Path(issued.root), size, issued_root=issued.root)


def _validated_settings(project_root: Path) -> tuple[bool, str]:
    """Return ``(enabled, max_size)`` from strict checkout-local configuration."""
    config = _read_compiler_cache_section(project_root)
    enabled = config.get("enabled", True)
    size = config.get("max_size", "5G")
    if type(enabled) is not bool:
        raise CompilerCacheConfigurationError("flows.sim.compiler_cache.enabled must be a boolean")
    if not isinstance(size, str) or not re.fullmatch(r"[1-9][0-9]*(?:M|G|Mi|Gi)", size):
        raise CompilerCacheConfigurationError(
            "flows.sim.compiler_cache.max_size must be a positive integer with suffix M, G, Mi, or Gi (for example 5G)"
        )
    return enabled, size


def compose_environment(
    policy: CompilerCachePolicy,
    target: Mapping[str, str],
    *,
    ambient: Mapping[str, str],
    build_root: Path | None = None,
) -> dict[str, str]:
    """Managed values win over *ambient* and explicit Target cache values.

    Active builds also get ``CCACHE_BASEDIR`` and a ``-fdebug-prefix-map`` for
    the generated build directory, so objects compiled with ``-g`` hit across
    build generations; without ``-g`` the map has no effect.
    """
    managed = policy.environment()
    if policy.active and build_root is not None:
        generated_root = str(build_root.absolute())
        managed["CCACHE_BASEDIR"] = generated_root
        prefix_map = shlex.quote(f"-fdebug-prefix-map={generated_root}=.").replace("$", "$$")
        user_flags = target.get("USER_CPPFLAGS", ambient.get("USER_CPPFLAGS", ""))
        managed["USER_CPPFLAGS"] = f"{user_flags} {prefix_map}".strip()
    for name in (RESERVED - {"USER_CPPFLAGS"}) & target.keys():
        if target[name] != managed.get(name, ambient.get(name, "")):
            logger.warning(
                "Target compiler-cache setting %s conflicts with Booley policy; using managed setting",
                name,
            )
    return {**target, **managed}


def _prepare_directory(root: Path) -> None:
    """Create children without following cache-owned ancestor links."""
    if os.name == "posix":
        _prepare_directory_posix(root)
        return
    current = _cache_owner(root)
    for name in COMPILER_CACHE_SEGMENTS:
        current = current / name
        with contextlib.suppress(FileExistsError):
            current.mkdir()
        mode = current.lstat()
        if (
            not stat.S_ISDIR(mode.st_mode)
            or getattr(mode, "st_file_attributes", 0) & stat.FILE_ATTRIBUTE_REPARSE_POINT
        ):
            raise OSError("cache storage contains a link or special file")
    with tempfile.TemporaryFile(dir=root):
        pass


def _cache_owner(root: Path) -> Path:
    """Return the Project-data owner that *root* (``owner/<segments>``) lives under."""
    return root.parents[len(COMPILER_CACHE_SEGMENTS) - 1]


def _prepare_directory_posix(root: Path) -> None:
    # The authorized owner may itself be a supported mount/relocation. Only
    # newly cache-owned children must be no-follow directories.
    descriptor = os.open(_cache_owner(root), os.O_RDONLY | os.O_DIRECTORY)
    try:
        for name in COMPILER_CACHE_SEGMENTS:
            with contextlib.suppress(FileExistsError):
                os.mkdir(name, dir_fd=descriptor)
            child = os.open(name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=descriptor)
            os.close(descriptor)
            descriptor = child
        probe = ".probe-" + secrets.token_hex(16)
        test_fd = os.open(
            probe, os.O_CREAT | os.O_EXCL | os.O_WRONLY | os.O_NOFOLLOW, 0o600, dir_fd=descriptor
        )
        os.close(test_fd)
        os.unlink(probe, dir_fd=descriptor)
    finally:
        os.close(descriptor)


def execution_environment(
    policy: CompilerCachePolicy | None,
    environment: Mapping[str, str],
    *,
    ambient: Mapping[str, str],
    build_root: Path | None = None,
) -> dict[str, str]:
    """Finalize availability only when a build will execute; never use a home cache."""
    result = dict(environment)
    if policy is None:
        return result
    if not policy.enabled:
        logger.info("Verilator compiler cache disabled")
        return result
    if policy.root is None:
        logger.warning(
            "Verilator compiler cache unavailable: %s; compiling uncached",
            policy.unavailable_reason,
        )
        result["OBJCACHE"] = ""
        return result
    effective = {**ambient, **result}
    reason = "ccache is missing from the build PATH"
    try:
        compiler_directory = build_root or Path.cwd()
        search_path = os.pathsep.join(
            str(compiler_directory / entry) if not Path(entry).is_absolute() else entry
            for entry in effective.get("PATH", os.defpath).split(os.pathsep)
        )
        if shutil.which("ccache", path=search_path) is None:
            raise OSError(reason)
        reason = "Project cache storage is inaccessible or contains a link/special file"
        _prepare_directory(policy.root)
    except OSError:
        logger.warning(
            "Verilator compiler cache unavailable: %s; compiling uncached (size target %s)",
            reason,
            policy.max_size,
        )
        result["OBJCACHE"] = ""
    else:
        logger.info("Verilator compiler cache enabled; size target %s", policy.max_size)
    return result
