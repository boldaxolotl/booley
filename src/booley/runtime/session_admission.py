"""Host-wide admission control for Interactive Mode Sandboxes."""

from __future__ import annotations

import hashlib
import json
import os
import shlex
import subprocess
from collections.abc import Callable
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from booley.config.host_config import (
    HostConfigError,
    InteractiveHostPolicy,
    host_config_path,
    load_host_policy,
)
from booley.core.boundary import BoundaryError, require_bool, require_dict, require_str
from booley.core.private_store import PrivateStore
from booley.core.user_paths import config_dir
from booley.runtime import devcontainer as dc
from booley.runtime.platform_paths import host_path_from_docker_mount
from booley.runtime.timefmt import parse_timestamp

Run = Callable[..., subprocess.CompletedProcess[str]]
_SCHEMA_VERSION = 1
_CLAIM_SUFFIX = ".json"
_FOLDER_LABEL = "devcontainer.local_folder"
_PROJECT_LABEL = "booley.project-id"


class AdmissionError(RuntimeError):
    """Sandbox capacity cannot be determined or no slot is available."""


@dataclass(frozen=True, slots=True)
class PendingStartClaim:
    """One VS Code Sandbox start admitted before Docker creates it."""

    schema_version: int
    project_root: str
    claimed_at: str


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
        return
    root = _canonical_root(project_root)
    sandboxes = _inventory(run, include_stopped=True)
    if any(item.running and item.name == target_name for item in sandboxes):
        return
    claims = _reconcile_claims(sandboxes)
    policy = _policy()
    live = tuple(item for item in sandboxes if item.running)
    if len(live) + len(claims) >= policy.max_sessions:
        raise AdmissionError(_refusal(policy, live, claims, now or datetime.now(UTC)))
    # A headless start must not consume its Project's editor reservation.
    if any(claim.project_root == root for claim in claims):
        raise AdmissionError(_refusal(policy, live, claims, now or datetime.now(UTC)))


def claim_vscode_start(
    project_root: Path,
    *,
    run: Run = _run,
    now: datetime | None = None,
) -> bool:
    """Atomically reserve capacity for a later VS Code Docker start."""
    root = _canonical_root(project_root)
    sandboxes = _inventory(run, include_stopped=True)
    claims = _reconcile_claims(sandboxes)
    if any(item.running and item.vscode and item.project_root == root for item in sandboxes):
        return False
    if any(claim.project_root == root for claim in claims):
        return False
    policy = _policy()
    live = tuple(item for item in sandboxes if item.running)
    instant = now or datetime.now(UTC)
    if len(live) + len(claims) >= policy.max_sessions:
        raise AdmissionError(_refusal(policy, live, claims, instant))
    claim = PendingStartClaim(_SCHEMA_VERSION, root, _timestamp(instant))
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


def has_pending_claim(project_root: Path) -> bool:
    """Return whether this Project has a validated pending editor start."""
    root = _canonical_root(project_root)
    return any(claim.project_root == root for claim in _claims())


def vscode_sandboxes(project_root: Path, *, run: Run = _run) -> tuple[Sandbox, ...]:
    """Return strictly identified VS Code Sandboxes for one Project."""
    root = _canonical_root(project_root)
    return tuple(
        item
        for item in _inventory(run, include_stopped=True)
        if item.vscode and item.project_root == root
    )


def _policy() -> InteractiveHostPolicy:
    try:
        return load_host_policy()
    except HostConfigError as exc:
        raise AdmissionError(str(exc)) from exc


