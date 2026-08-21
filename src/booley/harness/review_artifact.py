"""Immutable typed records for persisted review package version 2."""

from __future__ import annotations

from collections.abc import Iterator, Mapping
from dataclasses import dataclass
from pathlib import Path, PurePosixPath, PureWindowsPath
from types import MappingProxyType
from typing import Any

from booley.core.boundary import BoundaryError, require_dict, require_str
from booley.harness.review_explanation import ExplanationError, StructuredExplanation

PACKAGE_VERSION = 2
FILE_ACTIONS = frozenset({"added", "modified", "deleted", "renamed", "copied", "type-changed"})
CONTENT_KINDS = frozenset({"regular", "symlink", "submodule"})
PRESENTATIONS = frozenset({"text", "binary", "unavailable"})
CRITERION_OUTCOMES = frozenset({"met", "unmet", "not_run"})
CRITERION_FRESHNESS = frozenset({"current", "stale", "unknown"})
REVIEW_DISPOSITIONS = frozenset({"reported", "open", "fixed", "waived", "excluded"})
RECOMMENDATIONS = frozenset({"approve", "reset", "archive", "hold"})


class ReviewArtifactError(ValueError):
    """Persisted review data violates the version-2 contract."""


def _freeze(value: Any) -> Any:
    if isinstance(value, Mapping):
        return MappingProxyType({str(key): _freeze(item) for key, item in value.items()})
    if isinstance(value, list | tuple):
        return tuple(_freeze(item) for item in value)
    return value


def _thaw(value: Any) -> Any:
    if isinstance(value, Mapping):
        return {key: _thaw(item) for key, item in value.items()}
    if isinstance(value, tuple):
        return [_thaw(item) for item in value]
    return value


def _rows(value: Any, field: str) -> list[Any]:
    if not isinstance(value, list):
        raise ReviewArtifactError(f"{field} must be a list")
    return value


def _strings(value: Any, field: str) -> tuple[str, ...]:
    rows = _rows(value, field)
    if any(not isinstance(item, str) for item in rows):
        raise ReviewArtifactError(f"{field} must contain only strings")
    return tuple(rows)


def _enum(row: Mapping[str, Any], key: str, allowed: frozenset[str]) -> str:
    value = require_str(row, key)
    if value not in allowed:
        raise ReviewArtifactError(f"unknown {key}: {value!r}")
    return value


def _optional_path(row: Mapping[str, Any], key: str) -> str | None:
    value = row.get(key)
    if value is None:
        return None
    if (
        not isinstance(value, str)
        or not value
        or not (
            Path(value).is_absolute()
            or PurePosixPath(value).is_absolute()
            or PureWindowsPath(value).is_absolute()
        )
    ):
        raise ReviewArtifactError(f"{key} must be an absolute path or null")
    return value


def _repository_path(row: Mapping[str, Any]) -> str:
    value = require_str(row, "repository_path")
    path = PurePosixPath(value)
    if path.is_absolute() or ".." in path.parts or value in {"", "."}:
        raise ReviewArtifactError(f"unsafe repository endpoint path: {value!r}")
    return path.as_posix()


@dataclass(frozen=True)
class RepositoryRevision:
    """Identity and revision range of one reviewed repository."""

    name: str
    base_sha: str
    head_sha: str
    worktree: str

    @classmethod
    def parse(cls, value: Any) -> RepositoryRevision:
        row = require_dict(value, field="repository revision")
        worktree = _optional_path(row, "worktree")
        if worktree is None:
            raise ReviewArtifactError("repository worktree must not be null")
        return cls(
            require_str(row, "name"),
            require_str(row, "base_sha"),
            require_str(row, "head_sha"),
            worktree,
        )

    def to_dict(self) -> dict[str, str]:
        return {
            "name": self.name,
            "base_sha": self.base_sha,
            "head_sha": self.head_sha,
            "worktree": self.worktree,
        }


