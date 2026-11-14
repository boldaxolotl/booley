# Docs post-review digest

How the maintainer reads the docs of pull requests that merged without a
pre-merge sign-off. Maintainers follow the steps; contributors read it to
change the workflow. The classes themselves are in
[doc-change-classes.md](doc-change-classes.md).

## What it is

Every Monday at 06:00 UTC,
[`docs-postreview-digest.yml`](../../../.github/workflows/docs-postreview-digest.yml)
runs
[`docs_postreview_digest.py`](../../../.github/scripts/docs_postreview_digest.py).
It opens one issue, labeled `docs-postreview-digest`, listing every PR that
carries `docs-postreview` and merged into `main` since the previous digest.
The issue is the maintainer's reading list. It is a stopgap: the weekly patch release
(#3831) will carry the same list in its release PR and replace this digest.

## The window

A digest covers `(previous cut-off, cut-off]`. The cut-off is the instant the
run started; a PR merged exactly on it belongs to that digest, never to the
next. The previous cut-off is the latest one recorded in any digest issue,
open or closed. The first run has no lower bound.

A week with no such PR opens no issue, so the next digest covers both weeks.
Removing the `docs-postreview` label from a merged PR before the run takes it
out of the digest.

## The issue

The title names the window: `Docs post-review: 09 NOV 2026 to 16 NOV 2026`.
The body has:

- the cut-off, in a hidden marker the next run reads;
- an edit-rate table, one row per kind, from closed digests (below);
- one entry per PR: its link, each doc file with the `rule` that matched it,
  and two boxes, `reviewed` and `needed edits`.

The classifier's current rules apply, run on what the PR's merge commit
brought into `main`. Edit only the boxes; the hidden markers must stay.
Never delete or unlabel a digest: the next run reads the newest labeled one
for its cut-off.

## Maintainer steps

1. Open the week's digest.
2. For each PR, read its doc files and tick `reviewed`.
3. Tick `needed edits` when the docs needed an edit. File a ticket for each
   correction and name the PR in it.
4. When the same mistake repeats, amend
   [DOCS_PRINCIPLES.md](../DOCS_PRINCIPLES.md).
5. Close the digest once every box is settled. Only closed digests feed the
   edit rate, so closing is what counts the week.

## The edit rate

The table counts, per kind, the reviewed PRs in closed digests and how many of
them needed edits. A PR counts toward each distinct `rule` of its files
whose class is not `none`, so a changelog edit counts for nothing. An entry
with `needed edits` ticked but `reviewed` unticked is ignored. Open digests
are not counted. Today every `postreview` file has the rule `default`, so the
table has one row until a kind with class `postreview` is added to the rules.

## Run it by hand

```bash
gh workflow run docs-postreview-digest.yml -f dry_run=true
gh workflow run docs-postreview-digest.yml -f dry_run=false
```

The first prints the digest in the run log and writes nothing. A manual run
defaults to a dry run, so it writes only when asked. Locally, from a
full-history checkout:

```bash
PYTHONPATH=src python3 .github/scripts/docs_postreview_digest.py --dry-run
```

## When it fails

The run reads everything first (earlier digests, the PR list, each PR's
files) and writes only at the end: it creates the label if missing, then the
issue in one call. A failed read, an earlier digest it cannot parse, or a
classifier error opens no issue and fails the run, naming the cause. Fix the
cause and rerun: the window did not move, so nothing is skipped. A digest
whose boxes were edited out of shape is the usual cause; restore the lines
`- [ ] reviewed` and `- [ ] needed edits` under each entry marker.
