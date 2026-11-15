# Admit Jobs against a measured memory budget

Status: accepted

## Context

Job Slots stood in for memory, badly. A fixed count of heavy Jobs, each
taking one slot whatever it needs, forces a Project whose full-chip synth
needs many gigabytes to serialize every heavy Job, so a small block synth or a
sim run waits behind it. An agent spent most of its first hour of an
optimization run blocked on Jobs.

Measuring Jobs showed what an estimate can and cannot rely on:

- Under one key and the same conditions, peak memory repeats closely and
  wall-clock mostly does.
- Across conditions the key cannot see, it does not: an RTL edit, compiler
  cache state, or a Test Variant sharing a key can move memory or wall-clock
  by multiples.
- The measurement is trustworthy, and a recent maximum with headroom covers
  every case observed.
- Concurrency did not change memory peaks. Jobs started together slowed down
  by competing for CPU or I/O, not memory.

Measurement uses no per-Job cgroups: the Sandbox's cgroup filesystem is
read-only, and delegating it would loosen the hardening.

## Decision

### What is admitted, and against what

- Every Job that runs EDA tools in the Sandbox needs a Memory Reservation:
  sim (including coverage), synth, fpga, lint and Custom Flows. Specialists
  and the Mutation Flow's coordinating Job are admitted by memory too, as
  companion claims (below). No count cap remains: the light Job Class is
  gone, and the Job queue has no limit. A waiting claim reserves no memory,
  so a long queue costs little, and a cap would refuse work already admitted
  (requeues, measurement children).
- Memory is the only budget. CPU is recorded, not admitted; tool thread
  counts come from the container's cores and the running Jobs.
- The Memory Budget is always computed from the container's memory limit
  (`[sandbox].memory`, else the host memory the container sees) minus a
  reserve for the agent, the MCP server and the Dashboard. Doctor warns when
  `[sandbox].memory` is unset. There is no override: one that replaces the
  computed budget subtracts no reserve, so the natural setting lets Jobs eat
  the memory the agents are using and invites OOM kills.
- Queue priority by requester role: `interactive` first; background work last.
  Standalone Jobs (ADR 0086) are admitted like any Job at their priorities.
- A Project that uses a licensed EDA tool must declare its Seat Cap in
  `[jobs.seats]`; admission then needs memory and a free seat. Without the
  entry Doctor fails and a Flow using the tool refuses before admission,
  naming the fix. No default (such as 1): only the user knows how many seats
  they own, so `booley-heal` cannot fill it in either.
- `[jobs]` keys for retired mechanisms (`max_heavy`, `heavy_memory`,
  `max_light`, `queue_max`, `memory_budget`) stop config loading with the fix
  named, and Doctor reports the same fix instead of crashing. There is no
  fallback to slots.

### Estimates

- A phase's memory estimate is the maximum over retained records of its key,
  with headroom. The maximum covers cold and warm compiler-cache builds without
  tracking cache state; too high an estimate costs only parallelism.
  Wall-clock estimates, used only for jump-ahead, follow the same rule.
- RTL changes are ignored: keys carry no input fingerprint. The agent edits
  RTL before nearly every run, so treating a changed input as unknown would
  serialize it. A large jump is caught by the OOM guard and requeue.
- Keys include the Test Variant but not the seed, so baseline runs of older
  revisions share keys with current runs. Records also store source input
  size as data for testing size-based scaling later; it is not policy,
  because source bytes track netlist size poorly.
