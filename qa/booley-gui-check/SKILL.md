---
name: booley-gui-check
description: Only when the user explicitly asks, check one Booley pull request's GUI change in T3 Code in a host browser, on a Sandbox built locally from the PR head, post screenshot evidence on the PR, and return pass, fail, or blocked per check step.
disable-model-invocation: true
---

# Check a PR's GUI change in T3 Code

Use this skill only when the user explicitly invokes it. Claude Code enforces
this through `disable-model-invocation` and Codex through
`agents/openai.yaml`; other clients get it as behavioral guidance only.

Before acting, resolve the real path of this loaded `SKILL.md` (following the
installed link). Its parent is the skill directory, its grandparent is `qa/`,
and the parent of `qa/` is the source checkout. Require that source to be a
clean primary checkout, with `.git` as a directory, on `main` unless the caller
named an unreleased copy (below), which may be on any clean branch; stop with
restore/re-enable guidance otherwise. The check installs the PR's own build as
the host's Booley for its duration and restores the one it found. Resolve
every input below from that source checkout, never from the process working
directory.

The procedure lives in the shared runbook, `qa/shared/t3/RUNBOOK.md`.
Read it in full before step 2; its hard rules apply throughout. This skill adds
what to check, how to report it, and what to clean up.

Inputs: a PR number and the issue number it closes. Optional: an artifact root
(default `~/workplace/qa-runs`). To trial an unreleased copy of this skill, the
caller names the source checkout to use instead. A pull request into the
`t3` branch needs this skill's copy from a `t3` checkout until `t3` lands on
`main`.

## Hard rules

- One check at a time per host: never run two of these, or this and the
  `gui` mission, at once.
- The check replaces the canonical host install (runbook step 1). Ask the
  maintainer before step 2 and make sure no other Booley run uses the host;
  without both, return `blocked: host install`.
- Write the check steps, with what each expects, before building anything or
  opening a browser tab. Don't change them after seeing the GUI; a step that
  turns out to be wrong is reported as `blocked: step wrong` with the reason,
  not quietly rewritten.
- External writes are limited to one evidence commit in
  `boldaxolotl/booley-gui-evidence` and one PR comment.
  Never push to the PR branch, review, approve, label, or merge.
- Leave the PR's code alone. A failure is reported, not fixed.

## Run directory

Create `<artifact-root>/gui-check-pr<n>-<UTC yyyymmddThhmmssZ>-<short>/` with
`checks.md` (the steps), `log.md` (identity, timings, decisions),
`resources.md` (append-only: resource, identity, how to release, released) and
`evidence/`. Add each external resource to `resources.md` before creating it.

## Procedure

