# Configuration reference

`.booley/booley.toml` holds your Project's Booley settings. This page covers the
Project-wide ones: Project identity, extra Protected Inputs, turning Flows and
Specialists off, Job limits, the Agent CLIs and their billing, models, the
Dashboard and Goal presence, worktree cleanup, and feedback redaction. Settings for one Flow, the Sandbox, or
Stealth Mode live on their own pages; the table below says where.

You rarely edit this file by hand. The `booley-setup` skill writes it during
setup (host doc, not shipped), and afterwards you ask the Developer Agent to change a
setting. `booley doctor` checks the file and names any setting it rejects. Come
here to look up what a setting means and what its default is.

## Where each setting lives

| Setting | Documented in |
|---|---|
| `[project]`, `enabled`, `[jobs]`, `[agent]`, `[models]`, `[goals]`, `[worktree]`, `[sandbox].dashboard`, `[feedback]` | this page |
| `[flows.sim]` | [Simulation](FLOW-SIM.md#configuration-flowssim) |
| `[coverage.waivers]` and per-Target coverage settings | [Coverage](COVERAGE.md#configuration) |
| `[flows.lint]` | [Lint](FLOW-LINT.md#configuration-flowslint) |
| `[flows.synth]` | [ASIC synthesis](FLOW-SYNTH.md#configuration-flowssynth) |
| `[flows.fpga]` | [FPGA implementation](FLOW-FPGA.md#configuration-flowsfpga) |
| `[sandbox]`, host `config.toml`, custom images | [SANDBOX.md](SANDBOX.md) |
| `[eda.<tool>]` | [Commercial EDA provisioning](SANDBOX.md#commercial-eda-provisioning) |
| `[stealth]` | STEALTH.md (host doc, not shipped) |
| `[submodules]`, `.core` files, Cocotb Targets | [TARGETS.md](TARGETS.md) |
| `tests.toml` | [TESTS.md](TESTS.md) |
| `.booley/doctor-waivers.toml` | Doctor waivers (host doc, not shipped) |

## booley.toml

The file sits in the Booley Directory, `.booley/` at the top of your repository.
`[project].name` is the Project's name; Booley shows it in messages and
replaces it in redacted feedback.

`.booley` can be a symlink to a Booley Directory kept elsewhere, for example in
a separate repository. `booley init` remembers where the link points. If you
move the link on purpose, run `booley init --repin-booley-dir`; otherwise point
it back. Until the two agree, `booley doctor` fails with `project.booley-dir-pin`.
A running Sandbox keeps using the location it started with until you run
`booley session refresh`.

### Additional Protected Inputs: `[project].protected_inputs`

In Goal Mode, Booley freezes the files that decide how a Goal is judged, so the
agent can't pass a Goal by changing the yardstick. Booley's own configuration
and `.booley/hooks/` are always frozen. List your own files here when they also
shape the result, such as a Makefile a hook runs, firmware sources, or
reference vectors. Booley can't find them from your scripts, so list each one:

```toml
[project]
name = "example"
protected_inputs = ["Makefile", "firmware/**/*.c", "vectors/"]
```

Leaving the key out or the list empty adds nothing. Paths are relative to the
Project Root of the Goal's worktree and use `/` on every host.

| Form | Example | Matches |
|---|---|---|
| File | `Makefile` | that file |
| Glob | `firmware/**/*.c` | `*` and `?` stay within one folder; `**` alone spans any number of folders, including none; dotfiles match |
| Directory | `vectors/` | every file below it; the trailing `/` is required |

**When you start a Goal**, Booley lists the matched files. It refuses to start,
naming the entry at fault, when an entry:

- matches no file, or matches a file Git doesn't track (untracked or ignored);
- matches into a submodule, a nested repository, `.git`, or Goal Record storage;
- is absolute, contains `..`, a backslash, `[]`, `{}`, or `**` joined to other
  characters (`**.c`), or names a directory without the trailing `/`;
- matches a symlink that doesn't resolve to a tracked file in the repository.

**While the Goal runs**, adding, removing, renaming, or editing a matched file,
or committing such a change, warns at once and blocks Finish. Restoring the file
clears it. To change the list itself, abandon the Goal and start again; an
approved Goal Change can't lift the protection.

Two limits:

- Keep build outputs out of the patterns. A build that writes into a protected
  folder counts as an edit, so prefer `firmware/**/*.c` to `firmware/` when
  compilation writes there.
- An edit made and reverted while a Flow is running goes unnoticed.

## Flow and Specialist availability: `enabled`

Every Flow and Specialist is on by default. Turn one off when the Project has no
use for it, for example lint on a design whose lint setup isn't ready:

```toml
[flows.lint]
enabled = false

[specialists.coverage_analyst]
enabled = false
```

A disabled Flow disappears from what the Developer Agent can run. A disabled
Specialist also disappears from `booley specialist` and from Goal sessions. The
value must be `true` or `false`, and Doctor rejects Specialist names it doesn't
know. Custom Flows and Specialists your Project adds use the same switch.

To choose a Specialist's model, see [`[models]`](#model-selection-models).

## Jobs & concurrency (`[jobs]`)

Every Flow or Specialist run is a **Job**. Simulation, synthesis, and FPGA
implementation are heavy Jobs; Specialists are light Jobs, limited by the AI
provider rather than memory. Each kind has a cap so parallel agents don't run the
Sandbox out of memory. Work beyond the cap waits in a queue instead of failing,
and running Jobs are never interrupted. The defaults:

```toml
[jobs]
max_heavy = 1         # heavy Jobs running at once
heavy_memory = "4g"   # memory set aside per heavy Job
max_light = 3         # Specialist Jobs running at once
queue_max = 8         # Jobs that may wait per kind; beyond this a request is refused
```

Raise `max_heavy` only when the Sandbox has the memory for it. `booley doctor
--deep` measures how much memory synthesis really needs; plain `booley doctor`
then warns when `[sandbox].memory` can't hold `max_heavy` heavy Jobs plus the
agents. With `max_heavy` above 1, a simulation campaign also runs independent
tests in parallel.

Caps apply to one Project. They don't share commercial license seats between
Projects.

### Job resource history

Booley measures every Job's peak memory, CPU time, and wall-clock time, and
keeps the newest 20 runs per Target and Job phase. `booley resources` prints
the run count and the minimum, median, and maximum peak memory and wall-clock
time for each (`--json` for scripts). Use it to choose `heavy_memory` and
`[sandbox].memory`. Each Test Variant of a simulation has its own entry
(`tests=first+slow`; the default Variant keeps the bare Test name), and the
seeds of one Variant share it. Flow runs that `booley doctor` starts are not
recorded.

## Agent CLIs (`[agent]`)

Booley runs its agents on an **Agent CLI**: Claude Code (`claude`) or Codex
(`codex`). `clis` lists the ones your Project may use, and `default` picks the
one the Developer Agent and Booley's own calls run on:

```toml
[agent]
clis = ["claude", "codex"]   # the Agent CLIs this Project may use
default = "claude"           # must be one of them
```

Without `clis`, the Project uses only the default CLI. `clis` also decides
which logins the Sandbox receives, see
[Agent CLI logins in the Sandbox](SANDBOX.md#agent-cli-logins-in-the-sandbox).
Without `default`, the
first entry of `clis` is the default. `booley init` asks for the default CLI
(`claude` if you just press Enter), records it as `default`, and keeps your
answer on re-runs; `booley init --provider codex` sets it without the prompt.
`provider` is the older name for `default` and still works. Each Specialist runs on the Agent CLI its model needs, so
list a second CLI only when a [`[models]`](#model-selection-models) entry asks
for it.

### Pinning what bills (`[agent] auth`)

When both a subscription and an API key are available, `auth` decides which one
pays:

```toml
[agent]
default = "claude"
auth = "subscription"   # "subscription", "api_key", or "auto" (default)
```

- **`auto`**: the Agent CLI decides, and an API key beats a subscription.
- **`subscription`**: Booley removes API keys from the agents' environment so
  the subscription pays.
- **`api_key`**: the API key pays.

A single value applies to every CLI in `clis`. To bill each CLI differently,
use a table with one entry per CLI; a CLI you leave out uses `auto`:

```toml
[agent]
clis = ["claude", "codex"]

[agent.auth]
claude = "subscription"
codex = "api_key"
```

A table may name only CLIs listed in `clis`. Codex also accepts `gateway`,
which sends its calls through a model gateway set up for your Sandbox; Claude
Code does not. A Sandbox that Booley issues for you forwards the gateway
settings from your shell for Codex only; see
[Codex through a model gateway](SANDBOX.md#codex-through-a-model-gateway).

With `subscription` or `api_key`, `booley doctor` fails when the default CLI's
credential is missing instead of quietly billing the other one; for another CLI
in `clis` it only warns, unless a `[models]` entry runs a Specialist on that CLI:
then a missing credential fails under any `auth` value. On the command line,
`booley init --auth api-key` (with a hyphen) records `api_key`. Why to pick one:
Sign-in and billing (host doc, not shipped).

### Git identity (`[agent.git]`)

The name and email that commits made inside the Sandbox carry, whether the
agent, you in the terminal, or the editor's Git panel makes them:

```toml
[agent.git]
name = "Expected Developer"
email = "developer@example.com"
```

Without it, commits use `Dev <dev@localhost>`. It overrides any gitconfig in the
Sandbox and leaves your host checkout alone; `git -c`, `--author`, and
`GIT_AUTHOR_*`/`GIT_COMMITTER_*` still win. After a change, recreate the Sandbox
(`booley session refresh`). Doctor run in the Sandbox warns when Git would use
another identity, and names the fix, including for a hand-written devcontainer
spec such as GitHub Codespaces.

## Interactive sessions (`[chat]`)

```toml
[chat]
worktree = "auto"    # "auto" (default), "always", or "never"
```

Decides where `booley chat` starts when you run it from the main checkout:

- `auto`: in the main checkout when no other Booley session is using it,
  otherwise in a new worktree `s<N>` on branch `booley/s<N>`.
- `always`: always in a new worktree.
- `never`: always in the main checkout.

`booley chat --new-worktree` and `--here` override it for one launch. Any other
value stops `booley chat` with exit 2 and fails Doctor. Run from inside a
linked worktree, `booley chat` starts there whatever this says. See
Several agents at once (host doc, not shipped).

## Model selection (`[models]`)

Each Specialist has its own `[models]` key. The model you name decides which
Agent CLI runs that Specialist: `claude-*` models run on Claude Code and
`gpt-*` models on Codex. That CLI must be listed in [`[agent] clis`](#agent-clis-agent):

```toml
[agent]
clis = ["claude", "codex"]
default = "claude"

[models]
reviewer = "gpt-6-astra"   # runs on Codex
```

The `[models]` keys are `reviewer`, `mutation_tester`, and `coverage_analyst`.
Any other key, a model that is neither `claude-*` nor `gpt-*`, or a model whose
CLI isn't in `clis` stops Booley from loading the config, and `booley doctor`
names the entry. The Developer Agent itself uses your Agent CLI's own model
setting, not this table.

A Specialist without an entry, and Booley's own small internal steps, use the
default CLI's built-in model, so most Projects need no `[models]` at all:

| Used by | Claude Code | Codex |
|---|---|---|
| Specialists | `claude-opus-5-5` | `gpt-6.1-sol` |
| Booley's internal calls | `claude-sonnet-5-5` | `gpt-6-luna` |

Booley passes the model to the Agent CLI as written and never swaps it: if your
account can't run it, the Specialist fails and the error names the `[models]`
entry to change. `booley doctor --deep` checks every model ahead of time.

`[models]` has no model tiers. A config with `heavy`, `standard`, or `light`
keys or a `[models.roles]` table doesn't load; the error shows the `[models]`
entries to use instead.

## Dashboard and quiet Goal presence (`[goals]`)

```toml
[sandbox]
dashboard = true     # open the Dashboard when VS Code attaches to the Sandbox

[goals]
quiet_after = 7200   # seconds, 60 to 604800
```

With `dashboard = false` the Dashboard (host doc, not shipped) does not
open on its own; `booley dashboard` in a container terminal still opens it, and
so does **Booley: Open Dashboard** (it needs a trusted Sandbox window with one
Git folder). If it doesn't open when it should, see [Dashboard
troubleshooting](TROUBLESHOOTING.md#the-dashboard-does-not-open-on-sandbox-attach).

A Goal session that makes no Booley call for `quiet_after` seconds (default two
hours) counts as quiet. The Dashboard may drop it from the Sessions list, and
Doctor warns that the Goal has no recent activity. Quiet never ends or abandons
a Goal.

## Worktree cleanup (`[worktree]`)

```toml
[worktree]
gc_older_than = "7d"   # d, h, m, s components such as "36h" or "1d12h"
```

`booley worktree gc` removes worktrees whose last use is older than this age,
keeping any that `booley worktree rm` would refuse (default `"7d"`). Every
`booley chat` launch runs the same cleanup with this age. The setting is read
from the main checkout's `.booley/booley.toml`; a worktree's own copy is
ignored. `booley worktree gc --older-than AGE` overrides it. See
Cleaning up idle worktrees (host doc, not shipped).

## Feedback (`[feedback]`)

`/booley-feedback` records bugs, confusion, and opinions in a local report,
`.booley/SETUP-REPORT.md` (`FEEDBACK-REPORT.md` on a Project that never ran
setup). Booley never sends feedback anywhere. When you ask the skill for a copy
to share, it writes `.booley/BOOLEY-FEEDBACK.md` with your Project's details
replaced; read it before you send it. How to report: When Booley itself
misbehaves (host doc, not shipped).

The `[feedback]` table tunes only that redaction:

```toml
[feedback]
# redact_extra = ["codename"]   # extra terms to replace
# redact_identifiers = false    # keep module and Target names (default: replace them)
```

**Replaced:** absolute paths (Project Root, home directory, any `/home/<user>`),
Git remote URLs and their `org/repo`, your Git name and email, and the design
names in `booley.toml` and your `.core` files (Project name, Target names, VLNV
parts, toplevels). Design names become stable placeholders such as `<module-3>`,
so the report still reads. `redact_extra` terms are replaced too, as are any
`[stealth]` `banned_words` and `banned_substrings` you set.

**Kept**, because a report without them can't be acted on: EDA tool names and
versions, error text, tracebacks, and timing and area numbers. That reveals
which commercial EDA tools you license, and a design name that appears only
inside a quoted log line slips through. Attached log tails are redacted with the
rest, but a log line is arbitrary text, so read them in the shared file.

Redaction is a best-effort list, not a guarantee. For an open-source design
whose module names are already public, `redact_identifiers = false` gives the
maintainer a more useful report.

## Explicit Flow Targets and Doctor selection

A Flow run that doesn't name a Target uses the only eligible one, and fails
when there are several ([Choosing Targets](FLOW_REFERENCE.md#choosing-targets)).
Doctor checks only the Targets you mark for it. The mark sits in the `.core`
file, beside the Target:

```yaml
targets:
  lint_core:
    flow: lint
    flow_options:
      tool: verilator
      booley: {doctor: [lint]}
```

The allowed names are `sim`, `lint`, `synth`, and `fpga`. Plain `booley doctor`
dry-runs each marked Target and `booley doctor --deep` runs it. For `fpga`,
deep Doctor skips the full implementation and prints the command to run it
yourself. An unmarked Target still runs whenever you or the agent ask for it.
With an enabled `[flows.fpga]` table, Doctor fails until some Target is marked
`fpga`; set `enabled = false` there if the Project has no FPGA build yet.

## Advanced setups

Every Flow builds from a FuseSoC `.core` Target (see [Design
description](TARGETS.md#design-description-core)). Two extension points remain
for what a `.core` can't express:

- **A build step per test**, such as compiling the selected test's firmware or
  staging vectors: [Pre-run
  commands](FLOW-SIM.md#pre-sim-commands-flowssimpre_run_commands).
- **A new kind of check** with its own pass criteria: a custom MCP tool, below.

The EDA tools Booley supports are listed in
[SUPPORTED-EDA-TOOLS.md](SUPPORTED-EDA-TOOLS.md).

### Custom MCP tools

A Project can add its own tools for the Developer Agent as Python files in
`.booley/mcp_tools/`. The agent discovers them and calls them like the built-in
ones; ask it to write one. To switch one off, prefix its filename with `_`, for
example `_project_check.py`. How they work: MCP-TOOLS.md (host doc, not shipped).

### Worktree setup hooks

Keep Project setup scripts, such as a `.booley/hooks/post-setup.sh` that builds a
toolchain or fetches vectors, under `.booley/hooks/`. `booley worktree new` runs
`post-setup` (`.sh`, `.py`, or no suffix) in the new worktree after it fills the
Git submodules, with `BOOLEY_WORKTREE` set to the worktree. The output is shown
and logged, and a failure removes the worktree again. Keep generated build
outputs ignored by Git, and commit deliberate inputs before Goal entry. Hooks
are frozen as Protected Inputs while a Goal runs.

Goal Mode's baseline runs, which measure the Goal's base commit, also run
`post-setup` in their fresh checkout of that commit, with `BOOLEY_WORKTREE` set
to it, so files the hook builds (a firmware image, say) exist at the base too. Make the hook safe to run
twice and read its inputs from the checkout. If it fails, only the Targets that
need its output lose their baseline, with the hook's error as the reason. A
hook that changes a tracked file makes the baseline unavailable.

## Something wrong here?

If a setting on this page doesn't behave as described, or you wish one existed,
type `/booley-feedback` in the Developer Agent chat, [open an
issue](https://github.com/boldaxolotl/Booley/issues/new), or email
[boldaxolotl@proton.me](mailto:boldaxolotl@proton.me).
