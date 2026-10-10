# PicoRV32 published demo

Drive the published PicoRV32 demo Project through Booley end to end. The run covers setup, an
Interactive B-Wave repair, Dhrystone and Zbb Goals, proposals, Finish, crash resume, a
Simulation Campaign, Vivado, and negative cases across primary and optional
follow-up runs. The IP is small and well known, so failures point
at Booley, usually at handoffs: paired repos, evidence freshness, Grants, Sessions, cleanup.

## Pins and prerequisites
- Booley: the candidate under test (set by the run skill; not pinned here).
- Project: https://github.com/boldaxolotl/booley-prj-picorv32 @
  `9e187ceae6357b47b50bdc835609b4570fd28832` (upstream RTL plus Stealth `.booley_project`).
- Upstream: https://github.com/YosysHQ/picorv32 @ `a473fc8fca393771d83b0ffcf0b14db3393339d8`.
- Tools/host: Docker (≥6 GB free); the `booley init` Sandbox image (xPack GCC 15.2, srec_cat, dtc,
  Spike, `/opt/riscv-docs`, Icarus, Verilator, sv2v/Yosys/OpenROAD); a logged-in Codex or Claude
  backend.
- Optional: Vivado 2025.2 on x86-64 Linux (areas 6 and 11), a paid license and relay (11), a Git
  server you control (13), and VS Code with the Codex extension (2).
- Budget: 8 h for the primary run: 460 area minutes (including 90 for
  continuity and 150 for Zbb/proposals) plus 20 contingency. Areas 2, 9 and
  11–14 are optional follow-up work outside this timebox (195 minutes total);
  the non-Stealth history control adds an optional 25 minutes. Preserve their
  original timeboxes and log them as skipped unless a follow-up is requested.
  Run primary areas sequentially; Interactive children never overlap an active
  Goal in the same Project. Rerun on Windows/another client only when asked.

## Mission-specific rules
- Upstream RTL and the Project pin are immutable inputs. Hidden faults stay
  with the operator. Send `prompts/*.md` verbatim; never reveal the seed recipe.
- The pinned Project already contains seeded `goalsets/` and the managed
  `.gitignore`; repeat initialization must not drift.
- Follow the shared [Goal-child operating rules](../../shared/GOAL-CHILD.md)
  for every Goal area.

## Areas

### 1. setup — Install, split repos, init, Stealth, Doctor, baseline (~70 min)
Intent: a first setup succeeds from the public docs alone, and the unchanged demo passes every Flow.
Try:
- Clone both pins and confirm they are clean. Make `.booley_project` a standalone Git repo, then run
  `booley init` and follow the demo steps it prints (Sandbox, then
  `bash .booley_project/hooks/post-setup.sh`). The demo ships preconfigured for sim, lint, and
  synth. Configure it with the edits below, not with the `booley-setup` skill.
