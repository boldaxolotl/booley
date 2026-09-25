# PicoRV32 published demo

Drive the published PicoRV32 demo Project through Booley end to end. The run covers setup, an
Interactive B-Wave repair, two dependent Tickets with Basis Refresh, a blocked-Ticket amendment, a
Simulation Campaign, Vivado, and negative cases. The IP is small and well known, so failures point
at Booley, usually at handoffs: paired repos, Ticket dependencies, Grants, Sessions, cleanup.

## Pins and prerequisites
- Booley: the candidate under test (set by the run skill; not pinned here).
- Project: https://github.com/boldaxolotl/booley-prj-picorv32 @
  `b8fe2370cb9aa7d93617850169f42f07821865d6` (upstream RTL plus Stealth `.booley_project`).
- Upstream: https://github.com/YosysHQ/picorv32 @ `a473fc8fca393771d83b0ffcf0b14db3393339d8`.
- Tools/host: Docker (≥6 GB free); the `booley init` Sandbox image (xPack GCC 15.2, srec_cat, dtc,
  Spike, `/opt/riscv-docs`, Icarus, Verilator, sv2v/Yosys/OpenROAD); a logged-in Codex or Claude
  backend.
- Optional: Vivado 2025.2 on x86-64 Linux (areas 6 and 11), a paid license and relay (11), a Git
  server you control (13), and VS Code with the Codex extension (2).
- Budget: 8 h, areas in priority order. Log any area not reached as skipped. Work areas 7 and 9–14
  while Tickets run. Rerun on Windows (WSL2, no Vivado) or another client only when asked.

## Mission-specific rules
- Upstream RTL and the Project pin are immutable. Change sources only through the prompts, the
  Tickets, or disposable fault branches that you restore afterwards.
- Submit the fenced blocks in `tickets/*.md` verbatim, rendering only the `{{ ... }}` placeholders
  as each file's preamble describes. Send `prompts/*.md` verbatim, and never reveal the fault.
- Apply an amendment only after a live human maintainer approves the exact preview. Without
  approval, leave the Ticket blocked and log it.

## Areas

### 1. setup — Install, split repos, init, Stealth, Doctor, baseline (~70 min)
Intent: a first setup succeeds from the public docs alone, and the unchanged demo passes every Flow.
Try:
- Clone both pins and confirm they are clean. Make `.booley_project` a standalone Git repo, then run
  `booley init`. Run Project Setup with no interview: sim, lint, synth (+ fpga with `[eda.vivado]`
  `host` if Vivado is present), merge-refreshed guidance, compatible Specialists, no native parity.
- Enable Stealth with `ignore_native_cores = true`. Only the hidden authored cores are projected,
  with no copied RTL and no symlinks. Dot-prefixed paths don't count as native.
- Create paired run-owned destination branches (outer and Project-data). Commit the setup changes
  (Flows, EDA, FPGA Doctor Target) to the Project-data one. Both repos must be clean before area 4.
- Build `firmware/firmware.hex` before the first Doctor run, since Doctor resolves the sim Target
  against it. `booley doctor` should show no warnings. `--deep` should show only
  `flow.synth-deep-warning:synth_core`, caused by upstream RTL or OpenROAD.
- Merge `fixtures/doctor-synth-warning-waiver.toml` into `.booley_project/doctor-waivers.toml`,
  keeping existing entries. Rerun and expect only `WAIVED`. The waiver must tolerate instance-name
  noise but reject a changed meaning. Repeat `booley init`: no drift; the auth policy matches the
  provider.
- Baseline: `booley targets`, Icarus sims (main, AXI, Wishbone, Dhrystone), Verilator lint, and
  physical synth all pass with fresh reports. The trees still match the setup branch and the pin.
Look for: undocumented steps, a stale image, over- or under-matching waivers, stale reports, wrong
tool resolution.

### 2. interactive-bwave — Interactive readiness and B-Wave semantics (~35 min)
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
  Targets, a Flow, and a Specialist, and must not create a Ticket.
Look for: off-by-one sampling, silent empty results, `--virtual` accepted where the docs reject it.
Known B-Wave defects still count as findings.

