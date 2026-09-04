"""Simulation artifact naming and authorization helpers."""

from __future__ import annotations

import glob
import hashlib
import os
import re
from dataclasses import dataclass
from pathlib import Path

from .freshness import (
    ArtifactEvidence,
    ArtifactStamp,
    ArtifactValidationError,
    snapshot_artifact,
    validate_fresh_artifact,
)

_SAFE_COMPONENT_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,127}\Z")
_TRACE_FILE_PATTERNS = ("**/*.fst", "**/*.vcd")


def _without_symlink_escape(path: Path) -> Path | None:
    lexical = Path(os.path.normpath(path.absolute()))
    resolved = path.resolve()
    return resolved if resolved == lexical else None


def artifact_path_component(value: str) -> str:
    """Return one traversal-safe, bounded component representing *value*."""
    if _SAFE_COMPONENT_RE.fullmatch(value):
        return value
    digest = hashlib.sha256(value.encode("utf-8")).hexdigest()
    return f"~sha256-{digest}"


def _configured_trace_paths(
    patterns: tuple[str, ...],
    search_roots: tuple[Path, ...],
) -> tuple[Path, ...]:
    matches: list[Path] = []
    for pattern in patterns:
        pattern_path = Path(pattern)
        bases = (None,) if pattern_path.is_absolute() else search_roots
        for base in bases:
            rendered = pattern_path if base is None else base / pattern
            if glob.has_magic(str(rendered)):
                anchor = Path(rendered.anchor)
                relative_pattern = str(rendered)[len(rendered.anchor) :].lstrip("/\\")
                candidates = anchor.glob(relative_pattern)
            else:
                candidates = (rendered,)
            for match in candidates:
                resolved_match = _without_symlink_escape(match)
                if (
                    resolved_match is not None
                    and match.is_file()
                    and resolved_match not in matches
                ):
                    matches.append(resolved_match)
    return tuple(matches)


@dataclass(frozen=True)
class TraceArtifactPolicy:
    """Authorize and freshness-check trace evidence for one execution attempt."""

    run_cwd: Path
    roots: tuple[Path, ...]
    patterns: tuple[str, ...]
    before: tuple[tuple[Path, ArtifactStamp], ...]

    @classmethod
    def capture(
        cls,
        *,
        run_cwd: Path,
        build_root: Path,
        patterns: tuple[str, ...],
    ) -> TraceArtifactPolicy:
        """Capture configured trace identities immediately before dispatch."""
        resolved_run_cwd = run_cwd.resolve()
        roots = (resolved_run_cwd, build_root.resolve())
        known_paths = set(_configured_trace_paths(patterns, roots))
        for root in roots:
            if not root.is_dir():
                continue
            for pattern in _TRACE_FILE_PATTERNS:
                for path in root.glob(pattern):
                    resolved = _without_symlink_escape(path)
                    if resolved is not None and path.is_file():
                        known_paths.add(resolved)
        stamps: list[tuple[Path, ArtifactStamp]] = []
        for path in known_paths:
            stamp = snapshot_artifact(path)
            if stamp is not None:
                stamps.append((path, stamp))
        return cls(resolved_run_cwd, roots, patterns, tuple(stamps))

    def validate_reported(
        self,
        reported: str,
    ) -> ArtifactEvidence:
        """Resolve and validate a trace path reported by the completed adapter."""
        path = Path(reported)
        if not path.is_absolute():
            path = self.run_cwd / path
        resolved = _without_symlink_escape(path)
        if resolved is None:
            raise ArtifactValidationError(
                f"Simulation trace path traverses a symbolic link: {path}"
            )
        configured = resolved in _configured_trace_paths(self.patterns, self.roots)
        return validate_fresh_artifact(
            path,
            roots=self.roots,
            before=dict(self.before).get(resolved),
            explicitly_allowed=(path,) if configured else (),
        )


__all__ = ["TraceArtifactPolicy", "artifact_path_component"]
