"""Deterministic facts and rendering for precomputed ticket-triage packages."""

from __future__ import annotations

import json
import shutil
import subprocess
import sys
import tempfile
from collections.abc import Mapping
from pathlib import Path, PurePosixPath
from typing import Any, Protocol
from urllib.parse import quote

from booley.core.boundary import require_dict, require_str
from booley.harness.review_artifact import ReviewArtifactError, ReviewPackage
from booley.harness.review_explanation import StructuredExplanation

TRIAGE_PACKAGE_VERSION = 2
TRIAGE_ASSESSMENTS = frozenset({"approve", "reset", "archive", "hold"})
_GIT_STATUS_ACTIONS = {
    "A": "added",
    "M": "modified",
    "D": "deleted",
    "R": "renamed",
    "C": "copied",
    "T": "type-changed",
}


class TriagePackageError(RuntimeError):
    """A triage package is incomplete, malformed, or cannot be prepared."""


class TriageContext(Protocol):
    """Review context fields consumed by deterministic package preparation."""

    project_root: Path
    slug: str
    log_dir: Path
    runtime_dir: Path
    worktree: Path
    ticket_path: Path
    base_sha: str
    head_sha: str
    feature_branch: str


def _git_at(worktree: Path, *args: str) -> str:
    result = subprocess.run(
        ["git", "-C", str(worktree), *args],
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )
    if result.returncode != 0:
        detail = (result.stderr or result.stdout).strip()[:1000]
        raise TriagePackageError(f"git {' '.join(args)} failed: {detail}")
    return result.stdout


def _git(ctx: TriageContext, *args: str) -> str:
    return _git_at(ctx.worktree, *args)


def _read_json(path: Path, default: Any) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return default


def _category(name: str) -> str:
    prefixes = (
        (("lint_",), "Lint"),
        (("elab_",), "Elaboration"),
        (("sim_",), "Simulation"),
        (("cycle_count_",), "Simulation"),
        (("synthesis_", "synth_"), "Synthesis"),
        (("fpga_",), "FPGA"),
        (("mutation",), "Mutation"),
        (("review_",), "Review"),
    )
    for candidates, category in prefixes:
        if name.startswith(candidates):
            return category
    return "Other"


_CATEGORY_ORDER = {
    name: index
    for index, name in enumerate(
        ("Lint", "Elaboration", "Simulation", "Synthesis", "FPGA", "Mutation", "Review", "Other")
    )
}


def _criterion_status(entry: Mapping[str, Any]) -> str:
    if entry.get("met") is True:
        return "met"
    if entry.get("stale") is True:
        return "STALE"
    if entry.get("ever_failed") is True:
        return "unmet"
    return "not run"


def _criterion_outcome(entry: Mapping[str, Any]) -> str:
    if entry.get("met") is True:
        return "met"
    if entry.get("ever_failed") is True:
        return "unmet"
    return "not_run"


def _criterion_freshness(entry: Mapping[str, Any]) -> str:
    if entry.get("stale") is True:
        return "stale"
    if entry.get("met") is True or entry.get("ever_failed") is True:
        return "current"
    return "unknown"


def _short_value(value: Any) -> str:
    if isinstance(value, bool):
        return "yes" if value else "no"
    if isinstance(value, (str, int, float)):
        return str(value)
    return json.dumps(value, sort_keys=True, separators=(",", ":"))


def _criterion_metric(entry: Mapping[str, Any]) -> str:
    detail = entry.get("detail")
    if not isinstance(detail, dict):
        return "booley_state.json"
    keys = (
        "cycles",
        "baseline_cycles",
        "delta_cycles",
        "delta_pct",
        "warnings",
        "tests_passed",
        "tests_total",
        "targets_passed",
        "targets_total",
        "cells",
        "cell_count",
        "area",
        "score",
        "detected",
        "total_valid",
        "not_detected",
        "invalid",
        "mutations_detected",
        "mutations_total",
        "issues",
        "error_gist",
        "reason",
    )
    parts = [f"{key}={_short_value(detail[key])}" for key in keys if key in detail]
    return (", ".join(parts) or "persisted criterion state")[:240] + " · booley_state.json"


def _criterion_report_path(
    name: str,
    entry: Mapping[str, Any],
    *,
    worktree: Path,
    project_root: Path,
) -> str | None:
    """Resolve a trusted ticket artifact that should open from a criterion."""
    if name != "mutation_score":
        return None
    detail = entry.get("detail")
    artifacts = detail.get("artifacts") if isinstance(detail, Mapping) else None
    raw_path = artifacts.get("results") if isinstance(artifacts, Mapping) else None
    if not isinstance(raw_path, str) or not raw_path:
        return None
    candidate = Path(raw_path)
    resolved = (candidate if candidate.is_absolute() else worktree / candidate).resolve()
    try:
        resolved.relative_to(project_root.resolve())
    except ValueError:
        return None
    return str(resolved) if resolved.is_file() else None


