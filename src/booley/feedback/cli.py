"""``booley feedback`` — the findings log, its reports, and the opt-in bug report.

Split from the skill on purpose. Anything an agent could get subtly wrong on a
path that ends in a public URL lives here as tested code: which findings are
filable, what gets redacted, whether submission is even allowed. The skill's job
shrinks to calling these subcommands and talking to the user.

    booley feedback add --title … --severity … --repro …   # something broke
    booley feedback friction --title … --expected …        # nothing broke, but…
    booley feedback say "…" --sentiment wish               # what you think of Booley
    booley feedback win --title …                          # a check that passed
    booley feedback triage F-3 --bucket booley             # whose problem is it
    booley feedback list                                   # what's logged so far
    booley feedback report                                 # render the user report
    booley feedback preview F-3                            # consent text for this batch
    booley feedback export F-3                             # optional redacted Markdown
    booley feedback submit F-3 --yes                       # host only, after preview
                                                           # (issue, or mail per [feedback] mode)
    booley feedback filed F-3 --url …                      # posted it by hand
    booley feedback redact --file notes.md                 # scrub anything by hand
"""

from __future__ import annotations

import argparse
import json
import sys
import tempfile
from collections.abc import Callable
from pathlib import Path

from booley.feedback import redact as redact_mod
from booley.feedback import render
from booley.feedback import submit as submit_mod
from booley.feedback.findings import (
    BUCKETS,
    KINDS,
    ORIGINS,
    SENTIMENTS,
    SEVERITIES,
    Finding,
    FindingsLog,
    append,
    read_log,
    rewrite,
)
from booley.feedback.storage import feedback_storage_dir


def _project_dir(project_root: Path) -> Path:
    """Return Project feedback state or source-checkout dogfood state."""
    return feedback_storage_dir(project_root)


def add_subparser(sub: argparse._SubParsersAction) -> None:
    """Register ``booley feedback`` and its subcommands."""
    parser = sub.add_parser(
        "feedback",
        help="Log setup/port findings, say what you think of Booley, render the "
        "reports, optionally report a bug",
    )
    fb = parser.add_subparsers(dest="feedback_command")
    _add_logging_subcommands(fb)
    _add_reporting_subcommands(fb)


def _add_shared_arguments(parser: argparse.ArgumentParser) -> None:
    """Arguments every log-writing subcommand takes."""
    parser.add_argument(
        "--origin",
        choices=ORIGINS,
        default="setup",
        help="Which flow logged this: a setup run, an ad-hoc bug report (`bug`), "
        "or an unprompted impression (`impression`)",
    )
    parser.add_argument(
        "--attach",
        action="append",
        default=[],
        metavar="PATH",
        dest="attach",
        help="File whose tail gets inlined into the reports — a run log, a doctor "
        "transcript. Read at report time and redacted with the rest of the body. "
        "Repeatable.",
    )


def _add_friction_subcommand(fb: argparse._SubParsersAction) -> None:
    """``friction`` — the "nothing broke, but…" report.

    A separate subcommand rather than a flag on ``add`` because the two ask for
    different things: ``add`` wants a reproduction, this wants somewhere to aim
    the fix and what the reporter expected instead.
    """
    friction_p = fb.add_parser(
        "friction",
        help="Log something confusing rather than broken (no reproduction needed)",
    )
    friction_p.add_argument("--title", required=True, help="What was confusing, in one line")
    friction_p.add_argument(
        "--component",
        default="",
        help="Booley subsystem or doc the confusion is about — where a fix would go",
    )
    friction_p.add_argument("--exposed-by", default="", help="What you were doing at the time")
    friction_p.add_argument(
        "--expected", default="", help="What you expected instead — the actionable half"
    )
    friction_p.add_argument("--notes", default="", help="Free-form detail")
    friction_p.add_argument("--step", default="")
    friction_p.add_argument("--severity", choices=SEVERITIES, default="note")
    friction_p.add_argument(
        "--bucket",
        choices=BUCKETS,
        default="booley",
        help="Defaults to booley: friction is by definition about Booley's own surface",
    )
    _add_shared_arguments(friction_p)


