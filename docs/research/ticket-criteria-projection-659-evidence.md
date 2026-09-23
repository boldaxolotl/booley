# Ticket Criteria projection #659 evidence

Date: 23 SEP 2026.

Issue [#659](https://github.com/boldaxolotl/booley/issues/659) moves the pure
projection from resolved Ticket documents into generic Criteria declarations
from `booley.criteria` to `booley.ticket_board`. This record separates exact
diagnostic evidence from the normative
[source dependency contract](../internals/SOURCE-DEPENDENCY-CONTRACT.md).

## Exact comparison

| Diagnostic | Before `46ad1684` | After `b774b7cc` |
| --- | ---: | ---: |
| Parsed Python modules | 547 | 547 |
| Located dependency facts | 2,830 | 2,830 |
| Unique module-to-module edges | 2,345 | 2,345 |
| Criteria-to-Ticket-Board edges | 1 | 0 |
| Direct mutual package pairs | 10 | 9 |
| Cyclic package groups | 11 members and 2 members | 11 members and 2 members |
| `booley.harness.setup.intake` fan-out | 27 | 27 |
| `booley.ticket_board.amendment` fan-out | 19 | 19 |

The removed dependency was:

```text
booley.criteria.ticket_projection -> booley.ticket_board.ticket_document
```

The implementation relocates that module to
`booley.ticket_board.criteria_projection`. Intake exchanges one imported module
for the new owner, while amendment changes from a cross-package import to a
Ticket Board sibling import. There is exactly one definition of
`project_ticket_criteria`; the old path is absent rather than retained as a
compatibility export.

The cyclic groups are unchanged:

```text
booley.agent_workspace, booley.bwave, booley.criteria, booley.dev_support,
booley.feedback, booley.flows, booley.harness, booley.mcp, booley.runtime,
booley.specialists, booley.ticket_board

booley.fusesoc, booley.targets
```

The only direct mutual pair removed is
`booley.criteria <-> booley.ticket_board`. The nine remaining pairs are:

```text
booley.bwave <-> booley.flows
booley.dev_support <-> booley.runtime
booley.feedback <-> booley.harness
booley.fusesoc <-> booley.targets
booley.harness <-> booley.mcp
booley.harness <-> booley.runtime
booley.harness <-> booley.ticket_board
booley.mcp <-> booley.specialists
booley.mcp <-> booley.ticket_board
```

## Reproduction

The reports were generated from archived exact revisions so working-tree files
could not affect either measurement:

```console
evidence_dir="$(mktemp -d)"
mkdir -p "${evidence_dir}/before" "${evidence_dir}/after"
git archive 46ad1684ff8462a11b4e44257fe04dd1a741b6ec src/booley tests/architecture \
  | tar -x -C "${evidence_dir}/before"
git archive b774b7cc src/booley tests/architecture \
  | tar -x -C "${evidence_dir}/after"
python3 "${evidence_dir}/before/tests/architecture/report.py" \
  --source-root "${evidence_dir}/before/src/booley" --top 30
python3 "${evidence_dir}/after/tests/architecture/report.py" \
  --source-root "${evidence_dir}/after/src/booley" --top 30
```

## Complete archived reports

The before report differs from the after report only by the additional mutual
pair `booley.criteria <-> booley.ticket_board`. Both reports otherwise contain
the totals, cyclic groups, and fan-out values shown below.

```text
Parsed Python modules: 547
Normalized dependency facts: 2830
Unique normalized edges: 2345

Cyclic top-level package groups:
- booley.agent_workspace, booley.bwave, booley.criteria, booley.dev_support, booley.feedback, booley.flows, booley.harness, booley.mcp, booley.runtime, booley.specialists, booley.ticket_board
- booley.fusesoc, booley.targets

Mutual top-level package pairs after #659:
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
- booley.flows.sim.flow: 63
- booley.harness.booley: 59
- booley.harness.doctor: 57
- booley.harness.developer: 45
- booley.harness.init_cmd: 40
- booley.flows.synth.flow: 36
- booley.mcp.server: 36
- booley.flows.fpga.flow: 33
- booley.flows.sim.execution.engine: 29
- booley.harness.setup.intake: 27
- booley.ticket_board.operations: 27
- booley.specialists.mutation_tester: 25
- booley.ticket_board.io: 24
- booley.harness.setup.workspace: 22
- booley.ticket_board.cli_handlers: 22
- booley.flows.base: 21
- booley.flows.endpoint_acceptance: 19
- booley.ticket_board.amendment: 19
- booley.ticket_board.review_lifecycle: 19
- booley.ticket_board.review_preparation: 19
- booley.specialists.specialist: 18
- booley.flows.lint.flow: 17
- booley.ticket_board.archive: 17
- booley.ticket_board.workspace_ops: 17
- booley.flows.sim.verilator_coverage_execution: 16
- booley.harness._ticket_ops: 16
- booley.flows.sim.campaign.coordinator: 15
- booley.harness.setup.readiness: 15
- booley.specialists.reviewer: 15
- booley.ticket_board.basis_refresh: 15

Named composition hotspot fan-out (diagnostic only):
- booley.harness.doctor: 57
- booley.harness.booley: 59
- booley.harness.init_cmd: 40
- booley.harness.developer: 45
- booley.flows.sim.flow: 63
- booley.flows.synth.flow: 36
- booley.mcp.server: 36
- booley.flows.fpga.flow: 33
- booley.specialists.mutation_tester: 25
- booley.specialists.coverage_analyst: 13
```

The production architecture gate and seeded D30 import-form tests are the
authoritative proof that the after tree has no static Criteria-to-Ticket-Board
import. Repository-wide text search supplements that AST analysis and finds no
old-path import or re-export.
