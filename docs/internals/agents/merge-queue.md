# Merge queue

Mergify is the only normal merge path into `main`. The queue updates and
validates one PR at a time so parallel agents do not repeatedly refresh every
waiting branch after each merge.

## Queue a ready PR

Queue only a non-draft PR targeting `main` whose work is final and whose
required checks are green. The authenticated GitHub identity issuing the
command needs write permission.

```bash
gh pr comment <number> --body '@mergifyio queue default'
```

The step is complete when the `Mergify Merge Queue` check or status comment
shows the PR queued. GitHub's merge button, GitHub auto-merge, and
`gh pr merge` are not queue requests.

## Own the queued PR

The PR's agent remains responsible until Mergify reports it merged or
dequeued. Monitor its checks and comments:

```bash
gh pr checks <number> --watch
gh pr view <number> --comments
```

Leave the queued branch unchanged. If the PR needs another commit, remove it
from the queue first:

```bash
gh pr comment <number> --body '@mergifyio dequeue'
```

Wait until Mergify reports it dequeued, then push the change, wait for ordinary
PR CI, and issue `@mergifyio queue default` again. Queue order is FIFO; agents
do not add priority or reorder entries.

## Recover a dequeued PR

Read the Mergify status and the underlying Actions failure before acting.

- A deterministic test, lint, confidential-content, or merge-conflict failure
  requires a fix and another ordinary green PR run before requeueing.
- A confirmed infrastructure interruption may be requeued unchanged with
  `@mergifyio queue default`.
- An unexplained or repeated interruption is an incident: report it instead of
  retrying until it happens to pass.

The recovery is complete when the cause is recorded in the PR and the fixed or
confirmed-transient candidate is queued once.

## Finish after merge

Confirm the merge before cleanup:

```bash
gh pr view <number> --json state,mergedAt,mergeCommit,url
```

Only a PR with a non-null `mergedAt` is finished. Then delete its remote branch,
local branch, and worktree as required by `AGENTS.md`. A closed or dequeued PR
is not a successful merge.

## Maintainer incident controls

Manual merging is an explicit maintainer incident decision, not an agent
fallback. During a Mergify or CI incident, maintainers pause the queue, record
the affected entries, and restore service or disable the separate
`mergify_exclusive` ruleset before using GitHub's protected PR merge path.
`main_protection` remains enabled throughout. Never uninstall Mergify while it
is the only actor permitted to update `main`.

Mergify API keys are account-scoped and are not part of the agent workflow.
Agents use the GitHub comment commands above; a maintainer uses the Mergify
dashboard for pause, resume, queue inspection, and exclusive-mode changes.
