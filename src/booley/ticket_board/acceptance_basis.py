"""Immutable acceptance inputs published automatically when a Ticket is enqueued."""

from __future__ import annotations

import hashlib
import json
import re
import subprocess
import tempfile
from collections.abc import Mapping
from copy import deepcopy
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from booley.core.boundary import BoundaryError, require_dict, require_list, require_str
from booley.runtime.project_dir import checkout_project_dir_relative_to, runtime_dir
from booley.runtime.ticket_repositories import (
    paired_project_repository,
    resolve_inner_project_repo,
)

from .acceptance_targets import ContractTargetBinding
from .contract_path_policy import is_static_contract_path
from .persistence import WriteOnceConflictError, atomic_write_once

SCHEMA_VERSION = 1
BLOCK_REASON = "acceptance-input-change-required"
RECORD_SCHEMA_VERSION = 1
TICKET_REF_PREFIX = "refs/heads/booley-generation"

_COMMIT_RE = re.compile(r"^(?:[0-9a-f]{40}|[0-9a-f]{64})$")
_SAFE_REF_RE = re.compile(r"^refs/heads/[A-Za-z0-9][A-Za-z0-9._/-]*$")
_AUTHORED_FIELDS = (
    "summary",
    "type",
    "branch",
    "project_destination_ref",
    "scope",
    "spec",
    "dependencies",
    "priority",
    "criteria",
    "on_success",
    "auto_approve",
    "synthesis",
    "baseline_tests",
    "scope_current",
    "scope_new",
)
_GENERATED_FIELDS = frozenset(
    {
        "acceptance_basis",
        "created",
        "feature_branch",
        "steps_completed",
        "stage",
    }
)
_RETIRED_FIELDS = frozenset({"target_contract", "target_contract_history", "base_sha"})
_AUTHORED_DEFAULTS: dict[str, Any] = {
    "scope": [],
    "spec": "",
    "dependencies": [],
    "priority": "medium",
    "criteria": {},
    "on_success": {
        "destination": "review",
        "merge": True,
        "cleanup": True,
        "triage_report": True,
        "remove_targets": [],
    },
}


class AcceptanceBasisError(ValueError):
    """An Acceptance Basis or its committed record is malformed."""


@dataclass(frozen=True)
class AcceptancePathPolicy:
    """Versioned discovery policy for schema-1 acceptance control paths."""

    schema: int = SCHEMA_VERSION

    def discover(self, project_root: Path | str) -> tuple[str, ...]:
        if self.schema != SCHEMA_VERSION:
            raise AcceptanceBasisError(f"unsupported Acceptance Path Policy {self.schema}")
        from .acceptance_targets import contract_control_paths

        return contract_control_paths(project_root)


PATH_POLICY = AcceptancePathPolicy()


@dataclass(frozen=True, order=True)
class BasisParticipant:
    """One repository participating in this Ticket generation."""

    role: str
    authoring_sha: str
    ticket_ref: str
    destination_ref: str
    destination_sha: str

    def as_dict(self) -> dict[str, str]:
        return {
            "role": self.role,
            "authoring_sha": self.authoring_sha,
            "ticket_ref": self.ticket_ref,
            "destination_ref": self.destination_ref,
            "destination_sha": self.destination_sha,
        }


