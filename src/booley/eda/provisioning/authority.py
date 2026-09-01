"""Secure host authority for EDA installations, licenses, and Project grants."""

from __future__ import annotations

import contextlib
import ipaddress
import json
import os
import re
from collections.abc import Iterator
from dataclasses import asdict, dataclass
from pathlib import Path

from booley.runtime.auth_token import config_dir
from booley.runtime.private_store import PrivateStore

from ..config import installation_name_error
from .policies.vivado import KIND as VIVADO_KIND
from .policies.vivado import POLICY_REVISION as VIVADO_POLICY_REVISION
from .policies.vivado import VivadoPolicyError, inspect_installation

SCHEMA_VERSION = 1
LICENSING_KIND = "xilinx-flexnet"
_STATE_FILENAME = "authority.json"
_LOCK_FILENAME = "authority.lock"
_HOST_ID_RE = re.compile(r"^(?=.{1,253}$)[A-Za-z0-9](?:[A-Za-z0-9.-]*[A-Za-z0-9])?$")
_PROJECT_MARKERS = (".git", ".booley_project")
_BROAD_SYSTEM_ROOT_NAMES = frozenset(
    {
        "bin",
        "boot",
        "dev",
        "etc",
        "home",
        "lib",
        "lib64",
        "media",
        "mnt",
        "opt",
        "proc",
        "root",
        "run",
        "sbin",
        "srv",
        "sys",
        "tmp",
        "usr",
        "var",
    }
)


class AuthorityError(RuntimeError):
    """Private EDA authority is absent, corrupt, insecure, or insufficient."""


class InstallationValidationError(AuthorityError):
    """A registered EDA installation is unavailable or has drifted."""


@dataclass(frozen=True)
class Installation:
    """One host-observed commercial EDA installation."""

    name: str
    kind: str
    source: str
    version: str
    architecture: str
    policy_revision: int


@dataclass(frozen=True)
class LicenseProfile:
    """One fixed Xilinx FlexNet topology (never a license file or secret)."""

    name: str
    kind: str
    server_ipv4: str
    server_hostid: str
    lmgrd_port: int
    vendor_port: int


@dataclass(frozen=True)
class ProjectGrant:
    """Authority for one exact canonical Project and EDA kind."""

    project_root: str
    kind: str
    installation: str | None = None
    license_profile: str | None = None


@dataclass
class AuthorityState:
    """Fully decoded host authority snapshot."""

    installations: dict[str, Installation]
    licenses: dict[str, LicenseProfile]
    grants: tuple[ProjectGrant, ...]


def state_dir() -> Path:
    """Return the private host-only EDA authority directory."""
    return config_dir() / "eda"


def state_path() -> Path:
    """Return the private authority registry path."""
    return state_dir() / _STATE_FILENAME


def _store() -> PrivateStore:
    root = state_dir()
    return PrivateStore(root, config_dir().parent, "EDA authority", AuthorityError)


def ensure_state_dir() -> Path:
    """Create or validate the private authority directory."""
    return _store().ensure_directory()


def load_state(*, allow_missing: bool = True) -> AuthorityState:
    """Load and validate private state, rejecting insecure existing objects."""
    store = _store()
    if not store.validate_existing_directory():
        if allow_missing:
            return _empty_state()
        raise AuthorityError(f"EDA authority directory is missing: {store.root}")
    path = state_path()
    if not path.exists():
        if allow_missing:
            return _empty_state()
        raise AuthorityError(f"EDA authority registry is missing: {path}")
    try:
        raw = store.read_json(_STATE_FILENAME)
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise AuthorityError(f"EDA authority registry is unreadable: {exc}") from exc
    return _decode_state(raw)


