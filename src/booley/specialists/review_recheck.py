"""Re-check the open findings of an earlier Reviewer evidence file (issue #4522).

Pure functions, no model call: read and validate the evidence file, build the prompt, and
judge the model's verdicts. ``reviewer.py`` runs the call and writes the new evidence file.
"""

from __future__ import annotations

import json
from collections.abc import Collection
from dataclasses import dataclass
from pathlib import Path
from typing import Any, cast

from booley.core.boundary import as_dict, is_str_list
from booley.evidence.review_receipt import REVIEW_DETAIL_VERSION
from booley.evidence.review_vocabulary import (
    OPEN_STATUSES,
    STATUS_CANT_TELL,
    STATUS_FIXED,
    STATUS_STILL_OPEN,
)

REASON_UNKNOWN_FINDING = "unknown_finding"
REASON_DUPLICATE_VERDICT = "duplicate_verdict"
REASON_INVALID_VERDICT = "invalid_verdict"
REASON_FIXED_WITHOUT_EVIDENCE = "fixed_without_evidence"
REASON_OMITTED_FINDING = "omitted_finding"

# How the report names each status a re-check writes.
STATUS_LABELS = {
    STATUS_FIXED: "fixed",
    STATUS_STILL_OPEN: "still open",
    STATUS_CANT_TELL: "can't tell",
}


class RecheckInputError(ValueError):
    """A Reviewer evidence file a re-check refuses; the message names the reason."""


@dataclass(frozen=True)
class RecheckInput:
    """A validated Reviewer evidence file."""

    path: Path
    contract: dict[str, Any]
    category: str
    focus: str
    scope: tuple[str, ...]
    findings: list[dict[str, Any]]

    @property
    def open_findings(self) -> list[dict[str, Any]]:
        """The records a re-check sends to the model."""
        return [row for row in self.findings if row["status"] in OPEN_STATUSES]


@dataclass(frozen=True)
class Verdict:
    """The judged state of one open finding."""

    status: str
    evidence: dict[str, Any] | None = None


def _obj(value: Any) -> dict[str, Any]:
    """*value* when it is a JSON object, else an empty one."""
    return cast("dict[str, Any]", value) if isinstance(value, dict) else {}


def _read_detail(path: Path) -> dict[str, Any]:
    if not path.is_file():
        raise RecheckInputError(f"Reviewer evidence file not found: {path}")
    try:
        data: Any = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise RecheckInputError(f"Reviewer evidence file {path} is not JSON: {exc}") from exc
    detail = as_dict(_obj(data).get("detail"))
    if detail is None:
        raise RecheckInputError(f"{path} is not a Reviewer evidence file")
    version = detail.get("review_detail_version")
    contract_version = _obj(detail.get("contract")).get("version")
    for found in (version, contract_version):
        if found != REVIEW_DETAIL_VERSION:
            raise RecheckInputError(
                f"Reviewer evidence file {path} has detail version {found}; this Booley reads "
                f"version {REVIEW_DETAIL_VERSION}. Run a new full review."
            )
    return detail


def _finding_rows(path: Path, detail: dict[str, Any], severities: Collection[str]) -> list[Any]:
    rows: Any = detail.get("issue_list")
    if not isinstance(rows, list):
        raise RecheckInputError(f"Reviewer evidence file {path} has no findings list")
    for number, row in enumerate(rows, 1):
        entry = _obj(row)
        if not (
            isinstance(entry.get("finding_id"), str)
            and isinstance(entry.get("status"), str)
            and isinstance(entry.get("severity"), str)
            and entry["severity"].upper() in severities
        ):
            raise RecheckInputError(f"Reviewer evidence file {path} finding {number} is malformed")
    return rows


def load_recheck_input(
    path: Path,
    *,
    rtl_focuses: Collection[str],
    tb_focuses: Collection[str],
    severities: Collection[str],
) -> RecheckInput:
    """Validate the evidence file at *path*; raise ``RecheckInputError`` naming the reason."""
    detail = _read_detail(path)
    findings = _finding_rows(path, detail, severities)
    contract = _obj(detail["contract"])
    category, focus, scope = contract.get("category"), contract.get("focus"), contract.get("scope")
    valid_focuses = {"rtl": rtl_focuses, "tb": tb_focuses}.get(str(category), ())
    if not (focus in valid_focuses and is_str_list(scope) and scope):
        raise RecheckInputError(f"Reviewer evidence file {path} has no usable review contract")
    recheck = RecheckInput(path, contract, str(category), str(focus), tuple(scope), findings)
    if not recheck.open_findings:
        raise RecheckInputError(
            f"Reviewer evidence file {path} has no open findings; nothing to re-check."
        )
    return recheck


def existing_scope(recheck: RecheckInput, work_dir: Path) -> tuple[list[str], list[str]]:
    """Split the review's scope into files still in *work_dir* and files removed since."""
    present = [name for name in recheck.scope if (work_dir / name).is_file()]
    return present, [name for name in recheck.scope if name not in present]


def recheck_system_prompt(category: str, focus: str) -> str:
    """The system prompt: judge each listed finding, never report a new one."""
    label = "RTL" if category == "rtl" else "testbench"
    return (
        f"You are a {label} code reviewer re-checking the {focus} findings of an earlier "
        "review against the current sources.\n\n"
        "Your only job is to judge each finding listed in the request: has the current code "
        "fixed it? Read the cited code. Never report a new finding, and never judge code the "
        "listed findings do not cite. You can only read files; you cannot run anything.\n\n"
        "Verdicts:\n"
        "- fixed: the cited problem is gone. Cite the current code that shows it: the file, "
        "a line number in that file and one sentence on why that line settles it. A fixed "
        "verdict without that evidence counts as still_open.\n"
        "- still_open: the problem is still there, or the fix is incomplete.\n"
        "- cant_tell: the sources do not let you decide.\n"
    )