def _inventory(run: Run, *, include_stopped: bool) -> tuple[Sandbox, ...]:
    command = ["docker", "ps"]
    if include_stopped:
        command.append("-a")
    command += [
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
    except (BoundaryError, ValueError, json.JSONDecodeError) as exc:
        raise AdmissionError(
            f"Docker returned incomplete inspection for Sandbox {listed_name}"
        ) from exc
    if labels.get("booley.role") != "interactive":
        raise AdmissionError(f"cannot prove Sandbox ownership for {listed_name}")
    name = str(document.get("Name", "")).removeprefix("/") or listed_name
    if name != listed_name:
        raise AdmissionError(f"Sandbox identity changed while inspecting {listed_name}")
    root = _project_root(document, labels, listed_name)
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
    if not all(isinstance(key, str) and isinstance(value, str) for key, value in values.items()):
        raise BoundaryError("Config.Labels must contain strings")
    return dict(values)


def _project_root(document: dict[str, Any], labels: dict[str, str], name: str) -> str:
    if folder := labels.get(_FOLDER_LABEL):
        return _canonical_text(folder)
    mounts = document.get("Mounts")
    if not isinstance(mounts, list):
        raise AdmissionError(f"cannot identify owning Project for Sandbox {name}")
    for raw in mounts:
        if (
            not isinstance(raw, dict)
            or raw.get("Type") != "bind"
            or raw.get("Destination") != "/work"
        ):
            continue
        source = raw.get("Source")
        if not isinstance(source, str) or not source:
            break
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


def _reconcile_claims(sandboxes: tuple[Sandbox, ...]) -> tuple[PendingStartClaim, ...]:
    remaining = []
    for claim in _claims():
        claimed_at = parse_timestamp(claim.claimed_at).astimezone(UTC)
        matched = any(_consumed(claim, claimed_at, item) for item in sandboxes)
        if matched:
            (_store().root / _claim_filename(claim.project_root)).unlink()
        else:
            remaining.append(claim)
    return tuple(remaining)


def _consumed(claim: PendingStartClaim, claimed_at: datetime, item: Sandbox) -> bool:
    if not item.vscode or item.project_root != claim.project_root:
        return False
    matching_id = item.project_id in {None, dc.canonical_project_id(Path(claim.project_root))}
    if not matching_id:
        raise AdmissionError(f"Sandbox Project identity disagrees with {claim.project_root}")
    return item.created_at >= claimed_at or (
        item.started_at is not None and item.started_at >= claimed_at
    )


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
        if set(values) != {"schema_version", "project_root", "claimed_at"}:
            raise AdmissionError("Sandbox admission claim has an invalid shape")
        version = values.get("schema_version")
        project_root = require_str(values, "project_root")
        claimed_at = require_str(values, "claimed_at")
        parse_timestamp(claimed_at)
    except (OSError, UnicodeDecodeError, json.JSONDecodeError, BoundaryError, ValueError) as exc:
        raise AdmissionError(f"cannot read Sandbox admission claim {path}: {exc}") from exc
    if version != _SCHEMA_VERSION or project_root != _canonical_text(project_root):
        raise AdmissionError(f"Sandbox admission claim is invalid: {path}")
    return PendingStartClaim(_SCHEMA_VERSION, project_root, claimed_at)


def _store() -> PrivateStore:
    return PrivateStore(
        config_dir() / "runtime" / "session-start-claims",
        config_dir().parent,
        "Sandbox admission claim",
        AdmissionError,
    )


def _claim_filename(project_root: str) -> str:
    return hashlib.sha256(project_root.encode()).hexdigest() + _CLAIM_SUFFIX


def _canonical_root(project_root: Path) -> str:
    return str(project_root.resolve(strict=True))


def _canonical_text(value: str) -> str:
    path = Path(value)
    if not path.is_absolute() or ".." in path.parts:
        raise AdmissionError(f"Project path is not canonical: {value!r}")
    return str(path.resolve(strict=False))


def _timestamp(value: datetime) -> str:
    aware = value if value.tzinfo is not None else value.replace(tzinfo=UTC)
    return aware.astimezone(UTC).replace(microsecond=0).strftime("%Y-%m-%dT%H:%M:%SZ")


def _age(started_at: datetime, now: datetime) -> str:
    seconds = max(0, int((now.astimezone(UTC) - started_at).total_seconds()))
    hours, remainder = divmod(seconds, 3600)
    minutes, trailing = divmod(remainder, 60)
    if hours:
        return f"{hours}h {minutes}m" if minutes else f"{hours}h"
    return f"{minutes}m {trailing}s" if minutes else f"{trailing}s"


def _refusal(
    policy: InteractiveHostPolicy,
    live: tuple[Sandbox, ...],
    claims: tuple[PendingStartClaim, ...],
    now: datetime,
) -> str:
    lines = [f"Sandbox start refused: host is at interactive.max_sessions={policy.max_sessions}."]
    for item in sorted(live, key=lambda value: (value.project_root, value.name)):
        started = item.started_at or item.created_at
        lines.append(f"- {item.name}: Project {item.project_root} (age {_age(started, now)})")
    for claim in sorted(claims, key=lambda value: value.project_root):
        lines.append(f"- pending editor start: Project {claim.project_root}")
    roots = sorted({item.project_root for item in live} | {claim.project_root for claim in claims})
    for root in roots:
        argv = ["booley", "session", "down", "--project-root", root]
        command = subprocess.list2cmdline(argv) if os.name == "nt" else shlex.join(argv)
        lines.append(f"Free capacity with `{command}`.")
    lines.append(f"Or raise [interactive].max_sessions in {host_config_path()}.")
    return "\n".join(lines)


def _detail(result: subprocess.CompletedProcess[str]) -> str:
    return result.stderr.strip() or result.stdout.strip() or f"exit {result.returncode}"