def register_installation(name: str, kind: str, source: Path) -> Installation:
    """Inspect and atomically register one built-in EDA installation."""
    _validate_name(name, "installation")
    if kind != VIVADO_KIND:
        raise AuthorityError(f"unsupported EDA installation kind: {kind!r}")
    try:
        observed = inspect_installation(source)
    except VivadoPolicyError as exc:
        raise AuthorityError(str(exc)) from exc
    record = Installation(
        name=name,
        kind=kind,
        source=str(observed.source),
        version=observed.version,
        architecture=observed.architecture,
        policy_revision=VIVADO_POLICY_REVISION,
    )
    with _locked_state() as state:
        if name in state.installations:
            raise AuthorityError(f"EDA installation {name!r} is already registered")
        state.installations[name] = record
    return record


def remove_installation(name: str) -> None:
    """Remove an unused installation, preserving grant referential integrity."""
    with _locked_state() as state:
        if name not in state.installations:
            raise AuthorityError(f"EDA installation {name!r} is not registered")
        if any(grant.installation == name for grant in state.grants):
            raise AuthorityError(f"EDA installation {name!r} is referenced by a live grant")
        del state.installations[name]


def register_license(
    name: str,
    *,
    server_ipv4: str,
    server_hostid: str,
    lmgrd_port: int,
    vendor_port: int,
) -> LicenseProfile:
    """Validate and atomically register one fixed FlexNet topology."""
    _validate_name(name, "license profile")
    profile = _build_license(name, server_ipv4, server_hostid, lmgrd_port, vendor_port)
    with _locked_state() as state:
        if name in state.licenses:
            raise AuthorityError(f"License Profile {name!r} is already registered")
        state.licenses[name] = profile
    return profile


def remove_license(name: str) -> None:
    """Remove an unused License Profile."""
    with _locked_state() as state:
        if name not in state.licenses:
            raise AuthorityError(f"License Profile {name!r} is not registered")
        if any(grant.license_profile == name for grant in state.grants):
            raise AuthorityError(f"License Profile {name!r} is referenced by a live grant")
        del state.licenses[name]


def add_grant(
    project_root: Path,
    kind: str,
    *,
    installation: str | None = None,
    license_profile: str | None = None,
) -> ProjectGrant:
    """Authorize an exact canonical Project root for installation/license use."""
    project = _canonical_project(project_root)
    _validate_new_grant_project(project)
    if kind != VIVADO_KIND:
        raise AuthorityError(f"unsupported EDA grant kind: {kind!r}")
    if installation is None and license_profile is None:
        raise AuthorityError("a grant requires an installation, a License Profile, or both")
    grant = ProjectGrant(str(project), kind, installation, license_profile)
    with _locked_state() as state:
        _validate_grant_refs(grant, state)
        _validate_grant_boundaries(grant, state)
        if installation is not None:
            _revalidate_installation(state.installations[installation], project)
        if any(_grant_key(item) == _grant_key(grant) for item in state.grants):
            raise AuthorityError(f"a {kind} grant already exists for {project}")
        state.grants = (*state.grants, grant)
    invalidate_project_specs(str(project))
    return grant


def revoke_grant(project_root: Path, kind: str) -> ProjectGrant:
    """Remove authority first and invalidate every affected runtime spec."""
    removed: ProjectGrant | None = None
    with _locked_state() as state:
        project = _revoke_project_identity(project_root, kind, state)
        kept = []
        for grant in state.grants:
            if _grant_key(grant) == (project, kind):
                removed = grant
            else:
                kept.append(grant)
        if removed is None:
            raise AuthorityError(f"no {kind} grant exists for {project}")
        state.grants = tuple(kept)
    invalidate_project_specs(removed.project_root)
    _cleanup_revoked_runtime(removed.project_root)
    return removed


def _cleanup_revoked_runtime(project_root: str) -> None:
    """Remove every exact Project runtime before its relay/network topology."""
    from .licensing.flexnet_docker import cleanup_project_resources_for_identity

    residual = cleanup_project_resources_for_identity(project_root)
    if residual:
        raise AuthorityError(
            "EDA authority is revoked, but Session Runtime cleanup left residual objects: "
            + ", ".join(residual)
        )