def _add_say_subcommand(fb: argparse._SubParsersAction) -> None:
    """``say`` — what the user thinks of Booley, with no evidence bar at all.

    Deliberately the cheapest subcommand in the family: one positional message
    and nothing else required. ``add`` and ``friction`` both interrogate the
    reporter (reproduce it, aim it, say what you expected), which is right for a
    defect and fatal for an opinion — nobody types four flags to say "the
    waveform flow is great, the setup grill is too long". Everything past the
    message is optional metadata for whoever reads the batch.
    """
    say_p = fb.add_parser(
        "say",
        help="Tell Booley's maintainers what you think — praise, gripes, feature wishes",
    )
    say_p.add_argument(
        "message",
        help="What you want to say, in one line. That alone is a complete report.",
    )
    say_p.add_argument(
        "--sentiment",
        choices=SENTIMENTS,
        default="mixed",
        help="What it is about: something you liked (praise), disliked (gripe), "
        "want (wish), or a general take (mixed, the default)",
    )
    say_p.add_argument(
        "--component",
        default="",
        help="The part of Booley it is about, if it is about one (sim, docs, setup)",
    )
    say_p.add_argument("--notes", default="", help="The longer version, if you have one")
    say_p.add_argument("--step", default="", help="What you were doing at the time")
    _add_shared_arguments(say_p)
    # An impression is its own provenance: it came from neither a setup run nor a
    # bug report, and mislabelling it as either would skew what the reports say
    # the batch is.
    say_p.set_defaults(origin="impression")


def _add_logging_subcommands(fb: argparse._SubParsersAction) -> None:
    """The write side: what setup steps and sub-agents call as they run."""
    add_p = fb.add_parser("add", help="Append a finding to the log")
    add_p.add_argument("--title", required=True, help="One-line description")
    add_p.add_argument("--severity", choices=SEVERITIES, default="note")
    add_p.add_argument(
        "--bucket",
        choices=BUCKETS,
        default="unknown",
        help="Whose problem it is; leave unset for the triage step to decide",
    )
    add_p.add_argument("--exposed-by", default="", help="The exact check that surfaced it")
    add_p.add_argument("--step", default="", help="Setup step or port phase")
    add_p.add_argument("--component", default="", help="Booley subsystem or doc path")
    add_p.add_argument("--repro", default="", help="Command that reproduces it, verbatim")
    add_p.add_argument("--observed", default="", help="What actually happened")
    add_p.add_argument("--expected", default="", help="What should have happened")
    add_p.add_argument("--notes", default="", help="Free-form detail")
    add_p.add_argument(
        "--verified-against-source",
        action="store_true",
        help="Set once you have confirmed the claim against Booley's own source",
    )
    _add_shared_arguments(add_p)

    _add_friction_subcommand(fb)
    _add_say_subcommand(fb)

    win_p = fb.add_parser("win", help="Record a check that passed first try")
    win_p.add_argument("--title", required=True)
    win_p.add_argument("--exposed-by", default="")
    win_p.add_argument("--step", default="")

    triage_p = fb.add_parser("triage", help="Re-bucket or re-grade a logged finding")
    triage_p.add_argument("finding_id", metavar="F-N", help="Finding to update")
    triage_p.add_argument("--bucket", choices=BUCKETS)
    triage_p.add_argument("--severity", choices=SEVERITIES)
    triage_p.add_argument("--component")
    triage_p.add_argument("--repro")
    triage_p.add_argument("--observed")
    triage_p.add_argument("--expected")
    triage_p.add_argument("--notes")
    triage_p.add_argument("--verified-against-source", action="store_true")
    triage_p.add_argument(
        "--attach",
        action="append",
        default=[],
        metavar="PATH",
        help="Attach evidence to an already-logged finding. Repeatable; adds to "
        "whatever is already attached.",
    )

    filed_p = fb.add_parser(
        "filed",
        help="Mark findings as already reported upstream, so they are never re-filed",
    )
    filed_p.add_argument("finding_ids", metavar="F-N", nargs="+", help="Findings to stamp")
    filed_p.add_argument(
        "--url",
        default="manual",
        help="Issue URL, or 'manual' when it was posted some other way (the default)",
    )


