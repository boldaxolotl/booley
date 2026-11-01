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
import posixpath
import time
from copy import deepcopy
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, ClassVar
from uuid import uuid4

from booley.agent_workspace.state_projection import filter_state_file_for_category
from booley.core.boundary import as_dict, as_int, as_str_list
from booley.core.models import AgentCallParams
from booley.criteria.freshness import review_policy_digest
from booley.criteria.state import DevelopmentState
from booley.evidence.review_receipt import (
    REVIEW_DETAIL_VERSION,
    ReviewInvocation,
    build_review_contract_detail,
)
from booley.evidence.review_vocabulary import (
    ALL_DISPOSITIONS,
    DISPOSITION_ADVISORY,
    DISPOSITION_CURRENT,
    DISPOSITION_DEFERRED,
    DISPOSITION_OUT_OF_SCOPE,
)
from booley.flows.cli_help import CliHelp
from booley.flows.endpoint_diff import read_source_dirs_from_toml
from booley.flows.execution_persistence import (
    EvidenceDiscarded,
    done_findings_require_approval_for,
    execution_goal_name,
)
from booley.mcp.base import (
    EXIT_ERROR,
    EXIT_FAILURE,
    EXIT_SUCCESS,
    McpToolResult,
)
from booley.runtime.booley_dir import resolve_booley_dir, resolve_checkout_booley_dir
from booley.runtime.exception_diagnostics import exception_report_text, log_exception
from booley.runtime.execution_records import atomic_write_json
from booley.runtime.file_lock import LockContentionError, nonblocking_file_lock
from booley.runtime.paths import refs_dir
from booley.specialists.verification_freshness import refresh_verification_freshness
from booley.targets.flow_names import config_section

from .review_contract import ReviewContractError, ReviewScopeContract, resolve_review_scope
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
ALL_ISSUE_KINDS = frozenset({"code_defect", "proof_gap", "spec_ambiguity"})
_NON_CORRECTIVE_DISPOSITIONS = frozenset(
    {DISPOSITION_ADVISORY, DISPOSITION_DEFERRED, DISPOSITION_OUT_OF_SCOPE}
)

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


# Source suffixes whose presence in a review scope but absence from any
# ``.core`` fileset is worth surfacing (ADR 0026 follow-through).
_SOURCE_SUFFIXES = (
    ".v",
    ".sv",
    ".svh",
    ".vh",
    ".py",
    ".c",
    ".cc",
    ".cpp",
    ".cxx",
    ".h",
    ".hh",
    ".hpp",
    ".hxx",
)


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
    "rtl": ".booley/rtl_style_guide.md",
    "tb": ".booley/tb_style_guide.md",
}


# TB checks that evaluate existing scenarios separately from demands for new
# stimulus. False-pass checks always apply: broken sentinels and dead comparisons
# remain defects regardless of the requested change.

# TB checklist items that demand *new* stimulus rather than sound checking of
# the stimulus already present. Only these are demoted for the types above.
# Named, not numbered: the SV and cocotb guides number their checklists
# independently, so numbers would silently point at the wrong rows.


@dataclass(frozen=True)
class TbProjectPolicy:
    """Project-owned simulation contracts relevant to TB review."""

    pass_sentinels: tuple[str, ...] = ()
    fail_sentinels: tuple[str, ...] = ()
    trace_files: tuple[str, ...] = ()

    @property
    def has_custom_sentinels(self) -> bool:
        return bool(self.pass_sentinels or self.fail_sentinels)


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
# Specification resolution (spec focus, ADR 0038)
# ---------------------------------------------------------------------------

# Max spec content size before truncation (bytes)
_SPEC_MAX_SIZE = 30_000


def _truncate_spec(content: str, *, label: str = "") -> str:
    """Truncate content to _SPEC_MAX_SIZE with a warning if needed."""
    if len(content) <= _SPEC_MAX_SIZE:
        return content
    if label:
        logger.warning("%s is %d bytes; truncating to %d", label, len(content), _SPEC_MAX_SIZE)
    return content[:_SPEC_MAX_SIZE] + "\n\n[SPEC TRUNCATED]"


def resolve_spec_content(
    spec_arg: str | None,
    work_dir: str | Path | None = None,
) -> tuple[str | None, str]:
    """Read the explicit specification relative to the selected worktree."""
    if not spec_arg:
        return None, ""
    spec_path = Path(spec_arg)
    if not spec_path.is_absolute() and work_dir:
        spec_path = Path(work_dir) / spec_path
    if not spec_path.is_file():
        raise OSError(f"Specification file not found: {spec_path}")
    content = spec_path.read_text(encoding="utf-8", errors="replace")
    return _truncate_spec(content, label=str(spec_path)), f"spec file: {spec_path}"


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
    kind: str = ""
    disposition: str = ""
    ticket_clause: str = ""
    proposal_ordinal: int = field(default=0, compare=False)

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
        if self.kind:
            d["kind"] = self.kind
        if self.disposition:
            d["disposition"] = self.disposition
        if self.ticket_clause:
            d["ticket_clause"] = self.ticket_clause
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
            kind=str(d.get("kind", "")),
            disposition=str(d.get("disposition", "")).lower(),
            ticket_clause=str(d.get("ticket_clause", "")),
        )


@dataclass(frozen=True)
class _DoneReviewOutcome:
    issues: list[ReviewIssue]
    records: list[dict[str, Any]]
    corrective_records: list[dict[str, Any]]
    counts: dict[str, int]
    gate_passed: bool


@dataclass
class _CleanVerifyContext:
    remaining: list[ReviewIssue]
    output_lines: list[str]
    existing_detail: dict[str, Any]
    pending: list[dict[str, Any]]
    resolved: list[dict[str, Any]]
    original_issues: int
    source_digest: str
    observations: list[dict[str, Any]]
    elapsed: float
    crit_key: str


