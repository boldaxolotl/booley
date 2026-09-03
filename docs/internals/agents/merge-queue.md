# Merge queue

Mergify is the only normal merge path into `main`. It updates and validates one
PR at a time, avoiding refreshes of every waiting branch after each merge.

## Queue a ready PR

Queue only a final, non-draft PR targeting `main` after its initial required
GitHub Actions checks pass. Mergify updates it against the latest `main` and
reruns those checks in the queue before merging. The authenticated GitHub
identity must have write permission.

```bash
gh pr comment <number> --body '@mergifyio queue default'
```

Confirm that the `Mergify Merge Queue` check or status comment says queued.
GitHub's merge button, auto-merge, and `gh pr merge` do not queue a PR.

## Own one queued PR

Exactly one agent owns a queued PR until Mergify reports it merged or dequeued.
Queue and dequeue commands belong to that owner. If another session is already
managing the PR, leave its queue state unchanged and coordinate the handoff.
Treat an unexpected queue or dequeue comment as evidence of another owner;
pause state-changing commands until ownership is clear.

After Mergify accepts the queue command, read that PR's status once:

```bash
gh pr view <number> --json state,mergedAt,labels,statusCheckRollup,comments
```

A non-null `mergedAt` finishes the wait; a `dequeued` label starts recovery.
Waiting ownership is otherwise passive. Trust Mergify to enforce the configured
serial priority queue and leave predecessor PRs to their owners. Sleep until
Mergify's reported merge estimate; when no future estimate is available, wait
ten minutes. Then check only the owned PR once. An unchanged status starts
another quiet wait at the same cadence. Each waiting interval contains no
GitHub status queries.

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
gh pr comment <number> --body '@mergifyio dequeue'
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
