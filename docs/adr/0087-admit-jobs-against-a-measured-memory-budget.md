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
  counts come from the container's cores and the running Jobs. Verilator
  builds and OpenROAD take the Job's share; Yosys and Vivado use their own
  defaults (a Yosys thread count changes the netlist's byte order, which
  would defeat OpenROAD reuse).
- The Memory Budget is always computed from the container's memory limit
  (`[sandbox].memory`, else the host memory the container sees) minus a
  reserve for the agent, the MCP server and the Dashboard. Doctor warns when
  `[sandbox].memory` is unset. There is no override: one that replaces the
  computed budget subtracts no reserve, so the natural setting lets Jobs eat
  the memory the agents are using and invites OOM kills.
- Swap is explicit. With `[sandbox].memory` set, the Sandbox gets a
  `--memory-swap` limit: `[sandbox].swap` beyond the memory, else a quarter of
  `memory`, capped at the swap the Docker host has (read from the host, or the
  Docker Desktop VM on Windows). Docker's default (swap equal to memory) never
  applies. The OOM kill, and so the single requeue, happens at memory plus
  swap. Doctor warns when the host cannot back the setting. With a swap
  allowance (read from the cgroup, capped at the host's swap) swap absorbs a
  short overshoot, so the budget holds back nothing beyond the reserve and
  memory estimates use a margin of 1.10. Without one the budget holds back a
  quarter of the memory instead, the room the default swap gives, and
  estimates keep 1.25, so a Sandbox without swap runs fewer Jobs.
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

- A phase's memory estimate is the maximum over retained records of its key
  (resident memory plus what the Job swapped out, as a Job that swapped records
  a resident peak below what it needed), times the swap-dependent margin. The
  maximum covers cold and warm compiler-cache builds without tracking cache
  state; too high an estimate costs only parallelism. Wall-clock estimates,
  used only for jump-ahead, use their own values and the no-swap margin.
- RTL changes are ignored: keys carry no input fingerprint. The agent edits
  RTL before nearly every run, so treating a changed input as unknown would
  serialize it. A large jump is caught by the OOM guard and requeue.
- Keys include the Test Variant but not the seed, so baseline runs of older
  revisions share keys with current runs. Records also store source input
  size as data for testing size-based scaling later; it is not policy,
  because source bytes track netlist size poorly.
- A key keeps every record of its newest Jobs, and `booley resources` counts
  Jobs, not records. One Job may measure a phase several times (a setup per
  simulator process, each Pre-Sim Command firing, the seeds of one Variant);
  records stay per command so estimates see each, and counting Jobs keeps run
  counts and retention true. A Simulation Record's execution records its
  repeat of the setup planning already measured only when the repeat fails.
- No history: borrow the largest estimate for the same Target and phase,
  then for that phase anywhere in the Project; with neither, the Job runs
  alone. (Always running unknown Jobs alone would serialize a fresh
  Project's first regression.)
- Pre-Sim Commands are a measured phase of their own, `sim_pre_run`, keyed per
  Target like preparation and build and recorded for every firing. A Job or
  Simulation Record item that runs them reserves the larger of their estimate
  and its simulation's. They never borrow across Targets: with no record for
  the Target, their estimate is the Target's build estimate, the safe side,
  since they may build as much as the build does. The build phase ends once
  the build settles, with or without Pre-Sim Commands, so the reservation
  drops to the pending phases.
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
  Job's tools rank first (it is the Job most likely to have tipped the
  container), then older Jobs' by age, then the Developer Agent's, the MCP
  server's and the Dashboard's. The Job's coordinator, the process holding
  its claim, ranks just below its own tools, so a kill takes a tool and leaves
  the coordinator able to report it or requeue the Job. The levels, the gap
  and the refresh interval live in the code, with a test that checks the
  ranking against the kernel's formula.
- The ranking is a bound, not a guarantee. The kernel kills the highest
  `1000 x share + oom_score_adj`, share being a process's share of the
  Sandbox's OOM limit, so a higher-ranked process dies first only while the
  other's share exceeds its own by less than their level difference over 1000.
  The newest Job's tools die before an older Job's processes while the older
  uses less than half the limit more. Older Jobs' tools die in age order while
  within 5 % of the limit of each other (2.5 % for a coordinator against the
  next older Job's tools; Jobs with five or more newer ones share one level).
  A Job's processes die before the agent's while the agent uses less than
  about a quarter of the limit more (27.5 % against a coordinator on the
  lowest level). A coordinator dies after its own tools while it uses less
  than 2.5 % of the limit more. Beyond a bound the larger process may die
  first. A stronger guarantee is not possible: every level must fit between
  the agent's 0 and the kernel's maximum 1000, since going below 0 needs
  privilege and would rank Jobs below the agent, so widening one gap narrows
  another, and an unconditional order needs a 1000-point gap at every step.
  The larger process is the likelier over-user, so the bound is accepted.
  `tests/runtime/test_oom_priority.py` checks these reference sizes against
  the kernel's formula; change a size there and here together:

  | Claim | Reference sizes |
  |---|---|
  | Newest Job's tools before the next older Job's | newest 0.1 %, older 45 % |
  | Newer before older Job's tools, among older Jobs | newer 0.1 %, older 4.5 % |
  | Job processes before the agent | Coordinator on the lowest level 0 %, agent 12 % |
  | Coordinator below its own tools | coordinator 2 %, tool 0 % |

- Raising `oom_score_adj` needs no privilege, so the Sandbox hardening is
  unchanged. The ranking assumes the container sets neither a starting score
  nor `memory.oom.group`, and that tools stay in the coordinator's process
  tree; where `/proc` is missing it does nothing.
- Booley does not add a memory-pressure watcher: it is new always-on
  machinery, and the kernel's priority already gives "newest first, never the
  agent" within those bounds. It would be added only if OOM kills turn out to
  be common.
- A Job killed by the OOM killer at the Sandbox's limit is requeued once, to
  run alone, with its estimate raised to at least its observed peak; a second
  kill while alone fails it. Results and evidence come only from the final
  attempt. A Job bound to a Goal is never requeued as a whole, so its
  evidence comes from its only attempt.
- The two kinds of kill are told apart by the cgroup's `oom` event count,
  read in one snapshot with `oom_kill`: `oom` counts the times the Sandbox
  reached its own limit, `oom_kill` every process an OOM killer took, the
  host's too. An own kill while `oom` did not rise is a host kill: the host
  ran out of memory while the Sandbox stayed within its limit. Running alone
  cannot relieve pressure from outside the Sandbox, so a host kill is never
  requeued; the result leads with a line naming host memory, the Sandbox
  limit, and the remedy (free host memory or lower `[sandbox].memory`), and
  the Flow's own error follows it. The Resource History records outcome
  `host_oom_kill`, a lower bound like `oom_kill`. A Sandbox with no memory
  limit never reaches one, so every own kill in it is a host kill. An
  unreadable `oom` count proves nothing: the kill is handled as a Sandbox
  limit kill.
- The OOM-kill counter is container-wide, so a rise alone cannot say which
  Job was hit. A command counts as OOM-killed only when the counter rose while
  it ran, it exited nonzero, and a process of its own tree died by a SIGKILL
  Booley did not send. A timeout, a cancellation or an unreadable counter
  never marks a kill. The signs are the root's SIGKILL, a Booley launcher's
  report, and a kill line in the command's output or, for Vivado, in the run
  scripts' own logs.
  Detection is deliberately conservative: a kill hidden behind a wrapper that
  reports none of these (a FuseSoC generator names no signal) is a plain
  failure and is not requeued, as is a kill in a Pre-Sim Command or a
  mutation run. A Pre-Sim Command is measured but not blamed for a kill: its
  failure stays the hook's result, as the Simulation's tests see it.

### Simulation Records

Every work item of a simulation or coverage record is admitted on its own,
with its own key. The Flow's reservation covers preparation and build, then
drops to zero, and admission decides how many items run at once. An item
reserves the larger of its Pre-Sim Commands and simulation estimates, and its
build too when it builds inside itself (a private build, a coverage aggregate
or a shared build not yet settled). Each item ranks for the OOM killer as a
Job newer than its Flow, so the newest item's
simulator ranks first, within the bounds of the Out-of-memory guard. An item's
own OOM kill at the Sandbox's limit stops only that item: it is requeued once,
to run alone after the running items finish, and its second failure is its
result. A host kill of an item is never requeued. A kill of an item running
under the Flow's own claim requeues the whole Flow Job instead.

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
