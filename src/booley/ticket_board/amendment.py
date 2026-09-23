"""Human-only, recoverable publication of blocked Ticket amendments."""

from __future__ import annotations

import hashlib
import json
import os
import secrets
import subprocess
import tempfile
from copy import deepcopy
from dataclasses import asdict
from pathlib import Path
from typing import Any

import yaml

from booley.runtime.project_dir import runtime_dir
from booley.ticket_board.criteria_projection import project_ticket_criteria
from booley.ticket_board.ticket_repositories import resolve_inner_project_repo

from .acceptance_path_policy import is_static_acceptance_path
from .amendment_proposal import AmendmentProposal
from .amendment_v2 import build_v2_amendment_proposal
from .basis_publication import load_basis_publication
from .git_status import GitStatusEntry, parse_porcelain_v1_z
from .logs import PROGRESS_DEFAULTS, load_progress, save_progress
from .paths import existing_runtime_file, human_log_file, ticket_log_dir
from .persistence import atomic_replace_bytes, atomic_write_once
from .scanner import find_ticket_file
from .ticket_baseline import (
    PATH_POLICY,
    BasisParticipant,
    TicketBaseline,
    assert_live_inputs_unchanged,
    canonical_json,
    load_ticket_baseline_from_document,
    materialize_basis_checkout,
    ticket_baseline_from_machine,
    ticket_machine_digest,
    ticket_machine_from_spec,
    validate_current_basis_refs,
    worktree_for_ref,
)
from .ticket_document import (
    TicketDocument,
    convert_ticket_document,
    ticket_conversion_context,
)
from .validation import _scope_contains_path, validate_ticket_spec


class AmendmentError(RuntimeError):
    """An amendment cannot be previewed or safely published."""


