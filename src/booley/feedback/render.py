"""Render the user report and the optional outbound feedback view.

One findings log serves two audiences without creating two reports by default:

- ``SETUP-REPORT.md`` / ``FEEDBACK-REPORT.md`` — for the **user**. What their
  project got, what is not covered, what they should act on. Always written,
  never leaves the machine, keeps every detail verbatim. Which of the two names
  it takes follows the log's origin: a setup run owns ``SETUP-REPORT.md``, and a
  log that only ever held ad-hoc bug reports gets the neutral name.
- The outbound feedback view is for **Booley's maintainers**. It contains only
  findings that are Booley's fault, actionable, and not already filed, put
  through :mod:`redact`. Preview and submission render it transiently;
  ``booley feedback export`` is the explicit escape hatch that persists it as
  ``BOOLEY-FEEDBACK.md``.

The user report lives in ``<project_dir>/`` — inside Booley's state dir, never
in the RTL repo's tracked tree (the setup footprint guardrail). An explicit
export lands there by default too. The one exception is the maintainer dogfood
flow, which asks for the user report at a clone's root.
"""

from __future__ import annotations

import platform
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from booley.feedback import redact as redact_mod
from booley.feedback.findings import (
    BUCKET_TITLE,
    Finding,
    FindingsLog,
    read_log,
)
from booley.timefmt import format_human_date, format_human_datetime_safe

USER_REPORT_NAME = "SETUP-REPORT.md"
#: The user report's name when nothing in the log came from a setup run.
BUG_USER_REPORT_NAME = "FEEDBACK-REPORT.md"
#: Default name used only by the explicit ``booley feedback export`` command.
BOOLEY_REPORT_NAME = "BOOLEY-FEEDBACK.md"

#: Order findings are presented in, worst first.
_SEVERITY_ORDER = {"blocker": 0, "workaround": 1, "note": 2}

#: How much of an attached file makes it into a report. The tail, because that
#: is where a failing run's error is; the cap exists so one 200 MB run.log
#: cannot turn a bug report into something nobody can read or post.
MAX_ATTACHMENT_LINES = 120
MAX_ATTACHMENT_CHARS = 8000


def report_origin(log: FindingsLog) -> str:
    """Which flow this log belongs to — ``"setup"``, ``"bug"`` or ``"impression"``.

    A single setup-origin entry makes the whole log a setup log: the project's
    local report was born as ``SETUP-REPORT.md`` and renaming it later, when the
    user files an unrelated bug, would strand the file they already know about.

    ``"impression"`` needs every entry to be one. A log holding one bug and one
    "love the waveform viewer" is a bug log with a nice note in it, and framing
    the whole report as feedback would bury the bug.
    """
    entries = [e for e in log.entries if e.kind != "win"]
    if not entries or any(e.origin == "setup" for e in entries):
        return "setup"
    if all(e.kind == "impression" for e in entries):
        return "impression"
    return "bug"


def user_report_name(origin: str) -> str:
    """File name of the local report for *origin*."""
    return USER_REPORT_NAME if origin == "setup" else BUG_USER_REPORT_NAME


@dataclass
class Environment:
    """The fingerprint every Booley-bound report carries.

    Deliberately small and non-identifying: versions and platform, never paths,
    hostnames, or usernames. It exists so a maintainer can cluster duplicate
    reports, which is the difference between "50 issues" and "6 bugs".
    """

    booley_version: str = ""
    python_version: str = ""
    platform: str = ""
    in_container: bool = False
    doctor_deep_clean: bool | None = None

    def as_rows(self) -> list[tuple[str, str]]:
        doctor = {True: "yes", False: "no", None: "not recorded"}[self.doctor_deep_clean]
        return [
            ("Booley", self.booley_version),
            ("Python", self.python_version),
            ("Platform", self.platform),
            ("Runtime", "Session Runtime container" if self.in_container else "host CLI"),
            ("doctor --deep clean", doctor),
        ]


def collect_environment(project_dir: Path | None = None) -> Environment:
    """Gather the fingerprint, degrading to blanks rather than failing.

    A report is still worth filing from a half-broken environment — arguably
    more so — so every probe here is allowed to come back empty.
    """
    env = Environment(
        python_version=platform.python_version(),
        platform=f"{platform.system()} {platform.machine()}",
        in_container=Path("/.dockerenv").exists(),
    )
    try:
        from booley import __version__

        env.booley_version = __version__
    except ImportError:  # pragma: no cover - defensive
        pass
    if project_dir is not None:
        try:
            from booley.harness import doctor_stamp

            stamp = doctor_stamp.load_stamp(project_dir)
            if stamp is not None:
                env.doctor_deep_clean = bool(stamp.get("deep"))
        except (ImportError, OSError):
            pass
    return env