def resolve_grant(project_root: Path, kind: str) -> ProjectGrant:
    """Resolve the exact canonical Project grant or fail closed."""
    project = _canonical_project(project_root)
    state = load_state(allow_missing=False)
    matches = [grant for grant in state.grants if _grant_key(grant) == (str(project), kind)]
    if len(matches) != 1:
        raise AuthorityError(f"Project {project} has no exact {kind} grant")
    _validate_grant_refs(matches[0], state)
    return matches[0]


def resolve_installation(project_root: Path) -> Installation:
    """Resolve the grant-selected, non-drifted installation for one Project."""
    grant = resolve_grant(project_root, VIVADO_KIND)
    if grant.installation is None:
        raise AuthorityError("Project grant does not authorize a Vivado installation")
    state = load_state(allow_missing=False)
    record = state.installations[grant.installation]
    _revalidate_installation(record, project_root)
    return record


def resolve_license(project_root: Path, kind: str = VIVADO_KIND) -> LicenseProfile | None:
    """Return the host-selected License Profile for a Project, if authorized."""
    grant = resolve_grant(project_root, kind)
    if grant.license_profile is None:
        return None
    return load_state(allow_missing=False).licenses[grant.license_profile]


@contextlib.contextmanager
def resolve_for_issuance(
    project_root: Path,
    host_provisioning: bool,
) -> Iterator[tuple[Installation | None, LicenseProfile | None]]:
    """Resolve one coherent authority snapshot while holding the registry lock.

    The caller must create or validate its stamp before leaving the context.
    That makes a concurrent grant change serialize either wholly before or
    wholly after issuance instead of combining records from different states.
    """
    project = _canonical_project(project_root)
    store = _store()
    store.ensure_directory()
    with store.locked(
        _LOCK_FILENAME,
        busy_message="EDA authority is busy with another operation; retry after it completes",
    ):
        state = load_state()
        matches = [
            grant for grant in state.grants if _grant_key(grant) == (str(project), VIVADO_KIND)
        ]
        if len(matches) > 1:
            raise AuthorityError(f"duplicate {VIVADO_KIND} grants exist for {project}")
        grant = matches[0] if matches else None
        if not host_provisioning and grant is None:
            yield None, None
            return
        if grant is None:
            raise AuthorityError(f"Project {project} has no exact {VIVADO_KIND} grant")
        _validate_grant_refs(grant, state)
        if host_provisioning and grant.installation is None:
            raise AuthorityError(
                "Project requests host-provisioned Vivado, but its grant has no installation"
            )
        if not host_provisioning and grant.installation is not None:
            raise AuthorityError(
                "Project grant authorizes a host Vivado installation, but Project configuration "
                "does not request host provisioning"
            )
        installation = (
            state.installations[grant.installation] if grant.installation is not None else None
        )
        if installation is not None:
            _revalidate_installation(installation, project)
        profile = (
            state.licenses[grant.license_profile] if grant.license_profile is not None else None
        )
        yield installation, profile


def invalidate_project_specs(project_root: str) -> None:
    """Remove host-issued spec stamps for one canonical Project identity."""
    from .runtime_spec import stamp_path_for_identity

    with contextlib.suppress(FileNotFoundError):
        stamp_path_for_identity(project_root).unlink()


def _revalidate_installation(record: Installation, project_root: Path) -> None:
    try:
        observed = inspect_installation(Path(record.source), project_root=project_root)
    except (OSError, VivadoPolicyError) as exc:
        raise InstallationValidationError(
            f"registered Vivado installation failed revalidation: {exc}"
        ) from exc
    identity = (observed.version, observed.architecture, VIVADO_POLICY_REVISION)
    recorded = (record.version, record.architecture, record.policy_revision)
    if identity != recorded:
        raise InstallationValidationError(
            "registered Vivado installation identity or policy has drifted"
        )


