# Changelog

All notable user-visible changes to Booley are recorded here. Release entries
use stable `MAJOR.MINOR.PATCH` headings so Booley can review an exact upgrade
range from the packaged copy of this file.

Packaged release history starts at 0.2.7. For older changes, see
[GitHub Releases](https://github.com/boldaxolotl/Booley/releases).

## 0.2.12 - 04 SEP 2026

### New features

- B-Wave's `--virtual` option now works across the documented five-command
  matrix. Virtual Signals support fail-fast ordered resolution, `sample`
  trigger and capture expressions, and `value` point evaluation. The CLI,
  public docs, and Coverage Analyst guidance now describe the same behavior.
  ([#320](https://github.com/boldaxolotl/booley/issues/320))

### Quality of life

- `booley auth` now mints Claude credentials inside the validated Session
  Runtime while credential storage and runtime-spec reseeding remain on the
  host. It safely reuses running runtimes and recovers interrupted refreshes.
- Doctor now probes the live MCP catalog through a supported entry point and
  reconciles positively identified stopped VS Code Session Runtimes. Deep
  Doctor resolves only its selected Targets, avoiding unnecessary work across
  large vendored Target matrices.
- Project initialization and Doctor now share a typed, guarded line-ending
  reconciliation path. Repairs revalidate the worktree, index, attributes, and
  Git configuration before changing files, preserving unrelated staged work.
  ([#259](https://github.com/boldaxolotl/booley/issues/259))
- The Session Runtime now includes Claude Code 2.1.259 and Codex CLI 0.153.1.
  Development checks use Ruff 0.16.6.
- Installation guidance now distinguishes first-time setup from upgrades.

### Bug fixes

- Concurrent synthesis runs can no longer delete a shared Target workspace
  while another run is using it. Workspace leases remain held through report
  snapshotting, and timeouts or release failures are reported as infrastructure
  errors.
- Acceptance Journal recovery now tracks and reconciles exact prepared and
  finalized commit identities, preserves finalized commits across interrupted
  cleanup, and safely handles concurrent or symbolic ref movement.
  ([#257](https://github.com/boldaxolotl/booley/issues/257))
- Cycle Count grading preserves both the durable Target identity and callable
  selector across current and baseline evidence, and rejects ambiguous or
  identity-drifting baseline resolution.
  ([#267](https://github.com/boldaxolotl/booley/issues/267))
- Testbench reviews bind their selected simulation Target before inspecting
  candidates, so unrelated or ambiguous Targets cannot redirect the review.
  ([#268](https://github.com/boldaxolotl/booley/issues/268))
- Doctor runs simulation probes from the correct Project directory and opens
  generated contracts transactionally, leaving failed attempts retryable.
  ([#269](https://github.com/boldaxolotl/booley/issues/269))
- Windows initialization again recognizes trusted installed console launchers
  after path normalization without accepting Project-controlled executables.
  ([#313](https://github.com/boldaxolotl/booley/issues/313))
- Managed Project Images now force PEP 517 isolation when installing pinned
  dependencies, restoring packages with legacy source distributions such as
  `cocotb-test` on the Python 3.13 sandbox.
- Native B-Wave metadata now remains correct for both single-root and
  multi-root traces. ([#266](https://github.com/boldaxolotl/booley/issues/266))

### Upgrade notes

- No configuration migration is required. After upgrading, run
  `booley bootstrap`, then use `booley session refresh` for a headless runtime
  or **Dev Containers: Rebuild Container** for a VS Code runtime.

[Full changes from v0.2.11](https://github.com/boldaxolotl/booley/compare/v0.2.11...v0.2.12)

## 0.2.11 - 02 SEP 2026

### Quality of life

- Added the host-owned Project Inventory and `booley projects` command, with
  shared discovery, status, access-grant, and JSON views. Help text now marks
  commands by host or Session Runtime.
- FPGA setup now exposes target-aware Doctor probes and dry-run checks across
  the CLI and MCP surfaces, including clearer Vivado and board-target guidance.
- Updated the Session Runtime to Verible v0.0-4163-g6cce8f19, Claude Code
  2.1.258, and Codex CLI 0.152.1, and refreshed the immutable Python and Docker
  CLI bases used by sidecars.
- Version-change warnings are more prominent during startup, and the demo
  guidance now links directly to feedback.

### Bug fixes

- Host bootstrap now requires Git 2.37.2 or newer, avoiding a Git for Windows
  temporary-name exhaustion failure during large line-ending repairs.
- Host bootstrap now secures the shared Booley configuration directory before
  preparing the PDK cache, so existing caches cannot block Session Runtime
  issuance during an upgrade.
- Flow entry points now validate Target compatibility and identity before EDA
  setup, and implementation comparison evidence retains durable Target
  identities instead of relying on checkout-local objects.
- Schema-4 Cycle Count criteria now join simulation and baseline evidence by
  durable Target identity while retaining the callable selector. This closes a
  later uncovered Simulation consumer of the Target representation fixed in
  [#131](https://github.com/boldaxolotl/booley/issues/131). ([#267](https://github.com/boldaxolotl/booley/issues/267))
- Session Runtime issuance now finds trusted Booley executables installed in
  Python's per-user scripts directory even when that directory is absent from
  `PATH`.
- Skill reconciliation now always deploys packaged skills to `.agents/skills`
  for Codex, while continuing to deploy to a distinct existing `.claude/skills`
  directory.
- `booley session refresh` now journals replacement checkpoints and recovers
  safely after interruption. It parks the target Session Runtime, reconciles
  dependencies, commits forward only after verifying the replacement, and
  retains recoverable state when cleanup cannot finish.
- Source checkouts no longer acquire Project or Stealth policy accidentally;
  repository classification, hook installation, and managed-state placement
  now preserve the source-checkout boundary.
- Session Image provenance is now scoped to the image that actually runs the
  Project, avoiding stale rebuild prompts from unrelated images.
- Stealth projects now keep core projections and FPGA target metadata within
  their protected project state.

### Upgrade notes

- Percentage-based acceptance-criterion values must include an explicit `%`
  suffix, for example `cycle_count_reduce_at_least: 5%`.
- Custom MCP clients must negotiate protocol version `2026-07-28`. Booley's
  built-in Claude Code and Codex configurations are updated automatically.
- Host bootstrap now requires Git 2.37.2 or newer. The complete demo stack
  requires at least 21 GB of free storage.

## 0.2.10 - 01 SEP 2026

### New features

- Bare `booley` now opens the Project's configured Claude Code or Codex chat;
  `booley chat` is the explicit equivalent and `booley --help` remains the
  command reference.
- Added Project-independent `booley bootstrap` for host prerequisites, skills,
  the shared PDK cache, the base Session Image, and global proxy and reaper
  services. `booley init` performs the same reconciliation when needed.
- Added durable, version-aware upgrade review state with scriptable status and
  compare-and-swap acknowledgment. Doctor and Session Runtime startup identify
  pending or stale reviews; `/booley-heal` reviews the exact packaged changelog
  range and acknowledges it only after verification.

### Quality of life

- Updated the Session Runtime to Node.js 24.20.0 and Claude Code 2.1.252.
- Updated the egress proxy, FlexNet relay, and reaper sidecars to Python 3.14.7,
  with tests for CONNECT streaming, relay forwarding, owned-container cleanup,
  unavailable-daemon handling, and container hardening.
- Replaced the unavailable historical OpenROAD package with the official 26Q3
  OCI channel at an immutable digest. Logical and physical synthesis matched
  the 0.2.9 area, utilization, cell count, and netlist results.
- The RISC-V image now pins Spike to a validated upstream snapshot and runs its
  upstream test suite during the build. The PicoRV32 release smoke now tests
  the project's public `main` commit rather than a private CI-only branch.
- Documentation now distinguishes simulation, lint, synthesis, and FPGA
  implementation as separate Booley Flows. It also describes the current stock
  VS Code interface and agent-written development process, and expands the
  Ticket Mode and CI roadmap.

### Bug fixes

- Claude Code and Codex now install their required Linux/x64 native artifacts
  explicitly so optional-package failures cannot leave unusable launchers.
- Updated to cocotb 2.1.0 and its Icarus GPI contract while retaining cocotb
  1.x and 2.0 compatibility. Production-image Icarus and Verilator cocotb flows
  now run in CI.
- Confidential-content pre-push checks now validate destination refs and scan
  changed tree entries. They still cover newly exposed history, merges,
  renames, deletions, and malformed input.
- Runtime-image builds now use bounded pip download timeouts and retries.

### Upgrade notes

- Run `booley bootstrap` once after upgrading. Existing Project
  `booley.toml [interactive]` host-policy fields are retired; move them to the
  host policy file named by `booley init` or `booley doctor`.
- When Booley reports a version change, invoke `/booley-heal` to review these
  notes, repair drift, and acknowledge the upgrade.

## 0.2.9 - 31 AUG 2026

Booley 0.2.9 moves elaboration checks into `sim`, gives synthesis and FPGA
implementation a shared report format, fixes setup, ticket, image, and
diagnostic failures, and updates the runtime toolchain.

### New features

- `synth` and `fpga` write the same versioned `implementation` object with the
  policy-resolved grade, Target identity, quality-of-results metrics, recipe,
  provenance, baseline comparison, cache state, and immutable report and log
  pointers. Both Flows also write atomic stable aliases, numbered reports, and
  live multi-Target progress. [PR #187](https://github.com/boldaxolotl/booley/pull/187)
- `booley flow sim --elab-only` compiles, elaborates, and links an ordinary
  untraced Simulation Target without running tests. `--build-only` is a
  permanent alias, `--standalone` adds the reusable-module sweep, and successful
  builds record `elab_pass_<target>` before simulation. See Upgrade notes for
  migration steps. [PR #185](https://github.com/boldaxolotl/booley/pull/185)
- `booley init` accepts links into another live checkout when the packaged skill
  trees match. Retargeting a managed or equivalent link requires
  `booley init --force`, displays both targets, and preserves unrelated files,
  directories, links, and junctions. [Issue #178](https://github.com/boldaxolotl/booley/issues/178)

### Quality of life

- The ticket-creation approval gate shows each new Target's name, destination,
  and full definition with the Ticket. If validation changes a Target, the gate
  asks for approval again. [PR #170](https://github.com/boldaxolotl/booley/pull/170)
- `booley session refresh` and Session and Project Image builds stream
  redirected output and emit heartbeats during silent stages. Bounded failure
  diagnostics remain available. [Issue #176](https://github.com/boldaxolotl/booley/issues/176)
- Booley keeps user guides under `docs/user/` and implementation material under
  `docs/internals/`, with separate Flow reference and troubleshooting guides.
  Setup directs new Tickets to `booley run` and labels the host check
  `host_prerequisites`. [PR #162](https://github.com/boldaxolotl/booley/pull/162),
  [PR #171](https://github.com/boldaxolotl/booley/pull/171)
- The Session Runtime ships Verible v0.0-4157-gfdbac312, Claude Code 2.1.251,
  and Codex CLI 0.151.0. [PR #189](https://github.com/boldaxolotl/booley/pull/189)

### Bug fixes

- Pulled GHCR sandbox flavors verify parent ancestry by registry digest, retain
  their short tags, and pass an immutable local image ID into Interactive Mode.
  Locally built images still reject stale ancestry.
  [Issue #172](https://github.com/boldaxolotl/booley/issues/172)
- Sealed Tickets resolve criterion Targets in the contract worktree after
  creating it. Contract-only Targets work during fresh setup and resume, while
  destination-only Targets cannot replace the reviewed contract.
  [Issue #173](https://github.com/boldaxolotl/booley/issues/173)
- On Windows, `booley init` applies guarded LF normalization to the project
  checkout and a separately cloned project-data repository. Doctor identifies
  the repository that remains unsafe.
  [Issue #174](https://github.com/boldaxolotl/booley/issues/174)
- Deep Doctor carries its internal Target authority into Lint. The deliberate
  bad-case Target receives a design-failure grade without becoming visible on
  public Target surfaces. [Issue #175](https://github.com/boldaxolotl/booley/issues/175)
- Live checkouts read their own `VERSION` instead of combining stale
  distribution metadata with the checkout's commit identity. Wheels still
  report their owning distribution metadata.
  [Issue #177](https://github.com/boldaxolotl/booley/issues/177)

### Upgrade notes

- Replace `booley flow elab` with `booley flow sim --elab-only`. Move
  `standalone_frontend` from `[flows.elab]` to `[flows.sim]`, remove `elab` from
  Target Doctor lists, and read `sim_<target>.json` with `mode: "elab_only"`
  instead of `elab_<target>.json`. Remove `keep_build_dir`; Simulation now
  retains its untraced build cache after success and failure.

[Full changes from v0.2.8](https://github.com/boldaxolotl/booley/compare/v0.2.8...v0.2.9)

## 0.2.8 - 29 AUG 2026

Booley 0.2.8 makes project setup safer for automation, expands ticket
acceptance-criteria guidance, and corrects Claude runtime and deep Doctor
behavior. Release preflight now catches Docker/demo failures before tagging.

### New features

- [`booley init --skip-credentials`](https://github.com/boldaxolotl/booley/pull/164)
  configures a project's provider and authentication policy without entering or
  storing placeholder credentials. Normal policy validation still applies.
- [Ticket acceptance-criteria guidance](https://github.com/boldaxolotl/booley/pull/163)
  now explains how projects can use area and cycle-count criteria to enforce PPA
  budgets, and coverage and mutation-testing criteria to strengthen
  testbenches.

### Bug fixes

- [Claude agents now use the supported Claude Agent SDK launcher and command construction](https://github.com/boldaxolotl/booley/pull/160),
  including Windows launchers. Authentication and traffic overrides stay scoped
  to the child process.
- [Deep Doctor checks now use the same isolated FuseSoC registry and build context as execution](https://github.com/boldaxolotl/booley/pull/167).
  This prevents generated projections or good firmware from masking bad
  simulation and lint fixtures.
- [The release Docker/demo preflight is now credential-free and candidate-only](https://github.com/boldaxolotl/booley/pull/164).
  It validates the exact release commit before tagging without promoting
  version or `latest` images, using a reviewed, pristine, Doctor-clean PicoRV32
  demo ([#166](https://github.com/boldaxolotl/booley/pull/166),
  [#167](https://github.com/boldaxolotl/booley/pull/167)).

[Full commit history](https://github.com/boldaxolotl/booley/compare/v0.2.7...v0.2.8)

## 0.2.7 - 28 AUG 2026

Booley 0.2.7 adds per-test cycle criteria, directed Target comparisons,
reusable ticket guidance, CLI-readable review packages, and optional Target
cleanup. It fixes image refresh, cancellation, Target resolution, tracing,
completion recovery, and acceptance evidence.

### New features

- Cycle Count criteria can grade each Target and test with absolute limits or
  relative percentage and cycle-delta thresholds. Directed Target pairs run the
  frozen baseline and candidate on different Targets for synthesis, FPGA, and
  cycle comparisons. Existing single-Target syntax still compares the same
  Target on both sides.
  ([Cycle Count criteria](https://github.com/boldaxolotl/booley/pull/105),
  [directed Target pairs](https://github.com/boldaxolotl/booley/pull/113))
- Project-owned Ticket Creation Guidance can describe defaults and policy as
  free-form Markdown. Booley applies the relevant guidance, validates the
  resolved Ticket, records its `on_success` policy, and keeps
  `ticket_defaults.md` as a fallback.
  ([guidance](https://github.com/boldaxolotl/booley/pull/135),
  [defaults and policy](https://github.com/boldaxolotl/booley/pull/125))

### Quality of life

- Ticket Mode now writes a versioned JSON review package for every Ticket that
  reaches review, including runs without a triage agent. CLI agents receive the
  package path through `BOOLEY_RUN_RESULT`, and `booley board prepare-review`
  reports it directly.
  ([review packages](https://github.com/boldaxolotl/booley/pull/112))
- Ticket branches can carry prepared Target contracts for the outer repository
  and an optional paired Project repository. Contracts bind the selected
  branches and source commits before validation or candidate preparation.
  ([prepared Ticket workspaces](https://github.com/boldaxolotl/booley/pull/126))
- `on_success.remove_targets` can remove criterion-bound `.core` Targets and
  owned `tests.toml` tables from an accepted candidate. The sealed contract
  fixes the exact removal set before completion.
  ([Target cleanup](https://github.com/boldaxolotl/booley/pull/137))
- Setup documentation now explains ownership of the stealth Project repository
  and where project-specific state belongs. The README and PyPI description
  describe Booley as an integrated RTL IDE built around agent workflows.
  ([Project repository guidance](https://github.com/boldaxolotl/booley/pull/122),
  [README](https://github.com/boldaxolotl/booley/pull/115))

### Bug fixes

- Normal-use fixes cover Ticket control-artifact round trips, compiler-isolated
  mutation variants, staged Reviewer contracts, focused Cocotb trace
  diagnostics, Doctor fail-path fixtures, clean Developer handoffs, and
  release-matched sandbox images. Mutation evidence names the exact source
  variant, trace requests validate real B-Wave content, and expected Codex
  recovery is no longer a warning.
  ([issue #88](https://github.com/boldaxolotl/booley/issues/88))
- Fixes from the Ibex port require provider and authentication selection before
  init seeds runtime state, allow no-EDA sessions with a read-only authority
  store, keep elaboration on simulation Targets, refresh stale Doctor output,
  use the project root for an unset simulation `run_cwd`, support Ticket-less
  Interactive Reviewer receipts, parse legacy Cycle Count mappings, and prevent
  paired-contract archive collisions.
  ([issue #127](https://github.com/boldaxolotl/booley/issues/127))
- Session Image refresh now uses one provenance lifecycle for pulled, locally
  built, flavored, and Project-derived images. Same-version images from another
  source revision are stale; managed parents rebuild in order; custom external
  images remain unmanaged; and refresh verifies the recreated runtime.
  ([issue #128](https://github.com/boldaxolotl/booley/issues/128))
- Interrupting `booley session enter -- <command>` now cancels and reaps the
  complete container process tree. Renewable job leases, zombie detection, PID
  identity checks, and bounded recovery prevent an abandoned HEAVY slot from
  blocking later work.
  ([issue #129](https://github.com/boldaxolotl/booley/issues/129))
- Verilator tracing now resolves one VCD or native FST recipe across compiler
  flags, runtime objects, harness behavior, transport, and validation. Authored
  FST Targets are no longer partially rewritten into VCD builds.
  ([issue #130](https://github.com/boldaxolotl/booley/issues/130))
- Contracts, prompts, Doctor, and Flows now share one canonical resolved Target
  interface. It separates durable identity from the exact callable selector,
  resolves conditional FuseSoC inputs once, omits fabricated bindings for
  Target-independent criteria, and preserves schema 3 contracts while writing
  schema 4. ([issue #131](https://github.com/boldaxolotl/booley/issues/131))
- Paired-repository completion now validates an immutable plan before mutation
  and records each publication and cleanup step in a durable journal. Retries
  resume from verified commit identities, and cleanup cannot delete a
  destination branch. ([issue #132](https://github.com/boldaxolotl/booley/issues/132))
- Ticket acceptance evidence is now separate from Doctor diagnostics and other
  live runtime state. Completion freezes an accepted snapshot, so cleanup and
  self-tests cannot erase red-green evidence or make completed Tickets display
  `0/N`. ([issue #133](https://github.com/boldaxolotl/booley/issues/133))
- Isolated worktrees can materialize configured submodules from an already
  initialized Project without remotes, SSH, global Git configuration, or shared
  `.git` pointers. The destination gitlinks remain authoritative, and unsafe or
  incomplete local sources fail with actionable errors.
  ([offline submodule setup](https://github.com/boldaxolotl/booley/pull/118))
- CRLF repair now refreshes only normalized tracked index entries, heals stale
  index metadata, and never stages content. Doctor detects status-only failures
  and points Windows users to a fresh-clone repair path.
  ([issue #107](https://github.com/boldaxolotl/booley/issues/107))

### Upgrade notes

- `booley init` no longer selects a default agent provider. Choose the provider
  and authentication method explicitly before init creates or refreshes
  provider-dependent runtime state.
- Recreate Ticket contracts older than schema 3. Schema 3 contracts remain
  readable after the schema 4 Target-interface update.
- New projects use `ticket_creation.md`. Existing `ticket_defaults.md` files
  remain supported as a legacy fallback and do not require immediate migration.

[Full commit history](https://github.com/boldaxolotl/booley/compare/v0.2.6...v0.2.7)
