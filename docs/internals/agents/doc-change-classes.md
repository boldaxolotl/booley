# Doc change classes

How a pull request's doc changes are classified, so the maintainer knows
whether to sign off before merge or read after. The rules, with a comment on
every kind, are in
[`.github/scripts/doc_change_rules.toml`](../../../.github/scripts/doc_change_rules.toml);
the checks are in `.github/scripts/doc_change_class.py`. The rules for
writing docs are in [DOCS_PRINCIPLES.md](../DOCS_PRINCIPLES.md).

| Class | Label | Meaning |
| --- | --- | --- |
| `premerge` | `docs-premerge` | The maintainer signs off on the docs before the PR merges |
| `postreview` | `docs-postreview` | The PR merges without that sign-off; the maintainer reads the docs after merge |
| `none` | no label | No doc change that needs the maintainer |

A PR gets the highest class among its doc changes. A PR with no doc change, or
only `none` changes, carries no label.

## Run it locally

```bash
python3 .github/scripts/doc_change_class.py --base origin/main --head HEAD
```

It prints one JSON object: the PR's `class`, and every doc change with its
`status`, `class`, and `rule`. It compares from the merge base. It exits 2,
naming the problem, for a malformed rules file or a Git failure.

## A PR that edits the rules

The CI job runs the workflow, script, and rules from the base branch and reads
the PR's commits only as Git data. A PR that edits the rules is therefore
classified by the base branch's rules, not its own. Run the local command
against the merged rules to see the class they will give later PRs.