def _build_license(
    name: str, address: str, hostid: str, first: int, second: int
) -> LicenseProfile:
    try:
        parsed = ipaddress.ip_address(address)
    except ValueError as exc:
        raise AuthorityError("License Profile server must be a literal IPv4 address") from exc
    forbidden = (
        parsed.is_unspecified
        or parsed.is_multicast
        or parsed.is_loopback
        or parsed.is_link_local
        or parsed.is_reserved
    )
    if not isinstance(parsed, ipaddress.IPv4Address) or forbidden:
        raise AuthorityError("License Profile server must be an IPv4 unicast address")
    if not _HOST_ID_RE.fullmatch(hostid) or hostid.replace(".", "").isdigit():
        raise AuthorityError("License Profile SERVER Host Identifier is invalid")
    if not _valid_port(first) or not _valid_port(second) or first == second:
        raise AuthorityError(
            "License Profile ports must be distinct integers from 1 through 65535"
        )
    return LicenseProfile(name, LICENSING_KIND, str(parsed), hostid, first, second)


def _valid_port(value: object) -> bool:
    return isinstance(value, int) and not isinstance(value, bool) and 1 <= value <= 65535


def _validate_grant_refs(grant: ProjectGrant, state: AuthorityState) -> None:
    if grant.installation is not None:
        record = state.installations.get(grant.installation)
        if record is None or record.kind != grant.kind:
            raise AuthorityError("grant references a missing or mismatched EDA installation")
    if grant.license_profile is not None:
        profile = state.licenses.get(grant.license_profile)
        if profile is None or profile.kind != LICENSING_KIND:
            raise AuthorityError("grant references a missing or mismatched License Profile")


def _validate_grant_boundaries(grant: ProjectGrant, state: AuthorityState) -> None:
    """Reject a Project identity that contains, or is contained by, its EDA source."""
    if grant.installation is None:
        return
    installation = state.installations[grant.installation]
    project = Path(grant.project_root)
    source = Path(installation.source)
    if _paths_overlap(project, source):
        raise AuthorityError("Project grant root overlaps the registered EDA installation source")


def _canonical_project(project_root: Path) -> Path:
    try:
        project = project_root.resolve(strict=True)
    except OSError as exc:
        raise AuthorityError(f"Project root is unavailable: {project_root} ({exc})") from exc
    anchor = Path(project.anchor)
    broad_system_roots = {anchor / name for name in _BROAD_SYSTEM_ROOT_NAMES}
    if (
        not project.is_dir()
        or project in {anchor, Path.home().resolve()}
        or project in broad_system_roots
    ):
        raise AuthorityError(f"unsafe Project root: {project}")
    return project


def _revoke_project_identity(project_root: Path, kind: str, state: AuthorityState) -> str:
    """Select one persisted grant identity without requiring its root to exist."""
    if project_root.is_absolute():
        lexical = os.path.normpath(str(project_root))
        if any(_grant_key(grant) == (lexical, kind) for grant in state.grants):
            return lexical
    if not project_root.exists():
        raise AuthorityError(
            f"cannot match missing Project root {project_root}; use the absolute canonical "
            "path shown by `booley projects`"
        )
    project = _canonical_project(project_root)
    return str(project)


def _validate_new_grant_project(project: Path) -> None:
    """Require an exact Project marker without breaking no-grant lookups."""
    authority_root = state_dir().resolve()
    if _paths_overlap(project, authority_root):
        raise AuthorityError(f"Project root overlaps the private EDA authority: {project}")
    if not any((project / marker).exists() for marker in _PROJECT_MARKERS):
        markers = " or ".join(_PROJECT_MARKERS)
        raise AuthorityError(
            f"not a Booley Project root: {project} has no canonical {markers} marker"
        )


def _paths_overlap(left: Path, right: Path) -> bool:
    return left == right or left in right.parents or right in left.parents


def _validate_name(name: str, label: str) -> None:
    error = installation_name_error(name)
    if error:
        raise AuthorityError(f"{label} name {error}")


def _grant_key(grant: ProjectGrant) -> tuple[str, str]:
    return (grant.project_root, grant.kind)


def _empty_state() -> AuthorityState:
    return AuthorityState({}, {}, ())


