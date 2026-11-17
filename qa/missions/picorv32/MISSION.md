# PicoRV32 hunt

A bug hunt on the published PicoRV32 demo Project. Read Booley's docs and do
what a new user would: set the demo up and check its baseline, walk three
release journeys (Metric-Driven Development, multiple Agent CLIs, Simulation
Records), then use whatever you like, the way the docs say to. The aim is as
many real Booley bugs as you can find. The journeys say where to start, not
what to certify; the free hunt looks for bugs nobody planned for. The operator
runs it through
[`booley-qa-run`](../../booley-qa-run/SKILL.md), which owns the run directory,
the findings format and cleanup bookkeeping.

## Pins and prerequisites
- Booley: the candidate under test (set by the run skill; not pinned here).
- Upstream RTL (outer repo): https://github.com/YosysHQ/picorv32 @
  `a473fc8fca393771d83b0ffcf0b14db3393339d8`.
- Booley Directory: https://github.com/boldaxolotl/booley-prj-picorv32 @
  `a3520837bce9416c1a7209d779148a9eb12482a9`, cloned as `.booley` inside the upstream checkout
  (a Stealth, Versioned Booley Directory).
- Host: Docker with at least 6 GB free, and both Claude Code and Codex logged
  in. Optional: Vivado on x86-64 Linux, VS Code.
- Docs: drive Booley from the candidate's `README.md`, `docs/user/`, `--help`, cheat sheets and
  packaged skills. Read Booley's Python source and `docs/internals/` only to diagnose a finding.
- Budget: 8 h: 460 area minutes (50 baseline, 70 mdd, 60 agent-clis, 40
  sim-record, 220 hunt, 20 cleanup) plus 20 contingency.

## Mission-specific rules
- Work on run-owned branches, worktrees and disposable copies. Send `prompts/*.md` verbatim;
  never tell a child where a seeded fault is or how it was made.
- Follow the shared [Developer Agent child
  rules](../../shared/DEVELOPER-CHILD.md) whenever you
  ask a child for a Measured Commit.

## Areas

### 1. baseline — Quick start and baseline (~50 min)
Intent: a new user gets the unchanged demo working from its README alone.
Try:
- Follow the demo README's Quick start: clone both pins, `booley init`, the
  printed setup steps (`post-setup.sh` before the first Doctor run) and
  `booley doctor --deep`. Repeat `booley init`: nothing drifts.
- `booley targets`, then every shipped Verilator sim, Verilator lint and
  physical synthesis (sv2v, Yosys, OpenROAD). Each passes with a fresh report,
  and both repos stay clean.
Look for: undocumented steps, stale images or reports, wrong tool resolution.

### 2. mdd — Metric-Driven Development core (~70 min)
Intent: a user declares Target Metrics on the demo and gets a Measured Commit,
following
[METRIC-DRIVEN-DEVELOPMENT.md](../../../docs/user/METRIC-DRIVEN-DEVELOPMENT.md).
Try:
- Declare: `booley cheat --metrics`, then compare its blocks with
  `fixtures/metrics.toml`. Commit the fixture as `metrics.toml` in the Booley
  Directory's own repository; `booley measure --validate` lists no error. On a
  disposable copy, an unquoted dotted key, a relative bound and an unknown
  metric each get one listed error, all at once, with a `write instead:` line
  where the doc promises one.
- Measure: in a new worktree commit a harmless change and run
  `booley measure`: exit 0, green header, the commit Measured. Run it again:
  the stored run prints and no Flow starts.
- Amend: `git commit --amend` with only a new message keeps the commit
  Measured (no Flow on the next `booley measure`); an amend that changes a file
  is measured again.
- Base run: `booley measure --all <base>`, then measure the change with
  `--base <base>`: the report gains the Δ column. Before the base run exists,
  the report prints the command to create it. Then run
  `booley flow synth --target <t> --baseline <base>` against that measured
  base: the stored Measurement Run is reused instead of synthesizing again.
- Developer Agent: start a child per the child rules and send
  `prompts/continuity.md` (Dhrystone): its `"dhry.cycle_count_max"` bound,
  the approval it asks for, and the Measured Commit it reports.
Look for: a stale stored run served for a changed tree, a verdict or exit
status that disagrees with the report, and doc steps that don't work as
written.

### 3. agent-clis — Multiple Agent CLIs (~60 min)
Intent: one Project uses both Claude Code and Codex, as
[CONFIG.md](../../../docs/user/CONFIG.md) `[agent]` and `[models]` describe.
Try:
- Allow both: `[agent] clis = ["claude", "codex"]`, with `auth` set per CLI.
  `booley auth --status`, then `booley auth --cli codex` and
  `booley auth --cli claude`.
- `booley chat --cli codex` and `booley chat --cli claude` each open the
  named CLI with Booley's MCP tools; `--cli` with a CLI not in `clis` is
  refused with the fix.
- A `[models]` entry that runs one Specialist on the non-default CLI; the
  Specialist runs there, and a model that account can't run fails naming the
  entry.
- Doctor checks every CLI in `clis`: a missing credential fails for the
  default CLI and for one a `[models]` entry needs, and warns for any other.
  Missing or `"auto"` `auth` fails naming the value `booley init` would record.