@dataclass(frozen=True)
class FileEndpoint:
    """One repository object endpoint and its materialized presentation."""

    repository_path: str
    display_path: str
    revision: str
    diff_path: str
    workspace_path: str | None

    @classmethod
    def parse(cls, value: Any) -> FileEndpoint:
        row = require_dict(value, field="file endpoint")
        diff_path = _optional_path(row, "diff_path")
        if diff_path is None:
            raise ReviewArtifactError("diff_path must not be null")
        return cls(
            _repository_path(row),
            require_str(row, "display_path"),
            require_str(row, "revision"),
            diff_path,
            _optional_path(row, "workspace_path"),
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "repository_path": self.repository_path,
            "display_path": self.display_path,
            "revision": self.revision,
            "diff_path": self.diff_path,
            "workspace_path": self.workspace_path,
        }


@dataclass(frozen=True)
class FileChange:
    """One change with independent action, content, and presentation axes."""

    repository: str
    action: str
    content_kind: str
    presentation: str
    similarity: int | None
    old: FileEndpoint
    new: FileEndpoint
    status: str

    @classmethod
    def parse(cls, value: Any) -> FileChange:
        row = require_dict(value, field="file change")
        similarity = row.get("similarity")
        if similarity is not None and (
            isinstance(similarity, bool)
            or not isinstance(similarity, int)
            or not 0 <= similarity <= 100
        ):
            raise ReviewArtifactError("file similarity must be an integer from 0 through 100")
        action = _enum(row, "action", FILE_ACTIONS)
        if action in {"renamed", "copied"} and similarity is None:
            raise ReviewArtifactError(f"{action} file change requires similarity")
        return cls(
            require_str(row, "repository"),
            action,
            _enum(row, "content_kind", CONTENT_KINDS),
            _enum(row, "presentation", PRESENTATIONS),
            similarity,
            FileEndpoint.parse(row.get("old_endpoint")),
            FileEndpoint.parse(row.get("new_endpoint")),
            require_str(row, "status"),
        )

    def to_dict(self) -> dict[str, Any]:
        old = self.old.to_dict()
        new = self.new.to_dict()
        return {
            "repository": self.repository,
            "action": self.action,
            "content_kind": self.content_kind,
            "presentation": self.presentation,
            "similarity": self.similarity,
            "status": self.status,
            "path": self.new.display_path,
            "old_path": self.old.display_path if self.action in {"renamed", "copied"} else None,
            "diff_left": self.old.diff_path,
            "diff_right": self.new.diff_path,
            "old_endpoint": old,
            "new_endpoint": new,
        }


@dataclass(frozen=True)
class CriterionRow:
    """Criterion outcome kept independent from evidence freshness."""

    category: str
    criterion: str
    required: bool
    outcome: str
    freshness: str
    metric: str

    @classmethod
    def parse(cls, value: Any) -> CriterionRow:
        row = require_dict(value, field="criterion row")
        required_value = row.get("required")
        if required_value not in {"mandatory", "optional"}:
            raise ReviewArtifactError("criterion required must be mandatory or optional")
        return cls(
            require_str(row, "category"),
            require_str(row, "criterion"),
            required_value == "mandatory",
            _enum(row, "outcome", CRITERION_OUTCOMES),
            _enum(row, "freshness", CRITERION_FRESHNESS),
            require_str(row, "metric"),
        )

    def to_dict(self) -> dict[str, Any]:
        status = "STALE" if self.freshness == "stale" else self.outcome.replace("_", " ")
        return {
            "category": self.category,
            "criterion": self.criterion,
            "required": "mandatory" if self.required else "optional",
            "outcome": self.outcome,
            "freshness": self.freshness,
            "status": status,
            "metric": self.metric,
        }