def _decode_state(raw: object) -> AuthorityState:
    if not isinstance(raw, dict) or set(raw) != {"schema", "installations", "licenses", "grants"}:
        raise AuthorityError("EDA authority registry has an invalid top-level schema")
    if raw.get("schema") != SCHEMA_VERSION:
        raise AuthorityError("EDA authority registry has an unsupported schema version")
    try:
        installations = {
            name: Installation(**record) for name, record in raw["installations"].items()
        }
        licenses = {name: LicenseProfile(**record) for name, record in raw["licenses"].items()}
        grants = tuple(ProjectGrant(**record) for record in raw["grants"])
    except (AttributeError, KeyError, TypeError) as exc:
        raise AuthorityError(f"EDA authority registry record is corrupt: {exc}") from exc
    state = AuthorityState(installations, licenses, grants)
    _validate_decoded_state(state)
    return state


def _validate_decoded_state(state: AuthorityState) -> None:
    for name, record in state.installations.items():
        if not isinstance(name, str):
            raise AuthorityError("invalid installation record name type")
        _validate_name(name, "installation")
        if (
            record.name != name
            or record.kind != VIVADO_KIND
            or not isinstance(record.source, str)
            or not Path(record.source).is_absolute()
            or not isinstance(record.version, str)
            or record.version != "2025.2"
            or not isinstance(record.architecture, str)
            or record.architecture != "linux-x86_64"
            or not isinstance(record.policy_revision, int)
            or isinstance(record.policy_revision, bool)
            or record.policy_revision != VIVADO_POLICY_REVISION
        ):
            raise AuthorityError(f"invalid installation record: {name!r}")
    for name, profile in state.licenses.items():
        if (
            not isinstance(name, str)
            or not isinstance(profile.name, str)
            or not isinstance(profile.kind, str)
            or not isinstance(profile.server_ipv4, str)
            or not isinstance(profile.server_hostid, str)
        ):
            raise AuthorityError("invalid License Profile record types")
        _validate_name(name, "license profile")
        expected = _build_license(
            name,
            profile.server_ipv4,
            profile.server_hostid,
            profile.lmgrd_port,
            profile.vendor_port,
        )
        if profile.kind != LICENSING_KIND or expected != profile:
            raise AuthorityError(f"invalid License Profile record: {name!r}")
    seen_grants: set[tuple[str, str]] = set()
    for grant in state.grants:
        if (
            not isinstance(grant.project_root, str)
            or not isinstance(grant.kind, str)
            or (grant.installation is not None and not isinstance(grant.installation, str))
            or (grant.license_profile is not None and not isinstance(grant.license_profile, str))
        ):
            raise AuthorityError("invalid Project grant record types")
        key = _grant_key(grant)
        if key in seen_grants:
            raise AuthorityError(f"duplicate grant for {grant.project_root} and {grant.kind}")
        seen_grants.add(key)
        if grant.kind != VIVADO_KIND or not Path(grant.project_root).is_absolute():
            raise AuthorityError("invalid Project grant identity")
        _validate_grant_refs(grant, state)
        _validate_grant_boundaries(grant, state)


def _encode_state(state: AuthorityState) -> str:
    payload = {
        "schema": SCHEMA_VERSION,
        "installations": {
            name: asdict(item) for name, item in sorted(state.installations.items())
        },
        "licenses": {name: asdict(item) for name, item in sorted(state.licenses.items())},
        "grants": [asdict(item) for item in sorted(state.grants, key=_grant_key)],
    }
    return json.dumps(payload, indent=2, sort_keys=True) + "\n"


@contextlib.contextmanager
def _locked_state() -> Iterator[AuthorityState]:
    store = _store()
    store.ensure_directory()
    with store.locked(
        _LOCK_FILENAME,
        busy_message="EDA authority is busy with another operation; retry after it completes",
    ):
        state = load_state()
        yield state
        store.atomic_write_text(_STATE_FILENAME, _encode_state(state))
