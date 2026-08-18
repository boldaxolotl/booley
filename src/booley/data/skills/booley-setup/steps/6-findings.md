# Step 6 — Findings: report, triage, and the optional bug report

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
section. An impression rides the same preview and consent as everything else, so
logging one commits them to nothing.

## 2. Write the one report

```console
booley feedback report --project-name <name>
```

Writes `.booley_project/SETUP-REPORT.md` — the user's copy, unredacted and never
published. The maintainer-facing redacted view is derived transiently by
`preview`/`submit`, not saved as a second report. `booley feedback export`
persists it only when the user explicitly asks for a sanitized file.

The report stays inside `.booley_project/`. **Do not** put it in the RTL repo's
tracked tree; that is the footprint guardrail. The enclosing maintainer dogfood
flow may explicitly tell this step to pass `--user-report-path SETUP-REPORT.md`
for its throwaway clone. That writes the root report instead of an inner copy;
on a real project, never pass the flag.

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

## 4. Offer the bug report — once, honestly, and take no for an answer

Only if `booley feedback report` says filable findings are available. Skip the
whole step silently when `[feedback] mode` is `off` or `file-only`.

```console
booley feedback preview F-2 F-6 F-7
```

Pass exactly the IDs included in the offer you just walked through. Bare
`preview` is refused; `--all` intentionally includes older runs and
conversations, so do not use it here.

Show the user **the entire preview output, verbatim**. Do not summarize it, do
not paraphrase the redaction warnings, and do not oversell the contribution.
It already contains what they need to decide: the exact text, what was
substituted, what redaction structurally cannot catch, and who ends up seeing it
under whose name — a public issue carrying their GitHub name, or, under
`[feedback] mode = "email"`, a mail to the maintainer carrying their return
address. The preview says which; do not assert one when it says the other.

Then ask, plainly. Three answers, all fine:

1. **Yes** → pass the token the preview printed:
   ```console
   booley feedback submit F-2 F-6 F-7 --yes --confirm <token>
   ```
   Use the same IDs as `preview`; a different selection invalidates the token.
   `--yes` without the token is refused by design; the token proves the approval
   covers the exact text they read. **Never invent, guess, or scrape a token
   from an error message** — if you did not just show the user the preview, you
   have no business submitting.

   Under `mode = "email"` this prints a `mailto:` link and stops — **you** cannot
   send it and must not pretend it went anywhere. Give the user the link, and the
   `booley feedback filed … --url email` command it prints, for after they send.
2. **Not now / just give me the file** → export the same IDs, tell them the
   path, and stop. They can post it whenever, from any account.
3. **No** → stop. Do not re-ask, do not re-frame it as a smaller ask, do not
   bring it up again later in the run. Offer `[feedback] mode = "off"` if they'd
   rather never be asked again.

A submit that files an issue stamps everything it sent as filed, and filed
findings are excluded from every later report. The email route never stamps —
whether the mail was sent is not something Booley can see. Either way, if the
user sends it by hand, record that (`booley feedback filed F-3 F-7 --url <issue>`,
or `--url email`) — otherwise the next bug they report months from now drags this
whole batch along with it.

Later feedback is not this step's job: `/booley-feedback` handles anything that
turns up after setup is done, using the same log.

**If they want changes to the redaction** — a module name they'd rather keep, a
term that got missed — add it to `[feedback] redact_extra` (or set
`redact_identifiers = false` for an open-source project whose names are already
public), and preview the same IDs again. The token changes
with the text, which is the point.

**Submission is host-only.** Steps 2–5 run in the Session Runtime, whose egress
proxy allowlists model APIs and not github.com — and which has no mail client, so
`mode = "email"` is host-only for the same reason. If you are in-container,
`submit` will tell you so: finish the user report here, and either hand the user
the host command or export the same IDs (`booley feedback export F-2 F-6 F-7`).

## 5. Close out

Record in the final report: findings by severity, the top few called out, what
was triaged where, and what the user decided about the bug report — including a
decline. "The user declined to share findings upstream" is a normal line in a
successful setup.
