"""Host-wide admission control for Interactive Mode Sandboxes."""

from __future__ import annotations

import hashlib
import json
import logging
import os
import shlex
import subprocess
from collections.abc import Callable
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from booley.config.host_config import (
    HOST_POLICY_MIGRATION_GUIDANCE,
    HostConfigError,
    SandboxHostPolicy,
    host_config_path,
    load_host_policy,
)
from booley.core.boundary import (
    BoundaryError,
    require_bool_value,
    require_dict,
    require_list,
    require_str,
    require_str_value,
)
from booley.core.private_store import PrivateStore
from booley.core.project_identity import matches_project_label
from booley.core.user_paths import config_dir
from booley.runtime import devcontainer as dc
from booley.runtime.platform_paths import host_path_from_docker_mount
from booley.runtime.timefmt import format_age, parse_timestamp, rfc3339_from_datetime

Run = Callable[..., subprocess.CompletedProcess[str]]
_SCHEMA_VERSION = 2
_CLAIM_SUFFIX = ".json"
_FOLDER_LABEL = "devcontainer.local_folder"
_PROJECT_LABEL = "booley.project-id"
_PROJECT_LISTING_FORMAT = (
    '{{.ID}}\t{{.Names}}\t{{.Label "devcontainer.local_folder"}}'
    '\t{{.Label "booley.project-id"}}\t{{.Label "booley.role"}}'
)
# Roles a Project Sandbox (or its licensed relay) can carry. Any other role
# names a Booley host sidecar such as ``reaper`` or ``egress-proxy``.
_SANDBOX_ROLES = frozenset({"interactive", "license-relay"})
logger = logging.getLogger(__name__)


class AdmissionError(RuntimeError):
    """Sandbox capacity cannot be determined or no slot is available."""


@dataclass(frozen=True, slots=True)
class PendingStartClaim:
    """One VS Code Sandbox start admitted before Docker creates it."""

    schema_version: int
    project_root: str
    claimed_at: str
    baseline: dict[str, str | None]


@dataclass(frozen=True, slots=True)
class Sandbox:
    """Strictly inspected Booley Sandbox state."""

    container_id: str
    name: str
    project_root: str
    project_id: str | None
    created_at: datetime
    started_at: datetime | None
    running: bool
    vscode: bool
    observed_root: str | None = None


def _run(argv: list[str], *, timeout: int = 30) -> subprocess.CompletedProcess[str]:
    return subprocess.run(argv, capture_output=True, text=True, check=False, timeout=timeout)


def admit_start(
    project_root: Path,
    *,
    target_name: str,
    recovery: bool = False,
    run: Run = _run,
    now: datetime | None = None,
) -> None:
    """Admit a slot-consuming start or raise an actionable refusal."""
    if recovery:
        _report_recovery_capacity(run)
        return
    root = _canonical_root(project_root)
    live, claims, _candidates, diagnostics = _admission_snapshot(run, requesting_root=root)
    if any(item.name == target_name for item in live):
        return
    policy = _policy()
    if len(live) + len(claims) >= policy.max_sessions:
        raise AdmissionError(_refusal(policy, live, claims, now or datetime.now(UTC), diagnostics))
    # A headless start must not consume its Project's editor reservation.
    if any(_same_project_root(claim.project_root, root) for claim in claims):
        raise AdmissionError(_refusal(policy, live, claims, now or datetime.now(UTC), diagnostics))


def claim_vscode_start(
    project_root: Path,
    *,
    run: Run = _run,
    now: datetime | None = None,
) -> bool:
    """Atomically reserve capacity for a later VS Code Docker start."""
    root = _canonical_root(project_root)
    live, claims, candidates, diagnostics = _admission_snapshot(
        run, requesting_root=root, candidate_roots={root}
    )
    project_sandboxes = _project_vscode_sandboxes(root, candidates)
    if any(item.running for item in project_sandboxes):
        return False
    if any(_same_project_root(claim.project_root, root) for claim in claims):
        return False
    policy = _policy()
    instant = now or datetime.now(UTC)
    if len(live) + len(claims) >= policy.max_sessions:
        raise AdmissionError(_refusal(policy, live, claims, instant, diagnostics))
    baseline = {item.container_id: _started_token(item) for item in project_sandboxes}
    claim = PendingStartClaim(
        _SCHEMA_VERSION,
        root,
        rfc3339_from_datetime(instant, microseconds=True),
        baseline,
    )
    store = _store()
    store.ensure_directory()
    store.atomic_write_text(_claim_filename(root), json.dumps(asdict(claim), sort_keys=True))
    return True