def _sorted(findings: list[Finding]) -> list[Finding]:
    return sorted(findings, key=lambda f: (_SEVERITY_ORDER.get(f.severity, 9), f.id))


def _fence(body: str) -> str:
    """A code fence long enough to survive whatever backticks *body* contains."""
    longest = 0
    run = 0
    for char in body:
        run = run + 1 if char == "`" else 0
        longest = max(longest, run)
    return "`" * max(3, longest + 1)


def _attachment_block(raw_path: str) -> list[str]:
    """Inline the tail of an attached file, or say why it isn't here.

    Read at *render* time, not at log time: the file goes on being written while
    the run continues, and the report wants its final state. The cost is that a
    file deleted in between renders as a note instead of content — which is the
    honest outcome, and better than silently dropping the reference.
    """
    path = Path(raw_path).expanduser()
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError as e:
        return ["", f"- **Attached:** `{raw_path}` — unreadable at report time ({e})", ""]

    lines = text.splitlines()
    tail = lines[-MAX_ATTACHMENT_LINES:]
    excerpt = "\n".join(tail)
    if len(excerpt) > MAX_ATTACHMENT_CHARS:
        excerpt = excerpt[-MAX_ATTACHMENT_CHARS:]
    clipped = len(tail) < len(lines) or len(excerpt) < len("\n".join(tail))
    label = f"`{path.name}`" + (
        f" — last {len(excerpt.splitlines())} of {len(lines)} lines" if clipped else ""
    )
    fence = _fence(excerpt)
    return ["", f"- **Attached:** {label}", "", fence + "text", excerpt, fence, ""]


def _finding_block(finding: Finding, *, include_evidence: bool = True) -> list[str]:
    """One finding as a Markdown subsection."""
    if finding.kind == "friction":
        kind_tag = " *(friction)*"
    elif finding.kind == "impression":
        kind_tag = f" *(impression: {finding.sentiment or 'mixed'})*"
    else:
        kind_tag = ""
    lines = [f"### {finding.marker} {finding.id} — {finding.title}{kind_tag}", ""]
    meta = [
        ("Exposed by", finding.exposed_by),
        ("Component", finding.component),
        ("Step", finding.step),
    ]
    for label, value in meta:
        if value.strip():
            lines.append(f"- **{label}:** {value.strip()}")
    if include_evidence:
        for label, value in (("Observed", finding.observed), ("Expected", finding.expected)):
            if value.strip():
                lines.append(f"- **{label}:** {value.strip()}")
        if finding.repro.strip():
            lines += [
                "- **Reproduce:**",
                "",
                "  ```console",
                *(f"  {ln}" for ln in finding.repro.strip().splitlines()),
                "  ```",
            ]
    if finding.filed.strip():
        filed_at = format_human_datetime_safe(finding.filed_at, seconds=True)
        lines.append(f"- **Already reported:** {finding.filed.strip()} ({filed_at})")
    if finding.notes.strip():
        lines += ["", finding.notes.strip()]
    for attachment in finding.attachments:
        lines += _attachment_block(attachment)
    lines.append("")
    return lines


def _summary_table(log: FindingsLog) -> list[str]:
    counts = log.counts()
    lines = [
        "| Severity | Count |",
        "| --- | --- |",
        f"| 🔴 Blocker | {counts['blocker']} |",
        f"| 🟡 Workaround | {counts['workaround']} |",
        f"| 🔵 Note | {counts['note']} |",
        "",
    ]
    if log.friction:
        lines += [
            f"Friction reports (confusing, not broken): **{len(log.friction)}**.",
            "",
        ]
    if log.impressions:
        lines += [
            f"Impressions (what you think of Booley): **{len(log.impressions)}**.",
            "",
        ]
    if log.wins:
        lines += [f"Checks that passed first try: **{len(log.wins)}**.", ""]
    if log.corrupt_lines:
        lines += [
            f"> ⚠️ {log.corrupt_lines} log line(s) could not be parsed and are missing "
            f"from this report (see `{log.path.name}`).",
            "",
        ]
    return lines


