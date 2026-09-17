# Merge queue

Mergify is the only normal merge path into `main`. It validates up to three
cumulative candidates concurrently and merges in queue order. Each batch adds
one PR: the candidates represent A, A+B, and A+B+C, rather than three unrelated
changes tested against the same old `main`.

## Enable speculative checks

Before rolling out multiple check slots, verify that both `ci-required` and
`confidential-content` run and report success on a draft-PR compatibility
canary. Mergify uses temporary draft batch PRs for speculative validation;
ordinary PR success alone does not establish compatibility with their metadata
and cumulative commits.

The `main_protection` ruleset must have **Require branches to be up to date
before merging** disabled (`strict_required_status_checks_policy: false`).
With strict checks enabled, Mergify limits validation to one check slot even
when `max_parallel_checks` is three. Retain both required status checks, the
rest of `main_protection`, and the separate `mergify_exclusive` ruleset.
Coordinate this live setting with rollout; a configuration PR cannot change
the GitHub ruleset. Ensure runner capacity supports three concurrent CI runs.

See [Mergify queue modes](https://docs.mergify.com/merge-queue/queue-modes/)
and [parallel checks](https://docs.mergify.com/merge-queue/performance/).

## Queue a ready PR

Queue only a final, non-draft PR targeting `main` after its initial required
GitHub Actions checks pass. Mergify validates the cumulative candidate against
`main` and its queue predecessors before merging. The authenticated GitHub
identity must have write permission.

```bash
printf '%s' '@mergifyio queue default' > /tmp/booley-mergify-queue.txt
python3 .github/scripts/confidential_content_guard.py --repo . publish-pr comment \
  --pr <number> --body-file /tmp/booley-mergify-queue.txt
```

Confirm that the `Mergify Merge Queue` check or status comment says queued.
GitHub's merge button, auto-merge, and `gh pr merge` do not queue a PR.

## Own one queued PR

Exactly one agent owns a queued PR until Mergify reports it merged or dequeued.
Queue and dequeue commands belong to that owner. If another session is already
managing the PR, leave its queue state unchanged and coordinate the handoff.
Treat an unexpected queue or dequeue comment as evidence of another owner;
pause state-changing commands until ownership is clear.

After Mergify accepts the queue command, observe that PR once using the
watcher below or, when observing manually, read its status once:

```bash
gh pr view <number> --json state,mergedAt,labels,statusCheckRollup,comments
```

## Run one read-only watcher

The caller must establish queue ownership before starting a watcher. A local
watcher is an observer, not an ownership lock: exactly one foreground process
may observe the selected PR, and a second agent must coordinate with the owner.
The watcher performs reads only; it never queues, dequeues, reruns, pushes,
merges, comments, or cleans up.

For ordinary PR CI, capture the selected PR head and use a deadline that covers
the documented cold-cache validation time:

```bash
python3 .github/scripts/watch_pr.py \
  --repo OWNER/REPO --pr <number> --mode ci \
  --expected-head <40-character-sha> --timeout-seconds 7200
```

For an owned queued PR, include predecessor wait in the caller-selected
deadline. Two hours is an example, not a completion guarantee; Mergify's
configured validation bound is 90 minutes after the PR reaches its check slot:

```bash
python3 .github/scripts/watch_pr.py \
  --repo OWNER/REPO --pr <number> --mode queue --timeout-seconds 7200
```

The process prints one startup line and one final JSON summary. Resume the
existing process session while it waits. Keep each surrounding agent-tool wait
at 60 seconds or less, report elapsed waiting time from the last known summary,
and do not issue parallel GitHub status queries. The process itself observes CI
every 60 seconds and queue state every ten minutes. An expired deadline is
unresolved observation, not proof of failure; choose a new deadline explicitly
rather than restarting automatically.

CI outcomes are `ci_passed` (exit 0), `check_failed`, `closed`, or
`head_changed` (exit 1), `timeout` (exit 124), and `observation_error` (exit 2).
Only required checks reported as `pass` satisfy CI. Missing, pending, skipped,
or neutral checks remain unresolved; a cancelled required check is actionable.
The expected head is checked before and after each status read, and results
from an explicitly different head are ignored.

Queue outcomes are `merged` (exit 0), `dequeued`, `closed`, `check_failed`,
`queue_failed`, or `competing_control` (exit 1), `timeout` (exit 124), and
`observation_error` (exit 2). `mergedAt` is the merge confirmation; a closed PR
without it is not a merge. The initial comment snapshot baselines historical
queue/dequeue commands, while later exact control commands are competing-owner
evidence. The watcher relies on the configured `queued`,
`merge-queue-checking`, and `dequeued` labels plus Mergify check data, not new
bot status comments. Queue head updates are expected. An old failed attempt
does not stop a newer pending or same-head retry.

Cancellation is `cancelled` (exit 130 for SIGINT or 143 for SIGTERM).

After an actionable outcome, fetch detailed checks or failure logs once using
the recovery instructions below. A `merged` result is evidence for the caller
to perform the existing final confirmation and authorized cleanup; the watcher
does neither. Send SIGINT or SIGTERM when monitoring is no longer wanted; the
watcher terminates its child read and reports `cancelled` with the conventional
signal exit code.

A non-null `mergedAt` finishes the wait; a `dequeued` label starts recovery.
Waiting ownership is otherwise passive. Trust Mergify to enforce the configured
serial priority queue and leave predecessor PRs to their owners. Wait ten
minutes, then check only the owned PR once. An unchanged status starts another
quiet wait at the same cadence. Each waiting interval contains no GitHub status
queries.

Mergify gives PRs with either of these labels the same high-priority tier:

- `urgent`: an incident or regression whose delay is actively blocking or
  degrading repository development or a release.
- `ci`: a change whose primary purpose is to restore or materially improve
  required CI or merge infrastructure.

Apply a priority label before queueing. Use `urgent` only for active impact, not
for ordinary importance or deadlines. High-priority PRs are FIFO relative to
each other and lead unlabelled, ordinary work. Priority changes do not interrupt
checks already running; an expedited PR goes immediately after that work,
preserving the CI time already spent.

Use a one-shot `gh pr checks <number>` to inspect job details only after the
owned PR reports a failed required check or Mergify dequeues it. A predecessor's
failure needs no action from waiting agents: Mergify advances the queue, and
that PR's owner handles recovery.

Never change a queued branch. To add a commit, dequeue first:

```bash
printf '%s' '@mergifyio dequeue' > /tmp/booley-mergify-dequeue.txt
python3 .github/scripts/confidential_content_guard.py --repo . publish-pr comment \
  --pr <number> --body-file /tmp/booley-mergify-dequeue.txt
```

After Mergify confirms the dequeue, push the change, wait for ordinary PR CI to
pass, and use the queue command above. Agents do not manually reorder entries;
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

A non-null `mergedAt` indicates success; closed or dequeued does not. Then
delete the remote branch, local branch, and worktree as required by `AGENTS.md`.

## Maintainer incident controls

Manual merging requires a maintainer incident decision; it is never an agent
fallback. During a Mergify or CI incident, maintainers must pause the queue,
record affected entries, and restore service or disable the separate
`mergify_exclusive` ruleset before using GitHub's protected PR merge path. Keep
`main_protection` enabled. Never uninstall Mergify while it is the only actor
allowed to update `main`.

Account-scoped Mergify API keys are not for agents. Agents use the GitHub
comment commands; maintainers use the Mergify dashboard to pause, resume,
inspect the queue, or change exclusive mode.
