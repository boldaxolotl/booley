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

from booley.feedback.findings import CorruptFindingsLogError, Finding, log_path, read_log

MAX_ATTACHMENT_LINES = 120
MAX_ATTACHMENT_CHARS = 8000
_SAFE_NAME = re.compile(r"[^A-Za-z0-9_.-]+")


class MaterializationError(RuntimeError):
    """Raised when attachment evidence cannot be snapshotted safely."""


def normalize_attachment_path(raw: str, project_dir: Path) -> Path:
    """Resolve absolute, ``~``, and project-relative attachment spellings."""
    candidate = Path(raw).expanduser()
    if candidate.is_absolute():
        return candidate.resolve(strict=False)
    for base in (project_dir.parent, project_dir, Path.cwd()):
        resolved = (base / candidate).resolve(strict=False)
        if resolved.exists():
            return resolved
    return (project_dir.parent / candidate).resolve(strict=False)


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


def _snapshot_path(root: Path, finding: Finding, index: int, source: Path) -> Path:
    """Choose a stable, non-user-controlled evidence filename."""
    name = _SAFE_NAME.sub("-", source.name).strip("-") or "attachment.log"
    return root / "setup-evidence" / "attachments" / f"{finding.id or 'finding'}-{index}-{name}.txt"


def _attachment_matches(raw: str, project_dir: Path, sources: set[Path]) -> Path | None:
    """Return the selected source represented by one Finding attachment."""
    normalized = normalize_attachment_path(raw, project_dir)
    return normalized if normalized in sources else None


def _rewrite_attachments_only(project_dir: Path, entries: list[Finding]) -> None:
    """Atomically update attachments while retaining newer JSON fields."""
    path = log_path(project_dir)
    replacements = {entry.id: entry.attachments for entry in entries}
    output: list[str] = []
    for raw_line in path.read_text(encoding="utf-8").splitlines(keepends=True):
        if not raw_line.strip():
            output.append(raw_line)
            continue
        value = json.loads(raw_line)
        finding_id = value.get("id") if isinstance(value, dict) else None
        if finding_id in replacements:
            value["attachments"] = replacements[finding_id]
        output.append(json.dumps(value, ensure_ascii=False, sort_keys=True) + "\n")
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text("".join(output), encoding="utf-8")
    temporary.replace(path)


def _rewrite_selected(
    project_dir: Path,
    sources: set[Path],
    entries: list[Finding],
) -> tuple[set[Path], dict[str, list[str]]]:
    """Snapshot selected attachments and return the changed source map."""
    changed: set[Path] = set()
    before: dict[str, list[str]] = {}
    from booley.feedback import render

    for finding in entries:
        before[finding.id] = [
            line
            for attachment in finding.attachments
            for line in render._attachment_block(str(normalize_attachment_path(attachment, project_dir)))
        ]
        for index, attachment in enumerate(list(finding.attachments)):
            source = _attachment_matches(attachment, project_dir, sources)
            if source is None:
                continue
            try:
                content, metadata = _excerpt(source)
            except OSError as exc:
                raise MaterializationError(f"cannot read attachment {source}: {exc}") from exc
            target = _snapshot_path(project_dir, finding, index, source)
            _write_snapshot(target, content, metadata)
            finding.attachments[index] = str(target)
            changed.add(source)
    return changed, before


def materialize_attachments(project_dir: Path, sources: Iterable[Path]) -> tuple[Path, ...]:
    """Retarget selected structured attachments after equivalent render proof."""
    selected = {Path(source).expanduser().resolve(strict=False) for source in sources}
    if not selected:
        return ()
    log = read_log(project_dir)
    if log.corrupt_lines:
        raise CorruptFindingsLogError(
            f"refusing attachment materialization: {log.corrupt_lines} corrupt Findings Log line(s)"
        )
    changed, before = _rewrite_selected(project_dir, selected, log.entries)
    if not changed:
        return ()
    from booley.feedback import render

    for finding in log.entries:
        if finding.id not in before:
            continue
        expected = before[finding.id]
        actual: list[str] = []
        for attachment in finding.attachments:
            actual.extend(render._attachment_block(attachment))
        if expected and expected != actual:
            raise MaterializationError(f"attachment render changed for {finding.id}")
    _rewrite_attachments_only(project_dir, log.entries)
    return tuple(sorted(changed))


def referenced_sources(project_dir: Path, sources: Iterable[Path]) -> set[Path]:
    """Find structured attachment references without requiring a clean log."""
    selected = {path.expanduser().resolve(strict=False) for path in sources}
    log = read_log(project_dir)
    found: set[Path] = set()
    for finding in log.entries:
        for attachment in finding.attachments:
            normalized = normalize_attachment_path(attachment, project_dir)
            if normalized in selected:
                found.add(normalized)
    return found
