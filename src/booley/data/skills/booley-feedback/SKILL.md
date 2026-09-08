---
name: booley-feedback
description: Prepare sanitized Booley feedback for manual submission from an offline container. Use for bugs, documentation contradictions, confusing behavior, opinions, or feature wishes. Investigate failures, provide a verified workaround where practical, and deliver a report with GitHub and maintainer email handoff options.
---

# Prepare Booley feedback

Close out the feedback session with a sanitized report the user can send and a
concrete next step for the blocked task: an implementable workaround, or a clear
statement that no verified workaround was found. For a confirmed Booley defect
that still blocks progress, explain that the blocked operation needs a Booley
fix before retrying; do not promise a release date or that reporting schedules a
fix. For unresolved attribution, state what remains unknown and what diagnosis
or external action is needed.

This skill runs in the Session Runtime without internet access. Prepare files
locally and let the user submit them outside the container. Do not run `submit`,
probe authentication, request confirmation tokens, launch a browser, or send mail.
Generating the sanitized report is the default deliverable and needs no separate
approval question. A generated file is not a submitted report.

Use `booley feedback --help` to check the installed CLI. Run its local commands
for the user; do not make them choose subcommands.

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

Keep the printed `F-N` IDs. Record distinct problems separately; this
interaction's selected findings form the report batch, excluding older pending
entries. Use `--origin bug` for bugs and friction captured in this flow;
`say` supplies its own impression origin.

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
wording, and acknowledge capture briefly. A passing remark needs no diagnostic
investigation. If the user says "not now" or declines further feedback work,
leave captured findings local and stop; do not interpret that as a file request
or repeat the submission offer. An explicit request for a file proceeds to export.

## 2. Investigate the failure and find a workaround

Skip diagnosis for opinions. For bugs, inspect the installed source responsible
for the behavior when available:

```console
python -c "import booley, pathlib; print(pathlib.Path(booley.__file__).parent)"
```

Distinguish observed facts, suspected causes, and verified conclusions. Source
inspection can establish a failure path without proving its root cause. If source
is unavailable or diagnosis is incomplete, report that limitation; a useful
observation does not require a proven root cause.

| Bucket | Evidence supports |
| --- | --- |
| `project` | Project configuration or environment explains the failure; stays local. |
| `booley` | Observed Booley behavior violates its expected behavior. |
| `docs` | A specific documentation statement contradicts behavior. |
| `unknown` | Ownership remains unresolved; stays local under CLI export rules. |

A workaround changes impact, not ownership. A misunderstood document alone does
not establish a documentation defect. Update the bucket using evidence and set
`--verified-against-source` only when the claim was actually checked in source.

For a blocked task, investigate a safe practical workaround using the authority
already granted. Deliver the exact command, configuration change, or alternate
workflow the user can implement, what it bypasses, its limitations, how to undo
it, and verification using the original reproduction or closest safe check.
Apply it when already within scope; otherwise provide the concrete steps.
Label untested suggestions as unverified, not as successful workarounds.

If no verified workaround was found, say so and identify the remaining blocked
operation. Preserve the blocker severity. If a verified workaround provides a
usable but degraded path, retain the evidence-based bucket and record the result:

```console
booley feedback triage F-1 --severity workaround \
  --notes "Workaround: …. Verified by: …. Limitation: …. Revert by: …."
```

`--notes` replaces existing notes; retain any still-relevant evidence when updating.
Complete this step with either a verified workaround or an explicit unresolved
blocker and next action. Keep reporting possible even when diagnosis is incomplete.

## 3. Prepare safe outbound evidence

For a Booley bug depending on private RTL, testbench, configuration, or logs,
read [minimal-reproducer.md](minimal-reproducer.md). It defines how to build and
verify a synthetic reproducer. Skip that guide for opinions, friction,
documentation contradictions, and bugs reproducible with public Booley fixtures.
If no safe equivalent reproducer is practical, keep private evidence local and
report only independently actionable non-project facts, with the limitation stated.

Audit every outbound field: title, component, exposed-by, step, reproduction,
observed and expected behavior, notes, and every attachment's content and path.
Review workaround notes as carefully as the reproducer. The identifier redactor
is a denylist; it cannot establish that arbitrary text is safe to disclose.

Use `triage` to replace editable fields with sanitized text. It cannot change a
title, exposed-by, or step, or remove attachments. If any uneditable field or
attachment is unsafe, create a new finding of the same kind with a generic title
and only reviewed fields and attachments. Carry over the evidence-based bucket,
severity, and verification status. Add a local note to the original identifying
the replacement, mark the original `--bucket unknown` so later bulk exports
exclude it, and select only the replacement ID for this report. Preserve the
original evidence locally; do not mark it filed or edit the log by hand.

The result of this step is an explicit list of safe finding IDs and an account
of any evidence withheld, without disclosing that evidence in the account.

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

The CLI withholds `unknown`, `project`, and findings missing required evidence.
Name omissions and reasons. Do not invent evidence or change ownership merely to
pass export. If nothing exports, or a useful unresolved observation is withheld,
write a separate sanitized Markdown report in the resolved project data directory
using only reviewed facts, expected behavior, available versions, workaround
status, and explicit unknowns. Identify it as a manually prepared report and
include no private reproduction. Inspect it by the same standard. A project-only
configuration mistake belongs in the local report unless there is distinct
Booley feedback to send.

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

Use a normal issue link and separate file; keep the report out of URL query
strings. An email link may prefill the subject only. These are alternative manual
routes, not actions performed by the agent. Offer both unless the user has
already chosen one. Honor `[feedback] mode = "off"` or `"file-only"` and a prior
refusal by omitting unsolicited submission options; an explicit request for
submission instructions takes precedence. Do not ask permission to generate the
file or require a publication decision to finish the session.

Finish with the implementable workaround and verification, or "No verified
workaround was found" plus the blocked operation and required next action.
For non-blocking feedback, state that no workaround is needed. State that the
report is ready to send, not submitted. Mark findings filed only if the user
later confirms actual submission and provides its issue URL or email confirmation;
file creation and presenting links are not evidence of submission.
