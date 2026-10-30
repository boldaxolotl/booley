# Configuration reference

Reference for `.booley/booley.toml`: the "what each knob means" companion to
[SETUP.md](SETUP.md), which is the ordered list of setup **steps**. This page
covers Project-wide settings. Per-Flow, Sandbox, and Stealth settings have
their own pages; [Where each setting lives](#where-each-setting-lives) lists
them. You do not fill these files by hand: the `booley-setup` skill authors them
interactively, and `booley doctor` validates them. When a step needs the
meaning of a field, it links here.

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

## booley.toml

Project identity, Flow selection, and Developer Agent and Specialist configuration live in
`.booley/booley.toml`. A per-knob reference follows, section by section.

The `.booley` entry may itself be a symlink to a Booley Directory elsewhere, such as a
separate clone of your Booley Directory. The directory it points to must contain
`booley.toml`. Booley uses that directory on the host, and in the Sandbox both
`/booley-project` and `/work/.booley` reach it. Cores you keep there still load only from
its `cores/` folder.

`booley init` pins where a linked `.booley` points. If you move the link on purpose, run
`booley init --repin-booley-dir` (or answer `y` at the prompt); otherwise point it back.
Doctor reports `project.booley-dir-pin` when they disagree. A running Sandbox keeps its previous
mount until `booley session refresh`. Start Sandboxes with `booley session up` or VS Code;
a raw `docker start` skips this check.

`[project]` carries the project `name`, `protected_inputs`, and `preflight_checks`.

Before a run, Booley checks that each Target's files exist on disk.
`[sources.testbench].preflight_checks = false` (default `true`) skips the check
for testbench files, for example when the TB is written during the run.
`[project].preflight_checks = false` skips it for all files, design and TB.

The sections below
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

`enabled` must be a TOML boolean; Doctor rejects unknown Specialist names.

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

`booley doctor --deep` measures synthesis peak memory and calibrates the HEAVY
reservation; plain Doctor then warns when `[sandbox].memory` is too small. An
OOM or timeout means calibration did not complete.

The slot store is per-project. It does not arbitrate separate Projects' shared
host resources such as commercial-license seats.

A Simulation Campaign runs its tests serially at `max_heavy = 1`; with a higher
cap, independent tests run in parallel up to that cap.

#### Job resource history

Booley measures every command a Job runs in the Sandbox and records each run,
by Target and Job phase, in `.booley/runtime/resources/history.json`. A record
holds:

- peak memory
- CPU time and wall-clock time
- the outcome

Measurement never changes a Job's result. `booley resources` summarizes the
history per Target and Job phase: run count and the minimum, median and maximum
peak memory and wall-clock time. `--json` prints the same statistics.

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

Impressions matter as much as bug reports: "the setup grill is exhausting" or
"this replaced three days of manual work" both shape what gets built.

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

`booley init` asks for the provider (default `claude`, auth `auto`); `--provider`
and `--auth` set them non-interactively. Existing values are preserved on re-run.

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

#### Git identity (`[agent.git]`)

```toml
[agent.git]
name = "Expected Developer"
email = "developer@example.com"
```

This is the default author and committer for every Git command in an
Interactive Mode or Goal Mode Sandbox (agents, the terminal, the editor's Git
UI). A missing or empty `name` falls back to `Dev`, `email` to `dev@localhost`.

It overrides any gitconfig in the Sandbox without touching the host checkout;
`git -c`, `--author`, and `GIT_AUTHOR_*`/`GIT_COMMITTER_*` still win. After
changing `[agent.git]`, refresh and recreate the Sandbox.

Doctor, run inside a Sandbox, warns when the identity Git would use differs
from `[agent.git]` and names the fix.

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
on Claude, whose SDK exposes no per-call effort knob).

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

`quiet_after` (seconds, 60–604800, default 7200) is how long a Goal session can
go without Booley calls before Doctor calls it quiet. Quiet never ends Goal Mode.

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

The current matrix — image-bundled plus authorized host-provisioned EDA tools — is in
[SUPPORTED-EDA-TOOLS.md](SUPPORTED-EDA-TOOLS.md), and it grows over time.

### Custom MCP tools

Project-specific MCP tools can be added via `.booley/mcp_tools/`, following the same base-class interface as built-in MCP tools. The Developer Agent discovers and invokes them just like built-in MCP tools. See [MCP-TOOLS.md](../internals/MCP-TOOLS.md) for the architecture and extension guide.

Custom `McpTool` subclasses have no `enabled` switch. To disable one, prefix its
filename with `_`, for example rename `mcp_tools/project_check.py` to
`mcp_tools/_project_check.py`. Custom `BooleyFlow` and `Specialist` subclasses
use their `[flows.<name>]` and `[specialists.<name>]` tables.

### Worktree setup hooks

Project setup scripts can live under `.booley/hooks/`. After creating a
Goal worktree with `booley worktree new`, run any Project-specific setup it
needs from that worktree before entry. The command creates the worktree and
Worktree Booley Directory; it does not invoke these scripts automatically. Keep generated
build outputs ignored and commit deliberate Project inputs before Goal entry.
Hooks are Protected Inputs during Goal Mode.
