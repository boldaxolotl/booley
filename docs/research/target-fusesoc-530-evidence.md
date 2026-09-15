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

## Merge-readiness refresh: 14 SEP 2026

The initial confidentiality CI failure rejected commit author/committer
identities against the older allowlist. The approved repair is already on
`main`; merging current `main` into this branch includes that repair without
altering the extraction or adding a policy exception.

Rechecked exact source/analyzer archives at base
`35062ddae5a60656dcd1b982904c214c74a7bca2` and refreshed implementation
`1a98ec85966f247a007fb853db07423d10438db0`:

| Diagnostic | Before refresh base | Refreshed implementation |
| --- | ---: | ---: |
| Python modules | 494 | 496 |
| Located dependency facts | 2,458 | 2,467 |
| Unique normalized edges | 2,017 | 2,026 |
| Cyclic group sizes | 18 | 16 and 2 |
| Mutual package pairs | 13 | 11 |

The exact cyclic groups, mutual pairs, all affected caller/owner fan-out, and
named composition hotspots match the initial before/after reports above.
The additional modules/edges are Ticket Board changes from `main`; their
fan-out changes are identical on both sides of this new comparison.
Reproduce using the same archive commands above with these two revisions.

Refresh validation: 467 passing tests covering architecture, the new core
modules, Target detail/catalog presentation, core security, Scope/Git, the
standalone hook and demo contracts; `ruff check src/ tests/` passed. GitHub CI
will rerun on the pushed candidate before the authorized Mergify queue request.

## Cumulative integration with #531: 14 SEP 2026

Mergify dequeued #535 because #534 landed conflicting direction-rule identifiers
and SCC documentation. Resolution preserves #531's D23–D25 and numbers the
Target/FuseSoC rules D26/D27. No production changes were needed for resolution.
The exact SCC baseline now contains the measured 11-member execution group and
the separate Target/FuseSoC pair. Regression coverage also rejects Audit, Config,
EDA, Projects, and Review rejoining execution. The original 12-member cumulative
projection predates the Ticket changes that also release Projects.

