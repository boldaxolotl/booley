# Merge queue

This runbook is for agents and maintainers who merge PRs into `main`. Read it
before you queue, dequeue, retry, watch, or clean up a PR. The contributor's
branch and PR workflow is in [CONTRIBUTING.md](../CONTRIBUTING.md), and
publishing PR text and the `confidential-content` check are in
[confidential-content.md](confidential-content.md).

## How the queue works

Mergify is the only normal merge path into `main`; GitHub's merge button,
auto-merge, and `gh pr merge` do not queue a PR. `.mergify.yml` holds the
configuration and `tests/ci/test_mergify_config.py` pins it.

- **Cumulative candidates.** The queue is serial: PRs merge in queue order.
  Mergify validates up to four candidates at once, and each candidate adds a
  batch of up to three PRs to the one before it, so up to 12 PRs are in
  flight. With batches A, B, C, and D, the candidates are A, A+B, A+B+C, and
  A+B+C+D. Each candidate is tested on top of `main` and everything queued
  ahead of it, so a passing candidate is exactly what `main` becomes.
- **Partial batches.** A batch starts after at most 30 seconds of waiting for
  batchmates, so a lone PR is not held for a full batch.
- **Candidate PRs.** Mergify validates each candidate through a temporary
  draft PR from a `mergify/merge-queue/` branch. The `Tests` workflow skips
  drafts but runs these, and both required checks, `ci-required` and
  `confidential-content`, must pass on them.
- **Time bound.** Each validation round may take 90 minutes after the PR
  reaches its check slot (a cold-cache image build takes about an hour).
  Mergify retries nothing automatically.