def clear_vscode_claim(project_root: Path) -> bool:
    """Remove this Project's unconsumed VS Code capacity claim."""
    root = _canonical_root(project_root)
    path = _store().root / _claim_filename(root)
    existed = path.exists()
    if existed:
        _load_claim(path)
        path.unlink()
    return existed


def has_pending_claim(project_root: Path, *, run: Run = _run) -> bool:
    """Return whether this Project has a validated pending editor start."""
    root = _canonical_root(project_root)
    claims = tuple(claim for claim in _claims() if _same_project_root(claim.project_root, root))
    if not claims:
        return False
    candidates = _vscode_inventory({root}, run)
    _project_vscode_sandboxes(root, candidates)
    remaining, _diagnostics = _reconcile_claims(claims, candidates, root)
    return bool(remaining)


def vscode_sandboxes(project_root: Path, *, run: Run = _run) -> tuple[Sandbox, ...]:
    """Return strictly identified VS Code Sandboxes for one Project."""
    root = _canonical_root(project_root)
    return _project_vscode_sandboxes(root, _vscode_inventory({root}, run))


def running_project_sandboxes(project_root: Path, *, run: Run = _run) -> tuple[Sandbox, ...]:
    """Read one Project's running inventory without touching admission claims.

    Listing hints exclude only positively identified foreign Projects. Every
    possibly relevant object is inspected, so uncertainty never means absence.
    """
    from booley.runtime.session_runtime import session_container_name

    root = _canonical_root(project_root)
    local_name = session_container_name(project_root)
    command = ["docker", "ps", "--no-trunc", "--format", _PROJECT_LISTING_FORMAT]
    try:
        result = run(command, timeout=30)
        if result.returncode:
            raise AdmissionError("cannot enumerate Project Sandboxes safely: " + _detail(result))
        items = {}
        for line in result.stdout.splitlines():
            fields = line.split("\t")
            if len(fields) != 5 or not fields[0] or not fields[1]:
                raise AdmissionError("Docker returned an incomplete Project Sandbox listing")
            container_id, name, folder, project_id, role = fields
            if not (folder or project_id or role or name == local_name):
                continue
            if _host_sidecar_listing(folder, project_id, role, name, local_name):
                continue
            if _foreign_listing(folder, project_id, name, root, local_name):
                continue
            if (
                name != local_name
                and role == "license-relay"
                and _noninteractive_relay(container_id, name, run)
            ):
                continue
            item = _inspect(container_id, name, run, exact_id=True)
            if not _valid_identity(item, item.project_root):
                raise AdmissionError(_identity_recovery(item, root))
            local_root = _same_project_root(item.project_root, root)
            local_label = matches_project_label(item.project_id, root) or (
                local_root and _valid_identity(item, root)
            )
            if local_root != local_label or (name == local_name and not local_root):
                raise AdmissionError(_identity_recovery(item, root))
            if local_root and item.running:
                _verify_workspace_mount(item, root, run)
                items[item.container_id] = item
        return tuple(items.values())
    except (OSError, subprocess.SubprocessError) as exc:
        raise AdmissionError(f"cannot inspect Project Sandboxes safely: {exc}") from exc


def _noninteractive_relay(container_id: str, name: str, run: Run) -> bool:
    """Exclude a relay only after proving it has no interactive workspace bind."""
    result = run(
        ["docker", "container", "inspect", container_id, "--format", "{{json .}}"], timeout=30
    )
    try:
        if result.returncode:
            raise AdmissionError(f"cannot inspect possible Sandbox {name}: {_detail(result)}")
        document = require_dict(json.loads(result.stdout), field="possible Sandbox")
        labels = _labels(require_dict(document.get("Config"), field="Config").get("Labels"))
        mounts = require_list(document.get("Mounts"), field="Mounts")
        return (
            require_str(document, "Id") == container_id
            and require_str(document, "Name").removeprefix("/") == name
            and labels.get("booley.role") == "license-relay"
            and _FOLDER_LABEL not in labels
            and not any(
                require_dict(mount, field="Mount").get("Destination") == "/work"
                for mount in mounts
            )
        )
    except (BoundaryError, ValueError) as exc:
        raise AdmissionError(f"cannot inspect possible Sandbox {name}") from exc