### 3. interactive-repair — Seeded Wishbone fault, diagnosis, repair (~30 min)
Intent: an Interactive child can find and fix a real bug from traces.
Try:
- Save a checkpoint. Inject `fixtures/wishbone-fault.md` yourself: in `picorv32_wb`, change `we`
  from the OR to the AND of `mem_wstrb[3:0]`. Send `prompts/repair-interactive.md` to the same
  child.
- Expected: byte stores hit `ERROR`. The trace is non-empty and shows `mem_wstrb`, `we`, `wbm_*`,
  `mem_valid`, `mem_ready`, and `ram_we`. B-Wave explains the fault, the fix is `assign we =
  |mem_wstrb;`, sim and lint pass, the local commit is non-empty, and the push is blocked.
- Before calling a diagnosis wrong, replay the child's exact B-Wave argv, sampling, and clock/reset
  defaults. Restore the checkpoint. Optional: file the fault as a `bug-fix` Ticket on a disposable
  branch.
Look for: a "repair" that restores HEAD or weakens a test, a push that gets through, lost MCP tools.

### 4. ticket-create — Create both Tickets before running either (~20 min)
Intent: agent-mode Ticket Create accepts complete packets and routes them to the paired refs.
Try:
- Render `tickets/continuity.md`: set `outer_destination_branch` to the outer branch without
  `refs/heads/`, and `project_destination_ref` to the full nested ref. Stage it under ignored
  `tmp/qa-inputs/`.
- Run `$booley-ticket-create --agent --no-confirm --input-file <path>` (Claude:
  `/booley-ticket-create …`). Expect ordered milestones, no clarification request, a board path, and
  `queued`.
- Read Ticket 1's actual slug and check `dependencies:` in `tickets/evolution.md` against it. If
  they differ, patch your copy and log a `qa-bug`. With Vivado, render `configured_fpga_criterion`
  as the block below; without it, leave it empty. Ticket 2 should come up as `waiting`.
  ```yaml
    FPGA:
      fpga_core_zbb (temp): pass
  ```
- On both Tickets, `branch` should map to the outer ref and `project_destination_ref` to the nested
  ref. The Baseline publishes on enqueue with no manual seal. Ticket Create should author only
  Target definitions, owned `tests.toml` tables, and empty `[new]` files.
Look for: swapped or collapsed refs, clarification requests on a complete packet, a slug mismatch.
Depends on: area 1. If its branches are missing or dirty, recreate them by hand and log it.

### 5. ticket1 — Dhrystone self-check Ticket and negative guard (~50 min)
Intent: a verification Ticket delivers exactly its contract and exports a persistent Target.
Try:
- `booley run --ticket <ticket-1-slug>`. Retry once, only on an exact `API Error: Response stalled
  mid-stream`. Only `dhrystone/dhry_1.c` and `dhrystone/testbench.v` change; 100 iterations stay.
- A mismatch prints an error and traps. `123456789` is written to `0x20000000` only after
  validation. The `[SIM_CYCLES] dhry <n>` line has n ≤ 110000 (calibration: 109734).
- The elab, sim, cycle, and TB-review Criteria bind to `sim_dhry_checked`, and it stays selectable.
  The Ticket reaches `done`, merges, cleans its worktree, and writes a triage report.
- Guard (`fixtures/dhrystone-guard.md`, disposable branch): flip one bit of the first expected
  operand. Expect a trap, no magic, and no cycles. Then restore, rerun to a pass, and discard the
  branch.
Look for: a pass without validation, the cap applied to the wrong test, temp and persistent Targets
mixed up, leftover worktrees.
Depends on: area 4. If Create failed, create Ticket 1 with the interactive skill and log it.

### 6. ticket2 — Basis Refresh and the Zbb feature Ticket (~120 min)
Intent: a dependent Ticket refreshes its Basis from the accepted provider and delivers a real
feature.
Try:
- Save Ticket 2's Basis before Ticket 1 is accepted. After the merge, the automatic refresh should
  pick up the Dhrystone source and `sim_dhry_checked` and move waiting → queued without approval.
- Refresh negatives (`fixtures/basis-refresh.md`, in disposable copies with Ticket 1 replayed):
  raising the cell threshold from 11% to 12% returns the Ticket to draft, keeps the old Basis, and
  starts no agent. Removing the exported `sim_dhry_checked` blocks it without a queue transition.
