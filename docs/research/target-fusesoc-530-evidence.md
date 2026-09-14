# Target/FuseSoC separation: #530 implementation evidence

Date: 14 SEP 2026. Related architecture programme: [#279](https://github.com/boldaxolotl/booley/issues/279).

## Revisions and outcome

- Before source and analyzer: `90c27b43d000f157dc967640434132aa71100fa9` (`main` when implementation began).
- After source and analyzer: `354b5e4a98eca477f9a409436568571329e5bb15` (implementation commit).
- Documentation follows in a separate commit so the implementation revision is
  stable and independently reproducible. No production or analyzer changes are
  required by this evidence update.

Two neutral owners replace upward dependencies: `core.build_paths` calculates the
existing checkout-local build directory, and `core.scope_matching` compares
Scope entries. Flows retain execution/leases; Runtime retains Git and dirty-file
policy; FuseSoC retains provenance checks; the standalone hook stays independent.
There are no forwarding aliases or new waivers.

The measured 18-member cyclic group splits into 16 members plus the Target/FuseSoC
pair. Mutual package pairs drop from 13 to 11. D23/D24 enforce the new directions,
and the tightened fixed SCC metadata plus a recombination regression prevents
rejoining the groups. Exact members and pairs appear in both reports below.

## Reproduction

For each revision, archive both its source and its own analyzer:

```bash
snapshot_ref=90c27b43d000f157dc967640434132aa71100fa9 # Repeat with 354b5e4a98eca477f9a409436568571329e5bb15.
snapshot_dir="$(mktemp -d)"
git archive "$snapshot_ref" src/booley tests/architecture | tar -x -C "$snapshot_dir"
python3 "$snapshot_dir/tests/architecture/report.py" \
  --source-root "$snapshot_dir/src/booley" --top 30
```

The tables include all affected modules, including those below the report's top
30. They use `file_fan_out(analyze_imports(source_root))` from the archived
`tests/architecture/import_graph.py`; absent modules are not presented as a
measured zero before extraction.

## Affected caller and owner fan-out

| Module | Before | After |
| --- | ---: | ---: |
| `booley.core.build_paths` | absent | 2 |
| `booley.core.scope_matching` | absent | 0 |
| `booley.targets.target_surface` | 5 | 5 |
| `booley.fusesoc.core_security` | 3 | 3 |
| `booley.runtime.git` | 2 | 3 |
| `booley.harness.scope_policy` | 2 | 3 |
| `booley.dev_support.demo_contract` | 12 | 12 |
| `booley.dev_support.scope_precommit_hook` | 2 | 2 |
| `booley.flows.edam` | 3 | 2 |
| `booley.flows.sim.build` | 10 | 11 |
| `booley.flows.sim.execution.engine` | 25 | 26 |
| `booley.flows.sim.flow` | 48 | 49 |
| `booley.flows.sim.standalone` | 7 | 7 |
| `booley.flows.sim.verilator_coverage_execution` | 15 | 15 |
| `booley.flows.lint.flow` | 16 | 17 |
| `booley.flows.synth.flow` | 35 | 36 |
| `booley.flows.fpga.flow` | 32 | 33 |

The graph loses six module edges and gains fifteen, for a net increase of nine.
This is expected: callers that still configure/execute Flows also import the
shared path owner, while Target/FuseSoC no longer import higher-level modules.
The new build-path owner imports the existing neutral checkout and Project-name
mechanisms; the pure Scope owner imports only the standard library.

Removed edges:

```text
booley.dev_support.demo_contract -> booley.runtime.git
booley.flows.edam -> booley.runtime.checkout_role
booley.flows.sim.standalone -> booley.flows.edam
booley.flows.sim.verilator_coverage_execution -> booley.flows.edam
booley.fusesoc.core_security -> booley.runtime.git
booley.targets.target_surface -> booley.flows.edam
```

New edges point to `core.build_paths` or `core.scope_matching`, apart from the
build-path owner's two dependencies within `core`. The default report below
also captures all named composition hotspots; increases are explicit rather
than hidden behind re-exports.

## Behavioral verification

The regression coverage preserves exact config sanitization/fallback, Flow and
variant identity, checkout rejection, per-worktree separation despite ambient
Project overrides, and Target detail's cheap/resolved payload/error behavior.
Scope cases cover literal/prefix/glob matching, new tags, exact and normalized
wildcard cases, deletion policy and actual canonical script-path classification.
Existing EDAM confinement, core structural/provenance, standalone-hook, Git
staging, Flow and golden tests remain in the verification set. CI classification
now retains exhaustive recovery selection for matcher-only changes.

From the implementation worktree, with the existing test virtualenv on `PATH`:

```console
pytest -q -rs -n 4 tests/architecture/ tests/core/ tests/test_target_surface.py tests/test_target_catalog.py tests/test_fusesoc_registry.py tests/test_core_security.py tests/test_checkout_role.py tests/harness/test_targets_cmd.py tests/harness/test_utils.py tests/harness/test_git_utils.py tests/harness/test_scope_policy.py tests/dev_support/test_scope_precommit_hook.py tests/ci/test_picorv32_demo_contract.py tests/docker/test_sandbox_dockerfile.py tests/ci/test_change_classifier.py tests/flows/test_edam.py tests/flows/sim/ tests/flows/lint/ tests/flows/synth/ tests/flows/fpga/ tests/golden/
ruff check src/ tests/
ruff check .
ruff format --check .
pyright --pythonpath <test-virtualenv>/bin/python
```

Results: **2,424 passed, 8 skipped**. Five skips require the native B-Wave binary;
three are opt-in Vivado profile characterization. Both Ruff checks and formatting
pass. Repository-pinned Pyright 1.1.411 reports zero errors and warnings. An
initial broader test run failed only because the shell lacked the `python`
command used by an MCP subprocess; the corrected virtualenv PATH run above passed.
The unchanged-base architecture/behavior baseline was 242 passing tests and a
clean Ruff check.

These are focused regression and architecture results, not a full release or
commercial-EDA qualification. New tests do not claim to add universal rejection
of external generator scripts or enforce read-only mounts; those semantics are
unchanged by #530.

## Before report

```text
Parsed Python modules: 492
Normalized dependency facts: 2424
Unique normalized edges: 1989

Cyclic top-level package groups:
- booley.agent_workspace, booley.audit, booley.bwave, booley.config, booley.criteria, booley.dev_support, booley.eda, booley.feedback, booley.flows, booley.fusesoc, booley.harness, booley.mcp, booley.projects, booley.review, booley.runtime, booley.specialists, booley.targets, booley.ticket_board

Mutual top-level package pairs:
- booley.bwave <-> booley.flows
- booley.config <-> booley.eda
- booley.dev_support <-> booley.runtime
- booley.eda <-> booley.runtime
- booley.feedback <-> booley.harness
- booley.flows <-> booley.targets
- booley.fusesoc <-> booley.runtime
- booley.fusesoc <-> booley.targets
- booley.harness <-> booley.mcp
- booley.harness <-> booley.runtime
- booley.harness <-> booley.ticket_board
- booley.mcp <-> booley.specialists
- booley.mcp <-> booley.ticket_board

Top 30 file fan-out:
- booley.harness.doctor: 66
- booley.harness.booley: 57
- booley.flows.sim.flow: 48
- booley.harness.developer: 44
- booley.harness.init_cmd: 39
- booley.flows.synth.flow: 35
- booley.mcp.server: 33
- booley.flows.fpga.flow: 32
- booley.flows.sim.execution.engine: 25
- booley.specialists.mutation_tester: 25
- booley.ticket_board.operations: 25
- booley.harness.setup.intake: 24
- booley.ticket_board.cli_handlers: 24
- booley.harness.setup.workspace: 22
- booley.ticket_board.io: 21
- booley.flows.endpoint_acceptance: 19
- booley.ticket_board.review_lifecycle: 19
- booley.ticket_board.review_preparation: 19
- booley.flows.base: 18
- booley.specialists.specialist: 18
- booley.flows.lint.flow: 16
- booley.harness._ticket_ops: 16
- booley.ticket_board.flow_execution: 16
- booley.ticket_board.workspace_ops: 16
- booley.flows.sim.verilator_coverage_execution: 15
- booley.ticket_board.basis_refresh: 15
- booley.specialists.reviewer: 14
- booley.ticket_board: 14
- booley.ticket_board.criteria_acceptance: 14
- booley.flows.sim.coverage_flow_context: 13

Named composition hotspot fan-out (diagnostic only):
- booley.harness.doctor: 66
- booley.harness.booley: 57
- booley.harness.init_cmd: 39
- booley.harness.developer: 44
- booley.flows.sim.flow: 48
- booley.flows.synth.flow: 35
- booley.mcp.server: 33
- booley.flows.fpga.flow: 32
- booley.specialists.mutation_tester: 25
- booley.specialists.coverage_analyst: 13
```

## After report

```text
Parsed Python modules: 494
Normalized dependency facts: 2433
Unique normalized edges: 1998

Cyclic top-level package groups:
- booley.agent_workspace, booley.audit, booley.bwave, booley.config, booley.criteria, booley.dev_support, booley.eda, booley.feedback, booley.flows, booley.harness, booley.mcp, booley.projects, booley.review, booley.runtime, booley.specialists, booley.ticket_board
- booley.fusesoc, booley.targets

Mutual top-level package pairs:
- booley.bwave <-> booley.flows
- booley.config <-> booley.eda
- booley.dev_support <-> booley.runtime
- booley.eda <-> booley.runtime
- booley.feedback <-> booley.harness
- booley.fusesoc <-> booley.targets
- booley.harness <-> booley.mcp
- booley.harness <-> booley.runtime
- booley.harness <-> booley.ticket_board
- booley.mcp <-> booley.specialists
- booley.mcp <-> booley.ticket_board

Top 30 file fan-out:
- booley.harness.doctor: 66
- booley.harness.booley: 57
- booley.flows.sim.flow: 49
- booley.harness.developer: 44
- booley.harness.init_cmd: 39
- booley.flows.synth.flow: 36
- booley.flows.fpga.flow: 33
- booley.mcp.server: 33
- booley.flows.sim.execution.engine: 26
- booley.specialists.mutation_tester: 25
- booley.ticket_board.operations: 25
- booley.harness.setup.intake: 24
- booley.ticket_board.cli_handlers: 24
- booley.harness.setup.workspace: 22
- booley.ticket_board.io: 21
- booley.flows.endpoint_acceptance: 19
- booley.ticket_board.review_lifecycle: 19
- booley.ticket_board.review_preparation: 19
- booley.flows.base: 18
- booley.specialists.specialist: 18
- booley.flows.lint.flow: 17
- booley.harness._ticket_ops: 16
- booley.ticket_board.flow_execution: 16
- booley.ticket_board.workspace_ops: 16
- booley.flows.sim.verilator_coverage_execution: 15
- booley.ticket_board.basis_refresh: 15
- booley.specialists.reviewer: 14
- booley.ticket_board: 14
- booley.ticket_board.criteria_acceptance: 14
- booley.flows.sim.coverage_flow_context: 13

Named composition hotspot fan-out (diagnostic only):
- booley.harness.doctor: 66
- booley.harness.booley: 57
- booley.harness.init_cmd: 39
- booley.harness.developer: 44
- booley.flows.sim.flow: 49
- booley.flows.synth.flow: 36
- booley.mcp.server: 33
- booley.flows.fpga.flow: 33
- booley.specialists.mutation_tester: 25
- booley.specialists.coverage_analyst: 13
```
