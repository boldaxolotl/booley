# Worktrees

A **worktree** is a second checkout of your Project where a separate Developer
Agent can work without touching your files, for example one for each task you
run in parallel; see [Several agents at once](USAGE.md#several-agents-at-once).
T3 Code creates a worktree for each thread that asks for one, names its branch,
and deletes it with the thread. Booley prepares it. Read this page to see what
Booley gives a worktree, prepare one made with Git, or remove one.
Run the commands in a container terminal.

## Preparing a worktree

A worktree made by T3 Code, or with `git worktree add`, has no Booley Directory
of its own and uses the main checkout's configuration. In the new worktree, run:

```bash
booley worktree prepare [PATH]
```

`PATH` is any folder in the worktree (default: the current directory). The
worktree gets the Booley Directory (the `.booley/` folder): nothing when Git
tracks it, otherwise a **Snapshot**, a copy of the Booley Directory's
configuration (settings, `cores/`, hooks, Ticket history, and the [waiver
directory](COVERAGE.md#waiver-directory-coveragewaivers) when its `anchor` is
`project_data_repository`). Build results, reports, logs, and evidence stay in
the main Booley Directory. A path Booley does not classify is not copied; the
command names it unless Git tracks or ignores everything in it.

Then the Project's `post-setup` hook (`.booley/hooks/post-setup.sh`, `.py`, or
no suffix), if it has one, runs in the worktree. Its output is shown and saved
to `.booley/worktrees/.logs/<folder>.log` in the main Booley Directory, where
`<folder>` is the worktree's folder name. If the hook fails, Booley removes what
the run created and exits nonzero. Running the command again creates nothing new
and runs the hook again. Git submodules are not filled in.

<details markdown>
<summary>In stealth mode</summary>

With [Stealth Mode](STEALTH.md), your Booley Directory is its own Git
repository, so the worktree gets a **Paired Checkout** instead: a checkout of
its last commit on branch `booley-worktree/<folder>`. Uncommitted and untracked
files are left out; one warning names them (the first 10, then a count). Commit
them first if the worktree needs them. Booley refuses a repository with no
commits or an existing branch of that name. Ignored files stay behind. Booley
refuses to prepare a worktree from inside a Paired Checkout.

</details>

Outside a Booley Project the command does nothing and exits 0. It refuses the
main checkout and a Booley Directory outside the checkout.

## Removing a worktree

Deleting a thread in T3 Code deletes its worktree. To remove one made with Git,
run `git worktree remove <worktree>` from the main checkout or another
worktree. With a Paired Checkout, remove that first:
`git -C <booley-directory> worktree remove <worktree>/.booley`.

Nothing copies anything out first. Deleting a worktree deletes the Flow reports
and build trees kept in it, and uncommitted edits in its Paired Checkout. Its
commits stay on its branch. Copy out a report you still need before you delete
the worktree.
