# Merge queue

Mergify is the only normal merge path into `main`. It merges in queue order and
validates up to three cumulative candidates concurrently: A, A+B, and A+B+C,
not three unrelated changes against the same old `main`.

## Enable speculative checks

Before enabling multiple check slots, verify that `ci-required` and
`confidential-content` succeed on a draft-PR compatibility canary. Mergify uses
temporary draft batch PRs for speculative validation, so ordinary PR success
does not establish compatibility with their metadata and cumulative commits.

The `main_protection` ruleset must have **Require branches to be up to date
before merging** disabled (`strict_required_status_checks_policy: false`).
Strict checks limit Mergify to one check slot even when `max_parallel_checks`
is three. Retain both required checks, the rest of `main_protection`, and the
separate `mergify_exclusive` ruleset. Coordinate this live setting with rollout;
a configuration PR cannot change the GitHub ruleset. Runner capacity must
support three concurrent CI runs.

See [Mergify queue modes](https://docs.mergify.com/merge-queue/queue-modes/)
and [parallel checks](https://docs.mergify.com/merge-queue/performance/).

## Queue a ready PR

Queue only a final, non-draft PR targeting `main` after its initial required
GitHub Actions checks pass. The authenticated GitHub identity needs write
permission. Mergify then validates the cumulative candidate against `main` and
its queue predecessors.

```bash
printf '%s' '@mergifyio queue default' > /tmp/booley-mergify-queue.txt
python3 .github/scripts/confidential_content_guard.py --repo . publish-pr comment \
  --pr <number> --body-file /tmp/booley-mergify-queue.txt
```

Confirm that the `Mergify Merge Queue` check or status comment says queued.
GitHub's merge button, auto-merge, and `gh pr merge` do not queue a PR.

## Own one queued PR

Exactly one agent owns a queued PR until Mergify reports it merged or dequeued;
only that owner queues or dequeues it. If another session is managing the PR,
leave its queue state unchanged and coordinate a handoff. An unexpected queue
or dequeue comment is evidence of another owner: pause state-changing commands
until ownership is clear.

After Mergify accepts the queue command, observe that PR once using the
watcher below or, when observing manually, read its status once:

```bash
gh pr view <number> --json state,mergedAt,labels,statusCheckRollup,comments
```

## Run one read-only watcher

Establish queue ownership before starting a watcher. The watcher is an observer,
not an ownership lock: use one foreground process for the selected PR, and have
any second agent coordinate with the owner. It reads only; it never queues,
dequeues, reruns, pushes, merges, comments, or cleans up.

For ordinary PR CI, capture the selected PR head and use a deadline that covers
the documented cold-cache validation time:

```bash
python3 .github/scripts/watch_pr.py \
  --repo OWNER/REPO --pr <number> --mode ci \
  --expected-head <40-character-sha> --timeout-seconds 7200 \
  --producer-registration-grace-seconds 300
```

For an owned queued PR, include predecessor wait in the caller-selected
deadline. Two hours is an example, not a completion guarantee; Mergify's
configured validation bound is 90 minutes after the PR reaches its check slot:

```bash
python3 .github/scripts/watch_pr.py \
  --repo OWNER/REPO --pr <number> --mode queue --timeout-seconds 7200
```

The process prints one startup line and one final JSON summary. Resume its
existing session while it waits. Keep surrounding agent-tool waits at 60 seconds
or less, report elapsed time from the last summary, and issue no parallel GitHub
status queries. The watcher polls CI every 60 seconds and queue state every ten
minutes. An expired deadline is unresolved observation, not failure; choose a
new deadline explicitly instead of restarting automatically.

CI outcomes are `ci_passed` (exit 0), `check_failed`, `closed`, `head_changed`,
or `required_checks_missing` (exit 1), `timeout` (exit 124), and
`observation_error` (exit 2). Only required checks reported as `pass` satisfy
CI. Pending, skipped, or neutral checks remain unresolved; a cancelled required
check is actionable. The expected head is checked before and after each status
read, and results from an explicitly different head are ignored.

Because required contexts can register after their workflows start, the watcher
matches each absent context to its Actions producer by exact expected head,
workflow name, and event. A queued, running, or approval-blocked producer stays
pending without a registration deadline. A completed producer that omitted its
context is immediately `required_checks_missing`. With no matching producer, a
five-minute registration grace starts; it cannot exceed one hour or the overall
CI deadline. Unreadable producer data, empty required rules, or an unknown
producer mapping fails closed as `observation_error`. The final summary names
missing contexts and includes available producer-run links.

For `required_checks_missing`, make one detailed PR/check/Actions read to verify
the result. Confirm that the PR is ordinary, open, non-draft, not queued, and
still at the expected head; then reconfirm the required checks and exact-head
producer runs:

```bash
gh pr view <number> --json state,isDraft,headRefOid,labels
gh pr checks <number> --required
gh run list --repo OWNER/REPO --commit <expected-head> \
  --json workflowName,event,status,conclusion,headSha,attempt,url
```

If no producer run exists, record in the owning task that its single recovery is
consumed. Then run the guarded retry below from a clean worktree. It proves the
local and PR heads still match, creates one empty commit, stops if the tree
changes, and pushes only that tree-preserving head.

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

Restart one watcher for `new_head` and record the new head/tree relationship:

```bash
python3 .github/scripts/watch_pr.py \
  --repo OWNER/REPO --pr <number> --mode ci \
  --expected-head "${new_head}" --timeout-seconds 7200 \
  --producer-registration-grace-seconds 300
```

Never use GitHub's update-branch operation or `workflow_dispatch`, or mutate a
queued PR. If a matching producer is queued, running, or awaiting approval, keep
waiting. If it completed without the required context, report a CI incident
instead of retrying. If the one retry also has no producer, report an incident
and stop. The equal tree hashes retain local verification tied to that tree.

Queue outcomes are `merged` (exit 0), `dequeued`, `closed`, `check_failed`,
`queue_failed`, or `competing_control` (exit 1), `timeout` (exit 124), and
`observation_error` (exit 2). `mergedAt` is the merge confirmation; a closed PR
without it is not a merge. The initial comment snapshot baselines historical
queue/dequeue commands, while later exact control commands are competing-owner
evidence. The watcher relies on the configured `queued`,
`merge-queue-checking`, and `dequeued` labels plus Mergify check data, not new
bot status comments. Queue head updates are expected. An old failed attempt
does not stop a newer pending or same-head retry. Likewise, a `dequeued` label
beside `queued` or `merge-queue-checking` is the previous attempt's leftover
during a requeue, not a new dequeue.

Cancellation is `cancelled` (exit 130 for SIGINT or 143 for SIGTERM).

After an actionable outcome, fetch detailed checks or failure logs once under
the recovery rules below. A `merged` result tells the caller to perform final
confirmation and authorized cleanup; the watcher does neither. To stop
monitoring, send SIGINT or SIGTERM. The watcher terminates its child read and
reports `cancelled` with the conventional signal exit code.

A non-null `mergedAt` finishes the wait; a `dequeued` label starts recovery.
Otherwise, ownership is passive: leave predecessors to their owners and trust
Mergify's serial priority queue. Wait ten minutes, check only the owned PR once,
then repeat the quiet wait if unchanged. Do not query GitHub during an interval.

Mergify gives PRs with either of these labels the same high-priority tier:

- `urgent`: an incident or regression whose delay is actively blocking or
  degrading repository development or a release.
- `ci`: a change whose primary purpose is to restore or materially improve
  required CI or merge infrastructure.

Apply a priority label before queueing. Reserve `urgent` for active impact, not
ordinary importance or deadlines. High-priority PRs are FIFO and precede
unlabelled work. Priority changes do not interrupt running checks; an expedited
PR follows that work, preserving CI time already spent.

Outside the `required_checks_missing` procedure above, use one-shot
`gh pr checks <number>` only after the owned PR reports a failed required check
or Mergify dequeues it. A predecessor failure needs no action from waiting
agents: Mergify advances the queue while that PR's owner handles recovery.

Never change a queued branch. To add a commit, dequeue first:

```bash
printf '%s' '@mergifyio dequeue' > /tmp/booley-mergify-dequeue.txt
python3 .github/scripts/confidential_content_guard.py --repo . publish-pr comment \
  --pr <number> --body-file /tmp/booley-mergify-dequeue.txt
```

After Mergify confirms the dequeue, push the change, wait for ordinary PR CI to
pass, then use the queue command above. Agents never reorder entries manually;
the configured labels are the only priority mechanism.

## Recover a dequeued PR

Read the Mergify status and underlying Actions failure, then:

- For a deterministic test, lint, confidential-content, or merge-conflict
  failure, fix it and obtain another ordinary green PR run before requeueing.
- For a confirmed infrastructure interruption, requeue unchanged with the
  command above.
- For an unexplained or repeated interruption, report an incident; do not retry
  until it happens to pass.

Record the cause in the PR. Recovery ends when the fixed or confirmed-transient
candidate has been queued once.

## Finish after merge

Confirm the merge before cleanup:

```bash
gh pr view <number> --json state,mergedAt,mergeCommit,url
```

A non-null `mergedAt` indicates success; closed or dequeued does not. Then delete
the remote branch, local branch, and worktree as required by `AGENTS.md`.

## Maintainer incident controls

Manual merging requires a maintainer incident decision; it is never an agent
fallback. During a Mergify or CI incident, maintainers pause the queue, record
affected entries, and restore service or disable `mergify_exclusive` before
using GitHub's protected PR merge path. Keep `main_protection` enabled. Never
uninstall Mergify while it is the only actor allowed to update `main`.

Account-scoped Mergify API keys are not for agents. Agents use the GitHub
comment commands; maintainers use the Mergify dashboard to pause, resume,
inspect the queue, or change exclusive mode.