@dataclass(frozen=True)
class AcceptanceBasis:
    """Minimal immutable pointer stored in Ticket frontmatter."""

    participants: tuple[BasisParticipant, ...]
    bindings: tuple[ContractTargetBinding, ...] = ()
    removal_targets: tuple[str, ...] = ()
    schema: int = SCHEMA_VERSION

    def __post_init__(self) -> None:
        if self.schema != SCHEMA_VERSION:
            raise AcceptanceBasisError(
                f"acceptance_basis.schema must be {SCHEMA_VERSION}, got {self.schema!r}"
            )
        roles = tuple(row.role for row in self.participants)
        if roles != tuple(sorted(set(roles))):
            raise AcceptanceBasisError(
                "acceptance_basis.participants must be sorted by unique role"
            )
        if "outer" not in roles:
            raise AcceptanceBasisError(
                "acceptance_basis.participants requires an outer participant"
            )

    @classmethod
    def from_mapping(cls, value: Any) -> AcceptanceBasis:
        """Validate the deliberately small frontmatter representation."""
        try:
            data = require_dict(value, field="acceptance_basis")
        except BoundaryError as exc:
            raise AcceptanceBasisError(str(exc)) from exc
        if set(data) != {"schema", "participants"}:
            raise AcceptanceBasisError(
                "acceptance_basis must contain exactly schema and participants"
            )
        if data.get("schema") != SCHEMA_VERSION:
            raise AcceptanceBasisError(
                f"acceptance_basis.schema must be {SCHEMA_VERSION}, got {data.get('schema')!r}"
            )
        try:
            rows = require_list(data.get("participants"), field="acceptance_basis.participants")
        except BoundaryError as exc:
            raise AcceptanceBasisError(str(exc)) from exc
        return cls(tuple(_parse_participant(row, index) for index, row in enumerate(rows)))

    def as_dict(self) -> dict[str, Any]:
        return {
            "schema": self.schema,
            "participants": [row.as_dict() for row in self.participants],
        }

    @property
    def basis_id(self) -> str:
        return hashlib.sha256(canonical_json(self.as_dict())).hexdigest()

    @property
    def outer_sha(self) -> str:
        return self.participant("outer").authoring_sha

    @property
    def project_sha(self) -> str:
        project = next((row for row in self.participants if row.role == "project"), None)
        return project.authoring_sha if project is not None else ""

    @property
    def surface_digest(self) -> str:
        """Stable identity for versioned evidence that still names a digest field."""
        return self.basis_id

    def participant(self, role: str) -> BasisParticipant:
        try:
            return next(row for row in self.participants if row.role == role)
        except StopIteration as exc:
            raise AcceptanceBasisError(f"Acceptance Basis has no {role!r} participant") from exc

    def with_record(self, record: Mapping[str, Any]) -> AcceptanceBasis:
        bindings = tuple(_binding_from_record(row) for row in record.get("bindings", ()))
        on_success = record.get("ticket", {}).get("frontmatter", {}).get("on_success", {})
        removals = (
            tuple(on_success.get("remove_targets", ())) if isinstance(on_success, Mapping) else ()
        )
        return AcceptanceBasis(self.participants, bindings, removals, self.schema)


def _parse_participant(value: Any, index: int) -> BasisParticipant:
    field = f"acceptance_basis.participants[{index}]"
    try:
        row = require_dict(value, field=field)
    except BoundaryError as exc:
        raise AcceptanceBasisError(str(exc)) from exc
    expected = {"role", "authoring_sha", "ticket_ref", "destination_ref", "destination_sha"}
    if set(row) != expected:
        raise AcceptanceBasisError(f"{field} must contain exactly {', '.join(sorted(expected))}")
    try:
        role = require_str(row, "role").strip()
        authoring_sha = require_str(row, "authoring_sha").strip().lower()
        ticket_ref = require_str(row, "ticket_ref").strip()
        destination_ref = require_str(row, "destination_ref").strip()
        destination_sha = require_str(row, "destination_sha").strip().lower()
    except BoundaryError as exc:
        raise AcceptanceBasisError(f"{field}: {exc}") from exc
    if role not in {"outer", "project"}:
        raise AcceptanceBasisError(f"{field}.role must be outer or project")
    if not _COMMIT_RE.fullmatch(authoring_sha) or not _COMMIT_RE.fullmatch(destination_sha):
        raise AcceptanceBasisError(f"{field} commit identities must be full Git SHAs")
    if not ticket_ref.startswith(TICKET_REF_PREFIX + "/") or not _valid_ref(ticket_ref):
        raise AcceptanceBasisError(f"{field}.ticket_ref is not a generation-qualified ref")
    if not _valid_ref(destination_ref):
        raise AcceptanceBasisError(f"{field}.destination_ref must be a full branch ref")
    return BasisParticipant(role, authoring_sha, ticket_ref, destination_ref, destination_sha)


