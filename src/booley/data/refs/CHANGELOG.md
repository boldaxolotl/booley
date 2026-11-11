# Changelog

All notable user-visible changes to Booley are recorded here. Release entries
use stable `MAJOR.MINOR.PATCH` headings so Booley can review an exact upgrade
range from the packaged copy of this file.

Packaged release history starts at 0.2.7. For older changes, see
[GitHub Releases](https://github.com/boldaxolotl/Booley/releases).

## Unreleased

- The Mutation Tester now generates Mutants deterministically and runs no LLM:
  the same RTL, `--count`, and the new `--seed` (default 0) give identical
  Mutants. `--scope` is optional and defaults to the Target's own RTL. The
  default pass threshold is 80 % of the count, rounded up, also for a Goal
  without `min_detected`. Each Mutant's per-test timeout scales with the
  baseline run (10 x, 30 s minimum, 300 s cap), and a timed-out Mutant counts
  as detected and is reported as `detected (timeout)`. Results record scope
  file hashes, generator and pyslang versions, seed, and count. `--steer` and
  `--regen-lock` are removed, and the mutation lock is no longer used.

- `booley resources` now keeps each Test Variant of a simulation apart
  (`tests=first+slow`; the default Variant keeps the bare Test name, and the
  seeds of one Variant share an entry). Flow runs that `booley doctor` starts
  are no longer recorded, and each record stores the size of the Target's
  source files when it is known.
- The `sim` result card now names each Test Run that did not pass with its log
  (`log:`) and, when it ran with `--trace`, its waveform (`waveform:`); a
  `--trace` run lists its passing Test Runs with their waveform too.
  `simulation.json` `tests[]` rows and the MCP result carry `log` and `trace`
  paths for every Test Run (`null` when absent), so nobody searches the
  Campaign's work items for them.
- A second `booley chat` while the main checkout is in use now starts in its
  own worktree `s<N>` on branch `booley/s<N>`, named after the session handle,
  and prints a line saying where it works. The first session and a lone
  session run in the main checkout as before. `booley chat --new-worktree`
  always uses a worktree, `booley chat --here` always uses the main checkout,
  and `[chat] worktree = "auto" | "always" | "never"` sets the default
  (`auto`). Run from inside an existing worktree, `booley chat` starts there.
  A plain `claude` tab or VS Code panel does not hold the main checkout, so it
  is noticed only on a best-effort basis.
- New `booley coverage uncovered <coverage.json>` lists every uncovered scored
  point of one retained coverage run as `source:line  metric  detail  hierarchy`,
  sorted by source, line, and metric, with a summary count and the number of
  waived points left out. `--metric` and `--source` narrow the list. It works
  on the host and in the Sandbox, and the coverage guide uses it instead of the
  `jq` recipes.
- `booley flow sim --mode elab-only-standalone` now probes each module with the
  simulator of the Targets that use it: Icarus for an Icarus Target, Verilator
  for a Verilator Target, and both for a module shared by the two (a
  mixed-simulator project can take up to twice as long). A parse error from the
  Target's own compiler is now a design failure instead of "ungraded"; an
  Icarus-only project is probed with Icarus even when Verilator is installed. The
  `[flows.sim].standalone_frontend` key is retired: every Flow refuses to run
  while it is set, so delete it.
- A failed Simulation run of a Target without registered Tests is now listed as
  `<default>` (`<default>@N` when seeded) in `simulation.json` and the "Test
  Runs" lines, and its rerun command is `booley flow sim --target <name>
  [--seed N]` instead of an unrunnable `--test=<name>@N`.
- `[flows.sim].output_dir` is retired. It has had no effect; Doctor now warns
  `config.flow-key-retired` while it is set. Delete it. `[flows.synth].output_dir`
  is unchanged.
- `booley init` now runs inside a hand-written Sandbox (such as a Codespace)
  and performs Project Initialization only, so a spec can call it from
  `onCreateCommand` on every create: guidance links, Dashboard task, Git
  settings, and line endings, with no Docker or image work. It is idempotent
  and exits 0 when nothing changes. A Booley-issued Sandbox still refuses it
  and names the host command. The Dashboard attach refusal and Doctor hints
  now say where to run `booley init`.
- Stealth Mode is now off unless `.booley/booley.toml` sets
  `[stealth] enabled = true`; a missing key means off. A fresh `booley init`
  no longer hides `.booley/` or gives it its own repository before setup
  records your choice; the booley-setup skill's hidden answer does both. A
  Project set up earlier whose `booley.toml` has no `enabled` key has left
  Stealth: commit messages are no longer cleaned and pushes are no longer
  checked. `booley init` and Doctor warn `stealth.enabled-unset` while
  `.booley/` is still excluded; add `enabled = true` to keep Stealth, or
  `enabled = false` to confirm it is off. A `booley.toml` that can't be read
  keeps Stealth on. Unattended setup now keeps `.booley/` in your repository
  instead of hiding it.
- String parameters now reach Verilator and Icarus through Edalize's own
  quoting again, narrowing the 0.4.0 note that `str` and `file` vlogparams reach
  them as strings. Plain values such as `MAC` work; values with quotes,
  backslashes or whitespace, are not supported there. `default: ""` string
  parameters are dropped by FuseSoC again in every flow: Yosys (slang) and sv2v
  use the module's own HDL default for them, and the Vivado refusal does not
  fire for them. Non-empty Yosys (slang), sv2v and Vivado behavior is unchanged.
- `booley bootstrap --egress proxy|unproxied` records `[sandbox] egress` in the
  host config, creating the file if needed and changing nothing else in it, then
  bootstraps under that mode. `--check-only` reports the pending change without
  writing it, and running Sandboxes still on the old mode are named with their
  recreate steps.
- A fresh Sandbox no longer shows Claude Code's auto-mode default nudge on the
  first chat: registration marks it as seen (`hasSeenAutoDefaultNudge`), with or
  without a credential, and keeps any value already in `~/.claude.json`.
- `sim --trace` converts a declared `trace_files` VCD of any size; the 2 GiB
  limit is gone. Post-run VCD-to-FST conversion now has its own
  `[flows.sim].trace_convert_timeout_ms` budget (default `1800000`, 30 minutes)
  on top of `timeout_ms`, so a simulation that passes near its timeout is no
  longer reported as `timeout` while its waveform converts. A failed or
  timed-out conversion reports a trace incident: a failing simulation stays
  failed, a passing one is reported inconclusive, and neither is `timeout`.
  Icarus no longer reports an unconverted raw VCD as a waveform.
- A new IP now follows the same sequence as a port: `booley init`, then
  `/booley-setup new` on the host. `/booley-setup new` asks the design choices
  and writes the starter project with your answers, without prompting again. It
  now also works after a completed plain `booley init`: it keeps the `[agent]`
  settings, and unless you choose Stealth it removes an empty inner `.booley`
  repository and `/.booley` git exclusion left by an older `booley init`, or
  refuses (changing nothing) when that repository has commits. Plain `booley init` in a repo with no RTL now suggests
  `/booley-setup new`.
- `booley init --scaffold` now names Targets `<axis>_<ip>` (`sim_<ip>`,
  `lint_<ip>`, `synth_<ip>`, `fpga_<ip>`) with a matching `tests.toml` section.
  Doctor notes a Target it runs (or one in a stealth core) named just `sim`,
  `lint`, `synth`, or `fpga` and suggests `<axis>_<core name>`.
- A Coverage Campaign whose stored rollups or pass/fail metric rows disagree
  with its points now loads with values recomputed from the points instead of
  failing. A forged pass/fail on a blocked or not-requested evaluation or one
  that cannot be re-scored (no thresholds, invalid thresholds or a missing
  rollup) is still refused (`COV_EVALUATION_MISMATCH`), as is an incomplete
  collection (`COV_INCOMPLETE_EVALUATED`); an unknown threshold metric or
  malformed or repeated metric rows are refused with the field codes. The
  load no longer cross-checks collection state, selected tests against runs,
  or native-format and unknown-record consistency; those fields load as stored.
- `sim --trace` now converts a declared `trace_files` entry that is a literal
  relative `.vcd` path inside the run directory while the test runs, so the
  waveform is ready at run end and no VCD is left on disk. Other entries still
  convert after the run. `bwave build` now fails on a VCD holding a second
  header instead of merging the dumps.
- Doctor now scans the per-Project Session stores on the host and fails when a
  file is not named for the Project Root it holds, naming the file and its
  repair. On Windows the stores now name a Project Root without regard to
  case, so Sandbox issuance, pending refresh, and pending invalidation files
  written by earlier releases are not migrated: each fails closed until it is
  removed, naming the file. Remove it, then run
  `booley init --seed --project <root>` on the host to reissue the Sandbox
  spec. Linux and macOS file names do not change.
- A Sandbox that Booley issues now accepts `[agent.auth] codex = "gateway"`. It
  forwards the gateway settings from your shell for Codex only, and hides them
  from Claude sessions. Start VS Code from a shell that exports them (see
  "Codex through a model gateway" in `docs/user/SANDBOX.md`).
- Booley no longer migrates Sandbox state from releases before 0.3.0. The
  host-issued Sandbox spec stamp format version is bumped, so every spec issued
  by an earlier release is rejected as stale. Stamps at the old
  `eda/session-specs` location, specs that use the old registrar module or the
  `--project-root` initialize command, and stamps without a Booley Directory
  layout are rejected the same way. Doctor names the repair: run
  `booley init --seed` on the host.

  Finish or recover any interrupted `booley session refresh` before upgrading,
  because its journal cannot be resumed after the upgrade. If you already
  upgraded, every lifecycle command fails closed naming the journal file:
  remove it, then run `booley init --seed --project <root>` on the host.

  A Sandbox Image without the current provenance schema is stale and is rebuilt
  or re-pulled.

  `booley session prepare` now refuses, rather than stops and removes, a running
  VS Code container whose Booley Directory mounts are outdated, including any
  from before 0.3.0. Close VS Code, stop and remove that container, run
  `booley init --seed`, then reopen the folder in the container.

## 0.4.0 - 04 NOV 2026

Booley, until now "the open-source agentic RTL IDE", now describes itself as
the open-source Agentic Development Environment (ADE) for FPGA and ASIC
development. Only the positioning changed: it is the same product, and the
package, CLI and name stay the same. Goal Mode replaces Ticket Mode, and you
can try Booley in a browser through a free GitHub Codespaces demo.

### Major features

- **Goal Mode replaces Ticket Mode.** Ticket Mode was too heavy, took too
  long to set up and brought too many bugs. Goal Mode runs inside your own
  agent session: open one in the Sandbox and type `/booley-goal <request>`
  (`$booley-goal` in Codex). The agent turns the request and the Project's
  Goalsets into Goals, such as `SIM:sim_core` or `REVIEW:rtl_bugs`, and
  works on a Goal Branch in its own linked worktree.
  Every Goal is mandatory, and only a human can approve a Goal change. Clear
  the chat and type `/booley-goal start` to do the work; the same command
  resumes after a crash or reconnection. Finish produces a Review Package and
  a Session Summary, and the agent ends with their paths.
  - Flow and Specialist results count as Goal evidence only when they come
    from Goal-bound MCP calls. `goal_status`, `booley goal status`, the
    Dashboard and the Review Package show every Goal, its evidence and any
    open review findings. With no active Goal, status shows the last ended
    Goal and its Review Package.
  - Relative SIM, SYNTH and FPGA Goals measure a baseline at the Goal's base
    commit in a background Job. Status shows `baseline → current (delta, %)`
    and, for each frozen Test Run, how it changed since the baseline. Baseline
    Jobs run the Project's `.booley/hooks/post-setup` hook first and never
    delay your own runs.
  - A Review Goal names one `target` or `paths`. After you fix a clean
    review's findings, `reviewer --verify [--waive ID "reason"]` re-checks
    only those findings.
  - `[project].protected_inputs` lists extra tracked files, globs or
    directories, such as a `Makefile` or firmware, that a Goal must not
    change. Editing one discards affected evidence and blocks Finish until you
    restore it.
  - `booley worktree new <name>` creates a worktree under `.booley/worktrees/`
    with submodules filled in offline. Goal Mode uses it, and you can use it
    for parallel work. It works in Stealth Mode and with a Paired Checkout.
- **Free GitHub Codespaces demo.** Click "Open in GitHub Codespaces" in the
  README Quick Start or on <https://booley.dev/demos/> and run a Goal on the
  PicoRV32 demo with nothing installed. The demo fits GitHub's free monthly
  Codespaces allowance; the first start takes 3 to 4 minutes. After a GitHub
  sign-in, `booley-demo-claim` gets sponsored GPT-6 Luna tokens, or you can
  sign in with your own Claude or ChatGPT account. <https://booley.dev/demo/>
  lists the limits.
- **Sandbox Dashboard.** `booley dashboard` shows Sessions and Jobs, each in
  its own window with its own selection, scroll and history. Sessions are
  grouped by whether their agent is working, waiting for a tool call, or
  waiting for you, as reported by Claude Code and Codex hooks in the Sandbox
  Image. Jobs show their EDA tool, CPU time and peak memory, and can be
  filtered. Sessions and Jobs have short handles such as `S17` and `J-SIM25`
  that `booley chat` also uses. Navigate with the arrow keys, Enter and Esc; a
  terminal at least 120 columns wide gets a preview pane. Report, artifact
  and evidence paths open in the editor on click. The Dashboard opens in its
  own editor tab when you attach VS Code to a Sandbox or open a Codespace.
- **Test Variants and seeded Test Runs** (#1233). `tests.toml` declares extra
  run-time configurations of a Test as Test Variants
  (`[<target>.variants.<test>]`: arguments, environment and a required
  `default`), and every Test Run of a seeded Target has a recorded seed. A
  Test Run is written `test[+variant][@seed]`.
  - `booley flow sim --test` and the `sim` MCP tool accept `test`,
    `test+variant`, `test@seed`, `test+variant@seed` and `test[+variant]@xN`
    for N drawn seeds. `--seed N|random` (MCP `seed`) sets or draws the seed
    of every selected Test Run.
  - Booley passes the seed: `+verilator+seed+<seed>` on Verilator, cocotb's
    seed variable on Cocotb Targets, and the new `tests.toml` `seed` template
    on Verilator and Icarus. An Icarus HDL-testbench Target without a `seed`
    template is unseeded and refuses seeds. A Target can set `default_seed`.
  - Reports list every Test Run and print a rerun command, with its seed, for
    each failure. `simulation.json` gains `test_runs[]` rows. Coverage runs
    accept every selector form and merge their Test Runs.
  - Pre-Sim Commands receive `BOOLEY_TEST_RUN_NAMES`, and for a single Test
    Run `BOOLEY_TEST_RUN_NAME`, `BOOLEY_TEST_VARIANT` and `BOOLEY_TEST_SEED`.
    Doctor prints how each Simulation Target seeds its Test Runs.
  - Simulation and coverage Goals take the same selectors, `@xN` included.
    Goal Mode freezes the exact Test Runs at entry, so later `tests.toml`
    edits cannot change what a Goal requires. A failed Test Run stays failed
    until the code changes.
- **booley.dev.** Booley has a project site at <https://booley.dev/> with
  Demos and Videos pages, a blog, and application note AN-001, "Same AI agent,
  same chip bug: 56 minutes with a shell, 21 with the right tools". The user
  docs live there too, split into smaller pages with search (press `/`), an
  "On this page" outline and light and dark themes.
  Links Booley prints, the README and PyPI point there. Old
  `boldaxolotl.github.io/booley/` links redirect; links into a section that
  moved open its old page.

### Regular features

- `BOOLEY_AGENT_APP=auto` (hand-written Sandbox specs, such as a Codespace)
  picks the agent app at every start: Claude, then Codex, then a configured
  gateway, then Claude with its login offered. Specialists, the MCP server,
  `booley chat` and Doctor follow that choice, and Doctor says why. `auto`
  outside a Sandbox is refused.
- **Sandboxes without the egress proxy.** Host `config.toml` `[sandbox]
  egress = "unproxied"` (default `proxy`) runs Sandboxes on a plain Docker
  network; it cannot be combined with an `egress_allowlist`. Host Bootstrap
  then stops running the proxy and removes it once no Sandbox uses it.
  In-Sandbox Doctor WARNs `sandbox.unproxied`; host Doctor WARNs
  `host.sandbox-egress-mismatch` for a running Sandbox in the other mode.
  After changing the setting, run `booley init --seed` and rebuild.
- **Live Flow progress.** `booley flow synth|fpga|sim|lint` shows a compact
  status row with the Flow, Target, step, elapsed time and latest tool output,
  with one row per parallel lane. Redirected output gets timestamped
  `[booley-progress]` heartbeats. `-q/--quiet` shows only the verdict and
  errors. Synthesis and FPGA runs end with a per-Target table (Δ columns with
  `--baseline`), and Simulation ends with a Test, Result, Cycles, Time and
  Build table.
- **`booley resources`.** Booley records each Flow command's peak memory,
  temporary-directory size, CPU and wall time in
  `.booley/runtime/resources/history.json`. `booley resources [--json]`
  prints min, median and max per Target, phase and Test Run. Each Flow
  command now gets a private `TMPDIR` that is removed afterwards.
- **B-Wave clock analysis.** The new `bwave overview` prints the sampling
  clock's verdict, any stretches where it stops, and trace metadata. Literal
  `find` and `sample` triggers skip cycles inside a clock stop unless you pass
  `--include-stopped`, and `--sample-mode pre-edge` reads values just before
  each edge. `signal`, `wave` and `distance` gain `--format json`.
- `bwave gui` now drives VaporView 1.6.0 through a Viewer Bridge that the
  Booley Sandbox Integration extension runs in every attached window, instead
  of VaporView's WCP port. Scoped views keep groups, radix and color
  suffixes, `--time` markers, `--cursor`, `--append` and readback; markers are
  read back too. With several windows attached, a command targets the window
  whose terminal you typed it in. `-t` or `--cursor` without signals only
  moves the time window and markers. VaporView now installs from the Sandbox
  Image, with no network and no auto-update.
- B-Wave `-s` globs also match relative to the common scope `bwave list`
  prints: with `# scope: TOP.tb`, `-s 'dut.*'` finds `TOP.tb.dut.*`. A glob
  that matches nothing suggests the full-path glob that would.
- The MCP server picks up `booley.toml` and custom-endpoint changes without a
  Sandbox restart. Disabled Flows and Specialists leave the tool list,
  re-enabled and custom tools appear, and clients are told to reload. An
  invalid configuration fails closed.
- `booley init --scaffold --synth-mode logical|physical` sets the scaffold's
  ASIC synthesis mode (default `physical`; a logical scaffold has no SDC).
  `booley init --scaffold <name> --stealth` keeps the scaffold's cores under
  `.booley/cores/`, so nothing Booley-authored reaches the RTL repository.
- `booley projects forget <root>` lists the Docker images, state volumes,
  containers and networks the Project still holds, with sizes. `--purge`
  removes its unused images and state volumes, including the agent login copy
  and history, and says why it kept anything. `booley init` removes Project
  images its rebuild replaced.

### Quality of life

- `booley doctor` groups checks under section headers with one status glyph
  and ends with problem tables and one bold verdict line. Init, bootstrap,
  `auth --status`, `feedback list`, `eda … list`, `flow`/`specialist`
  listings, `goal status` and `booley targets` print aligned tables, and
  `booley cheat` fits the terminal. Piped output is plain; `--json` is
  unchanged.
- `booley targets` adds a group row per `.core` file, a `✓` Doctor column, and
  a FLOW column listing the Booley Flows that can drive each Target.
- Project commands accept `-C/--project PATH`, and Flows and Specialists take
  `--timeout DURATION` in human units. Commands that need a Project refuse up
  front outside one.
- When `--target` is omitted, `flow sim`, `lint`, `synth` and `fpga` pick the
  only eligible Target and say so; with none or several they exit 2 and list
  the eligible Targets.
- Flow and Specialist `--help` uses CLI spelling, groups options into Common,
  Output, Flow-specific and Expert sections, says what each default does, and
  ends with an example.
- `booley help` matches `booley --help`, which groups commands by where they
  run. Bare `booley` on the host prints help and a getting-started hint.
  `booley cheat` lists its sections; `--all` prints everything, and `--goals`
  includes the Goal catalog.
- `booley --version` shows the commit date, for example
  `booley 0.4.0 (d4c94dc35, 2026-10-26)`.
- `booley chat` passes extra arguments to the agent CLI, for example
  `booley chat resume --last` (Codex) or `booley chat --resume <id>` (Claude).
- The Dashboard and CLI show durations as `10m 00s`, ages as `Started 10m ago`
  and local clock times. JSON and report fields are unchanged.
- `booley init` on an unmodified clone of a published `booley-prj-*` Project
  reports the Project ready instead of sending you to `/booley-setup`.
- Setup's Step 0 asks its questions in plain English with a hardware glossary
  and one confirmation block for the defaults; "Keep Booley out of your git
  history?" sets Stealth Mode. Bootstrap warns, with install links, when
  neither Claude Code nor Codex is on the host.
- Project Setup's Memory Timing Surrogate guidance now requires every read-data
  bit to depend, through registers, on the memory's write data, address and
  enables, and adds a no-pruning probe. A surrogate built under the old rule
  can let synthesis delete logic and understate area and Fmax: re-run setup's
  surrogate validation and fix any surrogate that fails.
- In a Codespace or any Sandbox without a seeded host agent config, Claude
  Code and Codex skip onboarding, login and folder-trust prompts when a
  credential is present, and no longer try to update themselves.
- Local Sandboxes install the Verilog-HDL and Live Preview extensions from the
  Sandbox Image; before, the locked-down network blocked them. VS Code's Chat
  side bar is closed once per workspace, and Codespace startup files open
  with their associated editor, so a getting-started Markdown file opens
  rendered.
- Cost readouts price `gpt-6-astra`, `gpt-6-luna` and `claude-haiku-5-5` at
  their own rates; they used to show $0.00 or the Haiku 4.5 price. Unknown
  `gpt-*` models are costed at the Astra rate.
- Doctor adds WARNs for: Git excludes hiding Project inputs
  (`project.inputs-ignored`), a `copyto` input that overwrites another file
  (`sim.runtime-input-collision`), a Sandbox Git identity that differs from
  `[agent.git]` (`sandbox.git-identity-override`, `sandbox.gitconfig-identity`),
  oversized MCP handle maps (`handles.map-size`), and a Stealth scaffold whose
  cores are still tracked (`stealth.scaffold-core-tracked`). Many directory
  symlinks to one ancestor now produce one FAIL with examples.
- In a Codespace on the demo gateway, a missing gateway token is a Doctor WARN
  instead of a FAIL. With an explicit `BOOLEY_AGENT_APP`, it still fails.
- Mutation Tester `--dry-run` explains how `--count auto` was computed.
- Job reports carry per-Target coverage percentages and up to 20 lint
  findings, so MCP clients need not open the full reports.
- Persistent pipx, uv tool and ordinary wheel venv installs can be the
  canonical host installation. The README leads with pipx.
- The user docs were rewritten for the engineer using Booley, with an Install
  guide and a new Specialists page, and checked against the code.
- The RISC-V Sandbox Image ships the unprivileged and privileged ISA manuals
  from upstream release `20250508`, as `riscv-isa-unprivileged.{pdf,html}`
  and `riscv-isa-privileged.{pdf,html}` under `/opt/riscv-docs`. References
  to `riscv-isa-manual.{pdf,html}` must name a volume.

### Bug fixes

- A Project whose `.booley` is a symlink, as in a GitHub Codespace or a
  Booley Directory kept in its own clone, now works throughout: Sandbox start,
  Targets and Stealth cores, Simulation and coverage (including agent `sim`
  calls), synthesis, Goal Mode, Pre-Sim hooks, deep Doctor and the
  Specialists. Only the `.booley` entry itself may be a link.
- Goal worktrees and `booley worktree new` fill in the Project's git
  submodules offline, so baseline checkouts no longer fail with "submodule …
  not found in source Project".
- `sim` with relative Cycle Count limits now works: it no longer fails
  planning with `.baseline-wt-... already exists` or `prerequisite identities
  must be unique`, builds the baseline in the baseline checkout, and can meet
  the limit.
- `booley session refresh` no longer aborts on an untagged Docker image, and
  `session up` no longer fails with "Sandbox Project identity disagrees" or
  re-runs automatic Doctor on every start. `session` commands from a linked
  worktree find the owning Project's Sandbox.
- `booley init --check` and `--check-only` list everything a full init would
  do, with a matching exit code. `booley bootstrap --check-only` runs every
  safe check and exits 1 for pending work, 2 for a blocker. A failed
  line-ending step names the real cause.
- `booley init` and other writers into Sandbox-writable paths no longer write
  through a symlink, keep numbered backups (`.1`, `.2`, …), write atomically
  and never widen file modes.
- Reviewer runs and `booley feedback` no longer leave a versioned Booley
  Directory dirty once the `booley init` ignore backfill in Upgrade notes is
  committed.
- The Mutation Tester no longer leaves a mutant in your RTL after a cancelled
  or killed run, and no longer moves testbench directories out of the live
  worktree. It passes a Target's `copyto` inputs, plusarg defaults and `env`
  to every run, applies a qualified `--target`'s Criterion settings, and
  handles testbench tags on symlinked sources.
- Verilator and Icarus no longer kill themselves with exit 143 when Booley is
  PID 1, which showed up as `COV_RAW_FILE_MISSING`.
- `str` and `file` vlogparams reach Yosys, sv2v, Verilator and Icarus as
  strings. Vivado refuses a string override before running. Cached results
  that used string parameters are invalidated.
- ASIC synthesis no longer fails when Yosys warned about a combinational loop
  that its final check no longer finds; the warning becomes a WARN.
- Lint, synthesis and FPGA runs over several Targets no longer show one
  Target's log under another.
- A background `sim` Job polled through MCP reports its live stage instead of
  staying at `starting`. Losing one work item's Job Slot in a parallel
  Simulation Campaign stops only that item, and a passed item no longer fails
  with `heavy lease lost`. Parallel tests' progress lines stay under their
  own test.
- After a cocotb 2.x batch times out, finished tests keep their real
  outcomes, without duplicates. A traced Cocotb run on Verilator records
  `verilator` as its simulator.
- A traced Icarus run with `trace_args` set no longer exits 2, and it no longer
  reports `TRACE_OK` for a stale trace file.
- In Stealth Mode, `flow sim --trace` works for a Target from a
  repository-native `.core`.
- A bare-string `[flows.sim].pre_run_commands` is refused in preflight instead
  of being split into characters. Immutable-mode Pre-Sim Commands run in the
  Project Root as documented, including with `run_cwd`; a hook that relied on
  the old directory should `cd "$BOOLEY_RUN_CWD"`.
- `tests.toml` sections match Targets by identity: a qualified key serves only
  its core, a bare key serves Targets without one, and Doctor fails duplicate
  or dead keys.
- Compiler-cache settings no longer fail Simulation when `booley.toml` is
  unreadable. Without `BOOLEY_COMPILER_CACHE_ROOT`, builds run uncached with a
  warning. Verilator `make_options` and Make variables such as `OBJCACHE` and
  `MAKEFLAGS` are no longer rejected.
- Templated `run_cwd` cleanup removes the empty parents Booley created and no
  longer leaves run-lock files.
- B-Wave: physical time bounds and tokens convert exactly and pick the right
  clock edge, including on phase-offset clocks; `wave --async` windows are no
  longer empty; synchronous `find`, `sample` and `distance` use clock-edge
  values and ignore glitches between edges; `--format json` carries the same
  warnings as text output.
- Doctor no longer stops with a `TypeError` when the activity hooks or
  Dashboard extension are missing or when a Target has untagged C/C++
  harness files; it WARNs instead. Doctor's attached-window checks now run.
  In a Codespace it accepts `~/.ssh/authorized_keys` and a symlinked
  `BOOLEY_DIR`. Host `doctor --deep` no longer fails simulation checks on
  Booley's own `/booley-project` alias.
- Deep Doctor warns when a simulation bad overlay fails to build and requires
  a behavioral failure to accept it. It no longer blocks on the expected
  upgrade-review warning.
- The Reviewer no longer fails when a Claude agent reports findings through
  `ReportFindings` more than once.
- Text-only Codex Specialists such as `coverage_analyst` no longer need the
  model in the Codex models cache. When Codex exits early, Booley reports
  Codex's error instead of `BrokenPipeError`.
- Interactive Claude Code no longer moves long Booley MCP calls into the
  background after 120 seconds.
- Nested Codex homes link the real login instead of copying it, so token
  refreshes reach it; temp directories from killed runs are swept.
- `booley auth` checks permissions before writing and no longer reports
  success before failing.
- A command that cannot start prints `command not found` (127) or
  `permission denied` (126) instead of a traceback. Start notices appear only
  after the process starts.
- Quiet image builds print each progress line once.
- Booley MCP servers that Booley starts for its own agents no longer appear
  in the Dashboard.
- The idle reaper keeps a Sandbox while an editor or terminal is attached.
  Session commands reuse VS Code-owned Sandboxes and refuse to tear them down.
- Elab-only Simulation of standalone modules is no longer confused by `/*` or
  `//` inside string literals.
- Feedback IDs stay unique across processes.
- Booley finds VaporView and VS Code settings under `~/.vscode-remote`
  (Codespaces) and `~/.vscode-server-insiders`.
- `booley projects prune-keepers` prints `Nothing to release.` when no keeper
  is eligible.
- On Windows, reads and writes of Job, Simulation, Session, Goal and Doctor
  files retry through brief sharing errors instead of reporting the file
  missing, so Campaigns, baselines and cancellations no longer fail spuriously.

### Internal work

- Ticket Mode was removed (about 128,000 lines). Shared Flow loading, agent
  execution and waiver storage moved to their active owners;
  `booley.ticket_board` is gone.
- Simplification campaign: Goal Records, campaigns, coverage, image
  lifecycle, the CLI parser and MCP were slimmed, and formats that no release
  ever wrote were removed.
- The Codespaces demo's sponsored tokens go through an OpenAI-compatible
  gateway. Inside a Sandbox, `BOOLEY_PRIMARY_AUTH=gateway` with
  `BOOLEY_GATEWAY_URL` and `BOOLEY_GATEWAY_TOKEN` or
  `BOOLEY_GATEWAY_TOKEN_COMMAND` routes Codex, `booley chat` and Codex
  Specialists through it. This is plumbing for the demo.
- QA missions were reworked for Goal Mode, and CI drives the PicoRV32 demo
  through a Goal.
- CI: test timing and failure evidence, a guard against suppressed
  assertions, sharded Ubuntu suites, a narrower job set for docs-only PRs, and
  a weekly worktree-mount acceptance job.
- Designs: Goal Mode and the Dashboard (ADRs 0067, 0068), Test Variants (ADR
  0072), the Result Cache (ADR 0076), and one worktree per Interactive session
  (ADR 0082, proposed).

### Upgrade notes

- **Ticket Mode is gone.** `booley run` and `booley board` print a pointer to
  `/booley-goal` and exit 2; the Ticket skills are replaced by `/booley-goal`.
  Doctor WARNs `tickets.leftover-board` on nonempty
  `tickets/{board,state,logs,locks,waiver-candidates}`: keep what you need,
  then delete them. `tickets.leftover-guidance` covers `ticket_creation.md`
  and `ticket_defaults.md`; move edited rules into Goalsets. `tickets/history/`
  is untouched. `[developer]`, `[developer.auto_retry]` and
  `[jobs].max_tickets` are ignored; Doctor WARNs until you delete them.
- **The Booley Directory is now `.booley/`.** Use `BOOLEY_DIR` and
  `booley.runtime.booley_dir`. Run full `booley init` in **each existing
  clone before pulling** a Project's rename commit; it migrates the old
  directory and repairs linked worktrees and Paired Checkouts. Stop running
  Sandboxes first. `init --check` and Doctor report a pending migration.
  Review and commit the tracked renames in each checkout; init does not stage
  them. If a pull already left both `.booley_project/` and `.booley/`, init
  refuses unchanged. Do not `git add -A`: removed old ignore rules may expose
  local runtime, Goal and worktree state. Stop consumers and back up both trees
  and Git state. If the new directory holds only current tracked files, park
  it intact at a unique location outside every checkout, and keep the old
  directory and its worktrees. Then reconcile the parked tracked inputs into
  the old directory by hand, without overwriting local state, and run full
  init with only the old directory present. Verify tracked inputs, Goal
  inspection and both repositories' worktree registrations before retiring
  any backup. Booley never deletes or merges these trees itself. An
  interrupted migration keeps its journal: if a retry reports changed Git
  identities, HEADs or conflicting files, stop other writers, preserve the
  trees, Git metadata and journal, and reconcile the named conflicts before
  rerunning init. Do not delete the journal, prune worktrees or reset history
  to get past verification.
- **Refresh every Sandbox** after `booley bootstrap --update`: run
  `booley init`, then `booley session refresh` in each Project. The Sandbox
  now carries its image ID and is refused at start until refreshed. The
  refresh brings the Dashboard hooks, the bundled VaporView 1.6.0 and VS Code
  extensions, and drops the Claude Code, Codex and Tcl VS Code extensions;
  the agent CLI in the terminal is the agent surface. Doctor reports any
  remaining drift.
- **Seeded Targets now default to seed 1** (#1233). Verilator and Cocotb runs
  that name no seed use seed `1` instead of the simulator's default, so
  random stimulus can change. Set `default_seed` to keep another seed.
  Icarus HDL-testbench Targets are unchanged until they declare a `seed`
  template.
- **`tests.toml` is stricter** (#1233). Rerun `booley doctor` after each fix
  until it loads:
  - A Test name must not be empty or `default`, start with `-`, exceed 512
    bytes, or contain whitespace, a control character, `+`, `@`, `<`, `>`, a
    quote or a backslash. A Test appears at most once in `tests` and once in
    `skip`.
  - A Target section accepts only `tests`, `test_list`, `skip`, `select`,
    `env`, `seed`, `default_seed` and `variants`.
  - `env` may not set `MODULE`, `TESTCASE`, `TOPLEVEL`, `TOPLEVEL_LANG`,
    `RANDOM_SEED`, `LIBPYTHON_LOC`, `PYTHONPATH`, `LD_LIBRARY_PATH`, or any
    `BOOLEY_*`, `COCOTB_*`, `PYGPI_*` or `GPI_*` name. Use `default_seed`
    instead of `RANDOM_SEED`.
  - `select` must contain `{index}` or `{name}` and may not start with `++`
    or `+-`.
  - Empty `pre_run_commands` entries, empty toplevels and `copyto`
    destinations outside the run directory are refused.
- **Simulation paths changed** (#1233). Run directories and coverage run IDs
  are named by Test Run, so seeded Targets gain `@seed`
  (`tests/reset@1/`, `run:001:reset@1`). Read paths from `report.json` and
  `simulation.json` instead of building them.
- **0.3.0 Simulation and Coverage Campaigns are no longer read** (#2342,
  #2421). Resume, inspect, prune, analyze and Cycle Count baselines stop with
  an error naming the directory; delete it by hand or collect again. You can
  delete `.runtime/campaign-child-executions/`.
- **Simulation grading** (#1233): an infrastructure stop is graded `error`;
  Mutation Tester scores can change now that runs use the Target's plusarg
  defaults and `env`; one request runs at most 1000 Test Runs of a Target.
- Doctor fails `.core` `flow_options.defines`, which no Flow ever applied;
  declare `vlogdefine` parameters instead and re-plan Campaigns that used it.
- `[models.roles]` accepts only `reviewer`, `mutation_tester` and
  `coverage_analyst`. Delete `developer`, `recovery`, `triage_report` and
  `tb_coder` pins; Doctor and agent commands refuse them. The hidden
  `tb_coder` Specialist is removed.
- `--work-dir`, `--project-root` and `--timeout-ms` still work but print a
  notice; switch to `-C/--project` and `--timeout`. The hidden top-level
  `--board/-b`, `--cheat` and `--doctor` flags are removed. `goal`,
  `session`, `feedback`, `cleanup` and `upgrade` are no longer listed in
  top-level help; `booley cheat --skills` shows them.
- Scripts that grep Doctor's `[PASS]` or `=== X ===` text, or init's `[!!]`
  and `[XX]` markers, must change; use `--json` where available.
- B-Wave JSON output is schema revision 2. Literal match counts change on
  gated clocks.
- Under `[agent] auth = "auto"`, Sandboxes forward an exported
  `ANTHROPIC_API_KEY`, which bills per token. Set
  `[agent] auth = "subscription"` to avoid that.
  `booley auth --status` and Doctor fail when the selected credential is
  missing.
- With a symlinked `.booley`, run `booley init` once to pin its target, and
  `booley init --repin-booley-dir` after you retarget the link. Doctor FAILs
  `project.booley-dir-pin` otherwise.
- Rerun `booley init` in each checkout and commit its ignore backfill for
  Specialist reports and state. New non-Stealth Projects keep Goal history
  eligible for commits; init never stages or commits it.
- Booley adds a Dashboard task to `.vscode/tasks.json`; opt out with
  `[sandbox].dashboard = false`. A tracked `tasks.json` is left alone.
- Doctor treats unreadable sibling Git attributes as unknown policy;
  `git.attributes-policy-unknown` can stay a WARN on older Git, and
  `init --check` exits 1 until it is resolved or waived. Existing
  `git.line-endings-unreadable` waivers do not cover it.
- `booley cleanup apply --run-id <id>` removes the whole Setup run root;
  `booley cleanup record` and its `--digest`, `--retention` and
  `--cache-disposition` options are removed. Delete run roots from older
  versions by hand.
- Coverage finds RTL files without coverage points by a lexical scan; the
  Analyst's `zero_point_sources` view drops `discovery_status`,
  `comparison_status` and `unusable_findings`.
- Deep Doctor keeps only the latest attempt; `deep_status` drops
  `last_success`, `checks` and `latest-attempt-not-qualified`.
- Native-only coverage pruning deletes the whole `native/` directory. Delete
  any `.native-pruned/` left by an older interrupted prune before retrying.
- Hook files retired by `booley init` are renamed `<name>.pre-booley`.
- Unbound Flow and Specialist reports omit `criterion_key` and
  `criterion_met`; read them as optional.
- Doctor defers generated cocotb test IDs to run time.
- Known issue (#1529): the Sandbox shares writable Git metadata, so a
  Developer Agent can plant commands that later run as you when host Git
  runs, including from Doctor. SECURITY.md lists precautions.

## 0.3.0 - 05 OCT 2026

From this release on, Booley's development focuses on quality: finding as many
bugs as possible and making the existing workflows easier to use, rather than
adding new features.

### Major features

- **Coverage support.** `booley flow sim --coverage` (alias `--cov`, MCP
  `coverage: true`) collects native Verilator line, branch, expression, toggle,
  and `cover_property` coverage. Coverage never changes a Simulation verdict;
  run headlines report simulation, collection, and evaluation separately.
  - Each run writes a Coverage Campaign under
    `sim/<N>/targets/<target>/coverage.json`, with a compressed points file and
    per-source-file rollups. Collection that is incomplete or uses an
    incompatible collector publishes no scores. RTL files with no coverage
    points get advisory source-gap findings.
  - A Ticket can require coverage with a `COVERAGE` Criterion that sets
    per-metric `min_pct` on Verilator Targets. Ticket validation rejects
    `COVERAGE` on other simulators before queueing. A missed threshold
    reports `criterion_met: false` and exits 0. Tests come from `--test`,
    `--tests-file`, or the registered suite, not from the Criterion.
  - `.core` files configure coverage under `flow_options.booley.coverage`
    (`reset_included`, `custom_main_hooks`). Booley validates the recipe before
    any build.
  - Approved waivers live in the `[coverage.waivers]` approval directory, can
    reference formal proof artifacts, and are protected Ticket acceptance
    inputs. Every coverage run applies them. Waived points are reported as
    `waived` and left out of the percentages. `--no-waivers` (MCP
    `no_waivers`) reports raw numbers.
  - The Coverage Analyst Specialist is now read-only. It takes one
    `--campaign <coverage.json>`, queries the evidence through a private tool,
    separates observed facts from hypotheses, and screens Waiver Candidates. In
    Ticket Mode, a Ticket that meets its mandatory coverage only with Waiver
    Candidates goes to `review`.
    `booley board approve <slug> --accept-waivers ID,... --reject-waivers ID,...`
    requires a decision for each candidate and publishes the accepted
    waivers with the Ticket's merge.
  - `python -m booley.flows.sim.campaign_retention` prunes old Campaigns
    (`--invocation N --native-target T`, or `--full`, with
    `--include-dependents`). It refuses to remove active or
    unauthenticated files.
- **QA workflows.** Public QA is now a set of timeboxed Markdown bug-hunt
  missions under `qa/missions/` (picorv32, taxi, uart, and coverage). They
  include an independent UART hardware evaluator and a shared coverage
  fixture project. A run records `findings.md` and `log.md` and works around
  failures so later areas still get tested. `qa/SMOKE.md` holds a ten-item
  release smoke list. Three maintainer skills support the missions:
  `booley-qa-run` runs a mission, `booley-qa-triage` walks findings one cluster
  at a time and logs decisions to `triage.md`, and `booley-add-to-qa` adds new
  coverage. As a maintainer-only exception to Bootstrap's source-checkout
  rule, `booley bootstrap --with-qa-skills` run from a clean source checkout
  installs them on the host, and `--without-qa-skills` removes only the
  managed links. They are not shipped in
  release wheels or mounted into the Sandbox.

### Regular features

- Each Simulation now records its exact Target workload as a durable
  Simulation Campaign with an immutable manifest, shared authenticated
  simulator builds, and isolated attempts.
  `sim --resume-from <campaign/manifest.json>` finishes only the named
  Campaign; with `--dry-run` it previews completed, interrupted, pending, and
  mismatched work. Campaign reports use relative, digest-bound references, so
  a reports root can be moved or copied. The CLI prints the
  `campaign manifest:` path before simulator work starts, and each verdict card
  repeats the manifest and report paths. Resume refusals and previews name the
  changed sources, runtime inputs, suite, and parameters; `--verbose` lists
  every mismatch.
- Simulation reuses a simulator build when a Target variant (plain, traced, or
  coverage) was already built from identical inputs, so a firmware-only
  iteration skips the Verilate step. Booley re-hashes every file Verilator and
  the C++ compiler read before reusing a build; any change to RTL, includes,
  flags, DPI sources, or the toolchain rebuilds. Pre-Sim Commands that write
  only run-time inputs, such as firmware in `$BOOLEY_RUN_CWD`, keep reuse.
  Each result records a `cache_decision`. Cocotb Targets always build fresh.
- Tickets use document format v2: they declare `CRITERIA_MANDATORY` and
  `CRITERIA_OPTIONAL` with uppercase Flow keys (`LINT`, `SIM`, `SYNTH`,
  `REVIEW`, `CYCLE_COUNT`, ...). Target annotations `(new)`, `(temp)`, and
  `(replaces X)` replace `target_plan`. `on_success` is a list such as
  `[triage_report, review, merge, cleanup]`; leaving out `review` goes straight
  to done unless a human decision is pending: open findings from a `_done`
  REVIEW Criterion, or Waiver Candidates. Tickets that
  add Targets may also add new FuseSoC filesets and parameters used only by
  those Targets.
- `booley board show`, `review` (`--request --reason` asks for human review),
  `validate`, and `approve` replace the older review commands. `approve`
  publishes acceptance and finishes merge and cleanup without a report agent.
  `board show` and review packages compare recorded evidence with the live
  worktree and hold on stale mandatory evidence. Blocked Tickets can be amended
  after human approval with
  `python -m booley.ticket_board amend <slug> --changes-file F --preview`, then
  `--apply --expected-preview <digest>`.
- Live Tickets sit at `tickets/board/<slug>.md` with their state in an ignored
  `tickets/state/<slug>.json`. Done and archived Tickets close into the tracked
  `tickets/history/`, and Booley commits each history record.
  `booley board --all` includes closed Tickets. `booley board archive <slug>`
  abandons a live Ticket.
- `booley specialist <name> [args...]` runs a registered Specialist inside
  the Sandbox; listing and help work on the host.
- The Sandbox runs Ubuntu 26.04 with Python 3.14 (glibc 2.43, GCC 15).
  OpenROAD moves to 26Q4, from upstream's 26.04 build. Host-provisioned Vivado
  2025.2 runs on the new base.
- The RISC-V Sandbox Image moves Spike to upstream master `609dbe0b`, which
  adds `--wfi-as-nop` and fixes debug-module, trigger, and CSR behavior.
  Booley now refreshes Spike once per release from the newest master commit
  that passes upstream's Debug Quick Test.
- Sandbox Images are built in layers: a runtime base, a standard or RISC-V
  substrate, an optional Project layer, and a wheel overlay. Host Bootstrap
  builds the shared layers; Project Initialization builds only the Project's
  private ones. `booley session refresh` rebuilds only the invalid layers (a
  Booley-only upgrade rebuilds just the overlay) and checks the new image
  before parking the old Sandbox. RISC-V Projects with `pip_requirements` no
  longer need a hand-written Dockerfile.
- A persistent Project-scoped Verilator compiler cache
  (`[flows.sim.compiler_cache]`, on by default, 5 GB) is shared by Ticket
  worktrees and recreated Sandboxes. Verilator builds also get an automatic
  `-j` sized from available CPUs and memory; an authored `-j` wins.
- `[flows.sim].build_timeout_ms` (default one hour) bounds every
  simulator-image build, separately from simulation and Pre-Sim Command
  budgets.
- FPGA `ppa_profile` (`compact`, `balanced`, `max_frequency`) selects
  characterized Vivado 2025.2 strategies per call or per Target.
- `bwave gui --group 'NAME=GLOB'` creates collapsible VaporView groups.
  Selectors accept `%b`/`%h`/`%d` radix and `@red`/`@blue`/`@green` color
  suffixes.
- With Git 2.48 or newer on both the host and the Sandbox, and a Sandbox
  recreated after the upgrade, Ticket worktrees use relative links, so host
  `git status` and `git worktree list` work.
- `booley projects forget` releases the forgotten Project's Sandbox Image
  keeper tag when no container uses it. `booley projects prune-keepers`
  previews keepers left by earlier forgets, and `--confirm <digest>` releases
  exactly that preview.
- Stealth Mode can exempt a trusted upstream's pristine history from the push
  guard: set `[stealth] upstream_repository` and the full `upstream_base`
  commit ID. The guard checks that the base is reachable from the upstream's
  advertised branches or tags, then still checks every later local commit.
- `booley cleanup` previews and applies cleanup of setup scratch files that
  the setup manifest owns; `booley-setup` uses it as Step 7.
- Doctor and session startup report whether a deep Doctor run is current for
  the running Booley version and Sandbox Image (`docs/user/DOCTOR.md`).

### Quality of life

- "Sandbox" replaces "Session Runtime" in output, diagnostics, and docs.
  `booley cheat --sandbox` is the main flag; `--runtime` remains an alias.
- The Ticket Mode Console shows readable Criterion labels, configured
  requirements against observed evidence, short Target scope labels, the
  observed test scope, and each Developer Agent B-Wave query.
- Review briefings show readable Criterion labels and a Coverage section.
- `--target` can be repeated or comma-separated on every built-in and Custom
  Flow. Previously only the last value was kept.
- The direct Flow CLI always prints its final verdict, including inside the
  Sandbox, where it used to print nothing. Bare `booley flow` lists Flows and
  exits 0.
- Deduplicated lint warnings list the Targets and EDA tools that produced
  them.
- Simulation build timeouts and out-of-memory kills are reported reason-first,
  naming the limit and the setting to change.
- `booley-ticket-create` agent mode accepts `--input-file <path>` and reports
  progress milestones.
- `booley board reset` confirms the slug, the queued state, and whether
  baseline worktrees were restored.
- `bwave gui` and Doctor tell apart an installed, missing, and unknown
  VaporView extension and give install guidance, including offline VSIX
  installation.
- `booley-ticket-triage` opens diffs only for human-authored sources and skips
  compiled outputs such as `.hex` and `.mem`.
- The packaged `booley-feedback` skill ends with a sanitized offline report,
  manual GitHub and email submission options, and a verified workaround or
  an explicit blocker.
- Sandbox rebuilds reuse a matching local runtime base image instead of
  rebuilding it.
- README, `USAGE.md`, and `FLOW_REFERENCE.md` were rewritten as shorter,
  task-focused guides.
- The `bwave` MCP tool description steers agents to investigate with traces,
  not only confirm fixes: rerun `sim` with `trace: true`, follow the wrong
  value back to the first divergent signal, and confirm the mechanism before
  editing RTL.
- Simulation verdict cards show `cycles=N` for tests that record a Cycle
  Count, and `report.json` lists them under `cycle_counts`.
- A simulation run timeout suggests raising `--timeout-ms` or
  `[flows.sim].timeout_ms`.
- After an EDA grant revoke or `booley projects forget`, Sandbox commands say
  the Sandbox issuance was withdrawn and to run `booley init --seed`, instead
  of reporting a missing or corrupt spec stamp.
- The `booley-setup` skill never writes or guesses SDC or XDC timing
  constraints. It uses the file the repository ships or one you supply; until
  one exists, that synthesis or FPGA Target stays unconfigured.

### Bug fixes

- Missing EDA tools, missing inputs, timeouts, and abnormal termination in
  lint, synthesis, FPGA, and simulation are infrastructure errors (exit 2), not
  design failures. Reports add `termination` and `failure_kind`. Unexpected
  Flow and Specialist exceptions exit 2 with a one-line diagnosis and a saved
  traceback. Synthesis reports a design FAIL only on positive RTL evidence;
  other tool failures, such as an sv2v usage error, are errors that name the
  stage and its first diagnostic. A Vivado run that exits 0 without starting
  Tcl or producing fresh route evidence is an infrastructure error. Simulation
  guard aborts and failed builds keep a machine-readable aborted result.
- Simulation, lint, and Mutation Tester use the EDA tool FuseSoC actually
  configured. A missing or unknown tool no longer silently falls back to
  Verilator, which had built some Icarus Targets with Verilator. Lint Targets
  accept `verilator`, `verible`, or Edalize's `veriblelint`, and
  `booley targets --for lint` and Doctor leave out Targets whose lint tool is
  missing or unsupported.
- A source edit that keeps the same size and modification time no longer
  reuses a stale simulator build.
- Traced Icarus runs with projected cores no longer abort before simulation.
  A passing simulation whose requested trace is missing reports
  `inconclusive`, with the reason; Doctor WARNs `sim.trace-dump-undeclared`
  when a Verilator dump name is not covered by `trace_files`.
- Target-declared `file_type: user` and `copyto` inputs are available in the
  simulation run directory for Icarus, Verilator, and Cocotb.
- `progress.json` always ends in a terminal phase after failure or
  cancellation.
- Evidence links in reports, MCP results, and Criteria point to numbered,
  immutable per-run copies instead of mutable "latest" files.
- Logs use UTC RFC 3339 timestamps, fixing a double timezone shift.
- Cocotb, Pre-Sim Commands, Custom Flows, and MCP children no longer write
  `__pycache__` or `.pytest_cache` into the RTL checkout.
- Synthesis counts every latch, including stat rows with an area column and
  Liberty-mapped latch cells. Unexpected latches are a design FAIL naming the
  counts and cell types; declare intentional ones with
  `[flows.synth].expected_latches`, and those designs now complete physical
  synthesis. A missing, stale, or empty Yosys log blocks completion. Clean
  Liberty-mapped synthesis no longer gets false Yosys and OpenROAD advisories.
- Synth and FPGA recipe identity uses the constraint VLNV and content digest,
  so identical SDC/XDC files in another worktree no longer fail comparisons.
- A named-test `SIM` Criterion is satisfied by a passing run of that test; `all`
  still needs the whole suite.
- Each `COVERAGE` Criterion takes its verdict from its own metric, so an unmet
  optional metric no longer fails mandatory metrics on the same Target.
- Ticket intake, readiness, and execution validate the same prepared checkout,
  so valid Tickets are no longer rejected at publish or enqueue, and
  generated ignored inputs are no longer reported as protected-input drift.
- `booley board create` works again; it previously always failed.
- `booley board approve` completes a Ticket that `booley run` accepted and
  handed to review, ending a loop where approve and review pointed at each
  other.
- A commit after review acceptance is reported as stale acceptance, with the
  frozen and live heads and the recovery commands, instead of as corruption.
- Tickets whose authored content drifted are reported, and `return-to-draft`
  carries the current content into a fresh draft. Basis Refresh no longer
  corrupts v2 Ticket bodies.
- `booley run` prints one machine-readable result for every Ticket ending:
  done returns success, blocked and failed return nonzero.
- Ticket completion succeeds with unrelated untracked Project files present and
  maps Ticket Board paths across bind-mount aliases.
- Smaller Ticket fixes: a queued Ticket with a missing workspace restarts on
  its generation branch; enqueue inside the Sandbox handles both Project
  mount paths; Reviewer receipts no longer go stale without
  `answered_questions.md`; the paired-layout Reviewer loads the control
  Project's Board; generated core projections and paired Basis checkouts no
  longer fail drift or pristine checks; a fresh blocked dossier is no longer
  rejected as stale; acceptance or report-publication failures return a
  structured exit-2 report; a dead CLI Flow releases its job slot.
- A Developer Agent's declared blocked reason stays the primary reason, also
  on Tickets with no mandatory Criteria; later guard findings are recorded as
  secondary. A failed run-report submission no longer counts as submitted.
- A `_done` REVIEW Criterion with open findings no longer blocks the Ticket as
  an unmeetable gate. The review completes and the Ticket waits for
  `booley board approve` with the findings visible, even when `on_success`
  has no `review`.
- The Reviewer honors valid explicit dispositions and no longer drops findings
  by phrase matching; `rtl/code_style` reviews include the packaged RTL style
  guide.
- One malformed acceptance journal no longer aborts the whole Ticket Board
  scan.
- The Ticket Mode Console keeps showing jobs after a cancelled or duplicated
  run. Console and worker crashes save full tracebacks, and auto-retry prints
  the command to resume.
- `max_sessions` in the host `config.toml` refuses new Sandboxes at the cap
  (exit 2) instead of the reaper stopping active ones. The idle reaper no
  longer stops a Sandbox while a supervised run is active.
- Host Bootstrap, Project Initialization, and Sandbox commands wait a bounded
  time for the shared Docker lifecycle lock, then exit 2 with one clean error.
- Drift and mismatch diagnostics name each differing field with its recorded and
  current values. Another Project rebuilding a shared image from the same
  sources no longer triggers false stale-image warnings.
- `booley init` creates a standalone Project-data repository on the outer
  repository's current branch instead of Git's default branch, reports unsafe
  line-ending repairs as errors, and `--check-only` reports exactly what it
  would reconcile. On Windows it now normalizes guidance files behind
  Booley's own hardlinks instead of refusing.
- Bootstrap and Init remove obsolete Booley image tags after verifying their
  replacements.
- Unsafe private-directory permissions are reported with the path and a
  `chmod 700` fix instead of a crash.
- `booley doctor --deep` grades its simulation self-test correctly, cleans up
  after itself, and prints a `RUN` line with the timeout before each long
  check. Doctor no longer crashes without a container runtime.
- Windows and Docker Desktop: no more WinError 5 crashes in the auth heartbeat
  and line-ending cleanup, WSL2 mounts are accepted, `session refresh` rollback
  tolerates a changed egress network, and atomic record writes retry
  transient sharing violations.
- The in-container MCP server always starts in Interactive Mode.
- A Codex usage cap, workspace spend cap, or depleted credits now fails the
  call instead of being reported as success when progress output came first.
  Queue the blocked Ticket again after resolving the cap.
- `[agent.git]` identity applies to Interactive Mode commits through
  Sandbox-only Git settings and leaves the host checkout's identity alone.
  Approve coverage waivers on the host with your own identity; the Sandbox
  refuses.
- B-Wave no longer rewrites `trace_status.json` during discovery, and a broken
  cached FST no longer blocks VCD conversion. The native B-Wave binary's hard
  link survives Docker's containerd image store. The VaporView patch applies
  when the extension is installed after the container starts.
- Review packages accept Reviewer findings with advisory, deferred,
  out-of-scope, or superseded dispositions instead of rejecting them, and
  reports show the Reviewer's original label next to the package disposition.
- `validate-ticket` prepares published Tickets with the Project's post-setup
  hook before checking Target inputs, so hook-generated inputs such as
  firmware images no longer fail validation.
- Commands run from a Stealth Project's data directory (`/booley-project` or
  `/work/.booley_project`) act on the owning Project checkout instead of
  treating the data repository as the Project; discovery, automatic Doctor,
  and report path hints treat both paths as one directory. A Git failure
  during an ancestry check exits 2 with "cannot verify ancestry" instead of a
  false "no longer descends" error or a crash.
- `booley eda …` with an explicit Project path, `booley auth status`, and
  `booley cheat` work from inside the Booley source checkout.
- `--baseline` synthesis, FPGA, and simulation runs work when
  `.booley_project` is a standalone Git repository.
- With Stealth on, `booley init` keeps its line-ending default in the
  repository's local `info/attributes` instead of leaving an untracked root
  `.gitattributes` in the upstream checkout.
- Stealth Mode catches protected terms inside filenames and identifiers and
  redacts only the matched span. Ordinary words such as `generated`, `docker`,
  and `agent` are no longer redacted from commit messages.
- A simulation that overruns `max_rundir_bytes` and exits before the next
  watchdog poll is a disk-budget abort, not a pass.
- Flow and Specialist reports set `criterion_key` and `criterion_met` only
  when the run maps to exactly one evaluated Criterion, and JSON null
  otherwise. A passing standalone synthesis or lint run used to report
  `criterion_met: false`.
- B-Wave: `stats` and `stuck` include the reset phase with `--with-reset`,
  count the value held at the window start, and no longer report zero-length
  values; `find` and `distance` match the `change` keyword on buses; exact
  alias selections are kept; `distance --stats` reports the median; limits
  and time windows apply as documented, and truncation is reported.
- Codex runs no longer start with an "Under-development features enabled"
  error item; Booley suppresses that warning unless you set it yourself.
- FlexNet license relay startup waits for the relay's full Docker
  health-check window (about 70 seconds instead of 12), fails at once if the
  relay exits or turns unhealthy, and reports the container state and the
  last relay log lines.
- Plain Doctor starts Vivado in batch Tcl mode inside the Sandbox (about 30
  seconds) instead of only finding `vivado` on PATH, so a Vivado that cannot
  initialize fails Doctor. `booley doctor --deep` no longer tells you to run
  `booley doctor --deep`.

### Internal work

- Architecture separation campaign: Config, EDA, Runtime, Flows, Criteria,
  Ticket Board, Feedback, and commit policy now have enforced dependency rules,
  with no intended behavior change.
- The QA scenario format (YAML scenarios, sealed run records, and
  Qualification) was designed and then replaced by the mission-based QA
  workflows before release.
- CI: Windows test sharding, a separate coverage job, candidate-image gates
  for Verilator 5.052 and coverage, the encrypted confidential-content guard,
  the Mergify queue with a PR watcher, and the Agent Readiness Check. RISC-V
  image-build timing is measured in controlled baseline, warm, and cold arms
  on the same Docker image store, so their timings are comparable. The RISC-V
  toolchain, Spike, and offline specifications build in a separate keyed
  stage that CI reuses from a published image.
- The runtime base image embeds a deterministic package inventory.
- Toolchain refresh: Yosys v0.69, OpenROAD 26Q4, Verible v0.0-4296, Node.js
  24.21.0, Claude Code 2.1.285, Codex 0.160.0, Rust 1.99.0, Docker CLI
  29.8.2, a refreshed Ubuntu 26.04 base, and Python 3.14.8 sidecars (egress
  proxy, FlexNet relay, reaper), which fixes ssl and asyncio hostname
  validation.
- Goal Mode, a planned replacement for Ticket Mode, and the Booley Dashboard
  were designed (ADRs 0067 and 0068); neither ships in this release.

### Upgrade notes

- Run `booley bootstrap --update` after upgrading Booley, then `booley init`
  and `booley session refresh` in each Project. The Sandbox moves to Ubuntu
  26.04 and Python 3.14, so Project-derived images and pip installs must work
  there. Until it is refreshed, an older Sandbox builds without the compiler
  cache and prints a warning. Host Bootstrap owns the shared runtime base:
  `booley init` no longer builds it and asks for `booley bootstrap --update`
  when it is missing or stale. Each Project gets a private Sandbox Image
  (`<project>-booley-sandbox-<digest>`). A hand-written
  `.booley_project/docker/Dockerfile` whose `FROM` matches the selected image
  is built on top of Booley's image; a mismatched `FROM` is reported and left
  unused. Host Bootstrap checks free disk for the whole build sequence before
  starting: about 40 GiB for the standard image, 47 GiB for RISC-V.
  `BOOLEY_SKIP_IMAGE_DISK_PREFLIGHT=1` skips the check.
- Booley records one canonical host installation and refuses to manage global
  skills from virtual environments or source checkouts. Install with
  `python3 -m pip install --user booley-rtl` rather than pipx, then run
  `booley bootstrap`.
- Rerun `booley init` before the first direct Flow after upgrading. It adds
  the `flow-reports/`, `/logs/`, and `/.baseline-wt-*/` ignore rules, sets the
  relative-worktree policy, and replaces the hook files in
  `.booley_project/hooks/` with
  `.booley_project/.managed/project-git-hooks.pyz`.
  `booley init` and the next Ticket activation repair existing worktree links
  once they prove which repository and Ticket own them; repair waits while an
  older Sandbox is still running.
- Rename `[interactive]` to `[sandbox]` in the host `config.toml`, keeping
  every setting. A legacy `[interactive]` table still loads with a migration
  warning; when both tables exist, `[sandbox]` supplies the whole policy
  without merging. Host-only keys (`idle_timeout_seconds`, `max_sessions`,
  `egress_allowlist`) in a Project's `booley.toml [sandbox]` are rejected with
  migration guidance.
- Ticket Boards from earlier versions need a one-time manual migration. Until
  then Doctor FAILs and `booley board` and `booley run` refuse to start. Follow
  [the Troubleshooting entry](https://github.com/boldaxolotl/Booley/blob/v0.3.0/docs/user/TROUBLESHOOTING.md#booley-board-refuses-to-start-the-ticket-board-needs-migrating).
  `booley board` lists live Tickets only; add `--all` for closed ones. Bare
  `booley board archive` only resumes an interrupted archive. `--force` and
  `--keep-logs` have no effect. Closed Tickets cannot be reopened, and their
  slugs cannot be reused. When the history commit cannot be made, the Ticket
  still closes and the commit is retried later. In the Project repository,
  Stealth Mode redacts banned phrases from that commit message.
- Old-format Tickets and Tickets carrying an `acceptance_basis` are rejected,
  so recreate draft and queued Tickets in the v2 format; the Ticket's
  `machine` frontmatter is now the only acceptance record. `REVIEW`
  declarations need exactly one scalar `done` or `clean` outcome.
  `booley board check-ready <slug>` finds invalid Tickets. `request-review`,
  `refresh-review`, `prepare-review`, `review-briefing`, `blocked-briefing`,
  and `finalize-review` are deprecated aliases. `board move` can no longer
  enter `review` or `done`.
- Ticket execution always uses the full-screen Console. `--no-console`,
  `-L`, and `BOOLEY_CONSOLE` are removed, and a Console startup failure exits
  1.
- `submit_run_report` requires `file_justifications` for every path in the
  final diff. Edits outside the planned scope are no longer refused; they are
  shown as scope deviations for review. Update Project-local agents and prompts.
- Simulation test selection is exact: repeat `--test <name>` or pass
  `--tests-file <path>`. The per-run `--skip` option is removed; `tests.toml`
  skips apply only to unfiltered runs. MCP `test` is now a nonempty array.
  Save the printed `campaign/manifest.json` path to pass to `--resume-from`;
  Booley does not pick the latest Campaign, and the manifest fixes Target,
  tests, mode, coverage, trace, and waiver choices. Unsupported or corrupt
  manifests fail closed.
- The default `pre_sim_build_access = "immutable"` hides the build path from
  Pre-Sim Commands; select `"legacy-per-test"` only when a hook must modify
  the private compile surface. Pre-Sim Commands have their own fixed
  10-minute budget. `--timeout-ms` and `[flows.sim].timeout_ms` no longer
  lengthen simulator-image builds; set `build_timeout_ms` if builds need more
  than an hour. A literal `run_cwd` must exist and be tracked before the build
  (Doctor WARNs `sim.run-cwd-missing` and `sim.run-cwd-untracked`).
  `run_cwd` accepts `{campaign}`, `{target}`, `{test}`, and `{attempt}`.
  Setting `OBJCACHE`, `CCACHE_*`, or other managed Make variables in
  `make_options` or `MAKEFLAGS` is rejected. A Target whose FuseSoC tool is
  missing, unknown, or different from its declared family now fails before
  the build.
- Per-Target `sim_<target>.json`, `coverage_report.json`, and mutable
  `coverage_waivers.json` are gone; read
  `sim/<N>/targets/<target>/{coverage.json,simulation.json}`. Coverage
  Campaigns from before schema v3 are rejected and must be recollected.
  `coverage_analyst` takes `--campaign <coverage.json>`. Formal proof artifacts
  placed at `<anchor>/proofs/...` must move to
  `<anchor>/<approval-directory>/proofs/...`; keep the authored proof
  reference. Invalid or unmatched waiver approvals block coverage runs with
  exit 2; fix them or pass `--no-waivers`. With a Coverage Criterion,
  `--no-waivers` requires `--diagnostic`.
- Synthesis fails on any Yosys combinational-loop warning, including
  intermediate `check` passes. The Reviewer no longer discards in-scope
  findings, so some `REVIEW` Criteria that passed may now be unmet.
- Synthesis recipe schema 3 and OpenROAD 26Q4 change PPA for every physical
  profile and invalidate fingerprints frozen at Ticket intake. Run
  `booley board reset <slug> --reason "refresh synthesis recipe schema 3"` and
  rerun affected baseline-comparison Tickets. Custom Liberty files must be
  accepted by Yosys `read_liberty -lib`. FPGA recipe schema 2 and Vivado cache
  schema 3 invalidate earlier cached FPGA results.
- Specialist MCP calls reject `model`, `max_turns`, and timeouts; use the CLI.
  `--timeout` is replaced by `--timeout-ms`: multiply old Specialist seconds
  by 1000. Project Specialists read `args.timeout_ms` and use
  `self.timeout_seconds()`.
- Rename Specialist sections in `booley.toml` from `[mcp_tools.<name>]` to
  `[specialists.<name>]`; `[mcp_tools]` and the older `[tools]` table now
  produce migration errors. Delete settings for protocol endpoints such as
  `submit_run_report`. To hide a custom `McpTool`, prefix its filename with
  `_`.
- Explicit lint Targets must name `verilator`, `verible`, or `veriblelint` in
  `flow_options.tool`; `default_tool` alone is rejected.
- Stealth Mode's built-in vocabulary is now `claude`, `anthropic`, `copilot`,
  `codex`, `openai`, `chatgpt`, `gemini`, `booley`, `cursor`, `ticket`, `gpt`,
  `llm`, and `co-authored-by`, and these terms now match inside identifiers,
  so pushes that passed before may be blocked. Add literal terms with the new
  `[stealth] banned_substrings`. The push guard checks imported upstream
  history unless `upstream_repository` and `upstream_base` are set. Run
  Project Setup or `booley doctor` to refresh the installed Git hooks.
- Scripts reading a Flow report's `criterion_met` must handle JSON null; the
  simulation report schema is now `booley.simulation-report/v3`.
- B-Wave query output defaults to 2,000 rows (was 5,000) and `list` to 400,
  and explicit limits above 10,000 are capped with a notice. `--format json`
  on a command without JSON output, and `distance -s`, exit 2 instead of being
  ignored.
- `booley feedback preview` and `submit` are removed and `[feedback].mode` is
  retired; Doctor FAILs until you delete the key. Only `booley feedback export`
  remains. ntfy notifications are removed; delete `[notifications]`, and
  remove `ntfy.sh` from the host `egress_allowlist` if it was authorized only
  for notifications. Preserve other authorized entries. Stop Project
  containers, run `booley bootstrap` on the host, and restart them to apply
  the policy change. `pipeline.toml` is no longer a Git identity fallback; set `[agent.git]`
  in `booley.toml`.
- Codex default models are `gpt-6-astra` (heavy), `gpt-5.6-sol` (standard),
  and `gpt-5.6-luna` (light), all at high reasoning effort. Doctor's
  `interactive.logs-gitignore` check is now `project.gitignore`; re-waive it
  under the new id. Stealth Mode's commit hook rejects attribution footers
  instead of silently removing them.
- During the upgrade review, replace the "Keep `booley doctor` green" bullet in
  `<project_dir>/AGENTS.md` (and any tracked root copy) with the "Doctor during
  task work" bullet from the packaged `booley-setup/AGENTS_TEMPLATE.md`, and
  refresh its Specialists bullet to list `coverage_analyst`.

## 0.2.15 - 08 SEP 2026

### New features

- Every built-in Flow now returns the same versioned `FlowPlan` from a dry run.
  The plan records each Target or baseline work unit, timeout, resolved inputs,
  recipe, command, expected artifacts, and planning errors without running EDA
  or changing durable Project state. When `--report-dir` is set, the dry run
  writes only `<report-dir>/<flow>/flow_plan.json`.
  ([PR #398](https://github.com/boldaxolotl/booley/pull/398),
  [PR #406](https://github.com/boldaxolotl/booley/pull/406))
- Simulation now uses one explicit mode selector for ordinary simulation,
  elaboration-only checks, and elaboration plus standalone module checks. The
  CLI values are `simulate`, `elab-only`, and `elab-only-standalone`.
  ([PR #400](https://github.com/boldaxolotl/booley/pull/400),
  [PR #406](https://github.com/boldaxolotl/booley/pull/406))

### Quality of life

- Built-in Flow timeouts now use one positive `timeout_ms` contract across
  configuration, CLI, and MCP calls. Each work unit gets the full active-time
  budget; time waiting for a job slot is excluded.
  ([PR #398](https://github.com/boldaxolotl/booley/pull/398))
- Reviewer and Mutation Tester calls now share required scope, repeatable
  steering, and non-persisting dry-run inputs. Reviewer calls are source-scoped
  and targetless. Mutation calls take one Target and derive the testbench and
  RTL closure from it.
  ([PR #405](https://github.com/boldaxolotl/booley/pull/405))
- The Session Runtime now uses Verilator v5.052 at source commit
  `ea338be98e1e838d3518809ce8899f85a009963c`. The release passed the compiler,
  Cocotb, waveform, diagnostic, and native coverage compatibility matrix.
  ([PR #233](https://github.com/boldaxolotl/booley/pull/233),
  [#153](https://github.com/boldaxolotl/booley/issues/153))

### Bug fixes

- Doctor keeps its known-good and known-bad Simulation overlays in separate,
  freshly reset build variants. Stale timestamps or a cached good executable
  can no longer make the deliberate failure probe pass.
  ([PR #397](https://github.com/boldaxolotl/booley/pull/397))
- Target validation distinguishes executable inputs from simulator option
  values. Valid settings such as the Verilator timescale `1ns/1ns` no longer
  appear as missing programs, while missing executables still fail strictly.
  ([PR #399](https://github.com/boldaxolotl/booley/pull/399))
- Acceptance Basis validation reconstructs Booley-generated core projections
  from the accepted commit. Setup, Flow entry, resume, and final handoff accept
  unchanged generated files and still reject altered or externally routed
  projections. ([PR #410](https://github.com/boldaxolotl/booley/pull/410))
- Returning a Ticket to draft now preflights standalone submodules before
  moving worktrees, recovers interruptions after the filesystem move, and
  reports the deinitialization command for unsupported native Git submodules.
  ([PR #412](https://github.com/boldaxolotl/booley/pull/412))

### Upgrade notes

- Replace `--timeout` with `--timeout-ms`. The old CLI spelling remains a
  deprecated alias for one compatibility window. Configuration and MCP calls
  use `timeout_ms`.
- Replace Simulation's agent-facing `elab_only` and `standalone` booleans with
  `mode: simulate`, `mode: elab_only`, or `mode: elab_only_standalone`. The CLI
  accepts `--mode simulate`, `--mode elab-only`, or
  `--mode elab-only-standalone`; `--elab-only`, `--build-only`, and
  `--standalone` remain deprecated CLI aliases for one compatibility window.
- Physical synthesis Targets must own an SDC fileset that creates a clock.
  Booley no longer accepts a per-run default clock or generates a timing
  constraint. Logical synthesis does not require or consume SDC.
- Reviewer callers must pass `--scope`; standalone specification reviews use
  `--spec` instead of `--ticket`. Remove `--diff-ref` and Reviewer `--target`.
  Mutation callers must select one Target instead of supplying DUT or testbench
  topology separately.
- When Acceptance Basis inputs must change, run
  `python -m booley.ticket_board return-to-draft <slug>`. Booley archives the previous run and
  starts a new authoring generation. Deinitialize native Git submodules first
  if the command reports them.
- After upgrading, run `booley bootstrap`. Refresh a headless runtime with
  `booley session refresh`, or use **Dev Containers: Rebuild Container** for a
  VS Code runtime so the Verilator v5.052 image is installed.

[Full changes from v0.2.14](https://github.com/boldaxolotl/booley/compare/v0.2.14...v0.2.15)

## 0.2.14 - 07 SEP 2026

### Bug fixes

- Python release publication now grants reusable source validation permission
  to inspect workflow artifacts, allowing exact-tag validation to start before
  PyPI upload and GitHub Release creation. This patch supersedes v0.2.13, whose
  tested container images published successfully but whose Python publication
  workflow was rejected before any job started.

[Full changes from v0.2.13](https://github.com/boldaxolotl/booley/compare/v0.2.13...v0.2.14)

## 0.2.13 - 07 SEP 2026

### New features

- Ticket Mode now publishes an immutable Acceptance Basis automatically when a
  Ticket is enqueued. The basis records the authored inputs, repository
  identities, Criteria bindings, and Target dispositions used during execution
  and final acceptance. The Acceptance Journal protects the exact source,
  prepared result, finalized result, destination, and cleanup state across
  retries. Returning a blocked Ticket to draft preserves the old basis and
  evidence while starting a new authoring generation.
  ([PR #378](https://github.com/boldaxolotl/booley/pull/378),
  [PR #361](https://github.com/boldaxolotl/booley/pull/361))

### Quality of life

- The standard Runtime Image is 71% smaller by visible filesystem size;
  the RISC-V image is 66% smaller. Both retain the supported EDA and agent
  toolchains, OpenROAD source provenance, and SPDX SBOM attestations. The demo
  stack needs about 6 GB of Docker storage, plus Project artifacts and temporary
  build or upgrade data.
  ([PR #350](https://github.com/boldaxolotl/booley/pull/350))
- Doctor reuses one FuseSoC library view when auditing equivalent Targets.
  `booley doctor --concise` hides PASS rows without changing findings, counts,
  evidence, or exit status. A focused 100-Target source inspection fell from
  7.32 seconds to 0.15 seconds.
  ([#352](https://github.com/boldaxolotl/booley/issues/352))
- Simulation results now record millisecond build and run durations plus setup,
  pre-run, result-processing, publication, and unattributed phases. Supported
  platforms also report peak RSS, OOM-kill deltas, and simulator child CPU time
  for diagnosing Windows Docker performance failures.
  ([#356](https://github.com/boldaxolotl/booley/issues/356))
- Host bootstrap now installs and verifies VS Code's Dev Containers extension.
  PicoRV32 guidance explains the automatic reopen popup and how to open the same
  command through the Command Palette (`F1` or `Ctrl+Shift+P`).
  ([PR #353](https://github.com/boldaxolotl/booley/pull/353))
- The Session Runtime now includes Claude Code 2.1.263, Codex CLI 0.153.4, and
  NumPy 2.5.3. Release checks use PyYAML 6.0.3, and the reaper and isolated
  evidence service use Docker 29.8.0.
  ([PR #394](https://github.com/boldaxolotl/booley/pull/394))
- The ticket-creation agent writes only the Ticket, approved new Target
  definitions, and zero-byte placeholders for new scope paths. Mutation
  creators may inspect RTL but may not modify files, run Project tools or
  simulators, or inspect testbenches. Reviewer prompts tell developers to omit
  `--target` for target-independent Criteria.
  ([PR #360](https://github.com/boldaxolotl/booley/pull/360),
  [PR #338](https://github.com/boldaxolotl/booley/pull/338),
  [PR #339](https://github.com/boldaxolotl/booley/pull/339))

### Bug fixes

- Synthesis reports now include normalized EDA warning totals, categories,
  dispositions, and representative diagnostics with a pointer to the full log.
  A final Yosys structural pass rejects combinational loops and multi-driven
  nets even when mapping and optimization otherwise complete; actionable
  warnings produce WARN without turning a structurally valid result into a
  failure. ([#344](https://github.com/boldaxolotl/booley/issues/344))
- B-Wave `sample` now applies `--first`, `--last`, `--before`, and `--after` to
  the selected trigger event while preserving time windows, `--count`, reset,
  sync/async, and stored or Virtual Signal behavior.
  ([#317](https://github.com/boldaxolotl/booley/issues/317))
- B-Wave now applies `-s` consistently to stored and Virtual Signal rows in
  `wave`, `value`, and `sample`. Unselected helpers remain available for
  composition without appearing in output, and exact stored or duplicate
  Virtual Signal name collisions fail before results are emitted.
  ([#318](https://github.com/boldaxolotl/booley/issues/318))
- Native B-Wave accepts and renders `--marker` only for `wave`; other commands
  reject it with exit status 2. Stored marker cycles and async marker columns
  now survive wrapper substitution and run-length encoding correctly.
  ([#319](https://github.com/boldaxolotl/booley/issues/319))
- First-time `booley init` in a checkout nested below another Project now keeps
  its agent selection and configuration in that checkout. The configuration
  guide distinguishes Booley's Icarus trace overlay from project-owned
  Verilator trace sources.
  ([#342](https://github.com/boldaxolotl/booley/issues/342))
- Parallel `booley session enter` commands now wait for the host lifecycle lock
  and release it before supervised commands run. Concurrent read-only B-Wave
  queries no longer fail with "host Docker lifecycle is busy".
  ([#345](https://github.com/boldaxolotl/booley/issues/345))
- Sidecar upgrade blockers now identify the owning Project and include a
  Project-scoped shutdown command. After an image upgrade, `booley init`
  reconciles a stopped, ownership-verified headless Session Runtime instead of
  reporting the Project ready with stale issuance.
  ([#352](https://github.com/boldaxolotl/booley/issues/352))
- Simulation adapters resolve registries, selectors, skips, environments,
  pre-sim commands, and artifact freshness from each Target checkout, so
  baseline worktrees cannot inherit active-Project cache state. Adapter results
  use attempt-bound atomic transport with explicit failure precedence.
  ([PR #359](https://github.com/boldaxolotl/booley/pull/359))
- The Stealth pre-push guard now rejects tracked `.booley_project/` paths and
  committed links into that state even when the Session Runtime exposes it at a
  different absolute path.
  ([PR #363](https://github.com/boldaxolotl/booley/pull/363))

### Upgrade notes

- After upgrading, run `booley bootstrap`. Refresh a headless runtime with
  `booley session refresh`, or use **Dev Containers: Rebuild Container** for a
  VS Code runtime. Run `booley init` in each existing Project so its vendored
  pre-push hook receives the corrected project-state guard.
- Booley rejects legacy Target Contract tickets. Recreate them with the current
  Ticket workflow. Enqueue now publishes the Acceptance Basis without a separate
  seal step; use `python -m booley.ticket_board return-to-draft <slug>` when a blocked Ticket
  needs different authored inputs.

[Full changes from v0.2.12](https://github.com/boldaxolotl/booley/compare/v0.2.12...v0.2.13)

## 0.2.12 - 04 SEP 2026

### New features

- B-Wave's `--virtual` option now works across the documented five-command
  matrix. Virtual Signals support fail-fast ordered resolution, `sample`
  trigger and capture expressions, and `value` point evaluation. The CLI,
  public docs, and Coverage Analyst guidance now describe the same behavior.
  ([#320](https://github.com/boldaxolotl/booley/issues/320))

### Quality of life

- `booley auth` now mints Claude credentials inside the validated Session
  Runtime while credential storage and runtime-spec reseeding remain on the
  host. It safely reuses running runtimes and recovers interrupted refreshes.
- Doctor now probes the live MCP catalog through a supported entry point and
  reconciles positively identified stopped VS Code Session Runtimes. Deep
  Doctor resolves only its selected Targets, avoiding unnecessary work across
  large vendored Target matrices.
- Project initialization and Doctor now share a typed, guarded line-ending
  reconciliation path. Repairs revalidate the worktree, index, attributes, and
  Git configuration before changing files, preserving unrelated staged work.
  ([#259](https://github.com/boldaxolotl/booley/issues/259))
- The Session Runtime now includes Claude Code 2.1.259 and Codex CLI 0.153.1.
  Development checks use Ruff 0.16.6.
- Installation guidance now distinguishes first-time setup from upgrades.

### Bug fixes

- Concurrent synthesis runs can no longer delete a shared Target workspace
  while another run is using it. Workspace leases remain held through report
  snapshotting, and timeouts or release failures are reported as infrastructure
  errors.
- Acceptance Journal recovery now tracks and reconciles exact prepared and
  finalized commit identities, preserves finalized commits across interrupted
  cleanup, and safely handles concurrent or symbolic ref movement.
  ([#257](https://github.com/boldaxolotl/booley/issues/257))
- Cycle Count grading preserves both the durable Target identity and callable
  selector across current and baseline evidence, and rejects ambiguous or
  identity-drifting baseline resolution.
  ([#267](https://github.com/boldaxolotl/booley/issues/267))
- Testbench reviews bind their selected simulation Target before inspecting
  candidates, so unrelated or ambiguous Targets cannot redirect the review.
  ([#268](https://github.com/boldaxolotl/booley/issues/268))
- Doctor runs simulation probes from the correct Project directory and opens
  generated contracts transactionally, leaving failed attempts retryable.
  ([#269](https://github.com/boldaxolotl/booley/issues/269))
- Doctor's simulation fail-path check now applies bad firmware and vector
  overlays through an isolated runtime view, preventing configured run
  directories from masking the deliberate failure or replacing real inputs.
- Doctor now recognizes tests generated by cocotb 2 `@cocotb.parametrize`,
  treating rendered IDs as statically unverifiable while retaining warnings
  for ordinary misspelled test names.
- Successful cocotb trace conversion removes simulator-owned raw VCD files,
  including files emitted outside the managed work directory. Failed
  conversions retain raw traces and diagnostics in the simulation work area.
- Simulation elaboration-only checks now classify missing build executables as
  EDA tool errors while preserving report metadata and leaving the design
  verdict unset.
- Windows initialization again recognizes trusted installed console launchers
  after path normalization without accepting Project-controlled executables.
  ([#313](https://github.com/boldaxolotl/booley/issues/313))
- Managed Project Images now force PEP 517 isolation when installing pinned
  dependencies, restoring packages with legacy source distributions such as
  `cocotb-test` on the Python 3.13 sandbox.
- Native B-Wave metadata now remains correct for both single-root and
  multi-root traces. ([#266](https://github.com/boldaxolotl/booley/issues/266))

### Upgrade notes

- No configuration migration is required. After upgrading, run
  `booley bootstrap`, then use `booley session refresh` for a headless runtime
  or **Dev Containers: Rebuild Container** for a VS Code runtime.

[Full changes from v0.2.11](https://github.com/boldaxolotl/booley/compare/v0.2.11...v0.2.12)

## 0.2.11 - 02 SEP 2026

### Quality of life

- Added the host-owned Project Inventory and `booley projects` command, with
  shared discovery, status, access-grant, and JSON views. Help text now marks
  commands by host or Session Runtime.
- FPGA setup now exposes target-aware Doctor probes and dry-run checks across
  the CLI and MCP surfaces, including clearer Vivado and board-target guidance.
- Updated the Session Runtime to Verible v0.0-4163-g6cce8f19, Claude Code
  2.1.258, and Codex CLI 0.152.1, and refreshed the immutable Python and Docker
  CLI bases used by sidecars.
- Version-change warnings are more prominent during startup, and the demo
  guidance now links directly to feedback.

### Bug fixes

- Host bootstrap now requires Git 2.37.2 or newer, avoiding a Git for Windows
  temporary-name exhaustion failure during large line-ending repairs.
- Host bootstrap now secures the shared Booley configuration directory before
  preparing the PDK cache, so existing caches cannot block Session Runtime
  issuance during an upgrade.
- Flow entry points now validate Target compatibility and identity before EDA
  setup, and implementation comparison evidence retains durable Target
  identities instead of relying on checkout-local objects.
- Schema-4 Cycle Count criteria now join simulation and baseline evidence by
  durable Target identity while retaining the callable selector. This closes a
  later uncovered Simulation consumer of the Target representation fixed in
  [#131](https://github.com/boldaxolotl/booley/issues/131). ([#267](https://github.com/boldaxolotl/booley/issues/267))
- Session Runtime issuance now finds trusted Booley executables installed in
  Python's per-user scripts directory even when that directory is absent from
  `PATH`.
- Skill reconciliation now always deploys packaged skills to `.agents/skills`
  for Codex, while continuing to deploy to a distinct existing `.claude/skills`
  directory.
- `booley session refresh` now journals replacement checkpoints and recovers
  safely after interruption. It parks the target Session Runtime, reconciles
  dependencies, commits forward only after verifying the replacement, and
  retains recoverable state when cleanup cannot finish.
- Source checkouts no longer acquire Project or Stealth policy accidentally;
  repository classification, hook installation, and managed-state placement
  now preserve the source-checkout boundary.
- Runtime Image provenance is now scoped to the image that actually runs the
  Project, avoiding stale rebuild prompts from unrelated images.
- Stealth projects now keep core projections and FPGA target metadata within
  their protected project state.

### Upgrade notes

- Percentage-based acceptance-criterion values must include an explicit `%`
  suffix, for example `cycle_count_reduce_at_least: 5%`.
- Custom MCP clients must negotiate protocol version `2026-07-28`. Booley's
  built-in Claude Code and Codex configurations are updated automatically.
- Host bootstrap now requires Git 2.37.2 or newer. The complete demo stack
  requires about 6 GB of free Docker storage, plus Project artifacts and
  temporary upgrade/build data.

## 0.2.10 - 01 SEP 2026

### New features

- Bare `booley` now opens the Project's configured Claude Code or Codex chat;
  `booley chat` is the explicit equivalent and `booley --help` remains the
  command reference.
- Added Project-independent `booley bootstrap` for host prerequisites, skills,
  the shared PDK cache, the base Runtime Image, and global proxy and reaper
  services. `booley init` performs the same reconciliation when needed.
- Added durable, version-aware upgrade review state with scriptable status and
  compare-and-swap acknowledgment. Doctor and Session Runtime startup identify
  pending or stale reviews; `/booley-heal` reviews the exact packaged changelog
  range and acknowledges it only after verification.

### Quality of life

- Updated the Session Runtime to Node.js 24.20.0 and Claude Code 2.1.252.
- Updated the egress proxy, FlexNet relay, and reaper sidecars to Python 3.14.7,
  with tests for CONNECT streaming, relay forwarding, owned-container cleanup,
  unavailable-daemon handling, and container hardening.
- Replaced the unavailable historical OpenROAD package with the official 26Q3
  OCI channel at an immutable digest. Logical and physical synthesis matched
  the 0.2.9 area, utilization, cell count, and netlist results.
- The RISC-V image now pins Spike to a validated upstream snapshot and runs its
  upstream test suite during the build. The PicoRV32 release smoke now tests
  the project's public `main` commit rather than a private CI-only branch.
- Documentation now distinguishes simulation, lint, synthesis, and FPGA
  implementation as separate Booley Flows. It also describes the current stock
  VS Code interface and agent-written development process, and expands the
  Ticket Mode and CI roadmap.

### Bug fixes

- Claude Code and Codex now install their required Linux/x64 native artifacts
  explicitly so optional-package failures cannot leave unusable launchers.
- Updated to cocotb 2.1.0 and its Icarus GPI contract while retaining cocotb
  1.x and 2.0 compatibility. Production-image Icarus and Verilator cocotb flows
  now run in CI.
- Confidential-content pre-push checks now validate destination refs and scan
  changed tree entries. They still cover newly exposed history, merges,
  renames, deletions, and malformed input.
- Runtime-image builds now use bounded pip download timeouts and retries.

### Upgrade notes

- Run `booley bootstrap` once after upgrading. Existing Project
  `booley.toml [interactive]` host-policy fields are retired; move them to the
  host policy file named by `booley init` or `booley doctor`.
- When Booley reports a version change, invoke `/booley-heal` to review these
  notes, repair drift, and acknowledge the upgrade.

## 0.2.9 - 31 AUG 2026

Booley 0.2.9 moves elaboration checks into `sim`, gives synthesis and FPGA
implementation a shared report format, fixes setup, ticket, image, and
diagnostic failures, and updates the runtime toolchain.

### New features

- `synth` and `fpga` write the same versioned `implementation` object with the
  policy-resolved grade, Target identity, quality-of-results metrics, recipe,
  provenance, baseline comparison, cache state, and immutable report and log
  pointers. Both Flows also write atomic stable aliases, numbered reports, and
  live multi-Target progress. [PR #187](https://github.com/boldaxolotl/booley/pull/187)
- `booley flow sim --elab-only` compiles, elaborates, and links an ordinary
  untraced Simulation Target without running tests. `--build-only` is a
  permanent alias, `--standalone` adds the reusable-module sweep, and successful
  builds record `elab_pass_<target>` before simulation. See Upgrade notes for
  migration steps. [PR #185](https://github.com/boldaxolotl/booley/pull/185)
- `booley init` accepts links into another live checkout when the packaged skill
  trees match. Retargeting a managed or equivalent link requires
  `booley init --force`, displays both targets, and preserves unrelated files,
  directories, links, and junctions. [Issue #178](https://github.com/boldaxolotl/booley/issues/178)

### Quality of life

- The ticket-creation approval gate shows each new Target's name, destination,
  and full definition with the Ticket. If validation changes a Target, the gate
  asks for approval again. [PR #170](https://github.com/boldaxolotl/booley/pull/170)
- `booley session refresh` and Session and Project Image builds stream
  redirected output and emit heartbeats during silent stages. Bounded failure
  diagnostics remain available. [Issue #176](https://github.com/boldaxolotl/booley/issues/176)
- Booley keeps user guides under `docs/user/` and implementation material under
  `docs/internals/`, with separate Flow reference and troubleshooting guides.
  Setup directs new Tickets to `booley run` and labels the host check
  `host_prerequisites`. [PR #162](https://github.com/boldaxolotl/booley/pull/162),
  [PR #171](https://github.com/boldaxolotl/booley/pull/171)
- The Session Runtime ships Verible v0.0-4157-gfdbac312, Claude Code 2.1.251,
  and Codex CLI 0.151.0. [PR #189](https://github.com/boldaxolotl/booley/pull/189)

### Bug fixes

- Pulled GHCR sandbox flavors verify parent ancestry by registry digest, retain
  their short tags, and pass an immutable local image ID into Interactive Mode.
  Locally built images still reject stale ancestry.
  [Issue #172](https://github.com/boldaxolotl/booley/issues/172)
- Sealed Tickets resolve criterion Targets in the contract worktree after
  creating it. Contract-only Targets work during fresh setup and resume, while
  destination-only Targets cannot replace the reviewed contract.
  [Issue #173](https://github.com/boldaxolotl/booley/issues/173)
- On Windows, `booley init` applies guarded LF normalization to the project
  checkout and a separately cloned project-data repository. Doctor identifies
  the repository that remains unsafe.
  [Issue #174](https://github.com/boldaxolotl/booley/issues/174)
- Deep Doctor carries its internal Target authority into Lint. The deliberate
  bad-case Target receives a design-failure grade without becoming visible on
  public Target surfaces. [Issue #175](https://github.com/boldaxolotl/booley/issues/175)
- Live checkouts read their own `VERSION` instead of combining stale
  distribution metadata with the checkout's commit identity. Wheels still
  report their owning distribution metadata.
  [Issue #177](https://github.com/boldaxolotl/booley/issues/177)

### Upgrade notes

- Replace `booley flow elab` with `booley flow sim --elab-only`. Move
  `standalone_frontend` from `[flows.elab]` to `[flows.sim]`, remove `elab` from
  Target Doctor lists, and read `sim_<target>.json` with `mode: "elab_only"`
  instead of `elab_<target>.json`. Remove `keep_build_dir`; Simulation now
  retains its untraced build cache after success and failure.

[Full changes from v0.2.8](https://github.com/boldaxolotl/booley/compare/v0.2.8...v0.2.9)

## 0.2.8 - 29 AUG 2026

Booley 0.2.8 makes project setup safer for automation, expands ticket
acceptance-criteria guidance, and corrects Claude runtime and deep Doctor
behavior. Release preflight now catches Docker/demo failures before tagging.

### New features

- [`booley init --skip-credentials`](https://github.com/boldaxolotl/booley/pull/164)
  configures a project's provider and authentication policy without entering or
  storing placeholder credentials. Normal policy validation still applies.
- [Ticket acceptance-criteria guidance](https://github.com/boldaxolotl/booley/pull/163)
  now explains how projects can use area and cycle-count criteria to enforce PPA
  budgets, and coverage and mutation-testing criteria to strengthen
  testbenches.

### Bug fixes

- [Claude agents now use the supported Claude Agent SDK launcher and command construction](https://github.com/boldaxolotl/booley/pull/160),
  including Windows launchers. Authentication and traffic overrides stay scoped
  to the child process.
- [Deep Doctor checks now use the same isolated FuseSoC registry and build context as execution](https://github.com/boldaxolotl/booley/pull/167).
  This prevents generated projections or good firmware from masking bad
  simulation and lint fixtures.
- [The release Docker/demo preflight is now credential-free and candidate-only](https://github.com/boldaxolotl/booley/pull/164).
  It validates the exact release commit before tagging without promoting
  version or `latest` images, using a reviewed, pristine, Doctor-clean PicoRV32
  demo ([#166](https://github.com/boldaxolotl/booley/pull/166),
  [#167](https://github.com/boldaxolotl/booley/pull/167)).

[Full commit history](https://github.com/boldaxolotl/booley/compare/v0.2.7...v0.2.8)

## 0.2.7 - 28 AUG 2026

Booley 0.2.7 adds per-test cycle criteria, directed Target comparisons,
reusable ticket guidance, CLI-readable review packages, and optional Target
cleanup. It fixes image refresh, cancellation, Target resolution, tracing,
completion recovery, and acceptance evidence.

### New features

- Cycle Count criteria can grade each Target and test with absolute limits or
  relative percentage and cycle-delta thresholds. Directed Target pairs run the
  frozen baseline and candidate on different Targets for synthesis, FPGA, and
  cycle comparisons. Existing single-Target syntax still compares the same
  Target on both sides.
  ([Cycle Count criteria](https://github.com/boldaxolotl/booley/pull/105),
  [directed Target pairs](https://github.com/boldaxolotl/booley/pull/113))
- Project-owned Ticket Creation Guidance can describe defaults and policy as
  free-form Markdown. Booley applies the relevant guidance, validates the
  resolved Ticket, records its `on_success` policy, and keeps
  `ticket_defaults.md` as a fallback.
  ([guidance](https://github.com/boldaxolotl/booley/pull/135),
  [defaults and policy](https://github.com/boldaxolotl/booley/pull/125))

### Quality of life

- Ticket Mode now writes a versioned JSON review package for every Ticket that
  reaches review, including runs without a triage agent. CLI agents receive the
  package path through `BOOLEY_RUN_RESULT`, and `booley board prepare-review`
  reports it directly.
  ([review packages](https://github.com/boldaxolotl/booley/pull/112))
- Ticket branches can carry prepared Target contracts for the outer repository
  and an optional paired Project repository. Contracts bind the selected
  branches and source commits before validation or candidate preparation.
  ([prepared Ticket workspaces](https://github.com/boldaxolotl/booley/pull/126))
- `on_success.remove_targets` can remove criterion-bound `.core` Targets and
  owned `tests.toml` tables from an accepted candidate. The sealed contract
  fixes the exact removal set before completion.
  ([Target cleanup](https://github.com/boldaxolotl/booley/pull/137))
- Setup documentation now explains ownership of the stealth Project repository
  and where project-specific state belongs. The README and PyPI description
  describe Booley as an integrated RTL IDE built around agent workflows.
  ([Project repository guidance](https://github.com/boldaxolotl/booley/pull/122),
  [README](https://github.com/boldaxolotl/booley/pull/115))

### Bug fixes

- Normal-use fixes cover Ticket control-artifact round trips, compiler-isolated
  mutation variants, staged Reviewer contracts, focused Cocotb trace
  diagnostics, Doctor fail-path fixtures, clean Developer handoffs, and
  release-matched sandbox images. Mutation evidence names the exact source
  variant, trace requests validate real B-Wave content, and expected Codex
  recovery is no longer a warning.
  ([issue #88](https://github.com/boldaxolotl/booley/issues/88))
- Fixes from the Ibex port require provider and authentication selection before
  init seeds runtime state, allow no-EDA sessions with a read-only authority
  store, keep elaboration on simulation Targets, refresh stale Doctor output,
  use the project root for an unset simulation `run_cwd`, support Ticket-less
  Interactive Reviewer receipts, parse legacy Cycle Count mappings, and prevent
  paired-contract archive collisions.
  ([issue #127](https://github.com/boldaxolotl/booley/issues/127))
- Runtime Image refresh now uses one provenance lifecycle for pulled, locally
  built, flavored, and Project-derived images. Same-version images from another
  source revision are stale; managed parents rebuild in order; custom external
  images remain unmanaged; and refresh verifies the recreated runtime.
  ([issue #128](https://github.com/boldaxolotl/booley/issues/128))
- Interrupting `booley session enter -- <command>` now cancels and reaps the
  complete container process tree. Renewable job leases, zombie detection, PID
  identity checks, and bounded recovery prevent an abandoned HEAVY slot from
  blocking later work.
  ([issue #129](https://github.com/boldaxolotl/booley/issues/129))
- Verilator tracing now resolves one VCD or native FST recipe across compiler
  flags, runtime objects, harness behavior, transport, and validation. Authored
  FST Targets are no longer partially rewritten into VCD builds.
  ([issue #130](https://github.com/boldaxolotl/booley/issues/130))
- Contracts, prompts, Doctor, and Flows now share one canonical resolved Target
  interface. It separates durable identity from the exact callable selector,
  resolves conditional FuseSoC inputs once, omits fabricated bindings for
  Target-independent criteria, and preserves schema 3 contracts while writing
  schema 4. ([issue #131](https://github.com/boldaxolotl/booley/issues/131))
- Paired-repository completion now validates an immutable plan before mutation
  and records each publication and cleanup step in a durable journal. Retries
  resume from verified commit identities, and cleanup cannot delete a
  destination branch. ([issue #132](https://github.com/boldaxolotl/booley/issues/132))
- Ticket acceptance evidence is now separate from Doctor diagnostics and other
  live runtime state. Completion freezes an accepted snapshot, so cleanup and
  self-tests cannot erase red-green evidence or make completed Tickets display
  `0/N`. ([issue #133](https://github.com/boldaxolotl/booley/issues/133))
- Isolated worktrees can materialize configured submodules from an already
  initialized Project without remotes, SSH, global Git configuration, or shared
  `.git` pointers. The destination gitlinks remain authoritative, and unsafe or
  incomplete local sources fail with actionable errors.
  ([offline submodule setup](https://github.com/boldaxolotl/booley/pull/118))
- CRLF repair now refreshes only normalized tracked index entries, heals stale
  index metadata, and never stages content. Doctor detects status-only failures
  and points Windows users to a fresh-clone repair path.
  ([issue #107](https://github.com/boldaxolotl/booley/issues/107))

### Upgrade notes

- `booley init` no longer selects a default agent provider. Choose the provider
  and authentication method explicitly before init creates or refreshes
  provider-dependent runtime state.
- Recreate Ticket contracts older than schema 3. Schema 3 contracts remain
  readable after the schema 4 Target-interface update.
- New projects use `ticket_creation.md`. Existing `ticket_defaults.md` files
  remain supported as a legacy fallback and do not require immediate migration.

[Full commit history](https://github.com/boldaxolotl/booley/compare/v0.2.6...v0.2.7)
