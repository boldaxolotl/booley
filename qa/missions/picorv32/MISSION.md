# PicoRV32 free hunt

A free bug hunt on the published PicoRV32 demo Project. Read Booley's docs and
do what a new user would: set the demo up and check its baseline, then use
whatever you like, the way the docs say to. The aim is as many real Booley bugs as you can find. This
mission has no checklist. The structured missions cover the planned journeys;
this one looks for bugs nobody planned for. The operator runs it through
[`booley-qa-run`](../../booley-qa-run/SKILL.md), which owns the run directory,
the findings format and cleanup bookkeeping.

## Pins and prerequisites
- Booley: the candidate under test (set by the run skill; not pinned here).
- Upstream RTL (outer repo): https://github.com/YosysHQ/picorv32 @
  `a473fc8fca393771d83b0ffcf0b14db3393339d8`.
- Booley Directory: https://github.com/boldaxolotl/booley-prj-picorv32 @
  `a3520837bce9416c1a7209d779148a9eb12482a9`, cloned as `.booley` inside the upstream checkout
  (a Stealth, Versioned Booley Directory).
- Host: Docker with at least 6 GB free, and a logged-in Codex or Claude backend. Optional:
  Vivado on x86-64 Linux, VS Code.
- Docs: drive Booley from the candidate's `README.md`, `docs/user/`, `--help`, cheat sheets and
  packaged skills. Read Booley's Python source and `docs/internals/` only to diagnose a finding.
- Budget: 8 h: 460 area minutes (50 baseline, 390 hunt, 20 cleanup) plus 20 contingency.

## Mission-specific rules
- Work on run-owned branches, worktrees and disposable copies. Send `prompts/*.md` verbatim;
  never tell a child where a seeded fault is or how it was made.
- Follow the shared [Goal-child operating rules](../../shared/GOAL-CHILD.md) whenever you run
  a Goal.

## Areas

### 1. baseline — Quick start, baseline and Stealth guard (~50 min)
Intent: a new user gets the unchanged demo working from its README alone.
Try:
- Follow the demo README's Quick start: clone both pins, `booley init`, the printed setup steps
  (`post-setup.sh` before the first Doctor run) and `booley doctor --deep`. Repeat `booley init`:
  nothing drifts.
- `booley targets`, then every shipped Verilator sim, Verilator lint and physical synthesis
  (sv2v, Yosys, OpenROAD). Each passes with a fresh report, and both repos stay clean.
- Stealth guard: the three cases in `fixtures/stealth.md`, on disposable empty commits.
Look for: undocumented steps, stale images or reports, wrong tool resolution, a Stealth guard
that rewrites a message instead of refusing it.

### 2. hunt — Free bug hunt on the demo (~390 min)
Intent: find as many real Booley bugs as possible, guided only by the user docs.
Go anywhere. Spend time where things feel fragile, and go past the happy path: bad input,
interruption, repeats, wrong order, stale state. Check results, not just exit codes. One area
covers the whole hunt, so log each thread in `log.md` as you finish it (what you tried, what
happened) and keep its evidence under `evidence/hunt/<thread>/`; a resumed run picks up from
that log.

Ground only this demo has, so it's worth hunting here:
- Stealth Mode with projected hidden cores, and the Paired Checkout that a Versioned Booley
  Directory adds to every worktree and Goal Branch.
- The RISC-V toolchain image: GCC, srec_cat, dtc, Spike and the specs under `/opt/riscv-docs`.
- A real CPU for Goals: the demo ships its own Goalsets, and this mission adds
  `goals/continuity.md` (Dhrystone), `goals/evolution.md` (Zbb), `goals/entry-probe.json` and
  `goals/fpga.json`.

Optional bait with known answers. Use any of it, or none:
- `fixtures/wishbone-fault.md`: a seeded Wishbone bug for an Interactive child to find and fix
  (send `prompts/repair-interactive.md`; `prompts/interactive.md` is the read-only opener).
- `fixtures/dhrystone-guard.md`: a corrupted Dhrystone operand that must trap.
- `fixtures/virtual-signals.md`: B-Wave virtual-signal values you can check by hand.
- `fixtures/lint/`: Verilator and Verible waiver fixtures.
- `fixtures/riscv/`: a bare-metal Spike probe.
- `fixtures/simulation-campaign/RUNBOOK.md`: an ordered, resumable Icarus campaign.

Look for: wrong results reported as success, stale evidence or reports, docs that don't
match behavior, and recovery steps that don't recover.

### 3. cleanup — Product cleanup (~20 min)
Use Booley's own cleanup first (Goal abandon or Finish, worktree removal,
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
- Use unique worktree names and Goal slugs: an existing same-day Goal Branch refuses entry.
- Integrate every printed branch before cutting the next worktree. When a Paired Checkout was
  printed, a clean outer tree alone does not prove its branch is current.
- Entry reads `default.md` only from the primary checkout's `.booley/goalsets/`. A copy
  committed only in a worktree's Paired Checkout does not trigger the include-or-skip decision.
- `goals/evolution.md` (Zbb) starts from integrated `goals/continuity.md` work and needs
  `docs/riscv-zbb-spec.txt` committed before entry. The operator extracts it from the installed
  manual under `/opt/riscv-docs`: the encodings and semantics of all 18 Zbb ops, HTML tags
  stripped, at most 30,000 characters (drop examples and commentary first). Record the manual's
  path, SHA-256 and byte ranges beside it. `goals/fpga.json` targets `fpga_core_zbb`, which
  exists only after that Goal, and needs Vivado registered and granted.
- The demo ships `goalsets/zbb.md` and `specs/zbb.md`. If you run this mission's
  `goals/evolution.md`, the child must not pick up the demo's `zbb` Goalset.
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
- Icarus has no `$system`, and SystemVerilog fixtures need `-g2012`. The campaign's `slow` test
  spins about 20 s; if it finishes too fast to interrupt, pass `+slow_steps=<n>`.
- Bare-metal Spike needs `tohost`/`fromhost` symbols to exit. A Linux `ecall` exit works only
  under `pk`.
