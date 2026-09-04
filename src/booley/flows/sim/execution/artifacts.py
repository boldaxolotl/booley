"""Simulation artifact naming and authorization helpers."""

from __future__ import annotations

import glob
import hashlib
import re
from dataclasses import dataclass
from pathlib import Path

from .freshness import ArtifactEvidence, ArtifactStamp, snapshot_artifact, validate_fresh_artifact

_SAFE_COMPONENT_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,127}\Z")


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
        bases = (Path("/"),) if Path(pattern).is_absolute() else search_roots
        for base in bases:
            rendered = Path(pattern) if Path(pattern).is_absolute() else base / pattern
            if glob.has_magic(str(rendered)):
                anchor = Path("/") if rendered.is_absolute() else Path()
                pattern_from_anchor = str(rendered).removeprefix("/")
                candidates = anchor.glob(pattern_from_anchor)
            else:
                candidates = (rendered,)
            for match in candidates:
                if match.is_file() and (resolved_match := match.resolve()) not in matches:
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
        stamps: list[tuple[Path, ArtifactStamp]] = []
        for path in _configured_trace_paths(patterns, roots):
            stamp = snapshot_artifact(path)
            if stamp is not None:
                stamps.append((path, stamp))
        return cls(resolved_run_cwd, roots, patterns, tuple(stamps))

    def validate_reported(
        self,
        reported: str,
        *,
        dispatched_ns: int,
    ) -> ArtifactEvidence:
        """Resolve and validate a trace path reported by the completed adapter."""
        path = Path(reported)
        if not path.is_absolute():
            path = self.run_cwd / path
        resolved = path.resolve()
        configured = resolved in _configured_trace_paths(self.patterns, self.roots)
        return validate_fresh_artifact(
            path,
            roots=self.roots,
            before=dict(self.before).get(resolved) if configured else None,
            explicitly_allowed=(path,) if configured else (),
            not_before_ns=None if configured else dispatched_ns,
        )


__all__ = ["TraceArtifactPolicy", "artifact_path_component"]