def _finding_record(issue: ReviewIssue) -> dict[str, Any]:
    """Return one persisted finding with a stable content-derived identifier."""
    record = issue.to_dict()
    identity_fields = {
        "category": issue.category,
        "file": issue.file.replace("\\", "/"),
        "line": issue.line,
        "kind": issue.kind or "legacy",
        "ticket_clause": " ".join(issue.ticket_clause.split()),
        "summary": " ".join(issue.summary.casefold().split()),
    }
    identity = json.dumps(identity_fields, sort_keys=True, separators=(",", ":"))
    record["finding_id"] = hashlib.sha256(identity.encode()).hexdigest()[:16]
    record["status"] = issue.disposition or DISPOSITION_CURRENT
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
    kind = d.get("kind")
    if not isinstance(kind, str) or kind not in ALL_ISSUE_KINDS:
        errs.append(f"kind must be one of {sorted(ALL_ISSUE_KINDS)} (got {kind!r})")
    disposition = d.get("disposition")
    if not isinstance(disposition, str) or disposition.lower() not in ALL_DISPOSITIONS:
        errs.append(f"disposition must be one of {sorted(ALL_DISPOSITIONS)} (got {disposition!r})")
    ticket_clause = d.get("ticket_clause")
    if not isinstance(ticket_clause, str) or not ticket_clause.strip():
        errs.append("ticket_clause must be a non-empty string")
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
    """Outcome of parsing reviewer Specialist output.

    ``json_present`` distinguishes "agent emitted no JSON wrapper at all"
    (typically free-form prose) from "agent emitted a wrapper with zero
    issues" — the latter is a legitimate clean review, the former is a
    schema violation the harness must not silently accept as a pass.
    """

    issues: list[ReviewIssue]
    json_present: bool
    rejected: list[tuple[int, list[str]]]  # (1-based item ordinal, errors)
    diagnostics: list[dict[str, Any]] = field(default_factory=list)


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
    """Extract the raw items list from Specialist output.

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
    """Strict-schema parser for the initial Reviewer Specialist output.

    Each item is validated against the issue schema. Items that violate
    the schema are dropped with a logged warning rather than coerced
    into placeholder defaults — the agent's output is the contract.
    """
    raw_items, json_present = _extract_issues_payload(output)
    issues: list[ReviewIssue] = []
    rejected: list[tuple[int, list[str]]] = []
    diagnostics: list[dict[str, Any]] = []
    wrapper = _find_balanced_json_object(output, "issues")
    if wrapper is not None and not isinstance(wrapper["issues"], list):
        diagnostics.append(
            {"ordinal": 1, "raw": wrapper["issues"], "errors": ["issues wrapper must be a list"]}
        )
    for ord_idx, item in enumerate(raw_items, 1):
        errs = _validate_issue_dict(item, allowed_category=allowed_category)
        if errs:
            rejected.append((ord_idx, errs))
            diagnostics.append({"ordinal": ord_idx, "raw": item, "errors": errs})
            logger.warning(
                "Rejecting review issue #%d due to schema violations: %s",
                ord_idx,
                "; ".join(errs),
            )
            continue
        issue = ReviewIssue.from_dict(item)
        issue.proposal_ordinal = ord_idx
        issues.append(issue)
    return ReviewParseResult(
        issues=issues,
        json_present=json_present,
        rejected=rejected,
        diagnostics=diagnostics,
    )


# Name of the native Claude Code review-reporting capability. When the review
# sub-agent runs on a Claude backend it reports findings through this capability
# call rather than printing the ``{"issues": [...]}`` text contract, so the
# harness captures the call inputs (see AgentCallParams.capture_agent_capability_calls)
# and this module checks them against the canonical issues JSON.
REPORT_FINDINGS_CAPABILITY = "ReportFindings"


_SEVERITY_ORDER = {SEVERITY_MINOR: 0, SEVERITY_MAJOR: 1, SEVERITY_CRITICAL: 2}


@dataclass(frozen=True)
class MirrorFinding:
    """One ``ReportFindings`` entry, reduced to what the mirror check compares.

    ``severity`` is set only when the agent sent one. The capability's own
    schema has no severity field, but agents add one (seen in recorded
    reviews). Its ``CONFIRMED``/``PLAUSIBLE`` verdict says whether a finding
    was verified, not how severe it is, so it is not used.
    """

    file: str
    line: int
    severity: str | None


def _is_mirror_entry(item: Any) -> bool:
    """True for a ReportFindings entry that names a file and a summary."""
    return (
        isinstance(item, dict)
        and bool(str(item.get("file", "")).strip())
        and bool(str(item.get("summary", "")).strip())
    )


def report_findings_to_mirror(findings: list[dict[str, Any]]) -> list[MirrorFinding]:
    """Map one call's valid ReportFindings entries onto MirrorFindings.

    File paths are normalized (``./a`` == ``a``) so they compare equal to
    the canonical JSON's.
    """
    mirror: list[MirrorFinding] = []
    for f in findings:
        line = f.get("line", 0)
        if not isinstance(line, int) or isinstance(line, bool) or line < 0:
            line = 0
        severity = str(f.get("severity", "")).strip().upper()
        mirror.append(
            MirrorFinding(
                file=posixpath.normpath(str(f["file"]).strip()),
                line=line,
                severity=severity if severity in _SEVERITY_ORDER else None,
            )
        )
    return mirror


def merge_report_calls(calls: list[list[MirrorFinding]]) -> list[MirrorFinding]:
    """Union several ReportFindings calls into one report.

    Agents re-send their findings, or split them across calls. A finding
    from a later call replaces an earlier call's finding at the same
    file:line, so a repeated report counts each finding once (#1963).
    """
    merged: list[MirrorFinding] = []
    for call in calls:
        resent = {(f.file, f.line) for f in call}
        merged = [f for f in merged if (f.file, f.line) not in resent] + call
    return merged


def mirror_findings_missing_from_canonical(
    mirror: list[MirrorFinding],
    canonical: list[ReviewIssue],
) -> list[MirrorFinding]:
    """Return mirror findings the canonical JSON leaves out or rates lower.

    Each mirror finding claims its own canonical issue at the same file and
    line, the most severe one when several share it. Severity is compared
    only when the mirror sent one.
    """
    unclaimed = list(canonical)
    disagreeing: list[MirrorFinding] = []
    for finding in mirror:
        same_place = [
            issue
            for issue in unclaimed
            if (posixpath.normpath(issue.file), issue.line) == (finding.file, finding.line)
        ]
        if not same_place:
            disagreeing.append(finding)
            continue
        match = max(same_place, key=lambda issue: _SEVERITY_ORDER[issue.severity])
        unclaimed.remove(match)
        if (
            finding.severity
            and _SEVERITY_ORDER[finding.severity] > _SEVERITY_ORDER[match.severity]
        ):
            disagreeing.append(finding)
    return disagreeing


def parse_issues(output: str) -> list[ReviewIssue]:
    """Parse issues JSON from Specialist output (schema-validated).

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
    return f"[{tag}] {issue.file}:{issue.line} — {issue.summary} (ID: {_finding_record(issue)['finding_id']})"


def validate_scope_category(
    scope_paths: list[str],
    category: str,
    work_dir: Path | None = None,
    *,
    allow_support: bool = False,
) -> list[str]:
    """Validate scope paths match the review category.

    Returns list of error messages (empty = valid).
    """
    if work_dir is not None:
        from booley.fusesoc.fusesoc_registry import (
            canonical_project_path,
            classified_sources,
            discover_cores,
        )

        if discover_cores(work_dir):
            sources = classified_sources(work_dir, strict=True)
            opposite = set(sources.tb_files if category == "rtl" else sources.rtl_source_files)
            return [
                f"Path '{path}' is an opposite-category source ({category} review)"
                + (
                    "; check .core role metadata; declare tags:[tb] only if this is testbench support"
                    if category == "tb"
                    and Path(path).suffix.lower()
                    in {".c", ".cc", ".cpp", ".cxx", ".h", ".hh", ".hpp", ".hxx"}
                    else ""
                )
                for path in scope_paths
                if canonical_project_path(work_dir, path) in opposite
            ]
    if allow_support:
        return []
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
    cli_help: ClassVar[CliHelp | None] = CliHelp(
        summary=(
            "Have an AI reviewer discover findings in Goal-owned files (Interactive: --scope) with one "
            "review focus and list the issues it finds by severity. Use it for a second "
            "opinion on a change before you merge it. The reviewer reads code and "
            "never edits it. Use --verify to check only the latest review's open findings; only --waive proposals may accompany verification."
        ),
        example="booley specialist reviewer --category rtl --focus bugs --scope rtl/fifo.sv",
    )
    code_modifying: bool = False
    config_aware: bool = False
    accepts_target: bool = False
    non_persisting_dry_run: bool = True
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

    # Reviewer Specialist only reads code through bounded read/search agent capabilities.
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
        self._tb_project_policy: TbProjectPolicy | None = None
        self._scope_contract: ReviewScopeContract | None = None
        self._non_corrective_issues: list[ReviewIssue] = []
        self._audit: dict[str, list[dict[str, Any]]] = {"filtered": [], "rejected": []}
        self._attempt_id = ""
        self._audit_phase = "initial"
        self._review_run_id = ""
        self._acceptance_current: bool | None = None
        self._goal_receipt_replayed = False
        self._goal_mode = False
        self._resolved_scope: list[str] | None = None
        self._goal_target_surface: dict[str, Any] | None = None
        self._verify_prior: dict[str, Any] = {}
        self._sampled_contract: dict[str, Any] | None = None
        self._review_state_bytes: bytes | None = None

    def read_state(self) -> DevelopmentState:
        """Persist Interactive review history in the resolved Booley Directory."""
        if self.args.state_file is None:
            root = Path(self.args.work_dir).resolve()
            checkout_id = hashlib.sha256(str(root).encode()).hexdigest()[:24]
            self.args.state_file = (
                resolve_checkout_booley_dir(root) / "reviewer-state" / checkout_id / "state.json"
            )
        self._review_state_bytes = self._read_review_state_bytes()
        return super().read_state()

    def _read_review_state_bytes(self) -> bytes | None:
        try:
            return Path(self.args.state_file).read_bytes() if self.args.state_file else None
        except FileNotFoundError:
            return None

    @staticmethod
    def _open_findings(detail: dict[str, Any]) -> list[dict[str, Any]]:
        rows = detail.get("pending", detail.get("issue_list", []))
        return [
            {
                **row,
                "finding_id": row.get("finding_id")
                or _finding_record(ReviewIssue.from_dict(row))["finding_id"],
            }
            for row in rows
            if row.get("status") not in {"fixed", "waived", "excluded", "superseded"}
            and row.get("disposition", "current") == "current"
        ]

    def _prepare_invocation(self) -> McpToolResult | None:
        """Resolve caller intent before validating or changing review state."""
        from booley.flows.execution_persistence import ReviewGoalInputs

        self._goal_mode = isinstance(self._acceptance_recorder, ReviewGoalInputs)
        if self._is_verify_pass():
            return self._prepare_verify_invocation()
        if getattr(self.args, "waive", []):
            return McpToolResult(exit_code=EXIT_ERROR, report_text="--waive requires --verify")
        if not self.args.category or not self.args.focus:
            return McpToolResult(
                exit_code=EXIT_ERROR, report_text="Discovery requires --category and --focus"
            )
        if self._goal_mode:
            return self._prepare_goal_discovery()
        return None

    def _prepare_goal_discovery(self) -> McpToolResult | None:
        forbidden = self._supplied_options() & {"--scope", "--spec", "--steer", "--instruction"}
        if forbidden:
            return McpToolResult(
                exit_code=EXIT_ERROR,
                report_text=f"{', '.join(sorted(forbidden))} refused: the Review Goal defines scope, spec and review context.",
            )
        try:
            key = self._criterion_key()
            open_key = next(
                (
                    name
                    for name, entry in self.state.criteria.items()
                    if name.startswith("review_")
                    and name.endswith("_clean")
                    and self._open_findings(entry.detail)
                ),
                None,
            )
            if open_key is not None:
                return self._open_findings_refusal(open_key)
            params = self._acceptance_recorder.review_goal_inputs(key)
            self._resolved_scope = list(params.scope)
            self._goal_target_surface = (
                dict(params.target_surface) if params.target_surface is not None else None
            )
            self.args.spec = params.spec
            self.args.steer = None
        except (OSError, ValueError) as exc:
            return McpToolResult(exit_code=EXIT_ERROR, report_text=str(exc))
        missing = [
            name for name in self._parse_scope() if not (Path(self.args.work_dir) / name).is_file()
        ]
        if missing:
            return McpToolResult(
                exit_code=EXIT_ERROR,
                report_text=f"Review Goal files not found: {', '.join(missing)}",
            )
        return None

    def _open_findings_refusal(self, key: str) -> McpToolResult:
        return McpToolResult(
            exit_code=EXIT_ERROR,
            report_text=f"{execution_goal_name(self._acceptance_recorder, key)} has open findings. Resolve this clean review before starting another discovery review. Use reviewer --verify and --waive <finding-id> <justification>.",
        )

    def _supplied_options(self) -> set[str]:
        return {word.split("=", 1)[0] for word in self._raw_argv if word.startswith("--")}

    def _prepare_verify_invocation(self) -> McpToolResult | None:
        allowed = {
            "--verify",
            "--waive",
            "--work-dir",
            "--project",
            "--report-dir",
            "--transcript-dir",
        }
        forbidden = self._supplied_options() - allowed
        if forbidden:
            return McpToolResult(
                exit_code=EXIT_ERROR,
                report_text=f"--verify accepts only --waive; refused {', '.join(sorted(forbidden))}",
            )
        selected = self._latest_review()
        if selected is None:
            return McpToolResult(
                exit_code=EXIT_ERROR,
                report_text="--verify refused: no prior review with open findings",
            )
        key, entry = selected
        prior = deepcopy(entry.detail)
        findings = self._open_findings(prior)
        if not findings:
            return McpToolResult(
                exit_code=EXIT_ERROR,
                report_text="--verify refused: the most recent review has no open findings",
            )
        if error := self._validate_waiver_proposals(findings):
            return error
        return self._restore_verify_context(key, prior, findings)

    def _restore_verify_context(
        self, key: str, prior: dict[str, Any], findings: list[dict[str, Any]]
    ) -> McpToolResult | None:
        contract = prior["contract"]
        self.args.category, self.args.focus = contract["category"], contract["focus"]
        self._resolved_scope = list(contract["scope"])
        self.args.spec = contract.get("spec_source") or None
        self.args.steer = None
        if self._criterion_key() != key:
            return McpToolResult(
                exit_code=EXIT_ERROR,
                report_text="Latest review no longer matches its Goal; run a review for the current Goal",
            )
        # Admission and publication fence the Goal revision. Verify uses the
        # discovery contract even when the current Target no longer resolves.
        if self._goal_mode:
            root = Path(self.args.work_dir).resolve()
            for path in self._resolved_scope:
                if not (root / path).resolve().is_relative_to(root):
                    return McpToolResult(
                        exit_code=EXIT_ERROR,
                        report_text=f"Review path {path!r} escapes the Goal worktree",
                    )
        if "pending" not in prior:
            prior["resolved"] = [
                *prior.get("resolved", []),
                *[
                    row
                    for row in prior.get("issue_list", [])
                    if row.get("status") in {"fixed", "waived", "excluded", "superseded"}
                ],
            ]
        prior.setdefault(
            "observations",
            [
                row
                for row in prior.get("issue_list", [])
                if row.get("disposition", "current") in _NON_CORRECTIVE_DISPOSITIONS
                and row.get("status") not in {"fixed", "waived", "excluded", "superseded"}
            ],
        )
        prior["pending"] = findings
        self._verify_prior = prior
        return None

    def _next_review_order(self) -> int:
        return 1 + max(
            (
                as_int(entry.detail.get("review_call_order"), default=0) or 0
                for entry in self.state.criteria.values()
            ),
            default=0,
        )

    def _latest_review(self):
        reviews = [
            (key, entry)
            for key, entry in self.state.criteria.items()
            if key.startswith("review_") and entry.detail.get("contract")
        ]
        return (
            max(
                reviews,
                key=lambda item: (
                    as_int(item[1].detail.get("review_call_order"), default=0) or 0,
                    item[1].updated_at,
                    item[0],
                ),
            )
            if reviews
            else None
        )

    def _validate_waiver_proposals(self, findings: list[dict[str, Any]]) -> McpToolResult | None:
        open_ids = {row["finding_id"] for row in findings}
        seen = set()
        for finding_id, justification in self.args.waive:
            if finding_id not in open_ids or finding_id in seen or not justification.strip():
                return McpToolResult(
                    exit_code=EXIT_ERROR,
                    report_text=f"Invalid --waive {finding_id!r}: name an open finding once with a nonempty justification",
                )
            seen.add(finding_id)
        return None

    def _workspace_isolation_category(self) -> str | None:
        """Hide opposite-category sources only inside the private snapshot."""
        if self._args is None:
            return None
        return self.args.category

    def _workspace_isolation_scope(self) -> tuple[str, ...]:
        return tuple(self._parse_scope())

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
        return bool(getattr(self.args, "verify", False))

    def _add_agent_args(self, parser: argparse.ArgumentParser) -> None:
        parser.add_argument(
            "--scope", default=None, help="Interactive Mode: comma-separated review files"
        )
        parser.add_argument(
            "--verify",
            action="store_true",
            help="Verify only the latest review call's open findings; no discovery",
        )
        parser.add_argument(
            "--waive",
            nargs=2,
            action="append",
            default=[],
            metavar=("FINDING_ID", "JUSTIFICATION"),
            help="Propose a waiver during --verify; repeatable",
        )
        parser.add_argument(
            "--category",
            default=None,
            choices=["rtl", "tb"],
            help="Review category: rtl or tb",
        )
        parser.add_argument(
            "--focus",
            default=None,
            help=(
                "Review focus category. "
                f"RTL: {', '.join(sorted(RTL_FOCUS_CATEGORIES))}. "
                f"TB: {', '.join(sorted(TB_FOCUS_CATEGORIES))}."
            ),
        )
        self._add_steer_arg(
            parser,
            help_text="Developer Agent context and steering instructions.",
        )
        parser.add_argument(
            "--spec",
            default=None,
            help=(
                "Path to the specification the review checks. Required for spec-focused "
                "reviews; pass the Project specification path."
            ),
        )
        self._add_dry_run_arg(parser)

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
            allow_support=self._goal_mode,
        )
        errors.extend(scope_errors)
        _warn_unregistered_sources(scope_paths, self.args.work_dir)

        return errors

    def _parse_scope(self) -> list[str]:
        """Parse comma-separated scope paths."""
        if self._resolved_scope is not None:
            return list(self._resolved_scope)
        return [s.strip() for s in (self.args.scope or "").split(",") if s.strip()]

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

    def _resolve_scope_contract(self) -> str | None:
        """Resolve source-language-specific review behavior once per invocation."""
        if self._scope_contract is not None:
            return None
        try:
            self._scope_contract = resolve_review_scope(
                self._parse_scope(),
                category=self.args.category,
                allow_support=self._goal_mode,
            )
        except ReviewContractError as exc:
            return str(exc)
        return None

    def _review_contract_detail(self) -> dict[str, Any]:
        if self._sampled_contract is not None:
            return deepcopy(self._sampled_contract)
        spec_arg = getattr(self.args, "spec", None)
        contract = build_review_contract_detail(
            ReviewInvocation(
                work_dir=Path(self.args.work_dir),
                category=self.args.category,
                focus=next(iter(self._parse_focus()), ""),
                scope=tuple(self._parse_scope()),
                mode="clean" if self._is_clean_mode() else "done",
                spec_path=Path(spec_arg) if spec_arg else None,
                steering=self.steering_text(),
                tb_policy_digest=review_policy_digest(
                    Path(self.args.work_dir), self.args.category
                ),
            )
        )
        if self._goal_target_surface is not None:
            contract["goal_target_surface"] = dict(self._goal_target_surface)
        return contract

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
        cat_label = "RTL" if category == "rtl" else "testbench"
        sections = [
            f"You are a {cat_label} code reviewer.\n",
            """\
## Review methodology

Work through every criterion in the review guide below. Read the files in scope \
first, then use Grep to trace signal drivers/consumers, package definitions, and \
state transitions. Report a finding only after confirming it in the code — drop \
anything you cannot substantiate. Make each finding's severity and confidence \
match the strength of the evidence.
""",
        ]
        self._append_review_guides(sections, focus, category, gp)
        style_focus = (category == "rtl" and focus == "code_style") or (
            category == "tb" and focus == "quality"
        )
        if style_focus:
            self._append_style_guides(sections, category, gp)
        sections.append(self._output_instructions(focus, category))
        return "\n".join(sections)

    def _append_review_guides(
        self,
        sections: list[str],
        focus: str,
        category: str,
        guide_paths: dict[str, str],
    ) -> None:
        """Append the source-kind-specific review checklists."""
        if category == "rtl":
            guide_name = "protocol-cdc" if focus == "protocol" else focus
            paths = [f"{guide_paths['rtl_guide_dir']}/{guide_name}.md"]
        else:
            if self._scope_contract is None:
                self._resolve_scope_contract()
            contract = self._scope_contract or ReviewScopeContract()
            if contract.has_cpp:
                sections.append(
                    "## C/C++ testbench review\n"
                    "Review the scoped C/C++ sources and headers for stimulus, checks, "
                    "pass/fail reporting, simulator lifecycle, memory safety and determinism. "
                    "Report findings only in explicit scope. HDL guides apply only to HDL files; "
                    "Python guides apply only to Python files."
                )
            names = []
            if contract.has_hdl:
                names.append("tb-review.md")
            if contract.has_cocotb:
                names.append("cocotb-tb-review.md")
            paths = [f"{guide_paths['tb_guide_dir']}/{name}" for name in names]

        for guide_path in paths:
            guide_content = self._read_guide(guide_path)
            if guide_content:
                sections.append(f"## Review guide — {focus} ({Path(guide_path).stem})\n")
                sections.append(guide_content)
                sections.append("")

    def _append_style_guides(
        self,
        sections: list[str],
        category: str,
        guide_paths: dict[str, str],
    ) -> None:
        """Append packaged and project-specific style guidance."""
        label = "RTL" if category == "rtl" else "Testbench"
        key = "rtl_style_guide" if category == "rtl" else "tb_style_guide"
        contract = self._scope_contract or ReviewScopeContract()
        skip_sv = category == "tb" and contract.has_cpp and not contract.has_hdl
        style_content = "" if skip_sv else self._read_guide(guide_paths[key])
        if style_content:
            sections.extend([f"## {label} style guide\n", style_content, ""])
        overlay = self._read_style_overlay(category)
        if not overlay:
            return
        sections.extend(
            [
                f"## {label} style guide — project overlay\n",
                "Rules below are authored by this project and take precedence over the "
                "generic guide above wherever the two conflict. Review against them with "
                "the same severity levels.\n",
                overlay,
                "",
            ]
        )

    # --- Prompt construction ---

    def _build_prompt(self, *, focus_override: str | None = None) -> str:
        """Build discovery context; focus_override leaves parsed arguments intact."""
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

        # Change-type severity policy (TB only — the RTL guides carry no
        # coverage-expansion checks to relax).
        if self.args.category == "tb":
            project_policy = self._build_tb_project_policy_section()
            if project_policy:
                sections.append(project_policy)

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
            getattr(self.args, "spec", None),
            getattr(self.args, "work_dir", None),
        )
        if not spec_content:
            return ""
        return f"## Specification (source: {spec_source})\n\n{spec_content}\n"

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

    def _tb_policy(self) -> TbProjectPolicy:
        """Return the cached TB policy for this worktree."""
        if self._tb_project_policy is None:
            self._tb_project_policy = _load_tb_project_policy(Path(self.args.work_dir))
        return self._tb_project_policy

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

