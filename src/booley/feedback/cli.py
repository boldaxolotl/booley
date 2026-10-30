"""``booley feedback`` — the findings log and its local reports.

Split from the skill on purpose. The tested code decides which findings are
exportable and what gets redacted. Booley never transmits the result; the
skill's job shrinks to calling these subcommands and talking to the user.

    booley feedback add --title … --severity … --repro …   # something broke
    booley feedback friction --title … --expected …        # nothing broke, but…
    booley feedback say "…" --sentiment wish               # what you think of Booley
    booley feedback win --title …                          # a check that passed
    booley feedback triage F-3 --bucket booley             # whose problem is it
    booley feedback list                                   # what's logged so far
    booley feedback report                                 # render the user report
    booley feedback export F-3                             # optional redacted Markdown
    booley feedback filed F-3 --url …                      # posted it by hand
    booley feedback redact --file notes.md                 # scrub anything by hand
"""

from __future__ import annotations

import argparse
import json
import sys
from collections.abc import Callable, Sequence
from pathlib import Path

from booley.core.boundary import BoundaryError
from booley.feedback import redact as redact_mod
from booley.feedback import render
from booley.feedback.findings import (
    BUCKETS,
    KINDS,
    ORIGINS,
    SENTIMENTS,
    SEVERITIES,
    AmbiguousFindingIdError,
    CorruptFindingsLogError,
    Finding,
    FindingsLockTimeoutError,
    FindingsLog,
    UnknownFindingIdError,
    append,
    read_log,
    rewrite,
    select_unique,
)
from booley.feedback.storage import feedback_storage_dir

#: Renders ``feedback list`` rows (given the whole log for its totals) as text.
ListView = Callable[[FindingsLog, Sequence[Finding]], str]


def add_subparser(sub: argparse._SubParsersAction) -> None:
    """Register ``booley feedback`` and its subcommands."""
    parser = sub.add_parser(
        "feedback",
        help="Log setup/port findings, say what you think of Booley, render the "
        "reports, optionally report a bug",
        description=(
            "In the Sandbox, a Project's worktrees share one Findings Log and the "
            "default local report in the Main Booley Directory. -C/--project "
            "selects the checkout for context; it does not relocate that shared "
            "log or the default report. For redaction, repository identifiers come "
            "from the selected checkout; configured names and redaction settings "
            "come from booley.toml in the feedback storage directory. "
            "For Projects, BOOLEY_DIR selects shared storage, as in the Sandbox. "
            "Without it, storage discovery starts from the selected checkout and may find "
            "a separate log. Booley source checkouts always share feedback in Git's "
            "shared metadata directory, regardless of BOOLEY_DIR."
        ),
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
        help="Mark findings as already shared, so later exports exclude them",
    )
    filed_p.add_argument("finding_ids", metavar="F-N", nargs="+", help="Findings to stamp")
    filed_p.add_argument(
        "--url",
        default="manual",
        help="Issue URL, or 'manual' when it was posted some other way (the default)",
    )


def _add_reporting_subcommands(fb: argparse._SubParsersAction) -> None:
    """The read side: inspect the log and render or export local views."""
    list_p = fb.add_parser("list", help="Show the findings logged so far")
    list_p.add_argument("--json", action="store_true", help="Machine-readable output")
    list_p.add_argument("--bucket", choices=BUCKETS, help="Only this bucket")
    list_p.add_argument("--kind", choices=KINDS, help="Only findings, or only wins")

    report_p = fb.add_parser("report", help="Render the one local user report")
    report_p.add_argument("--project-name", default="", help="Name for the report title")
    report_p.add_argument(
        "--user-report-path",
        metavar="PATH",
        help="Write the user report here instead of the default feedback storage directory "
        "(maintainer dogfood runs only — a real project keeps it out of the tracked tree)",
    )

    export_p = fb.add_parser("export", help="Explicitly write redacted Markdown for sharing")
    _add_finding_selection(export_p)
    export_p.add_argument(
        "--output",
        metavar="PATH",
        help="Write here instead of the feedback storage directory",
    )

    redact_p = fb.add_parser("redact", help="Scrub project identifiers out of a file or stdin")
    redact_p.add_argument("--file", metavar="PATH", help="File to scrub (default: stdin)")
    redact_p.add_argument(
        "--show-mapping",
        action="store_true",
        help="Also print the secret→placeholder table (LOCAL ONLY — never publish it)",
    )


def _add_finding_selection(parser: argparse.ArgumentParser) -> None:
    """Add the finding selector used by explicit redacted exports."""
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


