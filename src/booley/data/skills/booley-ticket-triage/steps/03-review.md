# Step 3: Review Tickets

For each `status: "review"` ticket, use the prepared package as the normal path.
The post-developer report agent has already inspected the ticket, source, diff,
logs, reports, scope, and state. The harness has already enumerated all criteria,
commits, changed files, health findings, economics, and durable diff pairs.

## 1. Render once

Run exactly once:

```bash
booley board review-briefing $SLUG
```

This command performs a fast freshness check, opens every prepared diff, and
prints the fixed review briefing. Present that output without rebuilding its
tables or rereading its underlying evidence. Do not run `prepare-review` during
interactive triage and do not poll the manifest.

If the command reports a missing or stale package, show that as a Booley
post-processing finding and offer **reset** / **skip**. `prepare-review --force`
is a maintenance/recovery operation and requires an explicit user request; it
is not the interactive fallback.

Tickets with `on_success.triage_report: false` intentionally have no semantic
report-agent assessment. The same command renders their deterministic criteria,
commit, scope, health, economics, and diff facts with a `hold` recommendation;
inspect those facts and diffs before offering the normal decision choices.

## 2. Evidence escalation only

Read raw evidence only when the user asks a follow-up the prepared briefing
cannot answer or the briefing identifies an anomaly requiring diagnosis. Start
with the one cited source relevant to that question. Do not routinely reread
`REPORT.md`, state, run logs, transcripts, Flow reports, Git history, or diffs.

The package is authoritative only while its manifest is fresh. Its deterministic
facts include every declared criterion, feature-branch commit (oldest first),
changed path (including renames and submodules), recorded scope deviation,
current-run usage summary, and mechanical health check. The report agent supplies
the recommendation, scope classifications, report summary, blockers, and findings.
Both `review_*_done` and `review_*_clean` are freshness-sensitive to their
recorded source fingerprint. The package also lists every review finding and
disposition deterministically; every accepted waiver, including `MINOR`, must
appear with its justification.

## 3. Decision

Ask: **approve** / **archive** / **reset** / **skip**.

- **Approve**: `python -m booley.ticket_board complete $SLUG`
- **Archive**: `python -m booley.ticket_board archive $SLUG --force`
- **Reset**: `python -m booley.ticket_board reset $SLUG`
- **Skip**: leave as-is

After the decision, invoke `/booley-feedback` for every confirmed Booley defect.
External submission remains behind that skill's explicit approval gate.
