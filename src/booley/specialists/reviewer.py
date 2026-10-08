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
import time
from copy import deepcopy
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, ClassVar
from uuid import uuid4

from booley.agent_workspace.isolation import (
    filter_state_file_for_category,
)
from booley.core.boundary import as_dict, as_str_list
from booley.core.models import AgentCallParams
from booley.evidence.review_receipt import (
    REVIEW_DETAIL_VERSION,
    ReviewContextError,
    ReviewInvocation,
    build_review_contract_detail,
    review_invocation_changed,
)
from booley.evidence.review_vocabulary import (
    ALL_DISPOSITIONS,
    DISPOSITION_ADVISORY,
    DISPOSITION_CURRENT,
    DISPOSITION_DEFERRED,
    DISPOSITION_OUT_OF_SCOPE,
    DISPOSITION_SUPERSEDED,
)
from booley.flows.execution_persistence import done_findings_require_approval_for
from booley.mcp.base import (
    EXIT_ERROR,
    EXIT_FAILURE,
    EXIT_SUCCESS,
    McpToolResult,
    read_source_dirs_from_toml,
)
from booley.runtime.exception_diagnostics import exception_report_text, log_exception
from booley.runtime.execution_records import atomic_write_json
from booley.runtime.paths import refs_dir
from booley.runtime.project_dir import resolve_project_dir
from booley.targets.flow_names import config_section
from booley.ticket_board.criteria_acceptance import refresh_verification_freshness
from booley.ticket_board.review_policy import review_policy_digest

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


def _load_ticket_text() -> tuple[str, str]:
    """Load the sealed Ticket Mode snapshot, when present.

    Returns (ticket_text, source_path); ("", "") outside Ticket Mode.
    """
    if os.environ.get("BOOLEY_GOAL_FILE"):
        return "", ""
    logs_dir = os.environ.get("BOOLEY_LOGS_DIR", "")
    if logs_dir:
        ticket_path = Path(logs_dir) / "ticket.md"
        if ticket_path.is_file():
            return ticket_path.read_text(encoding="utf-8", errors="replace"), str(ticket_path)

    return "", ""


def _load_ticket_document():
    """Resolve the sealed runtime copy through Ticket Board authority."""
    from booley.ticket_board.helpers import (
        detect_project_root,
        resolve_runtime_ticket_slug,
        tickets_dir_from_project_root,
    )
    from booley.ticket_board.io import TicketIO

    text, source = _load_ticket_text()
    if not text:
        return None, ""
    ticket = Path(source)
    root = detect_project_root()
    slug = resolve_runtime_ticket_slug(ticket)
    document = TicketIO(tickets_dir_from_project_root(root), project_root=root).load_document(
        slug, runtime_ticket_path=ticket
    )
    return document, source


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
    """Resolve the spec text the spec-focus review checks the RTL against.

    Priority:
      1. Ticket Mode's ``spec:`` frontmatter field (path to an external spec,
         relative to the project root / work_dir)
      2. Ticket Mode's ticket description body
      3. standalone ``--spec`` content, read verbatim

    Returns (spec_text, source_description), or (None, "") when no ticket
    is available or it carries neither a spec file nor a body.
    """
    document, ticket_source = (
        (None, "") if os.environ.get("BOOLEY_GOAL_FILE") else _load_ticket_document()
    )
    if document is None:
        if not spec_arg:
            return None, ""
        spec_path = Path(spec_arg)
        if not spec_path.is_absolute() and work_dir:
            spec_path = Path(work_dir) / spec_path
        if not spec_path.is_file():
            raise OSError(f"Specification file not found: {spec_path}")
        content = spec_path.read_text(encoding="utf-8", errors="replace")
        return _truncate_spec(content, label=str(spec_path)), f"spec file: {spec_path}"

    fields, body = document.spec.fields, document.spec.body

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


def resolve_ticket_type() -> str:
    """Return the ticket's ``type:`` frontmatter field, lowercased.

    Returns "" when no ticket is reachable or it declares no type — callers
    treat that as "no type-specific policy", i.e. the full checklist applies.
    """
    document, _ = _load_ticket_document()
    if document is None:
        return ""

    ticket_type = document.spec.fields.get("type")
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


