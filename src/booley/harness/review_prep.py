"""Precompute the rich HTML explanation before a successful ticket enters review."""

from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import shutil
import subprocess
import tarfile
import tempfile
import time
from base64 import b64encode
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
from html.parser import HTMLParser
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from booley.config.settings import get_backend_config, load_models_config
from booley.core.boundary import BoundaryError, require_dict, require_str
from booley.core.models import AgentCallParams, AgentResult
from booley.dev_support.development_state import DevelopmentState
from booley.runtime.agent import call_agent
from booley.runtime.paths import skills_dir
from booley.runtime.ticket_repositories import (
    paired_project_repository,
    project_repository_expected,
)
from booley.runtime.timefmt import utc_now_rfc3339
from booley.ticket_board.helpers import tickets_dir_from_project_root
from booley.ticket_board.io import TicketIO
from booley.ticket_board.paths import existing_runtime_file, ticket_runtime_dir

from .job_fence import wait_for_ticket_jobs
from .review_evidence import ReviewEvidenceError, ReviewEvidencePackage, build_review_evidence
from .triage_package import (
    TriagePackageError,
    build_review_facts,
    load_triage_package,
    open_package_diffs,
    render_review_briefing,
    validate_assessment,
    write_triage_package,
)

logger = logging.getLogger(__name__)

_PROMPT_FILE = "explain-diff-prompt.md"
_PROMPT_VERSION = 3
_MIN_HTML_CHARS = 1000
_REQUIRED_HTML_SECTIONS = ("background", "intuition", "code", "quiz")
_QUIZ_SCRIPT_BODY = """
document.addEventListener('click', function (event) {
  const button = event.target.closest('.quiz-option');
  if (!button) return;
  const question = button.closest('.quiz-question') || button.parentElement;
  const output = question.querySelector('.quiz-feedback');
  if (!output) return;
  const correct = button.dataset.correct === 'true';
  output.textContent = (correct ? 'Correct. ' : 'Not quite. ') +
    (button.dataset.feedback || 'Review the explanation and try again.');
  output.className = 'quiz-feedback ' + (correct ? 'correct' : 'incorrect');
});
""".strip()
_QUIZ_SCRIPT = f"<script>{_QUIZ_SCRIPT_BODY}</script>"
_CODE_BLOCK_STYLE = "<style>pre { white-space: pre-wrap !important; }</style>"
_SCRIPT_HASH = b64encode(hashlib.sha256(_QUIZ_SCRIPT_BODY.encode("utf-8")).digest()).decode()
_CSP = (
    f"default-src 'none'; style-src 'unsafe-inline'; script-src 'sha256-{_SCRIPT_HASH}'; "
    "img-src data:; font-src data:; connect-src 'none'; object-src 'none'; "
    "base-uri 'none'; form-action 'none'; frame-ancestors 'none'"
)
_DISALLOWED_CSS = (
    (re.compile(r"(?i)(?<![-\w])url\s*\("), "url()"),
    (re.compile(r"(?i)@import\b"), "@import"),
    (re.compile(r"(?i)(?<![-\w])expression\s*\("), "expression()"),
    (
        re.compile(r"(?i)(?:^|[;{])\s*[*_]?behavior\s*:"),
        "legacy behavior property",
    ),
)


class ReviewPrepError(RuntimeError):
    """The HTML explanation could not be prepared or validated."""


class ReviewPrepConcurrentChangeError(ReviewPrepError):
    """Live review inputs changed while an immutable package was being prepared."""


@dataclass(frozen=True)
class ProjectReviewRepository:
    """Immutable revision pair for the ticket's nested project repository."""

    worktree: Path
    base_sha: str
    head_sha: str
    feature_branch: str


@dataclass(frozen=True)
class ReviewPrepContext:
    """Resolved immutable inputs for one HTML-explanation generation."""

    project_root: Path
    slug: str
    log_dir: Path
    runtime_dir: Path
    worktree: Path
    ticket_path: Path
    base_sha: str
    head_sha: str
    feature_branch: str
    triage_report_enabled: bool = True
    project_repository: ProjectReviewRepository | None = None


@dataclass(frozen=True)
class ReviewPrepOutcome:
    """Result returned to automatic handoff and the manual retry command."""

    status: str
    message: str
    html_path: Path | None = None

    @property
    def ready(self) -> bool:
        return self.status in {"ready", "fresh"}


@dataclass(frozen=True)
class ReviewBriefingOutcome:
    """Fast-path result rendered from an already prepared review package."""

    status: str
    message: str
    briefing: str = ""
    diff_failures: tuple[str, ...] = ()


@dataclass(frozen=True)
class ReviewAgentWorkspace:
    """Disposable source snapshot and copied evidence exposed to the agent."""

    repository: Path
    evidence: dict[str, Path]


@dataclass(frozen=True)
class PreparedReviewOutput:
    """Validated agent output, with optional components degraded independently."""

    html_path: Path | None
    assessment: dict[str, Any]
    html_error: str | None = None
    assessment_error: str | None = None


