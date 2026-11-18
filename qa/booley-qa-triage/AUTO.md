# Automatic triage

Triage without the maintainer: a **jury** of models from `jury.toml` judges
every cluster blind, and a cluster is decided only when the jury is
**unanimous**. Every other cluster is **escalated**: logged with each juror's
verdict for the maintainer's next interactive triage. Decided clusters are
acted on exactly as in interactive mode, including posting the brief and
`ready-for-agent`, so the issue pipeline can pick them up.

## 1. Seat the jury

- Automatic mode runs inside T3 Code only, where `delegate_task` reaches every
  juror's provider. Call `orchestrator_capabilities` once; when it is not
  available, stop.
- Read `jury.toml` beside this file. A `jury=<model>,<model>,...` argument
  replaces it; each listed model runs at `high` effort on the provider whose
  catalog lists it.
- Check every juror against `orchestrator_capabilities` (use `search` with the
  model ID): its provider must be enabled with `canRunChildTask`. A jury
  needs at least two jurors.

On any stop, write the reason to the top of `<run-dir>/triage.md` and tell the
user which juror or capability is missing and the command to rerun. Run every
juror the jury names, or none: a smaller jury silently weakens the guardrail.

## 2. Prepare

Run interactive steps 1–4 (Read, Cluster, Check the tracker, Open the
decision log) from `SKILL.md`, logging the overview in `triage.md` instead
of showing it. Clusters an existing `triage.md` already decided stay
decided. Your tracker match is a prompt for your own
reading, not part of the jury packet. Run `git fetch origin` once, before the
jurors start, so they all judge the same `origin/main`.

## 3. Deliberate

1. **Packet.** Write one packet to `<run-dir>/jury/packet.md`: the source
   checkout path, the run directories, the `origin/main` SHA, and each
   cluster's ID and finding references. Give jurors raw findings only: your
   cluster analysis, tracker matches, and recommendations stay out, so each
   verdict is independent.
2. **Convene.** Start every juror in parallel with `delegate_task`
   (`role: review`, `isolation: shared`, the juror's provider and model, and
   `options: {effort: <effort>}`). Its task is `JUROR.md` from this directory,
   verbatim, followed by the packet. Give one juror at most eight clusters per
   task; for more, start further tasks of eight for every juror.
3. **Collect.** Save each juror's JSON to `<run-dir>/jury/<juror-slug>.json`.
   A juror that fails, times out, or returns invalid JSON gets one retry with
   the same task; after that, every cluster in its task is escalated with the
   reason `juror <name> returned no verdict`.

Done when every juror has a verdict or an escalation for every cluster.

## 4. Tally

A cluster is **decided** when every juror returns `shape: ok` and the same
`action`, and they also agree on:

| Action | Must also match |
|---|---|
| `file` | `category` |
| `comment` | `issue` |
| `fix-mission` | — |
| `drop` | — |

Merge the rest: `severity` is the highest any juror gave; `blast_radius` is
the most cautious (`yes`, then `likely`, then `no`).

**Fix direction** (decided `file` clusters only). Write the jurors' `fix`
proposals as lettered options, merging those that say the same thing, and
send them to every juror in one more parallel round: the cluster, the
options, and "Pick one letter, or `none`. Return `{"cluster", "pick",
"reason"}` JSON." A unanimous letter is the approved fix direction. Without
one, the issue is still filed, as open design: `needs-triage`, no brief.

Every cluster not decided is **escalated**.

## 5. Act

For each decided cluster, in interactive order (`qa-bug` first, then by
severity), act exactly as interactive step 6 describes, with the approved fix
direction standing in for the maintainer's decision. The issue body names the
jury (`Automatic triage: <juror names>, unanimous`). A confidential-content
guard hit escalates the cluster instead of filing it. Log each decision in
`triage.md` as it lands, with the jury tag `auto`.

Log each escalated cluster in `triage.md` as `escalated`, with every juror's
`action`, `category`, `issue`, and `reason` on one line each, and the fix
picks when the escalation came from the fix round.

## 6. Finish

Apply mission fixes as interactive step 7 does, then commit them on that
local branch; push or open a PR only when the user asks. Report as in
interactive step 8, plus:

- every `drop` with its reason, so the maintainer can veto it;
- the escalated clusters, and that `/booley-qa-triage <run-dir>` resumes on
  them interactively;
- juror failures and retries.
