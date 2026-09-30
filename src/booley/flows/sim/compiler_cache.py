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
import tomllib
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path

from booley.core.boundary import require_dict
from booley.core.config_paths import resolve_toml
from booley.runtime.compiler_cache import COMPILER_CACHE_RELATIVE, COMPILER_CACHE_ROOT_ENV
from booley.runtime.devcontainer import PROJECT_DIR_TARGET
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


@dataclass(frozen=True)
class CompilerCachePolicy:
    """Resolved policy; resolution does not create storage or invoke tools."""

    enabled: bool
    root: Path
    max_size: str

    def environment(self) -> dict[str, str]:
        return {
            "OBJCACHE": "ccache" if self.enabled else "",
            "CCACHE_DIR": str(self.root),
            "CCACHE_MAXSIZE": self.max_size,
            "CCACHE_COMPILERCHECK": "content",
        }


def _configuration(project_root: Path) -> dict:
    directory = resolve_checkout_project_dir(project_root)
    modern = directory / "booley.toml"
    if modern.is_symlink() and not modern.exists():
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
        values.extend(authored if isinstance(authored, (list, tuple)) else (authored,))
    for value in values:
        raw = str(value)
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


def resolve_policy(project_root: Path, *, owner: Path | None = None) -> CompilerCachePolicy:
    """Read selected-checkout settings and preserve shared operational ownership."""
    config = _configuration(project_root)
    enabled = config.get("enabled", True)
    size = config.get("max_size", "5G")
    if type(enabled) is not bool:
        raise CompilerCacheConfigurationError("flows.sim.compiler_cache.enabled must be a boolean")
    if not isinstance(size, str) or not re.fullmatch(r"[1-9][0-9]*(?:M|G|Mi|Gi)", size):
        raise CompilerCacheConfigurationError(
            "flows.sim.compiler_cache.max_size must be a positive integer with suffix M, G, Mi, or Gi (for example 5G)"
        )
    issued = os.environ.get(COMPILER_CACHE_ROOT_ENV)
    if (
        not issued
        and Path(PROJECT_DIR_TARGET).is_dir()
        and os.environ.get("BOOLEY_CONTAINER") == "1"
    ):
        raise CompilerCacheConfigurationError(
            "Sandbox lacks shared compiler-cache identity; run booley session refresh on the host"
        )
    root = (
        Path(issued)
        if issued
        else (owner or resolve_project_dir(project_root)).resolve() / COMPILER_CACHE_RELATIVE
    )
    if issued and issued != f"{PROJECT_DIR_TARGET}/{COMPILER_CACHE_RELATIVE}":
        raise CompilerCacheConfigurationError(
            "Compiler-cache root differs from the authorized Project mount; refresh the Sandbox"
        )
    return CompilerCachePolicy(enabled, root, size)


def compose_environment(
    policy: CompilerCachePolicy, target: Mapping[str, str], *, build_root: Path | None = None
) -> dict[str, str]:
    """Managed values win over ambient and explicit Target cache values."""
    managed = {
        **policy.environment(),
        COMPILER_CACHE_ROOT_ENV: os.environ.get(COMPILER_CACHE_ROOT_ENV, ""),
    }
    if policy.enabled and build_root is not None:
        generated_root = str(build_root.absolute())
        managed["CCACHE_BASEDIR"] = generated_root
        prefix_map = shlex.quote(f"-fdebug-prefix-map={generated_root}=.").replace("$", "$$")
        user_flags = target.get("USER_CPPFLAGS", os.environ.get("USER_CPPFLAGS", ""))
        managed["USER_CPPFLAGS"] = f"{user_flags} {prefix_map}".strip()
    for name in (RESERVED - {"USER_CPPFLAGS"}) & target.keys():
        if target[name] != managed.get(name, os.environ.get(name, "")):
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
    current = root.parents[2]
    for name in (".runtime", "compiler-cache", "ccache"):
        current = current / name
        with contextlib.suppress(FileExistsError):
            current.mkdir()
        mode = current.lstat()
        if (
            not stat.S_ISDIR(mode.st_mode)
            or getattr(mode, "st_file_attributes", 0) & stat.FILE_ATTRIBUTE_REPARSE_POINT
        ):
            raise OSError("cache storage contains a link or special file")
    import tempfile

    with tempfile.TemporaryFile(dir=root):
        pass


def _prepare_directory_posix(root: Path) -> None:
    # The authorized owner may itself be a supported mount/relocation. Only
    # newly cache-owned children must be no-follow directories.
    descriptor = os.open(root.parents[2], os.O_RDONLY | os.O_DIRECTORY)
    try:
        for name in (".runtime", "compiler-cache", "ccache"):
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
    build_root: Path | None = None,
) -> dict[str, str]:
    """Finalize availability only when a build will execute; never use a home cache."""
    result = dict(environment)
    if policy is None:
        return result
    if not policy.enabled:
        logger.info("Verilator compiler cache disabled")
        return result
    effective = {**os.environ, **result}
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
