"""Append-only findings log for setup and port runs.

One JSON object per line in ``<project_dir>/findings.jsonl``. Append-only and
line-oriented on purpose: several agents write it concurrently (the main agent
plus whatever sub-agents Steps 2/4 delegate to), and a corrupt or half-written
line must cost one finding, never the whole log.

Every entry is one of four kinds: a **finding** (something went wrong), a
**friction** report (nothing broke, but Booley was confusing), an **impression**
(what the user thinks of Booley — praise, gripes, feature wishes), or a **win**
(a check that passed first try). Wins are recorded in the same log so the
friction sample stays honest — a report with 40 findings and no wins reads as
"Booley is broken" when the truth may be "40 rough edges out of 300 checks".

Impressions are the one kind with no evidence bar at all. "I want X", "the
waveform flow is the best part", "the setup skill is exhausting" are not bugs
and cannot be reproduced, but they are the only signal saying which of the
fixable things is worth fixing — so they are logged, redacted and offered
upstream through exactly the same path as everything else.

The log outlives the run that started it. A project set up in March and hit by a
bug in July appends to the same file, which is why entries carry an ``origin``
(which flow logged them) and a ``filed`` stamp (whether they already went
upstream) — without the latter, every later bug report re-files the whole
backlog.
"""

from __future__ import annotations

import json
import logging
from collections.abc import Iterable
from dataclasses import asdict, dataclass, field, fields
from pathlib import Path
from typing import Any, Literal

from booley.project_dir import resolve_project_dir
from booley.timefmt import utc_now_rfc3339

logger = logging.getLogger(__name__)

LOG_NAME = "findings.jsonl"

Severity = Literal["blocker", "workaround", "note"]
Bucket = Literal["project", "booley", "docs", "unknown"]
Kind = Literal["finding", "friction", "impression", "win"]
Origin = Literal["setup", "bug", "impression"]
Sentiment = Literal["praise", "gripe", "wish", "mixed"]

SEVERITIES: tuple[str, ...] = ("blocker", "workaround", "note")
BUCKETS: tuple[str, ...] = ("project", "booley", "docs", "unknown")
KINDS: tuple[str, ...] = ("finding", "friction", "impression", "win")
ORIGINS: tuple[str, ...] = ("setup", "bug", "impression")
#: What an impression is *about*: something loved, something hated, something
#: wanted, or a general take. Coarse on purpose — a taxonomy nobody can pick
#: from in two seconds is a taxonomy that gets skipped.
SENTIMENTS: tuple[str, ...] = ("praise", "gripe", "wish", "mixed")

#: Marker for a friction report, which has a severity but is not a malfunction.
FRICTION_MARKER = "💬"

#: Marker for an impression. Not a severity colour and not the friction marker:
#: an impression is neither a malfunction nor a confusion, and counting it as
#: either would misreport how much of Booley is actually broken.
IMPRESSION_MARKER = "📣"

#: Severity → the marker the reports render it with (matches the port-ip
#: convention so a maintainer reads both report families the same way).
SEVERITY_MARKER = {"blocker": "🔴", "workaround": "🟡", "note": "🔵"}

#: Bucket → how the triage step described it, for report headings.
BUCKET_TITLE = {
    "project": "Your project or environment",
    "booley": "Booley behaviour",
    "docs": "Booley documentation",
    "unknown": "Not yet triaged",
}


