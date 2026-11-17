# Stealth Mode

Stealth Mode keeps Booley out of your RTL repository and its history. Booley's
configuration, and any FuseSoC cores it writes for your design, stay in a
`.booley/` folder that Git ignores. Git hooks reject commit messages that carry
AI attribution or words you ban, and block pushes that would still leak them. Use it
on a design repo you share with people who don't use Booley, or whose history
must not show AI tools. Leave it off when you want to share the Booley setup
through Git: with Stealth on, a colleague who clones the repo gets no Booley at
all.

## Turning it on

1. Setup asks whether to keep Booley out of your git history. Answer yes, and
   the agent writes this to `.booley/booley.toml`:

    ```toml
    [stealth]
    enabled = true
    ```

    For a new IP, answer yes to the Stealth question in `/booley-setup new`.
2. `booley init` hides `.booley/` from your repository and makes it a separate
   Git repository. Commit your Booley setup there, not in the RTL repo.
3. Commit and push the RTL repo as usual; the hooks below do their work.

Stealth is off unless `booley.toml` says `enabled = true`; a missing key means
off. To turn it off again, set `enabled = false` or delete the key. Turning
Stealth off stops the hooks at once, but `.booley/` stays hidden and
stays its own repository. Booley doesn't undo that, because `.booley/` now holds
its own Git history, and only you can choose whether to merge, keep, or discard
it. `booley init` warns about the hidden files instead. The one exception is a
new IP: an open `/booley-setup new` run removes a still-empty inner repository and the
`/.booley` exclusion, and refuses (changing nothing) if the inner repository
already has commits.

## What changes

| | Stealth off | Stealth on |
|---|---|---|
| `.booley/` | An ordinary folder you commit with your RTL | Hidden in `.git/info/exclude`, your clone's private ignore list, and kept in its own Git repository |
| Cores Booley writes | At the repository root, tracked | In `.booley/cores/`, with ignored `.booley-projected-*.core` copies at the root for FuseSoC |
| Commit messages | Not checked | Checked, see [Commit messages](#commit-messages) |
| Pushes | Not checked | Checked, see [Push checks](#push-checks) |
| `.booley/` in a worktree | A Snapshot copy | A Paired Checkout of its last commit ([Worktrees](WORKTREES.md#creating-a-worktree)) |
| Worktree branch names | `booley/<name>` | `wt/<name>`, so the product name stays out of branches you push |
| Rules a measurement compares with | The Base's | The rules Base, cut in `.booley`'s own repository ([Metric-Driven Development](METRIC-DRIVEN-DEVELOPMENT.md#read-the-measurement-report)) |
| A fresh clone gets | Your RTL and the Booley setup | Your RTL, `.core` files, and constraints; no Booley |

`.devcontainer/`, `.claude/`, and the root `AGENTS.md` and `CLAUDE.md` links are
excluded either way. With Stealth on, edit cores under `.booley/cores/`, not
the root copies; Booley refreshes those before each Flow. A Paired Checkout
takes only committed files, so commit Booley changes in `.booley/` before you
create a worktree.

With Stealth on, `.booley/` lives only on your machine unless you commit it in
its own repository or back it up. That includes `booley.toml`, `tests.toml`,
`AGENTS.md`, `cores/`, hooks, `mcp_tools/`, and any
[`pip_requirements`](SANDBOX.md#python-dependencies-sandboxpip_requirements)
file. `git add -f` can't add those files to the RTL repo; commit them in
`.booley/`. A file a tracked `.core` uses, such as a deliberately broken lint
fixture, belongs next to your sources (`verif/lint_bad.sv`), not in `.booley/`;
`booley doctor` warns about one that only exists on your disk. See
[DOCTOR.md: Self-test fixtures](DOCTOR.md#self-test-fixtures).

If your repository ships `.core` files that are broken, obsolete, or that you
don't want Booley to see, add `ignore_native_cores = true`. Booley then reads
only the cores in `.booley/cores/` and ignores every other `.core` file. It
works only together with `enabled = true`. FuseSoC run by hand still sees the
native cores, so run Flows through Booley.

## Commit messages

The commit-msg hook rejects a message that has an attribution footer
(`Co-Authored-By:`, a 🤖 line, or `Generated with <banned word>`) or a banned
word. It never rewrites the message: it names each match, keeps your text, and
prints the `git commit` command that reopens it in your editor. The agent
rewords and retries on its own. Finish a rejected merge with
`git commit -m "<message>"`, or pass `-m` to `git merge`. Comment lines and the
`git commit -v` diff aren't checked.

```toml
[stealth]
enabled = true
# banned_words = ["claude", "codex", "acme_codename"]  # replaces the default list
# banned_substrings = ["quokka"]                        # adds terms; default none
# max_body_lines = 5                                    # default: unlimited
```

- **Default `banned_words`:** `claude`, `anthropic`, `copilot`, `codex`,
  `openai`, `chatgpt`, `gemini`, `booley`, `gpt`, `llm`, `co-authored-by`.
  Setting the key replaces the list; `[]` turns the word check off.
- **Words match whole name parts**, ignoring case: `ticket` matches
  `ticket_fifo` but not `ticketing`. AI vendor and product names, and every
  `banned_substrings` entry, match anywhere, so `claude` matches
  `myClaudeHelper`.
- **Ordinary names in your design**, such as `gpt_core`, need `gpt` dropped
  from `banned_words`, or both commits and pushes that touch them are blocked.
- **`max_body_lines`** rejects a message with more body lines than the cap;
  `0` allows the subject only. `BOOLEY_SKIP_COMMIT_VALIDATION=1` skips the cap
  for one commit, never the banned-word checks.

## Push checks

`git revert`, `git cherry-pick`, and `--no-verify` skip the commit hook, so a
pre-push hook checks every commit the destination doesn't have yet. It blocks
the push when:

- a message, author, or committer has a banned word;
- a changed path or symlink target has a banned word or points into `.booley/`;
- an author or committer isn't on `allowed_authors`, when set;
- a pushed ref name has a banned word, such as `ticket/123`. Push it under
  another name (`git push <remote> <local>:<new-name>`); deleting such a ref is
  allowed.

File contents aren't checked. The hook also blocks when it can't finish, for
example in a shallow clone; run `git fetch --unshallow` and retry.
`BOOLEY_SKIP_PUSH_GUARD=1` skips every check for one push.

```toml
[stealth]
allowed_authors = ["*@example.com", "Jane Doe"]  # email, name, or `Name <email>`; * wildcard
# Exempt imported history when pushing to a new remote; set both or neither
upstream_repository = "https://example.com/team/design.git"
upstream_base = "0123456789abcdef0123456789abcdef01234567"
```

- **`allowed_authors`** also catches `--author`, rebases, and cherry-picks.
  Unset or `[]` allows anyone.
- **`upstream_base`** is the full ID of the upstream commit you started from. It
  must be in your clone and reachable from an upstream branch or tag; fetch
  upstream first. It and its ancestors are exempt, your commits never are.
  After merging newer upstream history, move it to the upstream commit you
  merged, not your merge commit.