def _merge_finding_records(
    current: list[dict[str, Any]],
    prior: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Merge advisory history without silently clearing corrective findings."""
    from booley.evidence.review_dispositions import outstanding_done_findings

    merged = {str(item.get("finding_id", "")): dict(item) for item in current}
    for old in prior:
        historical = dict(old)
        finding_id = str(
            old.get("finding_id") or _finding_record(ReviewIssue.from_dict(old))["finding_id"]
        )
        historical["finding_id"] = finding_id
        outstanding = bool(
            outstanding_done_findings({"review_history_done": {"detail": {"issue_list": [old]}}})
        )
        if finding_id in merged:
            if outstanding:
                merged[finding_id]["disposition"] = DISPOSITION_CURRENT
                merged[finding_id]["status"] = DISPOSITION_CURRENT
            continue
        historical["status"] = DISPOSITION_CURRENT if outstanding else DISPOSITION_SUPERSEDED
        merged[finding_id] = historical
    return list(merged.values())


def _review_observations(
    existing_detail: dict[str, Any],
    current: list[ReviewIssue],
    *,
    rediscovered: bool,
) -> list[dict[str, Any]]:
    prior = [
        dict(item) for item in existing_detail.get("observations", []) if isinstance(item, dict)
    ]
    if not rediscovered:
        return prior
    return _merge_finding_records([_finding_record(issue) for issue in current], prior)


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
    """Outcome of parsing reviewer agent output.

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
        self._tb_project_policy: TbProjectPolicy | None = None
        self._scope_contract: ReviewScopeContract | None = None
        self._non_corrective_issues: list[ReviewIssue] = []
        self._audit: dict[str, list[dict[str, Any]]] = {"filtered": [], "rejected": []}
        self._attempt_id = ""
        self._audit_phase = "initial"
        self._review_run_id = ""
        self._acceptance_current: bool | None = None
        self._goal_receipt_replayed = False

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
        self._add_scope_arg(parser, help_text="Comma-separated file paths to review")
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
        self._add_steer_arg(
            parser,
            help_text="Developer Agent context and steering instructions.",
        )
        parser.add_argument(
            "--spec",
            default=None,
            help=(
                "Path to the specification the review checks. Ticket Mode resolves "
                "its sealed ticket and linked spec automatically."
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

    def _resolve_scope_contract(self) -> str | None:
        """Resolve source-language-specific review behavior once per invocation."""
        if self._scope_contract is not None:
            return None
        try:
            self._scope_contract = resolve_review_scope(
                self._parse_scope(),
                category=self.args.category,
            )
        except ReviewContractError as exc:
            return str(exc)
        return None

    def _review_contract_detail(self) -> dict[str, Any]:
        spec_arg = getattr(self.args, "spec", None)
        try:
            document, _source = _load_ticket_document()
        except (OSError, RuntimeError, ValueError) as exc:
            raise ReviewContextError(f"Could not convert persisted Ticket: {exc}") from exc
        ticket_spec = document.spec.fields.get("spec") if document is not None else None
        return build_review_contract_detail(
            ReviewInvocation(
                work_dir=Path(self.args.work_dir),
                category=self.args.category,
                focus=next(iter(self._parse_focus()), ""),
                scope=tuple(self._parse_scope()),
                mode="clean" if self._is_clean_mode() else "done",
                spec_path=Path(spec_arg) if spec_arg else None,
                ticket_spec_path=Path(ticket_spec) if ticket_spec else None,
                steering=self.steering_text(),
                tb_policy_digest=review_policy_digest(
                    Path(self.args.work_dir), self.args.category
                ),
            )
        )

    def _invalidate_changed_invocation_contract(self, crit_key: str) -> None:
        """Restart any receipt when this invocation asks a new question."""
        if not self.state:
            return
        entry = self.state.criteria.get(crit_key)
        if entry is None:
            return
        if not entry.detail:
            return
        previous = as_dict(entry.detail.get("contract")) or {}
        current = self._review_contract_detail()
        if (
            previous.get("mode") == "clean"
            and current["mode"] == "done"
            and entry.detail.get("goal_derivation")
        ):
            from booley.flows.execution_persistence import criterion_is_current_for

            if criterion_is_current_for(self._acceptance_recorder, self.state, crit_key) is True:
                previous = {**previous, "mode": "done"}
        previous_version = (entry.detail or {}).get("review_detail_version")
        if previous_version == REVIEW_DETAIL_VERSION and not review_invocation_changed(
            previous, current
        ):
            return
        entry.met = False
        entry.stale = True
        previous_detail = dict(entry.detail)
        archive = self._write_review_evidence(previous_detail, "previous-receipt")
        entry.detail = {
            **previous_detail,
            "stale_reason": "Reviewer scope or context changed; restarting discovery.",
            "contract": current,
            "review_detail_version": REVIEW_DETAIL_VERSION,
            "needs_discovery": True,
            "filtered": [],
            "rejected": [],
            "receipt_history": [*previous_detail.get("receipt_history", []), str(archive)],
        }
        self._clear_session_id(
            f"reviewer-{self.args.category}-{next(iter(self._parse_focus()), '')}"
        )
        self.state.save()

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
        style_content = self._read_guide(guide_paths[key])
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
        """Build review task prompt (scope, focus, spec, steering).

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

        ticket_section = self._build_ticket_scope_section()
        if ticket_section:
            sections.append(ticket_section)

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

        # Steering
        steer_text = self.steering_text()
        if steer_text:
            sections.append(f"## Developer Agent Context\n{steer_text}\n")

        return "\n".join(sections)

    def _build_ticket_scope_section(self) -> str:
        """Inline the complete staged Ticket for every review focus."""
        ticket_text, source = _load_ticket_text()
        if not ticket_text:
            return ""
        return (
            f"## Staged Ticket (binding scope; source: {source})\n\n"
            "Review only behavior required by this Ticket and the accepted "
            "decisions below. Preserve explicitly deferred, future, and "
            "out-of-scope work. Cite a relevant Ticket or accepted-decision clause "
            "and explain its relation to the finding. Choose the disposition "
            "deliberately; headings do not override it.\n\n"
            f"{_truncate_spec(ticket_text, label=source)}\n"
        )

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
        ticket_type = resolve_ticket_type()
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

In Ticket Mode, anchor each finding in a relevant Ticket or accepted-decision
clause and explain its relation to the finding. In Interactive Mode without a
Ticket, use the supplied specification, steering, or concrete code behavior as
the anchor; do not invent a Ticket or accepted decision. Choose the disposition
deliberately. Ticket headings and Project-policy phrases do not override it.

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

    def _run(self) -> McpToolResult:
        """Single-focus review with terminal _done or disposition-loop _clean mode."""
        self._audit = {"filtered": [], "rejected": []}
        self._non_corrective_issues = []
        self._tb_project_policy = None
        self._scope_contract = None
        self._review_run_id = f"{self._invocation_id}-{uuid4().hex}"
        prepared = self._prepare_review_run()
        if isinstance(prepared, McpToolResult):
            return prepared
        if getattr(self.args, "dry_run", False):
            return self._dry_run_preview()
        result = self._execute_review(prepared)
        return self._publish_review_evidence(result, prepared)

    def _evidence_path(self, label: str) -> Path:
        directory = resolve_project_dir(Path(self.args.work_dir)) / "reviewer-evidence"
        return directory / f"{self._review_run_id or uuid4().hex}-{label}.json"

    def _write_review_evidence(self, detail: dict[str, Any], label: str) -> Path:
        path = self._evidence_path(label)
        atomic_write_json(path, detail)
        return path

    def _attach_review_evidence(self, detail: dict[str, Any], exit_code: int, label: str) -> Path:
        """Publish evidence before any Criterion can advertise the outcome."""
        for name in ("filtered", "rejected"):
            detail.setdefault(name, [])
        path = self._evidence_path(label)
        detail["audit_evidence"] = str(path)
        for name in ("filtered", "rejected"):
            detail[name] = [
                {**row, "evidence": row.get("evidence") or str(path)} for row in detail[name]
            ]
        detail["artifacts"] = {
            **detail.get("artifacts", {}),
            "reviewer_evidence": str(path),
        }
        atomic_write_json(path, {"exit_code": exit_code, "detail": detail})
        return path

    def _record_review_detail(self, crit_key: str, met: bool, detail: dict[str, Any]) -> None:
        self._attach_review_evidence(
            detail, EXIT_SUCCESS if met else EXIT_FAILURE, f"receipt-{uuid4().hex}"
        )
        self.set_criterion(crit_key, met, detail=detail)

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
        path = self._attach_review_evidence(result.detail, result.exit_code, "result")
        if result.exit_code == EXIT_ERROR and self.state:
            self.set_criterion(crit_key, False, detail=result.detail)
        if (
            self.state
            and not getattr(self.args, "diagnostic", False)
            and not self._goal_receipt_replayed
            and self._acceptance_current is None
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
        """Attach active audit history and archived receipt references to an outcome."""
        prior = self._get_prior_detail(self._criterion_key()) or {}
        return {**self._audit, "receipt_history": prior.get("receipt_history", [])}

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
            return McpToolResult(exit_code=EXIT_ERROR, report_text=str(exc))
        if SPEC_FOCUS not in self._parse_focus() or spec_content is not None:
            return None
        return McpToolResult(
            exit_code=EXIT_ERROR,
            report_text=(
                "Spec review needs a spec to check against, but none was found. "
                "In Ticket Mode the ticket body (or its spec: field) is used "
                "automatically; in Interactive Mode pass --spec <path>."
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
        self._invalidate_changed_invocation_contract(crit_key)
        prior = self._get_prior_detail(crit_key) or {}
        self._audit = {name: list(prior.get(name) or []) for name in ("filtered", "rejected")}

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

        self.emit_progress(f"reviewing {self.args.category}/{next(iter(self._parse_focus()))}")

        if self._is_clean_mode():
            return self._run_clean_mode(crit_key)

        # _done mode records terminal review completion, not cleanliness.
        # Fresh completed legacy receipts may still have an unmet cleanliness gate.
        if self._done_receipt_completed(crit_key):
            self._goal_receipt_replayed = self._acceptance_current is not None
            return self._replay_done_verdict(crit_key)

        # Run single-focus review
        overall_start = time.monotonic()
        issues, output_lines = self._run_single_review()

        if issues is None:
            return McpToolResult(
                exit_code=EXIT_ERROR,
                report_text=self._provider_failure_report(
                    output_lines, "Review agent invocation failed"
                ),
            )

        elapsed = time.monotonic() - overall_start
        return self._build_result(issues, output_lines, elapsed=elapsed)

    def _done_receipt_completed(self, crit_key: str) -> bool:
        """Replay only fresh completed review evidence, never incomplete/error detail."""
        entry = self.state.criteria.get(crit_key)
        if entry is None or entry.stale or self._acceptance_current is False:
            return False
        detail = entry.detail or {}
        findings = detail.get("issue_list")
        return (
            detail.get("review_detail_version") == REVIEW_DETAIL_VERSION
            and not detail.get("needs_discovery")
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
            f"{crit_key} already met for the current source fingerprint. "
            "Do not call this reviewer again; proceed with remaining work."
        )
        return McpToolResult(
            exit_code=EXIT_SUCCESS,
            report_text=msg,
            display_lines=[f"SKIPPED: {crit_key} already met (done — do not retry)"],
        )

    def _clean_verify_limit(self, crit_key: str, prior_detail: dict[str, Any]):
        if prior_detail.get("verify_attempts", 0) >= 2:
            msg = (
                f"{crit_key}: 2 verify attempts exhausted — unresolved findings remain. "
                "Fix them or propose explicit, justified review waivers; findings are never "
                "waived automatically."
            )
            return McpToolResult(exit_code=EXIT_FAILURE, report_text=msg)
        if prior_detail.get("total_verify_cycles", 0) >= 3:
            msg = (
                f"{crit_key}: 3 review/resolve cycles exhausted — unresolved findings "
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
                output_lines, "Verify review agent invocation failed"
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
        if self.state and self.state.is_met(crit_key) and self._acceptance_current is not False:
            self._goal_receipt_replayed = self._acceptance_current is not None
            return self._already_clean_result(crit_key)
        prior_detail = self._get_prior_detail(crit_key)
        if prior_detail is not None and prior_detail.get("needs_discovery"):
            return self._run_clean_initial(crit_key)
        has_prior_findings = prior_detail is not None and (
            "pending" in prior_detail or "issue_list" in prior_detail
        )
        if not has_prior_findings:
            return self._run_clean_initial(crit_key)
        return self._run_clean_verify(crit_key, prior_detail)

    def _run_clean_initial(self, crit_key: str) -> McpToolResult:
        """Run the first (no prior findings) review pass for _clean mode."""
        overall_start = time.monotonic()
        issues, output_lines = self._run_single_review()
        if issues is None:
            return McpToolResult(
                exit_code=EXIT_ERROR,
                report_text=self._provider_failure_report(
                    output_lines, "Review agent invocation failed"
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
        issues, lines = self._run_single_review(phase="rediscovery")
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
        prior, issues, current, counts = self._replayed_facts(crit_key)
        gate_passed = True
        _status, count_str = _format_status_and_counts(counts, gate_passed)
        outcome = "REVIEWED WITH FINDINGS" if issues else "REVIEWED — NO FINDINGS"

        lines = [
            f"{crit_key} already completed for the current source, so this call "
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
        """Run a verify review checking whether prior findings are fixed.

        Resumes the original review session when a persisted session_id is
        available, so the agent retains context about what it flagged.

        Returns (remaining_issues, output_lines, remaining_indices) where
        remaining_indices are 1-based positions into the original issue_list.
        """
        self._attempt_id = f"{self._review_run_id or self._invocation_id}-{uuid4().hex}"
        self._audit_phase = "verification"
        focus = next(iter(self._parse_focus()))
        # The "no --report-dir, nothing persisted" notice lives in McpTool._post_run:
        # the gap is every endpoint's, not the reviewer's (SETUP-F-39).
        output_lines = [f"[review-verify] {self.args.category}/{focus}"]
        session_key = f"reviewer-{self.args.category}-{focus}"
        prior_sid = self._load_session_id(session_key)
        start = time.monotonic()
        params = self._verify_agent_params(focus, prior_detail, prior_sid)
        self.emit_progress("invoking verify agent")
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
                    f"Verify review agent failed for focus={focus}",
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

        diagnostics: list[dict[str, Any]] = []
        issue_count = len(issue_list)
        dispositions = self._extract_verify_dispositions(output, diagnostics, issue_count)
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

    @staticmethod
    def _extract_verify_dispositions(
        output: str,
        diagnostics: list[dict[str, Any]] | None = None,
        issue_count: int | None = None,
    ) -> dict[int, dict[str, str]]:
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
        for ordinal, call in enumerate(rf_calls, 1):
            raw = call.get("findings", []) if isinstance(call, dict) else call
            if isinstance(raw, list):
                findings.extend(raw)
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
        malformed = []
        for ordinal, item in enumerate(findings, 1):
            if (
                not isinstance(item, dict)
                or not str(item.get("file", "")).strip()
                or not str(item.get("summary", "")).strip()
            ):
                malformed.append(
                    {
                        "ordinal": ordinal,
                        "raw": item,
                        "errors": ["mirror entry requires file and summary"],
                    }
                )
        self._record_rejections(malformed, "ReportFindings mirror")
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
        self._record_rejections(text_parsed.diagnostics, "canonical")
        if not text_parsed.json_present:
            logger.error(
                "Review agent for %s/%s called %s but emitted no canonical issues JSON",
                self.args.category,
                focus,
                REPORT_FINDINGS_CAPABILITY,
            )
            output_lines.append(
                f"ERROR: {REPORT_FINDINGS_CAPABILITY} is only a mirror; the final message "
                "must contain issues JSON with Ticket disposition metadata",
            )
            return None

        if self._note_rejected_issues(text_parsed, focus, output_lines):
            return None
        return self._pick_review_channel(
            capability_issues, text_parsed.issues, focus, output_lines
        )

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
    ) -> list[ReviewIssue] | None:
        """Use canonical text metadata unless the capability channel is more severe.

        ReportFindings cannot carry Ticket disposition fields. The final JSON
        is therefore canonical. A more-severe capability verdict indicates a
        broken mirror and fails the Specialist instead of persisting an
        unbound finding.
        """
        capability_rank = _channel_severity_rank(capability_issues)
        text_rank = _channel_severity_rank(text_issues)
        if capability_rank <= text_rank:
            return text_issues
        logger.error(
            "Review channel disagreement for %s/%s: %s reported %d issue(s) "
            "but the canonical text JSON reported %d with a weaker verdict",
            self.args.category,
            focus,
            REPORT_FINDINGS_CAPABILITY,
            len(capability_issues),
            len(text_issues),
        )
        output_lines.append(
            f"ERROR: {REPORT_FINDINGS_CAPABILITY} reported a more severe verdict than its "
            "canonical issues JSON mirror",
        )
        return None

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
        self._record_rejections(parsed.diagnostics, "canonical")
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
        self.emit_progress(f"invoking review agent ({self.args.category}/{focus})")
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
                    f"Review agent failed for focus={focus}",
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
        prior_records: list[dict[str, Any]] = []
        if self.state and crit_key in self.state.criteria:
            raw_prior = self.state.criteria[crit_key].detail.get("issue_list", [])
            if isinstance(raw_prior, list):
                prior_records = [dict(row) for row in raw_prior if isinstance(row, dict)]
        records = _merge_finding_records(current_records, prior_records)
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

    @staticmethod
    def _carry_review_obligations(
        issues: list[ReviewIssue], prior: dict[str, Any]
    ) -> list[ReviewIssue]:
        """Preserve open obligations when discovery restarts its contract."""
        old_pending = (
            prior.get("pending", prior.get("issue_list", []))
            if prior.get("needs_discovery")
            else []
        )
        records = {_finding_record(issue)["finding_id"]: issue for issue in issues}
        for row in old_pending:
            if (
                row.get("status") not in {"waived", "fixed", "excluded", DISPOSITION_SUPERSEDED}
                and row.get("disposition", "current") == "current"
            ):
                issue = ReviewIssue.from_dict(row)
                records.setdefault(_finding_record(issue)["finding_id"], issue)
        return list(records.values())

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
        prior = self._get_prior_detail(crit_key) or {}
        issues = self._carry_review_obligations(issues, prior)
        counts = count_by_severity(issues)
        gate_passed = not issues

        status, count_str = _format_status_and_counts(counts, gate_passed)
        output_lines.append(f"\nRESULT: {status} ({count_str})")

        report_text = "\n".join(output_lines)
        # Print concise result summary to stdout (consumed by callers and tests)
        print(report_text)

        detail: dict[str, Any] = {
            "review_detail_version": REVIEW_DETAIL_VERSION,
            **self._audit_detail(),
            "issues": len(issues),
            "pending": [_finding_record(iss) for iss in issues],
            "resolved": list(prior.get("resolved", [])),
            "observations": [_finding_record(iss) for iss in self._non_corrective_issues],
            **counts,
            "elapsed_s": round(elapsed, 1),
            "review_source_digest": self._review_source_digest(),
            "contract": self._review_contract_detail(),
        }

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
        """Build result for a verify pass and optional final rediscovery."""
        pending, resolved, original_issues = self._split_verify_findings(
            existing_detail, remaining_indices, dispositions
        )
        rediscovery = self._apply_clean_rediscovery(
            remaining, output_lines, existing_detail, pending, original_issues
        )
        if isinstance(rediscovery, McpToolResult):
            return rediscovery
        remaining, pending, original_issues, source_digest, rediscovered = rediscovery
        observations = _review_observations(
            existing_detail, self._non_corrective_issues, rediscovered=rediscovered
        )
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

    def _apply_clean_rediscovery(
        self,
        remaining: list[ReviewIssue],
        output_lines: list[str],
        existing_detail: dict[str, Any],
        pending: list[dict[str, Any]],
        original_issues: int,
    ) -> tuple[list[ReviewIssue], list[dict[str, Any]], int, str, bool] | McpToolResult:
        source_digest = str(existing_detail.get("review_source_digest", ""))
        rediscovery = self._rediscover_after_source_change(existing_detail, pending)
        if rediscovery is None:
            return remaining, pending, original_issues, source_digest, False
        discovered, discovery_lines, source_digest = rediscovery
        if discovered is None:
            report = self._provider_failure_report(
                discovery_lines, "Final clean-review discovery agent invocation failed"
            )
            return McpToolResult(exit_code=EXIT_ERROR, report_text=report)
        output_lines.extend(["", "[review] final discovery after source changes"])
        output_lines.extend(discovery_lines)
        pending = [_finding_record(issue) for issue in discovered]
        return discovered, pending, original_issues + len(discovered), source_digest, True

    def _finish_clean_verify(self, context: _CleanVerifyContext) -> McpToolResult:
        counts = count_by_severity(context.remaining)
        met = not context.pending
        status, count_str = _format_status_and_counts(counts, met)
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
        lines.extend(f"  {line}" for line in waiver_lines)
        lines.extend(f"  {_format_issue_line(issue)}" for issue in context.remaining)
        return McpToolResult(
            exit_code=EXIT_SUCCESS if met else 1,
            criterion_key=context.crit_key,
            criterion_met=met,
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
            **self._audit_detail(),
            "issues": len(context.remaining),
            "pending": context.pending,
            "resolved": context.resolved,
            "observations": context.observations,
            **counts,
            "verify_attempts": verify_attempts,
            "total_verify_cycles": context.existing_detail.get("total_verify_cycles", 0) + 1,
            "original_issues": context.original_issues,
            "elapsed_s": round(context.elapsed, 1),
            "review_source_digest": context.source_digest,
            "contract": self._review_contract_detail(),
        }
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