def render_user_report(
    log: FindingsLog,
    *,
    project_name: str = "",
    env: Environment | None = None,
    origin: str = "",
) -> str:
    """The local, unredacted report for the project owner.

    Ordered by what the reader can act on: their own findings first, then the
    Booley-side ones (so they know what was *not* their fault), then the honest
    negatives — untriaged entries and what setup never covered.
    """
    env = env or collect_environment()
    origin = origin or report_origin(log)
    when = format_human_date(datetime.now(UTC).astimezone())
    kind_word = "setup" if origin == "setup" else "feedback"
    title = f"Booley {kind_word} report"
    if project_name:
        title += f" — {project_name}"
    source = (
        "the log setup appended to as it ran"
        if origin == "setup"
        else "everything logged against Booley on this project"
    )
    out = [
        f"# {title}",
        "",
        f"Generated {when} from `{log.path.name}`, {source}.",
        "This file is **local**: it lives in Booley's state directory and is never",
        "published anywhere. Nothing is sent off this machine without you agreeing to it.",
        "",
        "## Summary",
        "",
        *_summary_table(log),
    ]

    for bucket in ("project", "booley", "docs", "unknown"):
        # Impressions get their own section below: they are bucketed as Booley's
        # so they ride the same outgoing path, but listing "I wish it had X"
        # among the defects would read as one more thing that is broken.
        items = _sorted([f for f in log.by_bucket(bucket) if f.kind != "impression"])
        if not items:
            continue
        out += [f"## {BUCKET_TITLE[bucket]}", ""]
        if bucket == "project":
            out += [
                "Things to fix in your repo, your config, or your environment.",
                "Setup could not resolve these for you."
                if origin == "setup"
                else "Booley behaved correctly here; the fix is on your side.",
                "",
            ]
        elif bucket == "booley":
            out += [
                "Booley's problem, not yours. Where a workaround is in place, it is noted",
                "in the finding. These are candidates for the transient redacted view shown",
                "by `booley feedback preview` (or an explicit `feedback export`).",
                "",
            ]
        elif bucket == "docs":
            out += ["Booley's documentation says one thing and its code does another.", ""]
        else:
            out += [
                "Logged but never triaged — nobody decided whose problem these are.",
                "Treat the list as unfinished business, not as a clean bill of health.",
                "",
            ]
        for finding in items:
            out += _finding_block(finding)

    if log.impressions:
        out += [
            "## What you told Booley",
            "",
            "Your own words about Booley — nothing broken here, just what you think.",
            "These go upstream with the rest only if you send the report.",
            "",
        ]
        for impression in log.impressions:
            out += _finding_block(impression)

    if log.wins:
        out += ["## What went right", ""]
        out += [
            f"- {win.title}" + (f" ({win.exposed_by})" if win.exposed_by.strip() else "")
            for win in log.wins
        ]
        out += [""]

    out += [
        "## Environment",
        "",
        "| | |",
        "| --- | --- |",
        *(f"| {label} | {value} |" for label, value in env.as_rows()),
        "",
    ]
    return "\n".join(out).rstrip() + "\n"


@dataclass
class BooleyReport:
    """A rendered Booley-facing report and everything the consent prompt needs."""

    body: str
    filable: list[Finding]
    withheld: list[Finding]
    redaction_hits: dict[str, int]
    risks: list[str]
    mapping: dict[str, str]
    origin: str = "setup"

    @property
    def has_content(self) -> bool:
        return bool(self.filable)

    @property
    def tag(self) -> str:
        """Issue-title prefix: what a maintainer should expect before reading.

        An all-friction batch is tagged ``[ux]`` whatever the origin — it says
        "nothing is broken here" up front, which is exactly the triage signal
        that keeps friction reports from being read as failed bug reports. An
        all-impression batch is ``[feedback]`` for the same reason, one step
        further: there is not even a confusion to fix, and a maintainer who
        opens it expecting a defect has been mislabelled to.
        """
        if not self.filable:
            return "setup" if self.origin == "setup" else "bug"
        if all(f.kind == "impression" for f in self.filable):
            return "feedback"
        if all(f.kind == "friction" for f in self.filable):
            return "ux"
        return "setup" if self.origin == "setup" else "bug"

    @property
    def label(self) -> str:
        """GitHub label to file under, when the repo has one."""
        return "setup-feedback" if self.origin == "setup" else "user-feedback"

    def issue_title(self) -> str:
        """A GitHub issue title for the whole batch.

        One issue per batch, not per finding: the entries share an environment
        and often a root cause, and splitting them loses that.
        """
        if len(self.filable) == 1:
            return f"[{self.tag}] {self.filable[0].title}"
        if self.tag == "feedback":
            return f"[feedback] {len(self.filable)} impressions from a Booley user"
        blockers = sum(1 for f in self.filable if f.severity == "blocker")
        suffix = f", {blockers} blocking" if blockers else ""
        where = "a project setup run" if self.origin == "setup" else "normal use"
        return f"[{self.tag}] {len(self.filable)} findings from {where}{suffix}"


