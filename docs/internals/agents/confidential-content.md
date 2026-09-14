# Confidential-content vocabulary

The tracked `.github/confidential-vocabulary.enc` is the single source of truth
for banned terms and allowed identities. It is authenticated AES-256-GCM
ciphertext. Local hooks, PR publication, and CI decrypt this same file. The
decryption key is separate: a local owner-only file under
`~/.config/booley/leak-guard/` (or `$XDG_CONFIG_HOME/booley/leak-guard/`), and
the `BOOLEY_LEAK_GUARD_KEY_B64` Actions secret. Neither the key nor plaintext
belongs in Git, PR text, logs, or artifacts.

## Edit and recover

Edit the private TOML at `.git/booley-leak-guard.toml`, shared by worktrees.
`BOOLEY_LEAK_GUARD_CONFIG` or the repo-local Git setting
`booley.leakGuardConfig` may select another private draft path. Seal the draft:

```sh
python3 .github/scripts/confidential_content_guard.py --repo . seal-config
```

The first seal creates a random 256-bit key outside the repository. Later seals
reuse it and update only the tracked ciphertext; CI secret updates are not
needed for vocabulary edits. Commit and push the encrypted file as part of the
ordinary change. The private TOML is only an edit draft, not a scanner input.

Back up the key file in a password manager or another private store before
deleting this machine's copy. Find its path without displaying the key:

```sh
python3 .github/scripts/confidential_content_guard.py --repo . key-path
```

Deleting and recloning the repository preserves the key in the external local
directory. On another machine, restore the backed-up key file to the printed
path with owner-only permissions. Recreate the private edit draft without
printing its contents:

```sh
python3 .github/scripts/confidential_content_guard.py --repo . restore-draft
```

Custom draft paths must stay outside the worktree or under its shared Git
metadata directory; restoration refuses a worktree plaintext destination.

GitHub cannot return the value of an Actions secret, so the CI key secret is
not a backup.

On initial setup, deploy the local key to GitHub Actions:

```sh
python3 .github/scripts/confidential_content_guard.py --repo . sync-ci-key
```

The command validates decryption before sending the key through `gh` stdin.
It never prints the key or plaintext. Keep the old CI configuration secret only
until the encrypted-file workflow has been merged and validated.
The current workflow requires the same key for the trusted base and proposed
encrypted file; rotating it needs a separate staged migration.
For PRs, CI scans with both the trusted base vocabulary and the proposed
vocabulary, so a newly banned term is enforced in the PR that introduces it.

## PR publication

Use `publish-pr` for agent-authored PR text. It reads draft files, scans them
against the encrypted vocabulary, and passes those same bytes to `gh` through
stdin. A missing key, a matched term, or a failed scan stops the GitHub write.
Inspect drafts, links, and attachments for sensitive facts the vocabulary may miss.

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
