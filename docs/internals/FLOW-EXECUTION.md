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

This entry point runs inside the Sandbox and performs the same boundary
validation, admission and persistence as the CLI. Ordinary direct typed and
`booley flow` calls use `StandaloneFlowExecution`, without Goal evidence
persistence. MCP composition supplies `GoalFlowExecution` with its captured
Run Binding for Goal verification; a direct caller must explicitly compose
that adapter to use the same evidence path. The typed entry point constructs
no parser or schema. `SimRequest`, `SynthRequest` and `FpgaRequest` live beside their respective
implementations. Requests are copied for execution because preparation and
baseline work may change the working inputs. The Goal binding and state-file
location are environment-derived preparation fields, excluded from constructors.
The legacy Ticket identity fields and adapters are retained until Phase 9a
removes them.

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

Preparation validates the runtime, Flow enablement, bound Goal worktree, and
Target-to-Goal binding before job admission. The Run Binding captures the record
and specification revisions plus Protected Inputs; publication rechecks it
before appending evidence or merging mutable state. Failed publication binding
discards the evidence rather than assigning it to another Goal Mode.

The retained Ticket adapter additionally validates its sealed acceptance surface
before loading mutable state; that path is retained until Phase 9a removes it.

The admitted claim remains held through invocation, final acceptance, mutable
completion persistence and reporting, then is released.

Acceptance has two recording points. Each `set_criterion` computes changes,
records immutable evidence, then saves mutable state and emits its update. At
completion, the coordinator records final acceptance/invalidation before saving
the timeline and final report. Preserve both boundaries; do not defer all
Criteria writes until completion.

A final acceptance-recording failure becomes exit 2 with
`detail.completion_error`, preserves the underlying Flow verdict and already-known
Target facts, skips final mutable persistence, and still attempts `report.json`
publication. Any per-Target acceptance dispositions committed before a later
projection failure remain visible in `detail.acceptance`; user-facing output has
no traceback. An acceptance append failure inside `_run` is different:
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
exercises direct typed and CLI calls, checks retained Ticket gates and acceptance
ordering, distinguishes acceptance failures, runs a real lint child through MCP with a fake
EDA executable, and checks Project-local constructor/schema/loader compatibility.
The Flow/backend, MCP, Goal/evidence, Criteria, and architecture suites cover
current execution and persistence behavior; retained Ticket suites characterize
compatibility code until Phase 9a removes it.

### Verilator compiler cache ownership

Simulation build preparation resolves strict checkout-local compiler-cache
configuration into an immutable policy. `PreparedSimulationBuild.environment`
is authoritative for compilation. `simulation_build_script` finalizes storage
availability only for execution and scopes compiler exports to the build half;
simulator environment and testbench fingerprints retain their existing owners.
Ordinary, compatibility, campaign, coverage, Cocotb, and elaboration-only builds
consume this shared interface; Mutation Tester builds, which run Make directly,
use `simulation_build_environment`. Icarus preparation does not resolve cache
policy. The cache policy module never reads the process environment: build
preparation passes `read_issued_identity(os.environ)` and the ambient environment in.
A missing (pre-cache Sandbox) or foreign issued root yields an inactive policy
that compiles uncached with a refresh warning; only malformed configuration
fails preparation.

Sandbox Issuance fixes `BOOLEY_COMPILER_CACHE_ROOT` under the existing Project
mount in both container and remote environments. Goal-bound and review
subprocesses inherit it; retained Ticket subprocesses also inherit it when
repointing `BOOLEY_PROJECT_DIR` to checkout-local authored data.
Its presence/value participates in spec validation, digest/drift detection, and
refresh; no new mount is introduced. Non-issued development resolves ownership
through `resolve_project_dir` and selected configuration through
`resolve_checkout_project_dir` plus `resolve_toml`.

The shared subtree is outside all mutable build generations and is never
registered as a disposable `flow-cache` artifact. Setup cleanup preserves
unowned legacy runtime residue. Legacy `.booley/project` resynchronization
excludes precisely `project/.runtime/compiler-cache`, preserving adjacent
runtime/authored content. Baseline core copying only copies the core subtree;
ordinary Project Git ignores exclude `.runtime` from source snapshots.