def _finding_block(row: dict[str, Any]) -> str:
    lines = [
        f"### {row['finding_id']}",
        f"- severity: {row['severity']}",
        f"- location: {row.get('file', '')}:{row.get('line', 0)}",
        f"- summary: {row.get('summary', '')}",
    ]
    if row.get("ticket_clause"):
        lines.append(f"- ticket_clause: {row['ticket_clause']}")
    if row.get("fix_suggestion"):
        lines.append(f"- fix_suggestion: {row['fix_suggestion']}")
    return "\n".join(lines)


def recheck_prompt(recheck: RecheckInput, spec_section: str, work_dir: Path) -> str:
    """The user prompt: scope, specification, the open findings and the output schema."""
    present, removed = existing_scope(recheck, work_dir)
    sections = ["Re-check the findings below against the current sources.\n", "Files in scope:"]
    sections.extend(f"  - {name}" for name in present)
    if removed:
        sections.append(
            f"\nFiles removed since the review: {', '.join(removed)}. "
            "Judge their findings from the files that exist now."
        )
    if spec_section:
        sections.extend(["", spec_section])
    sections.extend(["", "## Findings to re-check", ""])
    sections.extend(f"{_finding_block(row)}\n" for row in recheck.open_findings)
    sections.append(
        "Emit one JSON object with a ``verdicts`` array holding one entry per finding above: "
        '{"verdicts": [{"finding_id": "<id>", "verdict": "fixed" | "still_open" | "cant_tell", '
        '"evidence": {"file": "<path>", "line": <positive integer>, "reason": "<one sentence>"}}]}. '
        "``evidence`` is required for ``fixed`` and omitted otherwise. Prose outside the JSON "
        "is ignored."
    )
    return "\n".join(sections)


def _citation(entry: dict[str, Any], work_dir: Path) -> dict[str, Any] | None:
    """The entry's evidence when it cites an existing line of a file inside *work_dir*."""
    evidence = _obj(entry.get("evidence"))
    cited, line, reason = evidence.get("file"), evidence.get("line"), evidence.get("reason")
    if not (
        isinstance(cited, str)
        and cited.strip()
        and isinstance(line, int)
        and not isinstance(line, bool)
        and line >= 1
        and isinstance(reason, str)
        and reason.strip()
    ):
        return None
    root = work_dir.resolve()
    try:
        target = (root / cited).resolve()
        if not (target.is_relative_to(root) and target.is_file()):
            return None
        last_line = len(target.read_text(encoding="utf-8", errors="replace").splitlines())
    except (OSError, ValueError):
        return None
    if line > last_line:
        return None
    return {"file": cited.strip(), "line": line, "reason": reason.strip()}


def _row(ordinal: int, finding_id: str, reason: str, raw: Any) -> dict[str, Any]:
    return {"ordinal": ordinal, "finding_id": finding_id, "reason": reason, "raw": raw}


def judge_verdicts(
    entries: list[Any], open_findings: list[dict[str, Any]], work_dir: Path
) -> tuple[dict[str, Verdict], list[dict[str, Any]]]:
    """One verdict per open finding, and the audit rows of every entry dropped or demoted."""
    open_ids = {row["finding_id"] for row in open_findings}
    verdicts: dict[str, Verdict] = {}
    audit: list[dict[str, Any]] = []
    for ordinal, raw in enumerate(entries, 1):
        entry = _obj(raw)
        finding_id = entry.get("finding_id")
        if not isinstance(finding_id, str) or finding_id not in open_ids:
            audit.append(_row(ordinal, str(finding_id), REASON_UNKNOWN_FINDING, raw))
        elif finding_id in verdicts:
            audit.append(_row(ordinal, finding_id, REASON_DUPLICATE_VERDICT, raw))
        elif not isinstance(entry.get("verdict"), str) or entry["verdict"] not in STATUS_LABELS:
            audit.append(_row(ordinal, finding_id, REASON_INVALID_VERDICT, raw))
        elif entry["verdict"] != STATUS_FIXED:
            verdicts[finding_id] = Verdict(entry["verdict"])
        elif citation := _citation(entry, work_dir):
            verdicts[finding_id] = Verdict(STATUS_FIXED, citation)
        else:
            audit.append(_row(ordinal, finding_id, REASON_FIXED_WITHOUT_EVIDENCE, raw))
            verdicts[finding_id] = Verdict(STATUS_STILL_OPEN)
    for finding_id in sorted(open_ids - verdicts.keys()):
        audit.append(_row(0, finding_id, REASON_OMITTED_FINDING, None))
        verdicts[finding_id] = Verdict(STATUS_CANT_TELL)
    return verdicts, audit


def rechecked_records(recheck: RecheckInput, verdicts: dict[str, Verdict]) -> list[dict[str, Any]]:
    """Every input record in input order; an open one takes its verdict, the rest are unchanged."""
    records: list[dict[str, Any]] = []
    for row in recheck.findings:
        record = dict(row)
        verdict = verdicts.get(row["finding_id"])
        if verdict is not None and row["status"] in OPEN_STATUSES:
            record["status"] = verdict.status
            if verdict.evidence is not None:
                record["recheck_evidence"] = verdict.evidence
        records.append(record)
    return records
