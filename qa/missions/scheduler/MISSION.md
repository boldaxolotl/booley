# Memory-budget Job scheduling under real load

Hunt bugs in how Booley admits Jobs against a measured Memory Budget: the
estimates it keeps, who waits and what the queue says, how reservations shrink,
what happens when the kernel kills a Job for memory, how a Simulation Record
admits its items, thread counts, Seat Caps, and the retired Job Slots keys.
These fail under load, which unit tests do not reproduce, so the areas run on
Caliptra, whose Jobs reserve from 0.1 GB to 22 GB each, and on a small
Custom Flow, `mem_hog`, for the cases that need exact control. The
`booley-qa-run` skill runs this mission; skim [RUNBOOK.md](RUNBOOK.md) before
area 1.

## Pins and prerequisites
- Booley: the candidate under test (set by the run skill; not pinned here).
- Upstream RTL (outer repo): https://github.com/chipsalliance/caliptra-rtl @
  `e379800c5467379386f96a9a51a539bd662b608a`, with submodules.
- Booley Directory: https://github.com/boldaxolotl/booley-prj-caliptra @
  `6acf3bd2d2612e54f43fba4f88ba71ced795b75f`, cloned as `.booley` inside the upstream checkout, with its
  `patches/` applied as its README says.
- Mission fixtures: the Custom Flow `fixtures/mem_hog.py` and the helper
  `fixtures/history_edit.py`.
- Tools/host: Linux x86-64 with cgroup v2, at least 30 GB of RAM, 16 cores
  and 60 GB of free disk; the `booley-sandbox-riscv` image; Vivado 2025.2 on
  the host for area 9. No agent CLI credentials are needed, except for the
  one MCP check in area 3, which is skipped without them.
- Budget: 8 h for the primary run: 460 area minutes plus 20 contingency.
  Area 11 is an optional follow-up outside this timebox. Areas run in
  priority order; log areas not reached as skipped. Run areas sequentially:
  each one depends on what the Booley Directory holds.

## Mission-specific rules
- The docs are the oracle: `docs/user/CONFIG.md` ("Jobs and concurrency",
  "Job resource history"), `docs/user/TROUBLESHOOTING.md` (deleted `[jobs]`
  keys, Job killed out of memory, missing Seat Cap), `docs/user/FLOW-SIM.md`,
  `docs/user/FLOW-FPGA.md` and ADR 0087
  (`docs/adr/0087-admit-jobs-against-a-measured-memory-budget.md`). When the
  run and a doc disagree, that is the finding; say which you believe.
- Upstream RTL and the Project pin are immutable inputs. Configuration
  fragments, history edits and forced rebuilds work on the staged copy and
  are undone at the end of their area (RUNBOOK.md says how).
- Copy the claim files and queue lines at the moment you see them; they vanish
  when the Job moves on. A behavior with no evidence is not a finding.
- Time is part of the result: record when each Job was submitted, started and
  finished, and the queue position it reported.
- Watch the host: if host memory pressure threatens the desktop or other
  work, stop the area, record it, and lower `[sandbox].memory`.

## Areas

### 1. setup — Stage Caliptra, baselines, budget warning (~60 min)
Intent: a known Project and a Resource History with real numbers.
Try:
- Stage per RUNBOOK.md "Staging". Doctor (host, then deep in the Sandbox)
  passes; with `[sandbox].memory` removed, Doctor warns `sandbox.memory-unset`
  and the text says what the budget falls back to. Put it back.
- Baselines, one at a time: `lint_ecc`, `lint_caliptra`, `sim_ecc` (one
  Variant), `sim_caliptra` (one test), `synth_ecc_top`, `fpga_ecc_top`, and
  `mem_hog --mb 300 --hold 5`. Each passes; note its wall time.
- `booley resources`: one entry per Target, phase and Test Run; compare the
  peaks with RUNBOOK.md "Reference numbers" and note big differences.
Look for: a Doctor warning that misstates the fallback, a phase missing from
`booley resources`, a Job not admitted or not recorded.

### 2. sched-history — Estimates and their fallbacks (~40 min)
Intent: estimates come from recorded peaks, as the docs describe.
Try:
- Compare `booley resources --json` with `history.json`: per-phase records for
  sim (`sim_prepare`, `sim_build`, `sim_run`), synth (`yosys`, `openroad`),
  the Test Run naming (`tests=...`; seeds of one Variant share an entry).
- Reservation = largest recorded peak × 1.25: read `reservation_mb` in the
  claim file of a running `sim_caliptra` and check it against its history.
- Fallback: a `sim_ecc` Variant never run before borrows the Target's
  estimate; a `mem_hog` run with `--target sim_caliptra` borrows the
  Project's. Only a phase
  never measured runs alone. CONFIG.md describes this since #4026; check the
  wording against what happens.
