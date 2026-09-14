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
from fnmatch import fnmatchcase
from pathlib import Path
from typing import Any

from booley.criteria.templates import CriteriaTemplate
from booley.runtime.project_dir import runtime_dir
from booley.ticket_board.ticket_repositories import resolve_inner_project_repo

from .acceptance_basis import (
    PATH_POLICY,
    AcceptanceBasis,
    BasisParticipant,
    assert_live_inputs_unchanged,
    authored_ticket_record,
    canonical_json,
    load_acceptance_basis,
    materialize_basis_checkout,
    record_relative_path,
    validate_current_basis_refs,
    worktree_for_ref,
    write_basis_receipt,
)
from .acceptance_path_policy import is_static_acceptance_path
from .amendment_proposal import AmendmentProposal, build_amendment_proposal
from .basis_publication import load_basis_publication
from .frontmatter import format_frontmatter, parse_frontmatter
from .git_status import GitStatusEntry, parse_porcelain_v1_z
from .logs import PROGRESS_DEFAULTS, load_progress, save_progress
from .paths import existing_runtime_file, human_log_file, ticket_log_dir
from .persistence import atomic_replace_bytes, atomic_write_once
from .scanner import find_ticket_file


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
    path = _journal_path(root, slug)
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


def _repositories(root: Path, basis: AcceptanceBasis) -> dict[str, tuple[Path, Path]]:
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


def _scope_covers(scope: list[str], path: str) -> bool:
    for entry in scope:
        name = entry.removesuffix(" [new]")
        if name in {"*", path} or path.startswith(name.rstrip("/") + "/"):
            return True
        if any(token in name for token in "*?[") and fnmatchcase(path, name):
            return True
    return False


def _status_snapshot(
    root: Path,
    basis: AcceptanceBasis,
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
            not _scope_covers(scope, composite)
            or is_static_acceptance_path(composite)
            or composite in protected
            or any(composite.startswith(path.rstrip("/") + "/") for path in protected)
        ):
            raise AmendmentError(f"dirty source {composite!r} is outside prior Scope or protected")
        if (checkout / local).is_symlink():
            raise AmendmentError(f"dirty source {composite!r} is a symlink")


def _inspection(tio: Any, slug: str, request: Any) -> tuple[dict[str, Any], AmendmentProposal]:
    root = Path(tio._project_root).resolve()
    ticket, status = find_ticket_file(tio.tickets_dir, slug)
    if ticket is None or status != "blocked":
        raise AmendmentError(f"Ticket {slug!r} must be blocked to amend")
    source = ticket.read_bytes()
    fields, body = parse_frontmatter(source.decode("utf-8"))
    basis = load_acceptance_basis(root, slug, fields, body)
    heads = validate_current_basis_refs(root, basis)
    with tempfile.TemporaryDirectory(prefix="booley-amend-preview-") as directory:
        reference = materialize_basis_checkout(root, basis, Path(directory) / "basis")
        assert_live_inputs_unchanged(basis, root, reference)
    proposal = build_amendment_proposal(fields, request, root)
    rendered_fields, rendered_body = parse_frontmatter(format_frontmatter(proposal.fields, body))
    repositories = _repositories(root, basis)
    source_state = _status_snapshot(root, basis, rendered_fields.get("scope", []), repositories)
    if {role: value["head"] for role, value in source_state.items()} != heads:
        raise AmendmentError("Ticket refs changed during amendment preview")
    state_path = existing_runtime_file(tio.logs_dir, slug, "booley_state.json")
    state_digest = (
        hashlib.sha256(state_path.read_bytes()).hexdigest() if state_path.exists() else ""
    )
    current_results = _preview_evidence(state_path, proposal, basis, repositories["outer"][1])
    preview = {
        "schema": 1,
        "slug": slug,
        "ticket_sha256": hashlib.sha256(source).hexdigest(),
        "basis": basis.as_dict(),
        "source_state": source_state,
        "state_sha256": state_digest,
        "revised_fields": rendered_fields,
        "body": rendered_body,
        "reason": proposal.reason,
        "feedback": proposal.feedback,
        "changes": [asdict(item) for item in proposal.changes],
        "scope_added": list(proposal.scope_added),
        "current_results": current_results,
        "mandatory_after": sum(
            CriteriaTemplate.from_yaml(proposal.fields["criteria"]).expand(["default"]).values()
        ),
    }
    preview["digest"] = hashlib.sha256(canonical_json(preview)).hexdigest()
    return preview, proposal


