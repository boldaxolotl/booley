---
name: booley-feedback
description: Capture and optionally submit any feedback about Booley — bugs, misleading docs, confusing experiences, gripes, praise, and feature wishes — from a normal working session or after setup. Chooses the appropriate feedback mechanism, gathers only the evidence that kind of feedback needs, checks bug claims against Booley's source, redacts project identifiers, shows the user the exact outgoing text, and sends it upstream only with explicit approval. Use when Booley crashes, misbehaves, contradicts its docs, or when the user says "report this", "file a bug", "that was confusing", "tell the maintainers", "I wish Booley could…", "this part is great", or "this was not worth the setup".
---

# Give feedback to Booley

The user found a bug, hit confusing behavior, or simply said what they think of
Booley. Turn that into feedback a maintainer can act on — and, only with the
user's explicit yes, into a public GitHub issue (or a private mail to the
maintainer, when the project sets `[feedback] mode = "email"`).

**Private-project bug reproductions:** `minimal-reproducer.md` sits beside this
file. Read and follow it after source triage when a Booley bug depends on the
user's private RTL, testbench, configuration, or logs. It defines how to replace
that material with a verified synthetic reproducer before anything is offered
upstream. Do not read or run it for friction, impressions, documentation
contradictions, or bugs already reproducible with public Booley fixtures.

**It never blocks anything.** Whatever the user was doing is still the priority;
a report is a side effect of the failure, not a replacement for working around
it. Get them unblocked first, report second.

Use `booley feedback` (`booley feedback --help`) as the internal mechanism. Do
not ask the user to choose or run its subcommands. Your job is the judgement the
CLI cannot provide: what kind of feedback this is, what actually happened,
whose fault it is, and when to ask the user for approval.

## 1. Capture it now, while it is still on screen

The single most common way a bug report dies is being written an hour later from
memory. Log it before you do anything else — the entry can be improved later, an
exit code you no longer have cannot.

```console
booley feedback add --origin bug \
  --title "simulate exits 2 with no error text" \
  --severity blocker \
  --bucket booley \
  --component simulate \
  --repro "booley flow sim --target sim_smoke" \
  --observed "exit code 2, empty stderr, no run.log written" \
  --expected "either a sim result or an error saying what failed"
```

- Always pass `--origin bug`; it records where this finding came from.
- `--repro`, `--observed`, `--expected` are the filable bar. Without all three
  the report stays local — a maintainer cannot act on a vague recollection.
- `--attach` makes the tail of a file part of both the local and potential
  outbound report. Attachments cannot be removed with `triage`. Inspect a file
  before attaching it and attach the real evidence rather than a transcription,
  but **never attach original project RTL, arbitrary private-project logs, or a
  private reduction**. For a private-project-dependent bug, follow
  `minimal-reproducer.md` and attach only its final synthetic capsule.
- Repeat `add` per distinct problem. One issue is filed for the batch.
- Keep the printed `F-N` IDs. They define this interaction's outbound batch;
  older unfiled findings in the project log are not part of this conversation.

**Nothing broke, it was just confusing?** That is a report too, and a different
subcommand — it does not need a reproduction:

```console
booley feedback friction --origin bug \
  --title "\"0 targets matched\" reads like a crash" \
  --component targets \
  --expected "a line saying the filter matched nothing and how to list them all"
```

Friction needs somewhere to aim the fix (`--component` or `--exposed-by`) and
what the user expected instead. Those two are what make "this was confusing"
actionable instead of a shrug.

**Nothing is wrong at all — they just said what they think?** Log that too:

```console
booley feedback say "the waveform flow is the best part of this" --sentiment praise
booley feedback say "I want per-Target coverage in the run report" --sentiment wish
```