1. **Plan the checks.** Read the issue (`gh issue view <issue>`), its
   acceptance criteria, and the PR (`gh pr view <n> --json
   headRefOid,headRefName,state,files` and `gh pr diff <n>`). Write 3-6 steps
   into `checks.md` as a table: step, action, expected, oracle. Each expected
   result is something a screenshot can show, and each step names its CLI
   oracle from the runbook's step 8 table, or "none" with what it is compared
   against instead. Cover every GUI-visible acceptance criterion; if none is
   GUI-visible, stop and return `n/a` with the reason. A step may run a
   Developer Agent in a T3 Code thread (runbook step 5) when the criterion
   needs one; never a bare `claude` or `codex` session. A criterion you can't
   reach (for example one that needs an Agent CLI that isn't signed in) goes
   under "Not checked" with the reason. With no closing issue
   (`gh pr view <n> --json closingIssuesReferences` is empty), take the
   criteria from the PR body and say so in `checks.md` and the comment.
2. **Candidate build.** Runbook steps 1-2. For an open PR, `<sha>` is its
   head. A PR from a fork would run unreviewed code on the host: stop and
   return `blocked: fork PR`. For a merged PR, check its base branch instead
   (its change is there; say so in the comment): fix `<sha>` once with
   `gh api repos/boldaxolotl/booley/commits/<base> --jq .sha` and keep it even
   if the branch moves during the check. Record in `log.md` the canonical
   revision the check replaced and the build times. A build that fails or
   takes over two hours is `blocked: image build`.
3. **Project and pairing.** Runbook steps 3-5. Record the time from
   `session up` to the Project shown in your tab. Start the Dashboard when a
   check step needs it.
4. **Run each step.** Act, take the screenshot (runbook step 9 naming, area
   `check`, for example `check-02-jobs-list.png`), run the oracle, bring the
   GUI back and wait one refresh, then give the step exactly one result: `pass`,
   `fail`, or `blocked`. No other result exists, such as a pass with caveats.
   Step 5 can still turn a `pass` into `blocked`. Write what the screenshot
   shows in a sentence.
   - `pass` only when a screenshot taken with the runbook's screenshot tool in
     this run shows every clause of the step's expected result: each element
     and its state (row present, order, color, layout). Text read from the DOM
     in the same state may supply exact values for an element the screenshot
     visibly shows; it never stands in for a missing screenshot.
   - Otherwise `fail` when the GUI showed something else, or `blocked` when the
     result could not be shown; say why.
   - When the screenshot tool fails, retry as runbook step 6 ("Snapshot
     timeouts") says, then mark the step `blocked` with the tool's error. An
     image made any other way (a local headless browser, page content rendered
     again, an image from an earlier run) is not evidence: don't post it.
   - A step whose precondition makes the expected result trivially true, such
     as an ordering check on a list with one row, is `blocked: precondition`;
     name the setup that was missing.

   Retry a step whose result is `fail` once after a refresh to rule out timing,
   keep both screenshots, and use the retry's result.
5. **Publish the evidence.** Look at every image first: the text guard can't
   read images. Drop any image that shows a pairing link or token, an email
   address, a private repo, or anything outside your tab's page, and retake it
   if the step needs one. If it can't be retaken, a `pass` becomes
   `blocked: image dropped`; a `fail` stays `fail`, and its seen cell says the
   image was dropped. If no image is kept, skip the evidence commit below and
   say so in the comment. Otherwise commit the kept images in a scratch clone:

   ```sh
   E=<run-dir>/scratch/evidence-repo
   git clone --depth 1 https://github.com/boldaxolotl/booley-gui-evidence "$E"
   mkdir -p "$E/pr-<n>/<sha>"
   cp <run-dir>/evidence/check-*.png "$E/pr-<n>/<sha>/"
   git -C "$E" add pr-<n>
   git -C "$E" -c user.name=boldaxolotl -c user.email=boldaxolotl@proton.me \
     commit -m "PR #<n> GUI check at <short>"
   git -C "$E" push origin HEAD:main
   git -C "$E" rev-parse HEAD   # <evidence-commit>
   ```

   Link each image as
   `https://raw.githubusercontent.com/boldaxolotl/booley-gui-evidence/<evidence-commit>/pr-<n>/<sha>/<file>`,
   pinned to the evidence commit so later pushes can't change it.
6. **Clean up.** Runbook step 10, which also restores the canonical host
   install. If a failed step needs a live repro, follow the runbook's
   exception instead: leave the Sandbox up and the candidate installed, and
   name the Project path and the candidate revision in the comment (step 7)
   and the return. Mark every `resources.md` row released or kept.
7. **Comment.** A check that ran its steps posts a comment for every
   verdict, `blocked` included. Draft it in `<run-dir>/comment.md`:
   - a first line with the verdict (`GUI check: pass`, `GUI check: fail`, or
     `GUI check: blocked`) and the head SHA checked;
   - a table with step, expected, seen, result (one row per `checks.md` step).
     Result is `pass`, `fail`, or `blocked` with its reason. Seen states only
     what a posted screenshot shows; a step with none says so;
   - the screenshots, each under its step number;
   - the candidate revision, and the build and pairing timings from steps 2
     and 3;
   - what was not checked, and why;
   - any kept resources (step 6).

   Post it through the guard, from the source checkout:

   ```sh
   python3 .github/scripts/confidential_content_guard.py --repo . publish-pr comment \
     --pr <n> --body-file <run-dir>/comment.md
   ```

   Record the `GitHub reference:` URL it prints in `log.md`. If the guard
   refuses, fix the draft and post again; never post around it.
8. **Return.** Tell the caller: the verdict, the per-step results (each
   `pass`, `fail`, or `blocked`) with what was seen, the criteria not checked,
   the comment URL, and anything kept or not cleaned up. The verdict is `pass`
   only if every step passed; any failed step makes it `fail`; with no failed
   step, any blocked step makes it `blocked`, returned with each blocked
   step's reason.
