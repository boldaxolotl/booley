# Confidential-content vocabulary

The private local TOML is the source of truth for banned terms and allowed
identities. It lives at `.git/booley-leak-guard.toml` by default, shared by
worktrees. `BOOLEY_LEAK_GUARD_CONFIG` or the repo-local Git setting
`booley.leakGuardConfig` can select another private path. Keep the TOML outside
tracked files.

After editing the TOML, run this from a Booley checkout with a GitHub account
allowed to update repository Actions secrets:

```sh
python3 .github/scripts/confidential_content_guard.py --repo . sync-ci-secret
```

The command validates the local TOML, sends its base64 encoding to the
`BOOLEY_LEAK_GUARD_CONFIG_B64` Actions secret through `gh` stdin, and reports
only success or a nonsensitive error. GitHub Actions cannot return a secret's
value, so the CI secret is a deployed copy, never an editable source. Local
hooks and PR text preflight read the TOML; CI reads the deployed copy. Repeat
the sync after each vocabulary change, before relying on CI to enforce it.

## PR publication

Use `publish-pr` for agent-authored PR text. It reads draft files, scans them
against the local TOML, and passes those same bytes to `gh` through stdin. A
missing TOML, a matched term, or a failed scan stops the GitHub write. Inspect
drafts, links, and attachments for sensitive facts the vocabulary may miss.

```sh
python3 .github/scripts/confidential_content_guard.py --repo . publish-pr create \
  --base main --head my-branch --title-file /tmp/pr-title.txt \
  --body-file /tmp/pr-body.md
python3 .github/scripts/confidential_content_guard.py --repo . publish-pr edit \
  --pr 123 --body-file /tmp/pr-body.md
python3 .github/scripts/confidential_content_guard.py --repo . publish-pr comment \
  --pr 123 --body-file /tmp/pr-comment.md
python3 .github/scripts/confidential_content_guard.py --repo . publish-pr review \
  --pr 123 --verdict comment --body-file /tmp/pr-review.md
```

`edit` accepts a title file, body file, or both. `comment --edit-last` edits the
author's last comment. Review verdicts are `approve`, `comment`, and
`request-changes`. The older `pr-text` command remains available to check a
draft without publishing it.