@dataclass(frozen=True)
class ReviewDispositionRow:
    """One deterministic review finding and its current disposition."""

    criterion: str
    finding_id: str
    severity: str
    file: str
    line: int
    summary: str
    disposition: str
    evidence: str
    justification: str
    exclusion_reason: str
    actor: str

    @classmethod
    def parse(cls, value: Any) -> ReviewDispositionRow:
        row = require_dict(value, field="review disposition")
        line = row.get("line")
        if not isinstance(line, int) or isinstance(line, bool) or line < 0:
            raise ReviewArtifactError("review disposition line must be non-negative integer")
        disposition = _enum(row, "disposition", REVIEW_DISPOSITIONS)
        justification = str(row.get("justification", ""))
        if disposition == "waived" and not justification.strip():
            raise ReviewArtifactError("waived review disposition needs justification")
        return cls(
            criterion=require_str(row, "criterion"),
            finding_id=str(row.get("finding_id", "")),
            severity=require_str(row, "severity"),
            file=str(row.get("file", "")),
            line=line,
            summary=require_str(row, "summary"),
            disposition=disposition,
            evidence=str(row.get("evidence", "")),
            justification=justification,
            exclusion_reason=str(row.get("exclusion_reason", "")),
            actor=str(row.get("actor", "")),
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "criterion": self.criterion,
            "finding_id": self.finding_id,
            "severity": self.severity,
            "file": self.file,
            "line": self.line,
            "summary": self.summary,
            "disposition": self.disposition,
            "evidence": self.evidence,
            "justification": self.justification,
            "exclusion_reason": self.exclusion_reason,
            "actor": self.actor,
        }


@dataclass(frozen=True)
class ScopeAssessment:
    path: str
    classification: str
    reason: str

    @classmethod
    def parse(cls, value: Any) -> ScopeAssessment:
        row = require_dict(value, field="scope assessment")
        classification = require_str(row, "classification")
        if classification not in {"Justified", "Unjustified", "Needs review"}:
            raise ReviewArtifactError(f"unknown scope classification: {classification!r}")
        return cls(require_str(row, "path"), classification, require_str(row, "reason"))

    def to_dict(self) -> dict[str, str]:
        return {"path": self.path, "classification": self.classification, "reason": self.reason}


@dataclass(frozen=True)
class SemanticAssessment:
    recommendation: str
    reason: str
    decision_blockers: tuple[str, ...]
    scope_deviations: tuple[ScopeAssessment, ...]
    developer_summary: str
    uncertainties: str
    optional_omissions: str
    findings: tuple[str, ...]

    @classmethod
    def parse(cls, value: Any) -> SemanticAssessment:
        row = require_dict(value, field="semantic assessment")
        recommendation = _enum(row, "recommendation", RECOMMENDATIONS)
        return cls(
            recommendation,
            require_str(row, "reason"),
            _strings(row.get("decision_blockers"), "decision_blockers"),
            tuple(
                ScopeAssessment.parse(item)
                for item in _rows(row.get("scope_deviations"), "scope_deviations")
            ),
            require_str(row, "developer_summary"),
            require_str(row, "uncertainties"),
            require_str(row, "optional_omissions"),
            _strings(row.get("findings"), "findings"),
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "recommendation": self.recommendation,
            "reason": self.reason,
            "decision_blockers": list(self.decision_blockers),
            "scope_deviations": [row.to_dict() for row in self.scope_deviations],
            "developer_summary": self.developer_summary,
            "uncertainties": self.uncertainties,
            "optional_omissions": self.optional_omissions,
            "findings": list(self.findings),
        }


