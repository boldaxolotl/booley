"""Re-check the open findings of an earlier Reviewer evidence file (issue #4522).

Pure functions, no model call: read and validate the evidence file, build the prompt, and
judge the model's verdicts. ``reviewer.py`` runs the call and writes the new evidence file.
"""

from __future__ import annotations

import json
from collections.abc import Collection, Mapping, Sequence
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
    STATUS_WAIVED,
)

REASON_UNKNOWN_FINDING = "unknown_finding"
REASON_DUPLICATE_VERDICT = "duplicate_verdict"
REASON_INVALID_VERDICT = "invalid_verdict"
REASON_FIXED_WITHOUT_EVIDENCE = "fixed_without_evidence"
REASON_OMITTED_FINDING = "omitted_finding"
REASON_UNSOLICITED_WAIVER = "unsolicited_waiver"

# How the report names each status a re-check writes.
STATUS_LABELS = {
    STATUS_FIXED: "fixed",
    STATUS_STILL_OPEN: "still open",
    STATUS_CANT_TELL: "can't tell",
    STATUS_WAIVED: "waived",
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
    ruling: str = ""


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
    for name, found in (("detail", version), ("contract", contract_version)):
        if found != REVIEW_DETAIL_VERSION:
            raise RecheckInputError(
                f"Reviewer evidence file {path} has {name} version {found}; this Booley reads "
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
            and isinstance(entry.get("file"), str)
            and isinstance(entry.get("line"), int)
            and isinstance(entry.get("summary"), str)
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
    # A file whose last open finding was waived is still re-checked: it lists its waivers again.
    if not recheck.open_findings and not any(row["status"] == STATUS_WAIVED for row in findings):
        raise RecheckInputError(
            f"Reviewer evidence file {path} has no open findings; nothing to re-check."
        )
    return recheck


def validate_waivers(recheck: RecheckInput, waivers: Sequence[Sequence[str]]) -> dict[str, str]:
    """The waiver proposals as ``{finding ID: reason}``; refuse one that cannot be judged."""
    open_ids = {row["finding_id"] for row in recheck.open_findings}
    proposals: dict[str, str] = {}
    for finding_id, reason in waivers:
        if finding_id not in open_ids:
            problem = "not an open finding of the evidence file"
        elif finding_id in proposals:
            problem = "proposed more than once"
        elif not reason.strip():
            problem = "the reason is blank"
        else:
            proposals[finding_id] = reason.strip()
            continue
        raise RecheckInputError(f"--waive {finding_id}: {problem}")
    return proposals


def existing_scope(recheck: RecheckInput, work_dir: Path) -> tuple[list[str], list[str]]:
    """Split the review's scope into files still in *work_dir* and files removed since."""
    present = [name for name in recheck.scope if (work_dir / name).is_file()]
    return present, [name for name in recheck.scope if name not in present]


_WAIVER_RULES = (
    "- waived: only for a finding that lists a waiver proposal, and only when its reason "
    "justifies leaving the code as it is. A reason that does not hold is refused: judge the "
    "finding as fixed, still_open or cant_tell instead. Every finding with a waiver proposal "
    'gets a "waiver_ruling": one sentence on why the waiver is accepted or refused.\n'
)


def recheck_system_prompt(category: str, focus: str, *, waivers: bool) -> str:
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
        "- cant_tell: the sources do not let you decide.\n" + (_WAIVER_RULES if waivers else "")
    )


def _finding_block(row: dict[str, Any], proposals: Mapping[str, str]) -> str:
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
    if row["finding_id"] in proposals:
        lines.append(f"- waiver proposal: {proposals[row['finding_id']]}")
    return "\n".join(lines)


def recheck_prompt(
    recheck: RecheckInput, spec_section: str, work_dir: Path, proposals: Mapping[str, str]
) -> str:
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
    waivers = proposals
    sections.extend(f"{_finding_block(row, waivers)}\n" for row in recheck.open_findings)
    verdicts = '"fixed" | "still_open" | "cant_tell"'
    extra = ""
    if waivers:
        verdicts += ' | "waived"'
        extra = ' Add "waiver_ruling": "<one sentence>" to every finding with a waiver proposal.'
    sections.append(
        "Emit one JSON object with a ``verdicts`` array holding one entry per finding above: "
        f'{{"verdicts": [{{"finding_id": "<id>", "verdict": {verdicts}, '
        '"evidence": {"file": "<path>", "line": <positive integer>, "reason": "<one sentence>"}}]}. '
        "``evidence`` is required for ``fixed`` and omitted otherwise." + extra + " Prose outside "
        "the JSON does not count."
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


def _ruling(entry: dict[str, Any]) -> str:
    ruling = entry.get("waiver_ruling")
    return ruling.strip() if isinstance(ruling, str) else ""


def judge_verdicts(
    entries: list[Any],
    open_findings: list[dict[str, Any]],
    work_dir: Path,
    proposals: Mapping[str, str],
) -> tuple[dict[str, Verdict], list[dict[str, Any]]]:
    """One verdict per open finding, and the audit rows of every entry dropped or demoted.

    A ``waived`` verdict stands only for a finding with a waiver proposal; for any other it is
    ``still_open``.
    """
    proposed = proposals
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
        elif entry["verdict"] == STATUS_WAIVED and finding_id not in proposed:
            audit.append(_row(ordinal, finding_id, REASON_UNSOLICITED_WAIVER, raw))
            verdicts[finding_id] = Verdict(STATUS_STILL_OPEN)
        elif entry["verdict"] != STATUS_FIXED:
            verdicts[finding_id] = Verdict(entry["verdict"], ruling=_ruling(entry))
        elif citation := _citation(entry, work_dir):
            verdicts[finding_id] = Verdict(STATUS_FIXED, citation, _ruling(entry))
        else:
            audit.append(_row(ordinal, finding_id, REASON_FIXED_WITHOUT_EVIDENCE, raw))
            verdicts[finding_id] = Verdict(STATUS_STILL_OPEN, ruling=_ruling(entry))
    for finding_id in sorted(open_ids - verdicts.keys()):
        audit.append(_row(0, finding_id, REASON_OMITTED_FINDING, None))
        verdicts[finding_id] = Verdict(STATUS_CANT_TELL)
    return verdicts, audit


def rechecked_records(
    recheck: RecheckInput, verdicts: dict[str, Verdict], proposals: Mapping[str, str]
) -> list[dict[str, Any]]:
    """Every input record in input order; an open one takes its verdict, the rest are unchanged.

    An accepted waiver also carries the user's reason and the Reviewer's ruling.
    """
    reasons = proposals
    records: list[dict[str, Any]] = []
    for row in recheck.findings:
        record = dict(row)
        verdict = verdicts.get(row["finding_id"])
        if verdict is not None and row["status"] in OPEN_STATUSES:
            record["status"] = verdict.status
            if verdict.evidence is not None:
                record["recheck_evidence"] = verdict.evidence
            if verdict.status == STATUS_WAIVED:
                record["justification"] = reasons[row["finding_id"]]
                if verdict.ruling:
                    record["waiver_ruling"] = verdict.ruling
        records.append(record)
    return records


def refused_waivers(
    proposals: Mapping[str, str], verdicts: Mapping[str, Verdict]
) -> list[dict[str, str]]:
    """The proposals whose finding did not end ``waived``, with the Reviewer's ruling if any."""
    return [
        {"finding_id": finding_id, "justification": reason, "ruling": verdicts[finding_id].ruling}
        for finding_id, reason in proposals.items()
        if verdicts[finding_id].status != STATUS_WAIVED
    ]