def render_booley_report(
    log: FindingsLog,
    project_root: Path,
    *,
    project_dir: Path | None = None,
    env: Environment | None = None,
    plan: redact_mod.RedactionPlan | None = None,
    origin: str = "",
) -> BooleyReport:
    """Build the redacted, maintainer-facing report.

    Only ``booley``/``docs`` entries that clear
    :meth:`~booley.feedback.findings.Finding.is_filable` get in. Everything else
    is withheld and named in the return value, so the triage step can tell the
    user *why* something they reported isn't in the outgoing file instead of
    silently dropping it. Entries already filed are neither: they are simply
    gone, having been reported once already.
    """
    env = env or collect_environment(project_dir)
    plan = plan if plan is not None else redact_mod.build_plan(project_root, project_dir)
    origin = origin or report_origin(log)

    candidates = [
        f for f in log.findings if f.bucket in ("booley", "docs") and not f.filed.strip()
    ]
    filable = _sorted([f for f in candidates if f.is_filable()])
    withheld = _sorted([f for f in candidates if not f.is_filable()])

    all_impressions = bool(filable) and all(f.kind == "impression" for f in filable)
    if all_impressions:
        what_happened = [
            f"A Booley user sent {len(filable)} impression(s) of Booley — what they like,",
            "dislike, or wish existed. No bug is claimed here and nothing is broken;",
            "this was reported through the `booley-feedback` skill, with project",
            "identifiers redacted (see the note at the end).",
        ]
    elif origin == "setup":
        what_happened = [
            f"A `booley-setup` run on a private RTL project produced {len(filable)} finding(s)",
            "attributable to Booley. Reported through the setup skill's triage step,",
            "with project identifiers redacted (see the note at the end).",
        ]
    else:
        what_happened = [
            f"A Booley user hit {len(filable)} issue(s) during normal use on a private RTL",
            "project. Reported through the `booley-feedback` skill, with project",
            "identifiers redacted (see the note at the end).",
        ]

    out = [
        "## What happened",
        "",
        *what_happened,
        "",
        "## Environment",
        "",
        "| | |",
        "| --- | --- |",
        *(f"| {label} | {value} |" for label, value in env.as_rows()),
        "",
        "## What they said" if all_impressions else "## Findings",
        "",
    ]
    for finding in filable:
        out += _finding_block(finding)
        # Friction and impressions are *definitionally* the reporter's
        # experience, so the caveat says nothing there; on a bug claim it is the
        # difference between "I saw this" and "I read the code and it is wrong".
        if finding.kind == "finding" and not finding.verified_against_source:
            out += [
                "> Not verified against Booley's source — this is the reporter's "
                "observation, not a diagnosis.",
                "",
            ]

    out += [
        "---",
        "",
        "Project identifiers (paths, remotes, module and Target names) were replaced",
        "with placeholders by the feedback workflow before this report was shown to",
        "the reporter for approval. EDA-tool names, versions, and error text were kept — a",
        "report without them is not actionable. Ask if you need a detail that was",
        "scrubbed; the reporter holds the unredacted original.",
        "",
    ]

    body = "\n".join(out).rstrip() + "\n"
    redacted_body, hits = redact_mod.apply_plan(body, plan)
    return BooleyReport(
        body=redacted_body,
        filable=filable,
        withheld=withheld,
        redaction_hits=hits,
        risks=redact_mod.residual_risks(redacted_body, plan),
        mapping=plan.mapping(),
        origin=origin,
    )


def write_user_report(
    project_root: Path,
    project_dir: Path,
    *,
    project_name: str = "",
    user_report_path: Path | None = None,
) -> tuple[Path, BooleyReport]:
    """Write the one persistent user report and render outbound feedback in memory.

    Returns the user-report path and the transient :class:`BooleyReport` used by
    the CLI to explain what is filable. No redacted companion file is written.

    Args:
        user_report_path: Override for the user report's location. The dogfood
            flow points this at a throwaway clone's root; a real project must
            leave it alone so the report stays out of the tracked tree.
    """
    log = read_log(project_dir)
    env = collect_environment(project_dir)
    origin = report_origin(log)

    user_path = user_report_path or (project_dir / user_report_name(origin))
    user_path.parent.mkdir(parents=True, exist_ok=True)
    user_path.write_text(
        render_user_report(log, project_name=project_name, env=env, origin=origin),
        encoding="utf-8",
    )

    report = render_booley_report(
        log, project_root, project_dir=project_dir, env=env, origin=origin
    )
    return user_path, report


def export_booley_report(report: BooleyReport, output_path: Path) -> Path:
    """Persist an explicitly requested redacted outbound report.

    Preview and submission do not call this function. Keeping export separate is
    what makes a normal setup produce one report while retaining a manual-share
    path for users who want a sanitized Markdown file.
    """
    if not report.has_content:
        raise ValueError("cannot export an empty Booley feedback report")
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(report.body, encoding="utf-8")
    return output_path