def _criteria(
    state: Mapping[str, Any],
    *,
    worktree: Path,
    project_root: Path,
) -> list[dict[str, Any]]:
    raw = state.get("criteria")
    if not isinstance(raw, dict):
        return []
    rows = []
    for name, value in raw.items():
        if name.startswith("_") or not isinstance(value, dict):
            continue
        category = _category(name)
        rows.append(
            {
                "category": category,
                "criterion": name,
                "required": "mandatory" if value.get("mandatory", True) else "optional",
                "status": _criterion_status(value),
                "outcome": _criterion_outcome(value),
                "freshness": _criterion_freshness(value),
                "metric": _criterion_metric(value),
                "report_path": _criterion_report_path(
                    name,
                    value,
                    worktree=worktree,
                    project_root=project_root,
                ),
            }
        )
    return sorted(rows, key=lambda row: (_CATEGORY_ORDER[row["category"]], row["criterion"]))


def _recipe_comparisons(state: Mapping[str, Any]) -> list[dict[str, Any]]:
    """Extract deterministic old/new implementation-recipe evidence from state."""
    raw = state.get("criteria")
    if not isinstance(raw, Mapping):
        return []
    rows: list[dict[str, Any]] = []
    for criterion, entry in raw.items():
        name = str(criterion)
        if not name.startswith(("synthesis_ok_", "fpga_impl_ok_")) or not isinstance(
            entry, Mapping
        ):
            continue
        detail = entry.get("detail")
        comparison = detail.get("recipe_comparison") if isinstance(detail, Mapping) else None
        if not isinstance(comparison, Mapping):
            continue
        checks = detail.get("checks") if isinstance(detail.get("checks"), list) else []
        flow = comparison.get("flow") or ("fpga" if name.startswith("fpga_impl_ok_") else "synth")
        prefix = "fpga_impl_ok_" if flow == "fpga" else "synthesis_ok_"
        rows.append(
            {
                "criterion": name,
                "flow": flow,
                "target": comparison.get("target") or name.removeprefix(prefix),
                "changed": comparison.get("changed") is True,
                "baseline_ref": comparison.get("baseline_ref"),
                "baseline_fingerprint": comparison.get("baseline_fingerprint"),
                "current_fingerprint": comparison.get("current_fingerprint"),
                "changes": list(comparison.get("changes") or []),
                "qor_checks": [
                    dict(check)
                    for check in checks
                    if isinstance(check, Mapping)
                    and not str(check.get("param", "")).startswith("_")
                ],
            }
        )
    return sorted(rows, key=lambda row: row["criterion"])


def _cycle_comparisons(
    state: Mapping[str, Any], changed_files: list[dict[str, Any]]
) -> list[dict[str, Any]]:
    """Extract typed per-test Cycle Count comparison evidence from state."""
    raw = state.get("criteria")
    if not isinstance(raw, Mapping):
        return []
    rows: list[dict[str, Any]] = []
    for criterion, entry in raw.items():
        name = str(criterion)
        if not name.startswith("cycle_count_") or not isinstance(entry, Mapping):
            continue
        detail = entry.get("detail")
        comparison = detail.get("cycle_comparison") if isinstance(detail, Mapping) else None
        if isinstance(comparison, Mapping):
            row = {"criterion": name, **dict(comparison)}
            row["known_input_changes"] = _link_workload_changes(
                row.get("known_input_changes"), changed_files
            )
            rows.append(row)
    return sorted(rows, key=lambda row: row["criterion"])


