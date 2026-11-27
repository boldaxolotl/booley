# Worktrees

A **worktree** is a second checkout of your Project where a separate Developer
Agent can work without touching your files, for example one for each task you
run in parallel; see [Several agents at once](USAGE.md#several-agents-at-once).
A T3 Code thread started on a new worktree gets one in `t3-worktrees/` in the
Main Booley Directory (`.booley/t3-worktrees/` by default). T3 Code creates
it, names its branch, and deletes it with the last thread that uses it.
Booley prepares it. Read this page to see what Booley gives a worktree,
prepare one made with Git, or remove one. Run the commands in a container
terminal.

## Preparing a worktree

A new worktree has no Booley Directory of its own and uses the main checkout's
configuration until it is prepared. T3 Code in the Sandbox prepares each
worktree it creates before the thread's first turn; if that fails, the thread
does not start. For one made with `git worktree add`, run this in it:

```bash
booley worktree prepare [PATH]
```

`PATH` is any folder in the worktree (default: the current directory). The
worktree gets the Booley Directory (the `.booley/` folder): nothing when Git
tracks it, otherwise a **Snapshot**, a copy of the Booley Directory's
configuration (settings, `cores/`, hooks, and Ticket history).

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
files are left out, and one warning names them. Commit them first if the
worktree needs them.

Each run also deletes the `booley-worktree/*` branches whose worktree is gone
and whose commits are all on the Booley Directory's branch. Doctor warns about
the others (`project-data.worktree-branch-kept`) with the command that merges or
deletes each. A reused folder name whose old branch holds commits of its own
fails, naming the branch: merge or delete it, then run again.

</details>

Outside a Booley Project the command does nothing and exits 0. It refuses the
main checkout.

## Removing a worktree

Deleting the last thread that uses a worktree in T3 Code deletes the worktree.
To remove one made with Git, run `git worktree remove <worktree>` from the main
checkout or another worktree.

Nothing copies anything out first. Deleting a worktree deletes the Flow reports
and build trees kept in it, and uncommitted edits in its Paired Checkout. Its
commits stay on its branch, but commits made on a detached HEAD belong to no
branch: create a branch for them first. Copy out a report you still need before
you delete the worktree.