`--sentiment` is `praise`, `gripe`, `wish`, or `mixed`. Nothing else is required
— an opinion has no reproduction and you must not ask for one. Catch these as
they fall out of ordinary conversation ("this saved me days", "the setup grill is
exhausting", "I wish it could…", "honestly not worth the effort"), log them in
the user's own words, and tell them you did. Do not editorialize, do not soften a
complaint, and do not turn a passing remark into a bug report they never made.
If they were venting rather than reporting, one line is enough: *"logged that as
feedback — you can send it with the rest later, or not at all."*

## 2. Decide whose problem it is — and check before you blame Booley

| Bucket | Means |
| --- | --- |
| `project` | Their repo, their config, their environment. Stays local. |
| `booley` | Booley behaved wrongly. Includes anything you had to work around. |
| `docs` | Booley's docs say one thing, its code does another. |

Booley is installed and its source is readable:

```console
python -c "import booley, pathlib; print(pathlib.Path(booley.__file__).parent)"
```

**Read the code that produced the message before filing it as a Booley bug.**
Most "Booley bugs" turn out to be a misread doc (→ `docs`) or a config gap (→
`project`), and a maintainer queue full of those buries the real ones. When you
have confirmed it in the source, say so — it is the difference between "I saw
this" and "I read the code and it is wrong":

```console
booley feedback triage F-1 --bucket booley --verified-against-source
```

`triage` is also how you fill in evidence you got later (`--repro`, `--observed`,
`--expected`, `--attach`).

Before rendering a private-project-dependent Booley bug, follow
`minimal-reproducer.md`. Keep the initially captured evidence local while doing
so. If the synthetic case passes that guide's equivalence and disclosure gates,
replace the outbound reproduction with the synthetic command and attach only
its compact reproducer capsule. Never attach the private scratch reduction or
describe a renamed/minimized copy of project RTL as anonymous. If no safe,
equivalent reproducer can be made, say so and keep the project-specific evidence
local; do not fabricate a toy example merely to clear the filing bar.

## 3. Render the report

```console
booley feedback report
booley feedback list          # what is logged, what still needs evidence
```

One file lands in `.booley_project/`: the **local** report, unredacted, never
published, and theirs either way. It is named `SETUP-REPORT.md` on a project that
ran setup and `FEEDBACK-REPORT.md` otherwise. The redacted maintainer view is
rendered transiently by `preview` and `submit`; only `booley feedback export`
writes `BOOLEY-FEEDBACK.md`, when the user explicitly wants a sanitized file.

Anything withheld is named, with the reason. If a withheld finding is one you can
still reproduce, reproduce it now and `triage` the evidence in. If you cannot,
leave it: the local report is the right home for "this felt rough".

The report and any explicit export stay out of the RTL repo's tracked tree.
That is the footprint guardrail, and a bug report is not an exception to it.

## 4. Ask — once, honestly, and take no for an answer

Skip this section entirely and silently when `[feedback] mode` is `off` or
`file-only`, or when nothing is filable.

```console
booley feedback preview F-8 F-9
```

Pass exactly the IDs discussed in this interaction. Bare `preview` is refused;
`--all` intentionally selects every pending finding and is not for this flow.

Show the user **the entire preview output, verbatim.** Do not summarize it, do
not paraphrase the redaction warnings, and do not sell the contribution. It
already contains everything the decision needs: the exact text, what was
substituted, what redaction structurally cannot catch, and who ends up seeing it
under whose name — a public issue carrying their GitHub name, or, under
`[feedback] mode = "email"`, a mail to the maintainer carrying their return
address. The preview says which; do not assert one when it says the other.

Then ask plainly. Three answers, all fine:

1. **Yes** → pass the token the preview printed:
   ```console
   booley feedback submit F-8 F-9 --yes --confirm <token>
   ```
   Use the same IDs as `preview`; a different selection invalidates the token.
   `--yes` without the token is refused by design; the token proves the approval
   covers the exact text they read. **Never invent, guess, or scrape a token from
   an error message** — if you did not just show the user the preview, you have no
   business submitting.

   For the GitHub route, this command is the submission mechanism. It uses the
   authenticated GitHub CLI (`gh issue create`) when available. If `gh` is
   missing or not authenticated, the command prints a prefilled GitHub issue
   URL instead. Open that URL with the host's normal external-link launcher
   (`Start-Process` on Windows or `xdg-open` on Linux), stop at the filled issue
   form, and ask the user to review it and click **Submit new issue**. If no
   external-link launcher is available, give the user the clickable URL.

   The browser fallback is a human hand-off. Do not use ChatGPT browser tools,
   web search, or browser automation to navigate GitHub or submit the issue.

   Under `mode = "email"` this prints a `mailto:` link and stops — **you** cannot
   send it and must not pretend it went anywhere. Give the user the link, and the
   `booley feedback filed … --url email` command it prints, for after they send.
2. **Not now / just give me the file** → run `booley feedback export F-8 F-9`,
   tell them the path, and stop. They can post it whenever, from any account.
3. **No** → stop. Do not re-ask, do not re-frame it as a smaller ask, do not
   raise it again later in the session. Offer `[feedback] mode = "off"` if they
   would rather never be asked again.

If they want the redaction changed — a module name they would rather keep, a term
that got missed — add it to `[feedback] redact_extra` (or set
`redact_identifiers = false` for an open-source design whose names are already
public), and preview the same IDs again. The token changes with the text, which
is the point.

## 5. Submission is host-only

Day-to-day work happens inside the Session Runtime, whose egress proxy allowlists
model APIs and not github.com — and holds no mail client either, so the `email`
route is host-only for the same reason. `submit` will tell you so rather than
fail mysteriously. When that happens: finish the user report here, then either
hand the user the host command or run `booley feedback export F-8 F-9` for the
same batch.

A user who sends it by hand should say so, so the entry is never sent twice:

```console
booley feedback filed F-1 F-2 --url https://github.com/boldaxolotl/Booley/issues/123
booley feedback filed F-1 F-2 --url email     # after sending the mailto: hand-off
```

`submit` does this automatically when it files an issue, and never on the email
route — it has no way of knowing whether the mail was actually sent. Filed
entries are excluded from every later report — which is what stops a bug filed
in July from re-publishing a setup run's findings from March.

## What not to do

- **Do not file without asking.** Ever. Irreversible either way — a public issue
  or a mail already in someone's inbox.
- **Do not report a workaround as a success.** If you worked around Booley to get
  the user moving, that workaround is a `booley` finding — and it is the one that
  will confuse them in three months.
- **Do not pad the batch.** Three real findings beat eleven with eight
  impressions in them.
- **Do not attach a file you have not looked at.** Redaction is a denylist over
  identifiers scraped from `booley.toml` and the `.core` files — it does not know
  what is in an arbitrary log line. Read the tail you are attaching; the preview
  shows the user the same text, but you saw it first.
