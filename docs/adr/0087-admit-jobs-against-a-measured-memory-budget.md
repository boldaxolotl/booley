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

- While a Job holds a Memory Reservation, Booley ranks its processes for the
  kernel's OOM killer through `oom_score_adj`, by admission order. The kernel
  kills the process with the highest score: its memory share times 1000, plus
  `oom_score_adj`. The share is the process's memory over the container's
  memory limit plus the swap its cgroup may use (the limit alone without swap).
  The Job's **coordinator** is the process that holds its claim, and its
  **tools** are every process the coordinator starts.
- Tools get 1000 when the Job is the newest running Job, 500 when one newer Job
  runs, then 450, 400, 350 and 300 (300 for five or more newer Jobs). The
  newest Job is the one most likely to have tipped the container, so the first
  gap is wide. The coordinator holds its tools' level minus 25, so an OOM kill
  takes a tool and leaves the coordinator able to report the kill or requeue
  the Job. At equal sizes it still outranks every older Job's processes, down
  to the fifth newer Job (below that, levels tie and the coordinator at 275
  ranks below an older Job's tools at 300). It returns to its previous value
  when the claim ends. The agent, MCP server and Dashboard keep the default
  (0). A level depends only on how many running Jobs were admitted later, so it
  does not depend on how many Jobs ran earlier. Raising a process's
  `oom_score_adj` needs no privilege, so the Sandbox hardening is unchanged.
- The ranking is re-applied to the coordinator's process tree every 0.5 s: a
  tool just started runs at its coordinator's value for up to that long, and a
  re-rank after another Job starts or ends converges within one interval, so
  newest-first may not hold inside that window. A Job whose holder died without
  releasing counts as a newer Job until another claim reaps it, which can
  leave a live Job one level lower.
- The ranking assumes the Sandbox starts every process at 0 (Booley sets no
  `--oom-score-adj`; a coordinator that starts at 275 or above is left
  unranked, never lowered, unless it runs inside another Job's tree, which
  happens when a Custom Flow command runs a `booley` endpoint: that nested Job
  ranks its own tree and restores the value it inherited). It also assumes the
  container does not set `memory.oom.group` (the kernel would then kill the
  whole cgroup) and that tools stay in the coordinator's process tree (a tool
  that detaches and is re-parented elsewhere is not ranked). Without `/proc`
  nothing changes.
- A Campaign work item that holds its own Memory Reservation counts as a Job
  of its own, admitted after its Flow: its simulator processes rank by the
  item's admission order, so the newest item's simulator is killed first. With
  five items running, the Flow's own processes sit at 300 and its coordinator
  at 275; each further item moves the next-oldest item to 300.
- An item's own OOM kill stops only that item. Its attempt publishes no result
  and the item is requeued once: it goes to the back of the Campaign's queue,
  claims the whole budget with the reason `requeued after out-of-memory`, waits
  for the running items to finish, then runs with no other claim that reserves
  memory. Items already running keep going. If that second attempt is killed
  too, its failure is the item's result. While the requeued item runs, its
  Flow reads as requeued in the Job status and its timeout budget restarts
  once. An item running under the Flow's own claim, before the Flow's
  reservation drops, is not requeued on its own: its kill requeues the whole
  Flow Job, as the Job bullet below describes. A resumed Campaign forgets which
  items were requeued, so each item gets its one requeue again.
- The levels hold for these reference sizes, shares of the limit defined above,
  for example 32 GiB. Each check is `1000 × share + adj`, the share written as
  a fraction (0.001 is 0.1 %):

  | Claim | Sizes | Check |
  |---|---|---|
  | Newest Job before the next older | newer process 0.1 % (33 MiB), older process 45 % (14.4 GiB) | 1000 + 1 > 500 + 450 |
  | Newer before older among the older Jobs (tools, below saturation) | newer 0.1 %, older 4.5 % (1.4 GiB) | (older level + 50) + 1 > older level + 45 |
  | Never the agent | agent process 12 % (3.8 GiB), Job process 0 % on the lowest level | tools 300 > 120, coordinator 275 > 120 |
  | Coordinator below its own tools | coordinator 2 % (655 MiB), tool 0 % | 20 + (level − 25) < level |

  `tests/runtime/test_oom_priority.py` checks these against the kernel's
  formula; change a size there and here together.
- A Job killed by the OOM killer is **requeued once**, to run alone, with its
  estimate raised to at least its observed peak. The caller sees it queued
  again with the reason. If it is OOM-killed again while alone, it fails with
  the OOM outcome. Results and Goal evidence come only from the final attempt.
  A Job bound to a Goal is never requeued as a whole, so its evidence comes
  from its only attempt (#3511). The items of a Goal-bound Campaign are
  requeued all the same: each is its own Job, and a killed item attempt
  publishes no result.
- The OOM-kill count is container-wide, so a rise alone cannot say which Job
  was hit. With memory admission on, a command counts as OOM-killed only when
  the count rose while it ran, it exited nonzero, and a process of its own
  process tree died by a SIGKILL Booley did not send. Booley sees that SIGKILL
  in the command's exit status (its own, or the 137 a shell reports), in the
  simulator adapter's report of its simulator, or in the kill line a launcher
  prints for a killed child: GNU make (`make: *** [...] Killed`), the GCC
  driver (`Killed signal terminated program cc1plus`), clang and the
  `verilator` driver (`Verilator threw signal 9`). A Job is OOM-killed when one
  of its commands was.
- A timeout, a cancellation or an unreadable counter never marks a command
  OOM-killed, and a command that fails for its own reason while another Job is
  killed keeps that failure. A kill is not detected when the tool runs under a
  wrapper that exits with another status and prints none of those lines
  (Vivado run scripts, a project's own scripts, a non-GNU or localized make,
  colored compiler diagnostics). That Job fails with its own failure and is
  not requeued.
- A memory-pressure (PSI) watcher that kills before the OOM point is added
  only if OOM kills turn out to be common.

### Simulation Campaigns

- Every Campaign work item, the outer lane's included, is admitted on its own
  with its own key. The Flow's reservation covers preparation and build, then
  drops to zero.
- The lane count becomes min(items, the container's cores); admission decides
  how many run at once. Each item's simulator processes rank for the OOM
  killer by the item's admission order, and an OOM-killed item runs once more,
  alone (see the out-of-memory guard above).

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

## Amendments (2026-11-14)

Recorded while implementing
[#4066](https://github.com/boldaxolotl/booley/issues/4066).

- **The queue is unbounded; `queue_max` is retired.** "`queue_max` stays" above
  no longer holds. A Job that cannot start waits until it runs or is
  cancelled, and `[jobs].queue_max` is refused like `max_heavy` and
  `heavy_memory`, with its own reason.
- **Why.** No reason for the cap was recorded. It refused work that was already
  admitted: out-of-memory requeues and measurement children needed an
  exemption (`bounded=False`) and a start window to stay clear of it. A refused
  Job reached the agent as BLOCKED with only "retry". A waiting claim reserves
  no memory, so a long queue costs a few small files, not capacity.
- ADR 0086's "a full queue refuses the launch" is superseded: the review screen
  says where the Job queues and Submit never refuses for a queue.