def _add_reporting_subcommands(fb: argparse._SubParsersAction) -> None:
    """The read side: inspect the log, render/export views, offer feedback."""
    list_p = fb.add_parser("list", help="Show the findings logged so far")
    list_p.add_argument("--json", action="store_true", help="Machine-readable output")
    list_p.add_argument("--bucket", choices=BUCKETS, help="Only this bucket")
    list_p.add_argument("--kind", choices=KINDS, help="Only findings, or only wins")

    report_p = fb.add_parser("report", help="Render the one local user report")
    report_p.add_argument("--project-name", default="", help="Name for the report title")
    report_p.add_argument(
        "--user-report-path",
        metavar="PATH",
        help="Write the user report here instead of the project dir "
        "(maintainer dogfood runs only — a real project keeps it out of the tracked tree)",
    )

    preview_p = fb.add_parser(
        "preview", help="Print the exact transient text that would go upstream"
    )
    _add_finding_selection(preview_p)

    export_p = fb.add_parser("export", help="Explicitly write the redacted outbound Markdown")
    _add_finding_selection(export_p)
    export_p.add_argument(
        "--output",
        metavar="PATH",
        help="Write here instead of the feedback state directory",
    )

    submit_p = fb.add_parser(
        "submit",
        help="Send the redacted report upstream — a public GitHub issue, or a mail to "
        'the maintainer when [feedback] mode = "email"',
    )
    _add_finding_selection(submit_p)
    submit_p.add_argument(
        "--yes",
        action="store_true",
        help="Assert that the user agreed to send it. Required, and not sufficient — "
        "see --confirm.",
    )
    submit_p.add_argument(
        "--confirm",
        default="",
        metavar="TOKEN",
        help="The token printed by `booley feedback preview`. Proves the approval "
        "covers the exact text on disk.",
    )
    submit_p.add_argument(
        "--dry-run",
        action="store_true",
        help="Show what would be filed and stop. Use this to rehearse the path.",
    )
    submit_p.add_argument("--repo", default=submit_mod.UPSTREAM_REPO, help=argparse.SUPPRESS)

    redact_p = fb.add_parser("redact", help="Scrub project identifiers out of a file or stdin")
    redact_p.add_argument("--file", metavar="PATH", help="File to scrub (default: stdin)")
    redact_p.add_argument(
        "--show-mapping",
        action="store_true",
        help="Also print the secret→placeholder table (LOCAL ONLY — never publish it)",
    )


def _add_finding_selection(parser: argparse.ArgumentParser) -> None:
    """Add the outbound batch selector shared by preview/export/submit."""
    parser.add_argument(
        "finding_ids",
        metavar="F-N",
        nargs="*",
        help="Only include these findings.",
    )
    parser.add_argument(
        "--all",
        action="store_true",
        dest="all_findings",
        help="Intentionally include every pending finding.",
    )


def _warn_missing_evidence(finding: Finding) -> None:
    """Nag for the evidence a bug report needs, at log time.

    Said now rather than at report time: the person who can still get it is the
    one standing in front of the failure.
    """
    missing = finding.missing_evidence()
    if not missing or finding.bucket not in ("booley", "docs", "unknown"):
        return
    # "unknown" is nagged too: most untriaged entries end up in a Booley bucket,
    # and by the time triage says so the failure is no longer on screen.
    sys.stdout.flush()  # keep the stderr nag below the line it refers to
    print(
        f"  note: no {', '.join(missing)} — a Booley bug report needs these, "
        "so add them while you can still reproduce it.",
        file=sys.stderr,
    )


