"""Immutable acceptance inputs published automatically when a Ticket is enqueued."""

from __future__ import annotations

import contextlib
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

from booley.core.boundary import (
    BoundaryError,
    require_dict,
    require_int,
    require_list,
    require_str,
)
from booley.runtime.project_dir import (
    checkout_project_dir_relative_to,
    resolve_checkout_project_dir,
    runtime_dir,
)
from booley.runtime.ticket_repositories import (
    paired_project_repository,
    resolve_inner_project_repo,
)

from .acceptance_path_policy import is_static_acceptance_path
from .acceptance_targets import AcceptanceTargetBinding, validate_binding_selectors
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
        if (
            not isinstance(self.schema, int)
            or isinstance(self.schema, bool)
            or self.schema != SCHEMA_VERSION
        ):
            raise AcceptanceBasisError(f"unsupported Acceptance Path Policy {self.schema}")
        from .acceptance_targets import acceptance_control_paths

        try:
            return acceptance_control_paths(project_root)
        except (OSError, ValueError) as exc:
            raise AcceptanceBasisError(
                f"{BLOCK_REASON}: protected-input discovery failed in {project_root}: {exc}"
            ) from exc


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
    bindings: tuple[AcceptanceTargetBinding, ...] = ()
    removal_targets: tuple[str, ...] = ()
    schema: int = SCHEMA_VERSION

    def __post_init__(self) -> None:
        if (
            not isinstance(self.schema, int)
            or isinstance(self.schema, bool)
            or self.schema != SCHEMA_VERSION
        ):
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
        try:
            schema = require_int(data.get("schema"), field="acceptance_basis.schema")
        except BoundaryError as exc:
            raise AcceptanceBasisError(str(exc)) from exc
        if schema != SCHEMA_VERSION:
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

    def participant(self, role: str) -> BasisParticipant:
        try:
            return next(row for row in self.participants if row.role == role)
        except StopIteration as exc:
            raise AcceptanceBasisError(f"Acceptance Basis has no {role!r} participant") from exc

    def with_record(self, record: Mapping[str, Any]) -> AcceptanceBasis:
        bindings = tuple(_binding_from_record(row) for row in record.get("bindings", ()))
        frontmatter = record.get("ticket", {}).get("frontmatter", {})
        if not isinstance(frontmatter, Mapping):
            raise AcceptanceBasisError("Acceptance Basis record frontmatter is invalid")
        _validate_record_routing(self, frontmatter)
        on_success = frontmatter.get("on_success", {})
        removals = (
            tuple(on_success.get("remove_targets", ())) if isinstance(on_success, Mapping) else ()
        )
        return AcceptanceBasis(self.participants, bindings, removals, self.schema)


def _validate_record_routing(basis: AcceptanceBasis, frontmatter: Mapping[str, Any]) -> None:
    destination = frontmatter.get("branch")
    outer = basis.participant("outer")
    if not isinstance(destination, str) or outer.destination_ref != f"refs/heads/{destination}":
        raise AcceptanceBasisError(
            "Acceptance Basis outer destination disagrees with its committed Ticket record"
        )
    project = next((item for item in basis.participants if item.role == "project"), None)
    project_destination = frontmatter.get("project_destination_ref")
    if project is None:
        if project_destination is not None:
            raise AcceptanceBasisError(
                "Acceptance Basis record declares a project destination without a participant"
            )
        return
    if not isinstance(project_destination, str) or project.destination_ref != project_destination:
        raise AcceptanceBasisError(
            "Acceptance Basis project destination disagrees with its committed Ticket record"
        )


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
    if not valid_ticket_ref(ticket_ref):
        raise AcceptanceBasisError(f"{field}.ticket_ref is not a generation-qualified ref")
    if not valid_branch_ref(destination_ref):
        raise AcceptanceBasisError(f"{field}.destination_ref must be a full branch ref")
    return BasisParticipant(role, authoring_sha, ticket_ref, destination_ref, destination_sha)


def valid_branch_ref(value: str) -> bool:
    """Return whether *value* is a canonical full local branch ref."""
    return (
        bool(_SAFE_REF_RE.fullmatch(value))
        and not any(token in value for token in ("..", "//", "@{", "\\"))
        and not value.endswith(("/", ".", ".lock"))
    )


