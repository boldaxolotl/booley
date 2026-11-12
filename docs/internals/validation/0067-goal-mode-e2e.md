# Goal Mode live validation (ADR 0067)

Dated evidence record of the live end-to-end checks run at the exit of each
[ADR 0067](../../adr/0067-replace-ticket-mode-with-goal-mode.md) rollout phase
(Phases 2 to 6) on 7 and 8 OCT 2026. Each check drove the real Booley MCP
server, real EDA tools, and the real Reviewer Specialist against isolated
PicoRV32 fixtures in a Sandbox. Read it to find out which Goal Mode behaviors
were proven against real tools, on which source, and which were covered only by
regression tests. It is not updated: at the time, Goal Mode was hidden behind
the `BOOLEY_GOAL_MODE_PREVIEW=1` rollout switch, which Phase 7b (PR #1321)
removed. The Phase 0 spikes have their own records
([elicitation](0067-goal-mode-elicitation.md),
[session identity](0067-goal-mode-session-identity.md),
[Booley Directory](0067-goal-mode-project-dir.md)). For Goal Mode as it works
now, see [GOAL-MODE.md](../../user/GOAL-MODE.md) and the
[Goal Mode glossary](../../../src/booley/goals/GLOSSARY.md).

## How the checks ran

- **Fixtures.** Isolated clones and linked worktrees of the public PicoRV32
  example inside a Booley Sandbox. The installed framework and the user's
  Project were never touched; source under test was selected with
  `PYTHONPATH`.
- **Frozen source.** From Phase 4 on, the source under test was frozen before
  the live run and identified by a SHA-256 aggregate over its runtime files.
  A fix to the code produced a new frozen version (v1, v2, ...), and the live
  checks were repeated on it.
- **Who ran them.** The maintainer ran the live checks, separately from the
  coding agent that wrote the change and its regression tests.
- **Ticket Mode regression.** With the rollout switch unset, every phase also
  reran the released Ticket demo: `booley run --ticket <fixture slug>
  --check-ready` and the PicoRV32 demo contract verifier. The upstream checkout
  was pinned to `a473fc8fca393771d83b0ffcf0b14db3393339d8` and the public
  Project to `da79489482a7bed69e275ba2c46358ea6636af4d`, as
  `.github/contracts/picorv32-demo.toml` specified then. Real lint reported
  zero warnings and real simulation passed both `main` and `axi`. These checks
  passed in every phase.
- **Raw evidence.** JSON results, logs, and freeze manifests stayed in
  temporary directories on the validating host and are not in the repository.
  Test-suite, coverage, and type-check results, and the CI fixes made along
  the way (Windows shard deadlines, timing-model data, line-ending fixtures),
  are in each phase's PR: #1302 (Phase 3c), #1303 (Phase 4), #1305 (Phase 5),
  #1313 (Phase 6). None of those CI fixes changed runtime code.

## Phase 2: entry

Checked on 7 OCT 2026 through a real `mcp.Client(server, mode="2026-07-28")`,
against main after PR #1301 and again against the Phase 3c source:

- Entry without `work_dir` is refused.
- Entry in the primary checkout is refused.
- Entry in a dirty linked worktree is refused.
- Entry in a clean linked worktree resolves the declared lint and simulation
  Targets, persists an active record, and stores working and HEAD protected
  input digests after final revalidation.
- A second entry in an occupied worktree is refused.

Crash recovery at each durable boundary was not exercised live; the entry
regression tests cover it.

## Phase 3c: evidence and routing

Checked on 7 OCT 2026 through the modern MCP wire, with the server in
Interactive Mode and the Sandbox proxy environment, then repeated after PR
#1302 merged at `9d9e861a3aa35aaa930bd0c9f1689a3954fa878f`: **18/18 checks
passed**.

- The preview catalog exposes entry, status, built-in Flows, the Reviewer,
  and a Project custom tool.
- Declared Goals render unmet before any evidence.
- Flow and status calls without `work_dir` are refused while a Goal is active.
- Real Verilator lint and Icarus simulation publish met lint and simulation
  evidence; simulation runs the two named tests.
- A Project custom tool runs Icarus elaboration and publishes a met Goal. Its
  fingerprint uses the declared Target when the tool omits `source_target`.
- Jobs persist under their bound record with `binding` and `work_dir`. A
  forced detached simulation is polled without `work_dir`, found terminal at
  the binding root, and polled again after the server is rebuilt.
- New evidence reads fresh; an RTL edit makes it stale; restoring the exact
  source bytes makes it fresh again.
- A separate `python -m booley.mcp.server --transport http` process serves
  the status rules and runs Goal lint over HTTP.
- `booley_report` accepts the Goal worktree and returns its completed
  simulation report; a call without a worktree is refused.
- The `gpt-6-astra` Reviewer at high effort completes an advisory code-style
  review (124.5 seconds, two minor findings). Its `done` Goal becomes met and
  all lint, simulation, and elaboration evidence stays met and fresh.
- CLI status renders short and long views inside the Goal worktree and lists
  the active record from outside it. CLI and MCP status from a worktree
  subdirectory keep fresh evidence met.
- With the switch off, Goal tools are hidden and uncallable and ordinary lint
  still passes.

## Phase 4: approved Goal changes

Checked on 7 OCT 2026 through the real modern MCP application on frozen
source aggregate
`017a6595a92c1a4a3b77cc43d4319cc94158b65f0f8d797ba96039f3044f1e66`
(13 assertions, all passed):

- **Cycle Count relaxation without a rerun.** A real Icarus run measured
  481,039 cycles against a maximum of 1. Approving the proposal by its exact
  ID relaxed the maximum to 600,000. The cycle Goal and its unchanged
  simulation sibling became met and fresh. The derived evidence kept the
  original source stamps and producer time and linked the proposal, the
  ChangeEntry, and the original evidence. No simulator rerun and no Git
  commit occurred.
- **Coverage waiver.** Approving a branch-coverage Waiver Candidate by exact
  ID installed waiver files in the configured RTL anchor; a toggle candidate
  was rejected. The next real Verilator run consumed the approval: branch
  coverage rose from 50% (2 eligible, 0 waived) to 100% (1 eligible,
  1 waived). Toggle coverage stayed 58.33% with 0 waived, so the coverage Goal
  correctly stayed unmet against its 100% toggle floor. Files were installed
  without a commit. Only the approved proposal produced a ChangeEntry; the
  rejection was stored separately.

These decisions used the agent-recorded fallback, quoting the standing
verification instruction. They prove that exact saved proposals are applied
and rejected, not that a user saw a form or that the user's identity was
authenticated; the [elicitation spike](0067-goal-mode-elicitation.md) holds
the client UI evidence.

The live checks were repeated on each later frozen source. Every run gave the
same result: cycle status **2/2 met and fresh** with unchanged Goal bytes and
the original single Job; one new real Verilator run consumed the existing
branch approval (**100% branch, 1 waived**; toggle **58.33%, 0 waived**,
unmet); the producer's semantic policy digest matched the strict reader; and
no proposal, decision, audit file, policy, or Git HEAD changed.

| Source | Aggregate | New Verilator run | Fix it carried |
| --- | --- | --- | --- |
| v3 | `fd3e8724e3e07476526a71f8a3f24e942e5d82f0be7df701c864e403e2ebe86e` | `sim/4` | first-round review fixes |
| v4 | `87a20ac49ffb1440c0f558fe420d8a6cf1b2faceb459a1415ecfceddcc96b320` | `sim/5` | recovery binds the whole prepared transaction to the decision |
| v5 | `7a6a47b87e02ca887018c25e780a1ac0e6c4f605a888de5b67af3cf3eb7f8eb7` | `sim/6` | mutation Goal relaxation uses the producer's effective floor |
| v7 | `9a26eb6056a06d1b7de70dd81f5659180802337cfeb5986933a5c6bb99995930` | `sim/8` | a `clean` to `done` Reviewer change derives from the real receipt |

Each aggregate covers 751 runtime files. The fixes, in plain words:

- **Transaction authority (v4).** Recovery had not tied the complete prepared
  transaction to a separate durable decision. The decision now records a
  digest of the whole transaction, and recovery checks it before publishing.
  Unresolved captures from before the fix fail closed.
- **Mutation floor (v5).** A mutation Goal with no explicit detection floor
  was compared with None during relaxation. Fixed counts now use the
  producer's effective floor; an unresolved auto-scaled floor raises a clear
  policy error.
- **Reviewer receipt (v7).** Real `clean` receipts use
  pending/resolved/observations, which a synthetic fixture had hidden. A
  `clean` to `done` change now derives completion from the original receipt
  and keeps its observation, receipt ID, and timestamp.

## Phase 5: finish, summary publication, retry, and abandonment

Checked from 7 OCT 2026 in a new isolated non-Stealth PicoRV32 fixture with
configuration, cores, design sources, and firmware committed before entry. The
fixture used Booley's own Git-ignore and scanner helpers but not a full
`booley init` run. Each run passed six compound checks:

- Goal entry.
- Real Verilator lint, Icarus simulation (both required tests), and the
  configured `gpt-6-astra` high-effort RTL review (two minor findings) bring
  all three Goals to met and fresh.
- `goal_finish` publishes a summary commit whose sole parent is the validated
  pin `fc54360efb94e97d950cc4e72153e0ba3c8228d0` and whose only change is one
  `.booley/goals/history/<record>.md` file at mode `100644`. The rest of the
  tree and the primary checkout's HEAD and index stay unchanged. The frozen
  JSON, chat, and HTML package keep the exact selected evidence and the open
  findings, without the Ticket approval banner.
- An identical saved-result retry returns the original response and creates
  no second commit.
- Re-entry starts from the summary commit, and retrying the old operation
  leaves the new record unchanged.
- Abandonment closes the replacement Goal and keeps its branch and files.

| Source | Runtime files | Aggregate or freeze digest | Summary commit | Lint / sim / review / finish (s) | What changed |
| --- | ---: | --- | --- | --- | --- |
| v1 | 762 | `04b01fb2c02e67bdb582fa83cbb95ec3d0996078dbedeab734ae5ea91c9f30fb` | `c546f7b83da45b0072e66ce5542b6fee07800edc` | 3.3 / 41.8 / 66.2 / 1.9 | first implementation |
| v3 | 764 | `b98d25b63dd1a4f95337fffff94c7560595bbde90cfe0d8ae149915a33a1ce11` | `33cc37acb30fbe6ad11304c38e7aca43aef9bd91` | 3.1 / 51.8 / 111.0 / not recorded | first-round review fixes |
| v4 | 764 | `971813590097936dee5fb9a4e374c0210b2bc70042aa939f79689e72a2848725` | `8d6d677174c92f8f708f68122df6bfcad798b485` | 3.6 / 53.4 / 62.4 / 3.5 | different path spellings of one root keep their Target tests and labels |
| v6 | 772 | `a2bef79e134a51579106593b1b9e97d47393486d9a46be78c7f764b0a98f6e9f` | `53d2352a2ffe58903043fce5bc358ec37100d471` | 3.5 / 49.8 / 51.4 / 1.3 | reviewed recovery; submodule-aware committed input views |
| v7 | 772 | `8b1873c29c8a42f2bc760336716b6c4290f7634e718987ef58448e7edeae86e0` | `55cf5025a59a7d4a9ec358a9de4fdc292eaabd92` | 3.0 / 40.2 / 50.1 / 1.4 | second-round review corrections |
| v9 | 773 | freeze `cc09c71b25b4f454122311c66918669f5efe4cc2316359677d9184a86fb39d40` | `0ad688fe0629e6c8f90e15368f960aa37c85c0ae` | not recorded | adversarial-review repairs |
| v10 | 773 | freeze `0a81dc770db618ee979e133040db4a5bb3ad79265d704c632a0d0a18987b069e` | `668430fed730fc1c7339ee239e2391a5ae63efdc` | 2.8 / 50.4 / 77.8 / 1.1 | rebase onto main `5765840e7` |
| v12 | 773 | `c6030367d7fe0d72d4aa857486d7677f373f7c7e08b1fbdbef4266251b93c061` | `663254cce73e1e662b1a40c42bc40db1d586afe2` | 3.7 / 52.9 / 76.4 / 1.5 | Windows fixture repairs; rebase onto main `57eeb7ceb` |

Source v2 changed only the packaged changelog copy and was compared to v1
rather than rerun. Alongside the live runs, the maintainer wrote independent
controls against real Git: on v3, a tampered saved publication path was
refused before any external write, a stat-only change to a tracked file left
the index unchanged, and a Stealth finish preserved index bytes, HEAD, and
object count. On v6, v7, and v9, 19 such recovery controls passed (v4-era
upgrade fixtures, publication across filesystems, foreign index locks,
filter and replace immunity, failed-abandonment recovery), and v9 added 53
recovery and export regressions.

Limits recorded on the final source:

- Both selected versioned repositories must be clean as whole repositories.
- A custom Git filter, Git LFS, or an unsupported encoding anywhere in a
  selected repository refuses finish with an actionable message; Booley never
  runs an external transformation or substitutes a pointer for design bytes.
- A baseline submodule removed or moved by the Goal Branch is read from its
  original pinned objects in the retained local Git cache, never fetched.
  Missing objects, ambiguous or unsafe mappings, and linked cache paths
  refuse.
- Known limits: large-repository lock and export limits, unavailable original
  caches, and unsupported hard links.

Not exercised: native bind-mount identity, native Windows, a user answering a
form in a real client, a full host `booley init`, and two real agent client
sessions.

## Phase 6: sessions, Dashboard, and Doctor

Checked on 8 OCT 2026 against source `20b067198` (published as `70a9d69b6`; both
have Git tree `13347bc0e4aa284f1e629c6adb3333fabdefed67`). The real HTTP MCP
server ran in a throwaway container from the local `booley-sandbox:latest`
image, which gave a real PID namespace, Verilator 5.052, and Icarus, with
source mounted read-only. The fixture was a new non-Stealth Git repository
derived from the Ticket Mode smoke fixture, with a Verilator lint Target and a
long-running Icarus Target.

Separate SDK client processes over loopback HTTP stood in for agent tabs.

- **Session registry.** In five runs, two processes started at the same moment
  always produced two complete `pid:` rows. After one was killed, server
  maintenance removed its row 34 seconds later and kept the survivor. On the
  host, with about 617 processes, back-to-back calls from two processes also
  produced complete rows, although peer lookups exceeded the 50 ms response
  budget. Two raw HTTP processes sharing one MCP session id, each opening a
  new TCP connection per call, were each attributed to their own process. One
  Codex-style process with two `_meta.threadId` values produced two thread
  rows, and a call without a thread id used the worktree key. Goal entry under
  that fallback reported that attribution was unavailable. A call without
  `work_dir` did not move a process row.
- **Shared-worktree warning.** Goal entry, Goal status, and Goal-bound lint and
  asynchronous simulation replies carried the warning when another live
  process had called the same worktree. Interactive Flow, catalog, and
  validation replies did not.
- **Dashboard.** Real Verilator lint passed in 0.8–3.0 seconds and Icarus
  simulations in about 2 seconds. The Dashboard model listed Jobs from three
  Goal roots and the Interactive root, each identified by root and run ID; one
  model read took 0.42 seconds. During a long simulation the Job detail showed
  CPU at about 110% of one core, elapsed time, the EDA tool, and RSS; the
  Sandbox line showed CPU as a share of 24 CPUs. After five reads and a
  Textual pilot, all 5,259 fixture paths, including `.git` and every worktree,
  were byte-identical.
- **Doctor.** Inside the container, Doctor reported `goals.worktree-missing`
  and `goals.quiet-session` for a Goal whose checkout was deleted, and
  `goals.quiet-session` once a Goal's last live session was pruned. Host
  Doctor reported the checks unavailable. With a Goal worktree deleted,
  `goal_status` from other worktrees returned a missing-worktree warning
  rather than an error.
- **`booley dashboard` and its VS Code task.** A second instance reported the
  open view. Task reconciliation created the folder-open task, kept comments,
  unknown fields, and user edits, restored the user's `tasks.json` bytes
  exactly through four enable/disable cycles, and removed its own `.vscode/`
  and exclude entries.

Not exercised: real Claude Code or Codex clients, VS Code running the task
automatically, the full init and Sandbox Issuance path, and native Windows.
The review round that followed bounded how much Job history the Dashboard
reads (current bounds:
[ADR 0068](../../adr/0068-booley-dashboard.md#amendments-2026-10-11)) and left
one known latency risk: expensive Git or evidence work while the Dashboard
holds a Goal record lock.
