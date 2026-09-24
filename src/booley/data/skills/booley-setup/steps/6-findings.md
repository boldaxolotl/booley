# Step 6 — Findings: report, triage, and optional redacted export

The last step of setup. Everything logged with `booley feedback add` during
Steps 0–5 gets turned into a report for the user, sorted by whose problem each
finding is, and — only if the user agrees — a bug report to Booley.

**This step never blocks completion.** Step 4 is the gate; setup is already
finished by the time you get here. A user who declines everything in this step
has lost nothing.

## Before you start

Read the log:

```console
booley feedback list
```

If it is empty, something went wrong earlier: a setup run that hit **zero**
friction and recorded **zero** wins means nobody was logging, not that the run
was flawless. Say so plainly in the final report rather than presenting an empty
log as a clean bill of health.

## 1. Triage every finding

Each finding needs a bucket. Untriaged entries land in the user's report under
"Not yet triaged", which is an admission that the work is unfinished — so
finish it.

| Bucket | Means | Goes to |
| --- | --- | --- |
| `project` | Their repo, their config, their environment. Setup could not fix it for them. | the user's report, as an action item |
| `booley` | Booley behaved wrongly. Includes anything you had to work around. | the user's report **and** the bug-report candidate list |
| `docs` | Booley's docs say one thing, its code does another. | same as `booley` |

```console
booley feedback triage F-3 --bucket booley --severity workaround --component doctor
```

Anything logged with `booley feedback friction` during the run is triaged the
same way. It is held to a different evidence bar — where it happened and what
you expected instead, no reproduction — so do not withhold it for lack of a
command line, and do not re-grade it into a bug it isn't.

**Verify before you blame Booley.** A finding only goes in the `booley` bucket
if you have checked the claim against Booley's own source — it is installed and
readable (`python -c "import booley, pathlib; print(pathlib.Path(booley.__file__).parent)"`).
Read the code that produced the message. Most "Booley bugs" turn out to be a
misread doc (→ `docs`) or a config gap (→ `project`), and a maintainer queue full
of those buries the real ones. When you have confirmed it in the source, say so:

```console
booley feedback triage F-3 --bucket booley --verified-against-source
```

**Fill in the evidence while you still can.** `booley` findings need a
reproduction, an observed, and an expected — `booley feedback report` withholds
the ones that don't have all three, because a maintainer cannot act on a vague
recollection. If a finding is thin and you can still reproduce it, reproduce it
now and `triage --repro/--observed/--expected`. If you cannot, leave it thin:
it stays in the user's local report, which is the right home for "this felt
rough".

## 1b. Ask what they made of it — once, and take a shrug for an answer

They have just spent a session setting up a framework they had never used. Nobody
else is ever in a better position to say whether it was worth it. Ask, plainly,
and only once:

> "Anything you'd want the maintainers to hear about Booley itself — what was
> good, what was painful, what you wish it did? Doesn't have to be a bug."

Log whatever comes back **in their words**, one entry per thought:

```console
booley feedback say "the doctor output made the config obvious" --sentiment praise
booley feedback say "the plan grill took longer than the rest of setup" --sentiment gripe
booley feedback say "I want a dry-run that fakes the EDA tools" --sentiment wish
```

Rules: no reproduction is asked for, ever. Do not upgrade a complaint into a bug
report they did not make, do not soften a blunt one, and do not fish for praise
— "it was fine" is a complete answer, and "nothing comes to mind" ends this
section. An impression stays local unless the user explicitly requests a
redacted export and shares that file themselves.

## 2. Write the one report

```console
booley feedback report --project-name <name>
```

Writes `.booley_project/SETUP-REPORT.md` — the user's copy, unredacted and never
published. `booley feedback export` creates the separate redacted view only
when the user explicitly asks for a sanitized file.

The report stays inside `.booley_project/`. **Do not** put it in the RTL repo's
tracked tree; that is the footprint guardrail. The enclosing maintainer dogfood
flow may explicitly tell this step to pass `--user-report-path SETUP-REPORT.md`
for its throwaway clone. That writes the root report instead of an inner copy;
on a real project, never pass the flag.

When the report is complete, mark Step 6 complete in the §3 execution ledger.
Do not delete a raw attachment here: Step 7 owns the materialization decision
and must preserve Finding IDs, ordering, filed state, and semantic fields.

## 3. Walk the user through their report

In the onboarding voice — they may still be new to all of this:

- **What they need to act on** (the `project` bucket), most severe first.
- **What wasn't their fault** (the `booley`/`docs` buckets), and which
  workarounds are now baked into their config — those are the ones that will
  confuse them in three months.
- **What setup did not cover**: unconfigured flows, Targets not made, checks
  deferred. Prevents false confidence later.
- **What went right.** Not padding: it is the denominator that makes the
  findings count mean anything.

## 4. Export only on explicit request

Do not offer to transmit findings: Booley has no submission path. If the user
explicitly asks for a sanitized file, export exactly the Finding IDs from this
setup run:

```console
booley feedback export F-2 F-6 F-7
```

Do not use `--all`; it intentionally includes older runs and conversations.
Read the entire exported file with the user, including attachment blocks and
redaction caveats, then give them its path for manual sharing. Creating the file
does not prove that it was shared. Only after the user confirms actual delivery,
record it with `booley feedback filed F-3 F-7 --url <destination>` so later
exports do not repeat those Findings.

Later feedback is not this step's job: `/booley-feedback` handles anything that
turns up after setup is done, using the same log.

**If they want changes to the redaction** — a module name they'd rather keep, a
term that got missed — add it to `[feedback] redact_extra` (or set
`redact_identifiers = false` for an open-source project whose names are already
public), then export and inspect the same IDs again.

## 5. Close out

Record in the final report: findings by severity, the top few called out, what
was triaged where, and whether the user explicitly requested a redacted export.
