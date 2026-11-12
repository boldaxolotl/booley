# Confidential-content vocabulary

This runbook is for agents and maintainers. Use it to publish PR text through
the guard, to change the banned terms or allowed identities, to recover the
vocabulary key, and to understand why the `confidential-content` check blocked
or only warned on a finding. Queueing a PR, including one labeled
`confidential-review`, is in [merge-queue.md](merge-queue.md).

Every guard command is a subcommand of
`python3 .github/scripts/confidential_content_guard.py --repo .`; `--help` on
each lists its options.

## How the vocabulary is stored

The tracked `.github/confidential-vocabulary.enc` is the only source of banned
terms and allowed identities. It is authenticated AES-256-GCM ciphertext, so
the terms can live in Git without being readable there. Local hooks, PR
publication, and CI all decrypt this same file, so they enforce the same list.
The key is kept apart from it:

- Locally: an owner-only file under `$XDG_CONFIG_HOME/booley/leak-guard/`
  (`~/.config/booley/leak-guard/` when `XDG_CONFIG_HOME` is unset), or under
  `BOOLEY_LEAK_GUARD_KEY_DIR` when that is set. The guard refuses a key file
  that group or others can read.
- In CI: the `BOOLEY_LEAK_GUARD_KEY_B64` Actions secret.

Never put the key or the plaintext vocabulary in Git, PR text, logs, or
artifacts. Guard diagnostics name a matched term by a term id, never by the
term itself.

## Publish PR text

Publish every agent-authored PR creation, title or body edit, comment, and
review with `publish-pr`. It reads draft files, scans them against the
vocabulary, and passes those same bytes to `gh` through stdin, so the text
that was scanned is the text that is posted. A missing key, a matched term, or
a failed scan stops the GitHub write. The vocabulary cannot match every
sensitive fact: read drafts, links, and attachments yourself as well.

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

- `create` adds `--draft` for a draft PR.
- `edit` takes `--title-file`, `--body-file`, or both.
- `comment --edit-last` edits your last comment instead of adding one.
- `review --verdict` is `approve`, `comment`, or `request-changes`.

After a successful write, `publish-pr` prints `GitHub reference: <url>` when
`gh` reports the URL of the created PR, comment, or review. Record it: it is
how you later tell your own write from an identical one.

For issue text, which `publish-pr` does not post, scan the draft with
`pr-text --file <draft>` (or `pr-text --stdin`), then submit that same file
with `gh`.

## Edit the vocabulary

1. Edit the private plaintext draft. Its default path is
   `booley-leak-guard.toml` in the Git common directory
   (`.git/booley-leak-guard.toml` in the main checkout), so every worktree
   shares it. `BOOLEY_LEAK_GUARD_CONFIG`, or else the Git setting
   `booley.leakGuardConfig`, selects another path. The draft is only an edit
   copy; scans never read it.
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

## Local hooks

A clone can install Git hooks that call `commit-msg <message-file>` and
`pre-push`. A `pre-push` hook passes Git's remote name and location after the
command: without them, the guard can only exclude the updated ref's previous
tip, because it cannot prove what else the destination already contains.

## Provenance rules

The scanner attributes content to where it came from, so a merged outside PR
or a merge of `main` is not blamed on a maintainer. Attribution only relaxes a
finding from blocking to advisory (a `WARNING` that names a location and term
id); it never skips a check silently.