def _cmd_add(args: argparse.Namespace, project_dir: Path) -> int:
    finding = append(
        Finding(
            title=args.title,
            severity=args.severity,
            bucket=args.bucket,
            exposed_by=args.exposed_by,
            step=args.step,
            component=args.component,
            repro=args.repro,
            observed=args.observed,
            expected=args.expected,
            notes=args.notes,
            verified_against_source=args.verified_against_source,
            origin=args.origin,
            attachments=list(args.attach),
        ),
        project_dir,
    )
    print(f"{finding.id} logged ({finding.marker} {finding.severity}, bucket={finding.bucket})")
    _warn_missing_evidence(finding)
    return 0


def _cmd_friction(args: argparse.Namespace, project_dir: Path) -> int:
    """Log "this was confusing" — no reproduction demanded, evidence bar of its own."""
    finding = append(
        Finding(
            title=args.title,
            kind="friction",
            severity=args.severity,
            bucket=args.bucket,
            exposed_by=args.exposed_by,
            step=args.step,
            component=args.component,
            expected=args.expected,
            notes=args.notes,
            origin=args.origin,
            attachments=list(args.attach),
        ),
        project_dir,
    )
    print(f"{finding.id} logged ({finding.marker} friction, bucket={finding.bucket})")
    _warn_missing_evidence(finding)
    return 0


def _cmd_say(args: argparse.Namespace, project_dir: Path) -> int:
    """Log an impression — no evidence demanded, nothing to get wrong."""
    finding = append(
        Finding(
            title=args.message,
            kind="impression",
            # Bucketed as Booley's by construction: an opinion about Booley is
            # not the project's problem to fix, and leaving it "unknown" would
            # park it in the untriaged pile nobody sends anywhere.
            bucket="booley",
            sentiment=args.sentiment,
            component=args.component,
            notes=args.notes,
            step=args.step,
            origin=args.origin,
            attachments=list(args.attach),
        ),
        project_dir,
    )
    print(f"{finding.id} logged ({finding.marker} {finding.sentiment}). Thanks — that helps.")
    print(
        "It goes upstream only if you send it: `booley feedback preview`, then "
        "`submit` from the host."
    )
    return 0


def _cmd_filed(args: argparse.Namespace, project_dir: Path) -> int:
    """Stamp findings as already reported, so no later batch re-publishes them."""
    log = read_log(project_dir)
    if log.corrupt_lines:
        return _refuse_lossy_rewrite(log)
    by_id = {e.id: e for e in log.entries}
    missing = [fid for fid in args.finding_ids if fid not in by_id]
    if missing:
        print(f"No finding(s) {', '.join(missing)} in {log.path}", file=sys.stderr)
        return 1
    for finding_id in args.finding_ids:
        by_id[finding_id].mark_filed(args.url)
    rewrite(log.entries, project_dir)
    print(f"Marked as filed ({args.url}): {', '.join(args.finding_ids)}")
    return 0


def _cmd_win(args: argparse.Namespace, project_dir: Path) -> int:
    finding = append(
        Finding(title=args.title, kind="win", exposed_by=args.exposed_by, step=args.step),
        project_dir,
    )
    print(f"{finding.id} logged (win)")
    return 0


def _cmd_triage(args: argparse.Namespace, project_dir: Path) -> int:
    log = read_log(project_dir)
    if log.corrupt_lines:
        return _refuse_lossy_rewrite(log)
    target = next((e for e in log.entries if e.id == args.finding_id), None)
    if target is None:
        print(f"No finding {args.finding_id} in {log.path}", file=sys.stderr)
        return 1
    for field_name in (
        "bucket",
        "severity",
        "component",
        "repro",
        "observed",
        "expected",
        "notes",
    ):
        value = getattr(args, field_name, None)
        if value is not None:
            setattr(target, field_name, value)
    if args.verified_against_source:
        target.verified_against_source = True
    # Attachments accumulate rather than replace: triage is where a second
    # transcript gets added to a finding, never where one gets dropped.
    target.attachments += [a for a in args.attach if a not in target.attachments]
    rewrite(log.entries, project_dir)
    print(f"{target.id} → bucket={target.bucket}, severity={target.severity}")
    return 0