- Before source and analyzer: `9b25274683dfc408eb17582501bb5d403a0d3f64` (main including #531).
- After source and analyzer: `4a5b552dcb30019f2141217bbff0f5eec0923f2e` (resolved integration).
- Both reports below were generated from Git archives at these exact revisions.

| Diagnostic | Main with #531 | Integrated #530 and #531 |
| --- | ---: | ---: |
| Python modules | 499 | 501 |
| Located dependency facts | 2,472 | 2,481 |
| Unique normalized edges | 2,030 | 2,039 |
| Cyclic group sizes | 18 | 11 and 2 |
| Mutual package pairs | 11 | 9 |

Compared with the shared original base `90c27b43`, the cumulative mutual-pair
reduction is 13→9. All #530 affected caller/owner fan-out counts in the original
table remain unchanged on each respective side; both new core modules still
have fan-out 2 and 0. The full reports retain the integrated composition hotspots.

Validation: 1,537 passed, seven skipped in the combined architecture/core/config/
EDA/Runtime and affected boundary suites (six loopback tests unavailable under
the sandbox and one Windows-only test). The final expanded architecture suite
passed all 159 tests. Pyright reported zero errors/warnings, and Ruff lint and
format checks passed for all source and tests.

### Before cumulative integration

```text
Parsed Python modules: 499
Normalized dependency facts: 2472
Unique normalized edges: 2030

Cyclic top-level package groups:
- booley.agent_workspace, booley.audit, booley.bwave, booley.config, booley.criteria, booley.dev_support, booley.eda, booley.feedback, booley.flows, booley.fusesoc, booley.harness, booley.mcp, booley.projects, booley.review, booley.runtime, booley.specialists, booley.targets, booley.ticket_board

Mutual top-level package pairs:
- booley.bwave <-> booley.flows
- booley.dev_support <-> booley.runtime
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
- booley.ticket_board.cli_handlers: 26
- booley.ticket_board.operations: 26
- booley.flows.sim.execution.engine: 25
- booley.specialists.mutation_tester: 25
- booley.harness.setup.intake: 24
- booley.harness.setup.workspace: 22
- booley.ticket_board.io: 22
- booley.flows.endpoint_acceptance: 19
- booley.ticket_board.review_lifecycle: 19
- booley.ticket_board.review_preparation: 19
- booley.flows.base: 18
- booley.specialists.specialist: 18
- booley.ticket_board.amendment: 18
- booley.flows.lint.flow: 16
- booley.harness._ticket_ops: 16
- booley.ticket_board.flow_execution: 16
- booley.ticket_board.workspace_ops: 16
- booley.flows.sim.verilator_coverage_execution: 15
- booley.ticket_board.basis_refresh: 15
- booley.specialists.reviewer: 14
- booley.ticket_board: 14
- booley.ticket_board.criteria_acceptance: 14

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

### After cumulative integration

```text
Parsed Python modules: 501
Normalized dependency facts: 2481
Unique normalized edges: 2039

Cyclic top-level package groups:
- booley.agent_workspace, booley.bwave, booley.criteria, booley.dev_support, booley.feedback, booley.flows, booley.harness, booley.mcp, booley.runtime, booley.specialists, booley.ticket_board
- booley.fusesoc, booley.targets

Mutual top-level package pairs:
- booley.bwave <-> booley.flows
- booley.dev_support <-> booley.runtime
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
- booley.ticket_board.cli_handlers: 26
- booley.ticket_board.operations: 26
- booley.specialists.mutation_tester: 25
- booley.harness.setup.intake: 24
- booley.harness.setup.workspace: 22
- booley.ticket_board.io: 22
- booley.flows.endpoint_acceptance: 19
- booley.ticket_board.review_lifecycle: 19
- booley.ticket_board.review_preparation: 19
- booley.flows.base: 18
- booley.specialists.specialist: 18
- booley.ticket_board.amendment: 18
- booley.flows.lint.flow: 17
- booley.harness._ticket_ops: 16
- booley.ticket_board.flow_execution: 16
- booley.ticket_board.workspace_ops: 16
- booley.flows.sim.verilator_coverage_execution: 15
- booley.ticket_board.basis_refresh: 15
- booley.specialists.reviewer: 14
- booley.ticket_board: 14
- booley.ticket_board.criteria_acceptance: 14

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

## Doctor integration refresh: 15 SEP 2026

Mergify dequeued #535 after #536/#537 landed because the dependency contract
documentation conflicted. The merged rule table also reused D26. Resolution
preserves Doctor's landed D26 rule and assigns Target/FuseSoC D27/D28. No
production changes were required for conflict resolution.

- Before source and analyzer: `8d1af1717162b3c0ad869329ffa6677e9f65dfc9` (main with #531/#532).
- After source and analyzer: `41eeebf7050b4c4cda4780e9c8e130b723f13320` (resolved integration).
- Both complete reports below come from Git archives at those revisions.

| Diagnostic | Current main | Integrated #530 |
| --- | ---: | ---: |
| Python modules | 503 | 505 |
| Located dependency facts | 2,496 | 2,505 |
| Unique normalized edges | 2,055 | 2,064 |
| Cyclic group sizes | 18 | 11 and 2 |
| Mutual package pairs | 11 | 9 |

All affected caller/owner fan-out values in the original table were verified
against both archives and remain unchanged. Doctor fan-out is now 57 on both
sides; #532's new diagnostic-owner boundaries remain enforced alongside #530.
Validation: 1,982 tests passed, seven explicit skips (six unavailable loopback
socket tests and one Windows-only test); Pyright reported zero errors/warnings;
Ruff lint and format checks passed for all source and tests.

### Before Doctor integration refresh

```text
Parsed Python modules: 503
Normalized dependency facts: 2496
Unique normalized edges: 2055

Cyclic top-level package groups:
- booley.agent_workspace, booley.audit, booley.bwave, booley.config, booley.criteria, booley.dev_support, booley.eda, booley.feedback, booley.flows, booley.fusesoc, booley.harness, booley.mcp, booley.projects, booley.review, booley.runtime, booley.specialists, booley.targets, booley.ticket_board

Mutual top-level package pairs:
- booley.bwave <-> booley.flows
- booley.dev_support <-> booley.runtime
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
- booley.harness.booley: 57
- booley.harness.doctor: 57
- booley.flows.sim.flow: 48
- booley.harness.developer: 44
- booley.harness.init_cmd: 39
- booley.flows.synth.flow: 35
- booley.mcp.server: 33
- booley.flows.fpga.flow: 32
- booley.ticket_board.cli_handlers: 26
- booley.ticket_board.operations: 26
- booley.flows.sim.execution.engine: 25
- booley.specialists.mutation_tester: 25
- booley.harness.setup.intake: 24
- booley.harness.setup.workspace: 22
- booley.ticket_board.io: 22
- booley.flows.endpoint_acceptance: 19
- booley.ticket_board.review_lifecycle: 19
- booley.ticket_board.review_preparation: 19
- booley.flows.base: 18
- booley.specialists.specialist: 18
- booley.ticket_board.amendment: 18
- booley.flows.lint.flow: 16
- booley.harness._ticket_ops: 16
- booley.ticket_board.flow_execution: 16
- booley.ticket_board.workspace_ops: 16
- booley.flows.sim.verilator_coverage_execution: 15
- booley.harness.setup.readiness: 15
- booley.ticket_board.basis_refresh: 15
- booley.specialists.reviewer: 14
- booley.ticket_board: 14

Named composition hotspot fan-out (diagnostic only):
- booley.harness.doctor: 57
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

### After Doctor integration refresh

```text
Parsed Python modules: 505
Normalized dependency facts: 2505
Unique normalized edges: 2064

Cyclic top-level package groups:
- booley.agent_workspace, booley.bwave, booley.criteria, booley.dev_support, booley.feedback, booley.flows, booley.harness, booley.mcp, booley.runtime, booley.specialists, booley.ticket_board
- booley.fusesoc, booley.targets

Mutual top-level package pairs:
- booley.bwave <-> booley.flows
- booley.dev_support <-> booley.runtime
- booley.feedback <-> booley.harness
- booley.fusesoc <-> booley.targets
- booley.harness <-> booley.mcp
- booley.harness <-> booley.runtime
- booley.harness <-> booley.ticket_board
- booley.mcp <-> booley.specialists
- booley.mcp <-> booley.ticket_board

Top 30 file fan-out:
- booley.harness.booley: 57
- booley.harness.doctor: 57
- booley.flows.sim.flow: 49
- booley.harness.developer: 44
- booley.harness.init_cmd: 39
- booley.flows.synth.flow: 36
- booley.flows.fpga.flow: 33
- booley.mcp.server: 33
- booley.flows.sim.execution.engine: 26
- booley.ticket_board.cli_handlers: 26
- booley.ticket_board.operations: 26
- booley.specialists.mutation_tester: 25
- booley.harness.setup.intake: 24
- booley.harness.setup.workspace: 22
- booley.ticket_board.io: 22
- booley.flows.endpoint_acceptance: 19
- booley.ticket_board.review_lifecycle: 19
- booley.ticket_board.review_preparation: 19
- booley.flows.base: 18
- booley.specialists.specialist: 18
- booley.ticket_board.amendment: 18
- booley.flows.lint.flow: 17
- booley.harness._ticket_ops: 16
- booley.ticket_board.flow_execution: 16
- booley.ticket_board.workspace_ops: 16
- booley.flows.sim.verilator_coverage_execution: 15
- booley.harness.setup.readiness: 15
- booley.ticket_board.basis_refresh: 15
- booley.specialists.reviewer: 14
- booley.ticket_board: 14

Named composition hotspot fan-out (diagnostic only):
- booley.harness.doctor: 57
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
