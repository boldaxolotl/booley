# Built-in Flow execution

Built-in `sim`, `lint`, `synth` and `fpga` Flows consume typed requests and return
`runtime.endpoint_execution.ExecutionResult`, whose `outcome` is the existing
`EndpointOutcome`. They do not inherit `McpTool` or generate MCP schemas.

```python
from pathlib import Path

from booley.flows.lint.flow import LintFlow
from booley.flows.lint.request import LintRequest

result = LintFlow().execute(LintRequest(target="lint", work_dir=Path.cwd(), diagnostic=True))
print(result.exit_code, result.outcome.detail)
```

This entry point runs inside the Session Runtime and performs the same Ticket
validation, admission and persistence as the CLI. It constructs no parser or
schema. `SimRequest`, `SynthRequest` and `FpgaRequest` live beside their respective
implementations. Requests are copied for execution because preparation and
baseline work may change the working inputs. Ticket identity and state-file
location are environment-derived preparation fields, excluded from constructors.

## Ownership

- `FlowMechanics` supplies subprocess execution, timeout/cancellation cleanup,
  artifact age checks and the Flow pre-state gate.
- `BuiltinFlow` composes a fresh `FlowSession` per invocation. Its narrow service
  delegates expose prepared inputs, Criteria state/updates, report-directory
  reservation, EDA-tool identity and progress. Its CLI convenience methods keep
  existing commands and module entry points callable without inheriting the
  legacy endpoint facade.
- `FlowSession` binds concrete execution, job-class and display-label callbacks
  to `EndpointState`. The latter owns per-invocation state and delegates to the
  separate acceptance, admission, reporting and invocation modules.
- `builtin_cli` builds a parser from common definitions and the concrete Flow's
  argument adapter. It translates CLI input to the same typed request. Concrete
  implementations supply their adapter; common modules never select a Flow.
- `mcp.flow_adapter` owns schema extraction and MCP-specific schema adjustments,
  including canonical timeout/mode fields and hidden CLI aliases. The MCP server
  retains wire validation, subprocess dispatch, budgeting and result conversion.
- `runtime.endpoint_execution` remains the only execution coordinator. Moving
  Flow/Criteria-aware services into Runtime would introduce reverse package
  dependencies; those services live alongside their Flow policy instead.

The shared service modules are also used by legacy extensions. `EndpointContext`
is their compatibility facade: it preserves eager `_add_args` initialization,
CLI parsing and historical hooks. `BooleyFlow` remains the public Custom Flow
base; it combines that facade with `FlowMechanics`. `McpTool` retains its public
result compatibility and Specialist/direct-endpoint extension contract. MCP
loading recognizes both hierarchies and honors Project-local schema overrides.
Built-ins do not inherit `EndpointContext`.

## Ordering and failures

Preparation validates the runtime, Flow enablement and Ticket acceptance surface
before loading mutable state. Target-to-Criterion binding validation precedes job
admission. The admitted claim remains held through invocation, final acceptance,
mutable completion persistence and reporting, then is released.

Acceptance has two recording points. Each `set_criterion` computes changes,
records immutable evidence, then saves mutable state and emits its update. At
completion, the coordinator records final acceptance/invalidation before saving
the timeline and final report. Preserve both boundaries; do not defer all
Criteria writes until completion.

A final acceptance-recording failure skips final mutable persistence and still
runs completion cleanup. An acceptance append failure inside `_run` is different:
the immediate update does not save, but the invocation adapter normalizes the
exception to an error outcome, after which final error persistence can occur.
This existing distinction is characterized by tests; the separation does not
change failure semantics. Cancellation propagates while subprocess trees,
stdout redirection and admission claims are cleaned up.

Non-persisting built-in dry-runs validate first and skip admission, EDA execution,
acceptance and normal persistence. Their optional `flow_plan.json` remains
supported. Diagnostic/elaboration modes keep their existing distinct semantics.

## Architecture and validation

D15 in [the source-dependency contract](SOURCE-DEPENDENCY-CONTRACT.md) forbids
all `booley.flows -> booley.mcp` imports, including nested and type-only imports,
without exemptions. D5/D6/D9 remain unchanged. Criteria action and reference
rendering consume an immutable endpoint catalog assembled outside Criteria; the
former W1/W2 discovery waivers were retired by #284.

`tests/flows/test_transport_contract.py` captures all four pre-refactor schemas,
exercises direct typed and CLI calls, checks Ticket gates and acceptance ordering,
distinguishes acceptance failures, runs a real lint child through MCP with a fake
EDA executable, and checks Project-local constructor/schema/loader compatibility.
The existing Flow/backend, MCP, Ticket/Criteria and architecture suites cover the
remaining execution and persistence behavior.
