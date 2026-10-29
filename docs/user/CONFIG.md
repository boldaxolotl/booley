# Configuration reference

Reference for `.booley/booley.toml`: the "what each knob means" companion to
[SETUP.md](SETUP.md), which is the ordered list of setup **steps**. This page
covers Project-wide settings. Per-Flow, Sandbox, and Stealth settings have
their own pages; [Where each setting lives](#where-each-setting-lives) lists
them. You do not fill these files by hand: the `booley-setup` skill authors them
interactively, and `booley doctor` validates them. When a step needs the
meaning of a field, it links here.

**Audience:** humans and Developer Agents. A human can read this to understand the
config surface; a Claude Code or Codex session running the setup skill dereferences these anchors as it
fills each file.

**Read this first.** This is a reference, not a tutorial. It assumes you have
skimmed the [README](../../README.md) overview and know Booley's controlled
vocabulary: terms used here without definition — **Target**, **Booley Flow**,
**EDA Provisioning**, **Sandbox**, **Specialist**, **VLNV** —
are indexed by the [context map](../../GLOSSARY-MAP.md) if one is unfamiliar. Every
project has exactly two mandatory pieces: `.booley/booley.toml` (this page)
and at least one FuseSoC `.core` file describing your design. If you have not
described your design yet, start with [TARGETS.md](TARGETS.md) — every Booley
Flow builds from it — and come back to the per-Flow knobs after.

**Found a knob that lies, or one you wish existed?** Both are worth a minute:
tell the `/booley-feedback` skill in your Developer Agent chat. See
[Feedback](#feedback-feedback).

## Where each setting lives

| Setting | Documented in |
|---|---|
| `[project]`, `enabled`, explicit Flow Targets | [booley.toml](#booleytoml), below |
| `[flows.sim]` | [Simulation](FLOW-SIM.md#configuration-flowssim) |
| `[flows.sim]` coverage and coverage waivers | [Coverage](COVERAGE.md#configuration) |
| `[flows.lint]` | [Lint](FLOW-LINT.md#configuration-flowslint) |
| `[flows.synth]` | [ASIC synthesis](FLOW-SYNTH.md#configuration-flowssynth) |
| `[flows.fpga]` | [FPGA implementation](FLOW-FPGA.md#configuration-flowsfpga) |
| `[jobs]` | [Jobs & concurrency](#jobs--concurrency-jobs), below |
| `[sandbox]`, host `config.toml`, `BOOLEY_EGRESS`, custom images | [SANDBOX.md](SANDBOX.md) |
| `[eda.<tool>]` | [Commercial EDA provisioning](SANDBOX.md#commercial-eda-provisioning) |
| `[stealth]` | [STEALTH.md](STEALTH.md) |
| `[feedback]`, `[agent]`, `[models]` | below |
| `[goals]` | [Dashboard and quiet Goal presence](#dashboard-and-quiet-goal-presence-goals), below |
| `.core` files, Cocotb Targets | [TARGETS.md](TARGETS.md) |
| `tests.toml` | [TESTS.md](TESTS.md) |
| `.booley/doctor-waivers.toml` | [Doctor waivers](DOCTOR.md#doctor-waivers-doctor-waiverstoml) |

Doctor and `booley init` report retired or deprecated settings with the exact fix.

## booley.toml

Project identity, Flow selection, and Developer Agent and Specialist configuration live in
`.booley/booley.toml`. A per-knob reference follows, section by section.

The `.booley` entry may itself be a symlink to a Booley Directory elsewhere, such as a
separate clone of your Booley Directory. The directory it points to must contain
`booley.toml`. Booley uses that directory on the host, and in the Sandbox both
`/booley-project` and `/work/.booley` reach it. Cores you keep there still load only from
its `cores/` folder.

`booley init` pins that link: it records the directory the link resolves to, plus its
device and inode, in host-private state. Sandbox issuance and host commands refuse a linked
entry that has no pin, now resolves elsewhere, or points at a directory that was replaced in
place; Doctor reports one `project.booley-dir-pin` FAIL and stops. A Project that already
uses a linked entry needs one `booley init` after upgrading to create the pin. After you move
the link on purpose, run `booley init --repin-booley-dir`, or answer `y` at the interactive
re-pin prompt; a non-interactive run without the flag leaves the pin unchanged and fails.
The same flag replaces an unreadable pin. If you did not move it, point `.booley` back at
the pinned target. A Sandbox that is already running keeps its previous mount until you
restart it with `booley session refresh`. One gap remains: starting an existing container
directly with `docker start` bypasses the container-start validation hook, so the pin is not
checked on that path.

`[project]` carries the project `name`, `protected_inputs`, and `preflight_checks` — see
[Per-Target environment](TESTS.md#per-target-environment-env) for the last one, which is
about testbench file-existence checks rather than identity. The sections below
cover the shared settings, starting with `enabled` for Booley Flows and
Specialists; per-Flow tables are on the pages listed in
[Where each setting lives](#where-each-setting-lives).

### Additional Protected Inputs: `[project].protected_inputs`

Goal Mode already freezes Booley's own configuration and hooks. `protected_inputs`
adds tracked files of yours that decide how a Goal is evaluated, such as a
Makefile a hook runs, firmware sources, or reference vectors:

```toml
[project]
name = "example"
protected_inputs = ["Makefile", "firmware/**/*.c", "vectors/"]
```

Omitting the key or leaving the list empty protects nothing extra. Booley does
not infer these files from scripts or Makefiles; list each one.

Each entry is a path relative to the Project Root of the Goal's worktree, wherever
that worktree's `booley.toml` lives. Write `/` on every host.

| Form | Example | Matches |
|---|---|---|
| File | `Makefile` | That file. |
| Glob | `firmware/**/*.c` | `*` and `?` match within one path component; `**` as a whole component matches any number of components, including none. Dotfiles match. |
| Directory | `vectors/` | Every file below it, the same as `vectors/**`. The trailing slash is required: `vectors` is refused with a hint to add it. |

**At Goal entry** Booley lists the matched files. It refuses entry, naming the
entry at fault, when:

- an entry matches no file;
- a match is untracked or ignored, or sits in a submodule or nested repository;
- an entry is empty or absolute, or contains `..`, a backslash, brackets,
  braces, or `**` joined to other characters (`**.c`);
- an entry reaches into `.git` or Goal Record storage;
- a matched symlink does not resolve to a tracked file inside the repository.
  That covers directory symlinks and dangling or escaping links.

**During the Goal** Booley freezes the list itself, the set of matched files,
and each file's contents, type, and executable bit. Adding, removing, renaming,
or editing a matched file warns at once and blocks Finish. Committing such a
change does too, even when the working files are restored afterward. The
warning names the entry, the file, and the kind of change, once per file however
many entries or built-in protections cover it. Restoring the file clears the
violation. An approved Goal Change cannot lift the protection: to change the
list, abandon the Goal and enter again.

Two limits:

- Keep build outputs out of protected patterns. A build that writes into a
  protected directory is a violation, so prefer `firmware/**/*.c` to `firmware/`
  when compilation writes there.
- As with built-in Protected Inputs, an edit made and reverted while a Flow is
  running is not detected.

### Flow and Specialist availability: `enabled`

Every Booley Flow builds and executes its command inside the Sandbox.
Set `[flows.<name>].enabled = false` to remove a Flow from Developer Agent discovery and autonomous
execution. Specialists use `[specialists.<name>].enabled`:

```toml
[flows.lint]
enabled = false

[specialists.coverage_analyst]
enabled = false
```

`enabled` defaults to `true` when omitted. For a Specialist, `enabled = false`
removes its MCP tool from Interactive Mode, makes it unavailable through
`booley specialist`, and removes it from Goal sessions' available
Specialists. The same switch filters project-defined Specialists.
`enabled` is the visibility key under `[specialists.<name>]`;
Specialist model selection belongs in `[models.roles]` (see
[Model selection](#model-selection-models)).

Specialist sections must be tables, and `enabled` must be a TOML boolean
(`true` or `false`, without quotes). Doctor and discovery reject unknown
Specialist names, including Flow names or direct MCP endpoints placed under
`[specialists]`. Malformed or unreadable configuration stops endpoint discovery
and Project checks rather than enabling every capability by default.

Protocol utility endpoints such as `booley_poll` have no Project
`enabled` switch; MCP server filters control their visibility.
Custom direct `McpTool` subclasses also have no Project enable switch. To
disable discovery, prefix the implementation filename with `_`, for example
rename `mcp_tools/project_check.py` to `mcp_tools/_project_check.py`. Custom
`BooleyFlow` and `Specialist` subclasses use their respective tables.

The Target selects the concrete EDA tool. For an approved commercial tool, the
Project can request only a provisioning source; the host owns the installation,
mount, wrapper, environment, and any License Profile.

### Explicit Flow Targets and Doctor selection

Every Target-aware CLI and MCP Flow call requires a `target`; there is no
project-wide fallback. Select Doctor's audit matrix beside each `.core` Target:

```yaml
targets:
  lint_core:
    flow: lint
    flow_options:
      tool: verilator
      booley: {doctor: [lint]}
```

Doctor dry-runs and deep-smokes every compatible Target whose `doctor` list
contains `sim`, `lint`, or `synth`. For `fpga`, plain Doctor performs the same
Target setup and source inspection as `fpga --dry-run` and probes `vivado` in
the Sandbox; deep Doctor reports the full implementation as skipped and
prints the target-specific manual command. Omit `booley.doctor` to keep a Target
available only for explicit calls. The allowed Doctor names are `sim`, `lint`,
`synth`, and `fpga`. A `[flows.fpga]` table makes that optional axis applicable,
so an enabled table without a marked compatible Target is a Doctor failure;
`enabled = false` is the explicit opt-out.

### Jobs & concurrency (`[jobs]`)

All Booley work executes inside the one per-folder Sandbox, and the
number of running EDA tools must be limited or the container runs out of memory.
Every Booley Flow or Specialist run is a **Job** with a **Job Class** determined by where
it executes; each class has a cap, and work beyond the cap waits in a queue
(FIFO within a priority class, running Jobs never preempted) rather than being refused. The defaults:

```toml
[jobs]
max_heavy   = 1   # in-container EDA subprocesses (sim, synth)
heavy_memory = "4g" # reserved memory per HEAVY job; calibrate with Doctor --deep
max_light   = 3   # Specialists (model-API-bound: reviewer, mutation_tester)
queue_max   = 8   # per-class queue depth; a full queue is the only BLOCKED response
```

`booley doctor --deep` performs a real end-to-end synthesis of every Target
whose `flow_options.booley.doctor` includes `synth`. It records each synthesis
boundary's EDA process-tree peak RSS, retains the largest selected-target
measurement, and applies 15% rounded-up headroom to the HEAVY reservation. A later plain
Doctor run warns when `[sandbox].memory` cannot admit the calibrated peak plus
Developer Agent memory and fixed session headroom. An OOM or timeout is an
incomplete calibration, never a successful PPA result.

The slot store is per-project. It does not arbitrate separate Projects' shared
host resources such as commercial-license seats.

A Simulation Campaign borrows the already-admitted Simulation Job's heavy slot
as its first lane; it does not acquire a second slot for the same work. At
`max_heavy = 1`, Campaign work is serial. With a larger cap, ready independent
work items may acquire child heavy slots up to that same Project-wide cap.
Literal `run_cwd` collisions still serialize locally, while distinct templated
attempt directories may overlap. Cancellation withdraws queued child claims.
Losing the lease on one of the additional child slots stops only the test using
it; the other tests finish, and the run then reports the lost lease. Losing the
Simulation Job's own slot still cancels the whole run. Each occupied slot stays
held until the matching supervised process tree is terminal, so stale capacity
is never treated as free while an EDA child is still alive.

Each admitted holder has a renewable recovery lease that is separate from its
optional work timeout. A Job launched through an explicit Sandbox Attachment
command inherits that execution's opaque ID; zero or many Job leases may link
to the same execution. When an owner disappears or a lease expires, recovery
atomically changes the lease from `active` to `cancelling` and requests scoped
execution cancellation. The slot remains occupied until the supervisor has
reaped the complete execution tree and published a terminal record. If that
supervisor or its record is lost, a later reaper recovers only processes that
carry the same opaque execution identity, checks their durable PID identities
before signaling, and publishes terminality only after no running process with
that identity remains. A dead root PID therefore cannot free capacity while
EDA descendants still run, and a failed supervisor cannot leave the holder
permanently cancelling.

Callers without an execution identity are process-owned. Their
entries use the same renewable lease plus PID namespace, process start time,
zombie state, and the existing argv/work-timeout guards where available. A
missing work timeout or promotion stamp falls back to the finite recovery lease;
records without a promotion stamp anchor that lease at their creation stamp.
An expired live owner
is identity-checked and its current descendant tree is cancelled before the
slot is released. Process-owned leases cannot recover descendants that escaped
after the owner died, so the stronger durable identity guarantee still applies
only to execution-owned leases.
Unknown future slot schemas and indeterminate same-user process observations
fail closed rather than freeing capacity.
Complete execution records are retained for seven days; an active Job lease
pins its referenced record, and nonterminal or unfamiliar records are never
garbage-collected automatically.

### Feedback (`[feedback]`)

Booley keeps an append-only log of what went wrong, what was merely annoying, and
what you think of Booley — `.booley/findings.jsonl`. Setup fills it as
it runs; afterwards, tell the **`/booley-feedback`** skill from any normal
working session. The skill decides how to classify and record what you said:

| Feedback | For | What the skill needs |
| --- | --- | --- |
| Defect | something broke | a reproduction, what happened, and what you expected |
| Friction | nothing broke, but it was confusing | where it happened and what you expected instead |
| Impression | what you think of Booley — praise, gripes, wishes | one sentence |

**Impressions are the feedback nobody thinks to give, and they decide what gets
built.** Bug
reports say what is broken; they never say whether the thing is worth using.
"Best part is the waveform flow", "the setup grill is exhausting", "I want
per-Target coverage", "this replaced three days of manual work" — all of it is
wanted, all of it rides the same redaction and the same offer. The skill sorts
the sentiment without making you learn a reporting interface.

The skill keeps one persistent report from that log:

- **`.booley/SETUP-REPORT.md`** — yours. Local, unredacted, never
  published, and always written. On a project that never ran setup it is
  called `FEEDBACK-REPORT.md` instead.

The skill derives the maintainer-facing view from the same log: only entries
attributable to Booley, with enough evidence to act on, not already filed, and
with project identifiers redacted. It is not saved as a second report. Ask the
skill for a redacted export only when you explicitly want that view persisted as
`.booley/BOOLEY-FEEDBACK.md` for manual sharing.

Booley never transmits feedback. The `[feedback]` table contains only redaction
settings used by an explicit export:

```toml
[feedback]
# redact_extra = ["codename"]   # extra terms to scrub from exported feedback
# redact_identifiers = false    # keep module/Target names (default: replace them)
```

Ask `/booley-feedback` when you want a redacted Markdown file to inspect and
share manually. Normal reporting never creates the export as a side effect.
After you share it, tell the skill where it went so those Findings are marked
filed and excluded from later exports.

#### What redaction does and does not do

**Replaced:** absolute paths (Project Root, `$HOME`, any `/home/<user>`), git remote
URLs and their `org/repo` slug, your `git config` name and email, and design
identifiers scraped from `booley.toml` and your `.core` files (project name,
Target names, VLNV segments, toplevels) — mapped to stable `<module-N>`
placeholders so the report still reads coherently. `redact_extra` adds your own
terms; explicitly configured `[stealth] banned_words` and `banned_substrings`
are scrubbed with their Stealth matching rules too, without shipped defaults.

**Kept, deliberately,** because a report without them is not actionable: EDA tool
names and versions, error text, tracebacks, and performance/area numbers. Two of
those carry real signal — which commercial EDA tools you license, and any
identifier sitting inside a quoted log line that was never in a `.core`. The
export says so before you share it.

**It is a denylist, not a proof.** The honest promise is "best-effort scrubbing,
and here is the diff" — which is why you read the exported file before sharing it.
For an open-source design whose module names are already public,
`redact_identifiers = false` produces a much more useful report.

That caveat bites hardest on **attachments**. A report can inline the tail of a
run log or a doctor transcript, which is usually what makes a bug diagnosable —
and a log line is arbitrary text no denylist can vet. Give the path to the skill;
attachments are redacted with the rest of the body and included in the exported
file. Read them there.

### Agent provider (`[agent]`)

The Developer Agent chat launcher and Specialists use one selected provider, Claude
or Codex:

```toml
[agent]
provider = "claude"   # or "codex"
```

`booley init` records this choice before it seeds the Sandbox. A TTY
offers `claude` and `auto` as the defaults; unattended initialization applies
the same defaults unless flags override them (for example,
`--provider codex --auth subscription`). Existing explicit `[agent]` values are
preserved on re-run. A Project with only `provider` configured receives the documented
`auto` policy on its next init, which is then recorded in the project config.

#### Pinning what bills (`[agent] auth`)

Pick which credential the Developer Agent and Specialists bill when several are present:

```toml
[agent]
provider = "claude"
auth = "subscription"   # "subscription" | "api_key" | "auto" (default)
```

- **`auto`** (default): the CLI's own precedence decides (an API key outbids a
  subscription); Booley reports the winner but changes nothing.
- **`subscription`**: bill the subscription. Booley scrubs the API-key env vars
  from Developer Agent and Specialist environments, along with
  `ANTHROPIC_AUTH_TOKEN`; a `booley auth` OAuth token still rides along.
- **`api_key`**: bill the API key.

Either non-`auto` value fails loud (`booley doctor`) when its credential is
absent, rather than silently falling back. See [USAGE.md: Auth &
billing](USAGE.md#auth--billing).

#### Routing Codex through a gateway (`BOOLEY_PRIMARY_AUTH=gateway`)

An operator-run gateway can bill Codex Specialist runs instead of a native
OpenAI credential. This policy is selected by the environment, not by
`booley.toml` (`[agent] auth = "gateway"` is rejected). Set these in the
environment of a Sandbox that does not come from `booley init`, such as a
Codespace or a hand-written devcontainer; Booley-issued Sandboxes take their
policy from `booley.toml`, and a host export is ignored (`booley doctor`
warns about it).

| Variable | Meaning |
| --- | --- |
| `BOOLEY_PRIMARY_AUTH=gateway` | Select the gateway policy. |
| `BOOLEY_GATEWAY_URL` | The gateway's `https://` base URL, passed to Codex as is (for example `https://gw.example/v1`). No credentials in the URL. |
| `BOOLEY_GATEWAY_TOKEN` | The bearer token. It is never written to a config file, and Codex shell tools do not receive it. |

`[agent] provider` must be `codex`. Codex Specialists use the gateway; no
OpenAI or Anthropic credential and no Codex login reaches them. The gateway
host must be reachable from the Sandbox. A missing variable, a non-`https` URL,
or a Claude provider fails `booley doctor` and `booley auth --status` naming the
problem; a complete configuration passes Doctor and exits 0. Text-only Codex
Specialists such as `coverage_analyst` do not support this policy yet. To run
every Specialist on one gateway model, see
[Pinning one model under a gateway](#pinning-one-model-under-a-gateway).

#### Git identity (`[agent.git]`)

```toml
[agent.git]
name = "Expected Developer"
email = "developer@example.com"
```

This is the default author and committer for every Git command in an
Interactive Mode or Goal Mode Sandbox (agents, the terminal, the editor's Git
UI). A missing or empty `name` falls back to `Dev`, `email` to `dev@localhost`.

The issued devcontainer spec sets it as command-scope Git settings
(`GIT_CONFIG_COUNT`, `GIT_CONFIG_KEY_<n>`, `GIT_CONFIG_VALUE_<n>`) in
`containerEnv` and `remoteEnv`. These beat any gitconfig and leave the host
checkout untouched; a hand-written spec, such as one for GitHub Codespaces,
must set the same variables. `git -c`, `git commit --author`, and
`GIT_AUTHOR_*`/`GIT_COMMITTER_*` still win. After changing `[agent.git]`,
refresh and recreate the Sandbox. Sandbox creation and startup remove a
complete worktree identity pair matching the current settings or
`Dev <dev@localhost>`; a different custom identity and other Git settings are
kept.

Doctor, run inside a Sandbox, warns with:

- `sandbox.git-identity-override` when the override is missing, partial,
  malformed, or differs from `[agent.git]`, naming what Git would use instead,
  if anything. A throwaway shell container has no override, so it warns there.
- `sandbox.gitconfig-identity` when system or global gitconfig sets another
  identity, such as a copied host `~/.gitconfig`; Git falls back to it whenever
  the override is missing. The image's own `Dev <dev@localhost>` does not count.

Either can be waived in `doctor-waivers.toml`; an active warning keeps Doctor
from recording a clean run.

### Model selection (`[models]`)

Booley Specialists select models through three **capability tiers**:
`heavy`, `standard`, `light`. Specialists floor at `standard`; cheap internal
steps run `light`. The Developer Agent uses its native CLI model settings. Each
provider ships defaults, so an untouched project needs no `[models]` at all.

Override a tier to move every Specialist and internal call on it at once:

```toml
[models]
heavy = "claude-fable-5"
standard = "claude-opus-4-8"
light = "claude-sonnet-5"
```

Tiers you leave out keep the active provider's default; overriding `heavy`
alone does not disturb the other two.

#### Pinning one Specialist (`[models.roles]`)

To choose a model for one Specialist, pin its role by name:

```toml
[models.roles]
reviewer = "claude-opus-4-8"
mutation_tester = "light"            # …or a tier name
```

A tier name resolves through the `[models]` table (and tracks any override of
it); anything else is passed to the provider verbatim. Specialist roles are `reviewer`, `mutation_tester`, and `coverage_analyst`. Any other
role FAILs `booley doctor`.

A pin overrides a Specialist's minimum-tier floor and sets the **model only**;
reasoning effort still follows the tier the Specialist would otherwise use (a no-op
on Claude, whose SDK exposes no per-call effort knob), except under a gateway
([below](#pinning-one-model-under-a-gateway)).

#### Pinning one model under a gateway

When `BOOLEY_PRIMARY_AUTH=gateway` is honored (see [Routing Codex through a
gateway](#routing-codex-through-a-gateway-booley_primary_authgateway)) and
`[agent] provider` is `codex`, two optional environment variables pin the model
for every Codex Specialist:

| Variable | Meaning |
| --- | --- |
| `BOOLEY_GATEWAY_MODEL` | One model id, no spaces, passed to Codex as is. It replaces every `[models]` tier, every `[models.roles]` pin, and every Specialist minimum-tier floor, including the model `booley doctor` uses for its developer probe. |
| `BOOLEY_GATEWAY_EFFORT` | Reasoning effort for every Codex Specialist call: `none` (no reasoning, not "unset"), `minimal`, `low`, `medium`, `high`, `xhigh`, `max`, `ultra`, or `persistent` (case-insensitive). Unset, each call keeps its usual effort. Whether the gateway's model supports a level is the gateway's call. |

An invalid value of either fails `booley doctor` (`agent.credential-policy`),
`booley chat`, and every Codex Specialist call before it starts; the message
names the variable, not its value. The interactive `booley chat` session itself
uses neither variable. Under any other policy, outside a Sandbox, or with a
non-Codex provider, both variables are ignored and `booley doctor` prints a NOTE.

### Dashboard and quiet Goal presence (`[goals]`)

The Sandbox Dashboard opens on VS Code Sandbox attachment by default. Configure
these values in the Booley Directory's `booley.toml`:

```toml
[sandbox]
dashboard = true

[goals]
quiet_after = 7200
```

Set `dashboard = false` to disable automatic opening. You can always open the
Dashboard manually with `booley dashboard` in the Sandbox. For opt-outs and
startup problems, see [Dashboard troubleshooting](TROUBLESHOOTING.md#the-dashboard-does-not-open-on-sandbox-attach).

`quiet_after` is a finite number of seconds from 60 through 604800, default
7200 (two hours). Quiet thread and worktree-fallback presence expires at this
threshold; process presence is removed only when its captured identity is
observably gone, reused, or a zombie. Unknown liveness is retained. Quiet-session
Doctor warnings describe the absence of recent attributable Booley calls;
they do not establish client inactivity or abandonment and never end Goal Mode.

## Advanced setups

Every flow builds from a mandatory FuseSoC `.core` Target (see [Design
description](TARGETS.md#design-description-core)). The two escape
hatches that remain, and what each is for:

- a **per-test non-RTL build step** (compiling the selected test's firmware,
  staging vectors) is [Pre-run
  commands](FLOW-SIM.md#pre-sim-commands-flowssimpre_run_commands);
- a **new kind of analysis** with its own criteria is a [Custom
  MCP tool](../internals/MCP-TOOLS.md) — it adds an MCP tool alongside the built-ins, never a
  side door into `SIM:{target}`.

A simulator outside the built-in matrix is out of scope for Goal Mode by
declared boundary; widening the matrix is the sanctioned extension axis
(per EDA tool: Edalize wiring → output parser → criteria-map row → Doctor probe).
The current matrix — image-bundled plus authorized host-provisioned EDA tools — is in
[SUPPORTED-EDA-TOOLS.md](SUPPORTED-EDA-TOOLS.md), and it grows over time.

**Language:** Booley drives SystemVerilog/Verilog only — VHDL is unsupported in
every Flow (see [SUPPORTED-EDA-TOOLS.md](SUPPORTED-EDA-TOOLS.md)).

### Custom MCP tools

Project-specific MCP tools can be added via `.booley/mcp_tools/`, following the same base-class interface as built-in MCP tools. The Developer Agent discovers and invokes them just like built-in MCP tools. See [MCP-TOOLS.md](../internals/MCP-TOOLS.md) for the architecture and extension guide.

### Worktree setup hooks

Project setup scripts can live under `.booley/hooks/`. After creating a
Goal worktree with `booley worktree new`, run any Project-specific setup it
needs from that worktree before entry. The command creates the worktree and
Worktree Booley Directory; it does not invoke these scripts automatically. Keep generated
build outputs ignored and commit deliberate Project inputs before Goal entry.
Hooks are Protected Inputs during Goal Mode.

Stealth Project configuration is outside the outer Git history. Back it up or
version it separately together with `FUSESOC_IGNORE`; a fresh clone cannot
reconstruct hidden configuration that was never exported.
