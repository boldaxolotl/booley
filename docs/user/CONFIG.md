# Configuration reference

`.booley/booley.toml` holds your Project's Booley settings. This page covers the
Project-wide ones: Project identity, extra Protected Inputs, turning Flows and
Specialists off, Job limits, the Agent CLIs and their billing, models, the
Dashboard and Goal presence, worktree cleanup, and feedback redaction. Settings for one Flow, the Sandbox, or
Stealth Mode live on their own pages; the table below says where.

You rarely edit this file by hand. The `booley-setup` skill writes it during
[setup](SETUP.md), and afterwards you ask the Developer Agent to change a
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
| `[flows.mutation]` | [Mutation testing](FLOW-MUTATION.md#configuration-flowsmutation) |
| `[sandbox]`, host `config.toml`, custom images | [SANDBOX.md](SANDBOX.md) |
| `[eda.<tool>]` | [Commercial EDA provisioning](SANDBOX.md#commercial-eda-provisioning) |
| `[stealth]` | [STEALTH.md](STEALTH.md) |
| `[submodules]`, `.core` files, Cocotb Targets | [TARGETS.md](TARGETS.md) |
| `tests.toml` | [TESTS.md](TESTS.md) |
| `.booley/doctor-waivers.toml` | [Doctor waivers](DOCTOR.md#doctor-waivers-doctor-waiverstoml) |

## booley.toml

The file sits in the Booley Directory, `.booley/` at the top of your repository.
`[project].name` is the Project's name. `booley init` shows it in its output,
and [`/booley-feedback`](#feedback-feedback) replaces it with a placeholder in
the copy it writes for you to share.

`.booley` can be a symlink to a Booley Directory kept elsewhere, for example in
a separate repository. Booley follows the link on the host and mounts its
target into the Sandbox separately. `booley init` remembers where the link
points. If you move the link on purpose, run `booley init --repin-booley-dir`;
otherwise point it back. Until the two agree, `booley doctor` fails with `project.booley-dir-pin`.
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
use for it, for example ASIC synthesis on an FPGA-only Project:

```toml
[flows.synth]
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

Every Flow or Specialist run is a **Job**. Simulation, synthesis, FPGA
implementation, and lint are admitted against a **Memory Budget**: a Job starts
when its estimated memory fits in what the budget has left, and otherwise waits
in a queue instead of failing. Specialists are light Jobs, limited by the AI
provider rather than memory, so a count (`max_light`) caps them. Running Jobs
are never interrupted. The defaults:

```toml
[sandbox]
memory = "32g"           # the Sandbox's memory limit; the Memory Budget comes from it

[jobs]
# memory_budget = "24g"  # a fixed Memory Budget in place of the computed one
max_light = 3            # Specialist Jobs running at once
queue_max = 8            # Jobs that may wait per kind; beyond this a request is refused

[jobs.seats]
# vivado = 1             # licensed seats Booley may use at once; required for Vivado
```

The Memory Budget is the Sandbox's memory limit, `[sandbox].memory`, minus a
reserve: the memory the running agents, the MCP server, and the Dashboard use,
plus 2 GB. Without `[sandbox].memory` the Sandbox has no limit, so Booley starts
from the machine's installed memory, which the Sandbox shares with everything
else; `booley doctor` warns while it is unset. `[jobs].memory_budget` replaces
the computed budget with a fixed size, and Booley subtracts no reserve from it.

Booley reserves the largest peak memory it recorded for a Job
([resource history](#job-resource-history)), plus 25%. With no record of its
own, the Job borrows the largest peak recorded for the same Target and step,
then for that step anywhere in the Project. Only a step Booley has never
measured makes the Job run alone. A simulation campaign runs independent tests
in parallel while their reservations fit.

If the kernel kills a Job for running out of memory, Booley runs it again once,
alone. A second kill fails the Job. See
[Job killed out of memory](TROUBLESHOOTING.md#job-killed-out-of-memory).

Limits apply to one Project.

`[jobs.seats]` caps the licensed seats of a commercial tool, such as `vivado`.
Every licensed tool a Project uses needs one: without it Doctor fails and the
Flow is refused. See [Missing Seat Cap](TROUBLESHOOTING.md#missing-seat-cap).

<details markdown>
<summary>With host-installed licensed tools</summary>

Seat Caps don't share commercial license seats between Projects. See
[Commercial EDA provisioning](SANDBOX.md#commercial-eda-provisioning).

</details>

### Job resource history

Booley measures every Job's peak memory, CPU time, and wall-clock time, and
keeps the newest 20 runs per Target and Job phase, with no overall cap. A group
with no run in the last 90 days is deleted the next time any Job records. Peak
memory counts the Job's temporary files only when `TMPDIR` is on tmpfs; runs
recorded before that rule keep their earlier peak. `booley resources` prints the
run count and the minimum, median, and maximum peak memory and wall-clock time
for each (`--json` for scripts). Use it to choose `[sandbox].memory` and
`[jobs].memory_budget`. Each Test Variant of a simulation has its own entry
(`tests=first+slow`; the default Variant keeps the bare Test name), and the
seeds of one Variant share it. Flow runs that `booley doctor` starts are not
recorded.

## Agent CLIs (`[agent]`)

Booley runs every agent on an **Agent CLI**: Claude Code (`claude`) or Codex
(`codex`). That covers the Developer Agent, the Specialists, and Booley's small
internal steps.

```toml
[agent]
clis = ["claude", "codex"]   # every Agent CLI this Project may use
default = "claude"           # the one used unless you pick another
```

- **`default`** is what `booley chat` opens. It also runs Specialists and
  internal steps unless a [`[models]`](#model-selection-models) entry needs the
  other CLI. Without it, the first entry of `clis` is the default.
- **`clis`**: listing a second CLI lets you open it with
  `booley chat --cli codex`, lets a `[models]` entry run a Specialist on it, and
  gives the Sandbox its login (see
  [Agent CLI logins in the Sandbox](SANDBOX.md#agent-cli-logins-in-the-sandbox)).
  Without `clis`, the Project uses only `default`.

`booley init` asks for the default CLI (Enter picks `claude`) and keeps your
answer on re-runs. `booley init --provider codex` skips the prompt. `provider`
is the older name for `default` and still works.

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

A table may name only CLIs listed in `clis`.

`booley doctor` checks that the credential you picked exists, so Booley never
quietly bills the other one:

- **Default CLI**, with `subscription` or `api_key`: a missing credential fails.
- **Another CLI in `clis`**: a missing credential warns. If a `[models]` entry
  runs a Specialist on that CLI, it fails whatever `auth` says.

`booley init --auth api-key` (with a hyphen) records `api_key`. Why to pick one:
[Sign-in and billing](SETUP.md#sign-in-and-billing).

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
  otherwise in a new worktree `s<N>` on branch `booley/s<N>` (`wt/s<N>` in
  Stealth Mode).
- `always`: always in a new worktree; if the main checkout was free, the launch
  holds it until its session ends, so a later `auto` launch starts in a
  worktree too.
- `never`: always in the main checkout.

`booley chat --new-worktree` and `--here` override it for one launch. Any other
value stops `booley chat` with exit 2 and fails Doctor. Run from inside a
linked worktree, `booley chat` starts there whatever this says. See
[Several agents at once](USAGE.md#several-agents-at-once).

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

The `[models]` keys are `reviewer` and `coverage_analyst`. Any other key, a
model that is neither `claude-*` nor `gpt-*`, or a model whose CLI isn't in
`clis` stops Booley from loading the config, and `booley doctor`
names the entry. A leftover `mutation_tester` key is such a key: mutation testing
is now a [Flow](FLOW-MUTATION.md) and uses no model, so delete it. The Developer Agent itself uses your Agent CLI's own model
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

With `dashboard = false` the [Dashboard](USAGE.md#the-dashboard) does not
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
[Cleaning up idle worktrees](CLI-REFERENCE.md#worktrees).

## Feedback (`[feedback]`)

`/booley-feedback` records bugs, confusion, and opinions in a local report,
`.booley/SETUP-REPORT.md` (`FEEDBACK-REPORT.md` on a Project that never ran
setup). Booley never sends feedback anywhere. When you ask the skill for a copy
to share, it writes `.booley/BOOLEY-FEEDBACK.md` with your Project's details
replaced; read it before you send it. How to report: [When Booley itself
misbehaves](USAGE.md#when-booley-itself-misbehaves).

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
  staging vectors: [Pre-Sim
  Commands](FLOW-SIM.md#pre-sim-commands-flowssimpre_run_commands).
- **A new kind of check** with its own pass criteria: a custom MCP tool, below.

The EDA tools Booley supports are listed in
[SUPPORTED-EDA-TOOLS.md](SUPPORTED-EDA-TOOLS.md).

### Custom MCP tools

A Project can add its own tools for the Developer Agent as Python files in
`.booley/mcp_tools/`. The agent discovers them and calls them like the built-in
ones; ask it to write one. To switch one off, prefix its filename with `_`, for
example `_project_check.py`. How they work: [MCP-TOOLS.md](../internals/MCP-TOOLS.md).

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