def _cmd_add(args: argparse.Namespace, booley_dir: Path) -> int:
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
        booley_dir,
    )
    print(f"{finding.id} logged ({finding.marker} {finding.severity}, bucket={finding.bucket})")
    _warn_missing_evidence(finding)
    return 0


def _cmd_friction(args: argparse.Namespace, booley_dir: Path) -> int:
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
        booley_dir,
    )
    print(f"{finding.id} logged ({finding.marker} friction, bucket={finding.bucket})")
    _warn_missing_evidence(finding)
    return 0


def _cmd_say(args: argparse.Namespace, booley_dir: Path) -> int:
    """Log an impression — no evidence demanded, nothing to get wrong."""
    finding = append(
        Finding(
            title=args.message,
            kind="impression",
            # Bucketed as Booley's by construction: an opinion about Booley is
            # not the project's problem to fix, and leaving it "unknown" would
            # park it in the untriaged pile excluded from exports.
            bucket="booley",
            sentiment=args.sentiment,
            component=args.component,
            notes=args.notes,
            step=args.step,
            origin=args.origin,
            attachments=list(args.attach),
        ),
        booley_dir,
    )
    print(f"{finding.id} logged ({finding.marker} {finding.sentiment}). Thanks — that helps.")
    print("Use `booley feedback export` when you want a redacted file to share manually.")
    return 0


def _cmd_filed(args: argparse.Namespace, booley_dir: Path) -> int:
    """Stamp findings as already reported, so no later batch re-publishes them."""

    def update(entries: list[Finding]) -> None:
        log = FindingsLog(path=booley_dir / "findings.jsonl", entries=entries)
        selected = [select_unique(log, finding_id) for finding_id in args.finding_ids]
        for entry in selected:
            entry.mark_filed(args.url)

    try:
        rewrite(update, booley_dir)
    except (AmbiguousFindingIdError, UnknownFindingIdError) as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1
    print(f"Marked as filed ({args.url}): {', '.join(args.finding_ids)}")
    return 0


def _cmd_win(args: argparse.Namespace, booley_dir: Path) -> int:
    finding = append(
        Finding(title=args.title, kind="win", exposed_by=args.exposed_by, step=args.step),
        booley_dir,
    )
    print(f"{finding.id} logged (win)")
    return 0


def _cmd_triage(args: argparse.Namespace, booley_dir: Path) -> int:
    selected: list[Finding] = []

    def update(entries: list[Finding]) -> None:
        target = select_unique(
            FindingsLog(path=booley_dir / "findings.jsonl", entries=entries), args.finding_id
        )
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
        target.attachments += [a for a in args.attach if a not in target.attachments]
        selected.append(target)

    try:
        rewrite(update, booley_dir)
    except (AmbiguousFindingIdError, UnknownFindingIdError) as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1
    target = selected[0]
    print(f"{target.id} → bucket={target.bucket}, severity={target.severity}")
    return 0


def _cmd_list(args: argparse.Namespace, booley_dir: Path, list_view: ListView) -> int:
    log = read_log(booley_dir)
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
    print(list_view(log, entries))
    if log.corrupt_lines:
        print(f"⚠️  {log.corrupt_lines} unparseable line(s) skipped", file=sys.stderr)
    return 0


def _cmd_report(
    args: argparse.Namespace,
    project_root: Path,
    booley_dir: Path,
    *,
    env: render.Environment | None = None,
) -> int:
    override = Path(args.user_report_path).resolve() if args.user_report_path else None
    user_path, report = render.write_user_report(
        project_root,
        booley_dir,
        project_name=args.project_name,
        user_report_path=override,
        env=env,
    )
    print(f"Wrote {user_path}")
    if not report.has_content:
        print("No Booley-attributable findings with enough evidence to export.")
    else:
        print(
            f"{len(report.filable)} finding(s) available to export; no redacted file was written."
        )
        print(f"  redaction: {redact_mod.diff_summary(report.redaction_hits)}")
    old_export = booley_dir / render.BOOLEY_REPORT_NAME
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
    return 0


class _UnknownFindingIdsError(ValueError):
    """An export batch named findings that do not exist in this project."""

    def __init__(self, finding_ids: list[str], path: Path):
        self.finding_ids = finding_ids
        super().__init__(f"No finding(s) {', '.join(finding_ids)} in {path}")


def _select_findings(log: FindingsLog, finding_ids: list[str]) -> FindingsLog:
    """Return the requested export batch after CLI boundary validation."""
    by_id = {entry.id: entry for entry in log.entries}
    missing = list(dict.fromkeys(fid for fid in finding_ids if fid not in by_id))
    if missing:
        raise _UnknownFindingIdsError(missing, log.path)
    selected = [select_unique(log, fid) for fid in dict.fromkeys(finding_ids)]
    return FindingsLog(path=log.path, entries=selected, corrupt_lines=log.corrupt_lines)