def _valid_ref(value: str) -> bool:
    return (
        bool(_SAFE_REF_RE.fullmatch(value))
        and not any(token in value for token in ("..", "//", "@{", "\\"))
        and not value.endswith(("/", ".", ".lock"))
    )


def canonical_json(value: Any) -> bytes:
    return (
        json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False) + "\n"
    ).encode()


def authored_ticket_record(fields: Mapping[str, Any], body: str, bindings: Any) -> dict[str, Any]:
    """Build the canonical committed input record, rejecting unknown authored fields."""
    retired = sorted(_RETIRED_FIELDS & set(fields))
    if retired:
        raise AcceptanceBasisError(
            "legacy Target Contract tickets are unsupported after the hard cutoff; "
            f"remove or recreate fields: {', '.join(retired)}"
        )
    unknown = sorted(set(fields) - set(_AUTHORED_FIELDS) - _GENERATED_FIELDS)
    if unknown:
        raise AcceptanceBasisError(f"unknown authored Ticket field(s): {', '.join(unknown)}")
    frontmatter = _canonical_authored_fields(fields)
    rows = [_binding_to_record(row) for row in bindings]
    return {
        "schema": RECORD_SCHEMA_VERSION,
        "ticket": {"frontmatter": frontmatter, "body": body},
        "bindings": rows,
    }


def _binding_to_record(binding: ContractTargetBinding) -> dict[str, str]:
    return {
        "flow": binding.flow,
        "criterion": binding.criterion,
        "baseline_identity": binding.baseline,
        "baseline_selector": binding.baseline_selector,
        "candidate_identity": binding.candidate,
        "candidate_selector": binding.candidate_selector,
    }


def _canonical_authored_fields(fields: Mapping[str, Any]) -> dict[str, Any]:
    canonical = {
        name: deepcopy(value)
        for name, value in _AUTHORED_DEFAULTS.items()
        if name in _AUTHORED_FIELDS
    }
    canonical.update({name: fields[name] for name in _AUTHORED_FIELDS if name in fields})
    from booley.core.models import OnSuccess

    raw_on_success = canonical["on_success"]
    if not isinstance(raw_on_success, Mapping):
        raise AcceptanceBasisError("on_success must be a mapping")
    configured = OnSuccess.from_dict(dict(raw_on_success))
    errors = configured.validate()
    if errors:
        raise AcceptanceBasisError(errors[0])
    canonical["on_success"] = {
        "destination": configured.destination,
        "merge": configured.merge,
        "cleanup": configured.cleanup,
        "triage_report": configured.triage_report,
        "remove_targets": list(configured.remove_targets),
    }
    return canonical


def _binding_from_record(value: Any) -> ContractTargetBinding:
    if not isinstance(value, Mapping):
        raise AcceptanceBasisError("Acceptance Basis binding must be a mapping")
    expected = {
        "flow",
        "criterion",
        "baseline_identity",
        "baseline_selector",
        "candidate_identity",
        "candidate_selector",
    }
    if set(value) != expected or not all(isinstance(value[key], str) for key in expected):
        raise AcceptanceBasisError("Acceptance Basis binding has an invalid schema")
    return ContractTargetBinding(
        flow=value["flow"],
        criterion=value["criterion"],
        baseline=value["baseline_identity"],
        candidate=value["candidate_identity"],
        baseline_selector=value["baseline_selector"],
        candidate_selector=value["candidate_selector"],
    )


def record_relative_path(project_root: Path | str, *, project_participant: bool) -> Path:
    if project_participant:
        return Path("acceptance") / "bases"
    return checkout_project_dir_relative_to(Path(project_root)) / "acceptance" / "bases"