@dataclass(frozen=True)
class ReviewPackage(Mapping[str, Any]):
    """Composed immutable review package consumed by every presentation."""

    slug: str
    repositories: tuple[RepositoryRevision, ...]
    criteria: tuple[CriterionRow, ...]
    review_dispositions: tuple[ReviewDispositionRow, ...]
    changed_files: tuple[FileChange, ...]
    assessment: SemanticAssessment
    commits: tuple[Mapping[str, Any], ...]
    scope: Mapping[str, Any]
    recipe_comparisons: tuple[Mapping[str, Any], ...]
    developer_report_path: str
    html_path: str | None
    explanation: StructuredExplanation | None
    run_economics: str
    health: Mapping[str, Any]
    feature_branch: str
    kind: str = "review"
    version: int = PACKAGE_VERSION

    @classmethod
    def parse(cls, value: Any) -> ReviewPackage:
        try:
            row = require_dict(value, field="review package")
            if row.get("version") != PACKAGE_VERSION:
                raise ReviewArtifactError(
                    f"unsupported review package version: {row.get('version')!r}"
                )
            repositories = tuple(
                RepositoryRevision.parse(item)
                for item in _rows(row.get("repositories"), "repositories")
            )
            if not repositories:
                raise ReviewArtifactError("review package needs at least one repository")
            report_path = _optional_path(row, "developer_report_path")
            if report_path is None:
                raise ReviewArtifactError("developer_report_path must not be null")
            return cls(
                slug=require_str(row, "slug"),
                repositories=repositories,
                criteria=tuple(
                    CriterionRow.parse(item) for item in _rows(row.get("criteria"), "criteria")
                ),
                review_dispositions=tuple(
                    ReviewDispositionRow.parse(item)
                    for item in _rows(
                        row.get("review_dispositions", []),
                        "review_dispositions",
                    )
                ),
                changed_files=tuple(
                    FileChange.parse(item)
                    for item in _rows(row.get("changed_files"), "changed_files")
                ),
                assessment=SemanticAssessment.parse(row.get("assessment")),
                commits=tuple(
                    _freeze(require_dict(item, field="commit"))
                    for item in _rows(row.get("commits"), "commits")
                ),
                scope=_freeze(require_dict(row.get("scope", {}), field="scope")),
                recipe_comparisons=tuple(
                    _freeze(require_dict(item, field="recipe comparison"))
                    for item in _rows(row.get("recipe_comparisons", []), "recipe_comparisons")
                ),
                developer_report_path=report_path,
                html_path=_optional_path(row, "html_path"),
                explanation=(
                    StructuredExplanation.parse(row["explanation"])
                    if row.get("explanation") is not None
                    else None
                ),
                run_economics=require_str(row, "run_economics"),
                health=_freeze(require_dict(row.get("health"), field="health")),
                feature_branch=str(row.get("feature_branch", "")),
                kind=str(row.get("kind", "review")),
            )
        except (BoundaryError, ExplanationError) as exc:
            raise ReviewArtifactError(str(exc)) from exc

    def to_dict(self) -> dict[str, Any]:
        primary = self.repositories[0]
        return {
            "version": self.version,
            "kind": self.kind,
            "slug": self.slug,
            "feature_branch": self.feature_branch,
            "base_sha": primary.base_sha,
            "head_sha": primary.head_sha,
            "worktree": primary.worktree,
            "repositories": [row.to_dict() for row in self.repositories],
            "criteria": [row.to_dict() for row in self.criteria],
            "review_dispositions": [row.to_dict() for row in self.review_dispositions],
            "recipe_comparisons": [_thaw(row) for row in self.recipe_comparisons],
            "scope": _thaw(self.scope),
            "commits": [_thaw(row) for row in self.commits],
            "changed_files": [row.to_dict() for row in self.changed_files],
            "developer_report_path": self.developer_report_path,
            "run_economics": self.run_economics,
            "health": _thaw(self.health),
            "assessment": self.assessment.to_dict(),
            "html_path": self.html_path,
            "explanation": self.explanation.to_dict() if self.explanation is not None else None,
        }

    def __getitem__(self, key: str) -> Any:
        return self.to_dict()[key]

    def __iter__(self) -> Iterator[str]:
        return iter(self.to_dict())

    def __len__(self) -> int:
        return len(self.to_dict())