def _host_sidecar_listing(
    folder: str, project_id: str, role: str, name: str, local_name: str
) -> bool:
    """Skip a Booley host sidecar that carries no Sandbox identity at all.

    The reaper and egress proxy have no workspace mount, so inspecting them as
    Sandboxes would abort the whole inventory. Rows with an empty or Sandbox
    role, any Project label, or the local Sandbox name stay fail-closed.
    """
    return (
        bool(role)
        and role not in _SANDBOX_ROLES
        and not folder
        and not project_id
        and name != local_name
    )


def _foreign_listing(folder: str, project_id: str, name: str, root: str, local_name: str) -> bool:
    if not folder or not project_id or name == local_name:
        return False
    try:
        foreign_root = _canonical_text(folder)
    except AdmissionError:
        return False
    return (
        not _same_project_root(foreign_root, root)
        and matches_project_label(project_id, foreign_root)
        and not matches_project_label(project_id, root)
    )


def _verify_workspace_mount(item: Sandbox, root: str, run: Run) -> None:
    container_id = item.container_id
    result = run(
        ["docker", "container", "inspect", container_id, "--format", "{{json .}}"], timeout=30
    )
    try:
        if result.returncode:
            raise AdmissionError(f"cannot revalidate Sandbox {container_id}: {_detail(result)}")
        # The second probe verifies full identity again, rather than trusting a name.
        document = require_dict(json.loads(result.stdout), field="Sandbox")
        if (
            require_str(document, "Id") != container_id
            or require_str(document, "Name").removeprefix("/") != item.name
        ):
            raise AdmissionError("Sandbox identity changed during Project resolution")
        labels = _labels(require_dict(document.get("Config"), field="Config").get("Labels"))
        if (
            not _same_project_root(_workspace_mount_root(document, item.name), root)
            or labels.get("booley.role") != "interactive"
            or labels.get(_PROJECT_LABEL) != item.project_id
            or not _same_project_root(_project_root(document, labels, item.name), root)
            or require_dict(document.get("State"), field="State").get("Running") is not True
        ):
            raise AdmissionError(
                "Sandbox Project root/mount identity is contradictory or unavailable"
            )
    except (BoundaryError, ValueError) as exc:
        raise AdmissionError(f"cannot revalidate Sandbox {container_id} identity") from exc


def _policy() -> SandboxHostPolicy:
    try:
        return load_host_policy(on_deprecation=logger.warning)
    except HostConfigError as exc:
        raise AdmissionError(str(exc)) from exc


def _inventory(run: Run) -> tuple[Sandbox, ...]:
    command = [
        "docker",
        "ps",
        "--filter",
        f"label={dc.INTERACTIVE_ROLE_LABEL}",
        "--format",
        "{{.ID}}\t{{.Names}}",
    ]
    result = run(command)
    if result.returncode:
        raise AdmissionError("cannot enumerate Booley Sandboxes safely: " + _detail(result))
    items = []
    for line in result.stdout.splitlines():
        fields = line.split("\t")
        if len(fields) != 2 or not all(field.strip() for field in fields):
            raise AdmissionError("Docker returned an incomplete Sandbox listing")
        items.append(_inspect(fields[0].strip(), fields[1].strip(), run))
    return tuple(items)


def _vscode_inventory(project_roots: set[str], run: Run) -> tuple[Sandbox, ...]:
    """Inspect only stopped/running editor Sandboxes relevant to claims or cleanup."""
    if not project_roots:
        return ()
    result = run(
        [
            "docker",
            "ps",
            "-a",
            "--filter",
            f"label={dc.INTERACTIVE_ROLE_LABEL}",
            "--filter",
            f"label={_FOLDER_LABEL}",
            "--format",
            '{{.ID}}\t{{.Names}}\t{{.Label "devcontainer.local_folder"}}',
        ]
    )
    if result.returncode:
        raise AdmissionError("cannot enumerate VS Code Sandboxes safely: " + _detail(result))
    items = []
    for line in result.stdout.splitlines():
        fields = line.split("\t")
        if len(fields) != 3 or not all(field.strip() for field in fields):
            raise AdmissionError("Docker returned an incomplete VS Code Sandbox listing")
        container_id, listed_name, raw_root = (field.strip() for field in fields)
        try:
            root = _canonical_text(raw_root)
        except AdmissionError:
            if raw_root in project_roots:
                raise
            continue
        if any(_same_project_root(root, expected) for expected in project_roots):
            items.append(_inspect(container_id, listed_name, run))
    return tuple(items)


