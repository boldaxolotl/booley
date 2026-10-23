"""Snapshot the bounded excerpt of Findings Log attachments before cleanup removes them."""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Iterable
from pathlib import Path

from booley.feedback import findings, render
from booley.feedback.findings import CorruptFindingsLogError, Finding

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
    """Read the bounded excerpt the renderer shows, using the renderer's limits."""
    raw = source.read_bytes()
    lines = raw.decode("utf-8", errors="replace").splitlines()
    selected = "\n".join(lines[-render.MAX_ATTACHMENT_LINES :])
    excerpt = selected[-render.MAX_ATTACHMENT_CHARS :]
    metadata = {
        "original_path": str(source),
        "source_sha256": hashlib.sha256(raw).hexdigest(),
        "line_count": len(lines),
        "byte_count": len(raw),
        "clipped": len(lines) > render.MAX_ATTACHMENT_LINES or excerpt != selected,
        "excerpt_line_count": len(excerpt.splitlines()),
        "excerpt_char_count": len(excerpt),
    }
    return excerpt, metadata


def _snapshot(booley_dir: Path, finding: Finding, row: int, index: int, source: Path) -> str:
    """Write one row/index-keyed snapshot and its metadata sidecar."""
    try:
        content, metadata = _excerpt(source)
    except OSError as exc:
        raise MaterializationError(f"cannot read attachment {source}: {exc}") from exc
    name = _SAFE_NAME.sub("-", source.name).strip("-") or "attachment.log"
    target = booley_dir / "setup-evidence" / "attachments" / "rows"
    target = target / f"{finding.id or 'finding'}-{row}-{index}-{name}.txt"
    target.parent.mkdir(parents=True, exist_ok=True)
    meta = json.dumps(metadata, indent=2, sort_keys=True) + "\n"
    # The renderer re-reads the snapshot with splitlines(), which drops one trailing newline; pad it back.
    padded = content + "\n" if content.endswith("\n") else content
    for path, text in ((target, padded), (target.with_name(f"{target.name}.json"), meta)):
        temporary = path.with_name(f".{path.name}.tmp")
        temporary.write_text(text, encoding="utf-8")
        temporary.replace(path)
    return str(target)


def _rewrite_attachments_only(booley_dir: Path, entries: list[Finding]) -> None:
    """Atomically retarget attachments while keeping rows and unknown fields."""
    path = findings.log_path(booley_dir)
    rows = iter(entries)
    output: list[str] = []
    for raw_line in path.read_text(encoding="utf-8").splitlines(keepends=True):
        if not raw_line.strip():
            output.append(raw_line)
            continue
        value = json.loads(raw_line)
        value["attachments"] = next(rows).attachments
        output.append(json.dumps(value, ensure_ascii=False, sort_keys=True) + "\n")
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text("".join(output), encoding="utf-8")
    temporary.replace(path)


def materialize_attachments(booley_dir: Path, sources: Iterable[Path]) -> tuple[Path, ...]:
    """Snapshot selected attachments and retarget them under the log lock."""
    selected = {Path(source).expanduser().resolve(strict=False) for source in sources}
    if not selected:
        return ()
    with findings.transaction(booley_dir, operation="materialize attachments in"):
        log = findings.read_log(booley_dir)
        if log.corrupt_lines:
            raise CorruptFindingsLogError(
                f"refusing attachment materialization: {log.corrupt_lines} corrupt Findings Log line(s)"
            )
        changed: set[Path] = set()
        for row, finding in enumerate(log.entries):
            for index, attachment in enumerate(finding.attachments):
                source = normalize_attachment_path(attachment, booley_dir)
                if source in selected:
                    finding.attachments[index] = _snapshot(booley_dir, finding, row, index, source)
                    changed.add(source)
        if changed:
            _rewrite_attachments_only(booley_dir, log.entries)
        return tuple(sorted(changed))
