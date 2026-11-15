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
| Goal Mode Session Summaries | Committed on the Goal Branch | Kept local ([GOAL-MODE.md](GOAL-MODE.md)) |
| `.booley/` in a worktree | A Snapshot copy | A Paired Checkout of its last commit ([CLI-REFERENCE.md](CLI-REFERENCE.md#worktrees)) |
| Worktree branch names | `booley/<name>` | `wt/<name>`, so the product name stays out of branches you push |

`.devcontainer/`, `.claude/`, and the root `AGENTS.md` and `CLAUDE.md` links are
excluded either way. With Stealth on, edit cores under `.booley/cores/`, not
the root copies; Booley refreshes those before each Flow. A Paired Checkout
takes only committed files, so commit Booley changes in `.booley/` before you
create a worktree.

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

The commit hook can't see everything: `git revert`, `git cherry-pick`, and
`git commit --no-verify` skip it. So a pre-push hook checks every commit you
push that the destination doesn't already have, and blocks the push when:

- the message, author, or committer contains a banned word;
- a changed file path or symlink target contains a banned word, or points into
  `.booley/`;
- the author or committer isn't on `allowed_authors`, when you set it.

File contents aren't checked. The hook lists each offending commit and what it
found; rewrite those commits and push again.

It also blocks a push that creates or updates a branch, tag or other ref whose
name on the remote contains a word from the same list (`ticket`, for example,
so `ticket/123` is blocked too), and names the ref and the word. Push it under
another name (`git push <remote> <local>:<new-name>`) or rename it; only the
remote name is checked, and deleting such a remote branch or tag is always
allowed. To skip every check for one push,
set `BOOLEY_SKIP_PUSH_GUARD=1`.

The hook also blocks when it can't finish the check: an unreadable
`booley.toml`, missing history, or a shallow clone. Fetch the full history
(`git fetch --unshallow`) and retry.

### Author allowlist (`allowed_authors`)

Lists who may appear as author and committer of the commits you push. This is
checked at push time because the commit hook can't see the author, so it also
catches `git commit --author=...`, rebases, and cherry-picks. Each entry is a
shell-style pattern (`*` matches anything), matched without case against the email, the name,
or `Name <email>`. Unset or `[]` allows anyone.

```toml
[stealth]
allowed_authors = [
    "*@example.com",              # a whole domain
    "jane.doe@personal.example",  # one address
    "Jane Doe",                   # a name, any address
]
```

### Pushing imported history (`upstream_repository`, `upstream_base`)

Commits the destination already has aren't checked. But when you push a design
with its upstream history to a new or empty remote, every upstream commit is
checked, and upstream messages or authors can block the push. To exempt
them, pin the upstream source and the upstream commit you started from:

```toml
[stealth]
upstream_repository = "https://example.com/team/design.git"
upstream_base = "0123456789abcdef0123456789abcdef01234567"
```

- Set both keys or neither. `upstream_repository` is an HTTPS or SSH URL, or an
  absolute path to a local repository. `upstream_base` is a full commit ID.
- The base commit must be in your clone and reachable from a branch or tag the
  upstream publishes. The hook doesn't download anything, so fetch upstream
  first.
- Only that commit and its ancestors are exempt. Your own commits are always
  checked.
- After merging newer upstream history, move `upstream_base` to the upstream
  commit you merged, not your merge commit.

The hook names any problem with these settings and blocks the push. Remove both
keys to go back to checking all history.

## Redacted feedback (`[feedback]`)

`/booley-feedback` keeps its report on your machine. When you ask it for a copy
to share, it writes `.booley/BOOLEY-FEEDBACK.md` with your Project's details
replaced, whether or not Stealth Mode is on. Read the copy before you send it.
The `[feedback]` table tunes the replacement:

```toml
[feedback]
# redact_extra = ["codename"]   # extra terms to replace
# redact_identifiers = false    # keep module and Target names (default: replace them)
```

**Replaced:**

| What | Becomes |
|---|---|
| Project Root, in every checkout of the repository | `<repo>` |
| Your home directory, and any `/home/<user>`, `/Users/<user>`, or `C:\Users\<user>` path | `<home>` |
| Git remote URLs | `<remote>` |
| The remote's `org/repo` | `<org>/<repo>` |
| Your Git name and email | `<author>`, `<author-email>` |
| Any other email address | `<email>` |
| Design names: the Project name, checkout folder names, and the Target names, VLNV parts, and toplevels in your `.core` files | `<module-1>`, `<module-2>`, … |
| `redact_extra` terms, and any `[stealth]` `banned_words` and `banned_substrings` | `<redacted>` |

Each design name keeps the same number throughout, so the report still reads.
`redact_identifiers = false` keeps design names as they are. Names shorter than
four characters, and generic ones such as `top` or `core`, are never replaced.

**Kept**, because a report without them can't be acted on: EDA tool names and
versions, error text, tracebacks, and timing and area numbers. A design name
that appears only inside a quoted log line slips through. Attached log tails
are redacted with the rest, but a log line is arbitrary text, so read them in
the shared file.

<details markdown>
<summary>With host-installed licensed tools</summary>

Kept tool names reveal which commercial EDA tools you license. See
[Commercial EDA provisioning](SANDBOX.md#commercial-eda-provisioning).

</details>

Redaction is a best-effort list, not a guarantee. For an open-source design
whose module names are already public, `redact_identifiers = false` gives the
maintainer a more useful report.

## What survives a fresh clone

The Booley setup is local to your clone. Someone who clones the repo gets your
RTL, `.core` files, and constraints, and no Booley. Everything under `.booley/`
lives only on your machine unless you commit it to that folder's own repository
or back it up:

- `booley.toml`, `tests.toml`, `AGENTS.md`, and the cores in `.booley/cores/`;
- hooks, `mcp_tools/`, and any
  [`pip_requirements`](SANDBOX.md#python-dependencies-sandboxpip_requirements)
  file.

Two consequences:

- **`git add -f` can't add a file under `.booley/`** to the RTL repo. Git sees
  `.booley/` as a separate repository and refuses its paths. Commit them in
  `.booley/` instead.
- **A file a tracked `.core` uses must be tracked too**, for example a
  deliberately broken lint fixture. `booley doctor` warns when it finds one
  that only exists on your disk. Put it next to your sources (say,
  `verif/lint_bad.sv`), not in `.booley/`. See
  [DOCTOR.md: Self-test fixtures](DOCTOR.md#self-test-fixtures).
