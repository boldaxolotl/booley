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
    require_bool,
    require_dict,
    require_list,
    require_str,
    require_str_value,
)
from booley.core.private_store import PrivateStore
from booley.core.user_paths import config_dir
from booley.runtime import devcontainer as dc
from booley.runtime.platform_paths import host_path_from_docker_mount
from booley.runtime.timefmt import parse_timestamp, rfc3339_from_datetime

Run = Callable[..., subprocess.CompletedProcess[str]]
_SCHEMA_VERSION = 2
_CLAIM_SUFFIX = ".json"
_FOLDER_LABEL = "devcontainer.local_folder"
_PROJECT_LABEL = "booley.project-id"
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
    live, claims, _candidates = _admission_snapshot(run)
    if any(item.name == target_name for item in live):
        return
    policy = _policy()
    if len(live) + len(claims) >= policy.max_sessions:
        raise AdmissionError(_refusal(policy, live, claims, now or datetime.now(UTC)))
    # A headless start must not consume its Project's editor reservation.
    if any(_same_project_root(claim.project_root, root) for claim in claims):
        raise AdmissionError(_refusal(policy, live, claims, now or datetime.now(UTC)))


def claim_vscode_start(
    project_root: Path,
    *,
    run: Run = _run,
    now: datetime | None = None,
) -> bool:
    """Atomically reserve capacity for a later VS Code Docker start."""
    root = _canonical_root(project_root)
    live, claims, candidates = _admission_snapshot(run, candidate_roots={root})
    project_sandboxes = _project_vscode_sandboxes(root, candidates)
    if any(item.running for item in project_sandboxes):
        return False
    if any(_same_project_root(claim.project_root, root) for claim in claims):
        return False
    policy = _policy()
    instant = now or datetime.now(UTC)
    if len(live) + len(claims) >= policy.max_sessions:
        raise AdmissionError(_refusal(policy, live, claims, instant))
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
    _live, claims, _candidates = _admission_snapshot(run)
    return any(_same_project_root(claim.project_root, root) for claim in claims)


def vscode_sandboxes(project_root: Path, *, run: Run = _run) -> tuple[Sandbox, ...]:
    """Return strictly identified VS Code Sandboxes for one Project."""
    root = _canonical_root(project_root)
    return _project_vscode_sandboxes(root, _vscode_inventory({root}, run))


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


def _inspect(container_id: str, listed_name: str, run: Run) -> Sandbox:
    result = run(["docker", "container", "inspect", container_id, "--format", "{{json .}}"])
    if result.returncode:
        raise AdmissionError(f"cannot inspect Sandbox {listed_name}: {_detail(result)}")
    try:
        document = require_dict(json.loads(result.stdout), field=f"Sandbox {listed_name}")
        config = require_dict(document.get("Config"), field="Config")
        state = require_dict(document.get("State"), field="State")
        labels = _labels(config.get("Labels"))
        running = require_bool(state, "Running", field="State.Running")
        started = _docker_time(require_str(state, "StartedAt"), allow_zero=not running)
        created = _docker_time(require_str(document, "Created"), allow_zero=False)
        name = require_str(document, "Name").removeprefix("/")
        root = _project_root(document, labels, listed_name)
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
    mounts = require_list(document.get("Mounts"), field="Mounts")
    for index, raw in enumerate(mounts):
        mount = require_dict(raw, field=f"Mounts[{index}]")
        mount_type = require_str(mount, "Type")
        destination = require_str(mount, "Destination")
        if mount_type != "bind" or destination != "/work":
            continue
        source = require_str(mount, "Source")
        host_path = host_path_from_docker_mount(source)
        if host_path is None:
            break
        return _canonical_text(str(host_path))
    raise AdmissionError(f"cannot identify owning Project for Sandbox {name}")


def _docker_time(value: str, *, allow_zero: bool) -> datetime | None:
    if value.startswith("0001-01-01"):
        if allow_zero:
            return None
        raise ValueError("Docker zero timestamp")
    parsed = parse_timestamp(value)
    return parsed.astimezone(UTC)