def _export_report(
    project_root: Path,
    booley_dir: Path,
    finding_ids: list[str],
    *,
    all_findings: bool,
    env: render.Environment | None = None,
) -> tuple[FindingsLog, render.BooleyReport] | None:
    """Read, validate, select, and render one export batch."""
    log = read_log(booley_dir)
    if bool(finding_ids) == all_findings:
        choice = "finding IDs or --all, not both" if finding_ids else "finding IDs or --all"
        print(f"Error: choose {choice} for the export batch.", file=sys.stderr)
        return None
    try:
        selected = log if all_findings else _select_findings(log, finding_ids)
    except (_UnknownFindingIdsError, AmbiguousFindingIdError) as error:
        print(f"Error: {error}", file=sys.stderr)
        return None
    report = render.render_booley_report(selected, project_root, booley_dir=booley_dir, env=env)
    return log, report


def _cmd_export(
    args: argparse.Namespace,
    project_root: Path,
    booley_dir: Path,
    *,
    env: render.Environment | None = None,
) -> int:
    prepared = _export_report(
        project_root,
        booley_dir,
        args.finding_ids,
        all_findings=args.all_findings,
        env=env,
    )
    if prepared is None:
        return 1
    _, report = prepared
    if not report.has_content:
        print("Nothing filable — nothing to export.")
        return 0
    output = Path(args.output).resolve() if args.output else booley_dir / render.BOOLEY_REPORT_NAME
    path = render.export_booley_report(report, output)
    print(f"Wrote {path} ({len(report.filable)} finding(s), redacted)")
    print(f"  redaction: {redact_mod.diff_summary(report.redaction_hits)}")
    return 0


def _cmd_redact(args: argparse.Namespace, project_root: Path, booley_dir: Path) -> int:
    text = Path(args.file).read_text(encoding="utf-8") if args.file else sys.stdin.read()
    plan = redact_mod.build_plan(project_root, booley_dir)
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


# Subcommand → handler. Every handler takes (args, project_root, booley_dir) so
# the dispatch stays a table rather than a ladder of ifs; handlers that don't need
# a value simply ignore it.
_HANDLERS: dict[str, Callable[[argparse.Namespace, Path, Path], int]] = {
    "add": lambda args, _root, pdir: _cmd_add(args, pdir),
    "friction": lambda args, _root, pdir: _cmd_friction(args, pdir),
    "say": lambda args, _root, pdir: _cmd_say(args, pdir),
    "win": lambda args, _root, pdir: _cmd_win(args, pdir),
    "triage": lambda args, _root, pdir: _cmd_triage(args, pdir),
    "filed": lambda args, _root, pdir: _cmd_filed(args, pdir),
    "redact": _cmd_redact,
}

_ENVIRONMENT_HANDLERS = {
    "report": _cmd_report,
    "export": _cmd_export,
}


def _report_boundary_error(exc: BoundaryError) -> int:
    """Print a malformed ``booley.toml`` value as a plain error; exit status 2."""
    print(f"Error: {exc}", file=sys.stderr)
    return 2


def requires_environment(args: argparse.Namespace) -> bool:
    """Return whether a Feedback command renders environment observations."""
    return getattr(args, "feedback_command", None) in _ENVIRONMENT_HANDLERS


def run(
    args: argparse.Namespace,
    project_root: Path,
    *,
    env: render.Environment | None = None,
    list_view: ListView,
) -> int:
    """Dispatch ``booley feedback <subcommand>``."""
    command = getattr(args, "feedback_command", None)
    # ``list`` prints through the view the harness injects: the palette and table
    # renderer live there, and this package may not import them.
    handlers = {**_HANDLERS, "list": lambda a, _root, pdir: _cmd_list(a, pdir, list_view)}
    handler = handlers.get(command)
    environment_handler = _ENVIRONMENT_HANDLERS.get(command)
    if handler is None and environment_handler is None:
        commands = [*handlers, *_ENVIRONMENT_HANDLERS]
        print(
            f"Usage: booley feedback {{{'|'.join(commands)}}}\n       booley feedback --help",
            file=sys.stderr,
        )
        return 2
    booley_dir = feedback_storage_dir(project_root)
    if environment_handler is not None:
        try:
            return environment_handler(args, project_root, booley_dir, env=env)
        except BoundaryError as exc:
            return _report_boundary_error(exc)
    assert handler is not None
    try:
        return handler(args, project_root, booley_dir)
    except BoundaryError as exc:
        return _report_boundary_error(exc)
    except (FindingsLockTimeoutError, CorruptFindingsLogError) as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 2
