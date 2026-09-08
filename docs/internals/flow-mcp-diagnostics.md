# Flow/MCP separation: dependency diagnostics

Evidence for [#422](https://github.com/boldaxolotl/booley/issues/422) and the
architecture-fitness record in [#279](https://github.com/boldaxolotl/booley/issues/279).

- Before: `9d9ae6832b944a125acf3a926820d2a0d65728cb` (current main when the implementation was rebased).
- After: `3a5b871daf960ec7ee4d044096f7faac7ee98912` (implementation commit; subsequent documentation does not change the graph).
- Analyzer: the existing test-only source analyzer, including nested, conditional,
  relative and TYPE_CHECKING imports.

Reproduce at either revision with `python tests/architecture/report.py --top 5`.
The baseline was analyzed from a temporary `git archive` of the exact commit;
the final tree was analyzed directly. No production dependency on the analyzer
was added.

| Diagnostic | Before | After |
| --- | ---: | ---: |
| Python modules | 421 | 443 |
| Normalized dependency facts | 2077 | 2159 |
| Unique normalized edges | 1671 | 1748 |
| Flow-to-MCP edges | 5 | 0 |
| Mutual package pairs | 21 | 19 |
| Cyclic top-level group | Same 18 packages | Same 18 packages |

## Removed Flow-to-MCP edges

- `booley.flows.base -> booley.mcp.base`
- `booley.flows.base -> booley.mcp.schema_extractor`
- `booley.flows.fpga.flow -> booley.mcp.base`
- `booley.flows.sim.flow -> booley.mcp.base`
- `booley.flows.synth.flow -> booley.mcp.base`

## Package cycle check

Removed mutual pairs:

- `booley.criteria <-> booley.mcp`
- `booley.flows <-> booley.mcp`

No mutual pair was added, and SCC membership did not change or expand. The
Criteria/MCP mutual pair disappears because MCP no longer owns Criteria
execution services. Criteria's producer-discovery dependencies and W1/W2 waivers
remain intact; this does not implement #284 or override its conditional gates.

## Named composition hotspot fan-out

These are diagnostics, not numeric limits. Increases reflect explicit typed
request/adapter imports and the two loaders recognizing the separate built-in
hierarchy.

| Module | Before | After |
| --- | ---: | ---: |
| `booley.harness.doctor` | 64 | 64 |
| `booley.harness.booley` | 52 | 53 |
| `booley.harness.init_cmd` | 42 | 42 |
| `booley.harness.developer` | 41 | 41 |
| `booley.flows.sim.flow` | 37 | 39 |
| `booley.flows.synth.flow` | 34 | 35 |
| `booley.mcp.server` | 29 | 30 |
| `booley.flows.fpga.flow` | 29 | 31 |
| `booley.specialists.mutation_tester` | 25 | 25 |
| `booley.specialists.coverage_analyst` | 25 | 25 |