def _inspect(container_id: str, listed_name: str, run: Run, *, exact_id: bool = False) -> Sandbox:
    result = run(
        ["docker", "container", "inspect", container_id, "--format", "{{json .}}"], timeout=30
    )
    if result.returncode:
        raise AdmissionError(f"cannot inspect Sandbox {listed_name}: {_detail(result)}")
    try:
        document = require_dict(json.loads(result.stdout), field=f"Sandbox {listed_name}")
        config = require_dict(document.get("Config"), field="Config")
        state = require_dict(document.get("State"), field="State")
        labels = _labels(config.get("Labels"))
        running = require_bool_value(state.get("Running"), field="State.Running")
        started = _docker_time(require_str(state, "StartedAt"), allow_zero=not running)
        created = _docker_time(require_str(document, "Created"), allow_zero=False)
        name = require_str(document, "Name").removeprefix("/")
        root = _project_root(document, labels, listed_name)
        if exact_id and require_str(document, "Id") != container_id:
            raise AdmissionError(f"Sandbox identity changed while inspecting {listed_name}")
    except (BoundaryError, ValueError, json.JSONDecodeError) as exc:
        raise AdmissionError(
            f"Docker returned incomplete inspection for Sandbox {listed_name}"
        ) from exc
    if labels.get("booley.role") != "interactive":
        raise AdmissionError(f"cannot prove Sandbox ownership for {listed_name}")
    if name != listed_name:
        raise AdmissionError(f"Sandbox identity changed while inspecting {listed_name}")
    return Sandbox(
        container_id,
        name,
        root,
        labels.get(_PROJECT_LABEL),
        created,
        started,
        running,
        _FOLDER_LABEL in labels,
        labels.get(_FOLDER_LABEL),
    )


def _labels(raw: object) -> dict[str, str]:
    values = require_dict(raw, field="Config.Labels")
    return {
        require_str_value(key, field="Config.Labels key"): require_str_value(
            value, field=f"Config.Labels[{key!r}]"
        )
        for key, value in values.items()
    }


def _project_root(document: dict[str, Any], labels: dict[str, str], name: str) -> str:
    if folder := labels.get(_FOLDER_LABEL):
        return _canonical_text(folder)
    return _workspace_mount_root(document, name)


def _workspace_mount_root(document: dict[str, Any], name: str) -> str:
    mounts = require_list(document.get("Mounts"), field="Mounts")
    roots = []
    for index, raw in enumerate(mounts):
        mount = require_dict(raw, field=f"Mounts[{index}]")
        mount_type = require_str(mount, "Type")
        destination = require_str(mount, "Destination")
        if mount_type == "bind" and destination == "/work":
            host_path = host_path_from_docker_mount(require_str(mount, "Source"))
            if host_path is not None:
                roots.append(_canonical_text(str(host_path)))
    if len(roots) != 1:
        raise AdmissionError(f"cannot identify owning Project for Sandbox {name}")
    return roots[0]


def _docker_time(value: str, *, allow_zero: bool) -> datetime | None:
    if value.startswith("0001-01-01"):
        if allow_zero:
            return None
        raise ValueError("Docker zero timestamp")
    parsed = parse_timestamp(value)
    return parsed.astimezone(UTC)


def _admission_snapshot(
    run: Run, *, requesting_root: str, candidate_roots: set[str] | None = None
) -> tuple[
    tuple[Sandbox, ...], tuple[PendingStartClaim, ...], tuple[Sandbox, ...], tuple[str, ...]
]:
    """Inspect and conservatively reconcile host claims for the requesting Project."""
    live = _inventory(run)
    claims = _claims()
    roots = {claim.project_root for claim in claims}
    roots.update(candidate_roots or ())
    candidates = _merge_sandboxes(live, _vscode_inventory(roots, run))
    _project_vscode_sandboxes(requesting_root, candidates)
    remaining, diagnostics = _reconcile_claims(claims, candidates, requesting_root)
    return live, remaining, candidates, diagnostics


