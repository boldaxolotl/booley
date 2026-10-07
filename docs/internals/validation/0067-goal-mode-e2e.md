# Goal Mode live validation (ADR 0067)

Goal Mode remains gated by `BOOLEY_GOAL_MODE_PREVIEW=1`. These checks use
isolated clones and linked worktrees of the public PicoRV32 example inside a
Booley Sandbox. They do not alter the installed framework or the user Project.
Source snapshots are selected through `PYTHONPATH`.

## Phase 2 entry, repeated during Phase 3c

On 7 October 2026, entry checks passed through a real
`mcp.Client(server, mode="2026-07-28")` against main after PR #1301 and
against the Phase 3c source:

- Entry without `work_dir` is refused.
- Entry in the primary checkout is refused.
- Entry in a dirty linked worktree is refused.
- Entry in a clean linked worktree resolves declared lint and simulation
  Targets, persists an active record, and stores working and HEAD protected
  input digests after final revalidation.
- A second entry in an occupied worktree is refused.

The test archives only its own records between runs. It does not exercise
crash recovery at each durable boundary; those cases are covered by the
entry regression tests.

## Phase 3c evidence and routing

The full live run passed on 7 October 2026 through the modern MCP wire.
The server uses Interactive mode and the Sandbox proxy environment. Checks:

- The preview catalog exposes entry, status, built-in Flows, Reviewer, and a
  Project custom tool through the modern MCP wire.
- Declared Goals initially render unmet without evidence.
- Flow and status calls without `work_dir` are refused while a Goal is active.
- Real Verilator lint and Icarus simulation publish met lint and simulation
  evidence; simulation executes the two named tests.
- A Project custom tool runs Icarus elaboration and publishes a met Goal. Its
  fingerprint uses the declared Target when the tool omits `source_target`.
- Jobs persist under their bound record with `binding` and `work_dir`. A
  separate run forces detached simulation, polls without `work_dir`, checks
  the terminal Job at the binding root, and polls it after reconstructing
  the server.
- Newly produced evidence reads fresh. An RTL edit makes it stale, and
  restoring the exact source bytes restores freshness.
- A separate `python -m booley.mcp.server --transport http` process exposes
  status rules and executes Goal lint successfully over HTTP.
- Public `booley_report` accepts the Goal worktree and recovers its completed
  simulation report over the modern MCP wire. Omitted worktrees are refused;
  regression coverage separates two Goal roots from an older Interactive report
  and scopes the available-endpoint hints to the selected record.

- Astra high Reviewer completes an advisory code-style review with two open
  findings. Its `done` Goal becomes met, and all lint, simulation, and
  elaboration evidence stays met and fresh.
- CLI status renders short and long views inside the Goal worktree and lists
  the active record from outside it. CLI and MCP status from a worktree
  subdirectory also keep fresh evidence met. Status rules return the agent rules text.
- With preview disabled, Goal tools are hidden and uncallable, and ordinary
  lint still passes.

The Reviewer regression tests exercise both `done` and `clean` policy with a
provider stub: Goal freshness is read without rewriting other producers'
evidence. Competing-receipt tests cover a waiting Reviewer with an older
loaded receipt and a receipt replaced after its freshness check. An obsolete
loaded receipt triggers a real review; replay never saves over the newer
Criterion. A real MCP-to-CLI replay additionally preserves all Criterion bytes.
The regressions failed before the fixes and pass afterward. Multi-root ambiguity,
cancellation, reconciliation, discarded evidence, corrupted state, and the
protected-input/specification/checkout checks are additionally covered by the
regression suite.

Phase 3c verification: **20,682 passed, 152 skipped** in the complete Python
profile; complete Ruff check and format gates pass; configured Pyright passes.
New modules pass strict checking. Touched legacy modules introduce zero strict
diagnostics against main (90 pre-existing diagnostics removed). An additional
Python 3.11 compatibility run passes **895 tests**, including Goal, Reviewer,
Job, CLI, and Ticket acceptance-ledger golden coverage. CI-recovery coverage
adds a separate Python 3.11 run of **84 tests** for the runner, formatter,
candidate routing, adapter policy, and MCP routing. Changed-line coverage is
**96%** against the required 90%. The Reviewer freshness regression has a
120-second budget after measuring 31.9 seconds on Windows, following the
repository timeout-headroom sizing rule.

## Released Ticket demo

With the preview switch unset, the existing CI-owned demo Ticket fixture passes
`booley run --ticket <fixture slug> --check-ready` and the PicoRV32 demo
contract validator. The isolated upstream checkout is pinned to
`a473fc8fca393771d83b0ffcf0b14db3393339d8`; its public Project is pinned to
`da79489482a7bed69e275ba2c46358ea6636af4d`, as specified by
`.github/contracts/picorv32-demo.toml`. Real lint reports zero warnings, and
real simulation passes both `main` and `axi`. The verifier's explicit checkout
paths point to the isolated fixture rather than the Sandbox's user checkout.
