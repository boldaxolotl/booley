---
name: booley-feedback
description: Prepare sanitized Booley feedback for manual submission from an offline container. Use for bugs, documentation contradictions, confusing behavior, opinions, or feature wishes. Investigate failures, provide a verified workaround where practical, and deliver a report with GitHub and maintainer email handoff options.
---

# Prepare Booley feedback

Deliver an inspected sanitized report for manual submission and the blocked
task's next step (see §5). This skill runs offline in the Session Runtime: use
local commands, not `submit`, authentication probes, confirmation tokens,
browser launches, or mail sending. Generate the report by default without a
separate approval question or publication decision.

Check the installed CLI with `booley feedback --help`; run its commands for the
user rather than asking them to choose subcommands.

## 1. Capture and classify

Capture perishable evidence first: the command, exit status, diagnostic output,
expected behavior, and relevant versions. Keep private evidence local. Use a
generic title and component from the start; inspect files before attaching them.
Attachments become potential outbound content and cannot be removed by `triage`.
Private source, original project logs, and scratch reductions stay unattached.

```console
booley feedback add --origin bug \
  --title "simulate exits 2 with no diagnostic" \
  --severity blocker --bucket unknown --component simulate \
  --repro "booley flow sim --target sim_smoke" \
  --observed "exit code 2, empty stderr, no run.log written" \
  --expected "a simulation result or an actionable diagnostic"
```

Record distinct problems separately and keep their printed `F-N` IDs for this
interaction's batch. Use the origins below; `say` supplies its impression origin.

| Feedback | Local command | CLI evidence required for export |
| --- | --- | --- |
| Bug | `add --origin bug` | `repro`, `observed`, `expected` |
| Documentation contradiction | `add --origin bug --bucket docs` | `observed` describing the contradiction, `component` identifying the document |
| Friction | `friction --origin bug` | `component` or `exposed-by`, plus `expected` or explanatory `notes` |
| Opinion or wish | `say` | The user's words; no reproduction |

```console
booley feedback add --origin bug --bucket docs --severity note \
  --title "Documented option differs from CLI" --component "<document path>" \
  --observed "<document says X; installed CLI accepts Y>"
booley feedback friction --origin bug \
  --title '"0 targets matched" is confusing' --component targets \
  --expected "Explain the empty result and how to list targets"
booley feedback say "I want per-Target coverage in the run report" --sentiment wish
```

For impressions, use `praise`, `gripe`, `wish`, or `mixed`, preserve the user's
words, and acknowledge capture briefly. If the user says "not now" or declines
further feedback work, leave findings local and stop without exporting or
repeating the offer. An explicit file request proceeds to export.

## 2. Investigate the failure and find a workaround

Skip diagnosis for opinions. For bugs, inspect the installed source responsible
for the behavior when available:

```console
python -c "import booley, pathlib; print(pathlib.Path(booley.__file__).parent)"
```

Separate observed facts, suspected causes, and verified conclusions. A verified
source path need not prove a root cause. Report unavailable source or incomplete
diagnosis as limitations; neither prevents reporting useful observations.

| Bucket | Evidence supports |
| --- | --- |
| `project` | Project configuration or environment explains the failure; stays local. |
| `booley` | Observed Booley behavior violates its expected behavior. |
| `docs` | A specific documentation statement contradicts behavior. |
| `unknown` | Ownership remains unresolved; stays local under CLI export rules. |

A workaround changes impact, not ownership. A misunderstood document alone does
not establish a documentation defect. Update the bucket using evidence and set
`--verified-against-source` only when the claim was actually checked in source.

For blocked tasks, investigate a safe practical workaround within existing
authority. Apply it when authorized; otherwise provide implementable steps.
Verify with the original reproduction or closest safe check, and label untested
suggestions unverified. Record the exact command/configuration/alternate workflow,
what it bypasses, limitations, verification, and reversal steps for the handoff.

Retain the evidence-based bucket. Preserve blocker severity unless a verified
workaround provides a usable but degraded path; then record:

```console
booley feedback triage F-1 --severity workaround \
  --notes "Workaround: …. Verified by: …. Limitation: …. Revert by: …."
```

`--notes` replaces existing notes; preserve still-relevant evidence. Finish this
step with a verified workaround or an unresolved blocker and next action.

## 3. Prepare safe outbound evidence