def _link_workload_changes(
    workload: Any,
    changed_files: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Attach materialized diff endpoints to known changed workload paths."""
    by_path: dict[str, dict[str, Any]] = {}
    for change in changed_files:
        for key in ("path", "old_path"):
            path = change.get(key)
            if isinstance(path, str) and path:
                by_path[path] = change
    linked: list[dict[str, Any]] = []
    for item in workload if isinstance(workload, list) else []:
        if not isinstance(item, Mapping):
            continue
        row = dict(item)
        materialized = by_path.get(str(row.get("path", "")))
        if materialized is not None:
            row["diff_left"] = materialized.get("diff_left")
            row["diff_right"] = materialized.get("diff_right")
        linked.append(row)
    return linked


def _repository_commits(
    worktree: Path, base_sha: str, head_sha: str, repository: str
) -> list[dict[str, str]]:
    revision = f"{base_sha}..{head_sha}"
    output = _git_at(worktree, "log", "--reverse", "--format=%H%x00%h%x00%s", revision, "--")
    rows = []
    for line in output.splitlines():
        parts = line.split("\0", 2)
        if len(parts) == 3:
            rows.append(
                {
                    "sha": parts[0],
                    "abbrev": parts[1],
                    "subject": parts[2],
                    "repository": repository,
                }
            )
    return rows


def _commits(ctx: TriageContext) -> list[dict[str, str]]:
    rows = _repository_commits(ctx.worktree, ctx.base_sha, ctx.head_sha, "rtl")
    project = getattr(ctx, "project_repository", None)
    if project is not None:
        rows.extend(
            _repository_commits(
                project.worktree,
                project.base_sha,
                project.head_sha,
                "project",
            )
        )
    return rows


def _safe_repo_path(value: str) -> str:
    path = PurePosixPath(value)
    if path.is_absolute() or ".." in path.parts or value in {"", "."}:
        raise TriagePackageError(f"unsafe changed path: {value!r}")
    return path.as_posix()


def _repository_changed_files(
    worktree: Path,
    base_sha: str,
    head_sha: str,
    *,
    repository: str,
    path_prefix: str = "",
) -> list[dict[str, Any]]:
    revision = f"{base_sha}..{head_sha}"
    output = _git_at(
        worktree,
        "diff",
        "--find-renames",
        "--find-copies",
        "--name-status",
        "-z",
        revision,
        "--",
    )
    tokens = output.split("\0")
    if tokens and not tokens[-1]:
        tokens.pop()
    rows: list[dict[str, Any]] = []
    index = 0
    while index < len(tokens):
        status = tokens[index]
        index += 1
        if status.startswith(("R", "C")):
            if index + 1 >= len(tokens):
                raise TriagePackageError("truncated rename/copy record from git diff")
            old_path = _safe_repo_path(tokens[index])
            path = _safe_repo_path(tokens[index + 1])
            index += 2
        else:
            if index >= len(tokens):
                raise TriagePackageError("truncated changed-file record from git diff")
            old_path = None
            path = _safe_repo_path(tokens[index])
            index += 1
        display_path = f"{path_prefix}/{path}" if path_prefix else path
        display_old = f"{path_prefix}/{old_path}" if path_prefix and old_path else old_path
        action = _GIT_STATUS_ACTIONS.get(status[:1])
        if action is None:
            raise TriagePackageError(f"unsupported git change action: {status!r}")
        rows.append(
            {
                "status": status,
                "action": action,
                "similarity": int(status[1:]) if status[1:].isdigit() else None,
                "path": display_path,
                "old_path": display_old,
                "repository": repository,
                "_worktree": worktree,
                "_base_sha": base_sha,
                "_head_sha": head_sha,
                "_local_path": path,
                "_old_local_path": old_path,
            }
        )
    return rows


def _changed_files(ctx: TriageContext) -> list[dict[str, Any]]:
    rows = _repository_changed_files(ctx.worktree, ctx.base_sha, ctx.head_sha, repository="rtl")
    project = getattr(ctx, "project_repository", None)
    if project is not None:
        rows.extend(
            _repository_changed_files(
                project.worktree,
                project.base_sha,
                project.head_sha,
                repository="project",
                path_prefix=".booley_project",
            )
        )
    return rows


def _revision_content(repository: Any, revision: str, path: str) -> bytes:
    """Read one revision path, accepting a context for API compatibility."""
    worktree = Path(getattr(repository, "worktree", repository))
    tree = subprocess.run(
        ["git", "-C", str(worktree), "ls-tree", "-z", revision, "--", path],
        capture_output=True,
        timeout=60,
        check=False,
    )
    if tree.returncode != 0 or not tree.stdout:
        return b""
    header = tree.stdout.split(b"\t", 1)[0].decode("ascii", errors="replace")
    mode, object_type, object_id = header.split(" ", 2)
    if mode == "160000" or object_type == "commit":
        return f"Submodule commit {object_id}\n".encode()
    result = subprocess.run(
        ["git", "-C", str(worktree), "show", f"{revision}:{path}"],
        capture_output=True,
        timeout=60,
        check=False,
    )
    if result.returncode != 0:
        detail = result.stderr.decode(errors="replace").strip()[:1000]
        raise TriagePackageError(f"could not extract {revision}:{path}: {detail}")
    return result.stdout


def _repository_root(repository: Any) -> Path:
    worktree = getattr(repository, "worktree", None)
    if worktree is not None:
        return Path(worktree)
    if isinstance(repository, str | Path):
        return Path(repository)
    return Path()


def _content_kind(repository: Any, revision: str, path: str) -> str | None:
    worktree = _repository_root(repository)
    result = subprocess.run(
        ["git", "-C", str(worktree), "ls-tree", "-z", revision, "--", path],
        capture_output=True,
        timeout=60,
        check=False,
    )
    if result.returncode != 0 or not result.stdout:
        return None
    header = result.stdout.split(b"\t", 1)[0].decode("ascii", errors="replace")
    mode, object_type, _object_id = header.split(" ", 2)
    if mode == "120000":
        return "symlink"
    if mode == "160000" or object_type == "commit":
        return "submodule"
    return "regular"


def _write_diff_pair(
    ctx: TriageContext, root: Path, index: int, change: dict[str, Any]
) -> dict[str, Any]:
    number = f"{index:03d}"
    old_path = change.get("old_path") or change["path"]
    new_path = change["path"]
    left = root / number / "base" / old_path
    right = root / number / "head" / new_path
    left.parent.mkdir(parents=True, exist_ok=True)
    right.parent.mkdir(parents=True, exist_ok=True)
    repository = change.get("_worktree", ctx)
    repository_root = _repository_root(repository)
    local_path = change.get("_local_path", new_path)
    local_old = (
        change.get("_old_local_path") or local_path if "_old_local_path" in change else old_path
    )
    base_sha = change.get("_base_sha", ctx.base_sha)
    head_sha = change.get("_head_sha", ctx.head_sha)
    left_content = _revision_content(repository, base_sha, local_old)
    right_content = _revision_content(repository, head_sha, local_path)
    left.write_bytes(left_content)
    right.write_bytes(right_content)
    old_kind = _content_kind(repository, base_sha, local_old)
    new_kind = _content_kind(repository, head_sha, local_path)
    content_kind = new_kind or old_kind or "regular"
    presentation = "binary" if b"\0" in left_content or b"\0" in right_content else "text"
    public = {key: value for key, value in change.items() if not key.startswith("_")}
    workspace_path = None
    action = public.get("action") or _GIT_STATUS_ACTIONS.get(
        str(public.get("status", ""))[:1], "modified"
    )
    public["action"] = action
    public.setdefault("similarity", None)
    if action != "deleted":
        candidate = repository_root / local_path
        if candidate.exists() or candidate.is_symlink():
            workspace_path = str(candidate.absolute())
    return {
        **public,
        "content_kind": content_kind,
        "presentation": presentation,
        "diff_left": str(left),
        "diff_right": str(right),
        "old_endpoint": {
            "repository_path": local_old,
            "display_path": old_path,
            "revision": base_sha,
            "diff_path": str(left),
            "workspace_path": None,
        },
        "new_endpoint": {
            "repository_path": local_path,
            "display_path": new_path,
            "revision": head_sha,
            "diff_path": str(right),
            "workspace_path": workspace_path,
        },
    }


def _materialize_diffs(ctx: TriageContext, changes: list[dict[str, Any]]) -> list[dict[str, Any]]:
    ctx.runtime_dir.mkdir(parents=True, exist_ok=True)
    final = ctx.runtime_dir / "diffs"
    temporary = Path(tempfile.mkdtemp(prefix="diffs-", dir=ctx.runtime_dir))
    try:
        rows = [
            _write_diff_pair(ctx, temporary, index, row) for index, row in enumerate(changes, 1)
        ]
        if final.exists():
            shutil.rmtree(final)
        temporary.replace(final)
    except BaseException:
        shutil.rmtree(temporary, ignore_errors=True)
        raise
    prefix = str(final)
    for row in rows:
        row["diff_left"] = row["diff_left"].replace(str(temporary), prefix, 1)
        row["diff_right"] = row["diff_right"].replace(str(temporary), prefix, 1)
        row["old_endpoint"]["diff_path"] = row["old_endpoint"]["diff_path"].replace(
            str(temporary), prefix, 1
        )
        row["new_endpoint"]["diff_path"] = row["new_endpoint"]["diff_path"].replace(
            str(temporary), prefix, 1
        )
    return rows


def _usage_summary(ctx: TriageContext) -> str:
    result = subprocess.run(
        [sys.executable, "-m", "booley.ticket_board", "usage", "--slug", ctx.slug, "--summary"],
        cwd=ctx.project_root,
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )
    return result.stdout.strip() if result.returncode == 0 else "unavailable"


def _scope(ctx: TriageContext) -> dict[str, Any]:
    path = ctx.log_dir / ".runtime" / "scope_deviations.json"
    value = _read_json(path, {})
    return value if isinstance(value, dict) else {}


def _health(
    ctx: TriageContext, state: Mapping[str, Any], scope: Mapping[str, Any]
) -> dict[str, Any]:
    timeline = state.get("timeline") if isinstance(state.get("timeline"), list) else []
    exit_2 = [
        str(item.get("tool") or item.get("flow") or item.get("mcp_tool") or "unknown")
        for item in timeline
        if isinstance(item, dict) and item.get("exit_code") == 2
    ]
    crash_dir = ctx.log_dir / ".runtime" / "developer"
    crashes = sorted(str(path) for path in crash_dir.glob("*.crash.json"))
    missing = []
    for name, path in (
        ("REPORT.md", ctx.log_dir / "REPORT.md"),
        ("booley_state.json", ctx.log_dir / ".runtime" / "booley_state.json"),
    ):
        if not path.is_file():
            missing.append(name)
    dirty = _git(ctx, "status", "--short").splitlines()
    project = getattr(ctx, "project_repository", None)
    if project is not None:
        dirty.extend(
            f"{line[:3]}.booley_project/{line[3:]}"
            for line in _git_at(project.worktree, "status", "--short").splitlines()
        )
    return {
        "dirty_worktree": dirty,
        "exit_2_tools": exit_2,
        "developer_crashes": crashes,
        "missing_evidence": missing,
        "harness_paths": scope.get("harness_paths", []),
        "scope_undecidable": scope.get("decidable") is False,
        "unverified_transitions": _unverified_transitions(state),
    }


def _unverified_transitions(state: Mapping[str, Any]) -> list[str]:
    """Return passing fail->pass criteria whose failing leg was not observed."""
    criteria = state.get("criteria")
    if not isinstance(criteria, Mapping):
        return []
    names = []
    for name, value in criteria.items():
        if not isinstance(name, str) or name.startswith("_") or not isinstance(value, Mapping):
            continue
        params = value.get("params")
        if (
            value.get("met") is True
            and isinstance(params, Mapping)
            and params.get("from_state") == "fail"
            and value.get("ever_failed") is not True
        ):
            names.append(name)
    return sorted(names)


def build_review_facts(ctx: TriageContext) -> dict[str, Any]:
    """Collect and materialize exhaustive mechanical review facts once."""
    state_path = ctx.log_dir / ".runtime" / "booley_state.json"
    state = _read_json(state_path, {})
    if not isinstance(state, dict):
        raise TriagePackageError(f"invalid state file: {state_path}")
    scope = _scope(ctx)
    changes = _materialize_diffs(ctx, _changed_files(ctx))
    repositories = [
        {
            "name": "rtl",
            "base_sha": ctx.base_sha,
            "head_sha": ctx.head_sha,
            "worktree": str(ctx.worktree),
        }
    ]
    project = getattr(ctx, "project_repository", None)
    if project is not None:
        repositories.append(
            {
                "name": "project",
                "base_sha": project.base_sha,
                "head_sha": project.head_sha,
                "worktree": str(project.worktree),
            }
        )
    from booley.dev_support.review_dispositions import collect_review_dispositions

    return {
        "version": TRIAGE_PACKAGE_VERSION,
        "kind": "review",
        "slug": ctx.slug,
        "feature_branch": ctx.feature_branch,
        "base_sha": ctx.base_sha,
        "head_sha": ctx.head_sha,
        "worktree": str(ctx.worktree),
        "repositories": repositories,
        "criteria": _criteria(
            state,
            worktree=ctx.worktree,
            project_root=ctx.project_root,
        ),
        "review_dispositions": collect_review_dispositions(state.get("criteria", {})),
        "recipe_comparisons": _recipe_comparisons(state),
        "cycle_comparisons": _cycle_comparisons(state, changes),
        "scope": scope,
        "commits": _commits(ctx),
        "changed_files": changes,
        "developer_report_path": str(ctx.log_dir / "REPORT.md"),
        "run_economics": _usage_summary(ctx),
        "health": _health(ctx, state, scope),
    }


def validate_assessment(value: Any, facts: Mapping[str, Any]) -> dict[str, Any]:
    """Validate semantic agent judgments against deterministic package facts."""
    assessment = require_dict(value, field="triage assessment")
    recommendation = require_str(assessment, "recommendation")
    if recommendation not in TRIAGE_ASSESSMENTS:
        raise TriagePackageError(f"invalid triage recommendation: {recommendation}")
    for key in ("reason", "developer_summary", "uncertainties"):
        require_str(assessment, key)
    optional_omissions = assessment.get("optional_omissions")
    if not isinstance(optional_omissions, str):
        raise TriagePackageError("triage assessment optional_omissions must be a string")
    assessment["optional_omissions"] = optional_omissions or "none"
    for key in ("decision_blockers", "findings"):
        items = assessment.get(key)
        if not isinstance(items, list) or any(not isinstance(item, str) for item in items):
            raise TriagePackageError(f"triage assessment {key} must be a string list")
    scope = facts.get("scope")
    deviations = scope.get("deviations", []) if isinstance(scope, Mapping) else []
    if not isinstance(deviations, list) or any(not isinstance(item, str) for item in deviations):
        raise TriagePackageError("scope deviations must be a string list")
    scope_rows = assessment.get("scope_deviations")
    if not isinstance(scope_rows, list):
        raise TriagePackageError("triage assessment scope_deviations must be a list")
    assessment["scope_deviations"] = _normalize_scope_assessments(
        assessment,
        scope_rows,
        deviations,
    )
    return assessment


def _normalize_scope_assessments(
    assessment: dict[str, Any],
    scope_rows: list[Any],
    deviations: list[str],
) -> list[dict[str, str]]:
    """Align semantic rows to deterministic deviations without losing the package."""
    by_path: dict[str, list[dict[str, str]]] = {}
    for row_value in scope_rows:
        row = require_dict(row_value, field="scope deviation assessment")
        path = require_str(row, "path")
        classification = require_str(row, "classification")
        reason = require_str(row, "reason")
        if classification not in {"Justified", "Unjustified"}:
            raise TriagePackageError(f"invalid scope classification for {path}")
        by_path.setdefault(path, []).append(
            {"path": path, "classification": classification, "reason": reason}
        )

    unresolved = [path for path in deviations if len(by_path.get(path, [])) != 1]
    unknown = sorted(set(by_path) - set(deviations))
    if unresolved:
        assessment["recommendation"] = "hold"
        assessment["decision_blockers"].append(
            "Human scope classification required for: " + ", ".join(unresolved)
        )
        assessment["findings"].append(
            "Report agent omitted or duplicated scope assessments for: " + ", ".join(unresolved)
        )
    if unknown:
        assessment["findings"].append(
            "Report agent assessed paths not recorded as scope deviations: " + ", ".join(unknown)
        )
    return [_resolved_scope_row(path, by_path.get(path, [])) for path in deviations]


def _resolved_scope_row(path: str, rows: list[dict[str, str]]) -> dict[str, str]:
    """Use one agent judgment or a conservative deterministic placeholder."""
    if len(rows) == 1:
        return rows[0]
    return {
        "path": path,
        "classification": "Needs review",
        "reason": "The report agent did not return exactly one assessment for this deviation.",
    }


def write_triage_package(
    ctx: TriageContext,
    facts: dict[str, Any],
    assessment: dict[str, Any],
    html_path: Path | None,
    explanation: StructuredExplanation | None = None,
) -> Path:
    """Persist one machine-readable package consumed by interactive triage."""
    package_value = {
        **facts,
        "assessment": assessment,
        "html_path": str(html_path) if html_path is not None else None,
        "explanation": explanation.to_dict() if explanation is not None else None,
    }
    try:
        package = ReviewPackage.parse(package_value)
    except ReviewArtifactError as exc:
        raise TriagePackageError(f"invalid review package: {exc}") from exc
    path = ctx.runtime_dir / "briefing.json"
    path.write_text(
        json.dumps(package.to_dict(), indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    return path


def load_triage_package(path: Path) -> ReviewPackage:
    """Strictly parse an immutable version-2 review package."""
    value = _read_json(path, None)
    try:
        return ReviewPackage.parse(value)
    except ReviewArtifactError as exc:
        raise TriagePackageError(f"invalid or unsupported triage package {path}: {exc}") from exc


def open_package_diffs(package: Mapping[str, Any]) -> list[str]:
    """Open every prepared diff and return paths whose launch failed."""
    from booley.config.editor import resolve_editor

    rows = package.get("changed_files", [])
    editor = resolve_editor()
    if editor is None or editor.diff is None:
        return [str(row.get("path", "unknown")) for row in rows]
    failures = []
    for row in rows:
        left, right = row.get("diff_left"), row.get("diff_right")
        if not isinstance(left, str) or not isinstance(right, str):
            failures.append(str(row.get("path", "unknown")))
            continue
        try:
            argv = [part.replace("{left}", left).replace("{right}", right) for part in editor.diff]
            result = subprocess.run(argv, timeout=15, check=False)
        except (FileNotFoundError, subprocess.TimeoutExpired):
            failures.append(str(row.get("path", "unknown")))
            continue
        if result.returncode != 0:
            failures.append(str(row.get("path", "unknown")))
    return failures


def _markdown_link(label: str, path: str) -> str:
    return f"[{_markdown_text(label)}]({quote(path, safe='/:')})"


def _markdown_text(value: Any) -> str:
    """Render untrusted text inertly in Markdown and terminal output."""
    text = "".join(
        f"\\x{ord(char):02x}" if ord(char) < 32 or ord(char) == 127 else char
        for char in str(value)
    )
    for marker in ("\\", "`", "*", "[", "]", "<", "|"):
        text = text.replace(marker, f"\\{marker}")
    return text


def _render_criteria(lines: list[str], package: Mapping[str, Any]) -> None:
    lines.extend(
        [
            "",
            "#### Criteria",
            "",
            "| Category | Criterion | Required? | Status | Metric / evidence |",
            "|----------|-----------|-----------|--------|-------------------|",
        ]
    )
    for row in package.get("criteria", []):
        criterion = f"`{_markdown_text(row['criterion'])}`"
        if isinstance(row.get("report_path"), str):
            criterion = _markdown_link(row["criterion"], row["report_path"])
        lines.append(
            f"| {_markdown_text(row['category'])} | {criterion} | "
            f"{_markdown_text(row['required'])} | {_markdown_text(row['status'])} | "
            f"{_markdown_text(row['metric'])} |"
        )


def _render_review_dispositions(lines: list[str], package: Mapping[str, Any]) -> None:
    rows = package.get("review_dispositions", [])
    if not rows:
        return
    lines.extend(
        [
            "",
            "#### Review findings and dispositions",
            "",
            "| Criterion | Severity | Location | Disposition | Finding / justification |",
            "|-----------|----------|----------|-------------|-------------------------|",
        ]
    )
    for row in rows:
        location = f"{row.get('file', '')}:{row.get('line', 0)}"
        explanation = row.get("summary", "")
        if row.get("disposition") == "waived":
            explanation = f"{explanation} — Waiver: {row.get('justification', '')}"
        lines.append(
            f"| `{_markdown_text(row.get('criterion', ''))}` | "
            f"{_markdown_text(row.get('severity', ''))} | "
            f"`{_markdown_text(location)}` | "
            f"{_markdown_text(row.get('disposition', ''))} | "
            f"{_markdown_text(explanation)} |"
        )


def _render_recipe_comparisons(lines: list[str], package: Mapping[str, Any]) -> None:
    """Render Target recipe changes and the QoR checks they contextualize."""
    rows = package.get("recipe_comparisons", [])
    if not rows:
        return
    lines.extend(["", "#### Implementation Target recipes", ""])
    for row in rows:
        relation = "changed" if row.get("changed") else "unchanged"
        baseline = str(row.get("baseline_fingerprint") or "unavailable")[:12]
        current = str(row.get("current_fingerprint") or "unavailable")[:12]
        lines.append(
            f"- `{row.get('flow', 'implementation')}:{row['target']}` — **{relation}** "
            f"(baseline `{baseline}`, current `{current}`)"
        )
        for change in row.get("changes", []):
            before = _short_value(change.get("before"))
            after = _short_value(change.get("after"))
            lines.append(f"  - `{change.get('path')}`: `{before}` → `{after}`")
        for check in row.get("qor_checks", []):
            verdict = "PASS" if check.get("pass") else "FAIL"
            if check.get("skipped"):
                summary = str(check.get("reason", "not evaluated"))
            else:
                summary = (
                    f"{check.get('baseline')} → {check.get('current')}; "
                    f"measured {check.get('pct')}%, limit {check.get('threshold')}%"
                )
            lines.append(f"  - `{check.get('param')}` — **{verdict}**: {summary}")


def _render_cycle_comparisons(lines: list[str], package: Mapping[str, Any]) -> None:
    """Render observed per-test Cycle Count changes and workload disclosure."""
    rows = package.get("cycle_comparisons", [])
    if not rows:
        return
    lines.extend(["", "#### Cycle Count comparisons", ""])
    for row in rows:
        baseline = row.get("baseline_cycles")
        current = row.get("cycles")
        delta_cycles = row.get("delta_cycles")
        delta_pct = row.get("delta_pct")
        delta_text = "unavailable"
        if delta_cycles is not None:
            delta_text = f"{delta_cycles:+} cycles"
            if delta_pct is not None:
                delta_text += f" ({delta_pct:+.2f}%)"
        lines.append(
            f"- `{_markdown_text(row.get('target', ''))}` / "
            f"`{_markdown_text(row.get('test', ''))}` — observed Cycle Count change: "
            f"`{baseline}` → `{current}`; {delta_text}"
        )
        if row.get("workload_changed"):
            lines.append(
                "  - **WARNING:** known workload inputs changed; this comparison combines "
                "RTL and workload effects and does not establish causality."
            )
        else:
            lines.append("  - Known declared workload inputs: unchanged.")
        for change in row.get("known_input_changes", []):
            path = _markdown_text(change.get("path", ""))
            diff_path = change.get("diff_right") or change.get("diff_left")
            path_text = (
                _markdown_link(str(change.get("path", "")), str(diff_path))
                if isinstance(diff_path, str) and diff_path
                else f"`{path}`"
            )
            lines.append(
                f"  - {path_text} "
                f"({_markdown_text(change.get('role', 'workload'))}): "
                f"{_markdown_text(change.get('status', 'changed'))}"
            )
        for check in row.get("checks", []):
            verdict = "PASS" if check.get("pass") else "FAIL"
            summary = check.get("reason")
            if not summary:
                summary = f"threshold {check.get('threshold')} {check.get('unit', '')}"
            lines.append(
                f"  - `{_markdown_text(check.get('param', ''))}` — "
                f"**{verdict}**: {_markdown_text(summary)}"
            )
        limitation = row.get("provenance_limitation")
        if limitation:
            lines.append(f"  - Provenance boundary: {_markdown_text(limitation)}")


def _render_scope(lines: list[str], package: Mapping[str, Any]) -> None:
    lines.extend(["", "#### Scope deviations", ""])
    rows = package["assessment"].get("scope_deviations", [])
    scope = package.get("scope", {})
    undecidable = isinstance(scope, Mapping) and scope.get("decidable") is False
    if undecidable:
        lines.append(
            "- **UNRESOLVED** — the scope calculation was undecidable; do not infer clean scope."
        )
    elif not rows:
        lines.append("- none")
    for row in rows:
        lines.append(
            f"- `{_markdown_text(row['path'])}` — **{_markdown_text(row['classification'])}**: "
            f"{_markdown_text(row['reason'])}"
        )


def _render_commits(lines: list[str], package: Mapping[str, Any]) -> None:
    lines.extend(["", "#### Commit history", ""])
    commits = package.get("commits", [])
    lines.extend(
        [
            f"- `{_markdown_text(row['abbrev'])}` — {_markdown_text(row['subject'])}"
            for row in commits
        ]
        or ["- none — no feature-branch commits"]
    )


def _change_description(row: Mapping[str, Any], opened: bool) -> str:
    status = str(row.get("status", ""))
    action = {"A": "added", "D": "deleted", "M": "modified"}.get(status[:1], "changed")
    if status.startswith("R"):
        action = f"renamed from {_markdown_text(row.get('old_path'))}"
    return f"{action}; diff {'opened' if opened else 'unavailable'}"


def _render_changes(lines: list[str], package: Mapping[str, Any], failures: set[str]) -> None:
    lines.extend(["", "#### Changed files", ""])
    for row in package.get("changed_files", []):
        path = str(row["path"])
        worktree_path = Path(str(package.get("worktree", ""))) / path
        link_path = worktree_path.absolute() if worktree_path.exists() else row["diff_left"]
        lines.append(
            f"- {_markdown_link(path, str(link_path))} — "
            f"{_change_description(row, path not in failures)}"
        )


def _render_explanation_highlights(lines: list[str], package: Mapping[str, Any]) -> None:
    explanation = package.get("explanation")
    if not isinstance(explanation, Mapping):
        return
    lines.extend(["", "#### Explanation highlights", ""])
    for section in explanation.get("background", []):
        lines.append(
            f"- **{_markdown_text(section['title'])}:** {_markdown_text(section['body'])}"
        )
    for reference in explanation.get("code_references", []):
        lines.append(
            f"- `{_markdown_text(reference['path'])}` — {_markdown_text(reference['summary'])}"
        )


def _render_reports(lines: list[str], package: Mapping[str, Any]) -> None:
    assessment = package["assessment"]
    report = str(package["developer_report_path"])
    lines.extend(
        [
            "",
            "#### Reports",
            "",
            f"- {_markdown_link('Developer Agent report (REPORT.md)', report)}",
        ]
    )
    html_path = package.get("html_path")
    if isinstance(html_path, str) and html_path:
        lines.append(
            f"- {_markdown_link('Polished HTML report', html_path)} — "
            "open, then select **Show Preview**"
        )
    else:
        lines.append("- Polished HTML report: unavailable")
    lines.extend(
        [
            "",
            f"- **Developer summary:** {_markdown_text(assessment['developer_summary'])}",
            f"- **Uncertainties:** {_markdown_text(assessment['uncertainties'])}",
            f"- **Optional omissions:** {_markdown_text(assessment['optional_omissions'])}",
        ]
    )


def _render_decision(lines: list[str], package: Mapping[str, Any]) -> None:
    assessment = package["assessment"]
    blockers = list(assessment.get("decision_blockers", []))
    health = package.get("health", {})
    if health.get("scope_undecidable") is True:
        blockers.append("Scope calculation was undecidable.")
    recommendation = (
        "hold" if health.get("scope_undecidable") is True else assessment["recommendation"]
    )
    lines.extend(
        [
            "",
            "#### Decision summary",
            "",
            f"**Recommendation:** {_markdown_text(recommendation)} — "
            f"{_markdown_text(assessment['reason'])}",
            f"**Decision blockers:** {'none' if not blockers else ''}",
        ]
    )
    lines.extend(f"{index}. {_markdown_text(item)}" for index, item in enumerate(blockers, 1))


def _health_findings(package: Mapping[str, Any], diff_failures: list[str]) -> list[str]:
    health = package.get("health", {})
    findings = list(package["assessment"].get("findings", []))
    for label, key in (
        ("Dirty worktree", "dirty_worktree"),
        ("Flow/Specialist exit 2", "exit_2_tools"),
        ("Developer crashes", "developer_crashes"),
        ("Missing evidence", "missing_evidence"),
        ("Harness-path contamination", "harness_paths"),
    ):
        values = health.get(key, [])
        if values:
            findings.append(f"{label}: {', '.join(map(str, values))}")
    transitions = health.get("unverified_transitions", [])
    if transitions:
        findings.append(
            "UNVERIFIED TRANSITION: "
            + ", ".join(map(str, transitions))
            + " declared 'fail -> pass' but no failing run was recorded."
        )
    if diff_failures:
        findings.append(f"Diff viewer unavailable for: {', '.join(diff_failures)}")
    if health.get("scope_undecidable") is True:
        findings.append("Scope calculation was undecidable; scope cleanliness is unknown.")
    return findings


def _render_findings(
    lines: list[str], package: Mapping[str, Any], diff_failures: list[str]
) -> None:
    lines.extend(["", "#### Findings", ""])
    findings = _health_findings(package, diff_failures)
    lines.extend(
        [f"- {_markdown_text(item)}" for item in findings] or ["- Health checks: all passed."]
    )


def _render_economics(lines: list[str], package: Mapping[str, Any]) -> None:
    lines.extend(
        [
            "",
            "#### Run economics",
            "",
            f"- {_markdown_text(package['run_economics'])}",
        ]
    )


def render_review_briefing(package: Mapping[str, Any], diff_failures: list[str]) -> str:
    """Render the fixed interactive review template from a validated package."""
    lines = [f"### {_markdown_text(package['slug'])}"]
    _render_reports(lines, package)
    _render_decision(lines, package)
    _render_findings(lines, package, diff_failures)
    _render_explanation_highlights(lines, package)
    _render_scope(lines, package)
    _render_changes(lines, package, set(diff_failures))
    _render_criteria(lines, package)
    _render_review_dispositions(lines, package)
    _render_cycle_comparisons(lines, package)
    _render_recipe_comparisons(lines, package)
    _render_commits(lines, package)
    _render_economics(lines, package)
    lines.extend(["", "Choose: **approve** / **fix here** / **reset** / **archive** / **skip**."])
    return "\n".join(lines)