def valid_ticket_ref(value: str) -> bool:
    """Return whether *value* is a generation-qualified Ticket branch ref."""
    return value.startswith(TICKET_REF_PREFIX + "/") and valid_branch_ref(value)


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


def _binding_to_record(binding: AcceptanceTargetBinding) -> dict[str, str]:
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


def _binding_from_record(value: Any) -> AcceptanceTargetBinding:
    expected = {
        "flow",
        "criterion",
        "baseline_identity",
        "baseline_selector",
        "candidate_identity",
        "candidate_selector",
    }
    try:
        mapping = require_dict(value, field="Acceptance Basis binding")
        if set(mapping) != expected:
            raise BoundaryError("Acceptance Basis binding has an invalid schema")
        binding = AcceptanceTargetBinding(
            flow=require_str(mapping, "flow"),
            criterion=require_str(mapping, "criterion"),
            baseline=require_str(mapping, "baseline_identity"),
            candidate=require_str(mapping, "candidate_identity"),
            baseline_selector=require_str(mapping, "baseline_selector"),
            candidate_selector=require_str(mapping, "candidate_selector"),
        ).validate_persisted()
    except (BoundaryError, ValueError) as exc:
        raise AcceptanceBasisError(str(exc)) from exc
    return binding


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
    try:
        schema = require_int(record.get("schema"), field="Acceptance Basis record.schema")
    except BoundaryError as exc:
        raise AcceptanceBasisError(str(exc)) from exc
    if schema != RECORD_SCHEMA_VERSION:
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
        receipt = _validate_receipt(root, slug, basis, record)
        if receipt["source_sha256"] != source_sha256:
            raise AcceptanceBasisError(
                "existing Acceptance Basis receipt names a different source draft"
            )
        return receipt
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
        decoded = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise AcceptanceBasisError(f"Acceptance Basis receipt is unavailable: {path}") from exc
    try:
        actual = require_dict(decoded, field="Acceptance Basis receipt")
        receipt_schema = require_int(actual.get("schema"), field="Acceptance Basis receipt.schema")
        source_identity = require_str(actual, "source_sha256")
        operation_identity = require_str(actual, "operation_id")
    except BoundaryError as exc:
        raise AcceptanceBasisError(f"{BLOCK_REASON}: Acceptance Basis receipt mismatch") from exc
    expected = _receipt_payload(
        project_root,
        slug,
        basis,
        record,
        source_sha256=source_identity,
        operation_id=operation_identity,
    )
    identities_valid = (
        re.fullmatch(r"[0-9a-f]{64}", source_identity) is not None
        and re.fullmatch(r"[0-9a-f]{32}", operation_identity) is not None
    )
    if (
        receipt_schema != SCHEMA_VERSION
        or not identities_valid
        or actual != expected
        or canonical_json(actual) != path.read_bytes()
    ):
        raise AcceptanceBasisError(f"{BLOCK_REASON}: Acceptance Basis receipt mismatch")
    return actual