def load_basis_record(
    project_root: Path | str, slug: str, basis: AcceptanceBasis
) -> dict[str, Any]:
    """Read and validate the authored-input record from its owning participant commit."""
    root = Path(project_root).resolve()
    project = resolve_inner_project_repo(root)
    owner = basis.participant("project") if project is not None else basis.participant("outer")
    repository = project if project is not None else root
    if repository is None:
        raise AcceptanceBasisError("Acceptance Basis project participant is unavailable")
    relative = record_relative_path(root, project_participant=project is not None) / f"{slug}.json"
    result = subprocess.run(
        ["git", "show", f"{owner.authoring_sha}:{relative.as_posix()}"],
        cwd=repository,
        capture_output=True,
        timeout=30,
        check=False,
    )
    if result.returncode != 0:
        detail = result.stderr.decode(errors="replace").strip()
        raise AcceptanceBasisError(f"Acceptance Basis record is unavailable: {detail}")
    try:
        record = json.loads(result.stdout)
    except json.JSONDecodeError as exc:
        raise AcceptanceBasisError(f"Acceptance Basis record is invalid JSON: {exc}") from exc
    _validate_record(record)
    if canonical_json(record) != result.stdout:
        raise AcceptanceBasisError("Acceptance Basis record is not canonical JSON")
    return record


def _validate_record(value: Any) -> None:
    try:
        record = require_dict(value, field="Acceptance Basis record")
        ticket = require_dict(record.get("ticket"), field="Acceptance Basis record.ticket")
        frontmatter = require_dict(
            ticket.get("frontmatter"), field="Acceptance Basis record.ticket.frontmatter"
        )
        body = require_str(ticket, "body")
        bindings = require_list(record.get("bindings"), field="Acceptance Basis record.bindings")
    except BoundaryError as exc:
        raise AcceptanceBasisError(str(exc)) from exc
    if set(record) != {"schema", "ticket", "bindings"}:
        raise AcceptanceBasisError("Acceptance Basis record has invalid top-level fields")
    if record.get("schema") != RECORD_SCHEMA_VERSION:
        raise AcceptanceBasisError("Acceptance Basis record has an unsupported schema")
    if set(ticket) != {"frontmatter", "body"} or not isinstance(body, str):
        raise AcceptanceBasisError("Acceptance Basis record.ticket has an invalid schema")
    unknown = sorted(set(frontmatter) - set(_AUTHORED_FIELDS))
    if unknown:
        raise AcceptanceBasisError(
            f"Acceptance Basis record has unknown authored field(s): {', '.join(unknown)}"
        )
    if _canonical_authored_fields(frontmatter) != frontmatter:
        raise AcceptanceBasisError("Acceptance Basis record authored defaults are not canonical")
    for binding in bindings:
        _binding_from_record(binding)
    on_success = frontmatter.get("on_success")
    if on_success is not None and not isinstance(on_success, Mapping):
        raise AcceptanceBasisError("Acceptance Basis record on_success must be a mapping")
    if isinstance(on_success, Mapping):
        from booley.core.models import OnSuccess

        errors = OnSuccess.from_dict(dict(on_success)).validate()
        if errors:
            raise AcceptanceBasisError(f"Acceptance Basis record {errors[0]}")


def load_acceptance_basis(
    project_root: Path | str, slug: str, fields: Mapping[str, Any], body: str | None = None
) -> AcceptanceBasis:
    """Load the only supported executable Ticket format and cross-check its record."""
    if fields.get("target_contract") is not None:
        raise AcceptanceBasisError(
            "legacy Target Contract tickets are unsupported after the hard cutoff; recreate the Ticket"
        )
    basis = AcceptanceBasis.from_mapping(fields.get("acceptance_basis"))
    record = load_basis_record(project_root, slug, basis)
    _validate_receipt(Path(project_root), slug, basis, record)
    recorded_fields = record.get("ticket", {}).get("frontmatter")
    current_fields = _canonical_authored_fields(fields)
    if recorded_fields != current_fields:
        raise AcceptanceBasisError(f"{BLOCK_REASON}: authored Ticket frontmatter changed")
    if body is not None and record.get("ticket", {}).get("body") != body:
        raise AcceptanceBasisError(f"{BLOCK_REASON}: authored Ticket body changed")
    return basis.with_record(record)