- **Merges.** A merge commit is scanned for the paths whose merged result
  differs from every parent (Git's combined diff); content inherited from one
  parent is not the merge's own. Its author and committer identities and its
  message are always checked (the message is advisory only for an attributing
  merge, see Audit).
- **Base tip.** CI passes `--trusted-base <base sha>` to `pre-push` and
  `verify-candidate` for PRs to `main` only. A changed entry whose path and
  blob both equal an entry of that tip is already accepted on `main` and is
  skipped. A copy at a new path, an edit, and a path that matches a term with
  changed content are still scanned. Local `pre-push` ignores `refs/pull/*`
  destination tips, so an unmerged fork head is scanned, not trusted.
- **Audit.** `audit --rev <rev> --repository-owner <login>` (exactly one
  `--rev`; the login must be a bare GitHub login) walks the first-parent
  history of that revision. A two-parent merge titled
  `Merge pull request #N from <owner>/<branch>`, with an owner other than the
  repository owner, attributes the commits its second parent brought in. Those
  commits are advisory unless their author is allowlisted. The attributing
  merge's own identities and merged result stay enforced; only its message is
  advisory, because GitHub copies the branch name and PR title into it.
  Everything else, including every commit of a same-repository PR, stays
  enforced. Without `--repository-owner`, nothing is exempt. CI runs this
  audit on every push to `main`.
- **Pull requests.** CI classifies the PR from GitHub's trusted event payload,
  never from PR content. A PR is *outside work* when its head repository ID
  differs from its base repository ID and its `author_association` is not
  `OWNER`, `MEMBER`, or `COLLABORATOR`. A missing or unreadable head
  repository (a deleted fork) or association counts as maintainer work. For
  outside work, findings in commits, identities, and the PR title, body, and
  head ref are advisory, except in commits whose author is allowlisted, which
  stay enforced. An incomplete scan, including an invalid proposed vocabulary,
  still fails. Advisory findings never displace enforced ones under the
  100-finding output cap. Same-repository PRs and maintainer fork PRs keep
  full enforcement.

    The required status is published on the head commit, which several PRs
    can share. An outside PR whose head commit another open, non-outside PR
    also has (found through the workflow token) is therefore enforced like
    maintainer work; a failed lookup also enforces, and the scan prints a
    `NOTE`. The lookup cannot see a closed PR or a maintainer PR opened while
    the scan runs: that PR's own run sets the status, and its commits are
    enforced when it is queued, or fall under the residuals below when they
    ride inside the outside PR. The outside PR is scanned with its own title,
    body, and head ref.

- **Mergify candidates.** A candidate is a same-repository PR opened by the
  Mergify bot (user ID `37929162`, type `Bot`) from a `mergify/merge-queue/`
  branch. The scanner walks its first-parent chain down to the PR base. For
  each first-parent commit whose whole message is `Merge of #N`, it reads PR N
  through the workflow token. When PR N is outside work and its head SHA
  equals that merge's second parent, the commits reachable from the second
  parent and not the first are advisory, except allowlisted-author commits. A
  failed lookup, a head mismatch, a PR that is not outside work, or any other
  first-parent commit (for example, a push onto the candidate branch) leaves
  its commits enforced. Each `Merge of #N` commit left enforced prints a
  `NOTE` with the reason, and so does a candidate with no such commit. The
  `Merge of #N` commits themselves stay enforced. The candidate's title, body,
  and head ref are advisory: Mergify generates them from queued PRs whose own
  status already checked their text. This depends on Mergify's `Merge of #N`
  message and bot user ID; if either changes, everything is enforced.
- **Advisory comment and label.** For outside-PR advisories on confidential
  terms, the scan posts one PR comment per distinct finding set, listing
  finding kind, redacted location, and term id only, and applies the
  `confidential-review` label. Identity-allowlist findings, which every
  outside contributor has, are only logged. The set is keyed on kind, path,
  and term id, not on commit IDs or line numbers, so a rebase or amend does
  not post again. Because a reviewed set is not posted again while later
  pushes keep the same kind, path, and term, review the PR head you queue. A
  vocabulary change alters term ids, so an open outside PR with findings gets
  a fresh comment and label on its next event. If posting or labeling fails,
  the required status fails. Nothing removes the label automatically: a
  maintainer removes it after reviewing the advisory.
- **Residuals.** Commits from a same-repository branch carried inside an
  outside PR are advisory unless their author is allowlisted; that branch's
  own PR stays enforced. Mergify's in-place update merge into an outside PR's
  branch has an allowlisted author, so it stays enforced, including its
  message, which names the branch: an outside PR whose branch name matches a
  term fails once queued. A `Merge of #N` commit is always enforced, so an
  outside PR whose term-matching file also changed on the base side fails in
  the candidate's combined diff until the contributor merges or rebases
  `main` themselves. In a candidate that merges an outside PR first, stacked
  on a maintainer PR merged second, the maintainer's commits fall in the
  outside PR's range and are advisory unless allowlisted. A maintainer's edit
  to an outside PR's title or body is advisory (a term match is still
  commented and labeled); agents publish PR text through `publish-pr`, which
  enforces.

### What maintainers must keep true

- Outside PRs are merged by Mergify with the merge method, GitHub's default
  merge-commit message, and no Mergify `commit_message_template`. Squash,
  rebase, or a changed template makes the commits unattributable, and the
  audit enforces them.
- Bring `main` into a branch by merging `main`. A non-merge commit that copies
  or restores merged outside content passes the PR's base-tip rule but is an
  enforced introduction in the audit, and fails every later audit; only a
  vocabulary change clears it, because history is not rewritten.
- A change to a merged outside file whose path or content matches a term
  removes the match in the same commit.
- Attribution is keyed on the commit author only. Because the fork PR scan is
  advisory for outside work, the `confidential-review` label is the human
  gate in its place: a maintainer reviews the advisory before queueing. An
  allowlisted-author commit with an unlisted committer inside an outside PR
  (cherry-pick, rebase, web edit) stays enforced, so the fork PR scan must be
  green before the merge.
- Attribution reads the audited revision's first-parent history, so auditing
  a branch that merged `main` as a second parent enforces outside commits.
- Attribution trusts the merge subject text, which Git does not verify. A
  merge with that subject pushed directly to `main`, outside Mergify, is
  treated as an outside PR merge; branch protection and the Mergify queue keep
  such pushes off `main`.
- The repository owner string is part of the trust assumption: renaming the
  owner makes earlier same-repository merges look like outside ones.
- The outside-PR rule assumes a user-owned repository, where every writer is
  `OWNER` or `COLLABORATOR`. Re-check `author_association` for private members
  before moving the repository to an organization.
