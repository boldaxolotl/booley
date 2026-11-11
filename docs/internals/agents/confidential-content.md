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

## Provenance rules

The scanner attributes content to where it came from, so a merged outside pull
request or a merge of `main` is not re-blamed on a maintainer. Attribution only
relaxes a finding from blocking to advisory (a `WARNING` that names a location
and term id, never the term); it never skips a check silently.

- **Merges.** A merge commit is scanned for the paths whose merged result
  differs from every parent (git's combined diff); content inherited from one
  parent is not the merge's. Its author and committer identities are always
  checked, and so is its message (advisory only for an attributing merge, see
  Audit).
- **Base tip.** CI passes `--trusted-base <base sha>` to `pre-push` and
  `verify-candidate` for pull requests to `main` only. A changed entry whose
  path and blob both equal an entry of that tip is already accepted on `main`
  and is skipped. A copy at a new path, an edit, or a path that matches a term
  with changed content is still scanned. Local `pre-push` ignores
  `refs/pull/*` destination tips, so an unmerged fork head is scanned, not
  trusted.
- **Audit.** `audit --repository-owner <login>` (exactly one `--rev`; the login
  must be a bare GitHub login) walks the first-parent history of that revision.
  A two-parent merge titled `Merge pull request #N from <owner>/<branch>`, with
  an owner other than the repository owner, attributes the commits its second
  parent brought in. Those commits are advisory unless their author is
  allowlisted. The attributing merge's own identities and merged result stay
  enforced; only its message is advisory, because GitHub copies the branch name
  and PR title into it. Everything else, including every commit of a
  same-repository PR, stays enforced. Without `--repository-owner`, nothing is
  exempt.
- **Pull requests.** CI classifies the PR from GitHub's trusted event payload,
  never from PR content. It is *outside work* when its head repository ID
  differs from its base repository ID and its `author_association` is not
  `OWNER`, `MEMBER`, or `COLLABORATOR`; a missing or unreadable head repository
  (a deleted fork) or association counts as maintainer work. For outside work,
  commit, identity, and PR title, body, and head ref findings are advisory
  (`WARNING`, not blocking), except commits whose author is allowlisted, which
  stay enforced. An incomplete scan, including an invalid proposed encrypted
  vocabulary, still fails. Advisory findings never displace enforced ones under
  the finding cap. Same-repository PRs and maintainer fork PRs keep full
  enforcement. The required status is published on the head commit, which
  several PRs can share, so an outside PR whose head commit another open
  non-outside PR also has (found in the open pull request list through the
  workflow token) is enforced like maintainer work; a failed lookup enforces too,
  and the scan prints a `NOTE`. A closed PR, or a maintainer PR opened while the
  outside scan runs, is not seen: that maintainer PR's own run sets the status,
  and its commits are enforced when it is queued itself, or fall under the
  residuals below when they ride inside the outside PR. The outside PR is scanned
  with its own title, body, and head ref, not the other PR's.
- **Mergify candidates.** A candidate is a same-repository PR opened by the
  Mergify bot (user ID `37929162`, type `Bot`) from a `mergify/merge-queue/`
  branch. The scanner walks its first-parent chain down to the PR base. For each
  first-parent commit whose whole message is `Merge of #N` it reads PR N through
  the workflow token. When PR N is outside work and its head SHA equals that
  merge's second parent, the commits reachable from the second parent and not
  the first are advisory (logged only), except allowlisted-author commits. A
  failed lookup, a head mismatch, a PR that is not outside work, or any other
  first-parent commit (for example a push onto the candidate branch) leaves its
  commits enforced; a `Merge of #N` commit left enforced prints a `NOTE` naming
  why, and so does a candidate with no such commit. The `Merge of #N`
  commits themselves stay enforced. The candidate's own title, body, and head ref are
  Mergify-generated from queued PRs whose own status already checked them, so
  its metadata findings are advisory. This depends on Mergify's `Merge of #N`
  message and bot user ID; a format change fails closed (everything enforced).
- **Advisory comment and label.** Outside-PR advisories on confidential terms
  (not identity-allowlist findings, which every outside contributor has and
  which are only logged) post one PR comment per distinct finding set, listing
  finding kind, redacted location, and term id only, and apply the
  `confidential-review` label. The set is keyed on kind, path, and term id, not
  on commit IDs or line numbers, so a rebase or amend does not post again.
  Because a reviewed set is not re-posted while later pushes keep the same
  kind, path, and term, review the PR head you queue. A vocabulary change alters
  term ids, so an open outside PR with findings gets a fresh comment and label on
  its next event (accepted noise). If posting or labelling fails, the required
  status fails. Create the `confidential-review` label once; the label is a
  human gate (see [merge-queue.md](merge-queue.md)).
- **Residuals.** Commits from a same-repository branch carried inside an outside
  PR are advisory unless their author is allowlisted; that branch's own PR stays
  enforced. Mergify's in-place update merge into an outside PR's branch is
  authored by an allowlisted identity, so it stays enforced, including its
  message, which names the branch; an outside PR whose branch name matches a
  term fails once queued. A `Merge of #N` commit is always enforced, so an outside
  PR whose term file also changed on the base side fails in the candidate's
  combined diff until the contributor merges or rebases `main` themselves.
  In a candidate that merges an outside PR first, stacked on a maintainer PR
  merged second, the maintainer's commits fall in the outside PR's range and are
  advisory unless allowlisted. A maintainer's edit to an outside PR's title or body is advisory (a
  term match is still commented and labelled); agents publish PR text through
  `publish-pr`, which enforces.

Consequences for maintainers:

- Outside PRs must be merged by Mergify with the merge method, with GitHub's
  default merge-commit message and no Mergify `commit_message_template`.
  Squash, rebase, or a changed template makes the commits unattributable and
  the audit enforces them.
- Bring `main` into a branch by merging `main`. A non-merge commit that copies
  or restores merged outside content passes the PR's base-tip rule but is an
  enforced introduction in the audit and fails every later audit; only a
  vocabulary change clears it, because history is not rewritten.
- A change to a merged outside file whose path or content matches a term must
  remove the match in the same commit.
- Attribution is keyed on the commit author only. The fork PR scan is advisory
  for outside work, so the `confidential-review` label is the human gate in its
  place: a maintainer reviews the advisory before queueing. An
  allowlisted-author commit with an unlisted committer inside an outside PR
  (cherry-pick, rebase, web edit) stays enforced, so the fork PR scan must be
  green before the merge.
- Attribution reads the audited revision's first-parent history, so auditing a
  branch that merged `main` as a second parent enforces outside commits.
- Attribution trusts the merge subject text, which git does not verify. A merge
  with that subject pushed directly to `main`, outside Mergify, is treated as
  an outside PR merge; branch protection and the Mergify queue are the controls
  that keep `main` from taking such pushes.
- The repository owner string is part of the trust assumption: renaming the
  owner makes earlier same-repository merges look like outside ones.
- The outside-PR rule assumes a user-owned repository, where every writer is
  `OWNER` or `COLLABORATOR`. Re-check `author_association` for private members
  before moving the repository to an organization.

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
`request-changes`. After a successful write, `publish-pr` prints
`GitHub reference: <url>` when `gh` reports the URL of the created PR, comment,
or review; record it to identify that write later. The older `pr-text` command
remains available to check a draft without publishing it.