For a Booley bug depending on private RTL, testbench, configuration, or logs,
follow [minimal-reproducer.md](minimal-reproducer.md) to build a verified synthetic
case. Skip it for opinions, friction, documentation contradictions, and bugs
reproducible with public Booley fixtures. If a safe equivalent case is impractical,
keep private evidence local and report only independently actionable non-project
facts, stating the limitation.

Audit every outbound field: title, component, exposed-by, step, reproduction,
observed/expected behavior, notes (including workarounds), and attachment contents
and paths. The identifier redactor is a denylist, not proof of safe disclosure.

Use `triage` to replace editable fields with sanitized text. It cannot change a
title, exposed-by, or step, or remove attachments. If any uneditable field or
attachment is unsafe, create a new finding of the same kind with a generic title
and only reviewed fields and attachments. Carry over the evidence-based bucket,
severity, and verification status. Add a local note to the original identifying
the replacement, mark the original `--bucket unknown` so later bulk exports
exclude it, and select only the replacement ID for this report. Preserve the
original evidence locally; do not mark it filed or edit the log by hand.

Finish with safe finding IDs and reasons for withholding evidence, without
exposing the withheld material.

## 4. Write and inspect the reports

```console
booley feedback report
booley feedback list
booley feedback export F-8 F-9
```

Use exactly this interaction's safe IDs; `--all` includes unrelated pending
findings. The local unredacted report is `SETUP-REPORT.md` for a setup-origin log
or `FEEDBACK-REPORT.md` otherwise. Export writes `BOOLEY-FEEDBACK.md`. Use the
paths printed by the CLI in the directory resolved by `booley.runtime.project_dir`;
keep reports and reproducer scratch work outside the RTL repository's tracked tree.

Read the entire exported file, including its environment section and attachment
blocks. Check for private identifiers, semantic disclosure, stale notes, clipped
reproducers, and unsupported claims. Correct source findings or create clean
replacements, then re-export and inspect again. Deliver only the inspected file.
Call synthetic evidence synthetic and sanitized, never guaranteed anonymous.

The CLI withholds `unknown`, `project`, and findings missing required evidence;
explain omissions without inventing evidence or changing ownership to pass export.
If nothing exports or a useful unresolved observation is withheld, write a separate
sanitized Markdown report in the resolved project data directory. Label it manually
prepared; include only reviewed facts, expectations, available versions, workaround
status, and explicit unknowns, with no private reproduction. Inspect it as above.
Project-only configuration mistakes stay local unless there is distinct Booley
feedback to send.

## 5. Hand off the report and the next step

Link the sanitized report with a host-accessible path when available. If the
container path is not directly accessible, explain how to retrieve it using the
session's supported artifact transfer. Keep the unredacted local report clearly
separate from the file intended for sharing.

Present two clean, highlighted submission options, using the installed source's
`NEW_ISSUE_URL` and `INTAKE_EMAIL` in `booley.feedback.submit` as the destination
source of truth. Current destinations are:

- **Submit on GitHub:** [Open a Booley issue](https://github.com/boldaxolotl/Booley/issues/new).
  Paste the sanitized report, review it, and submit. Explain briefly that the
  issue is public and associated with the user's GitHub account.
- **Email the maintainer:** [boldaxolotl@proton.me](mailto:boldaxolotl@proton.me).
  Supply a concise subject based on the report title and ask the user to attach
  the sanitized report in their mail client. Explain that this sends it privately
  to the maintainer and exposes the sender's email address to them.

Keep report bodies out of URLs; use a normal issue link and separate file, with
at most a subject prefilled in the email link. Offer both routes unless one is
already chosen. Omit unsolicited options after refusal or when `[feedback] mode`
is `"off"` or `"file-only"`; explicit requests for submission instructions take precedence.

End with the report ready to send, not submitted, and the task outcome:

- **Verified workaround:** provide the implementable steps and evidence from §2.
- **Still blocked:** say "No verified workaround was found", identify the blocked
  operation, and give the next action. A confirmed Booley defect needs a Booley
  fix before retrying; promise neither a release date nor that reporting schedules
  a fix. For unresolved ownership, state unknowns and needed diagnosis/external action.
- **Non-blocking feedback:** state that no workaround is needed.

Mark findings filed only after the user confirms actual submission with an issue
URL or email confirmation. File creation and handoff links do not prove submission.
