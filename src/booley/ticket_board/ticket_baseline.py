"""Resolve Ticket-owned baselines and acceptance inputs from pinned commits."""

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
from booley.core.models import TargetPlan, TargetPlanError, TargetPlanRole
from booley.runtime.project_dir import (
    PROJECT_DIR_NAME,
    checkout_project_dir_relative_to,
    resolve_checkout_project_dir,
)
from booley.ticket_board.ticket_repositories import (
    paired_project_repository,
    resolve_inner_project_repo,
)

from .acceptance_path_policy import is_static_acceptance_path
from .acceptance_targets import AcceptanceTargetBinding, validate_binding_selectors

SCHEMA_VERSION = 1
BLOCK_REASON = "acceptance-input-change-required"
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
    "target_plan",
    "on_success",
    "auto_approve",
    "synthesis",
    "baseline_tests",
    "scope_current",
    "scope_new",
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
    },
}


class TicketBaselineError(ValueError):
    """A Ticket baseline or its machine metadata is malformed."""


def authored_ticket_digest(fields: Mapping[str, Any], body: str) -> str:
    """Identify only human-authored Ticket content, excluding machine state."""
    from .criteria_markdown import strip_criteria_from_body

    payload = {
        "frontmatter": _canonical_authored_fields(fields),
        "body": strip_criteria_from_body(body).strip(),
    }
    return hashlib.sha256(canonical_json(payload)).hexdigest()


def ticket_machine_fields(
    basis: TicketBaseline,
    *,
    fields: Mapping[str, Any],
    body: str,
    generation: str,
) -> dict[str, Any]:
    """Build the Ticket's machine-only identity after authoring commits exist."""
    return ticket_machine_from_participants(
        basis.participants,
        authored_sha256=authored_ticket_digest(fields, body),
        generation=generation,
        providers=basis.providers,
    )


def ticket_machine_from_participants(
    participants: tuple[BasisParticipant, ...],
    *,
    authored_sha256: str,
    generation: str,
    providers: tuple[ProviderTargetBinding, ...] = (),
) -> dict[str, Any]:
    """Use one canonical shape for published and pre-commit Ticket metadata."""
    machine = {
        "schema": 1,
        "authored_sha256": authored_sha256,
        "generation": generation,
        "baseline": {
            row.role: {
                "commit": row.authoring_sha,
                "ticket_ref": row.ticket_ref,
                "destination_ref": row.destination_ref,
                "destination_commit": row.destination_sha,
            }
            for row in participants
        },
    }
    if providers:
        machine["providers"] = [row.as_dict() for row in providers]
    return machine


def ticket_machine_digest(machine: Mapping[str, Any]) -> str:
    """Digest machine identities without the outer commit's self-reference."""
    payload = deepcopy(dict(machine))
    payload.pop("authored_sha256", None)
    baseline = require_dict(payload.get("baseline"), field="machine.baseline")
    outer = require_dict(baseline.get("outer"), field="machine.baseline.outer")
    outer.pop("commit", None)
    baseline["outer"] = outer
    payload["baseline"] = baseline
    return hashlib.sha256(canonical_json(payload)).hexdigest()


def ticket_baseline_from_fields(fields: Mapping[str, Any], body: str) -> TicketBaseline:
    """Validate machine metadata and return its pinned repository identities."""
    if "acceptance_basis" in fields:
        raise TicketBaselineError(
            "unsupported Ticket format: recreate this Ticket without acceptance_basis"
        )
    retired = sorted(_RETIRED_FIELDS & set(fields))
    if retired:
        raise TicketBaselineError(
            "legacy Ticket fields are unsupported after the hard cutoff: " + ", ".join(retired)
        )
    from .constants import KNOWN_FIELDS, RUNTIME_FIELDS

    unknown = set(fields) - KNOWN_FIELDS - RUNTIME_FIELDS
    if unknown:
        raise TicketBaselineError(f"unsupported Ticket fields: {', '.join(sorted(unknown))}")
    basis = ticket_baseline_from_machine(fields.get("machine"))
    try:
        machine = require_dict(fields["machine"], field="machine")
    except BoundaryError as exc:
        raise TicketBaselineError(str(exc)) from exc
    if machine["authored_sha256"] != authored_ticket_digest(fields, body):
        raise TicketBaselineError(f"{BLOCK_REASON}: authored Ticket changed")
    return basis


