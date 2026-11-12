# Issue tracker: GitHub

How agents read and write this repository's issues and specs, which live as
GitHub issues. Skills that "publish to the issue tracker" or "fetch the
ticket" (`triage`, `to-spec`, `to-tickets`, `wayfinder`, the issue pipeline)
read it for the commands. Label names are in
[triage-labels.md](triage-labels.md); the brief a `ready-for-agent` issue
needs is in [agent-brief.md](agent-brief.md).

## Commands

Run `gh` inside the clone or a worktree: it takes the repository from the
`origin` remote, and `gh api` fills `{owner}` and `{repo}` the same way.

- **Read:** `gh issue view <n> --comments`, or
  `gh issue view <n> --json title,body,labels,assignees,comments` for fields.
- **List:** `gh issue list --state open --label <label> --limit 200`. Without
  `--limit`, `gh` returns 30 issues.
- **Create:** `gh issue create --title "$(cat <title-file>)" --body-file <body-file> --label <label>`
- **Comment:** `gh issue comment <n> --body-file <file>`
- **Labels:** `gh issue edit <n> --add-label <label> --remove-label <label>`
- **Close:** post the closing comment with `gh issue comment`, then
  `gh issue close <n> --reason completed` (or `--reason "not planned"`).

Draft every title, body, and comment in a local file and scan it with
`python3 .github/scripts/confidential_content_guard.py --repo . pr-text --file <draft>`
before submitting that same file, as `AGENTS.md` requires. That is why the
commands read files instead of taking `--body "..."`.

"Publish to the issue tracker" means create a GitHub issue. "Fetch the
relevant ticket" means `gh issue view <n> --comments`.

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

The issue pipeline, and any agent that works an issue unattended, claims it
before starting so concurrent runs on other hosts see each other. Post this
exact comment (drafted and scanned as above), then assign the issue to the
authenticated login:

```text
Claimed by agent `<agent-id>`.
```

The earliest claim that has not been released wins, ordered by `createdAt`,
then comment ID. Read every page of comments and the assignees before
assigning and again before each later state-changing command. Stop when a
claim or assignee appears that the last read did not show. These reads detect
a conflict; they are not a lock, so two hosts can still race.

Release a claim only after every command the claiming run started has
stopped and no PR of that run is still queued. Post a release comment that
names the claim comment ID, the run, and the reason, then remove the run's own
assignment:

```text
Released claim <claim-comment-id> by agent `<agent-id>`: <reason>.
```

GitHub authenticates the login, not the agent ID written in a comment, and
all agents may post under one login. A release therefore counts only when the
releasing run can match it to its own recorded command and the comment ID
GitHub returned, or when the maintainer authorizes the release. Elapsed time
never releases a claim.

The pipeline also skips an issue that has an assignee or an open PR, even
after its claim is released. To recover an abandoned claim, the maintainer
confirms that the claiming run and its processes have stopped, then
authorizes the takeover.
