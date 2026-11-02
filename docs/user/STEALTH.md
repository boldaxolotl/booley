# Stealth Mode

Stealth Mode keeps Booley out of your RTL repository and its history. Booley's
configuration, and any FuseSoC cores it writes for your design, stay in a
`.booley/` folder that Git ignores. Git hooks clean AI attribution and words you
ban out of commit messages and block pushes that would still leak them. Use it
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

    For a new IP, `booley init --scaffold <ip_name> --stealth` does the same.
2. `booley init` hides `.booley/` from your repository and makes it a separate
   Git repository. Commit your Booley setup there, not in the RTL repo.
3. Commit and push the RTL repo as usual; the hooks below do their work.

To turn it off, set `enabled = false`. Write the key either way: when it is
missing, the hooks treat Stealth as on, but Booley doesn't set up hidden cores.
Turning Stealth off stops the hooks at once, but `.booley/` stays hidden and
stays its own repository. Booley doesn't undo that; `booley init` warns about
the hidden files instead.

## What changes

- **Booley's files stay untracked.** `.booley/` goes into `.git/info/exclude`,
  your clone's private ignore list. The `.devcontainer/` and `.claude/` folders
  and the root `AGENTS.md` and `CLAUDE.md` links are excluded with or without
  Stealth.
- **Cores Booley writes live in `.booley/cores/`.** Your own `.core` files stay
  untouched. Booley places ignored copies named `.booley-projected-*.core` at
  the repository root so FuseSoC can find them, and refreshes them before each
  Flow. Edit the file under `.booley/cores/`, not the copy.
- **Commit messages are cleaned**, see [Commit messages](#commit-messages).
- **Pushes are checked**, see [Push checks](#push-checks).
- **Goal Mode summaries stay local** instead of being committed on the Goal
  Branch ([GOAL-MODE.md](GOAL-MODE.md)).

If your repository ships `.core` files that are broken, obsolete, or that you
don't want Booley to see, add `ignore_native_cores = true`. Booley then reads
only the cores in `.booley/cores/` and ignores every other `.core` file. It
works only together with `enabled = true`. FuseSoC run by hand still sees the
native cores, so run Flows through Booley.

## Commit messages

When you or the agent commit, the commit-msg hook:

- **Rejects attribution footers**: `Co-Authored-By:` lines, lines starting with
  🤖, and a last line `Generated with <name>` where the name is a banned word,
  such as "Generated with Codex". The message is left as written; remove the
  line and commit again. The agent does this on its own.
- **Replaces banned words with `redacted`** in the subject and body, and keeps
  everything else. It prints what it rewrote:

    ```text
    commit-msg: stealth-mode redaction rewrote the subject -> fix(redacted): drop stale flag
    ```

The default banned words are `claude`, `anthropic`, `copilot`, `codex`,
`openai`, `chatgpt`, `gemini`, `booley`, `cursor`, `ticket`, `gpt`, `llm`, and
`co-authored-by`.

```toml
[stealth]
enabled = true
# banned_words = ["claude", "codex", "acme_codename"]  # replaces the default list
# banned_substrings = ["quokka"]                        # adds terms; default none
```

- **`banned_words` replaces the default list**, it doesn't add to it. Copy the
  defaults you want to keep. `banned_words = []` turns word cleaning off;
  attribution lines are still rejected.
- **Words match whole identifier parts**, ignoring case. `_`, `-`, `.`, `/`, and
  case or letter-digit changes split a name into parts, so `ticket` matches
  `ticket_fifo` but not `Tickets` or `ticketing`, and `gpt` matches `GPT4`.
- **AI vendor and product names match anywhere**: `claude`, `anthropic`,
  `codex`, `openai`, `chatgpt`, `copilot`, `gemini`, and `booley` match inside
  `myClaudeHelper` too.
- **`banned_substrings` match anywhere**: `quokka` also matches `myquokkafile`.
  An entry that is part of a Booley placeholder, such as `redacted` or `<email>`,
  is an error: commits and pushes fail until you remove it.

If your design uses `cursor`, `ticket`, `gpt`, or `llm` as ordinary names, set
`banned_words` without them. Otherwise a commit about `ticket_fifo` reads
`redacted_fifo`, and a push that adds `rtl/ticket_fifo.sv` is blocked.

### Enforcing the subject convention (`enforce_convention`)

Off by default. Set `enforce_convention = true` to reject commits whose subject
isn't `type(scope): summary`, with a type of `feat`, `fix`, `refactor`, `test`,
`review`, `wip`, `docs`, or `chore`, and a summary of at most 72 characters.
Merge commits are exempt. It is off by default because a design repo usually
carries upstream and human commits in other styles.

### Capping the body (`max_body_lines`)

Unlimited by default. `max_body_lines = 0` allows the subject line only;
`max_body_lines = 5` allows five body lines. Blank lines and `#` comment lines
don't count. A message over the cap is rejected, not trimmed, so nothing you
wrote is lost. Merge commits are capped too.

```toml
[stealth]
enforce_convention = true
max_body_lines = 5
```

To skip both checks for one commit, set `BOOLEY_SKIP_COMMIT_VALIDATION=1`.
Attribution rejection and word cleaning still apply.

## Push checks

The commit hook can't see everything: `git revert`, `git cherry-pick`, and
`git commit --no-verify` skip it. So a pre-push hook checks every commit you
push that the destination doesn't already have, and blocks the push when:

- the message, author, or committer contains a banned word;
- a changed file path or symlink target contains a banned word, or points into
  `.booley/`;
- the author or committer isn't on `allowed_authors`, when you set it.

File contents aren't checked. The hook lists each offending commit and what it
found; rewrite those commits and push again. To skip every check for one push,
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
