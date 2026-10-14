"""Durable, lossless snapshots for Findings Log attachments.

Feedback attachments are live paths.  Project Setup cleanup must convert only
the bounded excerpt the Feedback renderer consumes before removing a raw log.
The sidecar metadata keeps the original name and clipping facts visible after
that conversion.
"""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Iterable
from pathlib import Path

from booley.feedback import render
from booley.feedback.findings import (
    CorruptFindingsLogError,
    Finding,
    log_path,
    read_log,
    transaction,
)

MAX_ATTACHMENT_LINES = 120
MAX_ATTACHMENT_CHARS = 8000
_SAFE_NAME = re.compile(r"[^A-Za-z0-9_.-]+")


class MaterializationError(RuntimeError):
    """Raised when attachment evidence cannot be snapshotted safely."""


def normalize_attachment_path(raw: str, booley_dir: Path) -> Path:
    """Resolve absolute, ``~``, and project-relative attachment spellings."""
    candidate = Path(raw).expanduser()
    if candidate.is_absolute():
        return candidate.resolve(strict=False)
    for base in (booley_dir.parent, booley_dir, Path.cwd()):
        resolved = (base / candidate).resolve(strict=False)
        if resolved.exists():
            return resolved
    return (booley_dir.parent / candidate).resolve(strict=False)


def _excerpt(source: Path) -> tuple[str, dict[str, object]]:
    """Read exactly the bounded evidence used by the report renderer."""
    raw = source.read_bytes()
    text = raw.decode("utf-8", errors="replace")
    lines = text.splitlines()
    selected = lines[-MAX_ATTACHMENT_LINES:]
    excerpt = "\n".join(selected)
    clipped = len(selected) < len(lines)
    if len(excerpt) > MAX_ATTACHMENT_CHARS:
        excerpt = excerpt[-MAX_ATTACHMENT_CHARS:]
        clipped = True
    metadata = {
        "original_path": str(source),
        "source_sha256": hashlib.sha256(raw).hexdigest(),
        "line_count": len(lines),
        "byte_count": len(raw),
        "clipped": clipped,
        "excerpt_line_count": len(excerpt.splitlines()),
        "excerpt_char_count": len(excerpt),
    }
    return excerpt, metadata


def _write_snapshot(path: Path, text: str, metadata: dict[str, object]) -> None:
    """Publish snapshot content and metadata together using sibling renames."""
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.tmp")
    temporary.write_text(text, encoding="utf-8")
    temporary.replace(path)
    meta = path.with_suffix(path.suffix + ".json")
    meta_tmp = meta.with_name(f".{meta.name}.tmp")
    meta_tmp.write_text(json.dumps(metadata, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    meta_tmp.replace(meta)


def _snapshot_path(root: Path, finding: Finding, row: int, index: int, source: Path) -> Path:
    """Choose a stable, non-user-controlled evidence filename."""
    name = _SAFE_NAME.sub("-", source.name).strip("-") or "attachment.log"
    return (
        root
        / "setup-evidence"
        / "attachments"
        / "rows"
        / f"{finding.id or 'finding'}-{row}-{index}-{name}.txt"
    )


def _attachment_matches(raw: str, booley_dir: Path, sources: set[Path]) -> Path | None:
    """Return the selected source represented by one Finding attachment."""
    normalized = normalize_attachment_path(raw, booley_dir)
    return normalized if normalized in sources else None


def _rewrite_attachments_only(booley_dir: Path, entries: list[Finding]) -> None:
    """Atomically update attachments while retaining newer JSON fields."""
    path = log_path(booley_dir)
    replacements = {row: entry.attachments for row, entry in enumerate(entries)}
    row = 0
    output: list[str] = []
    for raw_line in path.read_text(encoding="utf-8").splitlines(keepends=True):
        if not raw_line.strip():
            output.append(raw_line)
            continue
        value = json.loads(raw_line)
        value["attachments"] = replacements[row]
        row += 1
        output.append(json.dumps(value, ensure_ascii=False, sort_keys=True) + "\n")
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text("".join(output), encoding="utf-8")
    temporary.replace(path)


def _rewrite_selected(
    booley_dir: Path,
    sources: set[Path],
    entries: list[Finding],
) -> tuple[set[Path], dict[int, list[str]]]:
    """Snapshot selected attachments and return the changed source map."""
    changed: set[Path] = set()
    before: dict[int, list[str]] = {}
    from booley.feedback import render

    for row, finding in enumerate(entries):
        before[row] = [
            line
            for attachment in finding.attachments
            for line in render._attachment_block(
                str(normalize_attachment_path(attachment, booley_dir))
            )
        ]
        for index, attachment in enumerate(list(finding.attachments)):
            source = _attachment_matches(attachment, booley_dir, sources)
            if source is None:
                continue
            try:
                content, metadata = _excerpt(source)
            except OSError as exc:
                raise MaterializationError(f"cannot read attachment {source}: {exc}") from exc
            target = _snapshot_path(booley_dir, finding, row, index, source)
            _write_snapshot(target, content, metadata)
            finding.attachments[index] = str(target)
            changed.add(source)
    return changed, before


def _render_proofs(
    booley_dir: Path,
    entries: list[Finding],
    environment: render.Environment,
) -> tuple[str, str]:
    """Render both Feedback views so attachment migration proves equivalence."""
    log = read_log(booley_dir)
    log.entries = entries
    origin = render.report_origin(log)
    local = render.render_user_report(log, env=environment, origin=origin)
    export_view = render.render_booley_report(
        log,
        booley_dir.parent,
        booley_dir=booley_dir,
        env=environment,
        origin=origin,
    ).body
    return local, export_view


def materialize_attachments(
    booley_dir: Path,
    sources: Iterable[Path],
    *,
    env: render.Environment | None = None,
) -> tuple[Path, ...]:
    """Retarget selected structured attachments after equivalent render proof."""
    selected = {Path(source).expanduser().resolve(strict=False) for source in sources}
    if not selected:
        return ()
    environment = env or render.collect_environment()
    with transaction(booley_dir, operation="materialize attachments in"):
        return _materialize_locked(booley_dir, selected, environment)


def _materialize_locked(
    booley_dir: Path, selected: set[Path], environment: render.Environment
) -> tuple[Path, ...]:
    """Keep fresh render proofs, snapshots, and raw publication in one transaction."""
    log = read_log(booley_dir)
    if log.corrupt_lines:
        raise CorruptFindingsLogError(
            f"refusing attachment materialization: {log.corrupt_lines} corrupt Findings Log line(s)"
        )
    before_proof = _render_proofs(booley_dir, log.entries, environment)
    changed, _ = _rewrite_selected(booley_dir, selected, log.entries)
    if not changed:
        return ()
    if before_proof != _render_proofs(booley_dir, log.entries, environment):
        raise MaterializationError("attachment report or export changed during materialization")
    _rewrite_attachments_only(booley_dir, log.entries)
    return tuple(sorted(changed))


def referenced_sources(booley_dir: Path, sources: Iterable[Path]) -> set[Path]:
    """Find structured attachment references without requiring a clean log."""
    selected = {path.expanduser().resolve(strict=False) for path in sources}
    log = read_log(booley_dir)
    found: set[Path] = set()
    for finding in log.entries:
        for attachment in finding.attachments:
            normalized = normalize_attachment_path(attachment, booley_dir)
            if normalized in selected:
                found.add(normalized)
    return found