def ticket_baseline_from_machine(value: Any) -> TicketBaseline:
    """Parse the machine-only baseline without consulting mutable Ticket content."""
    try:
        machine = require_dict(value, field="machine")
        schema = require_int(machine.get("schema"), field="machine.schema")
        authored = require_str(machine, "authored_sha256")
        generation = require_str(machine, "generation")
        baseline = require_dict(machine.get("baseline"), field="machine.baseline")
    except BoundaryError as exc:
        raise TicketBaselineError(str(exc)) from exc
    required = {"schema", "authored_sha256", "generation", "baseline"}
    if schema != 1 or not required <= set(machine) or set(machine) - required - {"providers"}:
        raise TicketBaselineError("machine has invalid fields or schema")
    if not re.fullmatch(r"[0-9a-f]{32}", generation):
        raise TicketBaselineError("machine.generation must be a 32-digit hex identifier")
    if not re.fullmatch(r"[0-9a-f]{64}", authored):
        raise TicketBaselineError("machine.authored_sha256 is invalid")
    if set(baseline) not in ({"outer"}, {"outer", "project"}):
        raise TicketBaselineError("machine.baseline requires outer and optional project")
    participants = []
    for role in sorted(baseline):
        try:
            row = require_dict(baseline[role], field=f"machine.baseline.{role}")
        except BoundaryError as exc:
            raise TicketBaselineError(str(exc)) from exc
        if set(row) != {"commit", "ticket_ref", "destination_ref", "destination_commit"}:
            raise TicketBaselineError(f"machine.baseline.{role} has invalid fields")
        participants.append(
            {
                "role": role,
                "authoring_sha": row["commit"],
                "ticket_ref": row["ticket_ref"],
                "destination_ref": row["destination_ref"],
                "destination_sha": row["destination_commit"],
            }
        )
    basis = TicketBaseline.from_mapping({"schema": 1, "participants": participants})
    raw_providers = machine.get("providers", [])
    if not isinstance(raw_providers, list):
        raise TicketBaselineError("machine.providers must be a list")
    providers = tuple(provider_binding_from_mapping(row) for row in raw_providers)
    if providers != tuple(sorted(set(providers))):
        raise TicketBaselineError("machine.providers must be sorted and unique")
    return TicketBaseline(basis.participants, providers=providers, machine=dict(machine))