- **Failed batches.** When a batch fails, Mergify splits it and validates the
  parts until it isolates the culprit, dequeues only that PR, and merges the
  rest. See [React to a failed batch](#react-to-a-failed-batch).
- **Labels.** Mergify posts no status comments, so it does not notify every
  participant. Read its state from the `Mergify Merge Queue` check and the
  `queued`, `merge-queue-checking`, and `dequeued` labels.
- **Who may command.** Only a GitHub identity with write permission can post
  `@mergifyio queue` or `@mergifyio dequeue`.

### Priority

PRs labeled `urgent` or `ci` share one high-priority tier, ahead of unlabeled
work and first-in, first-out within it:

- `urgent`: an incident or regression whose delay is actively blocking or
  degrading repository development or a release. Reserve it for active
  impact, not ordinary importance or deadlines. Renovate security PRs carry it
  by configuration; keep it unless the advisory does not affect Booley, and
  then remove it before queueing.
- `ci`: a change whose main purpose is to restore or materially improve
  required CI or merge infrastructure.

Apply the label before queueing. A priority change does not interrupt running
checks, so an expedited PR waits for them and the CI time already spent is
kept. Labels are the only priority mechanism; agents never reorder the queue.

### Live GitHub settings

The queue depends on repository settings that a configuration PR cannot
change. Maintainers own them; agents only read them.

- The `main_protection` ruleset requires `ci-required` and
  `confidential-content`, and does not require branches to be up to date
  before merging (`strict_required_status_checks_policy: false`). Strict
  checks would limit Mergify to one check slot.
- The separate `mergify_exclusive` ruleset makes Mergify the only actor that
  can update `main`.
- Runner capacity supports four concurrent CI runs.
- A new required check must succeed on a draft candidate PR before it is
  required, because ordinary PR success says nothing about Mergify's draft
  metadata and cumulative commits.

Read the live required-check settings with:

```bash
gh api 'repos/{owner}/{repo}/rules/branches/main' \
  --jq '.[] | select(.type=="required_status_checks") | .parameters'
```

See [Mergify queue modes](https://docs.mergify.com/merge-queue/queue-modes/)
and [parallel checks](https://docs.mergify.com/merge-queue/performance/).

## Own one queued PR

Exactly one agent owns a queued PR until Mergify reports it merged or
dequeued, and only that owner queues or dequeues it. If another session is
managing the PR, leave its queue state unchanged and coordinate a handoff. An
unexpected queue or dequeue comment is evidence of another owner: pause
state-changing commands until ownership is clear.

Ownership is passive while the PR waits: the owner holds the authority to
queue or dequeue, runs one watcher, and changes nothing. Waiting needs no
review or test capacity, so an orchestrated run may schedule other work
meanwhile. The authority stays with the owner until one of these is recorded
in the owning task:

- **Handoff.** A living owner names its successor, the successor
  acknowledges, and only then does the successor act.
- **Replacement.** The owner's session is proven to have ended, and every
  state-changing command it started has finished. A session that stops
  answering is not proof, and no elapsed time authorizes a takeover.

When neither can be established, report an ownership incident and wait for
the maintainer to confirm that the old session and its processes have
stopped.

## Post a queue or dequeue command

Post every command through the confidential-content publisher, with this
guard each time:

1. Stop the watcher and confirm that it and its child `gh` reads have exited.
2. Read the PR's control comments and queue labels once. Stop if they show a
   command this owner did not post.
3. Record the intended command in the owning task, then post it unchanged:

    ```bash
    printf '%s' '@mergifyio queue' > /tmp/booley-mergify-queue.txt
    python3 .github/scripts/confidential_content_guard.py --repo . publish-pr comment \
      --pr <number> --body-file /tmp/booley-mergify-queue.txt
    ```

    For a dequeue, post `@mergifyio dequeue` the same way (for example from
    `/tmp/booley-mergify-dequeue.txt`).
4. Record the comment URL from the `GitHub reference:` line, then start a new
   watcher. Its baseline now includes that comment.

If the response is lost and the comment URL is unknown, an identical comment
under a shared login cannot be attributed to this owner. Treat it as an
ownership incident: keep observing read-only, post no further command, and
ask the maintainer to decide.

## Queue a ready PR

1. Queue only a final, non-draft PR that targets `main`. A draft PR runs no
   `Tests`: mark it ready with `gh pr ready <number>` and wait for
   `ci-required` and `confidential-content` to pass, for example with a
   [CI-mode watcher](#watch-a-pr).
2. Leave a PR labeled `confidential-review` alone. It is an outside PR with
   advisory confidential-content findings; only a maintainer queues it, after
   reviewing the advisory comment and removing the label. Nothing removes the
   label automatically, and Mergify's queue conditions do not check it (see
   [confidential-content.md](confidential-content.md#provenance-rules)).
3. A PR that is only behind `main` is ready as it is; see
   [Tell a stale base from a conflict](#tell-a-stale-base-from-a-conflict).
4. Apply a [priority label](#priority) if one fits.
5. Post `@mergifyio queue` as in
   [Post a queue or dequeue command](#post-a-queue-or-dequeue-command).
6. Confirm that the `Mergify Merge Queue` check or the `queued` label shows
   the PR queued, then start one queue-mode watcher.

## Watch a PR

`.github/scripts/watch_pr.py` is a read-only, deadline-bounded watcher. It
never queues, dequeues, reruns, pushes, merges, comments, or cleans up.

### Run one watcher

Establish queue ownership before starting a watcher. A watcher is an observer,
not an ownership lock: run one per PR, and have any second agent coordinate
with the owner. A live watcher is the only GitHub observer for its PR.

Run it in the foreground of the owner's session, or as the foreground child
of a finite supervisor: a process that starts this one watcher, saves its
startup line and final JSON summary to files, records the watcher's exit
status separately, and exits with it. A supervisor lets the watcher outlive a
single tool wait and lets a resumed owner adopt it from those files instead
of starting a second one. It is not a daemon and does not restart the
watcher.

For ordinary PR CI, pass the PR head you are waiting on and a deadline that
covers a cold-cache validation:

```bash
python3 .github/scripts/watch_pr.py \
  --repo OWNER/REPO --pr <number> --mode ci \
  --expected-head <40-character-sha> --timeout-seconds 7200 \
  --producer-registration-grace-seconds 300
```

For a queued PR, the deadline must also cover the wait behind queue
predecessors. Two hours is an example, not a completion guarantee: each
validation round may take 90 minutes, and a failed batch is split and
validated again, so a batchmate of a failing PR can wait several rounds.

```bash
python3 .github/scripts/watch_pr.py \
  --repo OWNER/REPO --pr <number> --mode queue --timeout-seconds 7200
```

The watcher prints one startup line and one final JSON summary. It polls CI
every 60 seconds and queue state every ten minutes. While it waits:

- Resume its session or read its saved output; keep surrounding agent-tool
  waits at 60 seconds or less.
- Report elapsed time from the last summary.
- Issue no other GitHub status queries for the PR. Run one-shot
  `gh pr checks <number>` only after the owned PR reports a failed required
  check or Mergify dequeues it, or in
  [Recover missing required checks](#recover-missing-required-checks).
- Ignore a failing predecessor or batchmate: Mergify advances the queue while
  that PR's owner handles recovery.

To stop the watcher, send SIGINT or SIGTERM: it ends its child read and
reports `cancelled`.

To restart a watcher that died, first confirm that it and its child `gh`
reads are gone. Then read the control comments posted since the last comment
it observed, because a new watcher's baseline would hide a foreign queue or
dequeue command posted in between. Keep the original overall deadline.

### Without a watcher

To observe a queued PR by hand, read it once:

```bash
gh pr view <number> --json state,mergedAt,labels,statusCheckRollup,comments
```

A non-null `mergedAt` finishes the wait; a `dequeued` label starts
[React to a failed batch](#react-to-a-failed-batch). Otherwise wait ten
minutes without querying GitHub, read the owned PR once more, and repeat.
Leave predecessors to their owners.

### Outcomes

| Outcome | Mode | Exit | Next step |
|---------|------|------|-----------|
| `ci_passed` | ci | 0 | Every required check passed at the expected head; queue the PR. |
| `merged` | queue | 0 | Follow [Finish after merge](#finish-after-merge). |
| `check_failed` | both | 1 | Read the failing checks once; in queue mode see [React to a failed batch](#react-to-a-failed-batch). |
| `required_checks_missing` | ci | 1 | Follow [Recover missing required checks](#recover-missing-required-checks). |
| `head_changed` | ci | 1 | The PR head is not the expected head; read the PR once and report. |
| `closed` | both | 1 | The PR closed without merging; read it once and report. |
| `queue_failed`, `dequeued` | queue | 1 | Follow [React to a failed batch](#react-to-a-failed-batch). |
| `competing_control` | queue | 1 | Another queue or dequeue command appeared; see [Own one queued PR](#own-one-queued-pr). |
| `timeout` | both | 124 | Unresolved, not failed: choose a new deadline explicitly instead of restarting automatically. |
| `observation_error` | both | 2 | The watcher could not read what it needs; unresolved, report it. |
| `cancelled` | both | 130 or 143 | Stopped by SIGINT (130) or SIGTERM (143). |

After any actionable outcome, fetch detailed checks or failure logs once,
under the recovery rules below.

### How CI mode decides

- Only required checks reported as `pass` satisfy CI. Pending, skipped, or
  neutral checks stay unresolved; a cancelled required check counts as
  failed.
- The expected head is checked before and after each status read, and
  results from a different head are ignored. An abbreviated expected head (at
  least seven hex digits) is expanded on the first read to the PR head it
  prefixes; a non-matching prefix reports `head_changed`.
- A required context can register after its workflow starts, so the watcher
  matches each absent context to its Actions producer by exact head, workflow
  name, and event. A queued, running, or approval-blocked producer stays
  pending with no registration deadline. A completed producer that never
  reported its context is `required_checks_missing` at once. With no matching
  producer, a registration grace starts (`--producer-registration-grace-seconds`,
  default five minutes, at most one hour or the CI deadline).
- Unreadable producer data, empty required rules, or an unknown producer
  mapping fail closed as `observation_error`. The final summary names missing
  contexts and links the producer runs it found.

### How queue mode decides

- `mergedAt` is the merge confirmation; a closed PR without it is not a merge.
- The first comment read sets the baseline of earlier queue and dequeue
  commands. Any later exact control command is evidence of a competing owner.
- The watcher reads the `queued`, `merge-queue-checking`, and `dequeued`
  labels and Mergify's check data. Queue head updates are expected, and an old
  failed attempt does not end a newer pending or same-head retry.
- A `dequeued` label beside `queued` or `merge-queue-checking` is left over
  from the previous attempt of a requeue, not a new dequeue. So is a lone
  `dequeued` label applied before the newest control comment when that
  comment is a queue command: the watcher keeps waiting and reports
  `dequeued` only if the label is still that old one poll (ten minutes)
  later. A `dequeued` label applied after the queue command is reported at
  once.

## Recover missing required checks

For `required_checks_missing`, make one detailed read to verify it. Confirm
that the PR is ordinary, open, non-draft, not queued, and still at the
expected head; then confirm the required checks and the producer runs for
that exact head:

```bash
gh pr view <number> --json state,isDraft,headRefOid,labels
gh pr checks <number> --required
gh run list --repo OWNER/REPO --commit <expected-head> \
  --json workflowName,event,status,conclusion,headSha,attempt,url
```

- If a matching producer is queued, running, or awaiting approval, keep
  waiting.
- If a producer completed without the required context, report a CI incident
  instead of retrying.
- If no producer run exists, record in the owning task that this PR's single
  retry is used, then run the guarded retry below from a clean worktree. It
  proves the local and PR heads still match, creates one empty commit, stops
  if the tree changes, and pushes only that tree-preserving head. Equal tree
  hashes keep the local verification valid for the new head.

```bash
expected_head=<40-character-sha>
pr_head=$(gh pr view <number> --json headRefOid --jq .headRefOid)
test "${pr_head}" = "${expected_head}" || { echo 'PR head changed'; exit 1; }
test "$(git rev-parse HEAD)" = "${expected_head}" || {
  echo 'local head does not match the PR'; exit 1;
}
git diff --quiet || { echo 'tracked worktree changes present'; exit 1; }
git diff --cached --quiet || { echo 'staged changes present'; exit 1; }
verified_tree=$(git rev-parse HEAD^{tree})
git commit --allow-empty -m "ci: retrigger required workflows"
new_head=$(git rev-parse HEAD)
test "$(git rev-parse HEAD^{tree})" = "${verified_tree}" || {
  echo 'empty commit changed the tree'; exit 1;
}
git push
```

Record the new head and its unchanged tree in the owning task, then start one
watcher for `new_head`:

```bash
python3 .github/scripts/watch_pr.py \
  --repo OWNER/REPO --pr <number> --mode ci \
  --expected-head "${new_head}" --timeout-seconds 7200 \
  --producer-registration-grace-seconds 300
```

If the retry also has no producer, report an incident and stop. Never use
GitHub's update-branch operation or `workflow_dispatch` instead, and never
push to a queued PR.

## React to a failed batch

When a batch fails, Mergify does not dequeue all of its PRs. It splits the
batch, validates the parts, merges the passing parts, returns the parts after
a failing part to the queue, and splits the failing part again until one PR
remains. That PR is the culprit and is dequeued with the failed checks as the
cause. If every part passes, Mergify blames the batch's last PR, which is
never retested alone. Each split round adds validation time.

- **Your PR is still queued:** do nothing. No requeue, dequeue, or push:
  Mergify merges passing parts and requeues later ones by itself.
- **`check_failed` in queue mode:** a required check failed on the PR's own
  head, never on a batch draft. Treat it as your PR's failure: follow
  [Recover a dequeued PR](#recover-a-dequeued-pr), dequeuing first if the PR
  is still queued.
- **`queue_failed` while the PR still has `queued` or `merge-queue-checking`
  and no `dequeued` label:** read the Mergify check once. If it shows the
  batch being split or the PR requeued, do not restart the watcher yet: a new
  watcher reports `queue_failed` again at once while the Mergify check still
  fails. Wait ten minutes, read the PR once with `gh pr view`, and repeat
  while it stays queued, up to the original deadline (a split round can take
  the full 90 minutes). When the Mergify check stops failing, start one new
  watcher under the restart rules. If the deadline passes with the PR still
  queued and the Mergify check failing, report an incident.
- **`dequeued`:** read the Mergify check once and confirm that Mergify did not
  already requeue the PR after a split (an active queue label is back). If it
  did, start one new watcher instead of requeueing. Only a confirmed dequeue
  starts [Recover a dequeued PR](#recover-a-dequeued-pr).
- **Blamed as a batch's last PR without a solo check:** compare the failing
  job with the PR's own diff to judge flake or deterministic failure before
  applying the known-flake rule. A flake costs batchmates a split round.

## Recover a dequeued PR

Read the Mergify check and the underlying Actions failure, then:

- **Deterministic failure** (test, lint, confidential-content, or merge
  conflict): fix it and get another ordinary green PR run before requeueing.
- **Confirmed infrastructure interruption:** requeue unchanged.
- **Known flake:** requeue unchanged once. A failure is a known flake when the
  PR's diff does not touch the failing test or the code it exercises, and
  either an open issue describes the same failure or recent `main` runs of
  that job show it alongside green runs.
    - A push to `main` runs only `changes`, `release-semantic`, `build-dist`,
      and `bwave-smoke`, so `main` evidence for every other job, including
      `package-artifacts` and the test jobs, comes from the nightly
      scheduled `Tests` run.
    - Match a crashed worker or a duration-budget overrun by job and error
      signature instead of a test ID.
    - When no issue exists, first file a `bug`, `ci`, `needs-triage` issue
      with the run link, test ID or job, and error, scanned as `AGENTS.md`
      requires.
    - When `main`'s latest run of that job (the latest nightly run, for jobs
      a `main` push skips) fails the same way, `main` has regressed: report
      it instead of requeueing.
    - A second failure of the same flake on the same PR is a repeated
      interruption.
- **Unexplained or repeated interruption:** report an incident; do not retry
  until it happens to pass.

Never change a queued branch. To add a commit, dequeue first, wait for
Mergify to confirm the dequeue, push, and wait for ordinary PR CI to pass.
Post each queue or dequeue command as in
[Post a queue or dequeue command](#post-a-queue-or-dequeue-command).

Record the cause, with any flake issue link, in a PR comment. Recovery ends
when the fixed or confirmed-transient head has been queued once.

## Tell a stale base from a conflict

A PR that is only behind `main` is ready to queue: `main_protection` does not
require an up-to-date branch, and Mergify validates the PR on top of `main`,
its queue predecessors, and its batchmates. Leave such a branch as it is, so
the local verification stays tied to its tree. Rebase or merge `main` only
for a real conflict or for a change the PR depends on, then verify the new
tree again.

Before relying on this, read the [live setting](#live-github-settings). If
it is unreadable or requires an up-to-date branch, update the branch before
queueing.

## Finish after merge

Confirm the merge before cleanup:

```bash
gh pr view <number> --json state,mergedAt,mergeCommit,url
```

Only a non-null `mergedAt` is success; closed or dequeued is not. Then delete
the remote branch, the local branch, and the worktree, as `AGENTS.md`
requires.

## Maintainer incident controls

Manual merging needs a maintainer's incident decision; it is never an agent
fallback. During a Mergify or CI incident, maintainers pause the queue,
record the affected entries, and either restore service or disable
`mergify_exclusive` before using GitHub's protected PR merge. Keep
`main_protection` enabled. Never uninstall Mergify while it is the only actor
allowed to update `main`.

Account-scoped Mergify API keys are not for agents. Agents use the GitHub
comment commands; maintainers use the Mergify dashboard to pause, resume,
inspect the queue, or change exclusive mode.