- Concurrent sessions: a second `booley chat` while the first runs lands in
  worktree `s<N>` with the banner; tab title, Dashboard row and worktree name
  match. `--here` and `--new-worktree` behave as documented.
  `booley chat --name <label>` shows `S<N> · <label>` in the tab and on the
  Dashboard, and an invalid label is refused. Repeat with each CLI.
Look for: the wrong CLI or login used, billing on a credential `auth` didn't
pick, stale locks after exit, and a label or handle that drifts.

### 4. sim-record — Simulation Records (~40 min)
Intent: the Simulation Record names and contracts hold on a small Icarus
Target.
Try:
- Follow `fixtures/simulation-record/RUNBOOK.md`: exact selection, manifest
  authority under `targets/<t>/record/`, interruption and
  `--resume-from <...>/record/manifest.json`, and measurement scope. Output,
  JSON fields and schema names use Record names throughout.
- Old records: in a run-owned venv, install the published 0.4.0 wheel and
  write one Simulation Campaign run with it. The candidate reports that run
  directory as written by an older Booley, never as corrupt, and names the
  directory to delete, for `--resume-from`.
Look for: leftover Campaign names, a resume that redoes finished work, and a
refusal that doesn't say what to delete.

### 5. hunt — Free bug hunt on the demo (~220 min)
Intent: find as many real Booley bugs as possible, guided only by the user docs.
Go anywhere. Spend time where things feel fragile, and go past the happy path:
bad input, interruption, repeats, wrong order, stale state. Check results, not
just exit codes. One area covers the whole hunt, so log each thread in
`log.md` as you finish it (what you tried, what happened) and keep its
evidence under `evidence/hunt/<thread>/`; a resumed run picks up from that
log.

Ground only this demo has, so it's worth hunting here:
- The Paired Checkout that a Versioned Booley Directory adds to every
  worktree.
- The RISC-V toolchain image: GCC, srec_cat, dtc, Spike and the specs under
  `/opt/riscv-docs`.
- A real CPU to measure: the Dhrystone Target from area 2, and further work
  on it as Measured Commits.

Optional bait with known answers. Use any of it, or none:
- `fixtures/wishbone-fault.md`: a seeded Wishbone bug for a child to find and
  fix (send `prompts/repair-interactive.md`; `prompts/interactive.md` is the
  read-only opener).
- `fixtures/dhrystone-guard.md`: a corrupted Dhrystone operand that must trap.
- `fixtures/virtual-signals.md`: B-Wave virtual-signal values you can check by
  hand.
- `fixtures/lint/`: Verilator and Verible waiver fixtures.
- `fixtures/riscv/`: a bare-metal Spike probe.

Look for: wrong results reported as success, stale evidence or reports, docs
that don't match behavior, and recovery steps that don't recover.

### 6. cleanup — Product cleanup (~20 min)
Use Booley's own cleanup first (worktree removal,
`booley session down`). Then release every `resources.md` row and nothing else. Leave borrowed
Grants, installations, images and Sessions untouched. Record the final pin state.

## Known traps
- The first `booley init` on the unchanged demo prints "the booley-setup skill does not apply".
  That's expected: the demo ships configured. Once you commit to `.booley`, a repeat run reports
  the Project as already configured instead.
- `post-setup.sh` builds `firmware/firmware.hex`, which the sim Target needs. Run it before the
  first Doctor run.
- The demo's Verilator Targets use a custom C++ `main` without `custom_main_hooks`, so they write
  no coverage data. Disable `[specialists.coverage_analyst]` rather than report that.
- The demo ships `doctor-waivers.toml`. Waived Doctor rows are expected; some apply only in
  GitHub Codespaces.
- The demo has no `[agent]` pin, so Booley defaults to Claude Code. To drive it with Codex, run
  the first init as `booley init --provider codex`.
- Integrate every worktree branch, and its `booley-worktree/<name>` branch,
  before cutting the next
  worktree; a clean outer tree alone does not prove the paired branch is
  current.
- Flow `--baseline` reuses a stored Measurement Run only once #3845 has
  landed. If it is still open on the candidate, a re-synthesized baseline is
  known, not a finding.
- `booley measure` reads `metrics.toml` from the Booley Directory repository's
  committed HEAD (Stealth Mode): commit it with `git -C .booley commit`.
- The demo's README still describes a removed feature in its main event and
  ships its records, so Doctor warns `goals.leftover`; both are known, not
  findings.
- A regrant alone leaves the Session spec stale. Reissue it before any Doctor, FPGA or mount
  check.
- A failing `booley session enter -- <argv>` exit code is not a Flow verdict: 126 or 127 means
  the first word is not runnable in the Sandbox (for example, `flow lint` without `booley`), and
  125 means Booley's command supervisor failed.
- B-Wave replays must use the child's defaults. Explicit sampling flags aren't equivalent.
- The Project configures `ALL TESTS PASSED.` as its pass sentinel. Configured sentinels replace
  the built-in `[SIM_RESULT]` markers, so a clean `$finish` alone grades INCONCLUSIVE.
- Stealth cores resolve fileset paths from the repository root, so a fixture core copied into
  `.booley/cores/<dir>/` must name its files as `.booley/cores/<dir>/<file>`.
- Icarus has no `$system`, and SystemVerilog fixtures need `-g2012`. The
  fixture's `slow` test
  spins about 20 s; if it finishes too fast to interrupt, pass `+slow_steps=<n>`.
- Bare-metal Spike needs `tohost`/`fromhost` symbols to exit. A Linux `ecall` exit works only
  under `pk`.