def validate_ticket_commit_trailers(
    project_root: Path | str,
    slug: str,
    basis: TicketBaseline,
    machine: Mapping[str, Any],
) -> None:
    """Check the independent Git anchor for the Ticket's authored and machine identity."""
    result = subprocess.run(
        ["git", "show", "-s", "--format=%B", basis.outer_sha],
        cwd=project_root,
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    if result.returncode != 0:
        raise TicketBaselineError(f"{BLOCK_REASON}: outer authoring commit is unavailable")
    trailers = dict(
        line.split(": ", 1)
        for line in result.stdout.splitlines()
        if line.startswith("Booley-") and ": " in line
    )
    expected = {
        "Booley-Ticket-Slug": slug,
        "Booley-Authored-SHA256": machine["authored_sha256"],
        "Booley-Machine-SHA256": ticket_machine_digest(machine),
    }
    if any(trailers.get(key) != value for key, value in expected.items()):
        raise TicketBaselineError(f"{BLOCK_REASON}: Ticket commit identity changed")


def requires_return_to_draft(fields: Mapping[str, Any]) -> bool:
    """Return whether Ticket baseline drift forbids direct requeue or execution."""
    return fields.get("blocked_reason") == BLOCK_REASON


@dataclass(frozen=True)
class AcceptancePathPolicy:
    """Versioned discovery policy for schema-1 acceptance control paths."""

    schema: int = SCHEMA_VERSION

    def discover(
        self,
        project_root: Path | str,
        *,
        git_owner: Path | None = None,
    ) -> tuple[str, ...]:
        if (
            not isinstance(self.schema, int)
            or isinstance(self.schema, bool)
            or self.schema != SCHEMA_VERSION
        ):
            raise TicketBaselineError(f"unsupported Acceptance Path Policy {self.schema}")
        from .acceptance_targets import acceptance_control_paths

        try:
            root = Path(project_root)
            command = tuple(_worktree_git_command(root, git_owner)) if git_owner else ("git",)
            return acceptance_control_paths(root, git_command=command)
        except (OSError, ValueError) as exc:
            raise TicketBaselineError(
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


@dataclass(frozen=True, order=True)
class ProviderTargetBinding:
    """One generation-pinned Target exported by a dependency Ticket."""

    provider: str
    ticket_generation: str
    target: str
    role: str
    surface_sha256: str

    def as_dict(self) -> dict[str, str]:
        return {
            "provider": self.provider,
            "ticket_generation": self.ticket_generation,
            "target": self.target,
            "role": self.role,
            "surface_sha256": self.surface_sha256,
        }


@dataclass(frozen=True)
class TicketBaseline:
    """Resolved baseline and Target inputs for one validated Ticket."""

    participants: tuple[BasisParticipant, ...]
    bindings: tuple[AcceptanceTargetBinding, ...] = ()
    removal_targets: tuple[str, ...] = ()
    schema: int = SCHEMA_VERSION
    target_plan: TargetPlan | None = None
    providers: tuple[ProviderTargetBinding, ...] = ()
    machine: dict[str, Any] | None = None

    def __post_init__(self) -> None:
        if (
            not isinstance(self.schema, int)
            or isinstance(self.schema, bool)
            or self.schema != SCHEMA_VERSION
        ):
            raise TicketBaselineError(
                f"ticket baseline.schema must be {SCHEMA_VERSION}, got {self.schema!r}"
            )
        roles = tuple(row.role for row in self.participants)
        if roles != tuple(sorted(set(roles))):
            raise TicketBaselineError("ticket baseline.participants must be sorted by unique role")
        if "outer" not in roles:
            raise TicketBaselineError("ticket baseline.participants requires an outer participant")

    @classmethod
    def from_mapping(cls, value: Any) -> TicketBaseline:
        """Validate the small internal participant representation."""
        try:
            data = require_dict(value, field="ticket baseline")
        except BoundaryError as exc:
            raise TicketBaselineError(str(exc)) from exc
        if set(data) != {"schema", "participants"}:
            raise TicketBaselineError(
                "ticket baseline must contain exactly schema and participants"
            )
        try:
            schema = require_int(data.get("schema"), field="ticket baseline.schema")
        except BoundaryError as exc:
            raise TicketBaselineError(str(exc)) from exc
        if schema != SCHEMA_VERSION:
            raise TicketBaselineError(
                f"ticket baseline.schema must be {SCHEMA_VERSION}, got {data.get('schema')!r}"
            )
        try:
            rows = require_list(data.get("participants"), field="ticket baseline.participants")
        except BoundaryError as exc:
            raise TicketBaselineError(str(exc)) from exc
        return cls(tuple(_parse_participant(row, index) for index, row in enumerate(rows)))

    def as_dict(self) -> dict[str, Any]:
        return {
            "schema": self.schema,
            "participants": [row.as_dict() for row in self.participants],
        }

    @property
    def basis_id(self) -> str:
        return hashlib.sha256(canonical_json(self.as_dict())).hexdigest()

    def ticket_identity(self) -> dict[str, Any]:
        """Return the Ticket-owned identity stamped into acceptance evidence."""
        if self.machine is None:
            raise TicketBaselineError("Ticket machine metadata is unavailable")
        return deepcopy(self.machine)

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
            raise TicketBaselineError(f"Ticket baseline has no {role!r} participant") from exc


def _validate_ticket_routing(basis: TicketBaseline, frontmatter: Mapping[str, Any]) -> None:
    destination = frontmatter.get("branch")
    outer = basis.participant("outer")
    if not isinstance(destination, str) or outer.destination_ref != f"refs/heads/{destination}":
        raise TicketBaselineError(
            "Ticket baseline outer destination disagrees with its authored branch"
        )
    project = next((item for item in basis.participants if item.role == "project"), None)
    project_destination = frontmatter.get("project_destination_ref")
    if project is None:
        if project_destination is not None:
            raise TicketBaselineError(
                "Ticket declares a project destination without a baseline participant"
            )
        return
    if not isinstance(project_destination, str) or project.destination_ref != project_destination:
        raise TicketBaselineError(
            "Ticket baseline project destination disagrees with its authored branch"
        )


def _parse_participant(value: Any, index: int) -> BasisParticipant:
    field = f"ticket baseline.participants[{index}]"
    try:
        row = require_dict(value, field=field)
    except BoundaryError as exc:
        raise TicketBaselineError(str(exc)) from exc
    expected = {"role", "authoring_sha", "ticket_ref", "destination_ref", "destination_sha"}
    if set(row) != expected:
        raise TicketBaselineError(f"{field} must contain exactly {', '.join(sorted(expected))}")
    try:
        role = require_str(row, "role").strip()
        authoring_sha = require_str(row, "authoring_sha").strip().lower()
        ticket_ref = require_str(row, "ticket_ref").strip()
        destination_ref = require_str(row, "destination_ref").strip()
        destination_sha = require_str(row, "destination_sha").strip().lower()
    except BoundaryError as exc:
        raise TicketBaselineError(f"{field}: {exc}") from exc
    if role not in {"outer", "project"}:
        raise TicketBaselineError(f"{field}.role must be outer or project")
    if not _COMMIT_RE.fullmatch(authoring_sha) or not _COMMIT_RE.fullmatch(destination_sha):
        raise TicketBaselineError(f"{field} commit identities must be full Git SHAs")
    if not valid_ticket_ref(ticket_ref):
        raise TicketBaselineError(f"{field}.ticket_ref is not a generation-qualified ref")
    if not valid_branch_ref(destination_ref):
        raise TicketBaselineError(f"{field}.destination_ref must be a full branch ref")
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
        raise TicketBaselineError("on_success must be a mapping")
    configured = OnSuccess.from_dict(dict(raw_on_success))
    errors = configured.validate()
    if errors:
        raise TicketBaselineError(errors[0])
    canonical["on_success"] = {
        "destination": configured.destination,
        "merge": configured.merge,
        "cleanup": configured.cleanup,
        "triage_report": configured.triage_report,
    }
    if "target_plan" in canonical:
        try:
            plan = TargetPlan.from_value(canonical["target_plan"])
        except TargetPlanError as exc:
            raise TicketBaselineError(str(exc)) from exc
        canonical["target_plan"] = plan.as_list()
    return canonical


def provider_binding_from_mapping(value: Any) -> ProviderTargetBinding:
    """Parse and validate one persisted provider Target binding."""
    expected = {"provider", "ticket_generation", "target", "role", "surface_sha256"}
    try:
        mapping = require_dict(value, field="Ticket baseline provider binding")
        if set(mapping) != expected:
            raise BoundaryError("Ticket baseline provider binding has an invalid schema")
        binding = ProviderTargetBinding(
            provider=require_str(mapping, "provider").strip(),
            ticket_generation=require_str(mapping, "ticket_generation").strip(),
            target=require_str(mapping, "target").strip(),
            role=require_str(mapping, "role").strip(),
            surface_sha256=require_str(mapping, "surface_sha256").strip(),
        )
    except BoundaryError as exc:
        raise TicketBaselineError(str(exc)) from exc
    if not binding.provider or not binding.target:
        raise TicketBaselineError("Ticket baseline provider names must be non-empty")
    if binding.role not in {TargetPlanRole.PERSISTENT.value, TargetPlanRole.REPLACEMENT.value}:
        raise TicketBaselineError("Ticket baseline provider role is not exportable")
    if not re.fullmatch(r"[0-9a-f]{32}", binding.ticket_generation):
        raise TicketBaselineError("machine provider ticket_generation is invalid")
    if not re.fullmatch(r"[0-9a-f]{64}", binding.surface_sha256):
        raise TicketBaselineError("Ticket baseline provider surface_sha256 is invalid")
    return binding


def selector_matches_canonical(authored: str, canonical: str) -> bool:
    authored_qualifier, separator, authored_target = authored.rpartition("#")
    if not separator:
        authored_target = authored
        authored_qualifier = ""
    canonical_qualifier, separator, canonical_target = canonical.rpartition("#")
    if not separator or authored_target != canonical_target:
        return False
    if not authored_qualifier:
        return True

    def identity_segments(value: str) -> list[str]:
        parts = value.split(":")
        return parts[:3] if len(parts) >= 3 else parts

    authored_segments = identity_segments(authored_qualifier)
    canonical_segments = identity_segments(canonical_qualifier)
    return (
        len(authored_segments) <= len(canonical_segments)
        and canonical_segments[-len(authored_segments) :] == authored_segments
    )


def _target_plan_removals(plan: TargetPlan | None) -> tuple[str, ...]:
    if plan is None:
        return ()
    removals = {
        entry.target if entry.role is TargetPlanRole.EPHEMERAL else entry.replaces
        for entry in plan.entries
        if entry.role is not TargetPlanRole.PERSISTENT
    }
    return tuple(sorted(removals))


def load_ticket_baseline(
    project_root: Path | str, slug: str, fields: Mapping[str, Any], body: str | None = None
) -> TicketBaseline:
    """Resolve Ticket-owned acceptance inputs from pinned commits."""
    retired = sorted(_RETIRED_FIELDS & set(fields))
    if retired:
        raise TicketBaselineError(
            "legacy Target Contract tickets are unsupported after the hard cutoff; "
            f"remove or recreate fields: {', '.join(retired)}"
        )
    if body is None:
        raise TicketBaselineError("executable Ticket body is required")
    basis = ticket_baseline_from_fields(fields, body)
    machine = require_dict(fields["machine"], field="machine")
    validate_ticket_commit_trailers(project_root, slug, basis, machine)
    _validate_ticket_routing(basis, fields)
    from .acceptance_targets import canonical_acceptance_bindings, criterion_targets
    from .target_plan import TargetPlanValidationError, canonical_target_plan

    with tempfile.TemporaryDirectory(prefix="booley-ticket-baseline-") as directory:
        checkout = materialize_basis_checkout(project_root, basis, Path(directory) / "checkout")
        from booley.fusesoc.core_projection import (
            native_cores_ignored,
            reconcile_isolated_registry,
            reconcile_projected_cores,
        )

        reconcile_projected_cores(checkout)
        if native_cores_ignored(checkout):
            reconcile_isolated_registry(checkout)
        try:
            plan = canonical_target_plan(fields, checkout) if fields.get("target_plan") else None
        except TargetPlanValidationError as exc:
            raise TicketBaselineError(str(exc)) from exc
        bindings = canonical_acceptance_bindings(
            checkout, criterion_targets(fields.get("criteria"))
        )
    return TicketBaseline(
        basis.participants,
        bindings,
        _target_plan_removals(plan),
        basis.schema,
        plan,
        basis.providers,
        machine=dict(machine),
    )


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
        raise TicketBaselineError(
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
    basis: TicketBaseline,
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
        raise TicketBaselineError(f"{BLOCK_REASON}: paired project repository is unavailable")
    _assert_repository_inputs_unchanged(
        project_repository,
        project.authoring_sha,
        project_protected,
        generated_reference=reference / prefix if reference is not None else None,
        ticket_prefix=prefix,
    )


def assert_live_inputs_unchanged(
    basis: TicketBaseline,
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
        recorded = _recorded_worktree_path(root, outer.ticket_ref)
        if recorded is None:
            raise TicketBaselineError(
                f"registered worktree for {outer.ticket_ref} disappeared during validation"
            )
        _assert_repository_inputs_unchanged(
            outer_worktree,
            outer.authoring_sha,
            outer_protected,
            git_owner=root,
            generated_reference=reference,
            generated_checkout_root=recorded,
            excluded_prefixes=(prefix,) if len(basis.participants) > 1 else (),
        )
    project = next((item for item in basis.participants if item.role == "project"), None)
    if project is None:
        return
    project_owner = _project_repository(root)
    project_worktree = worktree_for_ref(project_owner, project.ticket_ref)
    if project_worktree is not None:
        recorded = _recorded_worktree_path(project_owner, project.ticket_ref)
        if recorded is None:
            raise TicketBaselineError(
                f"registered worktree for {project.ticket_ref} disappeared during validation"
            )
        _assert_repository_inputs_unchanged(
            project_worktree,
            project.authoring_sha,
            project_protected,
            git_owner=project_owner,
            generated_reference=reference / prefix,
            generated_checkout_root=recorded.parent,
            ticket_prefix=prefix,
        )


def assert_candidate_inputs_unchanged(
    basis: TicketBaseline,
    project_root: Path | str,
    live_checkout: Path | str,
    generated_reference: Path | str,
) -> None:
    """Reject protected live changes, allowing only matching generated inputs."""
    root = Path(project_root).resolve()
    live = Path(live_checkout).resolve()
    reference = Path(generated_reference).resolve()
    prefix = _project_path_prefix(reference)
    outer = basis.participant("outer")
    outer_recorded = _require_participant_worktree(root, outer, live)
    project = next((item for item in basis.participants if item.role == "project"), None)
    project_state = _candidate_project_worktree(root, live, prefix, project)
    _prefix, outer_protected, project_protected = _candidate_protected_inputs(
        live,
        reference,
        basis,
        git_owner=root,
    )
    _assert_repository_inputs_unchanged(
        live,
        outer.authoring_sha,
        outer_protected,
        git_owner=root,
        generated_reference=reference,
        generated_checkout_root=outer_recorded,
        excluded_prefixes=(prefix,) if project is not None else (),
    )
    if project is None:
        return
    assert project_state is not None
    project_owner, project_live, project_recorded = project_state
    _assert_repository_inputs_unchanged(
        project_live,
        project.authoring_sha,
        project_protected,
        git_owner=project_owner,
        generated_reference=reference / prefix,
        generated_checkout_root=project_recorded.parent,
        ticket_prefix=prefix,
    )


def _candidate_project_worktree(
    root: Path,
    live: Path,
    prefix: str,
    participant: BasisParticipant | None,
) -> tuple[Path, Path, Path] | None:
    if participant is None:
        return None
    owner = _project_repository(root)
    candidate = live / prefix.rstrip("/")
    recorded = _require_participant_worktree(owner, participant, candidate)
    return owner, candidate, recorded


def _require_participant_worktree(
    owner: Path,
    participant: BasisParticipant,
    candidate: Path,
) -> Path:
    expected = worktree_for_ref(owner, participant.ticket_ref)
    if expected is None:
        raise TicketBaselineError(
            f"{BLOCK_REASON}: no registered worktree for {participant.ticket_ref}"
        )
    if not _same_worktree_directory(candidate, expected):
        raise TicketBaselineError(
            f"{BLOCK_REASON}: live checkout {candidate} is not the registered "
            f"worktree for {participant.ticket_ref}"
        )
    recorded = _recorded_worktree_path(owner, participant.ticket_ref)
    if recorded is None:
        raise TicketBaselineError(
            f"{BLOCK_REASON}: registered worktree for {participant.ticket_ref} disappeared"
        )
    return recorded


def _partition_protected_inputs(
    root: Path,
    basis: TicketBaseline,
) -> tuple[str, set[str], set[str]]:
    protected = _basis_control_paths(root, basis, PATH_POLICY.discover)
    return _partition_discovered_inputs(root, basis, protected)


def _candidate_protected_inputs(
    live: Path,
    reference: Path,
    basis: TicketBaseline,
    *,
    git_owner: Path,
) -> tuple[str, set[str], set[str]]:
    protected = set(PATH_POLICY.discover(live, git_owner=git_owner))
    protected.update(PATH_POLICY.discover(reference))
    return _partition_discovered_inputs(reference, basis, protected)


def _partition_discovered_inputs(
    root: Path,
    basis: TicketBaseline,
    protected: set[str],
) -> tuple[str, set[str], set[str]]:
    prefix = _project_path_prefix(root)
    outer_protected = {path for path in protected if not path.startswith(prefix)}
    project_protected = {
        path.removeprefix(prefix) for path in protected if path.startswith(prefix)
    }
    return prefix, outer_protected, project_protected


def _project_path_prefix(root: Path) -> str:
    try:
        return checkout_project_dir_relative_to(root).as_posix().rstrip("/") + "/"
    except (FileNotFoundError, ValueError):
        return f"{PROJECT_DIR_NAME}/"


def _basis_control_paths(root: Path, basis: TicketBaseline, discover: Any) -> set[str]:
    """Discover protected paths from both baseline and effective composite trees."""
    try:
        current = set(discover(root))
    except (FileNotFoundError, OSError, ValueError) as exc:
        raise TicketBaselineError(
            f"{BLOCK_REASON}: protected-input discovery failed in {root}: {exc}"
        ) from exc
    with tempfile.TemporaryDirectory(prefix="booley-basis-controls-") as raw_directory:
        baseline = Path(raw_directory) / "outer"
        materialize_basis_checkout(root, basis, baseline)
        try:
            current.update(discover(baseline))
        except (FileNotFoundError, OSError, ValueError) as exc:
            raise TicketBaselineError(
                f"{BLOCK_REASON}: protected-input discovery failed in {baseline}: {exc}"
            ) from exc
    return current


def materialize_basis_checkout(
    project_root: Path | str,
    basis: TicketBaseline,
    destination: Path | str,
) -> Path:
    """Materialize the immutable outer and paired-project commits for inspection."""
    root = Path(project_root).resolve()
    checkout = Path(destination)
    commits = {participant.role: participant.authoring_sha for participant in basis.participants}
    return _materialize_participant_commits(root, basis, checkout, commits)


def materialize_current_ticket_checkout(
    project_root: Path | str,
    basis: TicketBaseline,
    destination: Path | str,
) -> Path:
    """Materialize current generation refs after validating every Basis ref."""
    root = Path(project_root).resolve()
    commits = validate_current_basis_refs(root, basis)
    return _materialize_participant_commits(root, basis, Path(destination), commits)


def validate_current_basis_refs(
    project_root: Path | str,
    basis: TicketBaseline,
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
    basis: TicketBaseline,
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
        raise TicketBaselineError("recorded destination commits must cover every participant")
    for participant in basis.participants:
        recorded_sha = expected[participant.role]
        if not isinstance(recorded_sha, str) or not _COMMIT_RE.fullmatch(recorded_sha):
            raise TicketBaselineError(
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
    basis: TicketBaseline,
    destination: Path | str,
    commits: Mapping[str, str],
) -> Path:
    """Materialize durable Ticket commits after validating their Basis ancestry."""
    root = Path(project_root).resolve()
    expected_roles = {participant.role for participant in basis.participants}
    if set(commits) != expected_roles:
        raise TicketBaselineError("recorded Ticket commits must cover every Basis participant")
    validated: dict[str, str] = {}
    for participant in basis.participants:
        commit = commits[participant.role]
        if not isinstance(commit, str) or not _COMMIT_RE.fullmatch(commit):
            raise TicketBaselineError(
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
    basis: TicketBaseline,
) -> list[str]:
    """Validate protected inputs and selectors in one prepared Ticket view."""
    root = Path(checkout).resolve()
    assert_inputs_unchanged(basis, root, generated_reference=root)
    return validate_binding_selectors(root, basis.bindings)


def _worktree_records(repository: Path) -> tuple[tuple[Path, str | None], ...]:
    result = subprocess.run(
        ["git", "worktree", "list", "--porcelain"],
        cwd=repository,
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    if result.returncode != 0:
        detail = result.stderr.strip() or result.stdout.strip() or "no diagnostic"
        raise TicketBaselineError(f"git worktree list failed in {repository}: {detail}")
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
    return tuple(records)


def _recorded_worktree_path(repository: Path, ref: str) -> Path | None:
    return next(
        (path for path, item_ref in _worktree_records(repository) if item_ref == ref),
        None,
    )


def worktree_for_ref(repository: Path | str, ref: str) -> Path | None:
    """Return the checkout for one full branch ref, when it is materialized."""
    root = Path(repository).resolve()
    records = _worktree_records(root)
    match = next((path for path, item_ref in records if item_ref == ref), None)
    if match is None:
        return match
    if match.exists():
        if _worktree_has_identity(match, ref, root):
            return match
        raise TicketBaselineError(
            f"registered worktree for {ref} at {match} could not prove its Git identity"
        )
    mounted = _mounted_worktree_path(
        root,
        match,
        records[0][0] if records else None,
        ref,
    )
    if mounted is None:
        raise TicketBaselineError(
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
    matches: list[Path] = []
    for candidate in candidates:
        if not candidate.is_dir() or not _worktree_has_identity(candidate, ref, root):
            continue
        resolved = candidate.resolve()
        if not any(_same_worktree_directory(resolved, match) for match in matches):
            matches.append(resolved)
    if len(matches) > 1:
        rendered = ", ".join(str(candidate) for candidate in sorted(matches))
        raise TicketBaselineError(
            f"registered worktree for {ref} is ambiguous in the current mount: {rendered}"
        )
    return matches[0] if matches else None


def _same_worktree_directory(left: Path, right: Path) -> bool:
    """Recognize multiple bind-mounted names for one worktree directory."""
    try:
        return left.samefile(right)
    except OSError:
        return left == right


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
    basis: TicketBaseline,
    checkout: Path,
    commits: Mapping[str, str],
) -> Path:
    _clone_commit(root, checkout, commits["outer"])
    project = next((row for row in basis.participants if row.role == "project"), None)
    if project is not None:
        source = _project_repository(root)
        project_relative = checkout_project_dir_relative_to(root)
        _clone_commit(source, checkout / project_relative, commits["project"])
    from booley.runtime.submodule_materialization import (
        SubmoduleMaterializationError,
        materialize_project_submodules,
    )

    try:
        materialize_project_submodules(root, checkout)
    except SubmoduleMaterializationError as exc:
        raise TicketBaselineError(
            f"could not materialize Ticket baseline submodules offline: {exc}"
        ) from exc
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
        raise TicketBaselineError(f"Ticket baseline {kind} ref is unavailable: {ref}")
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
        raise TicketBaselineError(
            f"{BLOCK_REASON}: {identity} no longer descends from recorded "
            f"{role} commit {recorded_sha}"
        )
    return commit


def _project_repository(root: Path) -> Path:
    paired = paired_project_repository(root)
    repository = paired.worktree if paired is not None else resolve_inner_project_repo(root)
    if repository is None:
        raise TicketBaselineError(f"{BLOCK_REASON}: paired project repository is unavailable")
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
        raise TicketBaselineError(f"could not materialize Ticket baseline: {clone.stderr.strip()}")
    checkout = subprocess.run(
        ["git", "checkout", "--detach", commit],
        cwd=destination,
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    if checkout.returncode != 0:
        raise TicketBaselineError(
            f"could not materialize Ticket baseline commit {commit}: {checkout.stderr.strip()}"
        )


def _assert_repository_inputs_unchanged(
    repository: Path,
    authoring_sha: str,
    protected: set[str],
    *,
    git_owner: Path | None = None,
    generated_reference: Path | None = None,
    generated_checkout_root: Path | None = None,
    ticket_prefix: str = "",
    excluded_prefixes: tuple[str, ...] = (),
    include_reference_only_generated: bool = True,
) -> None:
    changed = _repository_changed_paths(
        repository,
        authoring_sha,
        git_owner=git_owner,
        generated_reference=generated_reference,
        generated_checkout_root=generated_checkout_root,
        excluded_prefixes=excluded_prefixes,
        include_reference_only_generated=include_reference_only_generated,
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
        raise TicketBaselineError(
            f"{BLOCK_REASON}: protected path(s) changed: {', '.join(violations)}"
        )


def _repository_changed_paths(
    repository: Path,
    authoring_sha: str,
    *,
    git_owner: Path | None,
    generated_reference: Path | None,
    generated_checkout_root: Path | None,
    excluded_prefixes: tuple[str, ...],
    include_reference_only_generated: bool = True,
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
        candidates = (
            generated | reference_generated if include_reference_only_generated else generated
        )
        generated = {
            path
            for path in candidates
            if not _same_generated_path(
                repository / path,
                generated_reference / path,
                live_checkout_root=generated_checkout_root,
            )
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


def _same_generated_path(
    live: Path,
    reference: Path,
    *,
    live_checkout_root: Path | None = None,
) -> bool:
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

        return isolated_core_contents_equivalent(
            live,
            reference,
            left_checkout_root=live_checkout_root,
        )
    except OSError:
        return False
