"""ReviewerSpecialist — Specialist for single-focus code review.

Runs LLM-powered code review on RTL or testbench files, reports issues by
severity (CRITICAL, MAJOR, MINOR).  Each invocation covers exactly ONE focus
category. An idempotency guard prevents re-running the same review focus while
its persisted source fingerprint remains current.

Exit codes: 0 = gate passed, 1 = gate failed, 2 = Specialist error.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import logging
import os
import re
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, ClassVar

from booley.core.boundary import as_dict, as_str_list
from booley.core.models import AgentCallParams
from booley.dev_support.development_state import compute_source_fingerprint
from booley.dev_support.workspace_isolation import (
    filter_state_file_for_category,
)
from booley.mcp.base import (
    EXIT_ERROR,
    EXIT_FAILURE,
    EXIT_SUCCESS,
    McpToolResult,
    read_source_dirs_from_toml,
)
from booley.runtime.paths import refs_dir
from booley.targets.flow_names import config_section

from .specialist import Specialist

logger = logging.getLogger(__name__)


# Valid review focus categories for RTL mode.
# NOTE: ``spec`` was removed when spec_arbiter took over spec-compliance
# arbitration (ADR-0014), then re-introduced (ADR 0038) after the arbiter
# itself was pruned — without it nothing checked RTL against the spec.
RTL_FOCUS_CATEGORIES = frozenset(
    {
        "spec",
        "bugs",
        "protocol",
        "security",
        "optimization",
        "code_style",
    }
)

# Valid review focus categories for TB mode (spec stays RTL-only — ADR 0038).
# ``quality`` is TB-only: the RTL side splits the same ground into ``bugs``
# (defects) and ``code_style`` (readability), so the word means one thing here.
TB_FOCUS_CATEGORIES = frozenset({"quality"})

# Spec focus category name
SPEC_FOCUS = "spec"

# Severity levels (ordered high-to-low)
SEVERITY_CRITICAL = "CRITICAL"
SEVERITY_MAJOR = "MAJOR"
SEVERITY_MINOR = "MINOR"
ALL_SEVERITIES = frozenset({SEVERITY_CRITICAL, SEVERITY_MAJOR, SEVERITY_MINOR})
_SEVERITY_TAG = {SEVERITY_CRITICAL: "C", SEVERITY_MAJOR: "M", SEVERITY_MINOR: "m"}
_TB_DUMP_CALL_RE = re.compile(r"\$(?:dumpfile|dumpvars)\b")
_DIFF_HUNK_RE = re.compile(r"^@@ -\d+(?:,\d+)? \+(\d+)(?:,(\d+))? @@")
_MAX_DIFF_PROMPT_CHARS = 60_000

# Confidence levels
CONFIDENCE_HIGH = "HIGH"
CONFIDENCE_MEDIUM = "MEDIUM"
CONFIDENCE_LOW = "LOW"
ALL_CONFIDENCES = frozenset({CONFIDENCE_HIGH, CONFIDENCE_MEDIUM, CONFIDENCE_LOW})

# Verify-pass per-finding status enum (case-insensitive on input,
# canonicalized to upper case here).
VERIFY_STATUS_FIXED = "FIXED"
VERIFY_STATUS_WAIVED = "WAIVED"
VERIFY_STATUS_STILL_PRESENT = "STILL_PRESENT"
ALL_VERIFY_STATUSES = frozenset(
    {VERIFY_STATUS_FIXED, VERIFY_STATUS_WAIVED, VERIFY_STATUS_STILL_PRESENT}
)
REVIEW_DETAIL_VERSION = 2

# RTL source prefixes — derived from the authored .core filesets.
_RTL_PREFIXES_DEFAULT = ("rtl/", "rtl\\", "fw/", "fw\\")
# TB source prefixes — directories end in a separator; flat files do not.
_TB_PREFIXES_DEFAULT = ("tb/", "tb\\")


def _get_prefixes(
    work_dir: Path | None = None,
) -> tuple[tuple[str, ...], tuple[str, ...]]:
    """Resolve file-aware RTL and TB prefixes, with cross-platform variants.

    Tries booley.toml in *work_dir* first, then shared_infra, then defaults.
    Returns (rtl_prefixes, tb_prefixes) with both ``/`` and ``\\`` variants.
    """
    parsed = read_source_dirs_from_toml(work_dir) if work_dir else None
    if parsed is None:
        try:
            from booley.runtime.shared_infra import get_rtl_prefixes, get_tb_prefixes

            return get_rtl_prefixes(), get_tb_prefixes()
        except Exception:  # noqa: BLE001 — legacy CWD path unavailable; fall back to default prefixes
            return _RTL_PREFIXES_DEFAULT, _TB_PREFIXES_DEFAULT

    from booley.runtime.shared_infra import source_dir_prefixes

    rtl_names, tb_names = parsed
    rtl_list = list(rtl_names)
    if "fw" not in {name.rstrip("/\\") for name in rtl_list}:
        rtl_list.append("fw")
    return (
        source_dir_prefixes(rtl_list, work_dir),
        source_dir_prefixes(tb_names, work_dir),
    )


def _get_rtl_prefixes(work_dir: Path | None = None) -> tuple[str, ...]:
    """RTL directory prefixes (convenience wrapper around _get_prefixes)."""
    return _get_prefixes(work_dir)[0]


def _get_tb_prefixes(work_dir: Path | None = None) -> tuple[str, ...]:
    """TB directory prefixes (convenience wrapper around _get_prefixes)."""
    return _get_prefixes(work_dir)[1]


# HDL source suffixes whose presence in a review scope but absence from any
# ``.core`` fileset is worth surfacing (ADR 0026 follow-through).
_SOURCE_SUFFIXES = (".v", ".sv", ".svh", ".vh")


def _warn_unregistered_sources(
    scope_paths: list[str],
    work_dir: Path | None,
) -> None:
    """Warn for review-scope source files absent from the project's ``.core``.

    Under pure-``.core`` classification a source file declared in no ``.core``
    fileset is invisible to RTL/TB gating (it classifies as neither). Surface it
    so the author registers it (``tags:[tb]`` for a testbench) or removes a stray
    file. No-op for a pre-migration project with no ``.core`` (the directory-
    prefix fallback governs there, so there is nothing meaningful to flag).
    """
    if not work_dir:
        return
    try:
        from booley.fusesoc.fusesoc_registry import classified_sources, discover_cores

        root = Path(work_dir)
        if not discover_cores(root):
            return
        cs = classified_sources(root)
        known = set(cs.rtl_source_files) | set(cs.tb_files)
    except Exception:  # noqa: BLE001 — best-effort probe; never block a review on it
        return
    for raw in scope_paths:
        path = raw.replace("\\", "/").removesuffix(" [new]").strip()
        if not path.lower().endswith(_SOURCE_SUFFIXES):
            continue
        norm = path[2:] if path.startswith("./") else path
        if norm not in known:
            logger.warning(
                "Review scope path %r is a source file not declared in any .core "
                "fileset — it is invisible to RTL/TB gating; register it in the "
                ".core (tags:[tb] for a testbench) or remove the stray file.",
                path,
            )


# Guide paths — resolved at call time via booley.runtime.paths
def _guide_paths() -> dict[str, str]:
    rd = refs_dir()
    return {
        "rtl_guide_dir": str(rd / "code_review" / "rtl"),
        "tb_guide_dir": str(rd / "code_review" / "testbench"),
        "tb_guide": str(rd / "code_review" / "testbench" / "tb-review.md"),
        "rtl_style_guide": str(rd / "rtl_style_guide.md"),
        "tb_style_guide": str(rd / "tb_style_guide.md"),
    }


# Project style-guide overlays, resolved against work_dir. Absence is the
# normal case for a project that has not authored one — never warn on a miss.
_STYLE_OVERLAYS = {
    "rtl": ".booley_project/rtl_style_guide.md",
    "tb": ".booley_project/tb_style_guide.md",
}


# Ticket types whose TB coverage baseline is "the scenarios already there"
# rather than "everything the checklist can ask for". The developer prompt
# tells a bugfix to minimize changes and a refactor to add no new scenarios;
# demanding brand-new stimulus classes at MAJOR/CRITICAL made those two
# instructions unsatisfiable at once. False-pass checks are NOT relaxed — a
# broken sentinel or a dead comparison is wrong regardless of ticket type.
_TB_COVERAGE_POLICY = {
    "bugfix": (
        "the developer is instructed to minimize changes and touch only what the defect requires"
    ),
    "refactor": ("the developer is instructed to preserve behavior and add no new test scenarios"),
}

# TB checklist items that demand *new* stimulus rather than sound checking of
# the stimulus already present. Only these are demoted for the types above.
# Named, not numbered: the SV and cocotb guides number their checklists
# independently, so numbers would silently point at the wrong rows.
_TB_COVERAGE_EXPANSION_CHECKS = (
    '"no edge-case vectors", "no randomized vectors", '
    '"insufficient stimulus diversity", "no reset-mid-operation test"'
)


@dataclass(frozen=True)
class ReviewDiff:
    """Scoped Git diff plus the exact new-file line ranges it authors."""

    patch: str
    changed_ranges: dict[str, tuple[tuple[int, int], ...]]

    def contains(self, file: str, line: int, work_dir: Path) -> bool:
        """Return whether ``file:line`` is an added or modified diff line."""
        normalized = _normalize_repo_path(file, work_dir)
        return any(start <= line <= end for start, end in self.changed_ranges.get(normalized, ()))

    def ranges_text(self) -> str:
        """Render a compact, deterministic changed-line allowlist."""
        lines: list[str] = []
        for path, ranges in sorted(self.changed_ranges.items()):
            spans = ", ".join(
                str(start) if start == end else f"{start}-{end}" for start, end in ranges
            )
            lines.append(f"- {path}: {spans}")
        return "\n".join(lines) or "- (no changed lines in the requested scope)"


@dataclass(frozen=True)
class TbProjectPolicy:
    """Project-owned simulation contracts relevant to TB review."""

    pass_sentinels: tuple[str, ...] = ()
    fail_sentinels: tuple[str, ...] = ()
    trace_files: tuple[str, ...] = ()

    @property
    def has_custom_sentinels(self) -> bool:
        return bool(self.pass_sentinels or self.fail_sentinels)


def _normalize_repo_path(raw: str, work_dir: Path) -> str:
    """Normalize a reviewer or Git path to a repository-relative POSIX path."""
    path = Path(raw.strip().replace("\\", "/"))
    if path.is_absolute():
        try:
            path = path.relative_to(work_dir.resolve())
        except ValueError:
            return path.as_posix()
    text = path.as_posix()
    return text[2:] if text.startswith("./") else text


def _parse_unified_diff(patch: str) -> dict[str, tuple[tuple[int, int], ...]]:
    """Extract exact new-file line ranges from a zero-context Git diff."""
    current_file = ""
    ranges: dict[str, list[tuple[int, int]]] = {}
    for raw_line in patch.splitlines():
        if raw_line.startswith("+++ "):
            current_file = raw_line[4:].strip()
            if current_file == "/dev/null":
                current_file = ""
            elif current_file.startswith("b/"):
                current_file = current_file[2:]
            continue
        match = _DIFF_HUNK_RE.match(raw_line)
        if not current_file or match is None:
            continue
        start = int(match.group(1))
        count = int(match.group(2) or "1")
        if count:
            ranges.setdefault(current_file, []).append((start, start + count - 1))
    return {path: tuple(spans) for path, spans in ranges.items()}


def _load_review_diff(work_dir: Path, diff_ref: str, scope: list[str]) -> ReviewDiff:
    """Resolve ``diff_ref`` and load a zero-context diff limited to ``scope``."""
    try:
        resolved = subprocess.run(
            ["git", "rev-parse", "--verify", f"{diff_ref}^{{commit}}"],
            cwd=work_dir,
            capture_output=True,
            text=True,
            timeout=15,
            check=False,
        )
    except (FileNotFoundError, OSError, subprocess.TimeoutExpired) as exc:
        raise ValueError(f"could not resolve --diff-ref {diff_ref!r}: {exc}") from exc
    if resolved.returncode != 0:
        reason = resolved.stderr.strip() or "not a commit in this worktree"
        raise ValueError(f"could not resolve --diff-ref {diff_ref!r}: {reason}")

    command = [
        "git",
        "-c",
        "core.quotePath=false",
        "diff",
        "--no-color",
        "--no-ext-diff",
        "--no-renames",
        "--unified=0",
        resolved.stdout.strip(),
        "--",
        *scope,
    ]
    try:
        result = subprocess.run(
            command,
            cwd=work_dir,
            capture_output=True,
            text=True,
            timeout=30,
            check=False,
        )
    except (FileNotFoundError, OSError, subprocess.TimeoutExpired) as exc:
        raise ValueError(f"could not read diff from {diff_ref!r}: {exc}") from exc
    if result.returncode != 0:
        reason = result.stderr.strip() or f"git diff exited {result.returncode}"
        raise ValueError(f"could not read diff from {diff_ref!r}: {reason}")
    return ReviewDiff(result.stdout, _parse_unified_diff(result.stdout))


def _load_tb_project_policy(work_dir: Path) -> TbProjectPolicy:
    """Read configured sentinel and trace contracts without leaking CWD config."""
    try:
        from booley.runtime.shared_infra import _load_rtl_config

        cfg = _load_rtl_config(work_dir)
    except ImportError:
        return TbProjectPolicy()
    flows = as_dict((cfg or {}).get("flows"), default={}) or {}
    sim = config_section(flows, "sim")
    return TbProjectPolicy(
        pass_sentinels=tuple(as_str_list(sim.get("pass_sentinels"))),
        fail_sentinels=tuple(as_str_list(sim.get("fail_sentinels"))),
        trace_files=tuple(as_str_list(sim.get("trace_files"))),
    )


# ---------------------------------------------------------------------------
# Ticket / spec resolution (spec focus, ADR 0038)
# ---------------------------------------------------------------------------

# Max spec content size before truncation (bytes)
_SPEC_MAX_SIZE = 30_000

# Max documented-assumptions content before truncation (bytes). Much smaller
# than the spec budget: this file is a short list of judgement calls, and a
# runaway one must not crowd out the spec it annotates.
_ASSUMPTIONS_MAX_SIZE = 8_000

# The developer records spec-silent judgement calls here (see the BLOCKED rule
# in developer_prompt.py). Reviews inline it so a documented assumption reads
# as a decision, not as an unexplained invention.
_ASSUMPTIONS_FILENAME = "answered_questions.md"


def _load_ticket_text(ticket_arg: str | None) -> tuple[str, str]:
    """Load ticket text from ``$BOOLEY_LOGS_DIR/ticket.md`` or the --ticket arg.

    Returns (ticket_text, source_path); ("", "") when neither is available.
    """
    logs_dir = os.environ.get("BOOLEY_LOGS_DIR", "")
    if logs_dir:
        ticket_path = Path(logs_dir) / "ticket.md"
        if ticket_path.is_file():
            return ticket_path.read_text(encoding="utf-8", errors="replace"), str(ticket_path)

    if ticket_arg:
        ticket_path = Path(ticket_arg)
        if ticket_path.is_file():
            return ticket_path.read_text(encoding="utf-8", errors="replace"), str(ticket_path)

    return "", ""


def _truncate_spec(content: str, *, label: str = "") -> str:
    """Truncate content to _SPEC_MAX_SIZE with a warning if needed."""
    if len(content) <= _SPEC_MAX_SIZE:
        return content
    if label:
        logger.warning("%s is %d bytes; truncating to %d", label, len(content), _SPEC_MAX_SIZE)
    return content[:_SPEC_MAX_SIZE] + "\n\n[SPEC TRUNCATED]"


def resolve_spec_content(
    ticket_arg: str | None,
    work_dir: str | Path | None = None,
) -> tuple[str | None, str]:
    """Resolve the spec text the spec-focus review checks the RTL against.

    Priority:
      1. ticket ``spec:`` frontmatter field (path to an external spec,
         relative to the project root / work_dir)
      2. ticket description body (everything after the frontmatter)

    Returns (spec_text, source_description), or (None, "") when no ticket
    is available or it carries neither a spec file nor a body.
    """
    from booley.ticket_board.frontmatter import parse_frontmatter

    ticket_text, ticket_source = _load_ticket_text(ticket_arg)
    if not ticket_text:
        return None, ""

    fields, body = parse_frontmatter(ticket_text)

    spec_field = fields.get("spec")
    if isinstance(spec_field, str) and spec_field.strip():
        base_dir = Path(work_dir) if work_dir else Path.cwd()
        spec_path = base_dir / spec_field.strip()
        if spec_path.is_file():
            content = spec_path.read_text(encoding="utf-8", errors="replace")
            return _truncate_spec(
                content, label=str(spec_path)
            ), f"spec file: {spec_field.strip()}"
        logger.warning("ticket spec: field points to missing file: %s", spec_path)

    if body.strip():
        return _truncate_spec(body.strip()), f"ticket body ({ticket_source})"

    return None, ""


def resolve_ticket_type(ticket_arg: str | None) -> str:
    """Return the ticket's ``type:`` frontmatter field, lowercased.

    Returns "" when no ticket is reachable or it declares no type — callers
    treat that as "no type-specific policy", i.e. the full checklist applies.
    """
    from booley.ticket_board.frontmatter import parse_frontmatter

    ticket_text, _ = _load_ticket_text(ticket_arg)
    if not ticket_text:
        return ""

    fields, _ = parse_frontmatter(ticket_text)
    ticket_type = fields.get("type")
    return ticket_type.strip().lower() if isinstance(ticket_type, str) else ""


def resolve_documented_assumptions() -> tuple[str, str]:
    """Load the developer's recorded spec-silent decisions, if any.

    Returns (text, source_path); ("", "") when the file is absent or empty.
    """
    logs_dir = os.environ.get("BOOLEY_LOGS_DIR", "")
    if not logs_dir:
        return "", ""

    path = Path(logs_dir) / _ASSUMPTIONS_FILENAME
    if not path.is_file():
        return "", ""

    try:
        content = path.read_text(encoding="utf-8", errors="replace").strip()
    except OSError:
        logger.warning("Could not read documented assumptions: %s", path)
        return "", ""

    if not content:
        return "", ""
    if len(content) > _ASSUMPTIONS_MAX_SIZE:
        logger.warning(
            "%s is %d bytes; truncating to %d", path, len(content), _ASSUMPTIONS_MAX_SIZE
        )
        content = content[:_ASSUMPTIONS_MAX_SIZE] + "\n\n[ASSUMPTIONS TRUNCATED]"
    return content, str(path)


def _strip_sv_comments(text: str) -> str:
    """Remove SystemVerilog comments before literal source-token checks."""
    text = re.sub(r"/\*.*?\*/", "", text, flags=re.DOTALL)
    return re.sub(r"//.*", "", text)


def _issue_claims_tb_dump_call(issue: dict[str, Any]) -> bool:
    """Return true when a reviewer finding is specifically about TB dump calls."""
    haystack = " ".join(str(issue.get(key, "")) for key in ("summary", "fix_suggestion")).lower()
    return "$dumpfile" in haystack or "$dumpvars" in haystack


def _issue_rejects_tb_owned_trace(issue: dict[str, Any]) -> bool:
    """Return true when a finding categorically forbids TB-owned tracing."""
    haystack = " ".join(str(issue.get(key, "")) for key in ("summary", "fix_suggestion")).lower()
    rejection = any(
        phrase in haystack
        for phrase in ("forbidden", "remove all", "user-authored", "override the harness")
    )
    return _issue_claims_tb_dump_call(issue) and rejection


def _issue_requires_builtin_sentinel(issue: dict[str, Any]) -> bool:
    """Return true for findings that reject a configured custom sentinel."""
    haystack = " ".join(str(issue.get(key, "")) for key in ("summary", "fix_suggestion")).lower()
    missing = "missing" in haystack or "never prints" in haystack or "must emit" in haystack
    return (
        "sentinel" in haystack
        and missing
        and ("[sim_result]" in haystack or "sim_result" in haystack)
    )


def _source_has_tb_dump_call(work_dir: str, source_file: str) -> bool | None:
    """Check current source for user-authored ``$dumpfile``/``$dumpvars``.

    Returns ``None`` when the cited source cannot be read, preserving the
    reviewer's fail-closed behavior for ambiguous cases.
    """
    path = Path(source_file)
    if not path.is_absolute():
        path = Path(work_dir) / path
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        logger.warning("Could not verify dump-call finding against missing file: %s", path)
        return None
    return bool(_TB_DUMP_CALL_RE.search(_strip_sv_comments(text)))


@dataclass
class ReviewIssue:
    """Single issue found during code review."""

    severity: str
    confidence: str
    category: str
    file: str
    line: int
    summary: str
    fix_suggestion: str = ""

    def to_dict(self) -> dict[str, Any]:
        d: dict[str, Any] = {
            "severity": self.severity,
            "confidence": self.confidence,
            "category": self.category,
            "file": self.file,
            "line": self.line,
            "summary": self.summary,
        }
        if self.fix_suggestion:
            d["fix_suggestion"] = self.fix_suggestion
        return d

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> ReviewIssue:
        return cls(
            severity=d.get("severity", "MINOR").upper(),
            confidence=d.get("confidence", "MEDIUM").upper(),
            category=d.get("category", ""),
            file=d.get("file", ""),
            line=d.get("line", 0),
            summary=d.get("summary", ""),
            fix_suggestion=d.get("fix_suggestion", ""),
        )


def _finding_record(issue: ReviewIssue) -> dict[str, Any]:
    """Return one persisted finding with a stable content-derived identifier."""
    record = issue.to_dict()
    identity = json.dumps(record, sort_keys=True, separators=(",", ":"))
    record["finding_id"] = hashlib.sha256(identity.encode()).hexdigest()[:16]
    return record


def _validate_issue_dict(d: Any, allowed_category: str | None = None) -> list[str]:
    """Return list of schema violations for one issue dict. Empty = valid.

    Enforces the strict reviewer-output schema at the parsing boundary so
    malformed entries can be rejected upstream rather than silently
    coerced into MINOR / MEDIUM defaults downstream.
    """
    if not isinstance(d, dict):
        return [f"issue must be a JSON object (got {type(d).__name__})"]

    errs: list[str] = []
    sev = d.get("severity")
    if not isinstance(sev, str) or sev.upper() not in ALL_SEVERITIES:
        errs.append(
            f"severity must be one of {sorted(ALL_SEVERITIES)} (got {sev!r})",
        )
    conf = d.get("confidence")
    if not isinstance(conf, str) or conf.upper() not in ALL_CONFIDENCES:
        errs.append(
            f"confidence must be one of {sorted(ALL_CONFIDENCES)} (got {conf!r})",
        )
    category = d.get("category")
    if not isinstance(category, str) or not category.strip():
        errs.append(f"category must be a non-empty string (got {category!r})")
    elif allowed_category is not None and category.strip().lower() != allowed_category:
        errs.append(
            f"category must be exactly {allowed_category!r} "
            f"for this review focus (got {category!r})",
        )
    file_ = d.get("file")
    if not isinstance(file_, str) or not file_.strip():
        errs.append(f"file must be a non-empty string (got {file_!r})")
    line = d.get("line")
    if not isinstance(line, int) or isinstance(line, bool) or line < 0:
        errs.append(f"line must be a non-negative int (got {line!r})")
    summary = d.get("summary")
    if not isinstance(summary, str) or not summary.strip():
        errs.append(f"summary must be a non-empty string (got {summary!r})")
    return errs


def _validate_finding_dict(d: Any) -> list[str]:
    """Return list of schema violations for one verify-finding dict.

    A FIXED status without a non-blank ``evidence`` string is a schema
    violation — the verify loop refuses to trust a FIXED claim that has
    no anchor in the actual code.
    """
    if not isinstance(d, dict):
        return [f"finding must be a JSON object (got {type(d).__name__})"]

    errs: list[str] = []
    idx = d.get("index")
    if not isinstance(idx, int) or isinstance(idx, bool) or idx < 1:
        errs.append(f"index must be a 1-based positive int (got {idx!r})")
    status_raw = d.get("status")
    if not isinstance(status_raw, str) or status_raw.upper() not in ALL_VERIFY_STATUSES:
        errs.append(
            f"status must be one of {sorted(ALL_VERIFY_STATUSES)} (got {status_raw!r})",
        )
    if isinstance(status_raw, str) and status_raw.upper() == VERIFY_STATUS_FIXED:
        evidence = d.get("evidence")
        if not isinstance(evidence, str) or not evidence.strip():
            errs.append(
                "FIXED status requires a non-empty 'evidence' string "
                "(format: '<file:line> — <justification>')",
            )
    if isinstance(status_raw, str) and status_raw.upper() == VERIFY_STATUS_WAIVED:
        justification = d.get("justification")
        if not isinstance(justification, str) or not justification.strip():
            errs.append("WAIVED status requires a non-empty 'justification' string")
    return errs


@dataclass
class ReviewParseResult:
    """Outcome of parsing reviewer agent output.

    ``json_present`` distinguishes "agent emitted no JSON wrapper at all"
    (typically free-form prose) from "agent emitted a wrapper with zero
    issues" — the latter is a legitimate clean review, the former is a
    schema violation the harness must not silently accept as a pass.
    """

    issues: list[ReviewIssue]
    json_present: bool
    rejected: list[tuple[int, list[str]]]  # (1-based item ordinal, errors)


def _find_balanced_json_object(
    text: str,
    required_key: str,
) -> dict[str, Any] | None:
    """Find the first balanced ``{...}`` JSON object containing ``required_key``.

    Walks ``text`` with brace tracking that respects JSON string literals
    and escapes, so payload content containing literal ``{``, ``}``,
    ``[``, ``]`` (e.g. Verilog bit-selects quoted inside ``spec_clause`` or
    ``evidence`` fields) does not confuse the scan.

    Returns the decoded dict on the first balanced object that parses as
    JSON *and* carries the expected wrapper key. Falls through past
    candidates that fail either check, so a malformed prose ``{...}``
    earlier in the output cannot mask a real wrapper later.
    """
    n = len(text)
    i = 0
    while i < n:
        if text[i] != "{":
            i += 1
            continue
        depth = 0
        in_string = False
        escape = False
        end = -1
        for j in range(i, n):
            ch = text[j]
            if in_string:
                if escape:
                    escape = False
                elif ch == "\\":
                    escape = True
                elif ch == '"':
                    in_string = False
                continue
            if ch == '"':
                in_string = True
            elif ch == "{":
                depth += 1
            elif ch == "}":
                depth -= 1
                if depth == 0:
                    end = j
                    break
        if end >= 0:
            try:
                data = json.loads(text[i : end + 1])
                if isinstance(data, dict) and required_key in data:
                    return data
            except json.JSONDecodeError:
                pass
        # Advance by one char (not past ``end``) so a real wrapper nested
        # inside a malformed outer ``{...}`` is still discoverable, and
        # so that an unbalanced earlier ``{`` (end<0) does not stop us
        # from finding a balanced wrapper later in the output.
        i += 1
    return None


def _extract_issues_payload(output: str) -> tuple[list[Any], bool]:
    """Extract the raw items list from agent output.

    Returns (items, json_present). ``json_present=True`` means a JSON
    wrapper object with an ``issues`` key was decoded, even if it
    contained zero items.
    """
    data = _find_balanced_json_object(output, "issues")
    if data is None:
        return [], False
    items = data.get("issues", [])
    if not isinstance(items, list):
        return [], False
    return list(items), True


def parse_review_output(
    output: str,
    allowed_category: str | None = None,
) -> ReviewParseResult:
    """Strict-schema parser for the initial review agent output.

    Each item is validated against the issue schema. Items that violate
    the schema are dropped with a logged warning rather than coerced
    into placeholder defaults — the agent's output is the contract.
    """
    raw_items, json_present = _extract_issues_payload(output)
    issues: list[ReviewIssue] = []
    rejected: list[tuple[int, list[str]]] = []
    for ord_idx, item in enumerate(raw_items, 1):
        errs = _validate_issue_dict(item, allowed_category=allowed_category)
        if errs:
            rejected.append((ord_idx, errs))
            logger.warning(
                "Rejecting review issue #%d due to schema violations: %s",
                ord_idx,
                "; ".join(errs),
            )
            continue
        issues.append(ReviewIssue.from_dict(item))
    return ReviewParseResult(
        issues=issues,
        json_present=json_present,
        rejected=rejected,
    )


# Name of the native Claude Code review-reporting capability. When the review
# sub-agent runs on a Claude backend it reports findings through this capability
# call rather than printing the ``{"issues": [...]}`` text contract, so the
# harness captures the call inputs (see AgentCallParams.capture_agent_capability_calls)
# and this module maps them onto ReviewIssues.
REPORT_FINDINGS_CAPABILITY = "ReportFindings"


def _report_finding_severity(verdict: str) -> tuple[str, str]:
    """Map a ReportFindings ``verdict`` to (severity, confidence).

    ReportFindings carries no severity/confidence — only a ``verdict`` that is
    ``CONFIRMED`` (survived adversarial verification) or ``PLAUSIBLE`` (likely
    but unverified), and is often *absent* on a single-pass review. The gate
    blocks on CRITICAL/MAJOR, so:
      - CONFIRMED or absent -> MAJOR (fail-closed: a reported defect blocks)
      - PLAUSIBLE           -> MINOR (advisory: noted, does not block)
    """
    if verdict.strip().upper() == "PLAUSIBLE":
        return SEVERITY_MINOR, CONFIDENCE_MEDIUM
    return SEVERITY_MAJOR, CONFIDENCE_HIGH


def report_findings_to_issues(
    findings: list[Any],
    focus: str,
) -> tuple[list[ReviewIssue], int]:
    """Map captured ReportFindings entries onto ReviewIssues.

    Returns (issues, dropped) where ``dropped`` counts entries skipped for
    missing a file or summary. ``category`` is forced to the active ``focus``
    (ReportFindings' own free-form ``category`` slug is not one of our focus
    names); the finding's ``failure_scenario`` is preserved as the fix hint so
    the downstream coder keeps the concrete repro.
    """
    issues: list[ReviewIssue] = []
    dropped = 0
    for f in findings:
        if not isinstance(f, dict):
            dropped += 1
            continue
        file_ = str(f.get("file", "")).strip()
        summary = str(f.get("summary", "")).strip()
        if not file_ or not summary:
            dropped += 1
            continue
        severity, confidence = _report_finding_severity(str(f.get("verdict", "")))
        line = f.get("line", 0)
        if not isinstance(line, int) or isinstance(line, bool) or line < 0:
            line = 0
        scenario = str(f.get("failure_scenario", "")).strip()
        issues.append(
            ReviewIssue(
                severity=severity,
                confidence=confidence,
                category=focus,
                file=file_,
                line=line,
                summary=summary,
                fix_suggestion=(f"Failure scenario: {scenario}" if scenario else ""),
            )
        )
    return issues, dropped


def parse_issues(output: str) -> list[ReviewIssue]:
    """Parse issues JSON from agent output (schema-validated).

    Convenience wrapper around :func:`parse_review_output` that returns
    just the valid issues. Callers that need to distinguish "no JSON at
    all" from "JSON with zero valid issues" should use
    ``parse_review_output`` directly.
    """
    return parse_review_output(output).issues


def count_by_severity(issues: list[ReviewIssue]) -> dict[str, int]:
    """Count issues grouped by severity."""
    counts: dict[str, int] = {
        SEVERITY_CRITICAL: 0,
        SEVERITY_MAJOR: 0,
        SEVERITY_MINOR: 0,
    }
    for issue in issues:
        sev = issue.severity.upper()
        if sev in counts:
            counts[sev] += 1
    return counts


def check_gate(counts: dict[str, int]) -> bool:
    """Gate passes when there are zero critical and zero major issues."""
    return counts[SEVERITY_CRITICAL] == 0 and counts[SEVERITY_MAJOR] == 0


def _channel_severity_rank(issues: list[ReviewIssue]) -> tuple[int, int, int, int]:
    """Order two reporting channels by how severe their verdict is.

    Sorts on (gate fails, criticals, majors, total) so that "blocks the
    gate" always outranks "more issues but all advisory". Used to resolve a
    ReportFindings-vs-text-JSON disagreement without merging (SETUP-F-33).
    """
    counts = count_by_severity(issues)
    gate_failed = 0 if check_gate(counts) else 1
    return (gate_failed, counts[SEVERITY_CRITICAL], counts[SEVERITY_MAJOR], len(issues))


def _format_status_and_counts(
    counts: dict[str, int],
    gate_passed: bool,
) -> tuple[str, str]:
    """Return (status, count_str) for a review result summary line.

    status is "PASS"/"FAIL"; count_str is a comma-joined per-severity
    tally like "1 critical, 2 minor", or "0 issues" when empty.
    """
    status = "PASS" if gate_passed else "FAIL"
    count_parts = []
    for sev in (SEVERITY_CRITICAL, SEVERITY_MAJOR, SEVERITY_MINOR):
        if counts[sev] > 0:
            count_parts.append(f"{counts[sev]} {sev.lower()}")
    count_str = ", ".join(count_parts) if count_parts else "0 issues"
    return status, count_str


def _format_issue_line(issue: ReviewIssue) -> str:
    """Render one finding as a compact ``[C] file:line — summary`` display line."""
    tag = _SEVERITY_TAG.get(issue.severity.upper(), issue.severity[0])
    return f"[{tag}] {issue.file}:{issue.line} — {issue.summary}"


def validate_scope_category(
    scope_paths: list[str],
    category: str,
    work_dir: Path | None = None,
) -> list[str]:
    """Validate scope paths match the review category.

    Returns list of error messages (empty = valid).
    """
    from booley.runtime.shared_infra import source_path_matches

    errors: list[str] = []
    for path in scope_paths:
        if category == "rtl":
            rtl_prefixes = _get_rtl_prefixes(work_dir)
            if not source_path_matches(path, rtl_prefixes):
                allowed = ", ".join(sorted(set(rtl_prefixes)))
                errors.append(f"Path '{path}' doesn't match RTL source paths ({allowed})")
        elif category == "tb":
            tb_prefixes = _get_tb_prefixes(work_dir)
            if not source_path_matches(path, tb_prefixes):
                allowed = ", ".join(sorted(set(tb_prefixes)))
                errors.append(f"Path '{path}' doesn't match TB source paths ({allowed})")
    return errors


def format_summary_line(
    category: str,
    focus: str,
    issue_count: int,
    counts: dict[str, int],
    duration_s: float,
) -> str:
    """Format a single summary line for output.

    Example: ``[review] rtl / functional   2 issues (1 CRITICAL, 1 MINOR)    45s``
    """
    label = f"{category} / {focus}" if focus else category
    # Build severity breakdown
    parts: list[str] = []
    for sev in (SEVERITY_CRITICAL, SEVERITY_MAJOR, SEVERITY_MINOR):
        c = counts.get(sev, 0)
        if c > 0:
            parts.append(f"{c} {sev}")
    breakdown = f" ({', '.join(parts)})" if parts else ""
    issue_word = "issue" if issue_count == 1 else "issues"
    return f"[review] {label:<25s} {issue_count} {issue_word}{breakdown:<30s} {duration_s:.0f}s"


class ReviewerSpecialist(Specialist):
    """Single-focus code review: reports issues by severity."""

    name: str = "reviewer"
    description: str = "Single-focus code review: reports issues by severity"
    code_modifying: bool = False
    config_aware: bool = False
    min_model: str = "standard"
    default_max_turns: int = 30
    default_timeout: int = 1800  # 30 min
    min_timeout: int = 600  # 10 min — read-only, faster

    satisfies: ClassVar[list[str]] = [
        "review_rtl_spec",
        "review_rtl_bugs",
        "review_rtl_protocol",
        "review_tb_quality",
        "review_rtl_security",
        "review_rtl_optimization",
        "review_rtl_code_style",
    ]
    satisfies_args: ClassVar[dict[str, str]] = {
        "review_rtl_spec": "--category rtl --focus spec",
        "review_rtl_bugs": "--category rtl --focus bugs",
        "review_rtl_protocol": "--category rtl --focus protocol",
        "review_tb_quality": "--category tb --focus quality",
        "review_rtl_security": "--category rtl --focus security",
        "review_rtl_optimization": "--category rtl --focus optimization",
        "review_rtl_code_style": "--category rtl --focus code_style",
    }

    # Review agent only reads code through bounded read/search agent capabilities.
    agent_capabilities: ClassVar[list[str]] = ["Read", "Grep", "Glob"]

    # The provider-independent boundary: the nested reviewer receives a
    # disposable snapshot, so neither backend can modify the real worktree.
    workspace_access = "read_only"

    # Claude's deny list remains defense-in-depth around the snapshot. Its
    # allowed_agent_capabilities list is advisory under unattended bypassPermissions, so
    # the reviewer used Bash despite the read-only-looking allowlist
    # (SETUP-F-35). Codex has no equivalent list; workspace_access is the
    # cross-provider boundary and this list only narrows Claude's agent-capability loop.
    READ_ONLY_DENY: ClassVar[list[str]] = [
        "Bash",
        "BashOutput",
        "KillShell",
        "Write",
        "Edit",
        "MultiEdit",
        "NotebookEdit",
        "Task",
        "WebFetch",
        "WebSearch",
        "SlashCommand",
    ]

    def __init__(self) -> None:
        super().__init__()
        self._review_diff: ReviewDiff | None = None
        self._tb_project_policy: TbProjectPolicy | None = None

    def _workspace_isolation_category(self) -> str | None:
        """Hide opposite-category sources only inside the private snapshot."""
        if self._args is None:
            return None
        return self.args.category

    @property
    def display_tag(self) -> str | None:
        if not self._args:
            return None
        tag = f"{self.args.category}/{next(iter(self._parse_focus()), '?')}"
        if self._is_verify_pass():
            tag += " verify fix"
        return tag

    def _is_verify_pass(self) -> bool:
        """True when this invocation will run verify (not initial) review."""
        if not self._is_clean_mode():
            return False
        prior = self._get_prior_detail(self._criterion_key())
        if prior is None:
            return False
        # ``pending`` is the new field, ``issue_list`` is legacy (kept for
        # in-flight state migration). Either marks a prior review pass.
        return "pending" in prior or "issue_list" in prior

    def _add_agent_args(self, parser: argparse.ArgumentParser) -> None:
        parser.add_argument(
            "--scope",
            required=True,
            help="Comma-separated file paths to review",
        )
        parser.add_argument(
            "--category",
            required=True,
            choices=["rtl", "tb"],
            help="Review category: rtl or tb",
        )
        parser.add_argument(
            "--focus",
            required=True,
            help=(
                "Review focus category. "
                f"RTL: {', '.join(sorted(RTL_FOCUS_CATEGORIES))}. "
                f"TB: {', '.join(sorted(TB_FOCUS_CATEGORIES))}."
            ),
        )
        parser.add_argument(
            "--diff-ref",
            default=None,
            help="Git ref to diff against (omit = review full files)",
        )
        parser.add_argument(
            "--steer",
            default=None,
            action="append",
            help=(
                "Developer Agent context / steering instructions. Repeatable — "
                'over MCP this is an array of strings (pass ["..."], not a '
                "bare string), each element appended as its own context line."
            ),
        )
        parser.add_argument(
            "--ticket",
            default=None,
            help=(
                "Path to the spec / ticket .md file the review is checking "
                "against. In Ticket Mode this is auto-resolved from "
                "$BOOLEY_LOGS_DIR/ticket.md; in Interactive Mode the outer "
                "agent passes it explicitly (e.g. --ticket /work/spec.md)."
            ),
        )

    # --- Validation ---

    def _validate_args(self) -> list[str]:
        """Validate argument combinations. Returns list of error messages."""
        errors: list[str] = []

        focus_cats = self._parse_focus()
        if not focus_cats:
            errors.append("--focus is required")
            return errors

        if self.args.category == "tb":
            invalid = focus_cats - TB_FOCUS_CATEGORIES
            if invalid:
                errors.append(
                    f"Invalid TB focus: {', '.join(sorted(invalid))}. "
                    f"Valid: {', '.join(sorted(TB_FOCUS_CATEGORIES))}"
                )
        elif self.args.category == "rtl":
            invalid = focus_cats - RTL_FOCUS_CATEGORIES
            if invalid:
                errors.append(
                    f"Invalid RTL focus: {', '.join(sorted(invalid))}. "
                    f"Valid: {', '.join(sorted(RTL_FOCUS_CATEGORIES))}"
                )

        # Validate scope paths against category
        scope_paths = self._parse_scope()
        scope_errors = validate_scope_category(
            scope_paths,
            self.args.category,
            self.args.work_dir,
        )
        errors.extend(scope_errors)
        _warn_unregistered_sources(scope_paths, self.args.work_dir)

        return errors

    def _parse_scope(self) -> list[str]:
        """Parse comma-separated scope paths."""
        return [s.strip() for s in self.args.scope.split(",") if s.strip()]

    def _parse_focus(self) -> set[str]:
        """Parse comma-separated focus categories."""
        if not self.args.focus:
            return set()
        return {f.strip().lower() for f in self.args.focus.split(",") if f.strip()}

    def _criterion_base_key(self) -> str:
        """Derive the criterion base key from category and focus.

        Returns the base key without the _done suffix.
        Produces e.g. ``review_rtl_spec``, ``review_tb_quality``.
        """
        focus = next(iter(self._parse_focus()), "")
        return f"review_{self.args.category}_{focus}"

    # --- System prompt construction ---

    @staticmethod
    def _read_guide(path: str) -> str:
        """Read a review guide file and return its content."""
        p = Path(path)
        if not p.is_file():
            logger.warning("Review guide not found: %s", path)
            return ""
        return p.read_text(encoding="utf-8", errors="replace")

    def _read_style_overlay(self, category: str) -> str:
        """Read the project's style guide overlay, or "" if it has none."""
        rel = _STYLE_OVERLAYS.get(category)
        if not rel:
            return ""
        root = Path(self.args.work_dir) if self.args.work_dir else Path.cwd()
        path = root / rel
        if not path.is_file():
            return ""
        logger.info("Applying project style guide overlay: %s", path)
        return path.read_text(encoding="utf-8", errors="replace")

    def _build_system_prompt(self, focus: str) -> str:
        """Build focus-specific system prompt with methodology and inlined guide."""
        category = self.args.category
        gp = _guide_paths()
        sections: list[str] = []

        # --- Methodology preamble ---
        cat_label = "RTL" if category == "rtl" else "testbench"
        sections.append(f"You are a {cat_label} code reviewer.\n")
        sections.append("""\
## Review methodology

Work through every criterion in the review guide below. Read the files in scope \
first, then use Grep to trace signal drivers/consumers, package definitions, and \
state transitions. Report a finding only after confirming it in the code — drop \
anything you cannot substantiate. Make each finding's severity and confidence \
match the strength of the evidence.
""")

        # --- Inlined focus guide ---
        if category == "rtl":
            guide_name = "protocol-cdc" if focus == "protocol" else focus
            guide_path = f"{gp['rtl_guide_dir']}/{guide_name}.md"
        else:
            guide_path = gp["tb_guide"]

        guide_content = self._read_guide(guide_path)
        if guide_content:
            sections.append(f"## Review guide — {focus}\n")
            sections.append(guide_content)
            sections.append("")

        # --- Style guides (quality focus only) ---
        if focus == "quality":
            label = "RTL" if category == "rtl" else "Testbench"
            key = "rtl_style_guide" if category == "rtl" else "tb_style_guide"
            style_content = self._read_guide(gp[key])
            if style_content:
                sections.append(f"## {label} style guide\n")
                sections.append(style_content)
                sections.append("")

            overlay = self._read_style_overlay(category)
            if overlay:
                sections.append(f"## {label} style guide — project overlay\n")
                sections.append(
                    "Rules below are authored by this project and take "
                    "precedence over the generic guide above wherever the two "
                    "conflict. Review against them with the same severity "
                    "levels.\n"
                )
                sections.append(overlay)
                sections.append("")

        # --- Output format ---
        # Guides may include human-oriented examples, but the parser contract
        # below is always authoritative.
        sections.append(self._output_instructions(focus, category))

        return "\n".join(sections)

    # --- Prompt construction ---

    def _build_prompt(self, *, focus_override: str | None = None) -> str:
        """Build review task prompt (scope, focus, diff-ref, steering).

        The RTL and TB paths differ ONLY in how the ``## Focus:`` header is
        derived — RTL joins a sorted multi-category set, TB uses a single
        category (defaulting to ``quality``). The surrounding scaffold is
        identical, so it lives here once.

        Args:
            focus_override: If set, use this focus instead of ``self.args.focus``.
                Avoids mutating shared state.
        """
        scope = self._parse_scope()

        if self.args.category == "rtl":
            # RTL: sorted, comma-joined multi-category focus.
            if focus_override is not None:
                focus_cats = sorted(
                    {f.strip().lower() for f in focus_override.split(",") if f.strip()}
                )
            else:
                focus_cats = sorted(self._parse_focus())
            focus_header = ", ".join(focus_cats)
        else:
            # TB: single focus, defaulting to "quality".
            focus_header = focus_override or next(iter(self._parse_focus()), "quality")

        sections: list[str] = []
        sections.append("Review the following files:\n")
        for path in scope:
            sections.append(f"  - {path}")
        sections.append("")

        sections.append(f"## Focus: {focus_header}")
        sections.append("")

        # Inline the spec for spec focus (ADR 0038, guarded in _run) and for
        # every TB review: the TB checklist asks whether the testbench encodes
        # the spec's own numbers and whether its expected-value model is
        # independent of the implementation, and neither is answerable without
        # the spec in hand. Absence is not fatal on the TB side — the checks
        # that need it simply go unreported.
        if SPEC_FOCUS in focus_header.split(", ") or self.args.category == "tb":
            spec_section = self._build_spec_section()
            if spec_section:
                sections.append(spec_section)

        # Spec-silent decisions the developer already recorded. Without these a
        # reasoned interpretation of an undefined corner reads as an invention.
        assumptions_section = self._build_assumptions_section()
        if assumptions_section:
            sections.append(assumptions_section)

        # Ticket-type severity policy (TB only — the RTL guides carry no
        # coverage-expansion checks to relax).
        if self.args.category == "tb":
            policy_section = self._build_ticket_type_policy_section()
            if policy_section:
                sections.append(policy_section)

            project_policy = self._build_tb_project_policy_section()
            if project_policy:
                sections.append(project_policy)

        # Diff-ref is a mechanically enforced finding boundary. The agent is
        # read-only and cannot run Git, so inline both the allowlist and patch.
        if self.args.diff_ref:
            sections.append(self._build_diff_section())

        # Steering
        steer_text = self.steering_text()
        if steer_text:
            sections.append(f"## Developer Agent Context\n{steer_text}\n")

        return "\n".join(sections)

    def _build_spec_section(self) -> str:
        """Build the inlined ``## Specification`` section for spec-focus prompts.

        Returns an empty string when no spec content is available.
        """
        spec_content, spec_source = resolve_spec_content(
            getattr(self.args, "ticket", None),
            getattr(self.args, "work_dir", None),
        )
        if not spec_content:
            return ""
        return f"## Specification (source: {spec_source})\n\n{spec_content}\n"

    def _build_assumptions_section(self) -> str:
        """Inline the developer's recorded spec-silent decisions.

        Returns an empty string when nothing has been recorded.
        """
        content, source = resolve_documented_assumptions()
        if not content:
            return ""
        return (
            f"## Documented Assumptions (source: {source})\n\n"
            "The developer recorded these decisions for points the spec does "
            "not settle. A behavior explained here is a documented judgement "
            "call, not an unexplained invention: report it only if the "
            "reasoning contradicts spec text, and then quote the text it "
            f"contradicts.\n\n{content}\n"
        )

    def _build_ticket_type_policy_section(self) -> str:
        """Build the TB severity policy implied by the ticket's type.

        Returns an empty string for types with no coverage-expansion relief
        (``feature``, ``verification``, or an unknown/absent type), where the
        checklist applies at full strength.
        """
        ticket_type = resolve_ticket_type(getattr(self.args, "ticket", None))
        rationale = _TB_COVERAGE_POLICY.get(ticket_type)
        if not rationale:
            return ""
        return (
            f"## Ticket Type: {ticket_type}\n\n"
            f"This ticket is a **{ticket_type}** — {rationale}. Coverage-"
            f"expansion checks ({_TB_COVERAGE_EXPANSION_CHECKS}) are therefore "
            "**MINOR at most** here, and worth reporting only when the missing "
            "stimulus bears directly on the change under review. This "
            "overrides the severity the review guide states for those checks.\n\n"
            "Every other check keeps its stated severity. False-pass risks, "
            "dead or missing comparisons, broken sentinels, ignored error "
            "counters, sampling races, and simulator-compatibility traps are "
            "about whether the existing checks work at all — no ticket type "
            "excuses those.\n"
        )

    def _build_tb_project_policy_section(self) -> str:
        """Render project-configured simulation contracts for TB review."""
        policy = self._tb_policy()
        if not policy.has_custom_sentinels and not policy.trace_files:
            return ""
        lines = [
            "## Project Simulation Contract",
            "",
            "This project policy is authoritative and overrides generic review-guide defaults.",
        ]
        if policy.has_custom_sentinels:
            lines.extend(
                [
                    f"- Configured pass sentinels: {list(policy.pass_sentinels)!r}",
                    f"- Configured fail sentinels: {list(policy.fail_sentinels)!r}",
                    "Do not require `[SIM_RESULT]` markers when these configured sentinels "
                    "provide the verdict contract.",
                ]
            )
        if policy.trace_files:
            lines.extend(
                [
                    f"- Configured testbench-owned trace files: {list(policy.trace_files)!r}",
                    "The project deliberately adopts these traces. Do not report guarded "
                    "`$dumpfile`/`$dumpvars` blocks merely for being testbench-authored.",
                ]
            )
        return "\n".join(lines) + "\n"

    def _build_diff_section(self) -> str:
        """Render the resolved diff and its exact finding allowlist."""
        if self._review_diff is None:
            return (
                f"## Diff Reference: {self.args.diff_ref}\n\n"
                "The diff is resolved when the Specialist runs. Findings are limited to changed code.\n"
            )
        patch = self._review_diff.patch
        if len(patch) > _MAX_DIFF_PROMPT_CHARS:
            patch = patch[:_MAX_DIFF_PROMPT_CHARS] + "\n[DIFF TRUNCATED — use the allowlist]\n"
        return (
            f"## Enforced Diff Boundary: {self.args.diff_ref}\n\n"
            "Only report a finding when its primary `file:line` is an added or modified "
            "line in the allowlist below. Unchanged baseline code may be read as context, "
            "but it is out of scope and cannot be a finding. In particular, do not report "
            "unchanged sentinel or trace-dump blocks. The harness discards findings outside "
            "this boundary.\n\n"
            f"### Changed-line allowlist\n{self._review_diff.ranges_text()}\n\n"
            f"### Scoped patch\n```diff\n{patch}\n```\n"
        )

    def _tb_policy(self) -> TbProjectPolicy:
        """Return the cached TB policy for this worktree."""
        if self._tb_project_policy is None:
            self._tb_project_policy = _load_tb_project_policy(Path(self.args.work_dir))
        return self._tb_project_policy

    def _prepare_diff_boundary(self) -> str | None:
        """Load ``--diff-ref`` once; return an actionable error on failure."""
        if not self.args.diff_ref:
            return None
        try:
            self._review_diff = _load_review_diff(
                Path(self.args.work_dir),
                self.args.diff_ref,
                self._parse_scope(),
            )
        except ValueError as exc:
            return str(exc)
        return None

    @staticmethod
    def _output_instructions(focus: str, category: str = "rtl") -> str:
        """Standard output format instructions appended to all prompts.

        The example ``file`` path follows the review category: a TB reviewer
        is told not to read RTL, so an ``rtl/`` example invited findings it had
        no business filing.
        """
        example_file = "verif/mod_a_tb.sv" if category == "tb" else "rtl/mod_a.sv"
        return f"""\
## Output Format (STRICT SCHEMA — malformed entries are rejected)

Emit one JSON object with an ``issues`` array; entries that violate the
schema are dropped upstream. Fields (all required unless noted):
  - severity:       "CRITICAL" | "MAJOR" | "MINOR"   (uppercase)
  - confidence:     "HIGH" | "MEDIUM" | "LOW"        (uppercase)
  - category:       "{focus}"                        (active focus)
  - file:           non-empty path string
  - line:           non-negative integer
  - summary:        non-empty one-line description
  - fix_suggestion: string (OPTIONAL; omit instead of empty)

```json
{{
  "issues": [
    {{
      "severity": "CRITICAL",
      "confidence": "HIGH",
      "category": "{focus}",
      "file": "{example_file}",
      "line": 42,
      "summary": "Description of the issue",
      "fix_suggestion": "What to change"
    }}
  ]
}}
```

No issues → output exactly {{"issues": []}}. Prose outside the JSON is
ignored by the parser. Never emit CRITICAL or MAJOR at LOW confidence —
downgrade to MINOR or omit.

The ``ReportFindings`` agent capability is available but is only a *mirror* of this
JSON, never a replacement: whatever you report through it MUST also
appear in the ``issues`` array above (its ``summary``/``file``/``line``
map straight across). A ``ReportFindings`` call with an empty
``findings`` list does not mean "clean" — only ``{{"issues": []}}`` in
this final message does. Always end your final message with the JSON
object, even after calling the capability.
"""

    # --- Mode detection helpers ---

    def _criterion_key(self) -> str:
        """Return the full criterion key (with suffix) from state.

        Checks for ``{base}_clean`` first, then ``{base}_done``.
        Raises ValueError if both exist (mutual exclusion).
        Falls back to ``_done`` when state is unavailable.
        """
        base_key = self._criterion_base_key()
        clean_key = f"{base_key}_clean"
        done_key = f"{base_key}_done"
        try:
            state = self.state
        except RuntimeError:
            return done_key
        if not state:
            return done_key
        has_clean = state.has_criterion(clean_key)
        has_done = state.has_criterion(done_key)
        if has_clean and has_done:
            raise ValueError(
                f"Mutual exclusion: both {done_key} and {clean_key} exist — "
                "remove one from the ticket criteria"
            )
        if has_clean:
            return clean_key
        return done_key

    def _is_clean_mode(self) -> bool:
        """True when the active criterion uses the ``_clean`` suffix."""
        return self._criterion_key().endswith("_clean")

    def _check_scope_files_exist(self) -> list[str]:
        """Return list of scope files that don't exist in the work directory.

        Only checks when work_dir looks like a harness worktree (contains
        rtl/ or verif/ subdirs). Skips in test or human-mode contexts where
        the work_dir is unrelated to the review scope.
        """
        work_dir = self.args.work_dir
        if work_dir is None:
            return []
        base = Path(work_dir)
        has_source_dirs = (base / "rtl").is_dir() or (base / "verif").is_dir()
        if not has_source_dirs:
            return []
        return [p for p in self._parse_scope() if not (base / p).exists()]

    # --- Main execution override ---

    def _run(self) -> McpToolResult:  # noqa: PLR0911 — one early return per validation/mode branch of the review flow
        """Single-focus review with terminal _done or disposition-loop _clean mode."""
        errors = self._validate_args()
        if errors:
            return McpToolResult(
                exit_code=EXIT_ERROR,
                report_text="Validation errors:\n" + "\n".join(f"  - {e}" for e in errors),
            )

        try:
            crit_key = self._criterion_key()
        except ValueError as exc:
            return McpToolResult(exit_code=EXIT_ERROR, report_text=str(exc))

        # Scope file existence guard: if review targets don't exist yet
        # (e.g., TB not coded yet), fail early instead of letting the LLM
        # agent silently report "0 issues" on non-existent code.
        missing = self._check_scope_files_exist()
        if missing:
            names = ", ".join(missing)
            return McpToolResult(
                exit_code=EXIT_ERROR,
                report_text=(
                    f"Scope files not found: {names}. "
                    "Code the missing files before running this review."
                ),
            )

        diff_error = self._prepare_diff_boundary()
        if diff_error:
            return McpToolResult(
                exit_code=EXIT_ERROR,
                report_text=f"Diff boundary error: {diff_error}",
            )

        # Spec-availability guard: a spec-compliance review without a spec is
        # meaningless — fail fast instead of letting the agent report a clean
        # pass against nothing (mirrors the scope-file guard above).
        if SPEC_FOCUS in self._parse_focus():
            spec_content, _ = resolve_spec_content(
                getattr(self.args, "ticket", None),
                getattr(self.args, "work_dir", None),
            )
            if spec_content is None:
                return McpToolResult(
                    exit_code=EXIT_ERROR,
                    report_text=(
                        "Spec review needs a spec to check against, but none "
                        "was found. In Ticket Mode the ticket body (or its "
                        "spec: field) is used automatically; in Interactive "
                        "Mode pass --ticket <path to spec/ticket .md>."
                    ),
                )

        self.emit_progress(f"reviewing {self.args.category}/{next(iter(self._parse_focus()))}")

        if self._is_clean_mode():
            return self._run_clean_mode(crit_key)

        # _done mode records terminal review completion, not cleanliness.
        # Findings remain available in detail; callers that need a blocking
        # disposition workflow must request the corresponding _clean gate.
        if self.state and self.state.is_met(crit_key):
            return self._replay_done_verdict(crit_key)

        # Run single-focus review
        overall_start = time.monotonic()
        issues, output_lines = self._run_single_review()

        if issues is None:
            return McpToolResult(
                exit_code=EXIT_ERROR,
                report_text="Review agent invocation failed",
            )

        elapsed = time.monotonic() - overall_start
        return self._build_result(issues, output_lines, elapsed=elapsed)

    # --- _clean mode ---

    def _run_clean_mode(self, crit_key: str) -> McpToolResult:  # noqa: PLR0911
        """Drive the _clean criterion through initial review → verify loop."""
        # Already met — nothing to do
        if self.state and self.state.is_met(crit_key):
            msg = (
                f"{crit_key} already met for the current source fingerprint. "
                "Do not call this reviewer again; proceed with remaining work."
            )
            return McpToolResult(
                exit_code=EXIT_SUCCESS,
                report_text=msg,
                display_lines=[f"SKIPPED: {crit_key} already met (done — do not retry)"],
            )

        prior_detail = self._get_prior_detail(crit_key)
        if prior_detail is not None:
            prior_detail, resolved_count = self._resolve_out_of_diff_pending(
                crit_key,
                prior_detail,
            )
            if resolved_count and not prior_detail.get("pending"):
                msg = (
                    f"{crit_key}: resolved {resolved_count} stale finding(s) outside the "
                    "enforced diff or in conflict with the project's simulation contract."
                )
                return self._build_result_clean_verify(
                    [],
                    [msg],
                    prior_detail,
                    remaining_indices=set(),
                    dispositions={},
                    elapsed=0.0,
                    crit_key=crit_key,
                )

        has_prior_findings = prior_detail is not None and (
            "pending" in prior_detail or "issue_list" in prior_detail
        )
        if not has_prior_findings:
            # Initial review (no prior detail or no findings list yet)
            return self._run_clean_initial(crit_key)

        # Verify mode
        verify_attempts = prior_detail.get("verify_attempts", 0)
        total_cycles = prior_detail.get("total_verify_cycles", 0)

        # Hard cap: consecutive attempts without a coder fix
        if verify_attempts >= 2:
            msg = (
                f"{crit_key}: 2 verify attempts exhausted — unresolved findings remain. "
                "Fix them or propose explicit, justified review waivers; findings are never "
                "waived automatically."
            )
            return McpToolResult(exit_code=EXIT_FAILURE, report_text=msg)

        if total_cycles >= 3:
            msg = (
                f"{crit_key}: 3 review/resolve cycles exhausted — unresolved findings "
                "remain. Blocking without creating an automatic waiver."
            )
            return McpToolResult(exit_code=EXIT_FAILURE, report_text=msg)

        self.emit_progress("verify mode: checking if prior findings are resolved")
        overall_start = time.monotonic()
        remaining, output_lines, remaining_indices, dispositions = self._run_verify_review(
            prior_detail
        )
        if remaining is None:
            return McpToolResult(
                exit_code=EXIT_ERROR,
                report_text="Verify review agent invocation failed",
            )
        elapsed = time.monotonic() - overall_start
        return self._build_result_clean_verify(
            remaining,
            output_lines,
            prior_detail,
            remaining_indices=remaining_indices,
            dispositions=dispositions,
            elapsed=elapsed,
            crit_key=crit_key,
        )

    def _resolve_out_of_diff_pending(
        self,
        crit_key: str,
        prior_detail: dict[str, Any],
    ) -> tuple[dict[str, Any], int]:
        """Resolve legacy pending findings outside diff/project policy."""
        policy = self._tb_policy() if self.args.category == "tb" else TbProjectPolicy()
        if (
            self._review_diff is None
            and not policy.has_custom_sentinels
            and not policy.trace_files
        ):
            return prior_detail, 0
        source = list(prior_detail.get("pending") or prior_detail.get("issue_list", []))
        kept: list[dict[str, Any]] = []
        resolved_now: list[dict[str, Any]] = []
        for finding in source:
            in_diff = self._review_diff is None or self._review_diff.contains(
                str(finding.get("file", "")), int(finding.get("line", 0)), Path(self.args.work_dir)
            )
            policy_conflict = (
                bool(policy.trace_files) and _issue_rejects_tb_owned_trace(finding)
            ) or (policy.has_custom_sentinels and _issue_requires_builtin_sentinel(finding))
            if in_diff and not policy_conflict:
                kept.append(finding)
            else:
                entry = dict(finding)
                entry["status"] = "excluded"
                entry["exclusion_reason"] = (
                    "conflicts with the configured project simulation contract"
                    if policy_conflict
                    else "outside the enforced diff scope"
                )
                entry["disposition_actor"] = "harness_policy"
                resolved_now.append(entry)
        if not resolved_now:
            return prior_detail, 0
        detail = dict(prior_detail)
        detail["review_detail_version"] = REVIEW_DETAIL_VERSION
        detail["pending"] = kept
        detail["resolved"] = list(detail.get("resolved", [])) + resolved_now
        detail.pop("issue_list", None)
        detail["issues"] = len(kept)
        counts = count_by_severity([ReviewIssue.from_dict(item) for item in kept])
        detail.update(counts)
        # Keep the gate unmet until the caller completes final-state
        # rediscovery. Persisting a passing fingerprint here would let the
        # early exclusion path accept changed source that was never reviewed.
        self.set_criterion(crit_key, False, detail=detail)
        return detail, len(resolved_now)

    def _run_clean_initial(self, crit_key: str) -> McpToolResult:
        """Run the first (no prior findings) review pass for _clean mode."""
        overall_start = time.monotonic()
        issues, output_lines = self._run_single_review()
        if issues is None:
            return McpToolResult(
                exit_code=EXIT_ERROR,
                report_text="Review agent invocation failed",
            )
        elapsed = time.monotonic() - overall_start
        return self._build_result_clean_initial(
            issues,
            output_lines,
            elapsed=elapsed,
            crit_key=crit_key,
        )

    def _review_source_digest(self) -> str:
        """Return the current digest for this review's RTL or TB category."""
        try:
            fingerprint = compute_source_fingerprint(Path(self.args.work_dir))
        except OSError:
            logger.warning("Could not fingerprint sources for review freshness", exc_info=True)
            return ""
        category = "rtl" if self.args.category == "rtl" else "tb"
        value = fingerprint.get(category, {})
        return str(value.get("digest", "")) if isinstance(value, dict) else ""

    def _rediscover_after_source_change(
        self,
        prior_detail: dict[str, Any],
        pending: list[dict[str, Any]],
    ) -> tuple[list[ReviewIssue] | None, list[str], str] | None:
        """Run a fresh discovery pass after fixes changed reviewed sources."""
        if pending:
            return None
        current = self._review_source_digest()
        previous = str(prior_detail.get("review_source_digest", ""))
        # Legacy in-flight state predates discovery fingerprints. Preserve its
        # existing targeted-verify behavior; every newly-created clean review
        # records the digest and receives the final discovery guarantee.
        if not previous or (current and previous == current):
            return None
        self.emit_progress("final discovery: source changed after review findings")
        issues, lines = self._run_single_review()
        return issues, lines, current

    def _replay_done_verdict(self, crit_key: str) -> McpToolResult:
        """Re-report an already-completed _done review instead of re-running it.

        Reviews are idempotent while their source fingerprint is current:
        re-reviewing burns a creator round for a verdict that cannot change. That is a
        *policy* decision, not a Specialist failure, so this is a benign idempotent
        outcome (exit 0) rather than the exit-2 reserved for "the Specialist could
        not run". The prior verdict is replayed verbatim from criterion state
        so a caller that re-invokes after further edits still gets the answer
        it was looking for, plus an explicit note that no new review ran.
        """
        prior = self._get_prior_detail(crit_key) or {}
        issues = [ReviewIssue.from_dict(d) for d in prior.get("issue_list", [])]
        counts = count_by_severity(issues)
        gate_passed = bool(prior.get("gate_passed", check_gate(counts)))
        _status, count_str = _format_status_and_counts(counts, gate_passed)
        outcome = "REVIEWED WITH FINDINGS" if issues else "REVIEWED — NO FINDINGS"

        lines = [
            f"{crit_key} already completed for the current source, so this call "
            "did NOT re-run the review.",
            "Replaying the recorded verdict verbatim:",
            f"\nRESULT: {outcome} ({count_str})",
        ]
        lines += [f"  {_format_issue_line(iss)}" for iss in issues]
        lines.append(
            "\nThis review is already completed for the current source. Calling "
            "`reviewer` again only replays this verdict while the reviewed source remains "
            "current. A later source edit makes the criterion stale and requires "
            "a new final review. Use a `_clean` review criterion when findings "
            "must be fixed or explicitly waived and re-verified."
        )
        report_text = "\n".join(lines)
        print(report_text)

        return McpToolResult(
            exit_code=EXIT_SUCCESS,
            criterion_key=crit_key,
            criterion_met=True,
            detail=dict(prior),
            display_lines=[
                f"SKIPPED: {crit_key} already completed for current source — prior verdict replayed"
            ],
            report_text=report_text,
        )

    def _get_prior_detail(self, crit_key: str) -> dict[str, Any] | None:
        """Retrieve the detail dict for a criterion from state."""
        if not self.state:
            return None
        entries = self.state._resolve_entries(crit_key)
        if not entries:
            return None
        return entries[0].detail

    # --- Verify review ---

    def _run_verify_review(
        self,
        prior_detail: dict[str, Any],
    ) -> tuple[
        list[ReviewIssue] | None,
        list[str],
        set[int],
        dict[int, dict[str, str]],
    ]:
        """Run a verify review checking whether prior findings are fixed.

        Resumes the original review session when a persisted session_id is
        available, so the agent retains context about what it flagged.

        Returns (remaining_issues, output_lines, remaining_indices) where
        remaining_indices are 1-based positions into the original issue_list.
        """
        focus = next(iter(self._parse_focus()))
        # The "no --report-dir, nothing persisted" notice lives in McpTool._post_run:
        # the gap is every endpoint's, not the reviewer's (SETUP-F-39).
        output_lines = [f"[review-verify] {self.args.category}/{focus}"]

        # Strip opposite-category detail from booley_state.json for the
        # duration of the agent run (see workspace_isolation comments).
        state_filter = filter_state_file_for_category(
            getattr(self.args, "state_file", None),
            self.args.category,
        )

        session_key = f"reviewer-{self.args.category}-{focus}"
        prior_sid = self._load_session_id(session_key)

        start = time.monotonic()
        prompt = self._build_verify_prompt(focus, prior_detail, resumed=prior_sid is not None)
        system_prompt = self._build_verify_system_prompt(focus)
        model = self._resolve_model()
        effort = self._resolve_effort()
        transcript = self._transcript_path()
        params = AgentCallParams(
            prompt=prompt,
            model=model,
            cwd=self.args.work_dir,
            allowed_agent_capabilities=self.agent_capabilities,
            # The verify contract is the ``{"findings": [...]}`` text schema
            # (index/status/evidence). ReportFindings cannot express a
            # per-index FIXED/STILL_PRESENT status, so deny it here and force
            # the agent onto the text schema (unlike the initial review, which
            # captures ReportFindings instead).
            disallowed_agent_capabilities=[*self.READ_ONLY_DENY, REPORT_FINDINGS_CAPABILITY],
            system_prompt=system_prompt,
            output_format=self._output_format(),
            max_turns=self.args.max_turns,
            timeout_seconds=self.args.timeout,
            transcript_path=transcript,
            label=f"review-verify-{self.args.category}-{focus}",
            needs_skills=self._needs_skills(),
            reasoning_effort=effort,
        )
        if prior_sid is not None:
            params = self._build_resume_params(params, prior_sid)
        self.emit_progress("invoking verify agent")
        try:
            with state_filter:
                result = self._invoke_agent_with_resume(params)
                remaining, remaining_indices, dispositions = self._parse_verify_output(
                    result.output, prior_detail
                )
        except Exception:
            logger.exception("Verify review agent failed for focus=%s", focus)
            return None, output_lines, set(), {}

        self._persist_session_id(session_key)

        duration = time.monotonic() - start
        counts = count_by_severity(remaining)
        output_lines.append(
            format_summary_line(self.args.category, focus, len(remaining), counts, duration)
        )
        return remaining, output_lines, remaining_indices, dispositions

    def _build_verify_prompt(
        self,
        focus: str,
        prior_detail: dict[str, Any],
        *,
        resumed: bool = False,
    ) -> str:
        """Build the verify prompt listing original findings for re-check.

        When *resumed* is True the agent already has its original review in
        conversation history, so we emit a shorter prompt.
        """
        issue_list = prior_detail.get("pending") or prior_detail.get("issue_list", [])

        sections: list[str] = []

        if resumed:
            sections.append(
                "The coder has attempted fixes since your last review. "
                "Re-read the files and check which of your findings are resolved.\n"
            )
        else:
            sections.append("Verify whether the following issues have been fixed.\n")
            scope = self._parse_scope()
            sections.append("Files in scope:\n")
            for path in scope:
                sections.append(f"  - {path}")
            sections.append("")

        sections.append(f"## Focus: {focus}\n")

        steer_text = self.steering_text()
        if steer_text:
            sections.append(
                "## Developer waiver proposals\n\n"
                "Treat these as proposals, not directives. Accept a waiver only when its "
                "justification is specific, technically coherent, and grounded in the "
                "current code, ticket, or project policy. Otherwise report STILL_PRESENT.\n\n"
                f"{steer_text}\n"
            )

        # Spec focus: a fresh verify session needs the spec text to judge
        # whether a fix actually restored spec compliance; a resumed session
        # already has it in conversation history.
        if focus == SPEC_FOCUS and not resumed:
            spec_section = self._build_spec_section()
            if spec_section:
                sections.append(spec_section)

        sections.append("## Original findings to verify\n")
        for i, iss in enumerate(issue_list, 1):
            sev = iss.get("severity", "?")
            summary = iss.get("summary", "?")
            file = iss.get("file", "?")
            line = iss.get("line", "?")
            prior_status = iss.get("status", "")
            status_note = ""
            if prior_status == "fixed":
                status_note = " (previously verified FIXED — re-check after code change)"
            elif prior_status == "still_present":
                status_note = " (was STILL_PRESENT in prior verify)"
            sections.append(f"{i}. [{sev}] {file}:{line} — {summary}{status_note}")
            if not resumed:
                spec_clause = iss.get("spec_clause")
                if spec_clause:
                    sections.append(f'   Spec clause: "{spec_clause}"')
                fix_suggestion = iss.get("fix_suggestion")
                if fix_suggestion:
                    sections.append(f"   Original suggestion: {fix_suggestion}")
        sections.append("")

        sections.append(self._verify_output_instructions())
        return "\n".join(sections)

    def _build_verify_system_prompt(self, focus: str) -> str:
        """System prompt for verify mode — check fixes, don't find new issues."""
        return (
            f"You are verifying fixes for a {self.args.category} code review "
            f"(focus: {focus}).\n\n"
            "Your ONLY job is to check whether each original finding has been "
            "fixed. Do NOT report new issues. For each finding, read the "
            "relevant code and determine: FIXED, WAIVED, or STILL_PRESENT. WAIVED "
            "means the issue intentionally remains and a concrete justification is "
            "accepted for user review; it is not a source-code or linter waiver.\n\n"
            "IMPORTANT: You MUST report a status for EVERY listed finding, "
            "not just the ones you think changed. Omitted findings keep "
            "their prior status, which may not reflect reality.\n\n"
            "EVIDENCE REQUIRED: Every FIXED status MUST include an "
            "``evidence`` field naming the exact file:line and a one-line "
            "justification anchored in the current code. A FIXED claim "
            "without concrete evidence will be rejected and treated as "
            "STILL_PRESENT — do not rubber-stamp.\n\n"
            "JUSTIFICATION REQUIRED: Every WAIVED status MUST include a specific "
            "``justification`` grounded in the current code, ticket, or project "
            "policy. Every accepted waiver is persisted and shown to the user, "
            "regardless of severity. Reject vague convenience claims as "
            "STILL_PRESENT.\n\n"
            "Use Read, Grep, and Glob to inspect the current code."
        )

    @staticmethod
    def _verify_output_instructions() -> str:
        """Output format for verify review."""
        return """\
## Output Format (STRICT SCHEMA — malformed findings are rejected)

You MUST report a status for EVERY original finding — omitting one
means it keeps its prior status, which may not be what you intend.

Per-finding schema (all fields required unless noted):
  - index:    1-based positive integer into the original findings list
  - status:   "FIXED" | "WAIVED" | "STILL_PRESENT"   (exact, uppercase)
  - evidence: REQUIRED when status = "FIXED"; format
              "<file:line> — <one-line justification>" anchored in code
              you actually read. Omit when status = "STILL_PRESENT".
  - justification: REQUIRED when status = "WAIVED"; explain specifically why
                   leaving the finding in place is acceptable. This text is
                   persisted and shown to the user regardless of severity.

```json
{
  "findings": [
    {
      "index": 1,
      "status": "FIXED",
      "evidence": "rtl/mod_a.sv:42 — renamed clk_i to clk in port list"
    },
    {
      "index": 2,
      "status": "WAIVED",
      "justification": "The ticket deliberately preserves this externally visible timing"
    }
  ]
}
```

Schema enforcement (applied upstream by the harness):
  - FIXED without a non-blank ``evidence`` string is demoted to
    STILL_PRESENT. No rubber-stamping.
  - WAIVED without a non-blank ``justification`` string is demoted to
    STILL_PRESENT. Every waiver is user-visible.
  - Bad index, unknown status, or wrong field types drop the finding;
    the affected original issue falls back to STILL_PRESENT.
  - Malformed JSON or a missing ``findings`` wrapper causes every
    original finding to be treated as STILL_PRESENT.
"""

    def _parse_verify_output(
        self,
        output: str,
        prior_detail: dict[str, Any],
    ) -> tuple[list[ReviewIssue], set[int], dict[int, dict[str, str]]]:
        """Return open issues, their indices, and validated dispositions.

        Strict-schema parser: each finding is validated against
        :func:`_validate_finding_dict`. Findings that fail validation
        are handled two ways depending on the failure mode:

        - FIXED-without-evidence or WAIVED-without-justification is demoted
          to STILL_PRESENT (refuses to rubber-stamp).
        - Any other schema violation (bad index, unknown status, wrong
          type) drops the finding entirely; the affected original issue
          falls back to its prior status (STILL_PRESENT by default).

        Malformed JSON or a missing ``findings`` wrapper is logged and
        treated as "no explicit statuses" — every still-open issue
        defaults to STILL_PRESENT (fail-closed).
        """
        # ``pending`` is the new field; fall back to legacy ``issue_list``
        # so in-flight tickets stay on the rails through a restart.
        issue_list = prior_detail.get("pending") or prior_detail.get("issue_list", [])

        dispositions = self._extract_verify_dispositions(output)

        # For issues the model didn't mention, fall back to prior status.
        # Issues previously verified as "fixed" stay fixed unless the model
        # explicitly says STILL_PRESENT; issues without prior status (first
        # verify pass) default to STILL_PRESENT for safety.
        remaining: list[ReviewIssue] = []
        remaining_indices: set[int] = set()
        for i, iss_dict in enumerate(issue_list, 1):
            if i in dispositions:
                if dispositions[i]["status"] == VERIFY_STATUS_STILL_PRESENT:
                    if (
                        self.args.category == "tb"
                        and _issue_claims_tb_dump_call(iss_dict)
                        and _source_has_tb_dump_call(
                            self.args.work_dir,
                            str(iss_dict.get("file", "")),
                        )
                        is False
                    ):
                        logger.warning(
                            "Treating stale dump-call verify finding as fixed: %s",
                            iss_dict.get("file", ""),
                        )
                        continue
                    remaining.append(ReviewIssue.from_dict(iss_dict))
                    remaining_indices.add(i)
            elif iss_dict.get("status") != "fixed":
                remaining.append(ReviewIssue.from_dict(iss_dict))
                remaining_indices.add(i)
        return remaining, remaining_indices, dispositions

    @staticmethod
    def _extract_verify_dispositions(output: str) -> dict[int, dict[str, str]]:
        """Pull validated per-index dispositions out of agent output.

        Applies the strict finding schema (see ``_validate_finding_dict``)
        at the parsing boundary. Schema violations are logged and
        either demoted (FIXED-without-evidence → STILL_PRESENT) or
        dropped (everything else).
        """
        data = _find_balanced_json_object(output, "findings")
        if data is None:
            logger.warning(
                "Verify agent output had no recognizable findings JSON "
                "wrapper — every original finding will be treated as "
                "STILL_PRESENT (fail-closed)",
            )
            return {}

        dispositions: dict[int, dict[str, str]] = {}
        for ord_idx, finding in enumerate(data.get("findings", []), 1):
            errs = _validate_finding_dict(finding)
            if errs:
                # Missing FIXED/WAIVED support is a rubber-stamp attempt. Demote
                # the targeted index to STILL_PRESENT rather than drop
                # the finding (otherwise a previously-fixed item would
                # silently stay fixed).
                idx = finding.get("index") if isinstance(finding, dict) else None
                only_support_missing = (
                    len(errs) == 1
                    and ("evidence" in errs[0] or "justification" in errs[0])
                    and isinstance(idx, int)
                    and idx >= 1
                )
                if only_support_missing:
                    logger.warning(
                        "Verify finding #%d (idx=%d) lacks required disposition "
                        "support — demoting to STILL_PRESENT",
                        ord_idx,
                        idx,
                    )
                    dispositions[idx] = {"status": VERIFY_STATUS_STILL_PRESENT}
                    continue
                logger.warning(
                    "Rejecting verify finding #%d due to schema violations: %s",
                    ord_idx,
                    "; ".join(errs),
                )
                continue
            disposition = {"status": finding["status"].upper()}
            for field in ("evidence", "justification"):
                value = finding.get(field)
                if isinstance(value, str) and value.strip():
                    disposition[field] = value.strip()
            dispositions[finding["index"]] = disposition
        return dispositions

    @staticmethod
    def _extract_verify_statuses(output: str) -> dict[int, str]:
        """Backward-compatible status-only view used by older callers/tests."""
        return {
            index: disposition["status"]
            for index, disposition in ReviewerSpecialist._extract_verify_dispositions(
                output
            ).items()
        }

    def _capability_channel_issues(
        self,
        result: Any,
        focus: str,
        output_lines: list[str],
    ) -> list[ReviewIssue] | None:
        """Map captured ``ReportFindings`` calls onto issues.

        Returns ``None`` when the agent never called the capability (channel
        absent), which is different from an empty call (channel present,
        zero findings).
        """
        rf_map = getattr(result, "captured_agent_capability_calls", None)
        rf_calls = rf_map.get(REPORT_FINDINGS_CAPABILITY) if isinstance(rf_map, dict) else None
        if not isinstance(rf_calls, list):
            return None
        findings: list[Any] = []
        for call in rf_calls:
            raw = call.get("findings", [])
            if isinstance(raw, list):
                findings.extend(raw)
        issues, dropped = report_findings_to_issues(findings, focus)
        if dropped:
            output_lines.append(
                f"WARN: dropped {dropped} malformed {REPORT_FINDINGS_CAPABILITY} "
                "entr" + ("y" if dropped == 1 else "ies") + " (missing file/summary)",
            )
        logger.info(
            "Review agent for %s/%s reported %d finding(s) via %s",
            self.args.category,
            focus,
            len(issues),
            REPORT_FINDINGS_CAPABILITY,
        )
        return issues

    def _extract_review_issues(
        self,
        result: Any,
        focus: str,
        output_lines: list[str],
    ) -> list[ReviewIssue] | None:
        """Turn a review agent result into issues, honoring both contracts.

        Claude-backend agents report through the native ``ReportFindings``
        capability (captured via ``capture_agent_capability_calls``); Codex agents — and Claude
        agents that print the JSON instead — use the ``{"issues": [...]}`` text
        contract. Both channels are read and the *more severe* verdict wins
        (see ``_channel_severity_rank``).

        Trusting the agent-capability channel alone produced a false PASS on a planted
        CRITICAL (SETUP-F-33): the agent described the bug in a full issue
        JSON in its final text but had also called ``ReportFindings`` once
        with ``{"findings": []}``, and an empty call used to short-circuit
        the text parse into "0 issues, gate passed". An empty agent-capability call is
        no longer evidence of a clean review — only agreement between the
        channels is.

        Returns ``None`` only when *neither* contract yielded a
        recognizable result (treated as a Specialist error, not a clean pass).
        """
        capability_issues = self._capability_channel_issues(result, focus, output_lines)
        if capability_issues is None:
            return self._interpret_review_parse(result.output, focus, output_lines)

        text_parsed = parse_review_output(result.output, allowed_category=focus)
        if not text_parsed.json_present:
            # Agent-capability channel is the only contract the agent honored. Non-empty
            # is usable; empty + no JSON is an agent that reported nothing at
            # all through either channel, which must not read as a clean pass.
            if capability_issues:
                return capability_issues
            logger.error(
                "Review agent for %s/%s called %s with zero findings and "
                "emitted no issues JSON — refusing to treat as a clean review",
                self.args.category,
                focus,
                REPORT_FINDINGS_CAPABILITY,
            )
            output_lines.append(
                f"ERROR: empty {REPORT_FINDINGS_CAPABILITY} call and no issues JSON "
                "in the agent's final message — not a clean pass",
            )
            return None

        if self._note_rejected_issues(text_parsed, focus, output_lines):
            return self._resolve_unusable_text_channel(capability_issues, focus, output_lines)
        return self._pick_review_channel(
            capability_issues, text_parsed.issues, focus, output_lines
        )

    def _resolve_unusable_text_channel(
        self,
        capability_issues: list[ReviewIssue],
        focus: str,
        output_lines: list[str],
    ) -> list[ReviewIssue] | None:
        """Decide the verdict when every text-channel issue died on the schema.

        Such a channel is *unknown*, not clean, so it must never be outvoted
        into a PASS. It used to be, whenever the agent-capability channel said anything at
        all (``... and not capability_issues``): ReportFindings severities cap at
        MAJOR/MINOR, so one ``PLAUSIBLE`` cosmetic nit mapped to MINOR, won the
        channel pick, and reported ``gate_passed: true`` in the same output
        that said every reported issue had been thrown away — with the agent's
        CRITICAL among them (residual SETUP-F-33).

        Returning issues is only allowed when they already fail the gate: a
        concrete FAIL is more actionable than a bare Specialist error and cannot be
        the false PASS this guards against. Everything else is a Specialist error.
        """
        if capability_issues and not check_gate(count_by_severity(capability_issues)):
            output_lines.append(
                f"WARN: text channel unusable (all entries schema-rejected) — reporting the "
                f"{len(capability_issues)} {REPORT_FINDINGS_CAPABILITY} issue(s), which already fail the gate",
            )
            return capability_issues
        logger.error(
            "Review agent for %s/%s: text channel entirely schema-rejected and the %s "
            "channel (%d issue(s)) does not block — refusing to report a clean pass",
            self.args.category,
            focus,
            REPORT_FINDINGS_CAPABILITY,
            len(capability_issues),
        )
        return None

    def _note_rejected_issues(
        self,
        parsed: ReviewParseResult,
        focus: str,
        output_lines: list[str],
    ) -> bool:
        """Report schema-rejected issues; True when *every* reported issue died.

        "The agent reported issues and the harness threw all of them away"
        must not render as a clean review — that is a false PASS with extra
        steps (same class as SETUP-F-33). The caller turns it into a Specialist
        error; a partial rejection is just a warning.
        """
        if not parsed.rejected:
            return False
        count = len(parsed.rejected)
        plural = "y" if count == 1 else "ies"
        if parsed.issues:
            output_lines.append(f"WARN: rejected {count} malformed issue entr{plural} (see logs)")
            return False
        logger.error(
            "Review agent for %s/%s: all %d reported issue(s) failed the schema — "
            "refusing to treat as a clean review",
            self.args.category,
            focus,
            count,
        )
        output_lines.append(
            f"ERROR: all {count} reported issue entr{plural} failed the schema "
            "(see logs) — not a clean pass",
        )
        return True

    def _pick_review_channel(
        self,
        capability_issues: list[ReviewIssue],
        text_issues: list[ReviewIssue],
        focus: str,
        output_lines: list[str],
    ) -> list[ReviewIssue]:
        """Resolve a agent-capability-channel / text-channel disagreement, fail-closed.

        The two channels are two renderings of the same review, so they are
        not merged (that would double-count every issue reported twice).
        Instead the channel with the more severe verdict wins — a
        disagreement can only ever make the gate stricter, never turn a
        reported defect into a PASS. Ties go to the agent-capability channel, which
        carries the agent's structured fields.
        """
        capability_rank = _channel_severity_rank(capability_issues)
        text_rank = _channel_severity_rank(text_issues)
        if text_rank <= capability_rank:
            return capability_issues
        logger.error(
            "Review channel disagreement for %s/%s: %s reported %d issue(s) "
            "but the text JSON reported %d — taking the text channel (fail-closed)",
            self.args.category,
            focus,
            REPORT_FINDINGS_CAPABILITY,
            len(capability_issues),
            len(text_issues),
        )
        output_lines.append(
            f"WARN: {REPORT_FINDINGS_CAPABILITY} reported {len(capability_issues)} issue(s) but the "
            f"text JSON reported {len(text_issues)} — using the more severe text channel",
        )
        return text_issues

    def _interpret_review_parse(
        self,
        output: str,
        focus: str,
        output_lines: list[str],
    ) -> list[ReviewIssue] | None:
        """Parse agent output for a review focus.

        Appends any warning/error lines to ``output_lines`` and returns the
        parsed issues, or ``None`` when no JSON wrapper was present (treated
        as a Specialist error rather than an implicit clean pass).
        """
        parsed = parse_review_output(output, allowed_category=focus)
        if not parsed.json_present:
            logger.error(
                "Review agent for %s/%s emitted no recognizable "
                "issues JSON wrapper — refusing to treat as a "
                "clean review",
                self.args.category,
                focus,
            )
            output_lines.append(
                "ERROR: agent output contained no parseable issues JSON — not a clean pass",
            )
            return None
        if self._note_rejected_issues(parsed, focus, output_lines):
            return None
        return parsed.issues

    def _run_single_review(self) -> tuple[list[ReviewIssue] | None, list[str]]:
        """Run exactly one focus review. Returns (issues, output_lines) or (None, lines) on error.

        Treats "agent emitted no JSON wrapper at all" as a Specialist error
        rather than an implicit clean pass — otherwise a stream of
        free-form prose would slip through as ``0 issues``.
        """
        focus = next(iter(self._parse_focus()))
        output_lines = [f"[review] {self.args.category}/{focus}"]

        # Strip opposite-category detail from booley_state.json for the
        # duration of the agent run (see workspace_isolation comments).
        state_filter = filter_state_file_for_category(
            getattr(self.args, "state_file", None),
            self.args.category,
        )

        start = time.monotonic()
        prompt = self._build_prompt(focus_override=focus)
        system_prompt = self._build_system_prompt(focus)
        model = self._resolve_model()
        effort = self._resolve_effort()
        transcript = self._transcript_path()
        self.emit_progress(f"invoking review agent ({self.args.category}/{focus})")
        try:
            with state_filter:
                result = self._invoke_agent(
                    AgentCallParams(
                        prompt=prompt,
                        model=model,
                        cwd=self.args.work_dir,
                        # Explicitly admit ReportFindings for the initial review so
                        # the agent's native review contract is available (and then
                        # captured below), rather than relying on the SDK leaking it
                        # past the allowlist.
                        allowed_agent_capabilities=[
                            *self.agent_capabilities,
                            REPORT_FINDINGS_CAPABILITY,
                        ],
                        # Hard deny (survives bypassPermissions) — see
                        # READ_ONLY_DENY. Category deny patterns stay in
                        # workspace_isolation; this is the read-only half.
                        disallowed_agent_capabilities=list(self.READ_ONLY_DENY),
                        system_prompt=system_prompt,
                        output_format=self._output_format(),
                        capture_agent_capability_calls=[REPORT_FINDINGS_CAPABILITY],
                        max_turns=self.args.max_turns,
                        timeout_seconds=self.args.timeout,
                        transcript_path=transcript,
                        label=f"review-{self.args.category}-{focus}",
                        needs_skills=self._needs_skills(),
                        reasoning_effort=effort,
                    )
                )
                issues = self._extract_review_issues(
                    result,
                    focus,
                    output_lines,
                )
                if issues is None:
                    return None, output_lines
                issues = self._filter_review_issues(issues, output_lines)
                self.emit_progress(f"review complete: {len(issues)} finding(s)")
        except Exception:
            logger.exception("Review agent failed for focus=%s", focus)
            return None, output_lines

        self._persist_session_id(f"reviewer-{self.args.category}-{focus}")

        duration = time.monotonic() - start
        counts = count_by_severity(issues)
        output_lines.append(
            format_summary_line(self.args.category, focus, len(issues), counts, duration)
        )
        return issues, output_lines

    def _filter_review_issues(
        self,
        issues: list[ReviewIssue],
        output_lines: list[str],
    ) -> list[ReviewIssue]:
        """Enforce diff and project-policy boundaries on agent findings."""
        kept: list[ReviewIssue] = []
        diff_dropped = 0
        policy_dropped = 0
        work_dir = Path(self.args.work_dir)
        policy = self._tb_policy() if self.args.category == "tb" else TbProjectPolicy()
        for issue in issues:
            issue_dict = issue.to_dict()
            if self._review_diff is not None and not self._review_diff.contains(
                issue.file,
                issue.line,
                work_dir,
            ):
                diff_dropped += 1
                continue
            if policy.trace_files and _issue_rejects_tb_owned_trace(issue_dict):
                policy_dropped += 1
                continue
            if policy.has_custom_sentinels and _issue_requires_builtin_sentinel(issue_dict):
                policy_dropped += 1
                continue
            kept.append(issue)
        if diff_dropped:
            output_lines.append(
                f"INFO: ignored {diff_dropped} finding(s) on unchanged baseline lines "
                f"outside --diff-ref {self.args.diff_ref}"
            )
        if policy_dropped:
            output_lines.append(
                f"INFO: ignored {policy_dropped} finding(s) that conflict with the "
                "project's configured sentinel/trace contract"
            )
        return kept

    def _build_result(
        self,
        all_issues: list[ReviewIssue],
        output_lines: list[str],
        *,
        elapsed: float,
    ) -> McpToolResult:
        """Compute gate, set criterion, and format display lines (_done mode)."""
        counts = count_by_severity(all_issues)
        gate_passed = check_gate(counts)

        _status, count_str = _format_status_and_counts(counts, gate_passed)
        outcome = "REVIEWED WITH FINDINGS" if all_issues else "REVIEWED — NO FINDINGS"
        result_line = f"\nRESULT: {outcome} ({count_str})"
        output_lines.append(result_line)

        report_text = "\n".join(output_lines)
        # Print concise result summary to stdout (consumed by callers and tests)
        print(report_text)

        crit_key = self._criterion_key()
        # _done means that a valid review completed. Keep the independent
        # cleanliness verdict in detail for reporting; _clean is the criterion
        # that blocks until MAJOR/CRITICAL findings are resolved.
        detail = {
            "review_detail_version": REVIEW_DETAIL_VERSION,
            "issues": len(all_issues),
            "issue_list": [_finding_record(iss) for iss in all_issues],
            **counts,
            "elapsed_s": round(elapsed, 1),
            "gate_passed": gate_passed,
            "review_outcome": "findings" if all_issues else "no_findings",
        }
        self.set_criterion(crit_key, True, detail=detail)

        lines = [f"{count_str}, review completed"]
        for issue in all_issues:
            lines.append(f"  {_format_issue_line(issue)}")

        return McpToolResult(
            exit_code=EXIT_SUCCESS,
            criterion_key=crit_key,
            criterion_met=True,
            display_lines=lines,
            detail=detail,
            report_text=report_text,
        )

    # --- _clean mode result builders ---

    def _build_result_clean_initial(
        self,
        issues: list[ReviewIssue],
        output_lines: list[str],
        *,
        elapsed: float,
        crit_key: str,
    ) -> McpToolResult:
        """Build result for initial _clean review. Met immediately if gate passes.

        Detail uses the ``pending`` / ``resolved`` split: on the initial
        pass every finding is pending (nothing has been verified fixed
        yet) and ``resolved`` is empty.
        """
        counts = count_by_severity(issues)
        gate_passed = not issues

        status, count_str = _format_status_and_counts(counts, gate_passed)
        output_lines.append(f"\nRESULT: {status} ({count_str})")

        report_text = "\n".join(output_lines)
        # Print concise result summary to stdout (consumed by callers and tests)
        print(report_text)

        detail: dict[str, Any] = {
            "review_detail_version": REVIEW_DETAIL_VERSION,
            "issues": len(issues),
            "pending": [_finding_record(iss) for iss in issues],
            "resolved": [],
            **counts,
            "elapsed_s": round(elapsed, 1),
            "review_source_digest": self._review_source_digest(),
        }

        if gate_passed:
            self.set_criterion(crit_key, True, detail=detail)
        else:
            detail["verify_attempts"] = 0
            detail["original_issues"] = len(issues)
            self.set_criterion(crit_key, False, detail=detail)

        lines = [f"{count_str}, gate {status}"]
        for issue in issues:
            lines.append(f"  {_format_issue_line(issue)}")

        return McpToolResult(
            exit_code=EXIT_SUCCESS if gate_passed else 1,
            criterion_key=crit_key,
            criterion_met=gate_passed,
            display_lines=lines,
            detail=detail,
            report_text=report_text,
        )

    def _build_result_clean_verify(
        self,
        remaining: list[ReviewIssue],
        output_lines: list[str],
        existing_detail: dict[str, Any],
        *,
        remaining_indices: set[int],
        dispositions: dict[int, dict[str, str]],
        elapsed: float,
        crit_key: str,
    ) -> McpToolResult:
        """Build result for verify pass.

        Splits findings into ``pending`` (still_present after this round)
        and ``resolved`` (fixed this round, plus any already-resolved
        carried in from prior cycles). Drops the legacy ``issue_list`` so
        the developer-side ``met=True with pending non-empty`` assertion
        bites.
        """
        pending, resolved, original_issues = self._split_verify_findings(
            existing_detail,
            remaining_indices,
            dispositions,
        )
        source_digest = str(existing_detail.get("review_source_digest", ""))
        rediscovery = self._rediscover_after_source_change(existing_detail, pending)
        if rediscovery is not None:
            discovered, discovery_lines, source_digest = rediscovery
            if discovered is None:
                return McpToolResult(
                    exit_code=EXIT_ERROR,
                    report_text="Final clean-review discovery agent invocation failed",
                )
            output_lines.extend(["", "[review] final discovery after source changes"])
            output_lines.extend(discovery_lines)
            remaining = discovered
            pending = [_finding_record(issue) for issue in discovered]
            original_issues += len(discovered)

        counts = count_by_severity(remaining)
        gate_passed = not pending
        status, count_str = _format_status_and_counts(counts, gate_passed)
        output_lines.append(f"\nVERIFY RESULT: {status} ({count_str})")
        waiver_lines = [
            f"[WAIVED {item.get('severity', '?')}] "
            f"{item.get('file', '?')}:{item.get('line', '?')} — "
            f"{item.get('summary', '?')} — Justification: "
            f"{item.get('justification', '')}"
            for item in resolved
            if item.get("status") == "waived"
        ]
        if waiver_lines:
            output_lines.extend(["", "ACCEPTED WAIVERS (user-visible):", *waiver_lines])

        report_text = "\n".join(output_lines)
        print(report_text)

        verify_attempts = existing_detail.get("verify_attempts", 0) + 1
        total_cycles = existing_detail.get("total_verify_cycles", 0) + 1
        detail: dict[str, Any] = {
            "review_detail_version": REVIEW_DETAIL_VERSION,
            "issues": len(remaining),
            "pending": pending,
            "resolved": resolved,
            **counts,
            "verify_attempts": verify_attempts,
            "total_verify_cycles": total_cycles,
            "original_issues": original_issues,
            "elapsed_s": round(elapsed, 1),
            "review_source_digest": source_digest,
        }

        met = gate_passed
        self.set_criterion(crit_key, met, detail=detail)

        if met:
            focus = next(iter(self._parse_focus()))
            self._clear_session_id(f"reviewer-{self.args.category}-{focus}")

        lines = [f"{count_str}, verify {status} (attempt {verify_attempts}/2)"]
        lines.extend(f"  {line}" for line in waiver_lines)
        for issue in remaining:
            lines.append(f"  {_format_issue_line(issue)}")

        return McpToolResult(
            exit_code=EXIT_SUCCESS if met else 1,
            criterion_key=crit_key,
            criterion_met=met,
            display_lines=lines,
            detail=detail,
            report_text=report_text,
        )

    def _split_verify_findings(
        self,
        existing_detail: dict[str, Any],
        remaining_indices: set[int],
        dispositions: dict[int, dict[str, str]],
    ) -> tuple[list[dict[str, Any]], list[dict[str, Any]], int]:
        """Split prior open findings into pending vs resolved for a verify pass.

        Returns (pending, resolved, original_issues), where resolved carries
        forward previously-resolved items plus those fixed this cycle.
        """
        # Split current cycle's findings into pending vs resolved using the
        # 1-based remaining_indices from _parse_verify_output. Source list
        # is the prior cycle's open set (``pending``), with fallback to the
        # legacy ``issue_list`` to keep in-flight tickets running.
        prior_pending = existing_detail.get("pending") or existing_detail.get("issue_list", [])
        pending: list[dict[str, Any]] = []
        newly_resolved: list[dict[str, Any]] = []
        for i, iss_dict in enumerate(prior_pending, 1):
            entry = dict(iss_dict)
            if i in remaining_indices:
                entry["status"] = "still_present"
                pending.append(entry)
            else:
                disposition = dispositions.get(i, {"status": VERIFY_STATUS_FIXED})
                if disposition["status"] == VERIFY_STATUS_WAIVED:
                    entry["status"] = "waived"
                    entry["justification"] = disposition["justification"]
                else:
                    entry["status"] = "fixed"
                    if evidence := disposition.get("evidence"):
                        entry["evidence"] = evidence
                entry["disposition_actor"] = "reviewer_agent"
                newly_resolved.append(entry)

        # Carry forward any previously-resolved items so the resolved
        # history grows monotonically across verify cycles.
        prior_resolved = existing_detail.get("resolved", [])
        resolved = list(prior_resolved) + newly_resolved

        # original_issues anchors on the first pass; prefer the stored
        # value, fall back to pending+resolved length (round-trip safe).
        original_issues = existing_detail.get(
            "original_issues",
            len(pending) + len(newly_resolved),
        )
        return pending, resolved, original_issues

    # --- _interpret_output (required by ABC, but _run is overridden) ---

    def _interpret_output(self, output: str, structured: dict | None) -> McpToolResult:
        """Parse review output (used when called via base Specialist._run)."""
        issues = parse_issues(output)
        counts = count_by_severity(issues)

        crit_key = self._criterion_key()
        return McpToolResult(
            exit_code=EXIT_SUCCESS,
            criterion_key=crit_key,
            criterion_met=True,
            detail={"issues": len(issues), **counts},
        )


if __name__ == "__main__":
    ReviewerSpecialist().cli()
