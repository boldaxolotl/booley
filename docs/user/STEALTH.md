# Stealth Mode

Keeps your AI-assisted workflow out of the git history (see
[FEATURES.md: Stealth Mode](FEATURES.md#stealth-mode)).
This table belongs only to an initialized Project. Booley's own Source Checkout
is never a Project, does not read this policy, and must not own
`.booley/` or Project Git hooks.

```toml
[stealth]
enabled = false              # open setup; hidden answer sets true
# ignore_native_cores = true # use only stealth-authored cores during Booley resolution
# banned_words = ["claude", "anthropic", "codex", "booley", ...]  # override
#                            # the built-in list; [] disables this tier
# banned_substrings = ["quokka"] # add literal substrings (default: [])
#                            # structural attribution is still rejected
# enforce_convention = true  # enforce type(scope): summary subjects
#                            # (default: off — opt in)
# max_body_lines = 0         # cap the commit body (0 = subject line only);
#                            # unset = unlimited
# allowed_authors = ["*@example.com", "Jane Doe"]  # identity allowlist checked
#                            # at push time; unset or [] = unrestricted
```

Setup asks whether to keep Booley out of your git history. Yes keeps
`.booley/` hidden and writes `[stealth] enabled = true`, enabling the
commit-message scrub and hidden-core projection when Booley authors cores.
No uses open config and native cores with `enabled = false`. Unattended setup
leaves stealth off unless hidden authored cores require it; existing hand-set
values are preserved. A missing `enabled` key still means **on**; write the
key rather than omitting it when you want stealth disabled.

Stealth also makes `.booley/` a self-contained home for authored
FuseSoC cores. Cores under `.booley/cores/` use repository-root-relative
fileset paths; `booley init` generates ignored `.booley-projected-*.core` copies
at the repository root so FuseSoC reads pristine RTL directly, with no source
symlinks or copied RTL. Booley refreshes the projections again before target
resolution. This filesystem behavior requires explicit `enabled = true`.

`ignore_native_cores` is an optional, default-false isolation switch for
repositories whose shipped `.core` files are invalid, obsolete, or deliberately
outside the Booley Target surface. It requires explicit `enabled = true`. When
set, Booley enumerates only `.booley/cores/` and gives FuseSoC a private
generated registry containing only those cores; repository-native `.core` files
are never parsed during Booley resolution. The private copies use absolute
source paths generated for the current workspace, so no source symlinks or RTL
copies are created. Run `booley init` or a Booley Flow rather than raw FuseSoC
when relying on this switch.

When enabled, a commit-msg hook rejects recognized machine-attribution footers,
then sanitizes the built-in banned-word list out of all other commit-message
prose. An already-installed hook no-ops at commit time when the flag is off.

The built-in list is `claude`, `anthropic`, `copilot`, `codex`, `openai`,
`chatgpt`, `gemini`, `booley`, `cursor`, `ticket`, `gpt`, `llm`, and
`co-authored-by`. `banned_words` replaces (not extends) it; `banned_substrings`
adds literal substring terms. Both lists are combined case-insensitively without
duplicates. An absent `banned_words` keeps the built-in vocabulary, while an
explicit empty list never restores defaults.

Matching is case-insensitive. The built-in identity names `booley`, `claude`,
`anthropic`, `codex`, `openai`, `chatgpt`, `copilot`, and `gemini` match as
substrings, including when supplied in `banned_words`. Other words use ASCII
alphanumeric token edges: `_`, `-`, `.`, `/`, and non-ASCII characters separate
tokens, as do CamelCase, acronym-to-titlecase, and letter/digit transitions.
Thus `axi_agent`, `XMLAgent`, and `GPT4` match, while `reagent` and `precursor`
do not. Multiword phrases retain their exact internal spaces or hyphens.
Custom `banned_words` entries use token matching; `banned_substrings =
["quokka"]` also matches `myquokkafile`. Redaction replaces only the matched
original spans, preserving surrounding identifier text.

`banned_substrings` must be a list of strings. Entries that occur inside reserved
redaction placeholders (`redacted`, `<author>`, `<author-email>`, `<repo>`,
`<home>`, `<remote>`, `<org>`, `<email>`, `<redacted>`, or `<module-N>` for
any positive integer N)
are rejected before publication. After upgrading or configuring this tier,
run Project Setup or Doctor to reconcile installed Git-hook bundles.

Project Initialization keeps `.booley/hooks/` for Project-authored
`post-setup` lifecycle hooks. Booley-managed commit and
push policy lives in the deterministic
`.booley/.managed/project-git-hooks.pyz` bundle, reached by the small
`commit-msg` and `pre-push` adapters under `.git/hooks/`.

**Your message is redacted, not truncated.** Subject *and* body are kept, with
banned phrases substituted in place; the hook prints what it rewrote. Recognized
attribution is different: `Co-Authored-By:` and robot-prefixed footer lines are
always rejected, while a plain "Generated with …" footer is rejected only when
it is the final nonblank body line and its entire visible payload matches one
entry in the active banned-word vocabulary. Markdown-linked payloads are
compared by their visible label. The hook leaves the raw message unchanged and
tells you to remove the footer and retry. This avoids both silent deletion and
recognizable redaction debris while preserving ordinary prose such as
"Generated with care by the whole team" or "Generated with Docker for
reproducibility." These lines are not rejected; banned words inside them are
still redacted. With the built-in vocabulary, both examples are stored verbatim:
`generated`, `docker`, and `agent` are ordinary prose, not default banned words.
The audit retains `cursor`, `ticket`, `gpt`, and `llm` in the token-matched tier;
they never gain the identity tier's substring matching. Override `banned_words`
when a Project uses these terms in its ordinary engineering vocabulary.

An empty `banned_words = []`, with `banned_substrings` absent or empty, disables
vocabulary redaction and therefore cannot confirm that an ambiguous plain "Generated with …" line names a protected
identity. The two structurally unambiguous footer forms remain rejected while
Stealth Mode is enabled.

## Enforcing the subject convention (`enforce_convention`)

**Off by default — opt in.** Booley's own commits follow a
`type(scope): summary` subject (`feat`, `fix`, `refactor`, `test`, `review`,
`wip`, `docs`, `chore`), but that convention is *not* forced on your repo unless
you ask for it. A design repo carries human- and upstream-style commits on code
you don't own, and rejecting every one that doesn't match the format is noise,
not hygiene. Turn it on for a team that wants the convention across its own
history:

```toml
[stealth]
enforce_convention = true
```

With it on, a non-conforming subject is rejected (merge commits are exempt).
Independent of the toggle: attribution rejection, the banned-word scrub, and
the `max_body_lines` cap are in force when stealth is enabled.
`BOOLEY_SKIP_COMMIT_VALIDATION=1` lands one commit past the convention check and
the body cap. It cannot bypass attribution rejection or sanitization.

## Capping the body (`max_body_lines`)

Unset by default, meaning unlimited. Set it if you want terse history:
`max_body_lines = 0` allows the subject line only, `= 5` allows five lines of
prose. Blank lines and git's `#` comment lines don't count — only lines that
actually reach history, so the cap doesn't move when you re-space a paragraph.

An over-long message is **rejected, not trimmed**: the commit fails and tells
you the line count, so nothing you wrote is silently thrown away. Merge commits
are capped too (unlike the subject-format check, which exempts them) — a merge
body is exactly where long hand-written narratives collect.

## Author allowlist (`allowed_authors`)

Enforced by the **pre-push** hook, not commit-msg — git hands the commit-msg
hook only the message file, so `git commit --author='someone <else@local>'`
cannot be caught there. Push time is the first point where both identities are
readable, and it also covers identities that arrive via rebase, cherry-pick, or
`--no-verify`.

The hook also inspects changed tracked paths and committed symlink targets. A
push is rejected when either carries a banned term or resolves into the hidden
Booley Directory. The reserved repository spelling `.booley/`
is blocked independently of the Sandbox's absolute project-state path,
and changing the worktree link after committing does not bypass the check.

Both the **author and the committer** of every outgoing commit must match at
least one entry. Each entry is an fnmatch glob, matched case-insensitively
against the bare email, the bare name, and the full `Name <email>` ident:

```toml
allowed_authors = [
    "*@example.com",              # a whole domain
    "jane.doe@personal.example",  # one exact address
    "Jane Doe",                   # a bare name, any address
]
```

Unset or `[]` disables the identity allowlist check. The hook scans all pushed
commit ancestry except history already at the actual destination and a verified
upstream import boundary, when configured. Local remote-tracking refs alone
grant no exemption. Without an upstream boundary, imported history is checked
too, including on a first push to an empty destination.

To import a trusted source's pristine history, record its canonical repository
location and the full commit ID at which local development began:

```toml
[stealth]
upstream_repository = "https://example.com/team/design.git"
upstream_base = "0123456789abcdef0123456789abcdef01234567"
```

Set both keys together in the Project's `booley.toml`. The base must be a
locally available commit reachable from the source's advertised branches or tags;
pull-request refs and other ref namespaces grant no import exemption. Fetch
the source's complete history if necessary before pushing. Verification needs
at least one currently advertised branch or tag containing the base available
locally. If upstream advances beyond your fetched history, fetch it again before
pushing; the hook never downloads objects automatically. This applies to
incremental pushes too, even when the destination already has the import.
Only that base and
its ancestors are exempt. Advancing upstream never exempts later local commits,
even if upstream now contains them. The trusted source and pinned base are
owner-selected policy: do not point them at a copy of your local development
branch to exempt new work.

After importing newer upstream history, deliberately advance the pin to the
upstream commit you imported, not your local merge commit. Until then, newer
upstream commits receive ordinary checks. The new local merge commit and its
changed paths remain checked in either case.

Supported source locations are canonical HTTPS or SSH URLs (including SSH
`host:path` syntax), or an absolute local repository path or `file://` URL.
A local source must have complete independent object storage: the pushing
repository itself, shared object directories, alternates, shallow sources, and
promisor sources are refused. Transport authentication, canonical location,
and Git policy checks apply to source discovery as they do to the destination.
Missing, malformed, or unverifiable import settings block an active push;
remove both keys to return to scanning imported history. No-update and deletion
pushes need no import discovery.

Malformed or unreadable `booley.toml`, missing inspected history, and shallow
boundaries in the selected scan range block the push. Run Project Setup or
Doctor after upgrading to refresh installed Git-hook bundles.
`BOOLEY_SKIP_PUSH_GUARD=1` skips every guard check for one push.

## What survives a fresh clone

Keeping Booley out of the git history has a price, and it is worth stating
plainly: **the setup is local**. `booley init` writes these entries into
`.git/info/exclude` (which is per-clone and itself never committed):

```
/.devcontainer
/.booley
/.claude
/AGENTS.md            # root symlink into .booley/AGENTS.md
/CLAUDE.md            # same file, second name
```

So a colleague who clones the repo gets your RTL, your `.core` files, your SDC
— and no Booley at all. Nothing breaks for them; that is the point. But it
means anything you park under `.booley/` is **your machine's state, not
the project's**, including:

- `booley.toml` and everything it configures;
- `AGENTS.md` / `CLAUDE.md` (the root files are symlinks — after a fresh clone
  they simply don't exist, and a stale one left behind by a copy is a dangling
  link);
- `hooks/post-setup.sh`, `mcp_tools/`, `tests.toml`;
- any requirements file you baked with
  [`pip_requirements`](SANDBOX.md#python-dependencies-sandboxpip_requirements).

Two consequences worth planning for:

**`git add -f` does not rescue a file under `.booley/`.** `booley init`
gives the Booley Directory its **own** git repo when stealth is on, so the host repo
sees an embedded repository and refuses to track paths inside it. Commit that
inner repo (or back it up) if you want the setup to survive a machine, and use
`git add -f` only for files *outside* the Booley Directory.

**A file that a tracked `.core` references must itself be tracked.** This is
most visible for a deliberately-broken lint fixture: it has to be a real file
in a real Target. `booley doctor` warns when it finds one on disk but not in git
("a fresh clone or CI checkout will lack them"), and it is right: put that file
in the **tracked tree**, not in `.booley/`. A `tb/fixtures/` or
`verif/lint_bad.sv` next to the sources reads as ordinary test material and
gives nothing away — which is what stealth mode is actually asking of you.

Simulation has one narrow exception because its fail-path fixture may replace
an already-staged firmware or vector rather than belong to a `.core`: mirror
the replacement beneath
`.booley/selftest/sim/bad-overlay/` (for example,
`bad-overlay/firmware/firmware.hex`). During `doctor --deep`, Booley overlays
that tree on an isolated simulation build variant of the default Target's
first runnable test. Ordinary simulation never applies or reuses it, and no
Doctor-only shell command belongs in `[flows.sim].pre_run_commands`.

Lint's corresponding convention is a Target named `lint_selftest_bad` in its
own tracked `.core`, with `flow_options.booley.doctor_selftest: true`. Keep the
known-bad source beside that dedicated core under ordinary tracked verification
material. Booley omits Doctor self-test Targets from `booley targets`, its MCP
equivalent, and ordinary Flow selection; Doctor can still resolve them for the
bad case. Doctor uses the first lint Doctor Target as the good case and that
conventional Target as the bad case.