@dataclass
class Finding:
    """One logged observation.

    The fields carrying evidence — ``repro``, ``observed``, ``expected`` — are
    what separate a filable bug from an impression. :meth:`is_filable` is the
    gate; it lives here rather than in the skill so the standard can't drift
    per run.
    """

    title: str
    kind: Kind = "finding"
    severity: Severity = "note"
    bucket: Bucket = "unknown"
    #: The exact check or activity that surfaced it ("Step 4, booley doctor --deep").
    exposed_by: str = ""
    #: Setup step or port phase, for grouping.
    step: str = ""
    #: Booley-side subsystem or doc path ("doctor", "sim", "docs/CONFIG.md").
    component: str = ""
    #: Shell command that reproduces it, verbatim.
    repro: str = ""
    observed: str = ""
    expected: str = ""
    notes: str = ""
    #: Impressions only: what this is about (praise / gripe / wish / mixed).
    sentiment: str = ""
    #: Set once someone verified the claim against Booley's own source.
    verified_against_source: bool = False
    #: Which flow logged this — a setup run, or an ad-hoc bug report.
    origin: Origin = "setup"
    #: Files whose contents get inlined (and, outgoing, redacted) into the
    #: reports: a run log, a doctor transcript, a traceback dump.
    attachments: list[str] = field(default_factory=list)
    #: Issue URL once this went upstream, or ``"manual"`` when the user posted it
    #: themselves. Non-empty means "do not file again".
    filed: str = ""
    filed_at: str = ""
    id: str = ""
    created: str = ""

    def __post_init__(self) -> None:
        if self.severity not in SEVERITIES:
            raise ValueError(f"severity must be one of {SEVERITIES}, got {self.severity!r}")
        if self.bucket not in BUCKETS:
            raise ValueError(f"bucket must be one of {BUCKETS}, got {self.bucket!r}")
        if self.kind not in KINDS:
            raise ValueError(f"kind must be one of {KINDS}, got {self.kind!r}")
        if self.origin not in ORIGINS:
            raise ValueError(f"origin must be one of {ORIGINS}, got {self.origin!r}")
        if self.sentiment and self.sentiment not in SENTIMENTS:
            raise ValueError(f"sentiment must be one of {SENTIMENTS}, got {self.sentiment!r}")
        if not isinstance(self.attachments, list) or any(
            not isinstance(a, str) for a in self.attachments
        ):
            raise ValueError(f"attachments must be a list of paths, got {self.attachments!r}")
        if not self.title.strip():
            raise ValueError("a finding needs a title")
        if not self.created:
            self.created = utc_now_rfc3339()

    @property
    def marker(self) -> str:
        """The emoji used in rendered reports.

        Friction gets its own marker rather than a severity colour: it is not a
        malfunction, and rendering it as one would inflate the bug count.
        """
        if self.kind == "friction":
            return FRICTION_MARKER
        if self.kind == "impression":
            return IMPRESSION_MARKER
        return SEVERITY_MARKER.get(self.severity, "🔵")

    def missing_evidence(self) -> list[str]:
        """Which evidence fields a Booley-bound report would still need.

        A maintainer cannot act on "the doctor output confused me": without a
        reproduction and an observed-vs-expected pair, a bug report is a feeling,
        and a queue of feelings buries the real bugs. Findings that fail this
        stay in the local report instead.

        Friction is held to a different standard on purpose. "This was
        confusing" *is* the report — demanding a reproduction for it would just
        filter out the entire class. What it does need is somewhere to aim the
        fix (a component or the check that surfaced it) and what the reporter
        expected instead; a confusion nobody can locate is not actionable either.

        Impressions have no bar whatsoever. "Booley is not worth the setup cost"
        aims at nothing, reproduces nothing, and is still the most useful
        sentence in the log — a maintainer who only ever hears bugs learns what
        is broken and never what matters.
        """
        if self.kind == "impression":
            return []
        if self.kind == "friction":
            missing = []
            if not (self.component.strip() or self.exposed_by.strip()):
                missing.append("component")
            if not (self.expected.strip() or self.notes.strip()):
                missing.append("expected")
            return missing
        if self.bucket == "docs":
            # A doc bug needs the contradiction and the file it is in, not a
            # command line: "CONFIG.md says X, the code does Y" is complete.
            return [name for name in ("observed", "component") if not getattr(self, name).strip()]
        return [
            name for name in ("repro", "observed", "expected") if not getattr(self, name).strip()
        ]

    def is_filable(self) -> bool:
        """Does this entry carry enough evidence to become a GitHub issue?

        Docs findings are exempt from the reproduction requirement: "CONFIG.md
        says X, the code does Y" is fully actionable, and demanding a command
        line for it would drop the single largest class of real findings.

        An entry that has already been filed is never filable again. The log is
        append-only and long-lived, so without this every bug report filed months
        after setup would re-publish that setup run's entire batch.
        """
        if self.kind not in ("finding", "friction", "impression") or self.bucket not in (
            "booley",
            "docs",
        ):
            return False
        if self.filed.strip():
            return False
        return not self.missing_evidence()

    def mark_filed(self, where: str) -> None:
        """Stamp this entry as already reported upstream (URL, or ``"manual"``)."""
        self.filed = where.strip()
        self.filed_at = utc_now_rfc3339()

    def to_json(self) -> str:
        return json.dumps(asdict(self), ensure_ascii=False, sort_keys=True)

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> Finding:
        """Build from a log line, ignoring fields a newer Booley wrote.

        Forward-compatible on purpose: a log written by a later version must
        still render here rather than abort the whole report.
        """
        known = {f.name for f in fields(cls)}
        return cls(**{k: v for k, v in raw.items() if k in known})