- Run `booley run --ticket <ticket-2-slug>`. All 18 Zbb ops match the ISA manual, and `ENABLE_ZBB`
  defaults to 0 in core, AXI, and WB. The registered PCPI responds in one cycle. The disabled test
  arms MMIO right before the first Zbb op and needs its illegal-instruction trap.
- The four temp sims go fail → pass. `sim_core`, `sim_wb`, and `sim_dhry_checked` really run and
  pass. Standalone elab passes, `lint_core_zbb` is clean, and mutation catches ≥14 of 15.
- `synth_core_zbb` is within +11% cells and +3% critical path of `synth_core`. `fpga_core_zbb`
  passes with Vivado. All RTL and TB reviews finish, and `review_rtl_bugs` is clean.
- Temp Targets live until acceptance, then leave along with their owned test tables. Expect `done`,
  a merge, cleanup, and a triage report. Then run a combined sim/lint/synth regression. Together the
  Tickets must cover every Criterion family, including FPGA when Vivado is present.
Look for: a refresh that rewrites authority, a provider Target selected but never run, temp Targets
leaking or disappearing too early, review Criteria passed without evidence.
Depends on: area 5. If Ticket 1 failed, hand-merge an equivalent self-check onto the destination
branches and log it.

### 7. amendment-block-preview — Block Tickets and preview amendments (~35 min)
Intent: an amendment preview on a blocked Ticket is read-only, fresh, and bounded.
Needed picorv32 state: area 1 only (initialized Project, Sandbox with Verilator). Use a disposable
copy of that Project, not the live one from areas 4–6. This area can run during area 6. Fallback:
cap `sim_dhry_checked` below Ticket 1's measured count (needs area 5).
Try:
- Copy in `fixtures/blocked-amendment/` and merge its `tests.toml`. `sim_amend`/`amend_smoke` must
  report exactly 438 cycles.
- Create three Tickets, each with the mandatory `cycle_count: [{target: sim_amend, test:
  amend_smoke, cycle_count_max: 400}]`. Threshold and Scope Tickets: `refactor` on
  `rtl/amend_counter.sv`, mandatory `sim_pass`, `assign`→`always_comb`, TB untouched.
- Optional Ticket: Scope only `docs/amend_note.md`, with an explanation task, so the cap is its sole
  mandatory Criterion. Run each Ticket on the live backend until it genuinely blocks.
- Take the expanded Criterion name from the sealed Ticket. Preview with `booley-ticket-triage`: cap
  450, reason `438 cycles meets the revised budget`, feedback `Continue from the preserved
  implementation`. Expect 400→450 and a digest, and nothing else changes.
- Apply an old digest after a scoped source edit: refused, no change. Restore, then preview again.
  These are refused before any change: tightening to 390, removing a Criterion, changing the Target
  or a build-control input, adding a protected Scope path.
Look for: previews that mutate state, stale digests that apply, relaxations that smuggle in other
changes.

### 8. amendment-apply-resume — Human-approved amendments and resume (~40 min)
Intent: an approved amendment continues the same Ticket under a new Baseline and keeps its history.
Try:
- Show the human a fresh 400 → 450 preview, and after approval apply that exact digest. Expect a new
  machine generation, the old 400 commit intact, one queue event, and a durable reason and feedback.
  Both repos should hold the same amendment.
- On resume, the Ticket continues the same implementation and passes on 438-cycle evidence. The 400
  failure stays in history, and the report and review finish.
- Optional Ticket: once approved, apply `make_optional: true` to its sole mandatory Criterion.
  Expect zero mandatory Criteria and no auto-accept. Finalizing needs an explanation.
- Scope Ticket: once approved, apply one `scope_add: ["tb/amend_tb.sv", "rtl/amend_extra.sv [new]"]`
  request. Both are added and the old ones kept. It re-queues under a new Baseline with no file
  edits.
Look for: a lost implementation checkpoint, repos out of step, auto-accept with zero mandatory
Criteria, Scope amendments that edit files.
Depends on: area 7.