def _refuse_lossy_rewrite(log: object) -> int:
    """Explain why a mutating feedback command cannot safely continue."""
    count = getattr(log, "corrupt_lines", 0)
    path = getattr(log, "path", "findings.jsonl")
    print(
        f"Error: refusing to rewrite {path}: {count} unparseable line(s) "
        "would be lost. Repair or preserve those raw lines first.",
        file=sys.stderr,
    )
    return 2


def _cmd_list(args: argparse.Namespace, project_dir: Path) -> int:
    log = read_log(project_dir)
    entries = log.entries
    if args.kind:
        entries = [e for e in entries if e.kind == args.kind]
    if args.bucket:
        entries = [e for e in entries if e.bucket == args.bucket]
    if args.json:
        print(json.dumps([e.__dict__ for e in entries], indent=2))
        return 0
    if not entries:
        print(f"Nothing logged yet ({log.path}).")
        return 0
    for entry in entries:
        if entry.kind == "win":
            print(f"{entry.id:6} ✅ win        {entry.title}")
            continue
        if entry.filed.strip():
            flag = f"  [filed: {entry.filed.strip()}]"
        elif entry.is_filable() or entry.bucket in ("project", "unknown"):
            flag = ""
        else:
            flag = "  [needs evidence]"
        print(f"{entry.id:6} {entry.marker} {entry.bucket:9} {entry.title}{flag}")
    counts = log.counts()
    # Bugs, friction, impressions and wins counted apart: the severity markers
    # describe bugs only, so folding the others into that total makes the
    # numbers not add up.
    print(
        f"\n{len(log.bugs)} finding(s): "
        f"🔴 {counts['blocker']}  🟡 {counts['workaround']}  🔵 {counts['note']}"
        f"   |   💬 {len(log.friction)} friction   |   📣 {len(log.impressions)} impression(s)"
        f"   |   {len(log.wins)} win(s)"
    )
    if log.corrupt_lines:
        print(f"⚠️  {log.corrupt_lines} unparseable line(s) skipped", file=sys.stderr)
    return 0


def _cmd_report(args: argparse.Namespace, project_root: Path, project_dir: Path) -> int:
    override = Path(args.user_report_path).resolve() if args.user_report_path else None
    user_path, report = render.write_user_report(
        project_root, project_dir, project_name=args.project_name, user_report_path=override
    )
    print(f"Wrote {user_path}")
    if not report.has_content:
        print(
            "No Booley-attributable findings with enough evidence to file — "
            "nothing to offer upstream."
        )
    else:
        print(
            f"{len(report.filable)} filable finding(s) available to preview; "
            "no second report was written."
        )
        print(f"  redaction: {redact_mod.diff_summary(report.redaction_hits)}")
    old_export = project_dir / render.BOOLEY_REPORT_NAME
    if old_export.is_file():
        print(
            f"\nExisting redacted export not updated: {old_export}\n"
            "It may be stale; run `booley feedback export` to refresh it or remove it."
        )
    if report.withheld:
        print(
            f"\nWithheld from the Booley report for lack of evidence "
            f"({len(report.withheld)}): "
            + ", ".join(
                f"{f.id} (needs {', '.join(f.missing_evidence()) or 'a component'})"
                for f in report.withheld
            )
        )
    if report.has_content:
        check = submit_mod.preflight(project_dir)
        if not check.ok:
            print(f"\nSubmission not available here: {check.reason}")
    return 0