- Project-data edits: set `[specialists.coverage_analyst] enabled = false` in `booley.toml` (the Icarus
  Targets can't produce Verilator Coverage Campaigns). If Vivado is present, also set
  `[flows.fpga] enabled = true`, add `[eda.vivado]` with `provisioning = "host"`, and add
  `booley: {doctor: [fpga]}` to `fpga_core` in `cores/picorv32_impl.core`.
- Enable Stealth with `ignore_native_cores = true`. Only the hidden authored cores are projected,
  with no copied RTL and no symlinks. Dot-prefixed paths don't count as native.
- Create run-owned destination branches (outer and standalone Project-data).
  Area 1 makes the Project its own Git repo: require the paired checkout printed
  by `booley worktree new` before using two-branch integration. Commit the setup changes
  (Flows, EDA, FPGA Doctor Target) to the Project-data one. Both repos must be clean before area 4.
- Build `firmware/firmware.hex` before the first Doctor run, since Doctor resolves the sim Target
  against it. `booley doctor` and `booley doctor --deep` should show no warnings. Repeat
  `booley init`: no drift; the auth policy matches the provider.
- Baseline: `booley targets`, Icarus sims (main, AXI, Wishbone, Dhrystone), Verilator lint, and
  physical synth all pass with fresh reports. The trees still match the setup branch and the pin.
Look for: undocumented steps, a stale image, over- or under-matching waivers, stale reports, wrong
tool resolution.

### 2. interactive-bwave — Interactive readiness and B-Wave semantics (~35 min, optional follow-up)
Intent: the Interactive child has the right context, and B-Wave returns correct values, not just
exit 0.
Try:
- Start one long-lived child in the Sandbox, via `booley session enter -- booley` or an inherited
  child with cwd, PTY, identity, and MCP. Send `prompts/interactive.md`. The child should report
  identity, cleanliness, Doctor, and Targets, then run the traced Wishbone sim and edit nothing.
- Follow `fixtures/virtual-signals.md`. `wave`/`find`/`sample`/`distance`/`value` with `--virtual
  qa_hsk` should match your own `mem_valid & mem_ready`, and each `d=` should equal end − start.
  `list`/`signal`/`diff`/`stats`/`stuck` should reject `--virtual` with parser exit 2.
- A traced sim should give a fresh, non-empty, queryable FST, and VCD-to-FST conversion from a file
  or FIFO should give the same values. In VS Code, the extension should attach and run status,
  Targets, a Flow, and a Specialist, and must keep the read-only tree unchanged.
Look for: off-by-one sampling, silent empty results, `--virtual` accepted where the docs reject it.
Known B-Wave defects still count as findings.

### 3. interactive-repair — Seeded Wishbone fault, diagnosis, repair (~30 min)
Intent: an Interactive child can find and fix a real bug from traces.
Try:
- Save a checkpoint. On a run-owned disposable branch, inject `fixtures/wishbone-fault.md`
  yourself in a disposable copy and commit it: in `picorv32_wb`, change `we` from the OR to the AND of
  `mem_wstrb[3:0]`. Start an Interactive Mode child on that branch and send
  `prompts/repair-interactive.md`.
- Expected: byte stores hit `ERROR`. The trace is non-empty and shows `mem_wstrb`, `we`, `wbm_*`,
  `mem_valid`, `mem_ready`, and `ram_we`. B-Wave explains the fault, the fix restores an OR
  reduction of `mem_wstrb[3:0]` (the upstream bytes or `|mem_wstrb`), sim and lint pass, the local
  repair commit on top of the fault commit is non-empty, and the push is blocked.
- Before calling a diagnosis wrong, replay the child's exact B-Wave argv, sampling, and clock/reset
  defaults. Preserve the repair evidence and restore the checkpoint before closing the area.
Look for: an empty repair commit, a repair that weakens a test, a push that gets through, lost MCP
tools.

### 4. goal-enter — Goalsets, ad-hoc entry and refusals (~20 min)
Intent: entry creates a bound Goal Branch with concrete mandatory Goals and clear refusals.
Depends on: area 1's clean outer and Project-data destination branches.
Try:
- Install `goals/continuity.md` and `goals/evolution.md` in Project `goalsets/`
  and commit. Create a linked workspace from the primary checkout with
  `booley session enter -- booley worktree new <unique-name>`.
- Start the Codex Goal child, send `$booley-goal` plus `goals/continuity.md`.
  Confirm translated Goals, origin, record ID, base commit, branch and worktree
  identity, then explicitly abandon this entry probe.
- Use `goals/entry-probe.json` for a separate disposable ad-hoc entry; abandon
  it on the operator's explicit instruction before removing its workspace.
- Probe refusal for the main checkout, dirty outer tree and dirty paired
  Project checkout, each in isolation.
- Separately probe an active/finishing occupant and existing same-day Goal Branch.
- Probe a missing relative baseline Target and missing spec separately.
  A missing candidate Target only warns and remains unmet. Preserve each
  diagnostic and restore that probe's owned inputs.
- Add and commit a disposable `default.md` in the primary checkout's Project
  `goalsets/`. Entry without an include-or-skip decision refuses; explicit skip
  needs the operator's instruction and reason.
  Include it on a control entry. Abandon probes, then remove `default.md` and
  commit that removal so later entries do not inherit the probe.
Look for: branch collisions on rerun, collapsed Project/worktree identity,
missing origins, or entry mutating a refused workspace.

### 5. goal-evidence — Dhrystone contract and fresh evidence (~90 min)
Intent: a verification Goal delivers self-checking firmware with evidence bound to its Target.
Depends on: area 4's working entry route; if entry failed, fix only the reported setup cause.
Try:
- Enter a fresh continuity Goal in a new clean linked worktree. The child follows
  `goals/continuity.md`: 100 iterations, deterministic final
  validation, mismatch error/trap, success magic `123456789` at `0x20000000`
  only after validation, and `[SIM_CYCLES] dhry <n>` with n ≤ 110000.
- Verify elab, full sim, cycle_count and TB-quality evidence through
  `goal_status(work_dir=...)`. A shell-only diagnostic cannot meet a Goal.
  Save initial evidence, make a harmless relevant edit within the Goal, check
  freshness, rerun and require fresh evidence. Restore and commit intended work.
- In a separate disposable Project copy, change a Protected Input, observe warnings/discarded
  evidence and blocked Finish, restore exact bytes and rerun the affected producer.
- Finish the continuity Goal, inspect the package as in area 7 and integrate
  the outer branch plus the Project-data branch when printed.
- After that integration, follow `fixtures/dhrystone-guard.md` in a separate
  disposable Project/Goal: corrupt one expected operand, require trap/no magic/
  no cycles, restore and rerun to pass. Explicitly abandon the probe and retain
  the real continuity Goal's Review Package before cutting the Zbb worktree.
Look for: a pass before validation, the cap bound to the wrong test, stale
Goal evidence or a missing `sim_dhry_checked` in a printed Project-data merge.

### 6. goal-change — Zbb feature and human decisions (~150 min child time)
Intent: a second Goal starts from integrated Dhrystone work and tests add, relax and retarget.
Depends on: area 5 finished and all printed branches integrated; never hand-implement a substitute.
Try:
- With participating destination checkouts clean, create a new linked worktree. Give a
  new Codex child `$booley-goal` plus `goals/evolution.md`. Before entry, the
  operator extracts the installed manual's normative Zbb encodings and semantics
  for all 18 ops, including their headings, as plain text with HTML tags removed,
  into `docs/riscv-zbb-spec.txt`. Record the installed manual's path, SHA-256,
  source byte range(s) and the tag-stripping step beside the excerpt. The
  reviewer reads at most 30,000 characters: if the text is longer, keep the
  encodings and semantics of all 18 ops, drop examples and commentary, and record
  what was dropped. Verify the excerpt is self-contained and within the limit,
  log its character count, then commit it and its record. Bind spec review to it.
  When Vivado is already registered and granted (area 11 ran first), the operator
  supplies `goals/fpga.json` as ad-hoc Goals; otherwise log that family as skipped.
- All 18 Zbb ops match the manual; `ENABLE_ZBB` defaults to 0 in core, AXI and
  WB, registered PCPI responds in one cycle, and the disabled test arms MMIO
  immediately before the encoding and requires its illegal-instruction trap.
- Full registered suites on four Zbb sims and existing core/WB/Dhrystone sims
  pass; elab and lint pass, mutation detects ≥14/15, synth is within +11% cells
  and +3% critical path against base `synth_core`, optional FPGA passes. Reviews
  provide real evidence, with RTL bugs clean and other reviews done.
- In separate disposable records using the explicit mutation floor, create an
  `add`, a `relax` (14/15 to 13/15) and a `retarget` proposal. Inspect exact
  before/after and saved proposal IDs; the operator approves or rejects each
  with a reason. Check Change Log, updated spec_revision, invalidated evidence
  and refreshed status. Approved probes rerun producers before Finish or are
  explicitly abandoned; never relax the real feature contract to hide failure.
- Silence probe: withhold a reply to one saved proposal, then resume that exact
  ID and check it stays pending with no applied Goal Change. Reject it explicitly
  afterwards, and abandon the disposable record.
- Before closing this area, use area 7's Finish/review/integration procedure for
  the real feature Goal; every disposable proposal probe is already resolved.
Look for: stale evidence surviving a changed specification, decisions invented
from silence, lost proposals or unreported Target/constraint changes.

### 7. goal-finish — Review Package, history and integration (~25 min)
Intent: Finish requires fresh evidence and a clean committed HEAD; integration is operator work.
Try:
- Attempt Finish while a Goal is unmet, with stale evidence and with dirty
  owned files in disposable probes: no completed claim. Restore, rerun, commit.
- The child saves record ID, stable operation ID and Session Summary before
  `goal_finish`; retry identical arguments and require the same completion.
  Inspect diff/base, final Goals, evidence pointers, Change Log, open `done`
  findings, `.core`, `tests.toml`, `.sdc`/`.xdc` changes and Target semantic diff.
- In this Stealth Project, the Session Summary stays local and no history commit
  is made. Optional follow-up (~25 min, outside the primary timebox): create a
  non-Stealth disposable control with a trackable history path, complete its
  Goal and require Finish to commit the summary before integration.
- Finish preserves branch/worktree. The operator reviews and merges the outer Goal Branch
  and any printed paired Project-data branch; remove the paired checkout first
  when present, following the printed cleanup steps.
  Regress sim/lint/synth on the integrated destination and save both final HEADs.
Look for: Finish merging or cleaning without instruction, missing commits from a printed pair,
a changed final HEAD escaping revalidation or advisory findings missing from the package.
Depends on: area 6; area 5 also uses this review/integration procedure.

### 8. goal-resume-after-crash — Resume and explicit abandon (~20 min)
Intent: a restarted client recovers the same Goal Record without losing work or inventing decisions.
Try:
- Enter a disposable Goal via `goals/entry-probe.json`, gather evidence and
  commit a harmless implementation checkpoint. Save worktree, record, branch,
  and evidence pointers. Before the crash, create an `add` proposal for a lint
  Goal on `lint_core`, save its exact pending proposal ID and withhold a decision.
  Then kill/reap the child.
- Restart a Codex child in the same worktree. Its first Booley call is
  `goal_status(work_dir=..., rules=true)`; recover rules, Goals, evidence and
  pending proposals before editing. Resume the exact saved proposal ID;
  it stays pending until the operator explicitly rejects it with a reason.
  No new record, duplicate approval or duplicate evidence may be fabricated.
- Quiet/stuck presence never abandons automatically. Give explicit operator
  instruction to abandon this disposable Goal; the child quotes it in
  `goal_finish(abandon=true, instruction_quote=...)`. Also exercise the human
  CLI route in another disposable Goal: have the child run `booley goal abandon`
  with that Goal worktree as cwd. A host call must explicitly select it, e.g.
  `booley session enter -- sh -c 'cd <worktree> && booley goal abandon …'`. Preserve
  branches/checkpoints, then remove only owned resources.
Look for: a new record substituted for recovery, lost commits, or abandonment
without an explicit instruction. Resolve every interrupted Goal within this area.

### 9. sim-protocol — Simulation grades, test protocol, guards (~30 min, optional follow-up)
Intent: every sim outcome gets the right verdict, and the guards fire.
Try (disposable Targets and TBs on a run-owned branch):
- Pass, design fail, inconclusive, and infra get distinct classes. A pass+fail+pass multi-Target run
  tries all three and keeps the strongest grade. Good and bad standalone modules differ.
- Elab-only results persist. A run-only argument to elab is rejected. A successful elab survives a
  later failing run.
- Sentinels map pass → pass, fail → fail, both → fail, and none → inconclusive. A named cycle count
  parses exactly, but a malformed one never satisfies a Goal and never reads as zero.
- A skipped-by-default test runs only when named. A plusarg or getopt token runs exactly that test.
- Guards: going over the disk budget kills the run and names the largest files. A frozen clock gets
  the documented watchdog class. A pre-run step stages fresh firmware with no TB edits. Restore,
  rerun, and expect fresh passes with no leftover producer.
Look for: inconclusive runs graded as pass, stale artifacts, orphaned simulators.

### 10. lint-synth — Lint and synthesis negatives, metric thresholds (~35 min)
Intent: lint and synth give distinct, correct outcomes and fail closed.
Try:
- Lint gives a distinct report for each of clean, warning, syntax error, and missing tool. A
  nonfatal warning keeps the rc separate from the verdict. File scope holds. Restore and rerun
  clean.
- Two Verilator Targets on `fixtures/lint/qa_lint_fixture.sv` (WIDTHTRUNC) should give one aggregate
  row listing both. Waivers: `fixtures/lint/verilator-waiver.vlt`, plus
  `fixtures/lint/verible-waiver.txt` on `fixtures/lint/render_verible.py` output. Only the intended
  warning goes away.
- `booley flow synth --target synth_core` reports a missing `sv2v` as missing and an incompatible
  one as incompatible. A latch is critical. A timing miss is advisory without a threshold and a
  failure with one. Metrics are fresh and name the tools.
- A disposable linked-worktree baseline pair names both commits with a numeric delta. Absolute,
  relative, directed, and named-clock Goals evaluate. A missing or mismatched baseline fails
  closed.
Look for: missing and incompatible tools confused, duplicate aggregate rows, waivers that hide other
warnings.

### 11. vivado-fpga — Provisioned Vivado, Grants, FPGA Flow, licenses (~40 min, Linux, optional follow-up)
Intent: host EDA provisioning is exact, read-only, and revocable.
Try:
- Register host Vivado 2025.2 under a run-owned name, then list and show it. Compare canonical paths
  (symlink spelling may differ). Inspect Grants first, and replace only run-owned ones.
- After each Grant change: `booley init --seed`, `session validate`, `session up`.
  `/opt/booley-eda/vivado` is read-only, and the release-layout binary (`bin/` or
  `Vivado/bin/vivado`) prints its version.
- An uncached implementation gives a fresh PASS with a routed DCP plus timing and utilization
  reports. Revoke the Grant, then grade the FPGA Flow before recovering: expect exit 2, no Vivado
  run, and no mount. Re-grant, reissue the Session, and retry. Doctor must then be clean.
- Dry-run lists the part, top, XDC, and sources. The same inputs hit the cache, and `--no-cache`
  runs fresh. A bad design gives rc 1, and a baseline/candidate pair gives a delta. `forget` on a
  granted root is refused. After revoking, `forget` removes exactly that root.
- With a paid license only: create, read, update, and attach a License Profile to the Grant. A relay
  fault should fail, and restoring the relay should recover. Unapproved destinations must be
  blocked. Delete the profile afterwards.
Look for: a stale Session spec after regrant, borrowed Grants or installations changed, cached
results passed off as fresh.

### 12. sim-campaign — Simulation Campaign (~30 min, optional follow-up)
Intent: a campaign keeps request order, resumes exactly, and grants Goals only for the full
suite.
Try: follow `fixtures/simulation-campaign/RUNBOOK.md` in a run-owned Project copy.
- `--test tail --test quick` keeps that order, and `--tests-file reverse-tests.txt` normalizes to
  it. A duplicate `--test quick` exits 2 before any manifest or process exists.
- The manifest bytes stay stable. Kill the run during `slow` and `--resume-from` it: `quick` stays
  unchanged. Resuming with changed sources or a changed suite is refused. A partial suite never
  grants `sim_pass_sim_campaign`. Cross-check with `validate_campaign.py`.
Look for: catalog order overriding request order, completed items rerun, subset-granted Goals.

### 13. git-stealth-security — Git safety, Stealth commits, runtime isolation (~35 min, optional follow-up)
Intent: the guards block unsafe history and escapes, and the Sandbox stays fenced.
Try:
- Stealth (`fixtures/stealth.md`, three cases on disposable empty commits): a `Co-Authored-By`
  footer is rejected with the raw message unchanged. Without it, `booley` is redacted and both
  rationale sentences are kept. A final `Generated with booley` is rejected, and `Generated with
  care by the whole team` is accepted (see the fixture for how its words are redacted). No Project state reaches outer history. A fresh outer
  clone shows the documented hidden-state limit.
- An invalid native core is ignored while authored Targets resolve. Refreshing after an
  authored-core edit updates the projection.
- Git policy (disposable bare remote): a bad subject or body is refused, not truncated. The escape
  skips the convention but still sanitizes. Identities, banned paths, symlinks, and the guard escape
  behave as documented. Restore, then commit and push cleanly.
- Isolation: non-root, with no host home, SSH, or Docker socket. Undeclared routes are blocked while
  provider calls work. The PDK mount refuses writes, and an interrupt leaves no descendants.
- A push to your Git server succeeds from outside. From inside, the network blocks it (not Git or
  auth), and the ref is unchanged.
Look for: truncation instead of refusal, symlinks getting through, host secrets in the Sandbox.

### 14. host-inventory — Install prerequisites, inventory, toolchain, docs (~25 min, optional follow-up)
Intent: host-level commands and the public docs hold up.
Try:
- Remove or age one prerequisite in a disposable host fixture. Readiness names it before any
  mutation, then passes with no orphans once the prerequisite is restored.
- `booley projects discover` on a bounded root with real outer and nested Projects plus a dir
  symlink imports only the real roots. A moved-aside root stays listed as missing. Human and JSON
  views agree.
- RISC-V: GCC, srec_cat, dtc, and Spike run. The PDF content covers the ISA, privileged, and debug
  specs. Build `fixtures/riscv/spike-probe.S` with `spike-probe.ld` (`-march=rv32imac -mabi=ilp32
  -nostdlib`), check PT_LOAD fits RAM, then run `spike --isa=RV32IMAC -m0x80000000:0x8000000 <elf>`:
  it exits 0.
- Docs: log every help, cheat, MCP, and skill route you use. Flow and Specialist names agree across
  them, and the documented recovery after a seeded fault is enough to resume.
Look for: symlinked roots imported, views that disagree, advertised names that don't exist.

### 15. cleanup — Product cleanup (~20 min)
Exercise Booley's own cleanup first (Goal worktrees, owned Targets, Session stop). Then release
every `resources.md` row: branches, worktrees, copies, Targets, projections, hooks, Sessions,
mounts, processes, Grants, registrations, License Profiles, relays. Leave borrowed Grants,
installations, images, and Sessions untouched. Record the final pin state. Nothing may be pushed.

## Known traps
- Use unique worktree names and Goal slugs: an existing same-day Goal Branch refuses entry.
- Integrate every printed branch before cutting the next worktree. When a paired
  checkout was printed, a clean outer tree alone does not prove its branch is current.
- Entry reads `default.md` only from the primary checkout's Project `goalsets/`. With a copy
  committed only in a worktree's paired Project checkout, entry does not ask for an include-or-skip
  decision.
- A regrant alone leaves the Session spec stale. Reissue it before any Doctor, FPGA, or mount check.
- Exit 125 from `booley session enter --` means your argv is missing the executable. It's not a lint
  verdict.
- B-Wave replays must use the child's defaults. Explicit sampling flags aren't equivalent.
- The first `booley init` on the unchanged demo prints "the booley-setup skill does not apply".
  That's expected: area 1 configures the demo by hand. Once area 1's edits are in, the repeat run
  reports the Project as already configured instead; it's no longer the published checkout.
- Fixture testbenches must print a pass sentinel the Project configures (`ALL TESTS PASSED.` here).
  Configured sentinels replace the built-in `[SIM_RESULT]` markers, so a clean `$finish` alone
  grades INCONCLUSIVE.
- Icarus has no `$system`, and SystemVerilog fixtures need `-g2012` (the campaign cores set it in
  `flow_options.iverilog_options`). The campaign's `slow` test spins about 20 s of wall time. If it
  finishes too fast to interrupt, pass `+slow_steps=<n>`.
- Bare-metal Spike needs `tohost`/`fromhost` symbols to exit. A Linux `ecall` exit only works under
  `pk`.
- Stealth cores resolve fileset paths from the repository root, not the core file. A fixture core
  copied into `.booley_project/cores/<dir>/` must name its files as
  `.booley_project/cores/<dir>/<file>`, or the Flow fails with `Project compile input is not a
  file: /work/<file>`.
