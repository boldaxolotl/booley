"""Retained attempt provenance and guarded canonical successful completion association."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from booley.goals.finish_attempt import validate_attempt, validate_local_artifacts
from booley.goals.lifecycle import LifecycleError, LifecycleOperation, read_bound_request
from booley.goals.proposals import digest, encode
from booley.runtime.atomic_files import atomic_replace_bytes, atomic_write_once
from booley.runtime.pinned_history import raw_git, tree_rows


def prior_attempts(
    operation: LifecycleOperation,
) -> list[tuple[LifecycleOperation, dict[str, Any]]]:
    """Enumerate exact record-owned immutable attempts, never latest/foreign evidence."""
    found: list[tuple[LifecycleOperation, dict[str, Any]]] = []
    for directory in sorted((operation.lock.record_dir / "operations").iterdir()):
        if directory == operation.directory or not (directory / "attempt.authority.json").exists():
            continue
        prior = LifecycleOperation(
            operation.store,
            operation.lock,
            read_bound_request(operation, directory.name),
            operation.root,
        )
        frozen = prior.read_sealed("attempt")
        validate_attempt(frozen, prior)
        validate_local_artifacts(frozen, prior)
        found.append((prior, frozen))
    return found


def earlier_attempt_facts(operation: LifecycleOperation) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for prior, frozen in prior_attempts(operation):
        result = prior.saved_result()
        rows.append(
            {
                "operation_id": prior.request.operation_id,
                "outcome": "interrupted" if result is None else result["status"],
                "reason": None if result is None else result.get("reason"),
                "pins": {key: frozen["inputs"][key] for key in ("rtl_pin", "project_pin")},
                "package_digest": "sha256:" + frozen["package_digest"],
                "summary_digest": "sha256:" + frozen["summary_digest"],
                "attempt_digest": "sha256:" + digest(frozen),
                "history_path": frozen["path"] if frozen["skip_publication"] is None else None,
                "artifact_path": str(prior.directory),
            }
        )
    return rows


def retained_outputs(operation: LifecycleOperation) -> frozenset[Path]:
    from booley.goals.publication_destination import require_destination

    found: set[Path] = set()
    tracked = tree_rows(
        operation.root, raw_git(operation.root, "rev-parse", "HEAD").strip().decode()
    )
    for prior, frozen in prior_attempts(operation):
        result = prior.saved_result()
        if result is None or result["status"] != "revalidation_required":
            continue
        destination = operation.root / frozen["path"]
        metadata = tracked.get(frozen["path"].encode())
        if metadata is not None and (
            not metadata.startswith(b"100644 blob ")
            or raw_git(operation.root, "cat-file", "blob", metadata.split()[2].decode())
            != bytes.fromhex(frozen["summary_hex"])
        ):
            # Later user-committed history edits are current pinned source, not an
            # untracked artifact this operation may adopt, replace or exempt.
            continue
        if require_destination(destination, bytes.fromhex(frozen["summary_hex"])):
            found.add(destination)
    return frozenset(found)


def validate_canonical_ownership(operation: LifecycleOperation, frozen: dict[str, Any]) -> None:
    """Refuse unknown canonical edits before this attempt can publish any new summary."""
    for name, content in (
        ("SUMMARY.md", bytes.fromhex(frozen["summary_hex"])),
        ("review-package.json", encode(frozen["package"])),
    ):
        _prior_content(operation, name, content)


def associate_completion(operation: LifecycleOperation, frozen: dict[str, Any]) -> None:
    """Canonical files identify success; an external edit is never overwritten/adopted."""
    contents = {
        "SUMMARY.md": bytes.fromhex(frozen["summary_hex"]),
        "review-package.json": encode(frozen["package"]),
    }
    authority = operation.directory / "canonical.authority.json"
    if authority.exists():
        association = operation.read_sealed("canonical")
    else:
        association = {
            name: _prior_content(operation, name, content) for name, content in contents.items()
        }
        operation.write_sealed("canonical", association)
    if set(association) != set(contents):
        raise LifecycleError("invalid canonical completion association")
    for name, content in contents.items():
        path = operation.lock.record_dir / name
        prior = association[name]
        if prior is not None and not isinstance(prior, str):
            raise LifecycleError("invalid canonical prior artifact proof")
        if path.is_symlink() or (path.exists() and not path.is_file()):
            raise LifecycleError("canonical completion artifact is not a regular owned file")
        actual = path.read_bytes() if path.exists() else None
        if actual == content:
            continue
        if actual != (None if prior is None else bytes.fromhex(prior)):
            raise LifecycleError("canonical completion artifact differs from its saved ownership")
        if actual is None:
            atomic_write_once(path, content)
        else:
            atomic_replace_bytes(path, content)


def _prior_content(operation: LifecycleOperation, name: str, content: bytes) -> str | None:
    path = operation.lock.record_dir / name
    if path.is_symlink() or (path.exists() and not path.is_file()):
        raise LifecycleError("canonical completion artifact is not a regular owned file")
    if not path.exists():
        return None
    actual = path.read_bytes()
    if actual == content:
        return actual.hex()
    for _, frozen in prior_attempts(operation):
        previous = (
            bytes.fromhex(frozen["summary_hex"])
            if name == "SUMMARY.md"
            else encode(frozen["package"])
        )
        if actual == previous:
            return actual.hex()
    raise LifecycleError("external canonical completion edit requires reconciliation")