def _admission_snapshot(
    run: Run, *, candidate_roots: set[str] | None = None
) -> tuple[tuple[Sandbox, ...], tuple[PendingStartClaim, ...], tuple[Sandbox, ...]]:
    """Return strict live inventory, reconciled claims, and relevant editor inventory."""
    live = _inventory(run)
    claims = _claims()
    roots = {claim.project_root for claim in claims}
    roots.update(candidate_roots or ())
    candidates = _merge_sandboxes(live, _vscode_inventory(roots, run))
    return live, _reconcile_claims(claims, candidates), candidates


def _merge_sandboxes(*groups: tuple[Sandbox, ...]) -> tuple[Sandbox, ...]:
    merged: dict[str, Sandbox] = {}
    for group in groups:
        merged.update((item.container_id, item) for item in group)
    return tuple(merged.values())


def _reconcile_claims(
    claims: tuple[PendingStartClaim, ...], sandboxes: tuple[Sandbox, ...]
) -> tuple[PendingStartClaim, ...]:
    remaining = []
    for claim in claims:
        matched = any(_consumed(claim, item) for item in sandboxes)
        if matched:
            (_store().root / _claim_filename(claim.project_root)).unlink()
        else:
            remaining.append(claim)
    return tuple(remaining)


def _consumed(claim: PendingStartClaim, item: Sandbox) -> bool:
    if not item.vscode or not _same_project_root(item.project_root, claim.project_root):
        return False
    if item.project_id != _project_id(claim.project_root):
        raise AdmissionError(f"Sandbox Project identity disagrees with {claim.project_root}")
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
    expected = _project_id(project_root)
    if any(item.project_id != expected for item in matched):
        raise AdmissionError(f"Sandbox Project identity disagrees with {project_root}")
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
    return str(project_root.resolve(strict=False))


def _canonical_text(value: str) -> str:
    path = Path(value)
    if not path.is_absolute() or ".." in path.parts:
        raise AdmissionError(f"Project path is not canonical: {value!r}")
    return str(path.resolve(strict=False))


def _project_id(project_root: str) -> str:
    canonical = os.path.normcase(project_root).encode()
    return hashlib.sha256(canonical).hexdigest()[:32]


def _same_project_root(left: str, right: str) -> bool:
    return os.path.normcase(left) == os.path.normcase(right)


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


def _age(started_at: datetime, now: datetime) -> str:
    seconds = max(0, int((now.astimezone(UTC) - started_at).total_seconds()))
    hours, remainder = divmod(seconds, 3600)
    minutes, trailing = divmod(remainder, 60)
    if hours:
        return f"{hours}h {minutes}m" if minutes else f"{hours}h"
    return f"{minutes}m {trailing}s" if minutes else f"{trailing}s"


def _refusal(
    policy: SandboxHostPolicy,
    live: tuple[Sandbox, ...],
    claims: tuple[PendingStartClaim, ...],
    now: datetime,
) -> str:
    lines = [f"Sandbox start refused: host is at sandbox.max_sessions={policy.max_sessions}."]
    for item in sorted(live, key=lambda value: (value.project_root, value.name)):
        started = item.started_at or item.created_at
        lines.append(f"- {item.name}: Project {item.project_root} (age {_age(started, now)})")
    for claim in sorted(claims, key=lambda value: value.project_root):
        lines.append(f"- pending editor start: Project {claim.project_root}")
    roots = sorted({item.project_root for item in live} | {claim.project_root for claim in claims})
    for root in roots:
        argv = ["booley", "session", "down", "--project", root]
        command = subprocess.list2cmdline(argv) if os.name == "nt" else shlex.join(argv)
        lines.append(f"Free capacity with `{command}`.")
    lines.append(f"Or raise [sandbox].max_sessions in {host_config_path()}.")
    lines.append(HOST_POLICY_MIGRATION_GUIDANCE)
    return "\n".join(lines)


def _detail(result: subprocess.CompletedProcess[str]) -> str:
    return result.stderr.strip() or result.stdout.strip() or f"exit {result.returncode}"
