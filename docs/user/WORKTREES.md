# Worktrees

A **worktree** is a second checkout of your Project where a separate Developer
Agent can work without touching your files, for example one for each task you
run in parallel. `booley chat` creates one (`s<N>`, on branch `booley/s<N>`)
when the main checkout is in use, and offers to remove it when its session
ends; see [Several agents at once](USAGE.md#several-agents-at-once). Read this
page to create a worktree by hand, see what each one holds, or remove one.
Run the commands in a container terminal.

## Creating a worktree

From the main checkout:

```bash
booley worktree new <name>
```

This creates `.booley/worktrees/<name>`, prints its path, and starts it at the
current commit on a new branch `booley/<name>`. A branch that occupies that name
(the name itself, a leading path such as `booley`, or a branch below it), or an
existing destination, is refused and left untouched; the error names the branch
in the way. So is a name Git cannot use in a branch name (such as `x.lock` or
`a..b`). After a Sandbox mount point change, a worktree whose checkout is
missing also stops the command; the error names it. A name of the form
`<branch>--<description>` checks out the existing branch `<branch>` instead and
creates no branch. Git submodules are filled in from the main checkout, without
network access. Then the Project's `post-setup` hook
(`.booley/hooks/post-setup.sh`, `.py`, or no suffix), if it has one, runs in the
new worktree. Its output is shown and saved to
`.booley/worktrees/.logs/<name>.log` in the main Booley Directory. If the
submodule step or the hook fails, Booley removes the worktree and its branch
(`booley/<name>`, or `wt/<name>` in Stealth Mode), plus any
`booley-worktree/<name>` branch, and exits nonzero. A branch that already gained
commits is kept.

If `booley worktree new` fails and leaves a checkout behind, it prints the
manual Git steps with the real Sandbox paths; use those, not host paths.

The worktree gets its own Booley Directory (the `.booley/` folder), a
**Snapshot**: a copy of the Booley Directory's configuration (settings,
`cores/`, hooks, Ticket history, and the [waiver
directory](COVERAGE.md#waiver-directory-coveragewaivers) when its `anchor` is
`project_data_repository`). Build results, reports, logs, and evidence stay in
the main Booley Directory. A path Booley does not classify is not copied; the
command names it unless Git tracks or ignores everything in it.

<details markdown>
<summary>In stealth mode</summary>

With [Stealth Mode](STEALTH.md), your Booley Directory is its own Git
repository, so the worktree gets a **Paired Checkout** instead: a checkout of
its last commit on branch `booley-worktree/<name>`. Uncommitted and untracked
files are left out; one warning names them (the first 10, then a count). Commit
them first if the worktree needs them. Booley refuses a repository with no
commits or an existing branch of that name. Ignored files stay behind. Booley
refuses to create a worktree from inside a Paired Checkout. The worktree's own
branch is `wt/<name>` (`wt/s<N>` for `booley chat`) instead of `booley/<name>`,
so the product name stays out of branch names you may push. A branch named `wt`
(or `wt/<name>/...`) blocks the name, like `booley` does outside Stealth Mode. A
worktree keeps the branch it was created with, and `list`, `rm` and `gc` work
with either prefix.

</details>

## Preparing a worktree made with Git

A worktree made with `git worktree add`, or by another tool such as T3 Code,
has no Booley Directory of its own and uses the main checkout's configuration.
In the new worktree, run:

```bash
booley worktree prepare [PATH]
```

`PATH` is any folder in the worktree (default: the current directory). The
worktree gets the Booley Directory that `booley worktree new` would give it:
nothing when Git tracks it, otherwise a **Snapshot**. Git submodules are not
filled in. Then the Project's `post-setup` hook runs, as it does for
`booley worktree new`. If it fails, Booley removes what the run created and
exits nonzero. Running the command again creates nothing new and runs the hook
again.

<details markdown>
<summary>In stealth mode</summary>

With [Stealth Mode](STEALTH.md), the worktree gets a **Paired Checkout** on
branch `booley-worktree/<folder>`, where `<folder>` is the worktree's folder
name. Uncommitted files are left out, with a warning.

</details>

Outside a Booley Project the command does nothing and exits 0. It refuses the
main checkout and a Booley Directory outside the checkout.

## Listing worktrees

`booley worktree list` shows one row per worktree
under `.booley/worktrees/`, from the main checkout or from any worktree:

| Column | Meaning |
|---|---|
| `NAME` | The worktree name |
| `BRANCH` | Its branch, or `detached` on a detached HEAD |
| `BOOLEY DIR` | `Paired Checkout` or `Snapshot` |
| `UNCOMMITTED` | Changed and untracked entries (an untracked folder counts once), not counting untracked files in the worktree's own `.booley/`. A Paired Checkout reads `N, paired M`, `M` being the same count in its inner checkout |
| `UNMERGED` | Commits not yet in the main checkout's branch. Commits whose change is already there count as merged, including a branch merged by squash. A Paired Checkout reads `N, paired M` |
| `SESSION` | Handles of live sessions in the worktree |
| `LAST USED` | When the worktree was created, a Booley tool call last ran there, `booley chat` was last launched inside it, or the session of a worktree that `booley chat` created ended (a resumed session counts through its tool calls); `never` for a worktree that predates the record |

A worktree whose directory is gone is not listed; a warning names it.

## Removing a worktree

Removing a worktree takes one command, run in the Sandbox from the main
checkout or another worktree (never from inside the one you remove):

```text
booley worktree rm <name> [--force]
```

It moves the worktree's evidence aside, removes the worktree, and keeps its
branch (`booley/<name>`, or `wt/<name>` in Stealth Mode), so commits made in the
worktree stay reachable. A worktree on a detached HEAD, which has no branch, is
reported as `detached HEAD was <commit>`; save that commit on a branch if you
need it.

Without `--force` it refuses, listing every reason, while the worktree has:

- a live Session (an unreadable Session Registry also refuses);
- uncommitted changes in the worktree, including untracked files in a
  Snapshot's `.booley/` that are neither evidence nor a copy of the Main
  Booley Directory's file;
- commits not yet in the main checkout's branch (a branch merged by squash
  counts as merged);