Anchor each finding in a Goal, supplied specification, accepted decision,
steering, or concrete code behavior and explain its relation to the finding.
Choose the disposition deliberately; headings and Project-policy phrases do
not override it.

Emit one JSON object with an ``issues`` array; entries that violate the
schema are dropped upstream. Fields (all required unless noted):
  - severity:       "CRITICAL" | "MAJOR" | "MINOR"   (uppercase)
  - confidence:     "HIGH" | "MEDIUM" | "LOW"        (uppercase)
  - category:       "{focus}"                        (active focus)
  - kind:           "code_defect" | "proof_gap" | "spec_ambiguity"
  - disposition:    "current" | "advisory" | "deferred" | "out_of_scope"
  - ticket_clause:  non-empty scope anchor appropriate to the review mode above
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
      "kind": "code_defect",
      "disposition": "current",
      "ticket_clause": "Relevant requirement or code behavior being reviewed",
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
map straight across). Call ``ReportFindings`` at most once, with every
finding in that one call. A ``ReportFindings`` call with an empty
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
                "remove one from the Criteria"
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

    def _run(self) -> McpToolResult:
        """Serialize review calls so latest-only verification cannot lose its history."""
        path = self.state.file_path
        if path is None or getattr(self.args, "dry_run", False):
            return self._run_review()
        lock_path = path.with_suffix(".review.lock")
        lock_path.parent.mkdir(parents=True, exist_ok=True)
        try:
            with lock_path.open("a+", encoding="ascii") as lock, nonblocking_file_lock(lock):
                if self._read_review_state_bytes() != self._review_state_bytes:
                    self.read_state()
                try:
                    return self._run_review()
                finally:
                    self._review_state_bytes = self._read_review_state_bytes()
        except LockContentionError:
            return McpToolResult(
                exit_code=EXIT_ERROR,
                report_text="Another review is running for this state; retry after it finishes.",
            )
        except EvidenceDiscarded as exc:
            self.discard_evidence(exc)
            return McpToolResult(exit_code=EXIT_ERROR, report_text=str(exc))

    def _run_review(self) -> McpToolResult:
        """Single-focus review with terminal _done or disposition-loop _clean mode."""
        self._audit = {"filtered": [], "rejected": []}
        self._non_corrective_issues = []
        self._tb_project_policy = None
        self._scope_contract = None
        self._sampled_contract = None
        self._resolved_scope = None
        self._goal_target_surface = None
        self._review_run_id = f"{self._invocation_id}-{uuid4().hex}"
        self._review_evidence_written = False
        try:
            if error := self._prepare_invocation():
                return error
        except EvidenceDiscarded as exc:
            self.discard_evidence(exc)
            return McpToolResult(exit_code=EXIT_ERROR, report_text=str(exc))
        prepared = self._prepare_review_run()
        if isinstance(prepared, McpToolResult):
            return prepared
        if getattr(self.args, "dry_run", False):
            return self._dry_run_preview()
        self._review_evidence_path = (
            resolve_booley_dir(Path(self.args.work_dir))
            / "reviewer-evidence"
            / f"{self._review_run_id}.json"
        )
        self._sampled_contract = self._review_contract_detail()
        result = self._execute_review(prepared)
        return self._publish_review_evidence(result, prepared)

    def _attach_review_evidence(self, detail: dict[str, Any], exit_code: int) -> Path:
        """Publish evidence before any Criterion can advertise the outcome."""
        for name in ("filtered", "rejected"):
            detail.setdefault(name, [])
        path = self._review_evidence_path
        detail["audit_evidence"] = str(path)
        detail["artifacts"] = {
            **detail.get("artifacts", {}),
            "reviewer_evidence": str(path),
        }
        atomic_write_json(path, {"exit_code": exit_code, "detail": detail})
        self._review_evidence_written = True
        return path

    def _record_review_detail(self, crit_key: str, met: bool, detail: dict[str, Any]) -> None:
        criterion_met = met
        if self._is_verify_pass() and crit_key.endswith("_done"):
            prior_entry = self.state.criteria[crit_key]
            prior = self._verify_prior
            detail["contract"] = {
                key: value
                for key, value in prior["contract"].items()
                if not key.startswith("decisions_")
            }
            for field in (
                "_source_fingerprint",
                "receipt_id",
                "observation_count",
                "review_outcome",
                "gate_passed",
            ):
                if field in prior:
                    detail[field] = deepcopy(prior[field])
            criterion_met = prior_entry.met
        self._attach_review_evidence(detail, EXIT_SUCCESS if met else EXIT_FAILURE)
        self.set_criterion(crit_key, criterion_met, detail=detail)

    def _publish_review_evidence(self, result: McpToolResult, crit_key: str) -> McpToolResult:
        """Publish complete evidence even when a review cannot issue a receipt."""
        prior = self._get_prior_detail(crit_key) or {}
        if result.exit_code == EXIT_ERROR:
            result.detail = {
                "review_detail_version": REVIEW_DETAIL_VERSION,
                "contract": self._review_contract_detail(),
                **prior,
                **result.detail,
                **self._audit,
                "review_error": True,
            }
        elif not result.detail:
            result.detail = dict(prior)
        if self._acceptance_current is not None:
            result.detail = deepcopy(result.detail)
        path = self._review_evidence_path
        if not self._review_evidence_written or result.exit_code == EXIT_ERROR:
            self._attach_review_evidence(result.detail, result.exit_code)
        if (
            result.exit_code == EXIT_ERROR
            and self.state
            and not (self._is_verify_pass() and crit_key.endswith("_done"))
        ):
            self.set_criterion(crit_key, False, detail=result.detail)
        if (
            self.state
            and not getattr(self.args, "diagnostic", False)
            and not self._goal_receipt_replayed
            and self._acceptance_current is None
            and self.evidence_discarded is None
        ):
            entry = self.state.criteria.get(crit_key)
            if entry is not None:
                entry.detail.update(
                    {
                        name: result.detail[name]
                        for name in ("filtered", "rejected", "audit_evidence")
                    }
                )
                self.state.save()
        result.report_text += f"\nReviewer evidence: {path}"
        return result

    def _record_rejections(self, rows: list[dict[str, Any]], channel: str) -> None:
        self._audit["rejected"].extend(
            {**row, "channel": channel, "phase": self._audit_phase, "attempt_id": self._attempt_id}
            for row in rows
        )

    def _audit_detail(self) -> dict[str, Any]:
        """Attach the active audit rows to an outcome."""
        return dict(self._audit)

    def _prepare_review_run(self) -> str | McpToolResult:
        """Validate source, criterion, and specification inputs."""
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
        missing = [] if self._is_verify_pass() else self._check_scope_files_exist()
        if missing:
            names = ", ".join(missing)
            return McpToolResult(
                exit_code=EXIT_ERROR,
                report_text=(
                    f"Scope files not found: {names}. "
                    "Code the missing files before running this review."
                ),
            )

        contract_error = self._resolve_scope_contract()
        if contract_error:
            return McpToolResult(
                exit_code=EXIT_ERROR,
                report_text=f"Review scope contract error: {contract_error}",
            )

        if spec_error := self._validate_review_spec():
            return spec_error
        return crit_key

    def _validate_review_spec(self) -> McpToolResult | None:
        """Require readable specification text for the spec focus."""
        try:
            spec_content, _ = resolve_spec_content(
                getattr(self.args, "spec", None),
                getattr(self.args, "work_dir", None),
            )
        except OSError as exc:
            recovery = ""
            if self._is_verify_pass():
                recovery = (
                    " Restore the original specification before retrying --verify, or request an approved Goal Change."
                    if self._goal_mode
                    else " Restore the original specification or start a new Interactive discovery review."
                )
            return McpToolResult(exit_code=EXIT_ERROR, report_text=str(exc) + recovery)
        if SPEC_FOCUS not in self._parse_focus() or spec_content is not None:
            return None
        return McpToolResult(
            exit_code=EXIT_ERROR,
            report_text=(
                "Spec review needs a spec to check against, but none was found. "
                "Pass --spec <path> with the Project specification to review."
            ),
        )

    def _dry_run_preview(self) -> McpToolResult:
        """Describe the validated review without invoking its agent."""
        contract = self._scope_contract or ReviewScopeContract()
        focus = next(iter(self._parse_focus()))
        if self.args.category == "rtl":
            guides = [focus]
        else:
            guides = []
            if contract.has_hdl:
                guides.append("hdl-testbench")
            if contract.has_cocotb:
                guides.append("cocotb-testbench")
            if contract.has_cpp:
                guides.append("c-cpp-testbench")
        summary = (
            f"reviewer dry-run: {self.args.category}/{focus}; "
            f"{len(self._parse_scope())} file(s); guides={','.join(guides)}"
        )
        return self._dry_run_result(
            summary=summary,
            detail={
                "category": self.args.category,
                "focus": focus,
                "scope": self._parse_scope(),
                "guides": guides,
                "spec": getattr(self.args, "spec", None) or "",
                "steering_count": len(getattr(self.args, "steer", None) or []),
            },
        )

    def _execute_review(self, crit_key: str) -> McpToolResult:
        """Run one validated terminal or corrective review."""
        prior = self._get_prior_detail(crit_key) or {}
        self._audit = {
            name: list(prior.get(name) or [])
            if self._is_verify_pass()
            or (
                prior.get("review_error")
                and prior.get("contract") == self._review_contract_detail()
            )
            else []
            for name in ("filtered", "rejected")
        }

        self._refresh_review_freshness(crit_key)

        self.emit_progress(f"reviewing {self.args.category}/{next(iter(self._parse_focus()))}")

        if self._is_verify_pass():
            return self._run_clean_verify(crit_key, self._verify_prior)
        if self._is_clean_mode():
            return self._run_clean_mode(crit_key)

        # _done mode records terminal review completion, not cleanliness.
        # Fresh completed legacy receipts may still have an unmet cleanliness gate.
        if self._goal_mode and self._done_receipt_completed(crit_key):
            self._goal_receipt_replayed = self._acceptance_current is not None
            return self._replay_done_verdict(crit_key)

        # Run single-focus review
        overall_start = time.monotonic()
        issues, output_lines = self._run_single_review()

        if issues is None:
            return McpToolResult(
                exit_code=EXIT_ERROR,
                report_text=self._provider_failure_report(
                    output_lines, "Reviewer Specialist invocation failed"
                ),
            )

        elapsed = time.monotonic() - overall_start
        return self._build_result(issues, output_lines, elapsed=elapsed)

    def _refresh_review_freshness(self, crit_key: str) -> None:
        """Use the acceptance owner's freshness policy before replaying evidence."""
        # Refresh before either mode's idempotency guard.  Report submission
        # uses this same operation, so Reviewer cannot replay a verdict which
        # the acceptance path will immediately call stale.
        from booley.flows.execution_persistence import criterion_is_current_for

        self._goal_receipt_replayed = False
        self._acceptance_current = (
            criterion_is_current_for(self._acceptance_recorder, self.state, crit_key)
            if self.state
            else None
        )
        if self.state and self._acceptance_current is None:
            refresh_verification_freshness(
                self.state,
                work_dir=Path(self.args.work_dir),
            )

    def _done_receipt_completed(self, crit_key: str) -> bool:
        """Replay only fresh completed review evidence, never incomplete/error detail."""
        entry = self.state.criteria.get(crit_key)
        if entry is None or entry.stale or self._acceptance_current is False:
            return False
        detail = entry.detail or {}
        findings = detail.get("issue_list")
        return (
            detail.get("review_detail_version") == REVIEW_DETAIL_VERSION
            and not detail.get("error")
            and isinstance(findings, list)
            and all(
                isinstance(row, dict) and row.get("severity") and row.get("summary")
                for row in findings
            )
        )

    # --- _clean mode ---

    def _already_clean_result(self, crit_key: str) -> McpToolResult:
        msg = (
            f"{execution_goal_name(self._acceptance_recorder, crit_key)} already met for the current source fingerprint. "
            "Do not call this reviewer again; proceed with remaining work."
        )
        return McpToolResult(
            exit_code=EXIT_SUCCESS,
            report_text=msg,
            display_lines=[
                f"SKIPPED: {execution_goal_name(self._acceptance_recorder, crit_key)} already met (done — do not retry)"
            ],
        )

    def _clean_verify_limit(self, crit_key: str, prior_detail: dict[str, Any]):
        if prior_detail.get("verify_attempts", 0) >= 2:
            msg = (
                f"{execution_goal_name(self._acceptance_recorder, crit_key)}: 2 verify attempts exhausted — unresolved findings remain. "
                "Fix them or propose explicit, justified review waivers; findings are never "
                "waived automatically."
            )
            return McpToolResult(exit_code=EXIT_FAILURE, report_text=msg)
        if prior_detail.get("total_verify_cycles", 0) >= 3:
            msg = (
                f"{execution_goal_name(self._acceptance_recorder, crit_key)}: 3 review/resolve cycles exhausted — unresolved findings "
                "remain. Blocking without creating an automatic waiver."
            )
            return McpToolResult(exit_code=EXIT_FAILURE, report_text=msg)
        return None

    def _run_clean_verify(self, crit_key: str, prior_detail: dict[str, Any]) -> McpToolResult:
        limit = self._clean_verify_limit(crit_key, prior_detail)
        if limit is not None:
            return limit
        self.emit_progress("verify mode: checking if prior findings are resolved")
        overall_start = time.monotonic()
        remaining, output_lines, remaining_indices, dispositions = self._run_verify_review(
            prior_detail
        )
        if remaining is None:
            report = self._provider_failure_report(
                output_lines, "Verify Reviewer Specialist invocation failed"
            )
            return McpToolResult(exit_code=EXIT_ERROR, report_text=report)
        return self._build_result_clean_verify(
            remaining,
            output_lines,
            prior_detail,
            remaining_indices=remaining_indices,
            dispositions=dispositions,
            elapsed=time.monotonic() - overall_start,
            crit_key=crit_key,
        )

    def _run_clean_mode(self, crit_key: str) -> McpToolResult:
        """Drive the _clean criterion through initial review → verify loop."""
        if (
            self._goal_mode
            and self.state.is_met(crit_key)
            and self._acceptance_current is not False
        ):
            self._goal_receipt_replayed = self._acceptance_current is not None
            return self._already_clean_result(crit_key)
        return self._run_clean_initial(crit_key)

    def _run_clean_initial(self, crit_key: str) -> McpToolResult:
        """Run the first (no prior findings) review pass for _clean mode."""
        overall_start = time.monotonic()
        issues, output_lines = self._run_single_review()
        if issues is None:
            return McpToolResult(
                exit_code=EXIT_ERROR,
                report_text=self._provider_failure_report(
                    output_lines, "Reviewer Specialist invocation failed"
                ),
            )
        elapsed = time.monotonic() - overall_start
        return self._build_result_clean_initial(
            issues,
            output_lines,
            elapsed=elapsed,
            crit_key=crit_key,
        )

    def _review_source_digest(self) -> str:
        """Return a digest of only the files declared in this review scope."""
        try:
            root = Path(self.args.work_dir)
            rows = [
                {
                    "path": path,
                    "sha256": hashlib.sha256((root / path).read_bytes()).hexdigest(),
                }
                for path in sorted(self._parse_scope())
            ]
        except OSError:
            logger.warning("Could not fingerprint sources for review freshness", exc_info=True)
            return ""
        return hashlib.sha256(json.dumps(rows, sort_keys=True).encode()).hexdigest()

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
        prior, issues, current, counts = self._replayed_facts(crit_key)
        gate_passed = True
        _status, count_str = _format_status_and_counts(counts, gate_passed)
        outcome = "REVIEWED WITH FINDINGS" if issues else "REVIEWED — NO FINDINGS"

        lines = [
            f"{execution_goal_name(self._acceptance_recorder, crit_key)} already completed for the current source, so this call "
            "did NOT re-run the review.",
            "Replaying the recorded verdict verbatim:",
            f"\nRESULT: {outcome} ({count_str})",
        ]
        lines += [f"  {_format_issue_line(iss)}" for iss in issues]
        lines.extend(self._done_approval_notice(bool(current)))
        lines.append(
            "\nThis review is already completed for the current source. Calling "
            "`reviewer` again only replays this verdict while the reviewed source remains "
            "current. A later source edit makes the criterion stale and requires "
            "a new final review. Use a review Goal requiring `clean` when findings "
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
                f"SKIPPED: {execution_goal_name(self._acceptance_recorder, crit_key)} already completed for current source — prior verdict replayed"
            ],
            report_text=report_text,
        )

    def _done_approval_notice(self, has_findings: bool) -> list[str]:
        """Fresh and replayed done verdicts share the same presentation policy."""
        if has_findings and done_findings_require_approval_for(self._acceptance_recorder):
            return [
                "INFO: current done findings require explicit human approval before acceptance."
            ]
        return []

    def _replayed_facts(
        self, crit_key: str
    ) -> tuple[dict[str, Any], list[ReviewIssue], list[dict[str, Any]], dict[str, int]]:
        """Compute the prior producer facts shared by replay rendering and its result."""
        from booley.evidence.review_dispositions import outstanding_done_findings

        prior = self._get_prior_detail(crit_key) or {}
        issues = [ReviewIssue.from_dict(d) for d in prior.get("issue_list", [])]
        current = outstanding_done_findings({crit_key: {"detail": prior}})
        counts = count_by_severity([ReviewIssue.from_dict(row) for row in current])
        detail = {
            **prior,
            "gate_passed": True,
            "issues": len(current),
            "observation_count": len(issues),
            **counts,
            "review_outcome": "corrective" if current else "advisory" if issues else "no_findings",
        }
        return detail, issues, current, counts

    def _get_prior_detail(self, crit_key: str) -> dict[str, Any] | None:
        """Retrieve the detail dict for a criterion from state."""
        if not self.state:
            return None
        entries = self.state._resolve_entries(crit_key)
        if not entries:
            return None
        return entries[0].detail

    # --- Verify review ---

    def _review_state_filter(self):
        return filter_state_file_for_category(
            getattr(self.args, "state_file", None),
            self.args.category,
        )

    @staticmethod
    def _provider_failure_report(output_lines: list[str], fallback: str) -> str:
        return next(
            (line for line in reversed(output_lines) if line.startswith("reviewer failed:")),
            "\n".join([fallback, *output_lines]),
        )

    def _record_review_provider_failure(
        self, exc: Exception, transcript: Path | None, summary: str
    ) -> str:
        log_exception(logger, exc, summary=summary)
        diagnostic_path = self._write_provider_diagnostic(exc, transcript)
        return exception_report_text(self.name, exc, diagnostic_path)

    def _verify_agent_params(
        self, focus: str, prior_detail: dict[str, Any], prior_sid: str | None
    ) -> AgentCallParams:
        params = AgentCallParams(
            prompt=self._build_verify_prompt(focus, prior_detail, resumed=prior_sid is not None),
            model=self._resolve_model(),
            cwd=self.args.work_dir,
            allowed_agent_capabilities=self.agent_capabilities,
            disallowed_agent_capabilities=[*self.READ_ONLY_DENY, REPORT_FINDINGS_CAPABILITY],
            system_prompt=self._build_verify_system_prompt(focus),
            output_format=self._output_format(),
            max_turns=self.args.max_turns,
            timeout_seconds=self.timeout_seconds(),
            transcript_path=self._transcript_path(),
            label=f"review-verify-{self.args.category}-{focus}",
            needs_skills=self._needs_skills(),
            reasoning_effort=self._resolve_effort(),
        )
        return self._build_resume_params(params, prior_sid) if prior_sid is not None else params

    def _run_verify_review(
        self,
        prior_detail: dict[str, Any],
    ) -> tuple[
        list[ReviewIssue] | None,
        list[str],
        set[int],
        dict[int, dict[str, str]],
    ]:
        """Check only open findings in a fresh, read-only Reviewer context."""
        self._attempt_id = f"{self._review_run_id or self._invocation_id}-{uuid4().hex}"
        self._audit_phase = "verification"
        focus = next(iter(self._parse_focus()))
        # The "no --report-dir, nothing persisted" notice lives in McpTool._post_run:
        # the gap is every endpoint's, not the reviewer's (SETUP-F-39).
        output_lines = [f"[review-verify] {self.args.category}/{focus}"]
        session_key = f"reviewer-{self.args.category}-{focus}"
        prior_sid = (
            None  # A fresh context prevents obsolete discovery steering from influencing verify.
        )
        start = time.monotonic()
        params = self._verify_agent_params(focus, prior_detail, prior_sid)
        self.emit_progress("invoking verification Specialist")
        try:
            with self._review_state_filter():
                result = self._invoke_agent_with_resume(params)
                remaining, remaining_indices, dispositions = self._parse_verify_output(
                    result.output, prior_detail
                )
        except Exception as exc:  # noqa: BLE001 — normalize the review-provider boundary
            output_lines.append(
                self._record_review_provider_failure(
                    exc,
                    params.transcript_path,
                    f"Verify Reviewer Specialist failed for focus={focus}",
                )
            )
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
        """List open findings and explicit waiver proposals, with current source context."""
        issue_list = self._open_findings(prior_detail)

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

        proposals = getattr(self.args, "waive", [])
        if proposals:
            sections.append("## Waiver proposals (Reviewer decides each justification)")
            sections.append(json.dumps(dict(proposals), ensure_ascii=False))

        if not resumed:
            spec_section = self._build_spec_section()
            if spec_section:
                sections.append(spec_section)
            if self.args.category == "tb":
                sections.append(self._build_tb_project_policy_section())

        sections.append("## Original findings to verify\n")
        sections.extend(self._verify_finding_lines(issue_list, resumed=resumed))
        sections.append("")

        sections.append(self._verify_output_instructions())
        return "\n".join(sections)

    @staticmethod
    def _verify_finding_lines(issue_list: list[dict[str, Any]], *, resumed: bool) -> list[str]:
        sections: list[str] = []
        for i, iss in enumerate(issue_list, 1):
            sev = iss.get("severity", "?")
            summary = iss.get("summary", "?")
            file = iss.get("file", "?")
            line = iss.get("line", "?")
            prior_status = iss.get("status", "")
            status_note = (
                " (was STILL_PRESENT in prior verify)" if prior_status == "still_present" else ""
            )
            sections.append(
                f"{i}. ID={iss.get('finding_id', '')} [{sev}] {file}:{line} — {summary}{status_note}"
            )
            if not resumed:
                spec_clause = iss.get("spec_clause")
                if spec_clause:
                    sections.append(f'   Spec clause: "{spec_clause}"')
                fix_suggestion = iss.get("fix_suggestion")
                if fix_suggestion:
                    sections.append(f"   Original suggestion: {fix_suggestion}")
        return sections

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
            "``justification`` grounded in the current code, Goal, or Project "
            "policy. WAIVED is allowed only for an explicit waiver proposal for that finding ID. Every accepted waiver is persisted and shown to the user, "
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
      "justification": "The Goal deliberately preserves this externally visible timing"
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
        # so persisted review cycles can resume after a restart.
        issue_list = self._open_findings(prior_detail)

        diagnostics: list[dict[str, Any]] = []
        issue_count = len(issue_list)
        dispositions = self._extract_verify_dispositions(output, diagnostics, issue_count)
        self._refuse_unsolicited_waivers(dispositions, issue_list, diagnostics)
        self._record_rejections(diagnostics, "verification")

        # For issues the model didn't mention, fall back to prior status.
        # Issues previously verified as "fixed" stay fixed unless the model
        # explicitly says STILL_PRESENT; issues without prior status (first
        # verify pass) default to STILL_PRESENT for safety.
        remaining: list[ReviewIssue] = []
        remaining_indices: set[int] = set()
        for i, iss_dict in enumerate(issue_list, 1):
            if i in dispositions:
                if dispositions[i]["status"] == VERIFY_STATUS_STILL_PRESENT:
                    remaining.append(ReviewIssue.from_dict(iss_dict))
                    remaining_indices.add(i)
            elif iss_dict.get("status") != "fixed":
                remaining.append(ReviewIssue.from_dict(iss_dict))
                remaining_indices.add(i)
        return remaining, remaining_indices, dispositions

    def _refuse_unsolicited_waivers(self, dispositions, issue_list, diagnostics) -> None:
        requested = dict(getattr(self.args, "waive", []))
        for index, disposition in list(dispositions.items()):
            if disposition["status"] == VERIFY_STATUS_WAIVED:
                finding_id = issue_list[index - 1].get("finding_id")
                if finding_id not in requested:
                    dispositions[index] = {"status": VERIFY_STATUS_STILL_PRESENT}
                    diagnostics.append({"index": index, "reason": "unsolicited_waiver"})

    @staticmethod
    def _extract_verify_dispositions(
        output: str,
        diagnostics: list[dict[str, Any]] | None = None,
        issue_count: int | None = None,
    ) -> dict[int, dict[str, str]]:
        """Pull validated per-index dispositions out of Specialist output.

        Applies the strict finding schema (see ``_validate_finding_dict``)
        at the parsing boundary. Schema violations are logged and
        either demoted (FIXED-without-evidence → STILL_PRESENT) or
        dropped (everything else).
        """
        data = _find_balanced_json_object(output, "findings")
        if data is None:
            logger.warning(
                "Verification Specialist output had no recognizable findings JSON "
                "wrapper — every original finding will be treated as "
                "STILL_PRESENT (fail-closed)",
            )
            return {}

        diagnostics = diagnostics if diagnostics is not None else []
        raw_findings = data["findings"]
        if not isinstance(raw_findings, list):
            diagnostics.append(
                {"ordinal": 1, "raw": raw_findings, "errors": ["findings wrapper must be a list"]}
            )
            return {}
        dispositions: dict[int, dict[str, str]] = {}
        for ord_idx, finding in enumerate(raw_findings, 1):
            errs = _validate_finding_dict(finding)
            idx = finding.get("index") if isinstance(finding, dict) else None
            if not errs and issue_count is not None and idx > issue_count:
                errs.append("index exceeds the pending finding count")
            if errs:
                diagnostic = {"ordinal": ord_idx, "raw": finding, "errors": errs}
                if isinstance(idx, int) and not isinstance(idx, bool) and idx >= 1:
                    diagnostic["verifier_index"] = idx
                diagnostics.append(diagnostic)
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
            for support_field in ("evidence", "justification"):
                value = finding.get(support_field)
                if isinstance(value, str) and value.strip():
                    disposition[support_field] = value.strip()
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

    def _capability_channel_findings(
        self,
        result: Any,
        focus: str,
        output_lines: list[str],
    ) -> list[MirrorFinding] | None:
        """Merge captured ``ReportFindings`` calls into one mirror report.

        Returns ``None`` when the agent never called the capability (channel
        absent), which is different from an empty call (channel present,
        zero findings).
        """
        rf_map = getattr(result, "captured_agent_capability_calls", None)
        rf_calls = rf_map.get(REPORT_FINDINGS_CAPABILITY) if isinstance(rf_map, dict) else None
        if not isinstance(rf_calls, list):
            return None
        calls = self._valid_mirror_calls(rf_calls, output_lines)
        findings = merge_report_calls([report_findings_to_mirror(call) for call in calls])
        logger.info(
            "Reviewer Specialist for %s/%s reported %d finding(s) in %d %s call(s)",
            self.args.category,
            focus,
            len(findings),
            len(calls),
            REPORT_FINDINGS_CAPABILITY,
        )
        return findings

    def _valid_mirror_calls(
        self,
        rf_calls: list[Any],
        output_lines: list[str],
    ) -> list[list[dict[str, Any]]]:
        """Return each call's valid entries; record malformed wrappers and entries."""
        calls: list[list[Any]] = []
        for ordinal, call in enumerate(rf_calls, 1):
            raw = call.get("findings", []) if isinstance(call, dict) else call
            if isinstance(raw, list):
                calls.append(raw)
            else:
                self._record_rejections(
                    [
                        {
                            "ordinal": ordinal,
                            "raw": raw,
                            "errors": ["findings wrapper must be a list"],
                        }
                    ],
                    "ReportFindings mirror",
                )
        flat = (item for call in calls for item in call)
        malformed = [
            {"ordinal": ordinal, "raw": item, "errors": ["mirror entry requires file and summary"]}
            for ordinal, item in enumerate(flat, 1)
            if not _is_mirror_entry(item)
        ]
        self._record_rejections(malformed, "ReportFindings mirror")
        if malformed:
            output_lines.append(
                f"WARN: dropped {len(malformed)} malformed {REPORT_FINDINGS_CAPABILITY} "
                "entr" + ("y" if len(malformed) == 1 else "ies") + " (missing file/summary)",
            )
        return [[item for item in call if _is_mirror_entry(item)] for call in calls]

    def _extract_review_issues(
        self,
        result: Any,
        focus: str,
        output_lines: list[str],
    ) -> list[ReviewIssue] | None:
        """Turn a Reviewer Specialist result into issues, honoring both contracts.

        Claude-backend agents report through the native ``ReportFindings``
        capability (captured via ``capture_agent_capability_calls``); Codex agents — and Claude
        agents that print the JSON instead — use the ``{"issues": [...]}`` text
        contract. The text JSON is canonical; the capability calls are a mirror
        that must agree with it (see ``mirror_findings_missing_from_canonical``).

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
        mirror = self._capability_channel_findings(result, focus, output_lines)
        if mirror is None:
            return self._interpret_review_parse(result.output, focus, output_lines)

        text_parsed = parse_review_output(result.output, allowed_category=focus)
        self._record_rejections(text_parsed.diagnostics, "canonical")
        if not text_parsed.json_present:
            logger.error(
                "Reviewer Specialist for %s/%s called %s but emitted no canonical issues JSON",
                self.args.category,
                focus,
                REPORT_FINDINGS_CAPABILITY,
            )
            output_lines.append(
                f"ERROR: {REPORT_FINDINGS_CAPABILITY} is only a mirror; the final message "
                "must contain issues JSON with disposition metadata",
            )
            return None

        if self._note_rejected_issues(text_parsed, focus, output_lines):
            return None
        return self._canonical_issues_if_mirror_agrees(
            mirror, text_parsed.issues, focus, output_lines
        )

    def _note_rejected_issues(
        self,
        parsed: ReviewParseResult,
        focus: str,
        output_lines: list[str],
    ) -> bool:
        """Report schema-rejected issues; True when *every* reported issue died.

        "The Reviewer Specialist reported issues and the harness threw all of them away"
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
            "Reviewer Specialist for %s/%s: all %d reported issue(s) failed the schema — "
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

    def _canonical_issues_if_mirror_agrees(
        self,
        mirror: list[MirrorFinding],
        text_issues: list[ReviewIssue],
        focus: str,
        output_lines: list[str],
    ) -> list[ReviewIssue] | None:
        """Return the canonical text issues unless the mirror reports more.

        ReportFindings cannot carry disposition fields, so the final JSON is
        canonical. A mirror finding that the JSON leaves out or rates lower
        means a broken mirror; that fails the Specialist instead of
        persisting an unbound finding (SETUP-F-33).
        """
        missing = mirror_findings_missing_from_canonical(mirror, text_issues)
        if not missing:
            return text_issues
        cited = ", ".join(f"{f.file}:{f.line}" for f in missing)
        logger.error(
            "Review channel disagreement for %s/%s: %s reported %d finding(s) that the "
            "canonical text JSON leaves out or rates lower: %s",
            self.args.category,
            focus,
            REPORT_FINDINGS_CAPABILITY,
            len(missing),
            cited,
        )
        output_lines.append(
            f"ERROR: {REPORT_FINDINGS_CAPABILITY} reported finding(s) its canonical issues "
            f"JSON leaves out or rates lower: {cited}",
        )
        return None

    def _interpret_review_parse(
        self,
        output: str,
        focus: str,
        output_lines: list[str],
    ) -> list[ReviewIssue] | None:
        """Parse Specialist output for a review focus.

        Appends any warning/error lines to ``output_lines`` and returns the
        parsed issues, or ``None`` when no JSON wrapper was present (treated
        as a Specialist error rather than an implicit clean pass).
        """
        parsed = parse_review_output(output, allowed_category=focus)
        self._record_rejections(parsed.diagnostics, "canonical")
        if not parsed.json_present:
            logger.error(
                "Reviewer Specialist for %s/%s emitted no recognizable "
                "issues JSON wrapper — refusing to treat as a "
                "clean review",
                self.args.category,
                focus,
            )
            output_lines.append(
                "ERROR: Specialist output contained no parseable issues JSON — not a clean pass",
            )
            return None
        if self._note_rejected_issues(parsed, focus, output_lines):
            return None
        return parsed.issues

    def _single_review_params(self, focus: str) -> AgentCallParams:
        return AgentCallParams(
            prompt=self._build_prompt(focus_override=focus),
            model=self._resolve_model(),
            cwd=self.args.work_dir,
            allowed_agent_capabilities=[
                *self.agent_capabilities,
                REPORT_FINDINGS_CAPABILITY,
            ],
            disallowed_agent_capabilities=list(self.READ_ONLY_DENY),
            system_prompt=self._build_system_prompt(focus),
            output_format=self._output_format(),
            capture_agent_capability_calls=[REPORT_FINDINGS_CAPABILITY],
            max_turns=self.args.max_turns,
            timeout_seconds=self.timeout_seconds(),
            transcript_path=self._transcript_path(),
            label=f"review-{self.args.category}-{focus}",
            needs_skills=self._needs_skills(),
            reasoning_effort=self._resolve_effort(),
        )

    def _run_single_review(
        self, *, phase: str = "discovery"
    ) -> tuple[list[ReviewIssue] | None, list[str]]:
        """Run exactly one focus review. Returns (issues, output_lines) or (None, lines) on error.

        Treats "agent emitted no JSON wrapper at all" as a Specialist error
        rather than an implicit clean pass — otherwise a stream of
        free-form prose would slip through as ``0 issues``.
        """
        focus = next(iter(self._parse_focus()))
        output_lines = [f"[review] {self.args.category}/{focus}"]
        self._non_corrective_issues = []
        self._attempt_id = f"{self._review_run_id or self._invocation_id}-{uuid4().hex}"
        self._audit_phase = phase

        start = time.monotonic()
        params = self._single_review_params(focus)
        self.emit_progress(f"invoking Reviewer Specialist ({self.args.category}/{focus})")
        try:
            with self._review_state_filter():
                result = self._invoke_agent(params)
                issues = self._extract_review_issues(
                    result,
                    focus,
                    output_lines,
                )
                if issues is None:
                    return None, output_lines
                issues = self._filter_review_issues(issues, output_lines)
                self.emit_progress(f"review complete: {len(issues)} finding(s)")
        except Exception as exc:  # noqa: BLE001 — normalize the review-provider boundary
            output_lines.append(
                self._record_review_provider_failure(
                    exc,
                    params.transcript_path,
                    f"Reviewer Specialist failed for focus={focus}",
                )
            )
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
        """Enforce explicit source membership and honor agent dispositions."""
        kept: list[ReviewIssue] = []
        dropped = 0
        for ordinal, issue in enumerate(issues, 1):
            if not self._issue_file_in_scope(issue.file):
                self._audit["filtered"].append(
                    {
                        **issue.to_dict(),
                        "finding_id": _finding_record(issue)["finding_id"],
                        "reason": "source_scope",
                        "explanation": "Cited file is outside the explicit source scope.",
                        "ordinal": issue.proposal_ordinal or ordinal,
                        "attempt_id": self._attempt_id,
                        "phase": self._audit_phase,
                    }
                )
                dropped += 1
            elif issue.disposition in _NON_CORRECTIVE_DISPOSITIONS:
                self._non_corrective_issues.append(issue)
            else:
                kept.append(issue)
        if dropped:
            output_lines.append(
                f"INFO: filtered {dropped} proposal(s) outside explicit source scope; preserved in audit evidence"
            )
        if self._non_corrective_issues and self._criterion_key().endswith("_clean"):
            output_lines.append(
                f"INFO: preserved {len(self._non_corrective_issues)} advisory/deferred observation(s) without gating this review"
            )
        return kept

    def _issue_file_in_scope(self, issue_file: str) -> bool:
        """Match an agent-reported file against the explicit review scope."""
        path = Path(issue_file)
        if path.is_absolute():
            try:
                path = path.resolve().relative_to(Path(self.args.work_dir).resolve())
            except ValueError:
                return False
        contract = self._scope_contract or ReviewScopeContract()
        return contract.contains_file(str(path))

    def _done_finding_records(
        self,
        all_issues: list[ReviewIssue],
    ) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
        # Schema-3 rows state whether they are corrective. Fail closed if a
        # non-canonical channel somehow reaches persistence without metadata.
        for issue in all_issues:
            if not issue.disposition:
                issue.disposition = DISPOSITION_CURRENT
        current_records = [
            _finding_record(issue) for issue in [*all_issues, *self._non_corrective_issues]
        ]
        crit_key = self._criterion_key()
        records = current_records
        from booley.evidence.review_dispositions import outstanding_done_findings

        corrective_records = outstanding_done_findings(
            {crit_key: {"detail": {"issue_list": records}}}
        )
        return records, corrective_records

    def _done_detail(
        self,
        outcome: _DoneReviewOutcome,
        *,
        elapsed: float,
    ) -> dict[str, Any]:
        return {
            "review_detail_version": REVIEW_DETAIL_VERSION,
            "review_call_order": self._next_review_order(),
            **self._audit_detail(),
            "issues": len(outcome.issues),
            "observation_count": len(outcome.records),
            "issue_list": outcome.records,
            **outcome.counts,
            "elapsed_s": round(elapsed, 1),
            "gate_passed": outcome.gate_passed,
            "review_outcome": (
                "corrective"
                if outcome.corrective_records
                else "advisory"
                if outcome.records
                else "no_findings"
            ),
            "contract": self._review_contract_detail(),
        }

    def _done_outcome(self, all_issues: list[ReviewIssue]) -> _DoneReviewOutcome:
        records, corrective_records = self._done_finding_records(all_issues)
        issues = [ReviewIssue.from_dict(row) for row in corrective_records]
        counts = count_by_severity(issues)
        return _DoneReviewOutcome(
            issues=issues,
            records=records,
            corrective_records=corrective_records,
            counts=counts,
            gate_passed=True,
        )

    def _build_result(
        self,
        all_issues: list[ReviewIssue],
        output_lines: list[str],
        *,
        elapsed: float,
    ) -> McpToolResult:
        """Compute gate, set criterion, and format display lines (_done mode)."""
        done_outcome = self._done_outcome(all_issues)
        _status, count_str = _format_status_and_counts(
            done_outcome.counts,
            done_outcome.gate_passed,
        )
        result_label = (
            "REVIEWED WITH FINDINGS" if done_outcome.records else "REVIEWED — NO FINDINGS"
        )
        output_lines.append(f"\nRESULT: {result_label} ({count_str})")
        lines = [f"{count_str}, review completed"]
        lines.extend(
            f"  {_format_issue_line(ReviewIssue.from_dict(row))} [{row.get('status', 'reported')}]"
            for row in done_outcome.records
        )
        lines.extend(self._done_approval_notice(bool(done_outcome.corrective_records)))
        historical = len(done_outcome.records) - len(done_outcome.corrective_records)
        if historical:
            lines.append(f"INFO: preserved {historical} advisory/historical observation(s).")
        output_lines.extend(lines[1:])
        report_text = "\n".join(output_lines)
        print(report_text)
        detail = self._done_detail(done_outcome, elapsed=elapsed)
        crit_key = self._criterion_key()
        self._record_review_detail(crit_key, True, detail)
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
        """Record discovery: every corrective finding starts open."""
        counts = count_by_severity(issues)
        gate_passed = not issues

        status, count_str = _format_status_and_counts(counts, gate_passed)
        output_lines.extend(_format_issue_line(issue) for issue in issues)
        output_lines.append(f"\nRESULT: {status} ({count_str})")

        report_text = "\n".join(output_lines)
        # Print concise result summary to stdout (consumed by callers and tests)
        print(report_text)

        detail = self._clean_discovery_detail(issues, counts, elapsed)

        if gate_passed:
            self._record_review_detail(crit_key, True, detail)
        else:
            detail["verify_attempts"] = 0
            detail["original_issues"] = len(issues)
            self._record_review_detail(crit_key, False, detail)

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

    def _clean_discovery_detail(
        self, issues: list[ReviewIssue], counts: dict[str, int], elapsed: float
    ) -> dict[str, Any]:
        detail: dict[str, Any] = {
            "review_detail_version": REVIEW_DETAIL_VERSION,
            "review_call_order": self._next_review_order(),
            **self._audit_detail(),
            "issues": len(issues),
            "pending": [_finding_record(iss) for iss in issues],
            "resolved": [],
            "observations": [_finding_record(iss) for iss in self._non_corrective_issues],
            **counts,
            "elapsed_s": round(elapsed, 1),
            "review_source_digest": self._review_source_digest(),
            "contract": self._review_contract_detail(),
        }

        return detail

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
        """Build a verify-only result without running discovery."""
        pending, resolved, original_issues = self._split_verify_findings(
            existing_detail, remaining_indices, dispositions
        )
        source_digest = self._review_source_digest()
        observations = list(existing_detail.get("observations", []))
        return self._finish_clean_verify(
            _CleanVerifyContext(
                remaining=remaining,
                output_lines=output_lines,
                existing_detail=existing_detail,
                pending=pending,
                resolved=resolved,
                original_issues=original_issues,
                source_digest=source_digest,
                observations=observations,
                elapsed=elapsed,
                crit_key=crit_key,
            )
        )

    def _finish_clean_verify(self, context: _CleanVerifyContext) -> McpToolResult:
        counts = count_by_severity(context.remaining)
        met = not context.pending
        status, count_str = _format_status_and_counts(counts, met)
        checked = {row["finding_id"] for row in self._open_findings(context.existing_detail)}
        status_lines = [
            f"{row['finding_id']}: {row.get('status', 'still_present').upper()} — {row.get('summary', '')}"
            for row in [*context.pending, *context.resolved]
            if row.get("finding_id") in checked
        ]
        context.output_lines.extend(status_lines)
        context.output_lines.append(f"\nVERIFY RESULT: {status} ({count_str})")
        waiver_lines = self._clean_verify_waiver_lines(context.resolved)
        if waiver_lines:
            context.output_lines.extend(["", "ACCEPTED WAIVERS (user-visible):", *waiver_lines])
        report_text = "\n".join(context.output_lines)
        print(report_text)
        detail, verify_attempts = self._clean_verify_detail(context, counts)
        self._record_review_detail(context.crit_key, met, detail)
        if met:
            focus = next(iter(self._parse_focus()))
            self._clear_session_id(f"reviewer-{self.args.category}-{focus}")
        lines = [f"{count_str}, verify {status} (attempt {verify_attempts}/2)"]
        lines.extend(f"  {line}" for line in status_lines)
        lines.extend(f"  {line}" for line in waiver_lines)
        return McpToolResult(
            exit_code=EXIT_SUCCESS if met else 1,
            criterion_key=context.crit_key,
            criterion_met=(
                self.state.criteria[context.crit_key].met
                if context.crit_key.endswith("_done")
                else met
            ),
            display_lines=lines,
            detail=detail,
            report_text=report_text,
        )

    @staticmethod
    def _clean_verify_waiver_lines(resolved: list[dict[str, Any]]) -> list[str]:
        return [
            f"[WAIVED {item.get('severity', '?')}] "
            f"{item.get('file', '?')}:{item.get('line', '?')} — "
            f"{item.get('summary', '?')} — Justification: "
            f"{item.get('justification', '')}"
            for item in resolved
            if item.get("status") == "waived"
        ]

    def _clean_verify_detail(
        self, context: _CleanVerifyContext, counts: dict[str, int]
    ) -> tuple[dict[str, Any], int]:
        verify_attempts = context.existing_detail.get("verify_attempts", 0) + 1
        detail: dict[str, Any] = {
            "review_detail_version": REVIEW_DETAIL_VERSION,
            "review_call_order": self._next_review_order(),
            **self._audit_detail(),
            "issues": len(context.remaining),
            "pending": context.pending,
            "resolved": context.resolved,
            "observations": context.observations,
            **counts,
            "verification_only": True,
            "verify_attempts": verify_attempts,
            "total_verify_cycles": context.existing_detail.get("total_verify_cycles", 0) + 1,
            "original_issues": context.original_issues,
            "elapsed_s": round(context.elapsed, 1),
            "review_source_digest": context.source_digest,
            "contract": {
                **{
                    k: v
                    for k, v in context.existing_detail["contract"].items()
                    if not k.startswith("decisions_")
                },
                "scope_hashes": self._review_contract_detail()["scope_hashes"],
            },
        }
        if context.crit_key.endswith("_done"):
            detail["issue_list"] = [*context.pending, *context.resolved, *context.observations]
        return detail, verify_attempts

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
        # legacy ``issue_list`` to resume persisted review cycles.
        prior_pending = self._open_findings(existing_detail)
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