def _preview_evidence(
    path: Path, proposal: AmendmentProposal, basis: AcceptanceBasis, checkout: Path
) -> dict[str, Any]:
    from booley.criteria.state import DevelopmentState

    if not path.exists():
        return {
            change.criterion: {"result": "unknown", "evidence": "rerun"}
            for change in proposal.changes
        }
    state = DevelopmentState.load(path)
    template = CriteriaTemplate.from_yaml(proposal.fields["criteria"])
    params = template.expand_params(["default"])
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
                raise AmendmentError("another Acceptance Basis publication is pending")
            preview, _proposal = _inspection(tio, slug, request)
            if preview["digest"] != expected_preview:
                raise AmendmentError("amendment preview is stale; preview the current proposal")
            journal = {
                **preview,
                "operation_id": secrets.token_hex(16),
                "prepared": {},
                "published": [],
                "new_basis": {},
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
    old = AcceptanceBasis.from_mapping(journal["basis"])
    repositories = _repositories(root, old)
    record = _amended_record(root, journal, old)
    for participant in old.participants:
        role = participant.role
        if role in journal["prepared"]:
            continue
        owner, checkout = repositories[role]
        expected = journal["source_state"][role]
        actual = _status_snapshot(
            root, old, journal["revised_fields"]["scope"], {role: (owner, checkout)}
        )[role]
        if actual != expected:
            raise AmendmentError(f"{role} source changed after the approved preview")
        commits = _prepare_participant(
            root,
            owner,
            checkout,
            participant,
            journal,
            record if _record_owner(old) == role else None,
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
        journal["new_basis"] = AcceptanceBasis(participants).as_dict()
        journal["phase"] = "prepared"
        _write_journal(root, journal)
    _publish_refs(root, journal, old, repositories)
    new_basis = AcceptanceBasis.from_mapping(journal["new_basis"])
    write_basis_receipt(
        root,
        journal["slug"],
        new_basis,
        source_sha256=journal["ticket_sha256"],
        operation_id=journal["operation_id"],
    )
    _publish_board_and_state(tio, journal, new_basis, repositories)
    _publish_handoff_and_queue(tio, journal, new_basis)
    return {
        "slug": journal["slug"],
        "basis_id": new_basis.basis_id,
        "operation_id": journal["operation_id"],
        "status": "queued",
    }


def _record_owner(basis: AcceptanceBasis) -> str:
    return "project" if basis.project_sha else "outer"


def _amended_record(root: Path, journal: dict[str, Any], old: AcceptanceBasis) -> bytes:
    from .acceptance_basis import load_basis_record

    old_record = load_basis_record(root, journal["slug"], old)
    loaded = old.with_record(old_record)
    payload = authored_ticket_record(
        journal["revised_fields"],
        journal["body"],
        loaded.bindings,
        target_plan=loaded.target_plan,
        removal_targets=loaded.removal_targets,
        providers=loaded.providers,
    )
    payload["schema"] = 3
    prior_conversions = old_record.get("amendment", {}).get("optional_conversions", [])
    conversions = [
        change["criterion"]
        for change in journal["changes"]
        if change["before_mandatory"] and not change["after_mandatory"]
    ]
    payload["amendment"] = {
        "old_basis": old.as_dict(),
        "operation_id": journal["operation_id"],
        "reason": journal["reason"],
        "changes": journal["changes"],
        "scope_added": journal["scope_added"],
        "optional_conversions": sorted(set(prior_conversions) | set(conversions)),
    }
    return canonical_json(payload)


def _temporary_index(
    repository: Path, tree: str, record_path: str | None, data: bytes | None
) -> str:
    descriptor, name = tempfile.mkstemp(prefix="booley-amend-index-")
    os.close(descriptor)
    index = Path(name)
    index.unlink()
    try:
        _git(repository, "read-tree", tree, index=index)
        if record_path is not None and data is not None:
            blob = _git(repository, "hash-object", "-w", "--stdin", data=data)
            _git(
                repository,
                "update-index",
                "--add",
                "--cacheinfo",
                f"100644,{blob},{record_path}",
                index=index,
            )
        return _git(repository, "write-tree", index=index)
    finally:
        index.unlink(missing_ok=True)


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
    record: bytes | None,
) -> dict[str, str]:
    role = participant.role
    operation = journal["operation_id"]
    checkpoint = _checkpoint(checkout, journal["source_state"][role], operation, role)
    record_path = (
        (
            record_relative_path(root, project_participant=role == "project")
            / f"{journal['slug']}.json"
        ).as_posix()
        if record is not None
        else None
    )
    authoring_tree = _temporary_index(owner, participant.authoring_sha, record_path, record)
    authoring = _git(
        owner,
        "commit-tree",
        authoring_tree,
        "-p",
        participant.authoring_sha,
        "-m",
        f"Ticket {operation}: amend {role} acceptance requirements",
    )
    _git(owner, "update-ref", f"refs/booley/amendments/{operation}/{role}/authoring", authoring)
    joined_tree = _temporary_index(owner, checkpoint, record_path, record)
    joined = _git(
        owner,
        "commit-tree",
        joined_tree,
        "-p",
        checkpoint,
        "-p",
        authoring,
        "-m",
        f"Ticket {operation}: join amended {role} basis",
    )
    _git(owner, "update-ref", f"refs/booley/amendments/{operation}/{role}/joined", joined)
    return {"checkpoint": checkpoint, "authoring": authoring, "joined": joined}


def _publish_refs(
    root: Path,
    journal: dict[str, Any],
    old: AcceptanceBasis,
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
    basis: AcceptanceBasis,
    repositories: dict[str, tuple[Path, Path]],
) -> None:
    root = Path(tio._project_root).resolve()
    if journal["phase"] in {"board", "handoff", "queued"}:
        return
    ticket, status = find_ticket_file(tio.tickets_dir, journal["slug"])
    if ticket is None or status != "blocked":
        raise AmendmentError("blocked Ticket disappeared during publication")
    fields = dict(journal["revised_fields"])
    fields["acceptance_basis"] = basis.as_dict()
    fields["acceptance_amendment"] = {
        "slug": journal["slug"],
        "operation_id": journal["operation_id"],
    }
    candidate = format_frontmatter(fields, journal["body"]).encode()
    if ticket.read_bytes() != candidate:
        original = hashlib.sha256(ticket.read_bytes()).hexdigest()
        if original != journal["ticket_sha256"]:
            raise AmendmentError("Board Ticket changed during amendment publication")
        atomic_replace_bytes(ticket, candidate, mode=0o644)
    loaded = load_acceptance_basis(root, journal["slug"], fields, journal["body"])
    _rebuild_state(tio, journal, loaded, repositories["outer"][1])
    journal["phase"] = "board"
    _write_journal(root, journal)


def _rebuild_state(
    tio: Any, journal: dict[str, Any], basis: AcceptanceBasis, checkout: Path
) -> None:
    from booley.criteria.state import DevelopmentState

    from .acceptance_basis import load_basis_record

    path = existing_runtime_file(tio.logs_dir, journal["slug"], "booley_state.json")
    prior = (
        ticket_log_dir(tio.logs_dir, journal["slug"])
        / "amendments"
        / (journal["operation_id"] + ".prior-state.json")
    )
    if path.exists():
        atomic_write_once(prior, path.read_bytes())
    state = DevelopmentState.load(path)
    template = CriteriaTemplate.from_yaml(journal["revised_fields"]["criteria"])
    params = template.expand_params(["default"])
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
    record = load_basis_record(Path(tio._project_root), journal["slug"], basis)
    conversions = record["amendment"]["optional_conversions"]
    state.authorized_zero_mandatory_basis_id = (
        basis.basis_id if not visible_mandatory and conversions else ""
    )
    state.save()


def _reevaluate_changed_entry(
    entry: Any, basis: AcceptanceBasis, journal: dict[str, Any], checkout: Path
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
        "old_basis_id": AcceptanceBasis.from_mapping(journal["basis"]).basis_id,
        "new_basis_id": basis.basis_id,
        "source_evidence_sha256": hashlib.sha256(canonical_json(stamp or {})).hexdigest(),
        "reused": bool(reusable and entry.met),
    }
    entry.detail = detail


def _publish_handoff_and_queue(tio: Any, journal: dict[str, Any], basis: AcceptanceBasis) -> None:
    root = Path(tio._project_root).resolve()
    slug = journal["slug"]
    history = ticket_log_dir(tio.logs_dir, slug) / "amendments" / f"{journal['operation_id']}.json"
    record = {
        "schema": 1,
        "operation_id": journal["operation_id"],
        "old_basis_id": AcceptanceBasis.from_mapping(journal["basis"]).basis_id,
        "new_basis_id": basis.basis_id,
        "reason": journal["reason"],
        "feedback": journal["feedback"],
        "changes": journal["changes"],
        "scope_added": journal["scope_added"],
        "checkpoint": {role: row["checkpoint"] for role, row in journal["prepared"].items()},
        "prior_state": f"{journal['operation_id']}.prior-state.json",
    }
    atomic_write_once(history, canonical_json(record))
    blocked = human_log_file(tio.logs_dir, slug, "blocked.md")
    marker = f"Amendment {journal['operation_id']}"
    content = blocked.read_text(encoding="utf-8") if blocked.exists() else "# Escalation History\n"
    if marker not in content:
        addition = f"\n### {marker}\n\nReason: {journal['reason']}\n\n{journal['feedback']}\n"
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
