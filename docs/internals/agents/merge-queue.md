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

## Own the queued PR

The PR's agent remains responsible until Mergify reports it merged or dequeued.
Monitor its checks and comments:

```bash
gh pr checks <number> --watch
gh pr view <number> --comments
```

Never change a queued branch. To add a commit, dequeue first:

```bash
gh pr comment <number> --body '@mergifyio dequeue'
```

After Mergify confirms the dequeue, push the change, wait for ordinary PR CI to
pass, and use the queue command above. The queue is FIFO; agents do not
prioritize or reorder entries.

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