class _UnknownFindingIdsError(ValueError):
    """An outbound batch named findings that do not exist in this project."""

    def __init__(self, finding_ids: list[str], path: Path):
        self.finding_ids = finding_ids
        super().__init__(f"No finding(s) {', '.join(finding_ids)} in {path}")


def _select_findings(log: FindingsLog, finding_ids: list[str]) -> FindingsLog:
    """Return the requested outbound batch after CLI boundary validation."""
    by_id = {entry.id: entry for entry in log.entries}
    missing = list(dict.fromkeys(fid for fid in finding_ids if fid not in by_id))
    if missing:
        raise _UnknownFindingIdsError(missing, log.path)
    selected = [by_id[fid] for fid in dict.fromkeys(finding_ids)]
    return FindingsLog(path=log.path, entries=selected, corrupt_lines=log.corrupt_lines)


def _outbound_report(
    project_root: Path,
    project_dir: Path,
    finding_ids: list[str],
    *,
    all_findings: bool,
) -> tuple[FindingsLog, render.BooleyReport] | None:
    """Read, validate, select, and render one outbound batch."""
    log = read_log(project_dir)
    if bool(finding_ids) == all_findings:
        choice = "finding IDs or --all, not both" if finding_ids else "finding IDs or --all"
        print(f"Error: choose {choice} for the outbound batch.", file=sys.stderr)
        return None
    try:
        selected = log if all_findings else _select_findings(log, finding_ids)
    except _UnknownFindingIdsError as error:
        print(f"Error: {error}", file=sys.stderr)
        return None
    report = render.render_booley_report(selected, project_root, project_dir=project_dir)
    return log, report


def _cmd_export(args: argparse.Namespace, project_root: Path, project_dir: Path) -> int:
    prepared = _outbound_report(
        project_root, project_dir, args.finding_ids, all_findings=args.all_findings
    )
    if prepared is None:
        return 1
    _, report = prepared
    if not report.has_content:
        print("Nothing filable — nothing to export.")
        return 0
    output = (
        Path(args.output).resolve() if args.output else project_dir / render.BOOLEY_REPORT_NAME
    )
    path = render.export_booley_report(report, output)
    print(f"Wrote {path} ({len(report.filable)} filable finding(s), redacted)")
    print(f"  redaction: {redact_mod.diff_summary(report.redaction_hits)}")
    return 0


def _cmd_preview(args: argparse.Namespace, project_root: Path, project_dir: Path) -> int:
    prepared = _outbound_report(
        project_root, project_dir, args.finding_ids, all_findings=args.all_findings
    )
    if prepared is None:
        return 1
    _, report = prepared
    if not report.has_content:
        print("Nothing filable — nothing to preview.")
        return 0
    print(
        submit_mod.preview(
            report.body,
            report.risks,
            redact_mod.diff_summary(report.redaction_hits),
            # The consent text names the destination, so it has to read the same
            # config `submit` will: previewing a public issue and then mailing it
            # (or the reverse) is consent for the wrong thing.
            route=submit_mod.route_for(submit_mod.read_mode(project_dir)),
            finding_ids=args.finding_ids,
            all_findings=args.all_findings,
        )
    )
    return 0


