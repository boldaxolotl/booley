# Admit Jobs against a measured memory budget

Status: accepted (2026-11-10)

Replaces the heavy Job Class's Job Slots (`[jobs] max_heavy`, `heavy_memory`)
with admission against a **Memory Budget**, using estimates from the
**Resource History**. The slot design this replaces was ADR 0028, which is not
in the repository; the code cites it as "Decision 5/6/8/12". Decided in
[#2513](https://github.com/boldaxolotl/booley/issues/2513) for
[#2460](https://github.com/boldaxolotl/booley/issues/2460).

## Context

Job Slots stand in for memory, badly. `max_heavy` is a fixed count (default 1)
and every heavy Job takes one slot, whatever it needs. A Project whose
full-chip synth needs ~17 GB has to keep `max_heavy = 1`, so a 2.7 GB block
synth or a 36 MB sim run waits behind it. In one area-optimization run the
agent spent 60 of its first 90 minutes blocked on Jobs.

Phase 1 ([#2470](https://github.com/boldaxolotl/booley/issues/2470)) records
each Job's peak memory (process-tree RSS plus its own `TMPDIR`), CPU time and
wall-clock time in a per-Project Resource History. A measurement run of about
100 Jobs on a large SoC Project found:

- Under one key and the same conditions, peak memory repeats within 0-4 % and
  wall-clock mostly within 1.2× (up to 1.9× under outside host load).
- Across conditions the key cannot see, it does not: an RTL edit moved synth
  memory 1.9× and 3.7× (wall 3.4× and 9.4×); compiler-cache state moved a
  Verilator build 2.6×; Test Variants sharing a key differed 3.2× in
  wall-clock; Doctor self-test runs polluted real keys.
- The measurement is trustworthy: the container peak is 0.58-1.23× the
  recorded peak, and recent max × 1.25 covers every case observed.
- Memory peaks did not change under concurrency. Five Jobs started together
  took 6.5-7 min against a 2.2-min longest Job with ~4.5 GB summed peak, so
  they competed for CPU or I/O, not memory.

Already decided on #2460 (2026-10-30): measure without per-Job cgroups (the
Sandbox's cgroup filesystem is read-only and delegating it would loosen the
hardening), one budget per container, no licence budgets in v1.

## Decision

### What is admitted

- Every Job that runs EDA tools in the Sandbox needs a Memory Reservation:
  sim (including coverage), synth, fpga, **lint** and Custom Flows. Lint and
  Custom Flows hold nothing today.
- Specialists keep the light Job Class's count cap (`max_light`). Their limit
  is the model API, not memory. `queue_max` stays.
- Queue priority by requester role is unchanged (`interactive` first;
  background work such as Goal baselines and `booley ci` last). Standalone
  Jobs (ADR 0086) and `booley ci` Jobs (ADR 0076) are admitted like any other
  Job at their existing priorities.

### The budget

- **Default:** the container's memory limit (`[sandbox].memory`) minus a
  reserve of the measured memory of the agent, the MCP server and the
  Dashboard, plus 2 GB.
- **No limit set:** use the host memory the container sees, minus the same
  reserve. Doctor warns that `[sandbox].memory` is unset.
- `[jobs] memory_budget` optionally overrides the default, for example to
  leave room for work done by hand in a Sandbox terminal.
- Memory is the only budget. CPU is recorded, not admitted. Tool thread counts
  (Verilator `-j`, OpenROAD threads) come from the container's cores and the
  number of running Jobs, no longer from `heavy_memory`. CPU admission may
  come later.

### Estimates

- A Job phase's memory estimate is the **maximum over all retained records**
  of its key × 1.25. The maximum covers cold and warm compiler-cache builds
  without tracking cache state. An estimate that is too high costs only
  parallelism.
- Wall-clock estimates, used only for jump-ahead, use the same rule on
  wall-clock time.
- **RTL changes are ignored.** Keys carry no input fingerprint. Most edits are
  small; a large jump is caught by the OOM guard and requeue below.
- **No history:** borrow the largest estimate for the same Target and phase
  (any Test Run or flags), then the largest for that phase anywhere in the
  Project. With neither, the Job runs alone.
- **Larger than the whole budget:** the Job waits until nothing else runs,
  then runs alone, with a warning in its result. It is never refused.

### Resource History

- Keys gain the Test Variant: (Target, phase, flags, Test Run without seed).
  Seeds stay out. Baseline runs of older revisions share keys with current
  runs.
- Each record also stores the size of the Job's source inputs in bytes. It is
  not part of the key; it is data for testing size-based scaling later.
- Doctor self-test runs write no records. Failed runs are kept. OOM-killed and
  timed-out runs count as lower bounds: they can raise an estimate, never
  lower it.
- Retention: 20 records per key, no global cap. A key whose newest record is
  older than 90 days is deleted.
- Measurement fixes: the process-tree walk includes children spawned by any
  thread, and `TMPDIR` is counted as memory only where it is tmpfs.

### Reservations

- A Job reserves its largest phase estimate at admission. As each phase
  finishes, the reservation **shrinks** to the largest estimate of the phases
  still to come. It never grows, so a running Job never waits partway
  through. A reused build ends its phase at once.
- A Job starts when its reservation fits the free budget. When the front Job
  does not fit, a later Job may **jump ahead** only if it fits now and its
  estimated finish comes before the front Job's earliest possible start. A
  Job with no wall-clock history never jumps ahead.
- A queued Job tells its caller the memory it needs, the memory free, its
  queue position and the running Jobs holding the most memory. An estimated
  start time is left for later: wall-clock estimates can be 9.4× off.

### Out-of-memory guard and requeue

- Each admitted Job's process tree raises its own OOM priority
  (`oom_score_adj`), newer Jobs higher than older ones. The agent, MCP server
  and Dashboard keep the default. When the container runs out of memory the
  kernel kills the newest Job first, never the agent. Raising one's own
  priority needs no privilege, so the Sandbox hardening is unchanged.
- A Job killed by the OOM killer is **requeued once**, to run alone, with its
  estimate raised to at least its observed peak. The caller sees it queued
  again with the reason. If it is OOM-killed again while alone, it fails with
  the OOM outcome. Results and Goal evidence come only from the final attempt.
- A memory-pressure (PSI) watcher that kills before the OOM point is added
  only if OOM kills turn out to be common.

### Simulation Campaigns

- Every Campaign work item, the outer lane's included, is admitted on its own
  with its own key. The Flow's reservation covers preparation and build, then
  drops to zero.
- The lane count becomes min(items, the container's cores); admission decides
  how many run at once. Each work item's simulator process gets its own OOM
  priority by start order.

### Licence seats

- A Project that uses a licensed EDA tool (Vivado today) must declare its
  **Seat Cap** in `[jobs.seats]`, for example `vivado = 1`. Admission then
  needs both memory and a free seat.
- Without the entry, Doctor FAILs and a Flow that uses the tool refuses before
  admission, naming the tool and the fix. `booley-heal` cannot fill it in:
  only the user knows how many seats they own.

### Rollout

- **One cutover.** Phases 2 and 3 of #2460 merge. The work lands in several
  pull requests behind an undocumented development switch, so `main` stays
  releasable with Job Slots in charge. The last pull request turns memory
  admission on, deletes the heavy Job Slots, `max_heavy` and `heavy_memory`,
  and removes the switch. There is no dogfood period and no fallback.
- **Retired keys stop config loading.** While `max_heavy` or `heavy_memory`
  is in `booley.toml`, every Booley command refuses with one message naming
  each key, its line, the fix and `booley-heal`. Doctor does not crash: it
  reports a FAIL with the same fix, and `booley-heal` deletes the keys.
- [#2299](https://github.com/boldaxolotl/booley/issues/2299) (detached
  Standalone Job worker) lands before this work starts, because it changes the
  same admission path.

## Considered options

- **Per-Job cgroups with `memory.peak`.** Exact, but they need cgroup
  delegation, which loosens the Sandbox hardening. Rejected on #2460.
- **Treat a changed input fingerprint as unknown.** Safe, but the agent edits
  RTL between nearly every run, so it would serialize the agent.
- **Scale estimates by input size.** Source bytes track netlist size poorly.
  Kept as recorded data for a later test, not as policy.
- **Memory-pressure watcher as a prerequisite.** New always-on machinery. The
  kernel's OOM priority gets "newest first, never the agent" without it.
- **Unknown Jobs always run alone.** A fresh Project's first regression would
  run fully serial.
- **Refuse Jobs estimated over the budget.** The estimate is padded and RSS
  double-counts shared pages, so such Jobs often fit.
- **Strict queue order, or anything that fits starts.** The first leaves
  memory idle; the second can hold a big synth back forever.
- **A two-week dogfood with a documented `admission = "slots"` fallback.**
  Rejected for an immediate switch. A fallback would keep alive the lease
  code being replaced.
- **Default Seat Cap of 1 when unset.** Rejected in favour of a mandatory,
  explicit entry.

## Consequences

- The heavy Job Class's definition changes from a count cap to the Memory
  Budget, and Jobs no longer "take Job slots". The glossary entries **Job**,
  **Standalone Job** and **Job Class** change with the cutover pull request.
  This ADR supersedes the Job Slot wording in ADR 0086 ("Standalone Jobs take
  Job slots") and ADR 0076 ("ordinary Job slots"); their priorities stand.
- Lint and Custom Flow Jobs can now be QUEUED.
- Doctor's `max_heavy × heavy_memory` capacity check and `doctor --deep`'s
  `heavy_memory` calibration go away. A missing `[sandbox].memory` and a
  missing Seat Cap become Doctor findings.
- Existing Projects with `max_heavy` or `heavy_memory` stop working until the
  keys are removed, and existing Vivado Projects also need a `[jobs.seats]`
  entry. Both are fixed in one `booley.toml` edit.
- The Campaign lease structure stays: a child lane still holds one lease per
  work item, now a Memory Reservation instead of a heavy slot.
