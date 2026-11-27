# Issue tracker: GitHub

How agents read and write this repository's issues and specs, which live as
GitHub issues. Skills that "publish to the issue tracker" or "fetch the
ticket" (`triage`, `to-spec`, `to-tickets`, `wayfinder`, the issue pipeline)
read it for the commands and labels. The brief a `ready-for-agent` issue
needs is in [agent-brief.md](agent-brief.md).

## Commands

Run `gh` inside the clone or a worktree: it takes the repository from the
`origin` remote, and `gh api` fills `{owner}` and `{repo}` the same way.

- **List:** `gh issue list --state open --label <label> --limit 200`. Without
  `--limit`, `gh` returns 30 issues.
- **Create:** `gh issue create --title "$(cat <title-file>)" --body-file <body-file> --label <label>`
- **Comment:** `gh issue comment <n> --body-file <file>`

Draft every title, body, and comment in a local file and scan it with
`python3 .github/scripts/confidential_content_guard.py --repo . pr-text --file <draft>`
before submitting that same file, as `AGENTS.md` requires. That is why the
commands read files instead of taking `--body "..."`. Close with a scanned
closing comment, then `gh issue close <n> --reason completed` (or
`--reason "not planned"`).

## Pull requests

**PRs as a request surface: no.** Triage covers issues only.

Issues and pull requests share one number space. Resolve a bare `#<n>` with
`gh pr view <n>` first and fall back to `gh issue view <n>`.

## Sub-issues and blocking

Split work, `to-tickets` output, and Wayfinder tickets all use GitHub's native
sub-issues. Link a child to its parent and start the child's body with
`Part of #<parent>.`:

```bash
child_id=$(gh api repos/{owner}/{repo}/issues/<child> --jq .id)
gh api --method POST repos/{owner}/{repo}/issues/<parent>/sub_issues -F sub_issue_id="$child_id"
```

The `id` here is the issue's database ID, not its `#` number.

Record each blocking edge twice. The issue pipeline reads only
`Blocked by: #<n>, #<m>` lines in the issue body or its Agent Brief, so write
that line. GitHub's UI and the Wayfinder frontier read native dependencies,
so add those too:

```bash
blocker_id=$(gh api repos/{owner}/{repo}/issues/<blocker> --jq .id)
gh api --method POST repos/{owner}/{repo}/issues/<n>/dependencies/blocked_by -F issue_id="$blocker_id"
```

An issue is unblocked when every blocker is closed.

## Wayfinding operations

Used by `/wayfinder`. The map is one issue labeled `wayfinder:map`, with its
body laid out as the `wayfinder` skill specifies.

- **Child ticket:** a sub-issue of the map (see above), labeled one of
  `wayfinder:research`, `wayfinder:prototype`, `wayfinder:grilling`, or
  `wayfinder:task`. The issue pipeline never admits an issue with a
  `wayfinder:` label.
- **Frontier:** the map's open children with no open blocker and no assignee,
  in map order:

    ```bash
    gh api --paginate repos/{owner}/{repo}/issues/<map>/sub_issues \
      --jq '.[] | select(.state == "open" and (.assignees | length) == 0
              and .issue_dependencies_summary.blocked_by == 0) | .number'
    ```

- **Claim:** `gh issue edit <n> --add-assignee @me`, before any other work.
- **Resolve:** comment the answer, close the issue, then add a one-line linked
  gist to the map's Decisions so far section with `gh issue edit <map> --body-file`.

## Agent claims

The issue pipeline claims an issue with `op claim` and `op release-claim`.
An agent that works an issue unattended by hand follows the same protocol:
post the comment ``Claimed by agent `<agent-id>`.`` (drafted and scanned as
above), then assign the issue to the authenticated login. The earliest claim
not released wins, ordered by `createdAt`, then comment ID. Read every page
of comments and the assignees before assigning and again before each later
state-changing command; these reads detect a conflict but are not a lock.

Release only after every command the run started has stopped and none of its
PRs is queued: post ``Released claim <claim-comment-id> by agent `<agent-id>`:
<reason>.``, then remove the run's own assignment. GitHub authenticates the
login, not the agent ID, so a release counts only when the releasing run can
match it to its own recorded command, or the maintainer authorizes it.
Elapsed time never releases a claim. To recover an abandoned claim, the
maintainer confirms the claiming run has stopped and authorizes the takeover.

## Triage labels

State labels equal the triage skill's role names: `needs-triage`,
`needs-info`, `ready-for-agent`, `ready-for-human`, `wontfix`, plus
`tracker`.

### Tracker and pipeline labels

`tracker` marks a parent whose work was split into sub-issues
([One PR per issue](agent-brief.md#one-pr-per-issue)). Whoever files the
sub-issues puts it on the parent in place of its state label: triage after its
own split, or the issue pipeline after its planner splits a `ready-for-agent`
issue. Sweeps for issues that need attention then skip the parent. A tracker
has no Agent Brief and is never `ready-for-agent`. Close it when its last
sub-issue closes.

The issue pipeline never admits an issue labeled `needs-triage`,
`needs-info`, or any `wayfinder:` label. While a ticket waits on a maintainer
question, labeling the issue `needs-triage`, `ready-for-human`, or `wontfix`
takes it back: the pipeline stops the ticket.

### Category labels

`bug`, `enhancement`, and `documentation`. `documentation` is a third
category beyond the triage skill's two. A `documentation` change edits only
`*.md` files and files under `docs/`, and may rerun an existing generator; the
pipeline refuses a `documentation` branch that changes any other file. Text
generated from code, text a guard test keeps in sync with code, and a
requested guard test all need code changes, so they are `bug` or
`enhancement`.

A code change that also updates docs is `bug` or `enhancement` and may carry
`documentation` as well. When an issue has several category labels, the
pipeline uses the strongest: `bug`, then `enhancement`, then `documentation`.
The category decides which models work the ticket: `documentation` tickets
get Claude only and never a GUI check.

### Flaky-test issues

`flaky` marks an issue about one test that fails only sometimes. Each such
issue covers exactly one test ID (the pytest node ID), and its title names
that ID, so every failure of the test collects in one place. Before filing,
search the open issues, with or without the label, for the test ID. When one
already reports that test failing, add the new failure (run link and error) to
it as a comment instead of opening another issue. A failure that names no test
ID, such as a job that ran out of time, is tracked by its job instead, without
`flaky`; see the known-flake rule in
[Recover a dequeued PR](merge-queue.md#recover-a-dequeued-pr). Issues filed
from the [flake ledger](../../../.github/ci/README.md#flake-ledger) follow the
same rule. A `flaky` issue may own a
[quarantine entry](../../../.github/ci/README.md#quarantine-of-flaky-tests):
closing it fails the `lint` check until the entry is removed, so the fix PR
removes the entry.

An issue or PR whose only fix for a flaky test is a larger time budget or
timeout closes as a duplicate of
[#4384](https://github.com/boldaxolotl/booley/issues/4384), with a closing
comment that links
[Test time budgets](../../../.github/ci/README.md#test-time-budgets). That
shared policy already scales test waits for slow runners, so raising one
test's budget hides the slowdown instead of fixing it. This holds even when a
timeout-headroom message suggests a larger `@pytest.mark.timeout`: make the
test faster instead. A change to the shared policy itself is not a duplicate.
Close such an issue with `gh issue close <n> --duplicate-of 4384`.