def _cmd_submit(args: argparse.Namespace, project_root: Path, project_dir: Path) -> int:
    prepared = _outbound_report(
        project_root, project_dir, args.finding_ids, all_findings=args.all_findings
    )
    if prepared is None:
        return 1
    log, report = prepared
    if log.corrupt_lines:
        return _refuse_lossy_rewrite(log)
    if not report.has_content:
        print("Nothing filable — nothing to submit.")
        return 0
    with tempfile.TemporaryDirectory(prefix="booley-feedback-") as temp_dir:
        body_path = Path(temp_dir) / render.BOOLEY_REPORT_NAME
        body_path.write_text(report.body, encoding="utf-8")
        outcome = submit_mod.submit(
            report.issue_title(),
            body_path,
            project_dir,
            approved=args.yes,
            confirm=args.confirm,
            dry_run=args.dry_run,
            repo=args.repo,
            label=report.label,
        )
    print(outcome.message)
    if outcome.posted:
        # Stamp what just went out, so the next report on this project — a bug
        # hit months later — starts from an empty batch instead of re-publishing
        # this one. Done here rather than in submit() because the log is the
        # CLI's business, and a failed stamp must not un-file a live issue.
        for finding in report.filable:
            finding.mark_filed(outcome.url or "manual")
        rewrite(log.entries, project_dir)
        print(f"Marked {len(report.filable)} finding(s) as filed; they will not be sent again.")
    elif outcome.handed_off:
        # Nothing is stamped here: Booley handed the user a mailto: link and has
        # no way of knowing whether they ever hit send. Stamping optimistically
        # would bury the batch — never re-offered, never actually reported.
        ids = " ".join(f.id for f in report.filable)
        print(
            f"\nOnce you have actually sent it, run:\n"
            f"    booley feedback filed {ids} --url email\n"
            "Nothing is marked as filed until you do, so an unsent mail is not lost."
        )
    # Not-posted is a legitimate outcome (declined, disabled, wrong runtime, no gh,
    # dry run, or an email hand-off) and the message always names the manual
    # route — not an error. A *rejected* approval is different: the caller asked
    # to send and it did not happen, so exit non-zero rather than let a script
    # read that as success.
    if outcome.posted or outcome.handed_off or not args.yes or args.dry_run:
        return 0
    return 1


def _cmd_redact(args: argparse.Namespace, project_root: Path, project_dir: Path) -> int:
    text = Path(args.file).read_text(encoding="utf-8") if args.file else sys.stdin.read()
    plan = redact_mod.build_plan(project_root, project_dir)
    redacted, hits = redact_mod.apply_plan(text, plan)
    sys.stdout.write(redacted)
    # Flush before the stderr commentary: piped stdout is block-buffered, and
    # without this the summary lands *above* the text it summarizes.
    sys.stdout.flush()
    print(f"\n--- {redact_mod.diff_summary(hits)} ---", file=sys.stderr)
    if args.show_mapping:
        print("--- LOCAL ONLY, do not publish ---", file=sys.stderr)
        for secret, placeholder in sorted(plan.mapping().items()):
            print(f"  {placeholder} ← {secret}", file=sys.stderr)
    return 0


# Subcommand → handler. Every handler takes (args, project_root, project_dir) so
# the dispatch stays a table rather than a ladder of ifs; handlers that don't need
# a value simply ignore it.
_HANDLERS: dict[str, Callable[[argparse.Namespace, Path, Path], int]] = {
    "add": lambda args, _root, pdir: _cmd_add(args, pdir),
    "friction": lambda args, _root, pdir: _cmd_friction(args, pdir),
    "say": lambda args, _root, pdir: _cmd_say(args, pdir),
    "win": lambda args, _root, pdir: _cmd_win(args, pdir),
    "triage": lambda args, _root, pdir: _cmd_triage(args, pdir),
    "filed": lambda args, _root, pdir: _cmd_filed(args, pdir),
    "list": lambda args, _root, pdir: _cmd_list(args, pdir),
    "report": _cmd_report,
    "preview": _cmd_preview,
    "export": _cmd_export,
    "submit": _cmd_submit,
    "redact": _cmd_redact,
}


def run(args: argparse.Namespace, project_root: Path) -> int:
    """Dispatch ``booley feedback <subcommand>``."""
    handler = _HANDLERS.get(getattr(args, "feedback_command", None))
    if handler is None:
        print(
            f"Usage: booley feedback {{{'|'.join(_HANDLERS)}}}\n       booley feedback --help",
            file=sys.stderr,
        )
        return 2
    return handler(args, project_root, _project_dir(project_root))