def _receipt_payload(
    project_root: Path,
    slug: str,
    basis: AcceptanceBasis,
    record: Mapping[str, Any],
    *,
    source_sha256: str,
    operation_id: str,
) -> dict[str, Any]:
    project_owner = any(row.role == "project" for row in basis.participants)
    locator = (
        record_relative_path(project_root, project_participant=project_owner) / f"{slug}.json"
    )
    return {
        "schema": 1,
        "basis_id": basis.basis_id,
        "participants": [row.as_dict() for row in basis.participants],
        "record": {
            "role": "project" if project_owner else "outer",
            "locator": locator.as_posix(),
            "sha256": hashlib.sha256(canonical_json(record)).hexdigest(),
        },
        "source_sha256": source_sha256,
        "operation_id": operation_id,
    }


def _receipt_path(project_root: Path, slug: str, basis: AcceptanceBasis) -> Path:
    return runtime_dir(project_root) / "acceptance" / "bases" / slug / f"{basis.basis_id}.json"


def write_basis_receipt(
    project_root: Path | str,
    slug: str,
    basis: AcceptanceBasis,
    *,
    source_sha256: str,
    operation_id: str,
) -> dict[str, Any]:
    """Create the write-once control-plane receipt before Board publication."""
    root = Path(project_root).resolve()
    record = load_basis_record(root, slug, basis)
    path = _receipt_path(root, slug, basis)
    if path.exists():
        return _validate_receipt(root, slug, basis, record)
    receipt = _receipt_payload(
        root,
        slug,
        basis,
        record,
        source_sha256=source_sha256,
        operation_id=operation_id,
    )
    payload = canonical_json(receipt)
    try:
        atomic_write_once(path, payload)
    except WriteOnceConflictError as exc:
        raise AcceptanceBasisError(f"conflicting Acceptance Basis receipt: {path}") from exc
    return receipt


def load_basis_receipt(
    project_root: Path | str, slug: str, value: Mapping[str, Any] | None
) -> dict[str, Any]:
    """Return the validated write-once receipt embedded in acceptance evidence."""
    root = Path(project_root).resolve()
    basis = AcceptanceBasis.from_mapping(value)
    record = load_basis_record(root, slug, basis)
    return _validate_receipt(root, slug, basis, record)


def _validate_receipt(
    project_root: Path, slug: str, basis: AcceptanceBasis, record: Mapping[str, Any]
) -> dict[str, Any]:
    path = _receipt_path(project_root, slug, basis)
    try:
        actual = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise AcceptanceBasisError(f"Acceptance Basis receipt is unavailable: {path}") from exc
    expected = _receipt_payload(
        project_root,
        slug,
        basis,
        record,
        source_sha256=str(actual.get("source_sha256", "")),
        operation_id=str(actual.get("operation_id", "")),
    )
    source_identity = actual.get("source_sha256")
    operation_identity = actual.get("operation_id")
    identities_valid = (
        isinstance(source_identity, str)
        and re.fullmatch(r"[0-9a-f]{64}", source_identity) is not None
        and isinstance(operation_identity, str)
        and re.fullmatch(r"[0-9a-f]{32}", operation_identity) is not None
    )
    if not identities_valid or actual != expected or canonical_json(actual) != path.read_bytes():
        raise AcceptanceBasisError(f"{BLOCK_REASON}: Acceptance Basis receipt mismatch")
    return actual


