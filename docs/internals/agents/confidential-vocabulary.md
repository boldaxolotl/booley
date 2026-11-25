# Confidential-content vocabulary

For maintainers and agents who change the banned terms or allowed identities,
recover the vocabulary key, or need to know why the `confidential-content`
check blocked or only warned on a finding. Publishing PR text is in
[confidential-content.md](confidential-content.md); queueing a PR is in
[merge-queue.md](merge-queue.md). Guard commands are subcommands of
`python3 .github/scripts/confidential_content_guard.py --repo .`.

The tracked `.github/confidential-vocabulary.enc` is the only source of banned
terms and allowed identities: authenticated AES-256-GCM ciphertext, so Git can
hold it unreadable.
The key is kept apart from it: locally in an owner-only file under
`$XDG_CONFIG_HOME/booley/leak-guard/` (or `BOOLEY_LEAK_GUARD_KEY_DIR`), in CI
in the `BOOLEY_LEAK_GUARD_KEY_B64` Actions secret. Never put the key or the
plaintext vocabulary in Git, PR text, logs, or artifacts.

## Edit the vocabulary

1. Edit the private plaintext draft. Its default path is
   `booley-leak-guard.toml` in the Git common directory
   (`.git/booley-leak-guard.toml` in the main checkout), so every worktree
   shares it. `BOOLEY_LEAK_GUARD_CONFIG`, or else the Git setting
   `booley.leakGuardConfig`, selects another path. Scans never read the draft.
2. Seal it into the tracked file:

    ```sh
    python3 .github/scripts/confidential_content_guard.py --repo . seal-config
    ```

    The first seal creates a random 256-bit key. Later seals reuse that key,
    so the CI secret needs no update for a vocabulary edit.
3. Commit `.github/confidential-vocabulary.enc` as part of the ordinary
   change. The PR's `confidential-content` check scans with both the `main`
   vocabulary and the proposed one, so a newly banned term is enforced in the
   PR that adds it.

CI rejects a proposed vocabulary sealed with a different key from `main`'s,
because it decrypts both with the one CI secret. Rotating the key therefore
needs its own staged migration.

## Back up and restore the key

Back up the key file in a password manager or another private store before
deleting this machine's copy. GitHub never returns an Actions secret's value,
so the CI secret is not a backup. Print the key file's path (not the key):

```sh
python3 .github/scripts/confidential_content_guard.py --repo . key-path
```

The key lives outside the repository, so deleting and recloning the repository
keeps it. On another machine, restore the backed-up key file to the printed
path with owner-only permissions, then recreate the private draft without
printing it:

```sh
python3 .github/scripts/confidential_content_guard.py --repo . restore-draft
```

`restore-draft` refuses a destination inside the worktree, other than the
shared Git directory, and refuses to overwrite an existing draft.

## Deploy the CI key

On initial setup, publish the local key to the `BOOLEY_LEAK_GUARD_KEY_B64`
Actions secret:

```sh
python3 .github/scripts/confidential_content_guard.py --repo . sync-ci-key
```

It first checks that the key decrypts the tracked file, then sends the key to
`gh secret set` through stdin; it never prints the key or the plaintext.

## Pull requests that share a head commit

The `confidential-content` status lives on a PR's head commit, so every
open PR with that commit writes the same status. A PR scan therefore
publishes `failure` when another open PR has the same head commit, or when
the list of open PRs cannot be read, whatever its own scan found. Close one
of the PRs, then re-run the newest `Confidential content` run of the other.
A run whose PR title, body, or branches changed after its event publishes
nothing, whatever its scan found; the newer event's run sets the status, so
re-run only the newest run.

## Provenance rules

The scanner attributes content to where it came from, so a merged outside PR
or a merge of `main` is not blamed on a maintainer. Attribution only relaxes a
finding from blocking to advisory (a `WARNING` naming a location and term id);
it never skips a check silently. Each rule has a named test in
`tests/ci/test_confidential_content_guard.py`.

- **Outside work** is a PR whose head repository differs from its base and
  whose `author_association` is not `OWNER`, `MEMBER`, or `COLLABORATOR`. Its
  commits, identities, title, body, and head ref are advisory, except commits
  by an allowlisted author. An incomplete scan still fails.
- **Merged outside PRs** are attributed from the merge commit
  `Merge pull request #N from <owner>/<branch>` (audit) or Mergify's
  `Merge of #N` (queue candidate), so the commits they brought in are
  advisory. If either message format or Mergify's bot user ID changes,
  everything is enforced.
- **Advisories** post one PR comment per distinct finding set and apply the
  `confidential-review` label. Nothing removes the label: review the advisory
  for the head you queue, then remove it.

Residuals that fail a queued PR with a confusing message:

- An outside PR whose branch name matches a term fails once queued: Mergify's
  update merge into its branch is enforced, and its message names the branch.
- A `Merge of #N` commit is always enforced, so an outside PR whose
  term-matching file also changed on the base side fails in the candidate
  until the contributor merges or rebases `main` themselves.

## What maintainers must keep true

- Outside PRs are merged by Mergify with the merge method, GitHub's default
  merge-commit message, and no Mergify `commit_message_template`. Squash,
  rebase, or a changed template makes the commits unattributable, and the
  audit enforces them.
- Bring `main` into a branch by merging `main`. A non-merge commit that copies
  or restores merged outside content passes the PR's base-tip rule but is an
  enforced introduction in the audit, and fails every later audit; only a
  vocabulary change clears it, because history is not rewritten. Auditing a
  branch that merged `main` as a second parent enforces outside commits for
  the same reason.
- A change to a merged outside file whose path or content matches a term
  removes the match in the same commit.
- Attribution is keyed on the commit author only. The `confidential-review`
  label is the human gate for outside work: review the advisory before
  queueing. An allowlisted-author commit with an unlisted committer inside an
  outside PR (cherry-pick, rebase, web edit) stays enforced, so the fork PR
  scan must be green before the merge.
- Attribution trusts the merge subject text, which Git does not verify. Branch
  protection and the Mergify queue keep a directly pushed merge with that
  subject off `main`.
- The repository owner string is part of the trust assumption: renaming the
  owner makes earlier same-repository merges look like outside ones.
- The outside-PR rule assumes a user-owned repository, where every writer is
  `OWNER` or `COLLABORATOR`. Re-check `author_association` for private members
  before moving the repository to an organization.