- No history: borrow the largest estimate for the same Target and phase,
  then for that phase anywhere in the Project; with neither, the Job runs
  alone. (Always running unknown Jobs alone would serialize a fresh
  Project's first regression.)
- A Job estimated over the whole budget waits until nothing else runs, then
  runs alone with a warning. It is never refused: estimates are padded and
  RSS double-counts shared pages, so such Jobs often fit.
- Doctor self-test runs write no records, since they polluted real keys.
  OOM-killed and timed-out runs count as lower bounds: they raise an estimate,
  never lower it.

### Reservations and queueing

- A Job reserves its largest phase estimate at admission and shrinks it as
  phases finish. It never grows, so a running Job never waits partway
  through.
- Order is by priority, but a later Job may jump ahead of a front Job that
  does not fit when it fits now and is estimated to finish before the front
  Job can start. A Job without wall-clock history never jumps. Strict order
  would leave memory idle; "anything that fits starts" could hold a big synth
  back forever.
- A queued Job tells its caller the memory it needs, the memory free, its
  queue position and the running Jobs holding the most memory. It promises no
  start time, because wall-clock estimates can be off by large factors.
- Companion claims (Specialists, a mutation coordinator) never keep another
  claim waiting on them alone, and a companion that does not fit is not the
  front. A Specialist's reservation is its own measured peak or a default
  based on the agent CLI's measured footprint; the coordinator reserves
  nothing, so it never changes how many Specialists fit. Companions may starve
  while ordinary claims keep arriving; ordinary claims come first. A
  companion is never run alone and never requeued.

### Out-of-memory guard

- While a Job holds a reservation, Booley ranks its processes for the kernel
  OOM killer through `oom_score_adj` by admission order: the newest running
  Job's tools are killed first (it is the Job most likely to have tipped the
  container), every Job's processes before the Developer Agent's, the MCP
  server's and the Dashboard's. The Job's coordinator, the process holding
  its claim, ranks just below its own tools, so a kill takes a tool and leaves
  the coordinator able to report it or requeue the Job. The levels, the gap
  and the refresh interval live in the code, with a test that checks the
  ranking against the kernel's formula.
- Raising `oom_score_adj` needs no privilege, so the Sandbox hardening is
  unchanged. The ranking assumes the container sets neither a starting score
  nor `memory.oom.group`, and that tools stay in the coordinator's process
  tree; where `/proc` is missing it does nothing.
- Booley does not add a memory-pressure watcher: it is new always-on
  machinery, and the kernel's priority already gives "newest first, never the
  agent". It would be added only if OOM kills turn out to be common.
- A Job killed by the OOM killer is requeued once, to run alone, with its
  estimate raised to at least its observed peak; a second kill while alone
  fails it. Results and evidence come only from the final attempt. A Job
  bound to a Goal is never requeued as a whole, so its evidence comes from its
  only attempt.
- The OOM-kill counter is container-wide, so a rise alone cannot say which
  Job was hit. A command counts as OOM-killed only when the counter rose while
  it ran, it exited nonzero, and a process of its own tree died by a SIGKILL
  Booley did not send. A timeout, a cancellation or an unreadable counter
  never marks a kill. Detection is deliberately conservative: a kill hidden
  behind a wrapper that reports none of the known signs is a plain failure
  and is not requeued.

### Simulation Records

Every work item of a simulation or coverage record is admitted on its own,
with its own key. The Flow's reservation covers preparation and build, then
drops to zero, and admission decides how many items run at once. Each item
ranks for the OOM killer as a Job newer than its Flow, so the newest item's
simulator dies first. An item's own OOM kill stops only that item: it is
requeued once, to run alone after the running items finish, and its second
failure is its result. A kill of an item running under the Flow's own claim
requeues the whole Flow Job instead.

## Considered options

- **Per-Job cgroups with `memory.peak`.** Exact, but they need cgroup
  delegation, which loosens the Sandbox hardening.
- **A dogfood period with an `admission = "slots"` fallback.** It would keep
  alive the lease code being replaced. The cutover was immediate.

## Consequences

- Jobs no longer "take Job slots": the Job queue is unbounded and admission
  is by reservation. Lint and Custom Flow Jobs can now be QUEUED.
- Projects that still set a retired `[jobs]` key stop working until it is
  removed, and Vivado Projects need a `[jobs.seats]` entry. A missing
  `[sandbox].memory` or Seat Cap is a Doctor finding.
- The Campaign lease structure stays: a child lane holds one lease per work
  item, now a Memory Reservation.