def _git_paths(repository: Path, *args: str, owner: Path | None = None) -> set[str]:
    result = subprocess.run(
        [*_worktree_git_command(repository, owner), *args],
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


def _worktree_git_command(repository: Path, owner: Path | None = None) -> list[str]:
    """Return Git arguments that survive host paths in bind-mounted worktrees."""
    dot_git = repository / ".git"
    if not dot_git.is_file():
        return ["git"]
    try:
        marker, raw_path = dot_git.read_text(encoding="utf-8").strip().split(":", 1)
    except (OSError, ValueError):
        return ["git"]
    if marker != "gitdir":
        return ["git"]
    recorded = Path(raw_path.strip())
    if not recorded.is_absolute():
        recorded = (repository / recorded).resolve()
    if recorded.is_dir():
        return ["git"]
    admin_name = recorded.name
    common_dir = _git_common_dir(owner or repository)
    if common_dir is not None:
        mounted = common_dir / "worktrees" / admin_name
        if mounted.is_dir():
            return ["git", f"--git-dir={mounted}", f"--work-tree={repository}"]
    return ["git"]


def _git_common_dir(repository: Path) -> Path | None:
    """Resolve the accessible common Git directory for a repository owner."""
    result = subprocess.run(
        ["git", "rev-parse", "--path-format=absolute", "--git-common-dir"],
        cwd=repository,
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    if result.returncode != 0 or not result.stdout.strip():
        return None
    try:
        return Path(result.stdout.strip()).resolve()
    except (OSError, RuntimeError):
        return None


def assert_inputs_unchanged(
    basis: AcceptanceBasis,
    project_root: Path | str,
    *,
    generated_reference: Path | str | None = None,
) -> None:
    """Reject tracked, staged, or untracked changes to protected acceptance inputs."""
    root = Path(project_root).resolve()
    reference = Path(generated_reference).resolve() if generated_reference is not None else None
    prefix, outer_protected, project_protected = _partition_protected_inputs(root, basis)
    project = next((row for row in basis.participants if row.role == "project"), None)
    _assert_repository_inputs_unchanged(
        root,
        basis.outer_sha,
        outer_protected,
        generated_reference=reference,
        excluded_prefixes=(prefix,) if project is not None else (),
    )
    if project is None:
        return
    local_project = root / prefix
    if (local_project / ".git").is_dir():
        project_repository = local_project
    else:
        paired = paired_project_repository(root)
        project_repository = (
            paired.worktree if paired is not None else resolve_inner_project_repo(root)
        )
    if project_repository is None:
        raise AcceptanceBasisError(f"{BLOCK_REASON}: paired project repository is unavailable")
    _assert_repository_inputs_unchanged(
        project_repository,
        project.authoring_sha,
        project_protected,
        generated_reference=reference / prefix if reference is not None else None,
        ticket_prefix=prefix,
    )


def assert_live_inputs_unchanged(
    basis: AcceptanceBasis,
    project_root: Path | str,
    reference_checkout: Path | str,
) -> None:
    """Reject protected changes in every checked-out participant Ticket ref."""
    root = Path(project_root).resolve()
    reference = Path(reference_checkout).resolve()
    prefix, outer_protected, project_protected = _partition_protected_inputs(reference, basis)
    outer = basis.participant("outer")
    outer_worktree = worktree_for_ref(root, outer.ticket_ref)
    if outer_worktree is not None:
        _assert_repository_inputs_unchanged(
            outer_worktree,
            outer.authoring_sha,
            outer_protected,
            git_owner=root,
            generated_reference=reference,
            excluded_prefixes=(prefix,) if len(basis.participants) > 1 else (),
        )
    project = next((item for item in basis.participants if item.role == "project"), None)
    if project is None:
        return
    project_owner = _project_repository(root)
    project_worktree = worktree_for_ref(project_owner, project.ticket_ref)
    if project_worktree is not None:
        _assert_repository_inputs_unchanged(
            project_worktree,
            project.authoring_sha,
            project_protected,
            git_owner=project_owner,
            generated_reference=reference / prefix,
            ticket_prefix=prefix,
        )


def _partition_protected_inputs(
    root: Path,
    basis: AcceptanceBasis,
) -> tuple[str, set[str], set[str]]:
    protected = _basis_control_paths(root, basis, PATH_POLICY.discover)
    try:
        prefix = checkout_project_dir_relative_to(root).as_posix().rstrip("/") + "/"
    except (FileNotFoundError, ValueError):
        prefix = ".booley_project/"
    outer_protected = {path for path in protected if not path.startswith(prefix)}
    project = next((row for row in basis.participants if row.role == "project"), None)
    if project is None:
        outer_protected.add((Path(prefix) / "acceptance" / "bases").as_posix())
    project_protected = {
        path.removeprefix(prefix) for path in protected if path.startswith(prefix)
    }
    project_protected.add("acceptance/bases")
    return prefix, outer_protected, project_protected


def _basis_control_paths(root: Path, basis: AcceptanceBasis, discover: Any) -> set[str]:
    """Discover protected paths from both baseline and effective composite trees."""
    try:
        current = set(discover(root))
    except (FileNotFoundError, OSError, ValueError) as exc:
        raise AcceptanceBasisError(
            f"{BLOCK_REASON}: protected-input discovery failed in {root}: {exc}"
        ) from exc
    with tempfile.TemporaryDirectory(prefix="booley-basis-controls-") as raw_directory:
        baseline = Path(raw_directory) / "outer"
        materialize_basis_checkout(root, basis, baseline)
        try:
            current.update(discover(baseline))
        except (FileNotFoundError, OSError, ValueError) as exc:
            raise AcceptanceBasisError(
                f"{BLOCK_REASON}: protected-input discovery failed in {baseline}: {exc}"
            ) from exc
    return current


def materialize_basis_checkout(
    project_root: Path | str,
    basis: AcceptanceBasis,
    destination: Path | str,
) -> Path:
    """Materialize the immutable outer and paired-project commits for inspection."""
    root = Path(project_root).resolve()
    checkout = Path(destination)
    commits = {participant.role: participant.authoring_sha for participant in basis.participants}
    return _materialize_participant_commits(root, basis, checkout, commits)


def materialize_current_ticket_checkout(
    project_root: Path | str,
    basis: AcceptanceBasis,
    destination: Path | str,
) -> Path:
    """Materialize current generation refs after validating every Basis ref."""
    root = Path(project_root).resolve()
    commits = validate_current_basis_refs(root, basis)
    return _materialize_participant_commits(root, basis, Path(destination), commits)


def validate_current_basis_refs(
    project_root: Path | str,
    basis: AcceptanceBasis,
) -> dict[str, str]:
    """Validate source and destination refs, returning pinned Ticket commits."""
    root = Path(project_root).resolve()
    commits: dict[str, str] = {}
    for participant in basis.participants:
        repository = root if participant.role == "outer" else _project_repository(root)
        commits[participant.role] = _descendant_ref_commit(
            repository,
            participant.ticket_ref,
            participant.authoring_sha,
            kind="Ticket",
            role=participant.role,
        )
    validate_destination_refs(root, basis)
    return commits


def validate_destination_refs(
    project_root: Path | str,
    basis: AcceptanceBasis,
    recorded_commits: Mapping[str, str] | None = None,
) -> None:
    """Require every destination ref to contain its recorded durable identity."""
    root = Path(project_root).resolve()
    expected = (
        recorded_commits
        if recorded_commits is not None
        else {participant.role: participant.destination_sha for participant in basis.participants}
    )
    roles = {participant.role for participant in basis.participants}
    if set(expected) != roles:
        raise AcceptanceBasisError("recorded destination commits must cover every participant")
    for participant in basis.participants:
        recorded_sha = expected[participant.role]
        if not isinstance(recorded_sha, str) or not _COMMIT_RE.fullmatch(recorded_sha):
            raise AcceptanceBasisError(
                f"recorded {participant.role} destination commit must be a full Git SHA"
            )
        repository = root if participant.role == "outer" else _project_repository(root)
        _descendant_ref_commit(
            repository,
            participant.destination_ref,
            recorded_sha,
            kind="destination",
            role=participant.role,
        )


def materialize_ticket_commits(
    project_root: Path | str,
    basis: AcceptanceBasis,
    destination: Path | str,
    commits: Mapping[str, str],
) -> Path:
    """Materialize durable Ticket commits after validating their Basis ancestry."""
    root = Path(project_root).resolve()
    expected_roles = {participant.role for participant in basis.participants}
    if set(commits) != expected_roles:
        raise AcceptanceBasisError("recorded Ticket commits must cover every Basis participant")
    validated: dict[str, str] = {}
    for participant in basis.participants:
        commit = commits[participant.role]
        if not isinstance(commit, str) or not _COMMIT_RE.fullmatch(commit):
            raise AcceptanceBasisError(
                f"recorded {participant.role} Ticket commit must be a full Git SHA"
            )
        repository = root if participant.role == "outer" else _project_repository(root)
        validated[participant.role] = _descendant_commit(
            repository,
            commit,
            participant.authoring_sha,
            role=participant.role,
        )
    return _materialize_participant_commits(root, basis, Path(destination), validated)


def validate_ticket_view(
    checkout: Path | str,
    basis: AcceptanceBasis,
    *,
    allow_generated: bool = False,
) -> list[str]:
    """Validate protected inputs and selectors in one materialized Ticket view."""
    root = Path(checkout).resolve()
    if allow_generated:
        from booley.fusesoc.core_projection import (
            CoreProjectionError,
            native_cores_ignored,
            reconcile_isolated_registry,
            reconcile_projected_cores,
        )

        try:
            reconcile_projected_cores(root)
            if native_cores_ignored(root):
                reconcile_isolated_registry(root)
        except (CoreProjectionError, OSError) as exc:
            raise AcceptanceBasisError(
                f"could not prepare generated Acceptance Basis inputs in {root}: {exc}"
            ) from exc
    assert_inputs_unchanged(basis, root, generated_reference=root if allow_generated else None)
    return validate_binding_selectors(root, basis.bindings)


def worktree_for_ref(repository: Path | str, ref: str) -> Path | None:
    """Return the checkout for one full branch ref, when it is materialized."""
    root = Path(repository).resolve()
    result = subprocess.run(
        ["git", "worktree", "list", "--porcelain"],
        cwd=root,
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    if result.returncode != 0:
        detail = result.stderr.strip() or result.stdout.strip() or "no diagnostic"
        raise AcceptanceBasisError(f"git worktree list failed in {root}: {detail}")
    records: list[tuple[Path, str | None]] = []
    worktree: Path | None = None
    branch: str | None = None
    for line in [*result.stdout.splitlines(), ""]:
        if line.startswith("worktree "):
            worktree = Path(line.removeprefix("worktree "))
        elif line.startswith("branch "):
            branch = line.removeprefix("branch ")
        elif not line:
            if worktree is not None:
                records.append((worktree, branch))
            worktree = None
            branch = None
    match = next((path for path, item_ref in records if item_ref == ref), None)
    if match is None:
        return match
    if match.exists():
        if _worktree_has_identity(match, ref, root):
            return match
        raise AcceptanceBasisError(
            f"registered worktree for {ref} at {match} could not prove its Git identity"
        )
    mounted = _mounted_worktree_path(
        root,
        match,
        records[0][0] if records else None,
        ref,
    )
    if mounted is None:
        raise AcceptanceBasisError(
            f"registered worktree for {ref} is unavailable at {match} and could not be "
            "identified in the current mount"
        )
    return mounted


def _mounted_worktree_path(
    root: Path,
    recorded: Path,
    primary: Path | None,
    ref: str,
) -> Path | None:
    """Translate host-recorded worktree paths into the current bind mount."""
    candidates: list[Path] = []
    if primary is not None:
        with contextlib.suppress(ValueError):
            candidates.append(root / recorded.relative_to(primary))
    try:
        worktrees_index = recorded.parts.index("worktrees")
    except ValueError:
        return next((candidate for candidate in candidates if candidate.exists()), None)
    suffix = Path(*recorded.parts[worktrees_index:])
    candidates.append(root / suffix)
    try:
        project_dir = resolve_checkout_project_dir(root)
    except (FileNotFoundError, ValueError):
        pass
    else:
        candidates.append(project_dir / suffix)
    matches = {
        candidate.resolve()
        for candidate in candidates
        if candidate.is_dir() and _worktree_has_identity(candidate, ref, root)
    }
    if len(matches) > 1:
        rendered = ", ".join(str(candidate) for candidate in sorted(matches))
        raise AcceptanceBasisError(
            f"registered worktree for {ref} is ambiguous in the current mount: {rendered}"
        )
    return next(iter(matches), None)


def _worktree_has_identity(candidate: Path, ref: str, owner: Path) -> bool:
    """Return whether a remapped checkout proves its top-level and branch identity."""
    command = _worktree_git_command(candidate, owner)
    top_level = subprocess.run(
        [*command, "rev-parse", "--show-toplevel"],
        cwd=candidate,
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    if top_level.returncode != 0:
        return False
    try:
        discovered = Path(top_level.stdout.strip()).resolve()
    except (OSError, RuntimeError):
        return False
    if discovered != candidate.resolve():
        return False
    branch = subprocess.run(
        [*command, "symbolic-ref", "--quiet", "HEAD"],
        cwd=candidate,
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    return branch.returncode == 0 and branch.stdout.strip() == ref


def _materialize_participant_commits(
    root: Path,
    basis: AcceptanceBasis,
    checkout: Path,
    commits: Mapping[str, str],
) -> Path:
    _clone_commit(root, checkout, commits["outer"])
    project = next((row for row in basis.participants if row.role == "project"), None)
    if project is not None:
        source = _project_repository(root)
        project_relative = checkout_project_dir_relative_to(root)
        _clone_commit(source, checkout / project_relative, commits["project"])
    return checkout


def _descendant_ref_commit(
    repository: Path,
    ref: str,
    recorded_sha: str,
    *,
    kind: str,
    role: str,
) -> str:
    result = subprocess.run(
        ["git", "rev-parse", "--verify", f"{ref}^{{commit}}"],
        cwd=repository,
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    if result.returncode != 0 or not _COMMIT_RE.fullmatch(result.stdout.strip()):
        raise AcceptanceBasisError(f"Acceptance Basis {kind} ref is unavailable: {ref}")
    return _descendant_commit(repository, result.stdout.strip(), recorded_sha, role=role, ref=ref)


def _descendant_commit(
    repository: Path,
    commit: str,
    recorded_sha: str,
    *,
    role: str,
    ref: str | None = None,
) -> str:
    ancestor = subprocess.run(
        ["git", "merge-base", "--is-ancestor", recorded_sha, commit],
        cwd=repository,
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    if ancestor.returncode != 0:
        identity = ref or commit
        raise AcceptanceBasisError(
            f"{BLOCK_REASON}: {identity} no longer descends from recorded "
            f"{role} commit {recorded_sha}"
        )
    return commit


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
    git_owner: Path | None = None,
    generated_reference: Path | None = None,
    ticket_prefix: str = "",
    excluded_prefixes: tuple[str, ...] = (),
) -> None:
    changed = _repository_changed_paths(
        repository,
        authoring_sha,
        git_owner=git_owner,
        generated_reference=generated_reference,
        excluded_prefixes=excluded_prefixes,
    )
    violations = sorted(
        path
        for path in changed
        if path in protected
        or any(path == prefix or path.startswith(prefix.rstrip("/") + "/") for prefix in protected)
        or any(prefix.startswith(path.rstrip("/") + "/") for prefix in protected)
        or is_static_acceptance_path(f"{ticket_prefix}{path}")
        or path.endswith("/FUSESOC_IGNORE")
        or path == "FUSESOC_IGNORE"
    )
    if violations:
        raise AcceptanceBasisError(
            f"{BLOCK_REASON}: protected path(s) changed: {', '.join(violations)}"
        )


def _repository_changed_paths(
    repository: Path,
    authoring_sha: str,
    *,
    git_owner: Path | None,
    generated_reference: Path | None,
    excluded_prefixes: tuple[str, ...],
) -> set[str]:
    pathspec = (
        ("--", ".", *(f":(exclude,literal){prefix.rstrip('/')}" for prefix in excluded_prefixes))
        if excluded_prefixes
        else ()
    )
    tracked_commands = (
        ("diff", "--name-only", "-z", authoring_sha, *pathspec),
        ("diff", "--cached", "--name-only", "-z", authoring_sha, *pathspec),
    )
    generated_commands = (
        ("ls-files", "--others", "--exclude-standard", "-z"),
        ("ls-files", "--others", "--ignored", "--exclude-standard", "-z"),
    )
    changed = _collect_repository_paths(repository, tracked_commands, git_owner)
    generated = _collect_repository_paths(repository, generated_commands, git_owner)
    if generated_reference is not None:
        reference_generated = _collect_repository_paths(
            generated_reference,
            generated_commands,
            None,
        )
        generated = {
            path
            for path in generated | reference_generated
            if not _same_generated_path(repository / path, generated_reference / path)
        }
    changed.update(generated)
    return {
        path
        for path in changed
        if not any(
            path.rstrip("/") == prefix.rstrip("/") or path.startswith(prefix.rstrip("/") + "/")
            for prefix in excluded_prefixes
        )
    }


def _collect_repository_paths(
    repository: Path,
    commands: tuple[tuple[str, ...], ...],
    git_owner: Path | None,
) -> set[str]:
    paths: set[str] = set()
    for command in commands:
        paths.update(_git_paths(repository, *command, owner=git_owner))
    return paths


def _same_generated_path(live: Path, reference: Path) -> bool:
    """Return whether a generated input exactly matches its prepared reference."""
    if live.is_symlink() or reference.is_symlink():
        return (
            live.is_symlink()
            and reference.is_symlink()
            and live.readlink() == reference.readlink()
        )
    if not live.is_file() or not reference.is_file():
        return False
    try:
        same_mode = (live.stat().st_mode & 0o111) == (reference.stat().st_mode & 0o111)
        if not same_mode:
            return False
        if live.read_bytes() == reference.read_bytes():
            return True
        from booley.fusesoc.core_projection import isolated_core_contents_equivalent

        return isolated_core_contents_equivalent(live, reference)
    except OSError:
        return False