def _git(
    repository: Path, *args: str, index: Path | None = None, data: bytes | None = None
) -> str:
    env = os.environ.copy()
    if index is not None:
        env["GIT_INDEX_FILE"] = str(index)
    env.setdefault("GIT_AUTHOR_NAME", "Booley Ticket Board")
    env.setdefault("GIT_AUTHOR_EMAIL", "board@booley.invalid")
    env.setdefault("GIT_COMMITTER_NAME", "Booley Ticket Board")
    env.setdefault("GIT_COMMITTER_EMAIL", "board@booley.invalid")
    try:
        result = subprocess.run(
            ["git", *args],
            cwd=repository,
            env=env,
            input=data,
            capture_output=True,
            timeout=120,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise AmendmentError(f"Git inspection failed in {repository}: {exc}") from exc
    if result.returncode:
        detail = result.stderr.decode(errors="replace").strip()
        raise AmendmentError(f"git {' '.join(args)} failed in {repository}: {detail}")
    return result.stdout.decode(errors="replace").rstrip("\n")


def _journal_path(root: Path, slug: str) -> Path:
    return runtime_dir(root) / "acceptance" / "amendments" / f"{slug}.json"


def pending_amendment(root: Path, slug: str) -> dict[str, Any] | None:
    """Read the recoverable current operation, if one exists."""
    try:
        path = _journal_path(root, slug)
    except FileNotFoundError:
        return None
    if not path.exists():
        return None
    try:
        row = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise AmendmentError(f"amendment journal is unreadable: {path}") from exc
    if (
        not isinstance(row, dict)
        or row.get("schema") != 1
        or row.get("slug") != slug
        or not isinstance(row.get("operation_id"), str)
        or len(row["operation_id"]) != 32
    ):
        raise AmendmentError("amendment journal has invalid identity")
    return row


def _write_journal(root: Path, row: dict[str, Any]) -> None:
    atomic_replace_bytes(_journal_path(root, row["slug"]), canonical_json(row))


def _repositories(root: Path, basis: TicketBaseline) -> dict[str, tuple[Path, Path]]:
    result: dict[str, tuple[Path, Path]] = {}
    project = resolve_inner_project_repo(root)
    for participant in basis.participants:
        owner = root if participant.role == "outer" else project
        if owner is None:
            raise AmendmentError("paired Project repository is unavailable")
        checkout = worktree_for_ref(owner, participant.ticket_ref)
        if checkout is None or not checkout.is_dir():
            raise AmendmentError(f"Ticket Workspace for {participant.role} is unavailable")
        result[participant.role] = (owner, checkout)
    return result


def _status_snapshot(
    root: Path,
    basis: TicketBaseline,
    scope: list[str],
    repositories: dict[str, tuple[Path, Path]],
) -> dict[str, Any]:
    snapshots: dict[str, Any] = {}
    protected = set(PATH_POLICY.discover(root))
    for role, (_owner, checkout) in repositories.items():
        head = _git(checkout, "rev-parse", "HEAD")
        if _git(checkout, "ls-files", "-u"):
            raise AmendmentError(f"{role} Ticket Workspace has an unresolved Git conflict")
        raw = _git(checkout, "status", "--porcelain=v1", "-z", "--untracked-files=all")
        entries = parse_porcelain_v1_z(raw)
        files: dict[str, str | None] = {}
        for entry in entries:
            _check_checkpoint_path(role, entry, scope, protected, checkout)
            path = checkout / entry.path
            files[entry.path] = (
                hashlib.sha256(path.read_bytes()).hexdigest() if path.is_file() else None
            )
        snapshots[role] = {
            "head": head,
            "index_sha256": hashlib.sha256(
                _git(checkout, "ls-files", "-s", "-z").encode()
            ).hexdigest(),
            "entries": [asdict(item) for item in entries],
            "files": files,
        }
    return snapshots


def _check_checkpoint_path(
    role: str, entry: GitStatusEntry, scope: list[str], protected: set[str], checkout: Path
) -> None:
    for local in (entry.path, entry.source_path):
        if local is None:
            continue
        composite = f".booley_project/{local}" if role == "project" else local
        if (
            not _scope_contains_path({"scope": scope}, composite)
            or is_static_acceptance_path(composite)
            or composite in protected
            or any(composite.startswith(path.rstrip("/") + "/") for path in protected)
        ):
            raise AmendmentError(f"dirty source {composite!r} is outside prior Scope or protected")
        if (checkout / local).is_symlink():
            raise AmendmentError(f"dirty source {composite!r} is a symlink")


def _render_ticket(fields: dict[str, Any], body: str) -> str:
    return "---\n" + yaml.safe_dump(fields, sort_keys=False, allow_unicode=True) + "---\n" + body


def _convert_ticket(root: Path, slug: str, source: str) -> TicketDocument:
    with ticket_conversion_context(root, slug, "executable") as context:
        converted = convert_ticket_document(source, context)
    if converted.document is None:
        detail = "; ".join(item.message for item in converted.diagnostics)
        raise AmendmentError(f"invalid amended Ticket: {detail}")
    return converted.document


def _inspection(tio: Any, slug: str, request: Any) -> tuple[dict[str, Any], AmendmentProposal]:
    root = Path(tio._project_root).resolve()
    ticket, status = find_ticket_file(tio.tickets_dir, slug)
    if ticket is None or status != "blocked":
        raise AmendmentError(f"Ticket {slug!r} must be blocked to amend")
    source = ticket.read_bytes()
    document = _convert_ticket(root, slug, source.decode("utf-8"))
    fields, body = dict(document.spec.fields), document.spec.body
    basis = load_ticket_baseline_from_document(root, slug, document)
    heads = validate_current_basis_refs(root, basis)
    proposal = _validated_proposal(root, slug, basis, document, request)
    revised_document = _converted_proposal(root, slug, document, proposal)
    repositories = _repositories(root, basis)
    prior_scope = fields.get("scope", [])
    source_state = _status_snapshot(root, basis, prior_scope, repositories)
    if {role: value["head"] for role, value in source_state.items()} != heads:
        raise AmendmentError("Ticket refs changed during amendment preview")
    state_path = existing_runtime_file(tio.logs_dir, slug, "booley_state.json")
    _preflight_state(state_path, proposal)
    state_digest = (
        hashlib.sha256(state_path.read_bytes()).hexdigest() if state_path.exists() else ""
    )
    current_results = _preview_evidence(
        state_path,
        proposal,
        basis,
        repositories["outer"][1],
        project_ticket_criteria(revised_document.spec).params,
    )
    preview = {
        "schema": 1,
        "slug": slug,
        "ticket_sha256": hashlib.sha256(source).hexdigest(),
        "basis": basis.as_dict(),
        "old_machine": basis.ticket_identity(),
        "source_state": source_state,
        "prior_scope": prior_scope,
        "state_sha256": state_digest,
        "revised_fields": dict(revised_document.spec.fields),
        "body": body,
        "reason": proposal.reason,
        "actor": proposal.actor,
        "feedback": proposal.feedback,
        "changes": [asdict(item) for item in proposal.changes],
        "scope_added": list(proposal.scope_added),
        "current_results": current_results,
        "mandatory_after": sum(row.mandatory for row in revised_document.spec.criteria),
    }
    preview["digest"] = hashlib.sha256(canonical_json(preview)).hexdigest()
    return preview, proposal


def _converted_proposal(
    root: Path, slug: str, document: TicketDocument, proposal: AmendmentProposal
) -> TicketDocument:
    candidate = {**proposal.fields, **document.generated}
    return _convert_ticket(root, slug, _render_ticket(candidate, document.spec.body))


def _validated_proposal(
    root: Path, slug: str, basis: TicketBaseline, document: TicketDocument, request: Any
) -> AmendmentProposal:
    spec = document.spec
    with tempfile.TemporaryDirectory(prefix="booley-amend-preview-") as directory:
        reference = materialize_basis_checkout(root, basis, Path(directory) / "basis")
        assert_live_inputs_unchanged(basis, root, reference)
        with ticket_conversion_context(root, slug, "executable") as context:
            view = context.resolve_view({"machine": basis.ticket_identity()})
            proposal = build_v2_amendment_proposal(spec, request, root, view)
        candidate = {**proposal.fields, **document.generated}
        revised = _convert_ticket(root, slug, _render_ticket(candidate, spec.body))
        errors = validate_ticket_spec(revised.spec, project_root=reference)
        if errors:
            raise AmendmentError("invalid amended Ticket: " + "; ".join(errors))
        return proposal


def _preflight_state(path: Path, proposal: AmendmentProposal) -> None:
    from booley.criteria.state import DevelopmentState

    if not path.is_file():
        raise AmendmentError("blocked Ticket has no current Criteria state; rerun intake first")
    try:
        state = DevelopmentState.load(path)
    except (OSError, ValueError) as exc:
        raise AmendmentError(
            "blocked Ticket Criteria state is unreadable; rerun intake first"
        ) from exc
    missing = [
        change.criterion for change in proposal.changes if change.criterion not in state.criteria
    ]
    if missing:
        raise AmendmentError(f"current state has no Criteria {missing!r}; rerun intake first")


def _preview_evidence(
    path: Path,
    proposal: AmendmentProposal,
    basis: TicketBaseline,
    checkout: Path,
    params: dict[str, dict[str, Any]],
) -> dict[str, Any]:
    from booley.criteria.state import DevelopmentState

    if not path.exists():
        return {
            change.criterion: {"result": "unknown", "evidence": "rerun"}
            for change in proposal.changes
        }
    state = DevelopmentState.load(path)
    result: dict[str, Any] = {}
    for change in proposal.changes:
        prior = state.criteria.get(change.criterion)
        if prior is None:
            result[change.criterion] = {"result": "unknown", "evidence": "rerun"}
            continue
        row = {"result": "met" if prior.met else "unmet"}
        if change.thresholds:
            candidate = deepcopy(prior)
            for param in change.thresholds:
                candidate.params[param] = params[change.criterion][param]
            _reevaluate_changed_entry(candidate, basis, {"basis": basis.as_dict()}, checkout)
            row["evidence"] = "reuse" if not candidate.stale else "rerun"
            row["after_result"] = "met" if candidate.met else "unmet"
        else:
            row["evidence"] = "unchanged"
            row["after_result"] = row["result"]
        result[change.criterion] = row
    return result


def preview_amendment(tio: Any, slug: str, request: Any) -> dict[str, Any]:
    """Render the exact, read-only proposal and source checkpoint identity."""
    root = Path(tio._project_root).resolve()
    if pending_amendment(root, slug) is not None:
        raise AmendmentError("amendment publication is pending; retry apply")
    preview, _proposal = _inspection(tio, slug, request)
    return preview


def apply_amendment(tio: Any, slug: str, request: Any, expected_preview: str) -> dict[str, Any]:
    """Apply an explicitly approved preview and queue the same Ticket to resume."""
    root = Path(tio._project_root).resolve()
    with tio._ticket_lock(slug):
        journal = pending_amendment(root, slug)
        if journal is None:
            if not isinstance(expected_preview, str) or len(expected_preview) != 64:
                raise AmendmentError("apply requires an exact preview digest")
            tio._validate_return_to_draft_preconditions(slug, check_owner=False)
            if load_basis_publication(root, slug) is not None:
                raise AmendmentError("another Ticket baseline publication is pending")
            preview, _proposal = _inspection(tio, slug, request)
            if preview["digest"] != expected_preview:
                raise AmendmentError("amendment preview is stale; preview the current proposal")
            journal = {
                **preview,
                "operation_id": secrets.token_hex(16),
                "prepared": {},
                "published": [],
                "new_basis": {},
                "machine": {},
                "phase": "preparing",
            }
            _write_journal(root, journal)
        elif journal.get("digest") != expected_preview:
            raise AmendmentError("pending amendment has a different preview digest")
        result = _finish_amendment(tio, journal)
    from .operations import _clear_lock_pid

    _clear_lock_pid(tio, slug)
    return result


def _finish_amendment(tio: Any, journal: dict[str, Any]) -> dict[str, Any]:
    root = Path(tio._project_root).resolve()
    old = ticket_baseline_from_machine(journal["old_machine"])
    if old.as_dict() != journal["basis"]:
        raise AmendmentError("amendment journal baseline identity changed")
    repositories = _repositories(root, old)
    _prepare_amendment_participants(root, journal, old, repositories)
    _publish_refs(root, journal, old, repositories)
    new_basis = ticket_baseline_from_machine(journal["machine"])
    if new_basis.as_dict() != journal["new_basis"]:
        raise AmendmentError("prepared amendment baseline identity changed")
    _publish_board_and_state(tio, journal, new_basis, repositories)
    _publish_handoff_and_queue(tio, journal, new_basis)
    return {
        "slug": journal["slug"],
        "basis_id": new_basis.basis_id,
        "operation_id": journal["operation_id"],
        "status": "queued",
    }


def _prepare_amendment_participants(
    root: Path,
    journal: dict[str, Any],
    old: TicketBaseline,
    repositories: dict[str, tuple[Path, Path]],
) -> None:
    for participant in sorted(old.participants, key=lambda row: row.role != "project"):
        role = participant.role
        if role in journal["prepared"]:
            continue
        owner, checkout = repositories[role]
        expected = journal["source_state"][role]
        actual = _status_snapshot(root, old, journal["prior_scope"], {role: (owner, checkout)})[
            role
        ]
        if actual != expected:
            raise AmendmentError(f"{role} source changed after the approved preview")
        commits = _prepare_participant(
            root,
            owner,
            checkout,
            participant,
            journal,
            _amendment_machine(root, old, journal) if role == "outer" else None,
        )
        journal["prepared"][role] = commits
        _write_journal(root, journal)
    if not journal["new_basis"]:
        participants = tuple(
            BasisParticipant(
                row.role,
                journal["prepared"][row.role]["authoring"],
                row.ticket_ref,
                row.destination_ref,
                row.destination_sha,
            )
            for row in old.participants
        )
        journal["new_basis"] = TicketBaseline(participants).as_dict()
        journal["machine"] = _amendment_machine(root, old, journal, participants=participants)
        journal["phase"] = "prepared"
        _write_journal(root, journal)


def _amendment_machine(
    root: Path,
    old: TicketBaseline,
    journal: dict[str, Any],
    *,
    participants: tuple[BasisParticipant, ...] | None = None,
) -> dict[str, Any]:
    """Build signed Ticket metadata after the project commit is prepared."""
    if participants is None:
        participants = tuple(
            BasisParticipant(
                row.role,
                journal["prepared"].get(row.role, {}).get("authoring", row.authoring_sha),
                row.ticket_ref,
                row.destination_ref,
                row.destination_sha,
            )
            for row in old.participants
        )
    basis = TicketBaseline(participants, providers=old.providers)
    revised = _convert_ticket(
        root,
        journal["slug"],
        _render_ticket(
            {**journal["revised_fields"], "machine": old.machine or old.ticket_identity()},
            journal["body"],
        ),
    )
    machine = ticket_machine_from_spec(basis, revised.spec, journal["operation_id"])
    prior_conversions = (old.machine or {}).get("amendment", {}).get("optional_conversions", [])
    conversions = [
        change["criterion"]
        for change in journal["changes"]
        if change["before_mandatory"] and not change["after_mandatory"]
    ]
    machine["amendment"] = {
        "slug": journal["slug"],
        "previous_generation": old.ticket_identity()["generation"],
        "operation_id": journal["operation_id"],
        "actor": journal["actor"],
        "reason": journal["reason"],
        "changes": journal["changes"],
        "scope_added": journal["scope_added"],
        "optional_conversions": sorted(set(prior_conversions) | set(conversions)),
    }
    return machine


def _checkpoint(checkout: Path, source: dict[str, Any], operation_id: str, role: str) -> str:
    head = source["head"]
    original_tree = _git(checkout, "rev-parse", f"{head}^{{tree}}")
    index_tree = _git(checkout, "write-tree")
    staged = head
    if index_tree != original_tree:
        staged = _git(
            checkout,
            "commit-tree",
            index_tree,
            "-p",
            head,
            "-m",
            f"Ticket {operation_id}: preserve staged {role} source",
        )
    unstaged_paths = [
        item["path"]
        for item in source["entries"]
        if item["status"] == "??" or item["status"][1] != " "
    ]
    checkpoint = staged
    if unstaged_paths:
        descriptor, name = tempfile.mkstemp(prefix="booley-amend-index-")
        os.close(descriptor)
        index = Path(name)
        index.unlink()
        try:
            _git(checkout, "read-tree", staged, index=index)
            _git(checkout, "add", "-A", "--", *unstaged_paths, index=index)
            tree = _git(checkout, "write-tree", index=index)
        finally:
            index.unlink(missing_ok=True)
        checkpoint = _git(
            checkout,
            "commit-tree",
            tree,
            "-p",
            staged,
            "-m",
            f"Ticket {operation_id}: preserve working {role} source",
        )
    _git(
        checkout,
        "update-ref",
        f"refs/booley/amendments/{operation_id}/{role}/checkpoint",
        checkpoint,
    )
    return checkpoint


def _prepare_participant(
    root: Path,
    owner: Path,
    checkout: Path,
    participant: BasisParticipant,
    journal: dict[str, Any],
    machine: dict[str, Any] | None,
) -> dict[str, str]:
    role = participant.role
    operation = journal["operation_id"]
    checkpoint = _checkpoint(checkout, journal["source_state"][role], operation, role)
    authoring_tree = _git(owner, "rev-parse", f"{participant.authoring_sha}^{{tree}}")
    message = f"Ticket {operation}: amend {role} acceptance requirements"
    if machine is not None:
        message += (
            f"\n\nBooley-Ticket-Slug: {journal['slug']}"
            f"\nBooley-Authored-SHA256: {machine['authored_sha256']}"
            f"\nBooley-Machine-SHA256: {ticket_machine_digest(machine)}"
        )
    authoring = _git(
        owner,
        "commit-tree",
        authoring_tree,
        "-p",
        participant.authoring_sha,
        "-m",
        message,
    )
    _git(owner, "update-ref", f"refs/booley/amendments/{operation}/{role}/authoring", authoring)
    joined_tree = _git(owner, "rev-parse", f"{checkpoint}^{{tree}}")
    joined = _git(
        owner,
        "commit-tree",
        joined_tree,
        "-p",
        checkpoint,
        "-p",
        authoring,
        "-m",
        f"Ticket {operation}: join amended {role} baseline",
    )
    _git(owner, "update-ref", f"refs/booley/amendments/{operation}/{role}/joined", joined)
    return {"checkpoint": checkpoint, "authoring": authoring, "joined": joined}


def _publish_refs(
    root: Path,
    journal: dict[str, Any],
    old: TicketBaseline,
    repositories: dict[str, tuple[Path, Path]],
) -> None:
    for role in ("project", "outer"):
        if role not in repositories or role in journal["published"]:
            continue
        owner, checkout = repositories[role]
        participant = old.participant(role)
        current = _git(owner, "rev-parse", participant.ticket_ref)
        expected = journal["source_state"][role]["head"]
        joined = journal["prepared"][role]["joined"]
        if current == expected:
            _git(owner, "update-ref", participant.ticket_ref, joined, expected)
        elif current != joined:
            raise AmendmentError(f"{role} Ticket ref changed during publication")
        _git(checkout, "reset", "--hard", joined)
        journal["published"].append(role)
        journal["phase"] = "publishing"
        _write_journal(root, journal)


def _publish_board_and_state(
    tio: Any,
    journal: dict[str, Any],
    basis: TicketBaseline,
    repositories: dict[str, tuple[Path, Path]],
) -> None:
    root = Path(tio._project_root).resolve()
    if journal["phase"] in {"board", "handoff", "queued"}:
        return
    ticket, status = find_ticket_file(tio.tickets_dir, journal["slug"])
    if ticket is None or status != "blocked":
        raise AmendmentError("blocked Ticket disappeared during publication")
    fields = dict(journal["revised_fields"])
    fields["machine"] = journal["machine"]
    candidate = _render_ticket(fields, journal["body"]).encode()
    if ticket.read_bytes() != candidate:
        original = hashlib.sha256(ticket.read_bytes()).hexdigest()
        if original != journal["ticket_sha256"]:
            raise AmendmentError("Board Ticket changed during amendment publication")
        atomic_replace_bytes(ticket, candidate, mode=0o644)
    loaded = load_ticket_baseline_from_document(
        root, journal["slug"], _convert_ticket(root, journal["slug"], candidate.decode())
    )
    _rebuild_state(tio, journal, loaded, repositories["outer"][1])
    journal["phase"] = "board"
    _write_journal(root, journal)


def _rebuild_state(
    tio: Any, journal: dict[str, Any], basis: TicketBaseline, checkout: Path
) -> None:
    from booley.criteria.state import DevelopmentState

    path = existing_runtime_file(tio.logs_dir, journal["slug"], "booley_state.json")
    prior = (
        ticket_log_dir(tio.logs_dir, journal["slug"])
        / "amendments"
        / (journal["operation_id"] + ".prior-state.json")
    )
    if path.exists():
        atomic_write_once(prior, path.read_bytes())
    state = DevelopmentState.load(path)
    revised_fields = dict(journal["revised_fields"])
    revised_fields["machine"] = basis.ticket_identity()
    revised = _convert_ticket(
        Path(tio._project_root),
        journal["slug"],
        _render_ticket(revised_fields, journal["body"]),
    )
    params = project_ticket_criteria(revised.spec).params
    for change in journal["changes"]:
        name = change["criterion"]
        if name not in state.criteria:
            raise AmendmentError(f"current state has no Criterion {name!r}; rerun intake first")
        entry = state.criteria[name]
        entry.mandatory = change["after_mandatory"]
        for param in change["thresholds"]:
            if param not in params.get(name, {}):
                raise AmendmentError(f"new Criterion parameter {name}.{param} is unavailable")
            entry.params[param] = params[name][param]
        if change["thresholds"]:
            _reevaluate_changed_entry(entry, basis, journal, checkout)
    report = state.criteria.get("_report_submitted")
    if report is not None:
        report.met = False
        report.stale = True
    state.criteria.pop("_blocked_reason", None)
    visible_mandatory = any(
        entry.mandatory for name, entry in state.criteria.items() if not name.startswith("_")
    )
    conversions = basis.ticket_identity()["amendment"]["optional_conversions"]
    state.authorized_zero_mandatory_basis_id = (
        basis.basis_id if not visible_mandatory and conversions else ""
    )
    state.save()


def _reevaluate_changed_entry(
    entry: Any, basis: TicketBaseline, journal: dict[str, Any], checkout: Path
) -> None:
    from booley.criteria.state import DevelopmentState
    from booley.evidence.fields import SOURCE_FINGERPRINT_DETAIL_KEY
    from booley.flows.source_fingerprint import compute_source_fingerprint

    detail = entry.detail if isinstance(entry.detail, dict) else {}
    stamp = detail.get(SOURCE_FINGERPRINT_DETAIL_KEY)
    implementation = detail.get("implementation")
    status = implementation.get("status") if isinstance(implementation, dict) else None
    reusable = isinstance(status, dict) and status.get("passed") is True
    reusable = reusable and status.get("tool_returncode") == 0 and not status.get("timed_out")
    reusable = reusable and not status.get("infra_error") and not status.get("failure_reasons")
    if (
        not isinstance(stamp, dict)
        or not isinstance(stamp.get("fingerprint"), dict)
        or not isinstance(stamp.get("categories"), list)
        or not stamp["categories"]
    ):
        reusable = False
    else:
        target = stamp.get("target")
        try:
            current = compute_source_fingerprint(
                checkout, target=target if isinstance(target, str) else None
            )
            reusable = reusable and all(
                stamp["fingerprint"].get(category, {}).get("digest")
                == current.get(category, {}).get("digest")
                for category in stamp.get("categories", [])
            )
        except (OSError, ValueError):
            reusable = False
    entry.met = bool(reusable)
    entry.stale = not reusable
    if reusable:
        evaluator = DevelopmentState()
        evaluator._evaluate_thresholds(entry)
        if any(item.get("skipped") for item in entry.detail.get("checks", [])):
            entry.met = False
            entry.stale = True
    detail["amendment_evaluation"] = {
        "old_basis_id": TicketBaseline.from_mapping(journal["basis"]).basis_id,
        "new_basis_id": basis.basis_id,
        "source_evidence_sha256": hashlib.sha256(canonical_json(stamp or {})).hexdigest(),
        "reused": bool(reusable),
    }
    entry.detail = detail


def _amendment_outcomes(tio: Any, journal: dict[str, Any]) -> dict[str, dict[str, str]]:
    from booley.criteria.state import DevelopmentState

    path = existing_runtime_file(tio.logs_dir, journal["slug"], "booley_state.json")
    state = DevelopmentState.load(path)
    outcomes: dict[str, dict[str, str]] = {}
    changed = {change["criterion"]: change for change in journal["changes"]}
    for name, entry in state.criteria.items():
        if name.startswith("_"):
            continue
        detail = entry.detail if isinstance(entry.detail, dict) else {}
        evaluation = detail.get("amendment_evaluation", {})
        evidence = "unchanged"
        if changed.get(name, {}).get("thresholds"):
            evidence = "reuse" if evaluation.get("reused") else "rerun"
        outcomes[name] = {
            "result": "met" if entry.met else "unmet",
            "evidence": evidence,
        }
    return outcomes


def _amendment_summary(journal: dict[str, Any], record: dict[str, Any]) -> str:
    lines = [
        f"\n## Amendment {journal['operation_id']}",
        f"Actor: {journal['actor']}",
        f"Reason: {journal['reason']}",
    ]
    for change in journal["changes"]:
        name = change["criterion"]
        for param, (before, after) in change["thresholds"].items():
            lines.append(f"- {name}.{param}: {before} -> {after}")
        if change["before_mandatory"] and not change["after_mandatory"]:
            lines.append(f"- {name}: mandatory -> optional")
    for path in journal["scope_added"]:
        lines.append(f"- Scope added: {path}")
    for name, outcome in record["evidence_outcomes"].items():
        lines.append(f"- {name}: {outcome['result']} ({outcome['evidence']})")
    lines.extend([f"Next: {record['next_action']}", journal["feedback"]])
    return "\n".join(lines) + "\n"


def _publish_handoff_and_queue(tio: Any, journal: dict[str, Any], basis: TicketBaseline) -> None:
    root = Path(tio._project_root).resolve()
    slug = journal["slug"]
    history = ticket_log_dir(tio.logs_dir, slug) / "amendments" / f"{journal['operation_id']}.json"
    record = _handoff_record(tio, journal, basis)
    atomic_write_once(history, canonical_json(record))
    blocked = human_log_file(tio.logs_dir, slug, "blocked.md")
    marker = f"Amendment {journal['operation_id']}"
    content = blocked.read_text(encoding="utf-8") if blocked.exists() else "# Escalation History\n"
    if marker not in content:
        addition = _amendment_summary(journal, record)
        atomic_replace_bytes(blocked, (content + addition).encode(), mode=0o644)
    progress = load_progress(tio.logs_dir, slug) or dict(PROGRESS_DEFAULTS)
    progress.update(
        {
            "workspace_intent": "resume",
            "blocked_reason": None,
            "blocked_step": None,
            "error": None,
            "failed_step": None,
        }
    )
    save_progress(tio.logs_dir, slug, progress)
    ticket, status = find_ticket_file(tio.tickets_dir, slug)
    if status == "blocked" and ticket is not None:
        queue = tio.tickets_dir / "board" / "queue" / ticket.name
        queue.parent.mkdir(parents=True, exist_ok=True)
        if queue.exists():
            raise AmendmentError("queue destination is occupied")
        ticket.replace(queue)
    elif status != "queued":
        raise AmendmentError("Ticket moved unexpectedly before resume queue")
    transition = human_log_file(tio.logs_dir, slug, "transitions.log")
    if not transition.exists() or marker not in transition.read_text(encoding="utf-8"):
        tio._append_transition_unlocked(slug, "blocked", "queued", "ticket-triage", marker)
    journal["phase"] = "queued"
    _write_journal(root, journal)
    _journal_path(root, slug).unlink()


def _handoff_record(tio: Any, journal: dict[str, Any], basis: TicketBaseline) -> dict[str, Any]:
    outcomes = _amendment_outcomes(tio, journal)
    rerun = [name for name, outcome in outcomes.items() if outcome["evidence"] == "rerun"]
    next_action = "Resume Ticket Mode under the revised Ticket baseline"
    if rerun:
        next_action += "; rerun stale Criteria: " + ", ".join(sorted(rerun))
    return {
        "schema": 1,
        "operation_id": journal["operation_id"],
        "old_basis_id": TicketBaseline.from_mapping(journal["basis"]).basis_id,
        "new_basis_id": basis.basis_id,
        "reason": journal["reason"],
        "actor": journal["actor"],
        "feedback": journal["feedback"],
        "changes": journal["changes"],
        "scope_added": journal["scope_added"],
        "checkpoint": {role: row["checkpoint"] for role, row in journal["prepared"].items()},
        "participant_heads_before": {
            role: row["head"] for role, row in journal["source_state"].items()
        },
        "participant_heads_after": {
            role: row["joined"] for role, row in journal["prepared"].items()
        },
        "evidence_outcomes": outcomes,
        "next_action": next_action,
        "prior_state": f"{journal['operation_id']}.prior-state.json",
    }
