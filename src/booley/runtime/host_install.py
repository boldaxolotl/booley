"""Identity and authority for the one canonical Booley host installation."""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import sys
import tempfile
from dataclasses import asdict, dataclass
from pathlib import Path
from uuid import uuid4

from booley.core.boundary import BoundaryError, require_dict, require_int, require_str
from booley.core.user_paths import config_dir
from booley.runtime.build_metadata import current_build_metadata

_SCHEMA_VERSION = 1
_STATE_FILENAME = "host-installation.json"
_INSTALLED_PACKAGE_DIRS = {"site-packages", "dist-packages"}
_EPHEMERAL_PARTS = {".runtime", ".worktrees", "qa-runs"}


class HostInstallationError(RuntimeError):
    """The canonical host-installation identity is absent, invalid, or different."""


@dataclass(frozen=True, slots=True)
class HostInstallationIdentity:
    """Persisted identity of the wheel allowed to mutate host-owned state."""

    schema_version: int
    executable: str
    interpreter: str
    distribution_root: str
    version: str
    revision: str
    payload_fingerprint: str


def host_installation_path() -> Path:
    """Return the machine-maintained identity path, separate from host policy."""
    return config_dir() / _STATE_FILENAME


def _resolved_executable() -> Path:
    raw = Path(sys.argv[0])
    located = shutil.which(str(raw)) if not raw.is_absolute() else str(raw)
    return Path(located or raw).resolve()


def _tree_fingerprint(root: Path) -> str:
    digest = hashlib.sha256()
    for child in sorted(root.rglob("*"), key=lambda item: item.relative_to(root).as_posix()):
        if not child.is_file() or "__pycache__" in child.parts or child.suffix == ".pyc":
            continue
        relative = child.relative_to(root).as_posix().encode()
        digest.update(relative + b"\0" + hashlib.sha256(child.read_bytes()).digest())
    return digest.hexdigest()


def current_host_installation(package_resource: Path) -> HostInstallationIdentity:
    """Describe the installed Booley distribution supplying *package_resource*."""
    import booley

    package_root = Path(booley.__file__).resolve().parent
    resolved_resource = package_resource.resolve()
    try:
        resolved_resource.relative_to(package_root)
    except ValueError as exc:
        raise HostInstallationError(
            f"package resource is not owned by the imported Booley distribution: "
            f"{resolved_resource}"
        ) from exc
    metadata = current_build_metadata()
    fingerprint = metadata.payload_fingerprint or _tree_fingerprint(package_root)
    return HostInstallationIdentity(
        schema_version=_SCHEMA_VERSION,
        executable=str(_resolved_executable()),
        interpreter=str(Path(sys.executable).resolve()),
        distribution_root=str(package_root.parent),
        version=metadata.version,
        revision=metadata.revision,
        payload_fingerprint=fingerprint,
    )


def _eligibility_error(
    package_resource: Path,
    *,
    prefix: Path | None = None,
    base_prefix: Path | None = None,
) -> str | None:
    active_prefix = Path(sys.prefix) if prefix is None else prefix
    interpreter_prefix = Path(sys.base_prefix) if base_prefix is None else base_prefix
    if active_prefix.resolve() != interpreter_prefix.resolve():
        return (
            "Booley is running from a virtual environment; machine-global resources "
            "may only be managed by the canonical host-installed wheel"
        )
    resolved = package_resource.resolve()
    temporary_root = Path(tempfile.gettempdir()).resolve()
    if resolved == temporary_root or resolved.is_relative_to(temporary_root):
        return (
            f"Booley package resource is inside temporary filesystem state: {resolved}; "
            "temporary QA installations cannot manage machine-global resources"
        )
    if not any(part in _INSTALLED_PACKAGE_DIRS for part in resolved.parts):
        return f"Booley package resource is not from an installed wheel: {resolved}"
    if _EPHEMERAL_PARTS.intersection(resolved.parts):
        return (
            f"Booley package resource is inside ephemeral workspace state: {resolved}; "
            "temporary worktrees and QA runs cannot manage machine-global resources"
        )
    return None


def _parse_identity(raw: object) -> HostInstallationIdentity:
    values = require_dict(raw, field="host installation identity")
    schema = require_int(values.get("schema_version"), field="schema_version")
    if schema != _SCHEMA_VERSION:
        raise BoundaryError(f"unsupported host installation schema {schema}")
    return HostInstallationIdentity(
        schema_version=schema,
        executable=require_str(values, "executable"),
        interpreter=require_str(values, "interpreter"),
        distribution_root=require_str(values, "distribution_root"),
        version=require_str(values, "version"),
        revision=require_str(values, "revision"),
        payload_fingerprint=require_str(values, "payload_fingerprint"),
    )


def load_host_installation(path: Path | None = None) -> HostInstallationIdentity:
    """Load and validate the canonical host-installation identity."""
    source = path or host_installation_path()
    try:
        return _parse_identity(json.loads(source.read_text(encoding="utf-8")))
    except FileNotFoundError as exc:
        raise HostInstallationError(
            "canonical host installation is not recorded; run `booley bootstrap` "
            "from the intended installed wheel"
        ) from exc
    except (BoundaryError, json.JSONDecodeError, OSError, UnicodeError) as exc:
        raise HostInstallationError(
            f"cannot read host installation identity {source}: {exc}"
        ) from exc


def _write_identity(identity: HostInstallationIdentity, path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    temporary = path.with_name(f".{path.name}.{uuid4().hex}.tmp")
    content = json.dumps(asdict(identity), indent=2, sort_keys=True) + "\n"
    try:
        temporary.write_text(content, encoding="utf-8")
        if os.name != "nt":
            temporary.chmod(0o600)
        temporary.replace(path)
    finally:
        temporary.unlink(missing_ok=True)


def register_host_installation(
    package_resource: Path,
    *,
    update: bool = False,
    path: Path | None = None,
) -> HostInstallationIdentity:
    """Record this eligible wheel as canonical, refusing implicit updates."""
    if error := _eligibility_error(package_resource):
        raise HostInstallationError(error)
    destination = path or host_installation_path()
    candidate = current_host_installation(package_resource)
    if destination.exists() and not update:
        current = load_host_installation(destination)
        if current != candidate:
            raise HostInstallationError(
                "a different canonical host installation is already recorded; run "
                "`booley bootstrap --update` after an intentional upgrade"
            )
        return current
    _write_identity(candidate, destination)
    return candidate


def host_install_error(
    package_resource: Path,
    *,
    prefix: Path | None = None,
    base_prefix: Path | None = None,
    path: Path | None = None,
) -> str | None:
    """Explain why this process cannot mutate Booley host-owned state."""
    if error := _eligibility_error(package_resource, prefix=prefix, base_prefix=base_prefix):
        return error + "; run the canonical host `booley bootstrap`"
    try:
        expected = load_host_installation(path)
        actual = current_host_installation(package_resource)
    except HostInstallationError as exc:
        return str(exc)
    if actual != expected:
        return (
            "this Booley process does not match the canonical host installation "
            f"({actual.distribution_root} != {expected.distribution_root}); run the canonical "
            "host `booley bootstrap --update` if this upgrade is intentional"
        )
    return None