- Retention: 21 quick `mem_hog --mb 32 --hold 0 --ramp 0` runs keep 20
  records for the key. Age one key's `finished_at` past 90 days with
  `history_edit.py`; the next recording Job deletes that key only.
- `booley doctor --deep` adds no records. A corrupt `history.json` (save,
  truncate, run, restore) does not stop a Job.
Look for: an estimate below a recorded peak, an OOM or timeout run that lowers
an estimate, a key that outlives retention, a Doctor run that records.

### 3. sched-admission — Waiting and what the queue says (~60 min)
Intent: a Job that does not fit waits, says why, and starts when it fits.
Try:
- Real mix, no fragment: start `synth_ecc_fau`, `sim_caliptra` (one test),
  `synth_ecc_top`, `lint_caliptra` and `sim_ecc` a few seconds apart. Sum the
  estimates against the budget; exactly the Jobs that do not fit wait. Each
  waiter reports memory needed, memory free of the budget, its position, and
  the largest holders by run, Flow, Target and MiB. The CLI repeats a `[slot]`
  line about every 30 seconds.
- `small-budget` below `sim_caliptra`'s estimate: it runs alone with the
  "exceeds the Memory Budget" warning in its result.
- `queue-zero`: a second blocked Job is refused with a retry hint, and nothing
  is left in the `memory/` directory afterwards.
- `int-budget`: the run fails with the size-string hint, not a traceback.
- With agent credentials: submit `lint_caliptra` over MCP while the budget is
  full; `booley_poll` shows `QUEUED (position N)` with the same reasons. On
  the Dashboard, the Job detail has a Queue row.
Look for: a waiter with no reason, "free" that does not match budget minus
holders, a Job that starts while it does not fit, a stale claim file, a
budget change ignored by later Jobs.

### 4. sched-jump — A shorter Job goes first (~45 min)
Intent: a later Job jumps the queue only when it surely finishes before the
front Job could start.
Try:
- Holder `synth_ecc_fau` (about 16 min); front waiter `synth_caliptra`, too
  big to fit beside it; then `lint_ecc` (seconds, own history). `lint_ecc`
  starts at once and `synth_caliptra` starts when `synth_ecc_fau` ends. Stop
  `synth_caliptra` once it has started unless area 11 runs next.
- The same with a `mem_hog --hold` longer than the holder's remaining time,
  its `wall_s` set to match with `history_edit.py`: it waits its turn.
- A small Job whose phase estimate is borrowed (a new `sim_ecc` Variant)
  never jumps; nor does anything while a holder's finish time is unknown.
Look for: a jump that delays the front Job, a jump on a borrowed estimate, no
jump when every condition holds.

### 5. sched-shrink — Reservations shrink as phases end (~40 min)
Intent: a running Job gives memory back as its phases finish, and never takes
more.
Try:
- `small-budget` that fits `sim_caliptra`'s build but not the build plus
  `synth_ecc_top`. Start `sim_caliptra` with a forced rebuild, then
  `synth_ecc_top`. Poll the holder's `reservation_mb`: it drops after the
  build, and `synth_ecc_top` starts at that moment.
- `synth_ecc_fau`: its reservation drops from the Yosys estimate to the
  OpenROAD one when Yosys ends.
- A `sim_ecc --coverage` run and a `mem_hog` run (a Custom Flow): their claim
  stays constant to the end, as ADR 0087 says.
Look for: a reservation that grows, a shrink that never lets a waiter in, a
shrink on a Flow that should hold.

### 6. sched-oom — Kernel OOM kills and the single requeue (~60 min)
Intent: an OOM-killed Job runs again once, alone; a second kill fails it.
Try:
- `sandbox-oom` (RUNBOOK.md): restart the Session with a 12 GB limit. Lower
  the `sim_build` estimates of `sim_caliptra` and `sim_ecc` with
  `history_edit.py` so both fit, then start both with forced rebuilds. While
  they build, read `oom_score_adj`: the Job admitted last has 1000, the
  earlier one a lower level, coordinators 25 below their tools, the MCP
  server and any agent 0.
- The kernel kills a compiler of the Job admitted last. Its output says it
  was requeued after an out-of-memory kill; it waits to run alone, then
  builds. Its result says it is from the second attempt. The other Job is
  untouched. History records `oom_kill`, which raises the estimate.
- Exactly controlled: `mem_hog --mb` above the container limit is killed
  twice and fails with an out-of-memory reason; it is not requeued again.
- Compare with TROUBLESHOOTING.md "Job killed out of memory".
Look for: the older Job killed first, the agent or MCP server killed, a second
requeue, a compiler kill reported as a compile error, a result that hides the
first attempt, a kill not recorded in history.