def _atomic_write(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(dir=path.parent, prefix=path.stem, suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            stream.write(content)
        Path(tmp_name).replace(path)
    except BaseException:
        Path(tmp_name).unlink(missing_ok=True)
        raise


def _write_json(path: Path, value: dict[str, Any]) -> None:
    _atomic_write(path, json.dumps(value, indent=2, sort_keys=True) + "\n")


def _git(worktree: Path, *args: str, timeout: int = 60) -> str:
    try:
        result = subprocess.run(
            ["git", "-C", str(worktree), *args],
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
        )
    except (FileNotFoundError, subprocess.TimeoutExpired) as exc:
        raise ReviewPrepError(f"git {' '.join(args)} failed: {exc}") from exc
    if result.returncode != 0:
        detail = (result.stderr or result.stdout).strip()[:1000]
        raise ReviewPrepError(f"git {' '.join(args)} failed: {detail}")
    return result.stdout


def _resolve_base_sha(worktree: Path, entry: dict[str, Any], head_sha: str) -> str:
    configured = entry.get("base_sha")
    if isinstance(configured, str) and configured.strip():
        _git(worktree, "rev-parse", "--verify", f"{configured.strip()}^{{commit}}")
        return configured.strip()
    branch = entry.get("branch")
    if not isinstance(branch, str) or not branch:
        raise ReviewPrepError("ticket has neither base_sha nor a base branch")
    return _git(worktree, "merge-base", branch, head_sha).strip()


def _find_checkout(project_root: Path, feature_branch: str) -> Path | None:
    """Find a branch checkout without depending on the caller's current directory."""
    output = _git(project_root, "worktree", "list", "--porcelain")
    checkout: Path | None = None
    wanted = f"refs/heads/{feature_branch}"
    for line in [*output.splitlines(), ""]:
        if line.startswith("worktree "):
            checkout = Path(line.removeprefix("worktree "))
        elif line == f"branch {wanted}" and checkout is not None:
            return checkout.resolve()
        elif not line:
            checkout = None
    return None


def _resolve_context(
    project_root: Path,
    slug: str,
    *,
    require_review: bool = False,
    allow_report_disabled: bool = False,
) -> ReviewPrepContext:
    tickets_dir = tickets_dir_from_project_root(project_root)
    tio = TicketIO(tickets_dir, project_root=project_root)
    entry = tio.find_ticket(slug)
    if not entry:
        raise ReviewPrepError(f"ticket '{slug}' was not found")
    slug = Path(str(entry["file"])).stem
    allowed_statuses = (
        {"review", "blocked"}
        if require_review
        else {
            "running",
            "review",
            "blocked",
        }
    )
    if entry.get("status") not in allowed_statuses:
        expected = "review or blocked" if require_review else "running, review, or blocked"
        raise ReviewPrepError(f"ticket '{slug}' is {entry.get('status')}, not {expected}")
    on_success = entry.get("on_success")
    report_enabled = not (
        isinstance(on_success, dict) and on_success.get("triage_report") is False
    )
    if not report_enabled and not allow_report_disabled:
        raise ReviewPrepError(f"ticket '{slug}' has on_success.triage_report disabled")
    feature_branch = str(entry.get("feature_branch") or slug)
    checkout = _find_checkout(project_root, feature_branch)
    if not checkout:
        raise ReviewPrepError(f"no worktree has feature branch '{feature_branch}' checked out")
    worktree = Path(checkout).resolve()
    head_sha = _git(worktree, "rev-parse", "HEAD").strip()
    base_sha = _resolve_base_sha(worktree, entry, head_sha)
    project_repository = _resolve_project_review_repository(worktree, slug)
    return ReviewPrepContext(
        project_root=project_root,
        slug=slug,
        log_dir=tio.logs_dir / slug,
        runtime_dir=ticket_runtime_dir(tio.logs_dir / slug) / "triage-prep",
        worktree=worktree,
        ticket_path=tickets_dir / str(entry["file"]),
        base_sha=base_sha,
        head_sha=head_sha,
        feature_branch=feature_branch,
        triage_report_enabled=report_enabled,
        project_repository=project_repository,
    )


def _resolve_project_review_repository(
    worktree: Path, slug: str
) -> ProjectReviewRepository | None:
    repository = paired_project_repository(worktree)
    if repository is None:
        if project_repository_expected(worktree):
            raise ReviewPrepError("configured project repository has no paired ticket checkout")
        return None
    feature_branch = _git(repository.worktree, "branch", "--show-current").strip()
    expected = f"booley-ticket/{slug}"
    if feature_branch != expected:
        raise ReviewPrepError(
            f"paired project checkout uses branch {feature_branch!r}, expected {expected!r}"
        )
    head_sha = _git(repository.worktree, "rev-parse", "HEAD").strip()
    upstream = _git(repository.worktree, "rev-parse", "@{upstream}").strip()
    base_sha = _git(repository.worktree, "merge-base", upstream, head_sha).strip()
    return ProjectReviewRepository(repository.worktree, base_sha, head_sha, feature_branch)


def _prompt_text() -> str:
    path = skills_dir() / "booley-ticket-triage" / _PROMPT_FILE
    try:
        return path.read_text(encoding="utf-8")
    except OSError as exc:
        raise ReviewPrepError(f"could not read packaged Explain Diff prompt: {exc}") from exc


def _prompt_hash(prompt: str) -> str:
    return hashlib.sha256(prompt.encode("utf-8")).hexdigest()


def _manifest_path(ctx: ReviewPrepContext) -> Path:
    return ctx.runtime_dir / "manifest.json"


def _read_manifest(ctx: ReviewPrepContext) -> dict[str, Any] | None:
    path = _manifest_path(ctx)
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    return value if isinstance(value, dict) else None


def _file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _verified_artifact(manifest: dict[str, Any], name: str) -> Path | None:
    raw_path = manifest.get(f"{name}_path")
    expected_hash = manifest.get(f"{name}_sha256")
    if not isinstance(raw_path, str) or not isinstance(expected_hash, str):
        return None
    path = Path(raw_path)
    if not path.is_file():
        return None
    return path if _file_sha256(path) == expected_hash else None


def _source_paths(ctx: ReviewPrepContext) -> list[tuple[str, Path]]:
    paths = [("ticket", ctx.ticket_path)]
    for relative in (
        "REPORT.md",
        ".runtime/booley_state.json",
        ".runtime/scope_deviations.json",
        ".runtime/developer",
        ".runtime/flow-reports",
        ".runtime/mcp-tool-reports",
    ):
        candidate = ctx.log_dir / relative
        if candidate.is_dir():
            paths.extend(
                (str(path.relative_to(ctx.log_dir)), path)
                for path in sorted(candidate.rglob("*"))
                if path.is_file()
            )
        elif candidate.is_file():
            paths.append((relative, candidate))
    return paths


def _source_fingerprint(ctx: ReviewPrepContext) -> str:
    """Hash stable review inputs and the live Git status.

    Human logs are deliberately excluded: review preparation writes its own
    start/finish messages to ``run.log`` and ``harness.log``. Including those
    append-only logs makes the package reject its own activity as a concurrent
    source change. The agent still receives a copied pre-call ``run.log``.
    """
    digest = hashlib.sha256()
    status = _git(
        ctx.worktree,
        "status",
        "--porcelain=v1",
        "-z",
        "--untracked-files=all",
    )
    digest.update(status.encode("utf-8"))
    if ctx.project_repository is not None:
        project = ctx.project_repository
        digest.update(project.base_sha.encode("ascii"))
        digest.update(project.head_sha.encode("ascii"))
        digest.update(
            _git(
                project.worktree,
                "status",
                "--porcelain=v1",
                "-z",
                "--untracked-files=all",
            ).encode("utf-8")
        )
    for label, path in _source_paths(ctx):
        digest.update(label.encode("utf-8"))
        digest.update(b"\0")
        digest.update(_file_sha256(path).encode("ascii"))
        digest.update(b"\0")
    return digest.hexdigest()


def _require_unchanged(ctx: ReviewPrepContext, expected_sha: str, message: str) -> None:
    """Reject an evidence package if its live inputs changed while it was built."""
    if _source_fingerprint(ctx) != expected_sha:
        raise ReviewPrepConcurrentChangeError(message)


def _fresh_outcome(
    ctx: ReviewPrepContext,
    manifest: dict[str, Any] | None,
    prompt_sha: str,
    source_sha: str,
) -> ReviewPrepOutcome | None:
    if not manifest or manifest.get("status") != "ready":
        return None
    expected = {
        "version": _PROMPT_VERSION,
        "prompt_sha256": prompt_sha,
        "base_sha": ctx.base_sha,
        "head_sha": ctx.head_sha,
        "source_sha256": source_sha,
    }
    if any(manifest.get(key) != value for key, value in expected.items()):
        return None
    briefing_path = _verified_artifact(manifest, "briefing")
    if briefing_path is None:
        return None
    has_html = manifest.get("html_path") is not None
    html_path = _verified_artifact(manifest, "html") if has_html else None
    html_unavailable = not has_html and isinstance(manifest.get("html_error"), str)
    if (has_html and html_path is None) or (not has_html and not html_unavailable):
        return None
    message = (
        "existing review package is current"
        if html_path is not None
        else "existing review briefing is current; HTML is unavailable"
    )
    return ReviewPrepOutcome("fresh", message, html_path)


def _base_manifest(
    ctx: ReviewPrepContext, prompt_sha: str, source_sha: str, status: str
) -> dict[str, Any]:
    manifest = {
        "version": _PROMPT_VERSION,
        "status": status,
        "slug": ctx.slug,
        "feature_branch": ctx.feature_branch,
        "base_sha": ctx.base_sha,
        "head_sha": ctx.head_sha,
        "prompt_sha256": prompt_sha,
        "source_sha256": source_sha,
        "updated_at": utc_now_rfc3339(),
    }
    if ctx.project_repository is not None:
        manifest["project_base_sha"] = ctx.project_repository.base_sha
        manifest["project_head_sha"] = ctx.project_repository.head_sha
    return manifest


def _collect_git_evidence(ctx: ReviewPrepContext) -> dict[str, Path]:
    paths = {
        "diff": ctx.runtime_dir / "change.diff",
        "commits": ctx.runtime_dir / "commits.txt",
        "files": ctx.runtime_dir / "files.txt",
        "status": ctx.runtime_dir / "git-status.txt",
    }
    revision = f"{ctx.base_sha}..{ctx.head_sha}"
    _atomic_write(
        paths["diff"], _git(ctx.worktree, "diff", "--no-color", "--submodule=log", revision)
    )
    _atomic_write(paths["commits"], _git(ctx.worktree, "log", revision, "--format=- %h %s"))
    _atomic_write(paths["files"], _git(ctx.worktree, "diff", "--name-status", revision))
    _atomic_write(paths["status"], _git(ctx.worktree, "status", "--short"))
    if ctx.project_repository is not None:
        project = ctx.project_repository
        project_revision = f"{project.base_sha}..{project.head_sha}"
        _atomic_write(
            paths["diff"],
            paths["diff"].read_text(encoding="utf-8")
            + "\n\n# .booley_project repository\n"
            + _git(project.worktree, "diff", "--no-color", project_revision),
        )
        _atomic_write(
            paths["commits"],
            paths["commits"].read_text(encoding="utf-8")
            + "\n# .booley_project repository\n"
            + _git(project.worktree, "log", project_revision, "--format=- %h %s"),
        )
        project_files = _git(project.worktree, "diff", "--name-status", project_revision)
        prefixed_files = "\n".join(
            "\t".join([columns[0], *(f".booley_project/{path}" for path in columns[1:])])
            for line in project_files.splitlines()
            if len(columns := line.split("\t")) > 1
        )
        _atomic_write(
            paths["files"],
            paths["files"].read_text(encoding="utf-8") + "\n" + prefixed_files,
        )
        project_status = _git(project.worktree, "status", "--short")
        _atomic_write(
            paths["status"],
            paths["status"].read_text(encoding="utf-8")
            + "\n"
            + "\n".join(
                f"{line[:3]}.booley_project/{line[3:]}" for line in project_status.splitlines()
            ),
        )
    return paths


def _extract_repository_snapshot(archive_path: Path, repository: Path) -> None:
    """Extract regular Git-archive members without materializing symlinks."""
    repository.mkdir(parents=True)
    with tarfile.open(archive_path) as archive:
        for member in archive.getmembers():
            target = (repository / member.name).resolve()
            if repository.resolve() not in target.parents and target != repository.resolve():
                raise ReviewPrepError(f"unsafe path in git archive: {member.name}")
            if member.isdir():
                target.mkdir(parents=True, exist_ok=True)
                continue
            if not member.isfile():
                continue
            source = archive.extractfile(member)
            if source is None:
                raise ReviewPrepError(f"could not extract git archive member: {member.name}")
            target.parent.mkdir(parents=True, exist_ok=True)
            with source, target.open("wb") as stream:
                shutil.copyfileobj(source, stream)


def _copy_review_input(source: Path, destination: Path) -> Path | None:
    if source.is_symlink() or not source.exists():
        return None
    if source.is_file():
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, destination)
        return destination
    destination.mkdir(parents=True, exist_ok=True)
    for child in source.rglob("*"):
        if child.is_file() and not child.is_symlink():
            target = destination / child.relative_to(source)
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(child, target)
    return destination


def _review_sources(ctx: ReviewPrepContext, evidence: dict[str, Path]) -> dict[str, Path]:
    """Return every named source governed by the review-evidence contract."""
    candidates = {
        **evidence,
        "ticket": ctx.ticket_path,
        "developer_report": ctx.log_dir / "REPORT.md",
        "run_log": ctx.log_dir / "human-logs" / "run.log",
        "state": ctx.log_dir / ".runtime" / "booley_state.json",
        "scope_deviations": ctx.log_dir / ".runtime" / "scope_deviations.json",
        "developer_transcripts": ctx.log_dir / ".runtime" / "developer",
        "flow_reports": ctx.log_dir / ".runtime" / "flow-reports",
        "specialist_reports": ctx.log_dir / ".runtime" / "mcp-tool-reports",
        "triage_facts": ctx.runtime_dir / "facts.json",
    }
    return {
        name: path for name, path in candidates.items() if path.exists() and not path.is_symlink()
    }


def _build_evidence_package(
    ctx: ReviewPrepContext,
    source_sha: str,
    evidence: dict[str, Path],
) -> ReviewEvidencePackage:
    """Build and persist the single contract consumed by review preparation."""
    package = build_review_evidence(
        slug=ctx.slug,
        base_sha=ctx.base_sha,
        head_sha=ctx.head_sha,
        source_sha256=source_sha,
        sources=_review_sources(ctx, evidence),
    )
    _write_json(ctx.runtime_dir / "evidence-manifest.json", package.manifest.as_dict())
    return package


@contextmanager
def _agent_workspace(
    ctx: ReviewPrepContext, package: ReviewEvidencePackage
) -> Iterator[ReviewAgentWorkspace]:
    """Expose only a disposable commit snapshot and copied review evidence."""
    try:
        package.verify()
    except ReviewEvidenceError as exc:
        raise ReviewPrepConcurrentChangeError(str(exc)) from exc
    with tempfile.TemporaryDirectory(prefix=f"booley-review-{ctx.slug}-") as tmp_name:
        root = Path(tmp_name)
        repository = root / "repository"
        archive_path = root / "source.tar"
        _git(
            ctx.worktree,
            "archive",
            "--format=tar",
            f"--output={archive_path}",
            ctx.head_sha,
        )
        _extract_repository_snapshot(archive_path, repository)
        if ctx.project_repository is not None:
            project_archive = root / "project-source.tar"
            _git(
                ctx.project_repository.worktree,
                "archive",
                "--format=tar",
                f"--output={project_archive}",
                ctx.project_repository.head_sha,
            )
            _extract_repository_snapshot(project_archive, repository / ".booley_project")

        input_dir = root / "review-input"
        copied: dict[str, Path] = {}
        for name, source in package.sources.items():
            suffix = source.suffix if source.is_file() else ""
            destination = input_dir / f"{name}{suffix}"
            if copied_path := _copy_review_input(source, destination):
                copied[name] = copied_path
        try:
            package.verify(copied)
        except ReviewEvidenceError as exc:
            raise ReviewPrepConcurrentChangeError(str(exc)) from exc
        manifest_source = ctx.runtime_dir / "evidence-manifest.json"
        copied["evidence_manifest"] = (
            _copy_review_input(manifest_source, input_dir / manifest_source.name)
            or manifest_source
        )
        yield ReviewAgentWorkspace(repository=repository, evidence=copied)


def _build_user_prompt(
    ctx: ReviewPrepContext, exact_prompt: str, workspace: ReviewAgentWorkspace
) -> str:
    evidence_lines = "\n".join(f"- {name}: `{path}`" for name, path in workspace.evidence.items())
    return f"""{exact_prompt.rstrip()}

## Booley automated triage contract

This is ticket `{ctx.slug}` on `{ctx.feature_branch}`. Explain the immutable change
`{ctx.base_sha}..{ctx.head_sha}`. The repository at `{workspace.repository}` is a disposable
snapshot of `{ctx.head_sha}`; all evidence paths below are disposable copies.

Read the following prepared evidence and inspect surrounding repository code as needed:
{evidence_lines}

Return the self-contained Explain Diff document as `html` and the concise semantic
`assessment` requested by the structured response. Deterministic facts (criteria,
commits, changed files, health, and economics) are assembled by the harness; use
the supplied facts to judge them without restating exhaustive tables.
For `assessment.scope_deviations`, return exactly one row for every path listed in
the deterministic facts' `scope.deviations`, and do not add paths from elsewhere.

Do not modify any file. Do not include `<script>` tags or inline event-handler
attributes in the HTML; the harness injects the trusted quiz behavior. Render each
quiz option as `<button class="quiz-option" data-correct="true|false"
data-feedback="..."></button>` inside `.quiz-question`, with a `.quiz-feedback`
element. The harness, not you, chooses and writes the final output path.
"""


def _output_schema() -> dict[str, Any]:
    return {
        "type": "object",
        "properties": {
            "html": {"type": "string"},
            "assessment": {
                "type": "object",
                "properties": {
                    "recommendation": {
                        "type": "string",
                        "enum": ["approve", "reset", "archive", "hold"],
                    },
                    "reason": {"type": "string"},
                    "decision_blockers": {"type": "array", "items": {"type": "string"}},
                    "scope_deviations": {
                        "type": "array",
                        "description": (
                            "Exactly one assessment for every path in the supplied "
                            "deterministic facts' scope.deviations list."
                        ),
                        "items": {
                            "type": "object",
                            "properties": {
                                "path": {"type": "string"},
                                "classification": {
                                    "type": "string",
                                    "enum": ["Justified", "Unjustified"],
                                },
                                "reason": {"type": "string"},
                            },
                            "required": ["path", "classification", "reason"],
                            "additionalProperties": False,
                        },
                    },
                    "developer_summary": {"type": "string"},
                    "uncertainties": {"type": "string"},
                    "optional_omissions": {"type": "string"},
                    "findings": {"type": "array", "items": {"type": "string"}},
                },
                "required": [
                    "recommendation",
                    "reason",
                    "decision_blockers",
                    "scope_deviations",
                    "developer_summary",
                    "uncertainties",
                    "optional_omissions",
                    "findings",
                ],
                "additionalProperties": False,
            },
        },
        "required": ["html", "assessment"],
        "additionalProperties": False,
    }


async def _invoke_agent(
    ctx: ReviewPrepContext, exact_prompt: str, workspace: ReviewAgentWorkspace
) -> AgentResult:
    cfg = get_backend_config()
    params = AgentCallParams(
        prompt=_build_user_prompt(ctx, exact_prompt, workspace),
        system_prompt=(
            "You are a read-only senior reviewer preparing a human triage package. "
            "Ground every claim in the supplied ticket, Git evidence, logs, or source."
        ),
        model=cfg.model_for_role("triage_report", "standard"),
        reasoning_effort=cfg.effort_for_tier("standard"),
        cwd=workspace.repository,
        allowed_agent_capabilities=["Read", "Glob", "Grep"],
        output_format=_output_schema(),
        max_turns=80,
        timeout_seconds=1800,
        transcript_path=ctx.runtime_dir / "agent.jsonl",
        label="triage-report",
        nested_mcp_tools=[],
    )
    return await call_agent(params)


def _strip_untrusted_scripts(html: str) -> str:
    cleaned = re.sub(r"(?is)<script\b[^>]*>.*?</script\s*>", "", html)
    if re.search(r"(?i)<script\b", cleaned):
        raise ReviewPrepError("HTML contains an unterminated script element")
    return cleaned


def _validate_css(value: str) -> None:
    for pattern, construct in _DISALLOWED_CSS:
        if pattern.search(value):
            raise ReviewPrepError(f"HTML CSS contains disallowed {construct}")


class _SafetyHTMLParser(HTMLParser):
    """Reject active HTML features before the trusted quiz script is injected."""

    _FORBIDDEN_TAGS = frozenset({"base", "embed", "form", "iframe", "math", "object", "svg"})
    _FORBIDDEN_ATTRIBUTES = frozenset({"action", "formaction", "poster", "srcdoc", "xlink:href"})

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self._in_style = False

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        lowered_tag = tag.lower()
        if lowered_tag in self._FORBIDDEN_TAGS:
            raise ReviewPrepError(f"HTML contains forbidden <{lowered_tag}> content")
        if lowered_tag == "style":
            self._in_style = True
        normalized = {name.lower(): value or "" for name, value in attrs}
        if lowered_tag == "meta" and "http-equiv" in normalized:
            raise ReviewPrepError("HTML contains a forbidden meta policy")
        for name, value in normalized.items():
            if name.startswith("on"):
                raise ReviewPrepError("HTML contains an inline event handler")
            if name in self._FORBIDDEN_ATTRIBUTES:
                raise ReviewPrepError(f"HTML contains forbidden {name} content")
            if name == "style":
                _validate_css(value)
            if name == "href":
                stripped = value.strip()
                if stripped and not stripped.startswith("#"):
                    scheme = urlsplit(stripped).scheme or "external"
                    raise ReviewPrepError(f"HTML contains unsafe href scheme: {scheme}")
            if name == "src":
                stripped = value.strip()
                safe_image = re.fullmatch(
                    r"(?is)data:image/(?:png|gif|jpeg|webp);base64,[a-z0-9+/=\s]+",
                    stripped,
                )
                if stripped and safe_image is None:
                    scheme = urlsplit(stripped).scheme or "external"
                    raise ReviewPrepError(f"HTML contains unsafe src scheme: {scheme}")

    def handle_startendtag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        self.handle_starttag(tag, attrs)
        if tag.lower() == "style":
            self._in_style = False

    def handle_endtag(self, tag: str) -> None:
        if tag.lower() == "style":
            self._in_style = False

    def handle_data(self, data: str) -> None:
        if self._in_style:
            _validate_css(data)


def _validate_html_source(html: str) -> None:
    lowered = html.lower()
    if len(html) < _MIN_HTML_CHARS or "<html" not in lowered or "</html>" not in lowered:
        raise ReviewPrepError("HTML is incomplete")
    missing = [section for section in _REQUIRED_HTML_SECTIONS if section not in lowered]
    if missing:
        raise ReviewPrepError(f"HTML is missing required sections: {', '.join(missing)}")
    if len(re.findall(r"(?i)class\s*=\s*['\"][^'\"]*quiz-question", html)) != 5:
        raise ReviewPrepError("HTML must contain exactly five quiz questions")
    if len(re.findall(r"(?i)class\s*=\s*['\"][^'\"]*quiz-option", html)) < 10:
        raise ReviewPrepError("HTML quiz questions need multiple-choice options")
    parser = _SafetyHTMLParser()
    try:
        parser.feed(html)
        parser.close()
    except ReviewPrepError:
        raise
    except Exception as exc:
        raise ReviewPrepError(f"HTML could not be safely parsed: {exc}") from exc


def _secure_html(structured: dict[str, Any]) -> str:
    html = require_str(structured, "html").strip()
    html = _strip_untrusted_scripts(html)
    _validate_html_source(html)
    csp = f'<meta http-equiv="Content-Security-Policy" content="{_CSP}">'
    trusted_head = f"{csp}\n{_CODE_BLOCK_STYLE}"
    html, count = re.subn(r"(?i)(<head\b[^>]*>)", rf"\1\n{trusted_head}", html, count=1)
    if count != 1:
        raise ReviewPrepError("HTML has no head element for the security policy")
    html, count = re.subn(r"(?i)</body\s*>", _QUIZ_SCRIPT + "\n</body>", html, count=1)
    if count != 1:
        raise ReviewPrepError("HTML has no body element for quiz behavior")
    return html.rstrip() + "\n"


def _error_summary(exc: Exception) -> str:
    return f"{type(exc).__name__}: {exc}"[:2000]


def _fallback_assessment(facts: dict[str, Any], error: str) -> dict[str, Any]:
    scope = facts.get("scope", {})
    deviations = scope.get("deviations", []) if isinstance(scope, dict) else []
    return {
        "recommendation": "hold",
        "reason": "the agent-prepared assessment was unavailable",
        "decision_blockers": ["Review the deterministic evidence before approval."],
        "scope_deviations": [
            {
                "path": path,
                "classification": "Needs review",
                "reason": "The report agent did not return a usable assessment.",
            }
            for path in deviations
            if isinstance(path, str)
        ],
        "developer_summary": "Inspect the prepared diffs and developer report manually.",
        "uncertainties": "The semantic report-agent assessment could not be validated.",
        "optional_omissions": "Agent-prepared semantic assessment.",
        "findings": [f"Agent assessment unavailable: {error}"],
    }


def _write_output(
    ctx: ReviewPrepContext, structured_value: Any, facts: dict[str, Any]
) -> PreparedReviewOutput:
    try:
        structured = require_dict(structured_value, field="triage report response")
    except BoundaryError as exc:
        error = _error_summary(exc)
        return PreparedReviewOutput(None, _fallback_assessment(facts, error), error, error)

    assessment_error = None
    try:
        assessment = validate_assessment(structured.get("assessment"), facts)
    except (BoundaryError, TriagePackageError) as exc:
        assessment_error = _error_summary(exc)
        assessment = _fallback_assessment(facts, assessment_error)

    try:
        html = _secure_html(structured)
    except (BoundaryError, ReviewPrepError) as exc:
        html_error = _error_summary(exc)
        assessment["findings"] = [
            *assessment["findings"],
            f"HTML explanation unavailable: {html_error}",
        ]
        return PreparedReviewOutput(None, assessment, html_error, assessment_error)

    html_name = f"{datetime.now(UTC):%Y-%m-%d}-explanation-{ctx.slug}.html"
    html_path = ctx.log_dir / html_name
    _atomic_write(html_path, html)
    return PreparedReviewOutput(html_path, assessment, assessment_error=assessment_error)


def _record_call(
    ctx: ReviewPrepContext,
    result: AgentResult | None,
    duration: float,
    *,
    exit_code: int,
) -> None:
    state_path = existing_runtime_file(ctx.log_dir.parent, ctx.slug, "booley_state.json")
    if not state_path.is_file():
        return
    state = DevelopmentState.load(state_path)
    state.record_mcp_tool_run(
        "triage_report",
        exit_code,
        duration_s=duration,
        cost_usd=result.cost_usd if result is not None else None,
    )
    state.save()


def _failure_outcome(
    ctx: ReviewPrepContext,
    prompt_sha: str,
    source_sha: str,
    exc: Exception,
    duration: float,
    result: AgentResult | None,
    *,
    status: str = "failed",
    record_call: bool = True,
) -> ReviewPrepOutcome:
    logger.exception("Triage report preparation failed for %s", ctx.slug)
    if record_call and not isinstance(exc, ReviewPrepConcurrentChangeError):
        try:
            _record_call(ctx, result, duration, exit_code=2)
        except Exception:  # Preserve the original preparation failure.
            logger.exception("Could not record triage report failure for %s", ctx.slug)
    try:
        source_sha = _source_fingerprint(ctx)
    except Exception:
        logger.exception("Could not refresh failed triage source hash for %s", ctx.slug)
    manifest = _base_manifest(ctx, prompt_sha, source_sha, status)
    manifest["error"] = f"{type(exc).__name__}: {exc}"[:2000]
    if result is not None:
        manifest["cost_usd"] = round(result.cost_usd, 4)
    try:
        _write_json(_manifest_path(ctx), manifest)
    except OSError:
        logger.exception("Could not write failed triage manifest for %s", ctx.slug)
    return ReviewPrepOutcome(status, manifest["error"])


def _write_early_failure(project_root: Path, slug: str, exc: Exception) -> None:
    """Persist setup failures when the ticket log directory can be identified."""
    log_dir = tickets_dir_from_project_root(project_root) / "logs" / slug
    if not log_dir.is_dir():
        return
    manifest = {
        "version": _PROMPT_VERSION,
        "status": "failed",
        "slug": slug,
        "error": f"{type(exc).__name__}: {exc}"[:2000],
        "updated_at": utc_now_rfc3339(),
    }
    try:
        _write_json(ticket_runtime_dir(log_dir) / "triage-prep" / "manifest.json", manifest)
    except OSError:
        logger.exception("Could not write early triage failure manifest for %s", slug)


def _failure_status(exc: Exception) -> str:
    """Map preparation exceptions to persisted outcome states."""
    return "changed" if isinstance(exc, ReviewPrepConcurrentChangeError) else "failed"


async def _resolve_stable_context(project_root: Path, slug: str) -> ReviewPrepContext:
    """Resolve review inputs after any detached ticket jobs have drained."""
    ctx = _resolve_context(project_root, slug)
    if await wait_for_ticket_jobs(ctx.log_dir):
        return _resolve_context(project_root, slug)
    return ctx


async def prepare_review(  # noqa: PLR0915 - lifecycle remains linear and auditable
    project_root: Path, slug: str, *, force: bool = False
) -> ReviewPrepOutcome:
    """Prepare one ticket's HTML explanation; failures are returned, never raised."""
    started = time.monotonic()
    call_recorded = False
    try:
        resolved_root = project_root.resolve()
        ctx = await _resolve_stable_context(resolved_root, slug)
        exact_prompt = _prompt_text()
        prompt_sha = _prompt_hash(exact_prompt)
        source_sha = _source_fingerprint(ctx)
        if not force and (
            fresh := _fresh_outcome(ctx, _read_manifest(ctx), prompt_sha, source_sha)
        ):
            return fresh
        _write_json(_manifest_path(ctx), _base_manifest(ctx, prompt_sha, source_sha, "running"))
        evidence = _collect_git_evidence(ctx)
        facts = build_review_facts(ctx)
        _write_json(ctx.runtime_dir / "facts.json", facts)
        package = _build_evidence_package(ctx, source_sha, evidence)
        source_sha = _source_fingerprint(ctx)
        with _agent_workspace(ctx, package) as workspace:
            _require_unchanged(
                ctx,
                source_sha,
                "live review inputs changed while the immutable snapshot was copied",
            )
            result = await _invoke_agent(ctx, exact_prompt, workspace)
        _require_unchanged(
            ctx,
            source_sha,
            "live review inputs changed concurrently during triage report generation",
        )
        prepared = _write_output(ctx, result.structured, facts)
        duration = time.monotonic() - started
        _record_call(ctx, result, duration, exit_code=0)
        call_recorded = True
        facts = build_review_facts(ctx)
        briefing_path = write_triage_package(ctx, facts, prepared.assessment, prepared.html_path)
        source_sha = _source_fingerprint(ctx)
        manifest = _base_manifest(ctx, prompt_sha, source_sha, "ready")
        manifest.update(
            {
                "briefing_path": str(briefing_path),
                "briefing_sha256": _file_sha256(briefing_path),
                "duration_s": round(duration, 2),
                "cost_usd": round(result.cost_usd, 4),
                "model": get_backend_config().model_for_role("triage_report", "standard"),
            }
        )
        if prepared.html_path is not None:
            manifest["html_path"] = str(prepared.html_path)
            manifest["html_sha256"] = _file_sha256(prepared.html_path)
        else:
            manifest["html_path"] = None
            manifest["html_error"] = prepared.html_error
        if prepared.assessment_error is not None:
            manifest["assessment_error"] = prepared.assessment_error
        _write_json(_manifest_path(ctx), manifest)
        if prepared.html_path is None:
            return ReviewPrepOutcome(
                "ready", "review briefing prepared; HTML explanation unavailable"
            )
        return ReviewPrepOutcome("ready", "review package prepared", prepared.html_path)
    except Exception as exc:
        duration = time.monotonic() - started
        if "ctx" not in locals():
            logger.exception("Triage report setup failed for %s", slug)
            _write_early_failure(project_root.resolve(), slug, exc)
            return ReviewPrepOutcome("failed", f"{type(exc).__name__}: {exc}"[:2000])
        if "prompt_sha" not in locals():
            prompt_sha = ""
        if "source_sha" not in locals():
            try:
                source_sha = _source_fingerprint(ctx)
            except (OSError, ReviewPrepError):
                source_sha = ""
        failed_result = (
            result if "result" in locals() and isinstance(result, AgentResult) else None
        )
        return _failure_outcome(
            ctx,
            prompt_sha,
            source_sha,
            exc,
            duration,
            failed_result,
            status=_failure_status(exc),
            record_call=not call_recorded,
        )


async def prepare_review_command(
    project_root: Path, slug: str, *, force: bool = False
) -> ReviewPrepOutcome:
    """Prepare a review package for a review or blocked ticket."""
    try:
        load_models_config(project_root)
        _resolve_context(project_root.resolve(), slug, require_review=True)
    except Exception as exc:
        logger.exception("Manual triage report setup failed for %s", slug)
        _write_early_failure(project_root.resolve(), slug, exc)
        return ReviewPrepOutcome("failed", f"{type(exc).__name__}: {exc}"[:2000])
    return await prepare_review(project_root, slug, force=force)


def review_briefing_command(
    project_root: Path, slug: str, *, open_diffs: bool = True
) -> ReviewBriefingOutcome:
    """Render a current review or blocked-ticket briefing without invoking an agent."""
    try:
        resolved_root = project_root.resolve()
        ctx = _resolve_context(
            resolved_root,
            slug,
            require_review=True,
            allow_report_disabled=True,
        )
        if not ctx.triage_report_enabled:
            facts = build_review_facts(ctx)
            scope = facts.get("scope", {})
            deviations = scope.get("deviations", []) if isinstance(scope, dict) else []
            package = {
                **facts,
                "assessment": {
                    "recommendation": "hold",
                    "reason": "the ticket opted out of agent-prepared review assessment",
                    "decision_blockers": ["Human review is required before approval."],
                    "scope_deviations": [
                        {
                            "path": path,
                            "classification": "Needs review",
                            "reason": "No report agent was requested for this ticket.",
                        }
                        for path in deviations
                    ],
                    "developer_summary": "Agent-prepared summary disabled by ticket configuration.",
                    "uncertainties": "Inspect the prepared diffs and developer report manually.",
                    "optional_omissions": "HTML explanation and semantic assessment.",
                    "findings": [],
                },
                "html_path": None,
            }
            failures = open_package_diffs(package) if open_diffs else []
            return ReviewBriefingOutcome(
                "ready",
                "deterministic no-agent review briefing loaded",
                render_review_briefing(package, failures),
                tuple(failures),
            )
        prompt_sha = _prompt_hash(_prompt_text())
        source_sha = _source_fingerprint(ctx)
        manifest = _read_manifest(ctx)
        fresh = _fresh_outcome(ctx, manifest, prompt_sha, source_sha)
        if fresh is None or manifest is None:
            return ReviewBriefingOutcome(
                "stale",
                "prepared review package is missing or stale; rerun the ticket report preparation",
            )
        package = load_triage_package(Path(str(manifest["briefing_path"])))
        failures = open_package_diffs(package) if open_diffs else []
        return ReviewBriefingOutcome(
            "ready",
            "prepared review briefing loaded",
            render_review_briefing(package, failures),
            tuple(failures),
        )
    except Exception as exc:  # noqa: BLE001 - CLI boundary returns a stable outcome
        return ReviewBriefingOutcome("failed", f"{type(exc).__name__}: {exc}"[:2000])
