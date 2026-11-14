# Doc change classes

How a pull request's doc changes are classified, so the maintainer knows
whether to sign off before merge or read after. Maintainers read the label;
contributors and agents read this page to change the rules. The rules
themselves are in
[`.github/scripts/doc_change_rules.toml`](../../../.github/scripts/doc_change_rules.toml),
the one home of the exact path patterns. The scope rules for writing docs are
in [DOCS_PRINCIPLES.md](../DOCS_PRINCIPLES.md).

## The classes

| Class | Label | Meaning |
| --- | --- | --- |
| `premerge` | `docs-premerge` | The maintainer signs off on the docs before the PR merges |
| `postreview` | `docs-postreview` | The PR merges without that sign-off; the maintainer reads the docs after merge |
| `none` | no label | No doc change that needs the maintainer |

A PR gets the highest class among its doc changes. A PR with no doc change, or
only `none` changes, carries no label.

The weekly [Docs post-review digest](docs-postreview-digest.md) lists every
merged `docs-postreview` PR.

## What counts as a doc change

A changed file is a doc change when its path (or, for a rename, its old path)
matches `doc_paths` in the rules file: any `*.md` file and anything under
`docs/`. Code, and non-Markdown files outside `docs/`, are not doc changes.
`changelog.d/` fragments are `*.md` files, so they are doc changes and fall
to the default class.

## The kinds

Each doc change is tested against every kind in the rules file. A renamed file
matches a kind when either path matches it, except a kind of class `none`,
which needs both paths to match. A kind matches when the path matches one of
its `paths` and its `check` holds. The file's class is the highest class among the matching kinds, and its `rule` is
the first matching kind with that class. A file that matches no kind gets
`default_class` and the rule `default`.

The kinds, by intent:

- **changelog:** `CHANGELOG.md` and its packaged copy; `none`.
- **glossary, adr:** the glossaries and ADRs.
- **user-page-set, user-page-headings:** adding, deleting, or renaming a user
  page, and changing its level-1 or level-2 headings.
- **principles, agent-instructions, skills:** the rules agents follow.
- **cli-reference-entry, config-reference-key:** a new command, option, or
  config key in the user references.

## The checks

| `check` | Holds when |
| --- | --- |
| `changed` | the file changed in any way |
| `page-set` | the file was added, deleted, or renamed |
| `headings` | the level-1 and level-2 headings differ between the old and new file (`#` headings and underlined headings, ignoring fenced code and YAML front matter) |
| `cli-entries` | the new file has a CLI entry the old one lacks |
| `config-keys` | the new file has a config key the old one lacks |

`cli-entries` and `config-keys` are heuristics on how the two reference pages
are written. They read code only: every line of a fenced block and every
inline code span.

- A CLI entry is a `booley` command path, a flag on a `booley` line, a flag in
  any other snippet, or an inline code span that is one bare word (the
  command table).
- A config key is a `table.key` pair in a fenced `toml` block (commented
  defaults included), an inline `[table]` or `[table].key` span, or an inline
  code span that is one bare word (key lists in prose).

Rewording prose or moving an entry is not new. A newly mentioned value in
code formatting (`` `always` ``) counts as an entry, which errs toward
sign-off. An option or key written without code formatting is not seen.

## Run it locally

```bash
python3 .github/scripts/doc_change_class.py --base origin/main --head HEAD
```

It prints one JSON object: the PR's `class`, and every doc change with its
`status`, `class`, and `rule`. It compares from the merge base, so later
commits on `main` do not count. It exits 2, naming the problem, for a
malformed rules file or a Git failure.

## The CI job

`.github/workflows/doc-change-class.yml` runs on `pull_request_target` when a
PR is opened, reopened, pushed to, or edited. It runs the command with
`--label-pr`, which adds the label for the class and removes the other one,
so the label follows the PR when a push changes its class. It makes no call
for a label already in the right state.

The job runs the workflow, script, and rules from the base branch and reads
the PR's commits only as Git data, so a fork PR is labeled without running
its files. A PR that edits the rules is therefore classified by the base
branch's rules. The job fails only when it cannot fetch the PR commits or when the classifier
or `gh` fails, never for a class.

## Change the rules

- **Move a kind between classes:** edit its `class` line in the rules file.
  Nothing else changes.
- **Add a kind that uses an existing check:** add a `[[kind]]` table with
  `name`, `class`, `check`, and `paths`; give it a comment saying why it has
  that class.
- **Add a check:** add it to `CHECKS` and `_check` in
  `.github/scripts/doc_change_class.py` with a test in
  `tests/ci/test_doc_change_class.py`, then use it in the rules file.

The command rejects unknown keys, classes, and checks, so a typo fails the
job instead of being ignored.
