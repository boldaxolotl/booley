# Step 3: Review Tickets

For each `status: "review"` ticket, use the prepared package as the normal path.
The post-developer or explicitly requested review preparation has already
inspected the ticket, source, diff, logs, reports, scope, and state. The harness has already enumerated all criteria,
commits, changed files, health findings, economics, and durable diff pairs.

## 1. Render once

Run exactly once:

```bash
booley board show $SLUG --no-open-diffs
```

This command performs a fast freshness check of the prepared package manifest
and prints the fixed review briefing with diff launching disabled. Review preparation separately compares
each source-sensitive Criterion and Reviewer receipt with the current Ticket
worktree. Stale rows name the changed source categories; a stale mandatory
Criterion forces a `hold` recommendation without changing its recorded outcome
or mutating runtime state. Always use `--no-open-diffs`: the automatic
filter uses extensions and binary detection, so it cannot recognize every
compiled output (for example, hexadecimal firmware stored as `.txt`).

Before opening any diff, apply the artifact classification rule in `SKILL.md`
to the changed paths. Use the prepared report first; when a path's provenance
is unclear, inspect only its relevant build rule/output declaration and, if
needed, a small content sample without displaying an artifact diff. Open the
prepared base/head pairs individually for confirmed human-authored files using
the configured diff viewer. Leave compiled artifacts and unresolved paths
unopened; do not rerun the command with automatic launching enabled.

Present the briefing with each changed-file diff status corrected to the actual
outcome: opened, omitted (compiled artifact), or not opened (provenance unclear
or viewer unavailable). The command's printed "diff opened" text is not evidence
of a launch when `--no-open-diffs` was used. Preserve all other briefing facts
and tables. Do not run `board review` during
interactive triage and do not poll the manifest.

The briefing presents the reports first: the Developer Agent's `REPORT.md`, then
the polished HTML report. It then presents the decision summary, actionable
findings, explanation highlights, scope deviations, changed files, deterministic
criteria, Waiver Candidates (when any), review findings and dispositions, Target
recipe comparisons, commit history, run economics, and the decision choices.

If the command reports an ordinary unaccepted missing or stale package, show
that as a Booley post-processing finding and offer **review** / **reset** /
**skip**. `board review --force` is a maintenance/recovery operation and
requires an explicit user request; it is not the interactive fallback. A
`STALE ACCEPTANCE — Ticket heads changed after acceptance.` marker is different:
follow the accepted-review choices below and do not regenerate the package.

Tickets without `triage_report` in their `on_success` list intentionally have no semantic
report-agent assessment. The same command renders their deterministic criteria,
commit, scope, health, economics, and diff facts with a `hold` recommendation;
inspect those facts and diffs before offering the normal decision choices.

## 2. Evidence escalation only

Read raw evidence only for the artifact-classification gate above, when the user
asks a follow-up the prepared briefing cannot answer, or when the briefing
identifies an anomaly requiring diagnosis. Start
with the one cited source relevant to that question. Do not routinely reread
`REPORT.md`, state, run logs, transcripts, Flow reports, Git history, or diffs.

An ordinary unaccepted package is authoritative only while its manifest is
fresh. A stale-marked accepted briefing remains authoritative as the immutable
record of what was accepted, but it is not evidence that the changed live heads
are current or acceptable; its integrity digests and the marker's exact-head
comparison govern the available actions. The package's deterministic facts
include every declared criterion, feature-branch commit (oldest first),
changed path (including renames and submodules), recorded scope deviation,
current-run usage summary, and mechanical health check. The report agent supplies
the recommendation, scope classifications, report summary, blockers, and findings.
Both `review_*_done` and `review_*_clean` are freshness-sensitive to their
recorded source fingerprint, or to their receipt for current package versions.
The package also lists every review finding and
disposition deterministically; every accepted waiver, including `MINOR`, must
appear with its justification.

## 3. Decision

Present every open `_done` finding and get explicit Human approval; a met
`_done` Criterion is not approval.

For a briefing marked **unaccepted**, offer **fix here** / **refresh** /
**approve when all mandatory Criteria are met** / **hold** / **reset** /
**archive**. Keep the Ticket in review
while making corrections. Run verification endpoints and `submit_run_report`
through `booley board validate $SLUG -- <normal endpoint command>` so they
record Ticket evidence. Commit changes, then use `booley board review $SLUG`
to capture new inputs. `booley board approve $SLUG` checks every normal
acceptance gate, publishes first acceptance, and completes the Ticket.
Hold leaves the Ticket unchanged.

### Waiver Candidates

A briefing whose choices include **decide waivers and approve** reached review
on a Provisional Coverage Verdict: coverage passes only if some Waiver
Candidates become Approved Waivers. Take the offered candidates **one at a
time**. For each, show its point, reason, coverage change, whether it is
needed, and its justification, saying that the justification is unverified
candidate-record text. Then ask the user **accept** or **reject**. Never
propose an answer, and never accept on the user's behalf. Do not offer stale,
invalid, or not-needed candidates.

When every offered candidate is decided, run:

```bash
booley board approve $SLUG --accept-waivers <id,...> --reject-waivers <id,...>
```

Omit a flag whose list is empty. If the user rejects a needed candidate,
approve refuses: it records the rejections, promotes nothing, and leaves the
Ticket in review. Then offer **fix here** / **reset** / **archive**. An accepted
waiver reaches the destination only with the merge, as the commit
`chore(<slug>): approve coverage waivers`.

If a review has no Criteria Satisfaction Record, the explicit recovery
operation is
`booley board review $SLUG --request --repair --reason "<recovery intent>"`.
It preserves work and creates an unaccepted package when its Basis/worktree are
valid. Never substitute a mechanical move or fabricate accepted evidence.

For current accepted review, ask: **approve** / **reset** / **archive** / **skip**.
For stale accepted review, ask: **restore exact accepted heads** / **reset** /
**archive** / **skip**.
Do not offer approval while any mandatory Criterion is marked stale; rerun its
Flow or Specialist and prepare a new review package first.

- **Approve**: `booley board approve $SLUG`
- **Restore exact accepted heads**: Criteria Satisfaction Records are immutable.
  First save every post-acceptance commit on a separate safety branch. Then move
  every Ticket ref and worktree named in the stale marker to its listed frozen
  commit, rerun `booley board show $SLUG`, and approve once the marker is gone.
  A new `git revert` commit does not restore an accepted head.
- **Reset**: ask why a clean run is required, then run
  `booley board reset $SLUG --reason "<correction reason>"`.
  This is a clean start:
  retire the Ticket worktree and branch, archive the current runtime artifacts
  as prior-run history, clear the active state, and return the Ticket to
  `queued`. Do not selectively retain reviewed work.
- **Archive**: `booley board archive $SLUG`
- **Skip**: leave as-is

Review never resumes through an ordinary move to `queued`. It closes into
Ticket History with outcome `done` or `archived`, stays in `review` while the
reviewer fixes it, or uses the explicit full reset above.

After the decision, invoke `/booley-feedback` for every confirmed Booley defect.
The skill never submits externally; the user controls any manual sharing.