- a populated submodule at a commit the main checkout's submodule lacks;
- a Git lock (`git worktree lock`);
- a damaged checkout: registered but its directory is gone, or no working
  `.git`.

`--force` removes it anyway; the evidence is still kept. Another worktree
registered inside the one you remove (a `.baseline-wt-*` checkout, a Snapshot's
own `worktrees/`) is refused even with `--force`: remove it first with
`booley worktree rm` or `git worktree remove`. So is a worktree whose `.booley/`
is a link or lies outside it.

Evidence is the worktree's `.booley/` content that Git does not keep (including
untracked files inside a directory Git tracks in part):
Common Booley State such as `reviewer-evidence/`, `plans/`, `handoffs/`, the
Findings Log (`findings.jsonl`), `setup-evidence/`, reports, and logs, plus Flow
reports with their Trace Artifacts. It moves to `.booley/worktrees/.evidence/<name>-<time>/`
in the Main Booley Directory, at the same relative paths; each attempt gets its
own new directory and nothing is overwritten. Copy out what you need from there,
and keep that archive separate from the Project's own Findings Log; don't merge
the two. If the move fails, for example across file systems, nothing is removed.

If the command is interrupted, run it again: it finishes the removal, and checks
a worktree that is still intact again, so work added since is never removed
without the refusal. It never prunes other worktrees' registrations. To reuse
the name, first delete each branch `rm` listed under `Branches kept` with
`git -C <absolute-project-root> branch -D <branch>`.

<details markdown>
<summary>In stealth mode</summary>

`booley worktree rm` removes the Paired Checkout first, then the outer
worktree, and also keeps the `booley-worktree/<name>` branch. Uncommitted
changes in the Paired Checkout count toward the refusal too. To reuse the name,
also delete that branch with
`git -C <absolute-booley-directory> branch -D booley-worktree/<name>`.
See [Stealth Mode](STEALTH.md).

</details>

## Cleaning up idle worktrees

`booley worktree gc` removes every worktree that
nobody has used for a while, so abandoned ones do not pile up:

```text
booley worktree gc [--older-than AGE]
```

A worktree counts as idle when its last use (the `LAST USED` column of `booley
worktree list`) is older than `AGE`: days, hours, minutes, and seconds as `7d`,
`36h`, `1d12h`, `90m`, or `30s`. The default is `7d`; set another default with
`gc_older_than` in the main checkout's `.booley/booley.toml`
([configuration](CONFIG.md#worktree-settings-worktree)). `--older-than` wins
over the setting. Run it from the main checkout or another worktree.

`booley chat` runs the same cleanup at the start of every launch, with the same
age, and prints only the worktrees it removed: it stays silent about the ones it
keeps and about a failed run (an invalid `gc_older_than`, for instance). Run
`booley worktree gc` to see why a worktree was kept.

Each idle worktree goes through `booley worktree rm` without `--force`, so
everything `rm` refuses is kept: a live Session and unsaved work (see [Removing
a worktree](#removing-a-worktree)). A removed worktree prints the same lines as
`rm`, evidence and branches included. Each worktree that stays prints `Kept
worktree <name>:` and why, such as that it was used too recently, that the
worktree you are in cannot be removed from inside, that it predates the last-use
record (`LAST USED` reads `never`), or `rm`'s refusal reasons. The command ends
with the totals, and exits 1 if the setting is invalid, the worktrees cannot be
listed, or a removal failed.
