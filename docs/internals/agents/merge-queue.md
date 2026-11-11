# Merge queue

Mergify is the only normal merge path into `main`. It merges in queue order and
validates up to four cumulative candidates concurrently, not four unrelated
changes against the same old `main`. Each candidate adds a batch of up to three
PRs, so up to 12 PRs are in flight: with batches A, B, C, and D the candidates
are A, A+B, A+B+C, and A+B+C+D. A partial batch starts after at most 30 seconds
of waiting for batchmates, so a lone PR is not held for a full batch.

## Enable speculative checks

Before enabling multiple check slots, verify that `ci-required` and
`confidential-content` succeed on a draft-PR compatibility canary. Mergify uses
temporary draft batch PRs for speculative validation, so ordinary PR success
does not establish compatibility with their metadata and cumulative commits.

The `main_protection` ruleset must have **Require branches to be up to date
before merging** disabled (`strict_required_status_checks_policy: false`).
Strict checks limit Mergify to one check slot even when `max_parallel_checks`
is four. Retain both required checks, the rest of `main_protection`, and the
separate `mergify_exclusive` ruleset. Coordinate this live setting with rollout;
a configuration PR cannot change the GitHub ruleset. Runner capacity must
support four concurrent CI runs.

See [Mergify queue modes](https://docs.mergify.com/merge-queue/queue-modes/)
and [parallel checks](https://docs.mergify.com/merge-queue/performance/).

## Queue a ready PR

Queue only a final, non-draft PR targeting `main` after its initial required
GitHub Actions checks pass. A draft PR runs no `Tests`, so mark it ready
(`gh pr ready <number>`) and wait for `ci-required` first. Mergify's own
speculative draft PRs are exempt and always run CI. The authenticated GitHub
identity needs write permission. Mergify then validates the cumulative
candidate against `main`, its queue predecessors, and the batchmates queued
with it.

```bash
printf '%s' '@mergifyio queue' > /tmp/booley-mergify-queue.txt
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

Ownership is passive while the PR waits: the owner holds the authority to
queue or dequeue, runs the watcher below, and changes nothing. Waiting for CI
or the queue needs no review or test capacity, so an orchestrated run may
schedule other work meanwhile. The authority stays with the owner until one of
these is recorded in the owning task:

- **Handoff.** A living owner names its successor, the successor acknowledges,
  and only then does the successor act.
- **Replacement.** The owner's session is proven to have ended, and every
  state-changing command it started has finished. A session that stops
  answering is not proof, and no elapsed time authorizes a takeover.

When neither can be established, report an ownership incident and wait for the
maintainer to confirm that the old session and its processes have stopped.

After Mergify accepts the queue command, observe that PR once using the
watcher below or, when observing manually, read its status once:

```bash
gh pr view <number> --json state,mergedAt,labels,statusCheckRollup,comments
```

## Run one read-only watcher

Establish queue ownership before starting a watcher. The watcher is an observer,
not an ownership lock: run one watcher for the selected PR, and have any second
agent coordinate with the owner. It reads only; it never queues, dequeues,
reruns, pushes, merges, comments, or cleans up.

Run the watcher in the foreground of the owner's session, or as the foreground
child of a finite supervisor: a process that starts this one watcher, saves its
startup line and final JSON summary to files, records the watcher's exit
status separately, and exits with it. A supervisor lets the watcher outlive a
single tool wait and lets a resumed owner adopt it from those files instead of
starting a second one. It is not a daemon and does not restart the watcher.

A live watcher is the only GitHub observer for its PR. Restart a watcher that
died only after confirming that it and its child `gh` reads are gone. First
read the control comments posted since the last comment it observed, because a
new watcher's baseline would hide a foreign queue or dequeue command posted in
between. Keep the original overall deadline.

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
configured validation bound is 90 minutes after the PR reaches its check slot.
The bound applies to each validation round, and a failed batch is split and
validated again, so a batchmate of a failing PR can wait several bounds; an
expired deadline stays unresolved observation:

```bash
python3 .github/scripts/watch_pr.py \
  --repo OWNER/REPO --pr <number> --mode queue --timeout-seconds 7200
```

The process prints one startup line and one final JSON summary. Resume its
existing session, or read its saved output, while it waits. Keep surrounding
agent-tool waits at 60 seconds or less, report elapsed time from the last
summary, and issue no parallel GitHub status queries. The watcher polls CI
every 60 seconds and queue state every ten minutes. An expired deadline is
unresolved observation, not failure; choose a new deadline explicitly instead
of restarting automatically.

CI outcomes are `ci_passed` (exit 0), `check_failed`, `closed`, `head_changed`,
or `required_checks_missing` (exit 1), `timeout` (exit 124), and
`observation_error` (exit 2). Only required checks reported as `pass` satisfy
CI. Pending, skipped, or neutral checks remain unresolved; a cancelled required
check is actionable. The expected head is checked before and after each status
read, and results from an explicitly different head are ignored. An abbreviated
expected head (at least seven hex digits) is expanded to the PR head it
prefixes on the first read; a non-matching prefix reports `head_changed`.

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
during a requeue, not a new dequeue. A lone `dequeued` label applied before the
newest control comment, when that comment is a queue command, is the same
leftover: the watcher keeps waiting and reports `dequeued` only if the label is
still that old at a poll ten minutes later. A label applied after the queue
command is reported at once.

Cancellation is `cancelled` (exit 130 for SIGINT or 143 for SIGTERM).

After an actionable outcome, fetch detailed checks or failure logs once under
the recovery rules below. A `merged` result tells the caller to perform final
confirmation and authorized cleanup; the watcher does neither. To stop
monitoring, send SIGINT or SIGTERM. The watcher terminates its child read and
reports `cancelled` with the conventional signal exit code.

A non-null `mergedAt` finishes the wait; a `dequeued` label starts recovery
(after the confirmation in "React to a failed batch").
Otherwise, ownership is passive: leave predecessors to their owners and trust
Mergify's serial priority queue. Wait ten minutes, check only the owned PR once,
then repeat the quiet wait if unchanged. Do not query GitHub during an interval.

Mergify gives PRs with either of these labels the same high-priority tier:

- `urgent`: an incident or regression whose delay is actively blocking or
  degrading repository development or a release.
  Renovate security PRs carry it by configuration; keep it unless the advisory
  does not affect Booley, then remove it before queueing.
- `ci`: a change whose primary purpose is to restore or materially improve
  required CI or merge infrastructure.

Apply a priority label before queueing. Reserve `urgent` for active impact, not
ordinary importance or deadlines. High-priority PRs are FIFO and precede
unlabelled work. Priority changes do not interrupt running checks; an expedited
PR follows that work, preserving CI time already spent.

Outside the `required_checks_missing` procedure above, use one-shot
`gh pr checks <number>` only after the owned PR reports a failed required check
or Mergify dequeues it. A predecessor or batchmate failure needs no action
from waiting agents: Mergify advances the queue while that PR's owner handles
recovery.

## React to a failed batch

When a batch fails, Mergify does not dequeue all of its PRs. It splits the
batch, validates the parts, merges the passing parts, returns the parts after a
failing part to the queue, and splits the failing part again until a batch of
one remains. That PR is the culprit and is dequeued with the failed checks as
the cause. If every part passes, Mergify blames the batch's last PR, which is
never retested alone.

- **Still-queued batchmate:** do nothing. No requeue, dequeue, or push:
  Mergify merges passing parts and requeues later ones by itself. Each split
  round adds validation time.
- **`check_failed` in queue mode:** it comes only from the PR's own-head
  required checks, never from a batch draft, so handle it as before.
- **`queue_failed` while the PR still has `queued` or `merge-queue-checking`
  and no `dequeued` label:** read the Mergify check once. If it shows the batch
  being split or the PR requeued, do not restart the watcher yet: a new watcher
  reports `queue_failed` again at once while the Mergify check still fails.
  Wait quietly ten minutes, read the PR once with `gh pr view`, and repeat
  while it stays queued, up to the original deadline (a split round can take
  the full 90 minutes). When the Mergify check stops failing, start one new
  watcher under the restart rules. If the deadline passes with the PR still
  queued and the Mergify check failing, report an incident.
- **`dequeued`:** before recovery, read the Mergify check once and confirm that
  Mergify did not already requeue the PR after a split (an active queue label is
  back). If it did, start one new watcher instead of requeueing. Only a
  confirmed dequeue starts recovery.
- **Dequeued culprit:** follow "Recover a dequeued PR". When Mergify blamed the
  PR as a batch's last PR without a solo check, compare the failing job with the
  PR's own diff to judge flake or deterministic failure before applying the
  known-flake rule. A flake costs batchmates a split round.

Never change a queued branch. To add a commit, dequeue first:

```bash
printf '%s' '@mergifyio dequeue' > /tmp/booley-mergify-dequeue.txt
python3 .github/scripts/confidential_content_guard.py --repo . publish-pr comment \
  --pr <number> --body-file /tmp/booley-mergify-dequeue.txt
```

After Mergify confirms the dequeue, push the change, wait for ordinary PR CI to
pass, then use the queue command above. Agents never reorder entries manually;
the configured labels are the only priority mechanism.

Guard each queue or dequeue command the owner posts:

1. Stop the watcher and confirm that it and its child reads have exited.
2. Read the PR's control comments and queue labels once. Stop if they show a
   command this owner did not post.
3. Record the intended command in the owning task, then post it unchanged
   through the publisher.
4. Record the returned comment ID, then start a new watcher. Its baseline now
   includes that comment.

If the response is lost and the comment ID is unknown, an identical comment
under a shared login cannot be attributed to this owner. Treat it as an
ownership incident: keep observing read-only, post no further command, and ask
the maintainer to decide.

## Tell a stale base from a conflict

A PR that is only behind `main` is ready to queue: `main_protection` does not
require an up-to-date branch, and Mergify validates the PR on top of `main`,
its queue predecessors, and its batchmates. Leave such a branch as it is, and
keep the local verification tied to its tree. Rebase or merge `main` only for a
real conflict or for a change the PR depends on, then verify the new tree again.

Check the live ruleset read-only before relying on this. If the setting is
unreadable or requires an up-to-date branch, update the branch before queueing.
Agents never change the live setting.

## Recover a dequeued PR

Read the Mergify status and underlying Actions failure, then:

- For a deterministic test, lint, confidential-content, or merge-conflict
  failure, fix it and obtain another ordinary green PR run before requeueing.
- For a confirmed infrastructure interruption, requeue unchanged with the
  command above.
- For a known flake, requeue unchanged once. A failure is a known flake when
  the PR's diff does not touch the failing test or the code it exercises, and
  either an open issue describes the same failure or recent `main` runs of that
  job show it alongside green runs. A push to `main` runs only `build-dist` and
  the image job (`bwave-smoke`), so `main` evidence for every other job,
  including `package-artifacts` and the test jobs, comes from the nightly
  scheduled `Tests` run. Match a crashed worker or a duration-budget overrun by
  job and error signature instead of a test ID. When no issue
  exists, first file a `bug`, `ci`, `needs-triage` issue with the run link,
  test ID or job, and error, scanned as `AGENTS.md` requires. When `main`'s
  latest run of that job (for jobs outside the main-push tier, the latest nightly run) fails the
  same way, `main` has regressed: report it instead of requeueing. A second
  failure of the same flake on the same PR is a repeated interruption.
- For an unexplained or repeated interruption, report an incident; do not retry
  until it happens to pass.

Record the cause, with any flake issue link, in the PR. Recovery ends when the fixed or confirmed-transient
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