def _git_paths(repository: Path, *args: str) -> set[str]:
    result = subprocess.run(
        ["git", *args],
        cwd=repository,
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    if result.returncode != 0:
        raise AcceptanceBasisError(
            f"git {' '.join(args)} failed in {repository}: {result.stderr.strip()}"
        )
    return {item for item in result.stdout.split("\0") if item}


def assert_inputs_unchanged(basis: AcceptanceBasis, project_root: Path | str) -> None:
    """Reject tracked, staged, or untracked changes to protected acceptance inputs."""
    root = Path(project_root).resolve()
    protected = _basis_control_paths(root, basis, PATH_POLICY.discover)
    project = next((row for row in basis.participants if row.role == "project"), None)
    try:
        prefix = checkout_project_dir_relative_to(root).as_posix().rstrip("/") + "/"
    except (FileNotFoundError, ValueError):
        prefix = ".booley_project/"
    outer_protected = {path for path in protected if not path.startswith(prefix)}
    if project is None:
        outer_protected.add((Path(prefix) / "acceptance" / "bases").as_posix())
    _assert_repository_inputs_unchanged(
        root,
        basis.outer_sha,
        outer_protected,
    )
    if project is None:
        return
    paired = paired_project_repository(root)
    project_repository = (
        paired.worktree if paired is not None else resolve_inner_project_repo(root)
    )
    if project_repository is None:
        raise AcceptanceBasisError(f"{BLOCK_REASON}: paired project repository is unavailable")
    project_protected = {
        path.removeprefix(prefix) for path in protected if path.startswith(prefix)
    }
    project_protected.add("acceptance/bases")
    _assert_repository_inputs_unchanged(
        project_repository,
        project.authoring_sha,
        project_protected,
        ticket_prefix=prefix,
    )


def _basis_control_paths(root: Path, basis: AcceptanceBasis, discover: Any) -> set[str]:
    """Discover protected paths from both baseline and effective composite trees."""
    try:
        current = set(discover(root))
    except FileNotFoundError:
        current = set()
    except ValueError as exc:
        raise AcceptanceBasisError(f"{BLOCK_REASON}: {exc}") from exc
    with tempfile.TemporaryDirectory(prefix="booley-basis-controls-") as raw_directory:
        baseline = Path(raw_directory) / "outer"
        _clone_commit(root, baseline, basis.outer_sha)
        project = next((row for row in basis.participants if row.role == "project"), None)
        if project is not None:
            source = _project_repository(root)
            project_relative = checkout_project_dir_relative_to(root)
            _clone_commit(source, baseline / project_relative, project.authoring_sha)
        try:
            current.update(discover(baseline))
        except FileNotFoundError:
            pass
        except ValueError as exc:
            raise AcceptanceBasisError(f"{BLOCK_REASON}: {exc}") from exc
    return current


def _project_repository(root: Path) -> Path:
    paired = paired_project_repository(root)
    repository = paired.worktree if paired is not None else resolve_inner_project_repo(root)
    if repository is None:
        raise AcceptanceBasisError(f"{BLOCK_REASON}: paired project repository is unavailable")
    return repository


def _clone_commit(repository: Path, destination: Path, commit: str) -> None:
    clone = subprocess.run(
        ["git", "clone", "--shared", "--no-checkout", str(repository), str(destination)],
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    if clone.returncode != 0:
        raise AcceptanceBasisError(
            f"could not materialize Acceptance Basis: {clone.stderr.strip()}"
        )
    checkout = subprocess.run(
        ["git", "checkout", "--detach", commit],
        cwd=destination,
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    if checkout.returncode != 0:
        raise AcceptanceBasisError(
            f"could not materialize Acceptance Basis commit {commit}: {checkout.stderr.strip()}"
        )


def _assert_repository_inputs_unchanged(
    repository: Path,
    authoring_sha: str,
    protected: set[str],
    *,
    ticket_prefix: str = "",
) -> None:
    changed = _git_paths(repository, "diff", "--name-only", "-z", authoring_sha)
    changed.update(_git_paths(repository, "diff", "--cached", "--name-only", "-z", authoring_sha))
    changed.update(_git_paths(repository, "ls-files", "--others", "--exclude-standard", "-z"))
    violations = sorted(
        path
        for path in changed
        if path in protected
        or any(path == prefix or path.startswith(prefix.rstrip("/") + "/") for prefix in protected)
        or any(prefix.startswith(path.rstrip("/") + "/") for prefix in protected)
        or is_static_contract_path(f"{ticket_prefix}{path}")
        or path.endswith("/FUSESOC_IGNORE")
        or path == "FUSESOC_IGNORE"
    )
    if violations:
        raise AcceptanceBasisError(
            f"{BLOCK_REASON}: protected path(s) changed: {', '.join(violations)}"
        )