@dataclass
class FindingsLog:
    """The parsed log: entries plus however many lines failed to parse."""

    path: Path
    entries: list[Finding] = field(default_factory=list)
    corrupt_lines: int = 0

    @property
    def findings(self) -> list[Finding]:
        """Everything that is not a win — bugs, friction and impressions alike.

        Friction and impressions belong here rather than in sidecar lists: they
        are bucketed and triaged like any other entry, and the reports group by
        bucket.
        """
        return [e for e in self.entries if e.kind != "win"]

    @property
    def bugs(self) -> list[Finding]:
        return [e for e in self.entries if e.kind == "finding"]

    @property
    def friction(self) -> list[Finding]:
        return [e for e in self.entries if e.kind == "friction"]

    @property
    def impressions(self) -> list[Finding]:
        return [e for e in self.entries if e.kind == "impression"]

    @property
    def wins(self) -> list[Finding]:
        return [e for e in self.entries if e.kind == "win"]

    def by_bucket(self, bucket: str) -> list[Finding]:
        return [f for f in self.findings if f.bucket == bucket]

    def counts(self) -> dict[str, int]:
        """Bugs per severity, for the summary line.

        Friction and impressions are counted separately (``len(log.friction)``,
        ``len(log.impressions)``): folding "this was confusing" or "I wish it
        did X" into the blocker/workaround/note table would overstate how much
        of the run was actually broken.
        """
        out = dict.fromkeys(SEVERITIES, 0)
        for f in self.bugs:
            out[f.severity] += 1
        return out


class CorruptFindingsLogError(RuntimeError):
    """A mutating rewrite was refused because raw evidence is unparseable."""


def log_path(project_dir: Path | None = None) -> Path:
    """Absolute path of the findings log."""
    return (project_dir or resolve_project_dir()) / LOG_NAME


def read_log(project_dir: Path | None = None) -> FindingsLog:
    """Read the log, skipping unparseable lines rather than failing.

    A missing log is an empty log — a setup that hit no friction and recorded
    no wins is unusual, not an error.
    """
    path = log_path(project_dir)
    log = FindingsLog(path=path)
    if not path.is_file():
        return log
    for lineno, raw in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
        line = raw.strip()
        if not line:
            continue
        try:
            log.entries.append(Finding.from_dict(json.loads(line)))
        except (json.JSONDecodeError, TypeError, ValueError) as e:
            log.corrupt_lines += 1
            logger.warning("%s:%d: skipping unparseable finding (%s)", path, lineno, e)
    return log


def _next_id(existing: Iterable[Finding]) -> str:
    """Allocate the next ``F-N``, continuing past whatever is already logged."""
    highest = 0
    for entry in existing:
        if entry.id.startswith("F-") and entry.id[2:].isdigit():
            highest = max(highest, int(entry.id[2:]))
    return f"F-{highest + 1}"


def append(finding: Finding, project_dir: Path | None = None) -> Finding:
    """Append *finding* to the log, assigning its id, and return it.

    The read-then-append is not atomic across processes, so two concurrent
    writers can hand out the same ``F-N``. That is deliberate: a duplicate
    label is cosmetic and self-evident in the report, whereas locking the log
    would let a wedged agent block every other writer for the rest of the run.
    Ordering within a single ``O_APPEND`` write is guaranteed by the OS, so no
    entry is ever lost or interleaved mid-line.
    """
    directory = project_dir or resolve_project_dir()
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / LOG_NAME
    if not finding.id:
        finding.id = _next_id(read_log(directory).entries)
    # Single write() of one newline-terminated line: O_APPEND makes that atomic
    # up to PIPE_BUF-sized payloads, which is what keeps concurrent appends from
    # shredding each other's lines.
    with path.open("a", encoding="utf-8") as fh:
        fh.write(finding.to_json() + "\n")
    return finding


def rewrite(entries: list[Finding], project_dir: Path | None = None) -> Path:
    """Replace the log wholesale — used by triage, which re-buckets entries.

    Written to a sibling temp file and renamed so an interrupted triage leaves
    the previous log intact rather than a truncated one.  A log containing an
    unparseable raw line is never rewritten: callers must repair or preserve
    that evidence first, rather than silently replacing the file with only the
    parseable subset.
    """
    path = log_path(project_dir)
    current = read_log(project_dir)
    if current.corrupt_lines:
        raise CorruptFindingsLogError(
            f"refusing to rewrite {path}: {current.corrupt_lines} unparseable "
            "line(s) would be lost"
        )
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".jsonl.tmp")
    tmp.write_text("".join(e.to_json() + "\n" for e in entries), encoding="utf-8")
    tmp.replace(path)
    return path