### 9. sim-protocol — Simulation grades, test protocol, guards (~30 min)
Intent: every sim outcome gets the right verdict, and the guards fire.
Try (disposable Targets and TBs on a run-owned branch):
- Pass, design fail, inconclusive, and infra get distinct classes. A pass+fail+pass multi-Target run
  tries all three and keeps the strongest grade. Good and bad standalone modules differ.
- Elab-only results persist. A run-only argument to elab is rejected. A successful elab survives a
  later failing run.
- Sentinels map pass → pass, fail → fail, both → fail, and none → inconclusive. A named cycle count
  parses exactly, but a malformed one never satisfies a Criterion and never reads as zero.
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
  relative, directed, and named-clock Criteria evaluate. A missing or mismatched baseline fails
  closed.
Look for: missing and incompatible tools confused, duplicate aggregate rows, waivers that hide other
warnings.

### 11. vivado-fpga — Provisioned Vivado, Grants, FPGA Flow, licenses (~40 min, Linux)
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

### 12. sim-campaign — Simulation Campaign (~30 min)
Intent: a campaign keeps request order, resumes exactly, and grants Criteria only for the full
suite.
Try: follow `fixtures/simulation-campaign/RUNBOOK.md` in a run-owned Project copy.
- `--test tail --test quick` keeps that order, and `--tests-file reverse-tests.txt` normalizes to
  it. A duplicate `--test quick` exits 2 before any manifest or process exists.
- The manifest bytes stay stable. Kill the run during `slow` and `--resume-from` it: `quick` stays
  unchanged. Resuming with changed sources or a changed suite is refused. A partial suite never
  grants `sim_pass_sim_campaign`. Cross-check with `validate_campaign.py`.
Look for: catalog order overriding request order, completed items rerun, subset-granted Criteria.

### 13. git-stealth-security — Git safety, Stealth commits, runtime isolation (~35 min)
Intent: the guards block unsafe history and escapes, and the Sandbox stays fenced.
Try:
- Stealth (`fixtures/stealth.md` on a disposable empty commit): `booley` is removed, the rationale
  is kept, and `Co-Authored-By` is dropped. No Project state reaches outer history. A fresh outer
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

### 14. host-inventory — Install prerequisites, inventory, toolchain, docs (~25 min)
Intent: host-level commands and the public docs hold up.
Try:
- Remove or age one prerequisite in a disposable host fixture. Readiness names it before any
  mutation, then passes with no orphans once the prerequisite is restored.
- `booley projects discover` on a bounded root with real outer and nested Projects plus a dir
  symlink imports only the real roots. A moved-aside root stays listed as missing. Human and JSON
  views agree.
- RISC-V: GCC, srec_cat, dtc, and Spike run. The PDF content covers the ISA, privileged, and debug
  specs. Build `fixtures/riscv/spike-probe.S` with `spike-probe.ld`, check PT_LOAD fits RAM, run on
  Spike.
- Docs: log every help, cheat, MCP, and skill route you use. Flow and Specialist names agree across
  them, and the documented recovery after a seeded fault is enough to resume.
Look for: symlinked roots imported, views that disagree, advertised names that don't exist.

### 15. cleanup — Product cleanup (~20 min)
Exercise Booley's own cleanup first (Ticket worktrees, temp Targets, Session stop). Then release
every `resources.md` row: branches, worktrees, copies, Targets, projections, hooks, Sessions,
mounts, processes, Grants, registrations, License Profiles, relays. Leave borrowed Grants,
installations, images, and Sessions untouched. Record the final pin state. Nothing may be pushed.

## Known traps
- Ticket 2's `dependencies:` must be Ticket 1's actual slug, which Booley derives from `summary`. A
  pre-baked mismatch once cost 84 checks. The packets now align
  (`dhrystone-self-checking-cycle-contract`, `rv32-zbb-pcpi`), but still read the created slug and
  patch it locally if it differs.
- Ticket Create needs a standalone `.git` in `.booley_project`. The synth baseline pair needs a
  linked nested worktree. Switch between the two topologies.
- A regrant alone leaves the Session spec stale. Reissue it before any Doctor, FPGA, or mount check.
- Exit 125 from `booley session enter --` means your argv is missing the executable. It's not a lint
  verdict.
- B-Wave replays must use the child's defaults. Explicit sampling flags aren't equivalent.