### 7. sched-records — Each Simulation Record item is admitted (~50 min)
Intent: a Simulation Record runs its items in parallel only while their
reservations fit.
Try:
- `sim_caliptra` with eight tests from RUNBOOK.md "Record tests": after the
  shared build, items overlap up to the core count. In `memory/`, the Flow's
  own claim drops to zero once the build is settled, then each item holds its
  own.
- `sim_ecc` with all six Variants under `small-budget` fitting two runs: never
  more than two at once; results come back in manifest order.
- `synth_ecc_top` started during the `sim_caliptra` Record waits for budget
  between items, not for the whole Record.
Look for: items running under the Flow's claim after the build, more items in
parallel than fit, a Record that stalls with free budget.

### 8. sched-threads — Build threads follow cores and running Jobs (~25 min)
Intent: Verilator's build parallelism is the Job's share of the cores.
Try:
- Alone: the `sim_caliptra` build uses `-j<cores>`; compare with `nproc` and
  the CPU quota. The history record's `threads` matches.
- With one other memory holder (`mem_hog --hold 600`), the next forced
  `sim_ecc` build uses about half; with three holders, a third.
- A `make_options` with its own `-j`, added on the staged copy, is kept.
Look for: a thread count above the cores, one that ignores other Jobs, a
Project `-j` overridden.

### 9. sched-seats — Seat Caps for Vivado (~35 min)
Intent: Vivado needs a Seat Cap, and a capped Job waits for a seat.
Try:
- Two `fpga_ecc_top` runs with `vivado = 1`: the second waits for the seat
  and its queue text says so, even when memory is free. Its real estimate
  (about 19 GB) also blocks on memory, so first lower the `fpga_impl` history
  to 4000 MiB with `history_edit.py`. It starts when the first ends.
- Remove `[jobs.seats]`: the Flow is refused before it queues and Doctor
  FAILs, both naming the fix. Each bad value in RUNBOOK.md "seats" fails
  with the key named. Restore `vivado = 1`: Doctor passes with the cap shown.
Look for: a refusal after queueing, a bad value accepted, a seat wait with no
seat in its reason, a second Vivado run beside the first.

### 10. sched-cutover — Retired Job Slots keys (~25 min)
Intent: the retired keys are refused everywhere with one clear fix.
Try:
- `retired`: `booley flow sim`, `booley resources` and a Project command over
  MCP are refused, naming each key and line with the fix; `booley doctor` and
  `booley upgrade` still run and Doctor FAILs with the same fix.
- Follow the fix hint literally (delete the lines, or run booley-heal); the
  Project runs again.
- Search help, `booley cheat` and the user docs for Job Slots, `max_heavy`,
  `heavy_memory` or slot counts described as current behavior.
Look for: a command that runs with retired keys, a hint naming a surface that
does not exist, docs still describing Job Slots.

### 11. sched-fullchip — Full-chip synthesis against everything else (~60 min, optional follow-up)
Intent: the largest Job behaves under a real mix.
Try:
- `synth_caliptra` (Yosys about 17.5 GB) with `sim_caliptra` Records and
  `lint_caliptra` submitted behind it: the queue text, the shrink to the
  OpenROAD estimate and who starts then; host memory stays healthy.
Look for: a Job admitted over the budget, a stalled queue, host swap or OOM
outside the Sandbox.

### 12. cleanup — Scheduler cleanup (~20 min)
Stop every Flow and `mem_hog`, confirm `runtime/jobs/slots/memory/` is empty,
restore `booley.toml`, `history.json`, forced-rebuild edits and
`[sandbox].memory`, restart the Session if its limit changed, and release the
staged Project and its resources in `resources.md`.

## Known traps
- There is no `booley jobs` command and `booley_status` shows no queue: read
  the CLI's `[slot]` lines, `booley_poll`, the Dashboard and the claim files.
- "Free" in the queue text is the budget minus reservations, not free host
  memory; it is 0 while a Job runs alone.
- Each claim keeps the budget it was created with; a `booley.toml` change only
  affects later claims.
- A repeat sim run reuses its build: force a rebuild (RUNBOOK.md) whenever a
  step depends on the build phase.
- Every run records new peaks: lower or restore the history before each step
  that depends on a given estimate.
- A Job bound to a Goal is never requeued after OOM. This mission runs none.
- Until booley#4048 is fixed, a plain `fpga` run stalls after Vivado's
  "Synthesis finished" with `[vivado] <defunct>` processes under the
  execution supervisor. Check whether the candidate still does this once;
  then run every `fpga` step as
  `booley session enter -- /sbin/docker-init -s -- booley flow fpga ...`.
- Stealth Mode is on in this Project: a commit message that contains
  "booley" is refused.
- Parallel sub-agents share the host Sandbox cap (`[sandbox] max_sessions`,
  default 4); new Sandbox starts are refused at capacity.