def _merge_sandboxes(*groups: tuple[Sandbox, ...]) -> tuple[Sandbox, ...]:
    merged: dict[str, Sandbox] = {}
    for group in groups:
        merged.update((item.container_id, item) for item in group)
    return tuple(merged.values())


def _reconcile_claims(
    claims: tuple[PendingStartClaim, ...], sandboxes: tuple[Sandbox, ...], requesting_root: str
) -> tuple[tuple[PendingStartClaim, ...], tuple[str, ...]]:
    remaining = []
    diagnostics = []
    consumed = []
    for claim in claims:
        applicable = tuple(
            item
            for item in sandboxes
            if item.vscode and _same_project_root(item.project_root, claim.project_root)
        )
        mismatches = tuple(
            item for item in applicable if not _valid_identity(item, claim.project_root)
        )
        for item in mismatches:
            message = _identity_recovery(item, claim.project_root)
            if _same_project_root(claim.project_root, requesting_root):
                raise AdmissionError(message)
            logger.warning("%s", message)
            diagnostics.append(message)
        if not mismatches and any(_consumed(claim, item) for item in applicable):
            consumed.append(claim)
        else:
            remaining.append(claim)
    for claim in consumed:
        (_store().root / _claim_filename(claim.project_root)).unlink()
    return tuple(remaining), tuple(diagnostics)


def _valid_identity(item: Sandbox, root: str) -> bool:
    roots = [root, item.project_root]
    if item.observed_root and _same_project_root(item.observed_root, root):
        roots.append(item.observed_root)
    return any(matches_project_label(item.project_id, candidate) for candidate in roots)


def _command(argv: list[str]) -> str:
    if os.name == "nt":
        return "& " + " ".join("'" + arg.replace("'", "''") + "'" for arg in argv)
    return shlex.join(argv)


def _identity_recovery(item: Sandbox, root: str) -> str:
    stop = _command(["docker", "stop", item.container_id])
    remove = _command(["docker", "rm", item.container_id])
    down = _command(["booley", "session", "down", "--project", root])
    return (
        f"Sandbox {item.name} ({item.container_id}) Project identity disagrees with {root}. "
        f"{'In PowerShell, s' if os.name == 'nt' else 'S'}top it with `{stop}`, "
        f"remove it with `{remove}`, then run `{down}` to release "
        "its pending claim. Recreate its VS Code container from the issued configuration and retry."
    )


def _consumed(claim: PendingStartClaim, item: Sandbox) -> bool:
    missing = object()
    previous = claim.baseline.get(item.container_id, missing)
    return previous is missing or previous != _started_token(item)


def _project_vscode_sandboxes(
    project_root: str, sandboxes: tuple[Sandbox, ...]
) -> tuple[Sandbox, ...]:
    matched = tuple(
        item
        for item in sandboxes
        if item.vscode and _same_project_root(item.project_root, project_root)
    )
    for item in matched:
        if not _valid_identity(item, project_root):
            raise AdmissionError(_identity_recovery(item, project_root))
    return matched


def _started_token(item: Sandbox) -> str | None:
    return item.started_at.isoformat() if item.started_at is not None else None


def _claims() -> tuple[PendingStartClaim, ...]:
    store = _store()
    if not store.validate_existing_directory():
        return ()
    claims = []
    for path in sorted(store.root.iterdir()):
        if path.name.startswith("."):
            continue
        if path.is_symlink() or not path.is_file() or not path.name.endswith(_CLAIM_SUFFIX):
            raise AdmissionError(f"unexpected Sandbox admission claim entry: {path}")
        claim = _load_claim(path)
        if path.name != _claim_filename(claim.project_root):
            raise AdmissionError(f"Sandbox admission claim identity mismatch: {path}")
        claims.append(claim)
    return tuple(claims)


def _load_claim(path: Path) -> PendingStartClaim:
    try:
        raw = _store().read_json(path.name)
        values = require_dict(raw, field="Sandbox admission claim")
        if set(values) != {"schema_version", "project_root", "claimed_at", "baseline"}:
            raise AdmissionError("Sandbox admission claim has an invalid shape")
        version = values.get("schema_version")
        project_root = require_str(values, "project_root")
        claimed_at = require_str(values, "claimed_at")
        parse_timestamp(claimed_at)
        raw_baseline = require_dict(values.get("baseline"), field="baseline")
        baseline = {}
        for container_id, raw_started_at in raw_baseline.items():
            key = require_str_value(container_id, field="baseline container ID")
            started_at = None
            if raw_started_at is not None:
                started_at = require_str_value(raw_started_at, field=f"baseline[{key}]")
                parse_timestamp(started_at)
            baseline[key] = started_at
    except (OSError, UnicodeDecodeError, json.JSONDecodeError, BoundaryError, ValueError) as exc:
        raise AdmissionError(f"cannot read Sandbox admission claim {path}: {exc}") from exc
    if version != _SCHEMA_VERSION or project_root != _canonical_text(project_root):
        raise AdmissionError(f"Sandbox admission claim is invalid: {path}")
    return PendingStartClaim(_SCHEMA_VERSION, project_root, claimed_at, baseline)


def _store() -> PrivateStore:
    return PrivateStore(
        config_dir() / "runtime" / "session-start-claims",
        config_dir().parent,
        "Sandbox admission claim",
        AdmissionError,
    )


def _claim_filename(project_root: str) -> str:
    return hashlib.sha256(os.path.normcase(project_root).encode()).hexdigest() + _CLAIM_SUFFIX


def _canonical_root(project_root: Path) -> str:
    try:
        return str(project_root.resolve(strict=False))
    except (OSError, RuntimeError) as exc:
        raise AdmissionError(f"cannot resolve Project path {project_root}: {exc}") from exc


def _canonical_text(value: str) -> str:
    path = host_path_from_docker_mount(value)
    if path is None:
        raise AdmissionError(f"Project path is unavailable: {value!r}")
    if not path.is_absolute() or ".." in path.parts:
        raise AdmissionError(f"Project path is not canonical: {value!r}")
    return _canonical_root(path)


def _same_project_root(left: str, right: str) -> bool:
    if os.path.normcase(left) == os.path.normcase(right):
        return True
    try:
        return Path(left).samefile(Path(right))
    except OSError:
        return False


def _report_recovery_capacity(run: Run) -> None:
    """Report a recovery that can temporarily restore the host above its cap."""
    try:
        live = _inventory(run)
        policy = _policy()
    except (AdmissionError, OSError, subprocess.SubprocessError) as exc:
        logger.warning("cannot inspect Sandbox capacity during recovery: %s", exc)
        return
    if len(live) >= policy.max_sessions:
        logger.warning(
            "Sandbox recovery is restoring work at or above sandbox.max_sessions=%d; "
            "ordinary starts remain blocked until capacity is freed",
            policy.max_sessions,
        )


def _refusal(
    policy: SandboxHostPolicy,
    live: tuple[Sandbox, ...],
    claims: tuple[PendingStartClaim, ...],
    now: datetime,
    diagnostics: tuple[str, ...] = (),
) -> str:
    lines = [f"Sandbox start refused: host is at sandbox.max_sessions={policy.max_sessions}."]
    for item in sorted(live, key=lambda value: (value.project_root, value.name)):
        started = item.started_at or item.created_at
        lines.append(
            f"- {item.name}: Project {item.project_root} (started {format_age(started, now=now)})"
        )
    for claim in sorted(claims, key=lambda value: value.project_root):
        lines.append(f"- pending editor start: Project {claim.project_root}")
    roots = sorted({item.project_root for item in live} | {claim.project_root for claim in claims})
    for root in roots:
        if any(item.vscode and _same_project_root(item.project_root, root) for item in live):
            lines.append(
                f"Free capacity for {root}: close the VS Code window or use Dev Containers to stop its Sandbox."
            )
            continue
        argv = ["booley", "session", "down", "--project", root]
        command = _command(argv)
        shell = " in PowerShell" if os.name == "nt" else ""
        lines.append(f"Free capacity{shell} with `{command}`.")
    lines.extend(diagnostics)
    lines.append(f"Or raise [sandbox].max_sessions in {host_config_path()}.")
    lines.append(HOST_POLICY_MIGRATION_GUIDANCE)
    return "\n".join(lines)


def _detail(result: subprocess.CompletedProcess[str]) -> str:
    return result.stderr.strip() or result.stdout.strip() or f"exit {result.returncode}"
