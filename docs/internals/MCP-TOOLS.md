# MCP Tools: Architecture, Execution, and Extension

This guide defines Booley's MCP tool framework and explains how Custom Flows
and custom MCP tools extend it. It covers discovery, the shared Python contracts,
Criteria, in-container execution, and validation. Custom MCP tools work in both
Interactive Mode and Goal Mode. Host EDA provisioning is deliberately not a
custom-MCP extension surface: it requires a built-in, evidence-backed policy.

## Document boundary

The documentation is split by responsibility, not by reader type:

| Document | Owns |
|---|---|
| **This document** | The MCP tool framework: discovery, lifecycle, base classes, `McpToolResult`, Criteria routing, and Custom Flows and MCP tools and built-in Specialist evidence contracts |
| [FLOW_REFERENCE.md](../user/FLOW_REFERENCE.md) | How RTL developers run built-in Booley Flows and interpret their results |
| [FLOW_IMPLEMENTATION.md](FLOW_IMPLEMENTATION.md) | The implementation and evidence contracts of the built-in deterministic `sim`, `lint`, `synth`, and `fpga` Booley Flows |
| [FLOW_REPORTS.md](FLOW_REPORTS.md) | Report locations, JSON schemas, and Campaign file layouts of the built-in Flows |
| [CONFIG.md](../user/CONFIG.md) | The project configuration surface: exact keys, defaults, examples, `.core` design description, and `tests.toml` |
| [SUPPORTED-EDA-TOOLS.md](../user/SUPPORTED-EDA-TOOLS.md) | The source-of-truth matrix of supported EDA engines, provisioning, trace support, and installation requirements |

This document may show small configuration fragments when an extension contract
needs context, but it does not define the configuration schema or document the
built-in flows. Follow the links above for those references.

## Read this first

This is an implementation-level guide. It assumes the vocabulary and whole-system model from:

- **[GLOSSARY-MAP.md](../../GLOSSARY-MAP.md)** — the controlled-vocabulary
  index. This guide uses the shared Booley, Goal Mode, and Simulation Coverage glossaries.
- **[ARCHITECTURE.md](ARCHITECTURE.md)** — how the agent session, Specialists, and the Booley Flow contract fit together at run time.
- **[CONFIG.md](../user/CONFIG.md)** — the configuration reference for `booley.toml`, `.core` files, `tests.toml`, EDA provisioning, and Pre-Sim Commands.
- **[FLOW_REFERENCE.md](../user/FLOW_REFERENCE.md)** — the public contract for invoking and interpreting built-in Booley Flows.
- **[FLOW_IMPLEMENTATION.md](FLOW_IMPLEMENTATION.md)** — how built-in deterministic Booley Flows turn FuseSoC Targets into commands and normalize EDA output into evidence.
- **[SUPPORTED-EDA-TOOLS.md](../user/SUPPORTED-EDA-TOOLS.md)** — which EDA tools and provisioning sources are supported and what each requires.

This guide owns the common MCP tool lifecycle and extension contract.

The lifecycle sequence itself lives in the transport-independent
`booley.runtime.endpoint_execution` module. Both the direct CLI path and the
process launched by the MCP adapter enter that coordinator; MCP remains
responsible for discovery, schema validation, and wire payloads. The public
base classes described below remain source-compatible facades for Project-local
extensions. CLI argument parsing finishes before the coordinator receives its
prepared request, so the shared execution interface has no CLI or MCP request
shape in it.

## Overview

The session's agent does not invoke an EDA command, project script, or Specialist directly. It calls a discovered MCP tool inside the Sandbox. That MCP tool owns the request schema, execution, result interpretation, and any bound Goal evidence updates.

Booley has three agent-facing implementation families:

| Family | Built-in examples | Responsibility |
|--------|-------------------|----------------|
| `BuiltinFlow` | `sim`, `lint`, `synth`, `fpga` | Run deterministic work and normalize its evidence; these are Booley Flows in Booley's controlled vocabulary |
| `Specialist` | `reviewer`, `mutation_tester` | Run a focused LLM agent with a purpose-built prompt and interpret its response |
| Direct `McpTool` subclass | Retained `submit_run_report` | Implement orchestration that is neither a deterministic Flow nor a Specialist; Project-local custom tools can use the same contract |

Built-in and custom MCP tools share the same base interfaces and MCP surface. Their source differs, but the calling model does not. Every agent-facing MCP tool and every subprocess it launches runs inside the Sandbox. A supported host-provisioned EDA installation changes where immutable tool files originate, not where the command executes.

### The Common Lifecycle

Every agent-facing call follows the same shape:

1. The MCP registry discovers an implementation and exposes its declared arguments.
2. The agent calls it by its discovered name.
3. The MCP tool validates common and endpoint-specific arguments.
4. The shared coordinator checks Target binding before admission and holds any
   admitted Job Class claim through completion.
5. A Booley Flow runs deterministic work inside the Sandbox, a Specialist runs its agent loop, or a direct `McpTool` subclass performs its own orchestration.
6. The implementation interprets raw output into a transport-neutral endpoint
   outcome. `McpToolResult` is the source-compatible public name for that
   outcome in Project-local extensions.
7. The coordinator calls the explicit acceptance-recorder interface before
   mutable state/report persistence, then releases admission. In Goal Mode,
   evidence is appended under the captured Run Binding before state is merged;
   calls outside Goal Mode have no persistent Goal evidence. If final acceptance
   recording fails, the coordinator returns exit 2, adds a structured `completion_error`,
   preserves existing result and Target facts, skips final mutable persistence,
   and makes one bounded recovery-report attempt before admission cleanup. Later
   recovery failures do not overwrite the first diagnosis. An append failure during an in-run Criterion
   update instead follows the invocation error path; see the failure distinctions
   in [Built-in Flow execution](FLOW-EXECUTION.md).

Interactive Mode outside Goal Mode uses the same registry and implementations without persistent Goal state. An occupying Goal Record adds evidence binding and freshness checks to calls into its worktree; it does not launch another agent. While any Goal Record occupies the Project, every endpoint call without explicit `work_dir` is refused, including calls intended to run outside that worktree.

### How Host-Provisioned EDA Fits

Host provisioning is an administrative startup operation, not an MCP
execution route:

1. A host administrator registers a supported installation and grants one
   exact Project root access.
2. The Project requests host provisioning without naming an installation.
3. Booley validates and stamps a runtime specification containing the fixed
   image, read-only mount, wrapper, labels, and optional licensing topology.
4. Docker creates or resumes the Sandbox only if the issued contract
   and live container state still match.
5. The ordinary built-in Booley Flow launches the EDA subprocess inside the
   Sandbox and interprets its evidence there.

Custom MCP code cannot add an arbitrary host path, command, environment
variable, license destination, or new commercial EDA policy. A new
host-provisioned EDA kind belongs in the built-in policy and support matrix
after equivalent security and full-Flow evidence.

```
┌──────────────────────────────────────────────────┐
│  Sandbox (Docker)                        │
│                                                  │
│  Agent ──MCP──► MCP tools                        │
│                 (built-in + custom)              │
│                        │                         │
│                        │                         │
│        built-in Flow launches EDA subprocess    │
│        from image files or an approved          │
│        read-only host installation mount        │
└──────────────────────────────────────────────────┘
```

### When to Extend the MCP Surface

Write a Custom Flow or custom MCP tool when:

- You need a project-specific check that doesn't belong in the framework (DRC, protocol compliance, custom linting)
- You need an LLM-powered specialist with project-specific prompting

First check the [supported EDA tool matrix](../user/SUPPORTED-EDA-TOOLS.md). If Booley already supports the workflow, configure the built-in Flow. Otherwise, use `BooleyFlow` for deterministic in-container subprocess logic, `Specialist` for LLM-powered work, or `McpTool` for other in-container orchestration. For a per-test build step, use [Pre-Sim Commands](../user/CONFIG.md#pre-sim-commands-flowssimpre_run_commands). A missing commercial EDA policy cannot be replaced by a custom host wrapper.

---

## Chapter 1: Discovery, Visibility, and Configuration

MCP tools are discovered from either the installed Booley package or the project's `.booley_project/mcp_tools/` directory. Discovery is inclusive by default: every valid implementation is enabled unless its own configuration section says otherwise.

### Default Discovery and Explicit Opt-Out

```toml
[specialists.reviewer]
enabled = false                 # remove one discovered Specialist MCP tool
```

- Built-in Flows are scanned from `booley.flows`, Specialists from `booley.specialists`, and protocol utilities from `booley.mcp`.
- Custom Flows and MCP tools are scanned from `.booley_project/mcp_tools/*.py`.
- `[flows.<name>].enabled = false` disables a Flow; `[specialists.<name>].enabled = false` disables a Specialist. Protocol utilities have no Project enable switch.
- Interactive, non-nested servers list `goal_enter`, `goal_status`, `goal_propose_change`, and `goal_finish` without a rollout switch. These Goal tools bypass `BOOLEY_MCP_TOOLS`; default and nested servers do not list them.
- For regular discovered tools, nested allowlists take precedence, followed by `BOOLEY_MCP_TOOLS`, then the `BOOLEY_MCP_MODE=interactive` filter. That interactive filter hides the retired `submit_run_report`; default servers can still expose it, and explicit allowlists can override the interactive filter. Its implementation is retained until Phase 9a removes it. `tb_coder` is currently de-registered in all modes. These environment filters are separate from Project registration.
- `booley flow` is the human diagnostic entry point for Booley Flows; the MCP tool diagnostic surface covers Specialists and non-Flow endpoints.

### Execution Boundary

Agent-facing MCP tools and the subprocesses they launch run inside the Sandbox. A custom endpoint that needs additional software adds it to a Project
image or uses a supported built-in EDA provisioning policy.

### Configuration Boundary

The framework reads Flow settings from `[flows.<name>]` and Specialist
settings from `[specialists.<name>]`. At this layer the shared effect is that
`enabled = false` removes the capability from normal discovery.

[CONFIG.md](../user/CONFIG.md#flow-and-specialist-availability-enabled) owns the exact TOML
schema, resolution order, defaults, and built-in per-Flow/endpoint settings. A
custom Flow can read its section with `_load_flow_config(name, work_dir)` from
`booley.flows.flow_config`. Discovery consumes `enabled` for Flows
and Specialists; there is no generic `_load_tool_config()` API for additional
custom `[specialists.<name>]` values, so an implementation that defines such values
must load and validate them explicitly.

The retired `[mcp_tools.*]` table is rejected with migration guidance. Rename
Specialist sections to `[specialists.*]` and remove protocol utility settings.
Direct MCP endpoints are available by default, subject to execution-mode and
server-level filters.
To opt out of a custom direct endpoint, prefix its implementation filename with
`_` (for example `mcp_tools/_project_check.py`); discovery skips such files.
Specialist settings require table entries and boolean `enabled` values. Doctor
and discovery reject names that do not belong to discovered Specialists.

### Summary: Discovery Rules

| MCP tool kind | Source | How enabled | Agent-visible? |
|-----------|--------|-------------|:---:|
| Built-in Flow | Installed `booley.flows` package | Enabled unless `[flows.<name>].enabled = false` | Yes, subject to mode-specific hiding |
| Built-in Specialist | Installed `booley.specialists` package | Enabled unless `[specialists.<name>].enabled = false` | Yes, subject to mode-specific hiding |
| Protocol utility | Installed `booley.mcp` package | Available by default; no Project enable switch | Yes, subject to mode-specific hiding |
| Custom MCP tool | `.booley_project/mcp_tools/*.py` | Flows use `[flows]`, Specialists use `[specialists]`, direct endpoints have no Project enable switch | Yes, subject to mode-specific hiding |
Use unique MCP tool names. Shared endpoint validation warns when a custom name collides with a discovered built-in MCP tool. Doctor runs that validation, but registry discovery is a separate pass, so the warning is not an enforcement boundary.

### Register a Custom Endpoint

Put one implementation with a unique name and literal `name` / `description`
metadata in `.booley_project/mcp_tools/<name>.py`; the file is the registration,
with no source allowlist. Chapter 2 covers implementation and exercise, and
Chapter 3 covers any project Criteria named by `satisfies`.

---

## Chapter 2: The Shared Python Contract

Built-in Flows compose a transport-independent execution session and accept
typed requests. CLI and MCP adapters expose them through the existing commands
and schemas; see [Built-in Flow execution](FLOW-EXECUTION.md). Custom Flows retain
`BooleyFlow`, while Specialists and direct MCP endpoints retain their existing
`McpTool` extension contract. All paths use the same execution coordinator and
acceptance/reporting services inside the Sandbox.

Criterion-aware MCP endpoints produce verdicts using the internal *Criteria*
evaluation interface. Goal Mode translates its mandatory Goals into this shared
representation; catalog entries alone do not define new Goal families. This
chapter explains how an implementation declares and records those verdicts; Chapter 3 explains how the
Criteria themselves are defined and expanded.

### Base Classes

| Class | Contract | Built-in examples |
|-------|----------|-------------------|
| `BuiltinFlow` | Execute typed requests through a composed Flow session | `sim`, `lint`, `synth`, `fpga` |
| `BooleyFlow` | Preserve the Custom Flow CLI/subprocess extension contract | Project-local Flows |
| `Specialist` | Build a focused prompt, run an LLM agent loop, and interpret its output | `reviewer`, `mutation_tester` |
| `McpTool` | Implement orchestration directly when neither higher-level contract fits; also available to Project-local custom tools | Retained `submit_run_report` |

Import `McpTool` / `McpToolResult` from `booley.mcp.base`, `Specialist`
from `booley.specialists.specialist`, and `BooleyFlow` from
`booley.flows.base`. The package `__init__` modules do not re-export them.

Every concrete implementation declares `name` and `description`. Criterion-aware implementations also declare `satisfies`; code-changing implementations declare `code_modifying = True` so successful edits invalidate stale evidence.

### Common CLI Arguments

Every MCP tool inherits a base argument set. A concrete implementation adds only its endpoint-specific flags:

| Arg | Meaning |
|-----|---------|
| `-C/--project PATH` | Human CLI Project checkout selection; transport parser and MCP keep `work_dir` |
| `--report-dir` | Where `report.json` lands; bound Goal runs default to the record's runtime reports |
| `--target` | Which project Target to operate on (comma-separated). This is what `self.args.target` reads in the examples below |

The Target value is also what the `per_target` Criterion convention keys on.

### Execution Hooks

#### `BooleyFlow`

A command-backed `BooleyFlow` overrides `_add_args`, `_build_command`, and
`_interpret_result`. Complex built-ins and custom host wrappers may
override `_run()` instead. The minimal Custom Flow below uses the command-backed
contract without backend-specific machinery.

Booley redirects Python bytecode and pytest caches for Project endpoints and
their subprocesses to runtime storage. Custom Flows must not depend on cache
files in the source checkout.

| Method | Responsibility |
|--------|----------------|
| `_add_args()` | Add Flow-specific CLI arguments |
| `_build_command()` | Return the subprocess argument list to execute |
| `_interpret_result()` | Convert the completed subprocess result into a `McpToolResult` and Criterion updates |

#### `Specialist`

Built-in Specialists such as `reviewer` override `_add_agent_args`, `_build_prompt`, and `_interpret_output`. The framework runs an LLM agent loop with the prompt and provider-native capabilities. `Specialist` implements `_add_args` itself to register shared flags such as `--model` and `--max-turns`; overriding it would break those flags.

| Method | Responsibility |
|--------|----------------|
| `_add_agent_args()` | Add Specialist-specific CLI arguments without replacing the shared agent arguments |
| `_build_prompt()` | Construct the prompt given to the Specialist's agent loop |
| `_interpret_output()` | Convert the agent's output into a `McpToolResult` and Criterion updates |

Useful Specialist class attributes are:

| Attribute | Meaning |
|-----------|---------|
| `min_model` | Lowest allowed model tier (`light`, `standard`, or `heavy`) |
| `default_timeout` | Default maximum run time in seconds |
| `agent_tools` | Provider-native capabilities requested for the agent loop; use this to shape behavior, not to enforce workspace access |
| `workspace_access` | `"read_write"` (default) or `"read_only"`; read-only calls use a disposable snapshot on both providers |

Shared human `--model`, `--max-turns`, and `--timeout` controls are CLI-only and
are excluded from strict Specialist MCP schemas, including custom schema hooks.
Use `booley specialist <name>` or the supported `python -m booley.specialists.<name>`
entry inside the Sandbox. Project subclasses must migrate `args.timeout` (seconds)
to `args.timeout_ms` (milliseconds); construct seconds-based `AgentCallParams`
with `self.timeout_seconds()`. Keep `default_timeout` and `min_timeout` class
attributes in seconds. The accessor rounds positive milliseconds up to seconds. The human parser converts
`--timeout DURATION` into `timeout_ms`; `_parser` remains the transport/extension
parser for schema hooks. Hidden human aliases `--work-dir` and `--timeout-ms`
remain for one compatibility release. Framework subprocesses set the private
`_BOOLEY_CLI_INVOCATION_ORIGIN=transport` parser marker so legacy serialized
argv keeps its contract without human deprecation notices. This marker grants
no execution authority. Custom option collisions preserve plugin-owned options;
use outer `booley flow -C PATH NAME` selection with the common inherited entrypoint.

The shared `code_modifying` and `satisfies` attributes are explained below.

#### Direct `McpTool` Subclasses

A direct subclass implements `_run()` and returns a `McpToolResult`.
The retired `submit_run_report` uses this shape to write Ticket execution state;
the `BOOLEY_MCP_MODE=interactive` filter hides it, while default servers can still
expose it and nested or explicit allowlists take precedence over that filter.
It is a built-in protocol utility in `booley.mcp`, retained until Phase 9a removes it.
Goal Mode receives its Session Summary through `goal_finish`. A deterministic
custom subprocess normally remains a `BooleyFlow` so it inherits the common in-runtime lifecycle.

### McpToolResult

```python
McpToolResult(
    exit_code: int,           # 0 = met, 1 = unmet, 2 = unable to run
    criterion_key: str,       # which criterion was evaluated
    criterion_met: bool | None, # default False; Flow/Specialist absence projects to None
    report_text: str,         # human-readable output (tail of log)
)
```

Flow and Specialist results supply `exit_code` and `report_text`; their headline
Criterion fields are derived centrally from effective `set_criterion()` evaluations.
Exactly one mapped and evaluated Criterion yields its key and boolean verdict;
zero/multiple mapped Criteria, no evaluation, or multiple selected Targets yield
an empty key and `None`. `McpToolResult` also carries optional fields,
most usefully `detail: dict` for structured evidence (written into
`report.json`). Token/cost fields are populated automatically by `Specialist`;
line-count fields (`lines_added`/`lines_removed`) are stamped by the base
`McpTool` for any code-modifying endpoint.

**`set_criterion()` and reports:** call `set_criterion(key, met)` for each Criterion
you evaluate. The effective changes determine the Flow/Specialist report even if
later persistence fails; `exit_code` independently exposes that failure. Goal
completion is judged on persisted, current evidence for the declared Goals.
Standalone evaluations remain in memory.
Flow/Specialist extensions that supplied result-only headlines must migrate to
`set_criterion()`; explicit result fields no longer determine their final headline.
Generic `mcp_tool` endpoints retain their existing result-field contract, including
the default `False` and an explicitly supplied `None`. Acceptance/history hooks
before projection retain raw fields; `_post_run` and `ExecutionResult.outcome`
receive the centrally projected Flow/Specialist headline.

### Common Artifact Contract

Reports with durable outputs carry an `artifacts` block: two entry-point files
plus the **directories** holding everything else.

```json
"artifacts": {
  "log":    ".booley_project/.runtime/edalize/synth/synth_soc/run.log",
  "report": ".booley_project/.runtime/flow-reports/synth_synth_soc.json",
  "dirs": {
    "build":  ".booley_project/.runtime/edalize/synth/synth_soc/synth",
    "timing": ".booley_project/.runtime/edalize/synth/synth_soc/synth/reports/timing"
  }
}
```

The block appears both at the top level of a durable report and inside the
MCP tool's `detail`, which is the copy that reaches an agent as MCP
`structuredContent`. It therefore survives tail-truncated stdout and the 64 KB
structured-output reduction applied to oversized results.

For `synth` and `fpga`, the versioned `detail.implementation.results[target]`
projection also carries the target's grade, QoR metrics, baseline deltas, cache
summary, and artifact pointers. Oversized reports discard embedded recipe
snapshots and diagnostic excerpts first, then per-clock expansion, then excess
targets in deterministic order. The compact result records omitted fields and
targets; it does not reduce an implementation result to artifact links while
discarding all QoR.

The contract uses directory roles rather than enumerating backend filenames.
EDA tools change or add filenames more often than the semantic directory roles
change, and a consumer can list the cited directory when it needs the complete
output set.

| Key | Meaning |
|---|---|
| `log` | This run's primary log, often outside the build-artifact directory |
| `report` | The durable structured report itself |
| `dirs` | `{role: path}` for artifact directories, such as `build`, `timing`, `impl`, `synth`, `mutant_logs`, or `verification_rounds` |

A multi-target Flow nests one block per Target (`artifacts[target]`). Two rules
make every pointer trustworthy:

- **Never present when wrong.** Omit a key when its file or directory does not
  exist or cannot be proven to belong to the current run. Flow-specific
  freshness requirements belong with that Flow's evidence contract in
  [FLOW_IMPLEMENTATION.md](FLOW_IMPLEMENTATION.md).
- **Always work-dir-relative.** Absolute container paths are not portable
  artifact references. Paths are relative to the MCP tool's work dir,
  including when Goal Mode places reports in a Goal Record outside the
  calling worktree.

A `--baseline` run is a deliberate exception: it executes in a throwaway
worktree, so paths relativized there could resolve to unrelated current-run
files from the real worktree. Baseline metrics therefore carry no `artifacts`
block.

Specialists that expose durable evidence follow the same convention. For
example, `mutation_tester` stages one self-contained campaign directory in its
numbered report invocation. Its atomic manifest cites the pristine baseline log,
one log per mutant, verification rounds, mutation specifications, results, and
isolated source variants; the `mutant_logs` role points at the durable invocation copy,
not the reusable runtime lock.

### `satisfies` and `satisfies_args`

Criterion-aware Flows, Specialists, and direct endpoints declare which Criteria
they can satisfy:

```python
class MyTool(BooleyFlow):
    satisfies = ["my_check"]  # list of criterion names
    satisfies_args = {}  # empty for simple Flows
```

For multi-Criterion MCP tools:

```python
class MultiTool(Specialist):
    satisfies = ["check_a", "check_b"]
    satisfies_args = {
        "check_a": "--mode a",
        "check_b": "--mode b",
    }
```

`satisfies_args` are **prompt hints**: they tell the session's agent which CLI arguments to pass when invoking the MCP tool for a specific Criterion. They are not executed directly.

**Static-discovery limitation:** The MCP tool registry reads class metadata without importing the file, using Python's abstract syntax tree (AST). It can extract only literal values. A computed expression such as `satisfies = BASE + ["extra"]` therefore appears empty, and shared endpoint validation warns about it.

### `per_target` Convention

The shared Criterion catalog uses `per_target = true` for keys such as `drc_clean_variant_a`, `drc_clean_variant_b`, and `drc_clean_variant_c`. Catalog expansion uses all Project Targets for custom Criteria and Target–Flow compatibility for built-in families (`sim_pass_*`, `lint_clean_*`, …). Goal entry instead translates concrete per-Target Goal arguments into their evidence keys; catalog expansion never creates additional Goals.

In your Flow code, use the convention:

```python
targets = [item.strip() for item in self.args.target.split(",") if item.strip()]
keys = [f"drc_clean_{target}" for target in targets] or ["drc_clean"]
for key in keys:
    self.set_criterion(key, passed)
```

### `code_modifying` Flag

Goal status independently checks source and Target fingerprints; an old passing
result cannot remain fresh merely because an endpoint omitted this flag. The
shared mutable Criterion invalidation interface still uses it as follows.

When any endpoint declares `code_modifying = True`:
- After a successful (exit 0) run, a git diff triggers automatic criteria invalidation
- All criteria matching the modified category (RTL or TB) are reset
- **Getting it wrong** means stale Criteria: a false negative on `code_modifying` means the session's agent won't know to re-run checks after the endpoint changes code

### Sandbox Boundary

For agent-facing MCP calls, the endpoint's Python orchestration runs inside the
Sandbox, as does any subprocess it starts. Booley Flows enforce that
boundary even when their Python module is invoked directly. A non-Flow custom
endpoint may support a host-side, read-only diagnostic entry point, but must not
use it to expose Flow or EDA execution. There is no configurable
execution-location contract for a custom endpoint.

The entire project root is mounted at `/work` in Docker, so custom MCP tool files are accessible inside the container without additional mount configuration.

### Implementation Examples

Choose `BooleyFlow`, `Specialist`, or direct `McpTool` from the contracts above,
implement only that base class's hooks, and return a consistent `McpToolResult`.
The examples below are followed by the common registration and exercise steps.

#### Implement a `BooleyFlow`

This example runs one project DRC command, applies its verdict to every selected
Target, and keeps infrastructure failures distinct from design failures:

```python
# .booley_project/mcp_tools/drc_check.py
import sys

from booley.flows.base import BooleyFlow, SubprocessResult
from booley.mcp.base import EXIT_ERROR, McpToolResult


class DrcCheckFlow(BooleyFlow):
    name = "drc_check"
    description = "Run project DRC rules against RTL"
    code_modifying = False
    satisfies = ["drc_clean"]

    def _add_args(self, parser):
        parser.add_argument("--rule-set", default="default")

    def _build_command(self):
        return [sys.executable, "scripts/run_drc.py", "--rules", self.args.rule_set]

    def _interpret_result(self, result: SubprocessResult) -> McpToolResult:
        targets = [item.strip() for item in self.args.target.split(",") if item.strip()]
        keys = [f"drc_clean_{target}" for target in targets] or ["drc_clean"]
        evidence = "\n".join(part for part in (result.stdout, result.stderr) if part)

        # Contract of scripts/run_drc.py: 0 = clean, 1 = violations;
        # every other code means the checker itself could not run.
        if result.timed_out or result.returncode not in {0, 1}:
            return McpToolResult(
                exit_code=EXIT_ERROR,
                report_text=(evidence or "DRC command could not run")[-2000:],
            )

        passed = result.returncode == 0
        for key in keys:
            self.set_criterion(key, passed)
        return McpToolResult(
            exit_code=0 if passed else 1,
            report_text=evidence[-2000:],
        )


if __name__ == "__main__":
    DrcCheckFlow().cli()
```

#### Implement a `Specialist`

```python
# .booley_project/mcp_tools/protocol_reviewer.py
from booley.specialists.specialist import Specialist
from booley.mcp.base import EXIT_ERROR, McpToolResult


class ProtocolReviewerSpecialist(Specialist):
    name = "protocol_reviewer"
    description = "LLM-powered protocol compliance review"
    code_modifying = False
    satisfies = ["protocol_compliant"]
    min_model = "standard"
    default_timeout = 1800
    agent_tools = ["Read", "Grep", "Glob"]
    workspace_access = "read_only"

    def _add_agent_args(self, parser):
        parser.add_argument("--scope", required=True, nargs="+")

    def _build_prompt(self):
        return (
            f"Review protocol compliance for: {' '.join(self.args.scope)}. "
            "End with exactly VERDICT: COMPLIANT or VERDICT: NON_COMPLIANT."
        )

    def _interpret_output(self, output: str, structured: dict | None) -> McpToolResult:
        lines = {line.strip() for line in output.splitlines()}
        if "VERDICT: COMPLIANT" in lines:
            passed = True
        elif "VERDICT: NON_COMPLIANT" in lines:
            passed = False
        else:
            return McpToolResult(
                exit_code=EXIT_ERROR,
                report_text="Specialist returned no valid VERDICT line",
            )
        self.set_criterion("protocol_compliant", passed)
        return McpToolResult(
            exit_code=0 if passed else 1,
            report_text=output[-3000:],
        )


if __name__ == "__main__":
    ProtocolReviewerSpecialist().cli()
```

#### Wire It In

Save each class under `.booley_project/mcp_tools/` and define every Criterion
named by `satisfies` as described in Chapter 3. An undefined Criterion draws a
shared endpoint-validation warning. Registering a project Criterion does not
make it selectable as a new Goal family.

#### Try It in Interactive Mode

Interactive Mode is the normal way to try a Custom Flow or Specialist. Once the MCP tool file exists, restart the Sandbox (for example, stop and reopen the dev container) so its MCP server rebuilds the MCP tool registry. Then ask the Claude Code or Codex agent to use it, just as you would a built-in capability:

- *"Run `drc_check` on the `variant_a` Target with the signoff rule set."*
- *"Ask `protocol_reviewer` to check `rtl/axi_slave.sv`."*

The agent selects the arguments and invokes the custom MCP tool. Goal Mode uses the same implementation and registry with an immutable Run Binding; evidence may update the declared Goals when its keys and subject match them. Outside Goal Mode, the verdict is returned to the session without persistent Goal state. While any Goal Record occupies the Project, every endpoint call must pass explicit `work_dir`, including diagnostic calls outside the Goal worktree: “Pass work_dir on every Booley call while Goal Mode is active”. This is enforced across endpoint dispatch, report-fetch, and Goal-tool paths rather than only for evidence-producing calls.

#### Diagnose a Flow Through the Direct CLI

`booley flow` is the diagnostic entry point for deterministic Flows. From a
terminal already inside the Sandbox:

```bash
booley flow drc_check --target variant_a --rule-set signoff
```

Invoke Specialists and other non-Flow MCP tools through the Interactive Mode
agent, which exercises their supported MCP interface.

You do not need `booley session enter` when VS Code or your terminal is already attached to the Sandbox. That command exists for headless automation that needs to enter the runtime without an Interactive Mode client.

Ordinary `booley flow` and direct typed calls use standalone execution and do
not capture a Goal Run Binding from the current worktree. They return diagnostic
verdicts without persisting Goal evidence; Goal verification should use the MCP
endpoint with explicit `work_dir`. Reports default to checkout-local Project
data (`flow-reports/` for Flows, `mcp-tool-reports/` for Specialists), unless the
caller supplies another report destination. Exit codes retain
their normal meaning: 0 = criterion met, 1 = ran and failed, 2 = unable to reach
a verdict.

The retained `BOOLEY_TICKET_FILE` environment adapter can still select Ticket
execution for `booley flow`; Phase 9a removes this compatibility path.

#### Make a Specialist Read-Only

Declare the workspace policy once, independently of the selected provider:

```python
class ProtocolReviewerSpecialist(Specialist):
    workspace_access = "read_only"
    agent_tools = ["Read", "Grep", "Glob"]
```

Booley runs a read-only Specialist's nested agent against a disposable snapshot containing the worktree's current tracked and ordinary untracked files. The snapshot includes uncommitted edits, so an interactive review sees the code on screen rather than only `HEAD`. The agent may write inside that private copy, but the copy is discarded after the call and absolute snapshot paths in its result are translated back to real-worktree paths. Git-ignored outputs and symlinks whose targets escape the worktree are omitted from the snapshot.

This snapshot is the cross-provider write boundary. `agent_tools` has a narrower job: it shapes the provider's agent loop. Claude understands names such as `Read`, `Grep`, and `Glob`; Codex has a different native-capability model and does not implement Claude's `disallowed_tools`. A custom Specialist should not override `_disallowed_tools()` merely to become read-only. Set `workspace_access` instead.

Built-in Specialists may still add provider-specific restrictions as defense-in-depth. For example, the Reviewer denies Claude's mutating and escaping capabilities:

```python
def _disallowed_tools(self) -> list[str] | None:
    return [
        "Bash",
        "BashOutput",
        "KillShell",
        "Write",
        "Edit",
        "MultiEdit",
        "NotebookEdit",
        "Task",
        "WebFetch",
        "WebSearch",
        "SlashCommand",
    ]
```

Category isolation is separate from write isolation. Some built-ins temporarily hide opposite-category sources through Booley's internal `workspace_isolation` helpers; `workspace_access = "read_only"` protects the real worktree from writes but does not hide files from the snapshot.

#### Find Its Logs

Interactive session logs land under `.booley_project/.interactive_logs/<session-id>/`. A bound Goal run keeps receipts and logs under the Goal Record's `logs/` and machine-owned reports under its `.runtime/flow-reports/`. Use the exact returned artifact paths. If a custom MCP tool does not appear, check its syntax and literal metadata, confirm the appropriate `[flows.<name>]` or `[specialists.<name>]` section is enabled, run Doctor's shared endpoint checks, and restart the Sandbox so MCP discovery runs again.

---

## Chapter 3: Goals, Criteria, and MCP Tool Routing

Goals are Goal Mode's mandatory success conditions. Only bound Booley Flow or
Specialist evidence can meet them; Finish checks current evidence for every Goal
(see [USAGE.md](../user/USAGE.md#working-with-evidence)). The implementation
translates Goals into shared Criteria definitions and evidence keys, then routes
producer `set_criterion()` updates to the declared Goals.
[Criterion](../../src/booley/ticket_board/GLOSSARY.md#execution-and-evidence)
remains live internal policy/evaluation vocabulary in `ticket_board/`,
`criteria/`, and `evidence/`, including the shared code used by Goal Mode and
Specialists. Phase 9a relocates the shared Ticket-owned implementations before
deleting `ticket_board/` and its glossary; Criteria do not define a second public workflow.

Entry accepts the fixed families in `goals.model.GoalFamily`: `lint`, `sim`,
`elab`, `synth`, `fpga`, `cycle_count`, `coverage`, `mutation`, and `review`.
Every per-Target Goal names its Target, and simulation Goals resolve exact Test
Runs. The agent translates Goalset prose into concrete entry arguments; entry
does not expand a project Criterion into a new family. Every Goal is mandatory;
changes require a human-approved Goal Change Proposal.

An implementation's literal `satisfies` metadata supplies the catalog relationship
between Criterion keys and producing endpoints. Built-in keys such as
`sim_pass_*`, `lint_clean_*`, and `synthesis_ok_*` are reused by Goal evidence.
Project Criteria remain in that catalog for extension validation and diagnostic
evaluation, but adding a name to `criteria.toml` or `satisfies` does not extend
Goal entry's schema. A Custom Flow can publish evidence for a declared supported
Goal only when its evidence contract, key, and subject match that Goal.

### Where Criteria Live

| Location | Purpose |
|----------|---------|
| Booley package `data/criteria.toml` | Base criteria (shipped with framework: sim, lint, etc.) |
| `.booley_project/criteria.toml` | Project-specific criteria you define |

Base criteria are read-only: look at them for format reference, but never redefine them in your project file (shared endpoint validation fails on collision).

### Criterion Schema

The base and project catalogs use the same TOML shape. A project definition looks like this:

```toml
[drc_clean]
description = "Project DRC rules pass"
workflow_region = "pre_sim"
per_target  = true
category    = "rtl"

[protocol_compliant]
description = "RTL complies with the reviewed protocol"
workflow_region = "post_sim"
per_target  = false
category    = "none"

[vendor_timing_ok]
description = "Vendor timing analysis passes for the selected Target"
workflow_region = "post_sim"
per_target  = true
category    = "rtl"
```

### Fields

| Field | Values | Meaning |
|-------|--------|---------|
| `description` | string | Human-readable purpose |
| `workflow_region` | `pre_sim`, `core_loop`, `post_sim` | The Workflow Region the criterion belongs to; organizes advisory capability guidance (see *Workflow Region* in [GLOSSARY.md](../GLOSSARY.md)) and never gates execution. Legacy key `phase` is still read |
| `per_target` | `true`/`false` | If true, expands to one criterion per target (e.g., `drc_clean_variant_a`, `drc_clean_variant_b`) |
| `category` | `rtl`, `tb`, `none` | Controls invalidation cascade |

**Invalidation cascade:** When a `code_modifying` endpoint runs and changes files, all Criteria whose `category` matches the type of files changed are marked unsatisfied and must be re-checked.

- `category = "rtl"`: reset when RTL files change
- `category = "tb"`: reset when TB files change
- `category = "none"`: never auto-invalidated (the endpoint must reset explicitly). **Warning:** `none` Criteria can go permanently stale if the implementation does not explicitly reset them after relevant code changes

### Rules

- Project criteria **cannot** override base criteria (hard error in shared endpoint validation, reported by Doctor)
- An MCP tool with empty `satisfies` gets a warning (probably misconfigured)
- Each Criterion family has one catalog endpoint binding. Conflicting claims raise `CriterionEndpointCatalogError`; repeated identical bindings are accepted rather than resolved by discovery order.
- While any Goal Record occupies the Project, every endpoint call without explicit `work_dir` is refused across dispatch paths, even if it is intended as a diagnostic call outside the Goal worktree.
- A Flow's Criterion contract is independent of whether a supported EDA installation is image- or host-provisioned

### Extending the Diagnostic Criterion Catalog

1. Choose a unique Criterion name that does not collide with the base catalog.
2. Add its `description`, `workflow_region`, `per_target`, and `category` fields to `.booley_project/criteria.toml`.
3. Add the base name to one custom MCP tool's literal `satisfies` list.
4. If invocation arguments differ by Criterion, add literal `satisfies_args` prompt hints.
5. For `per_target = true`, supply the expanded `<criterion>_<target>` key to `set_criterion()`.
6. Flow/Specialist headline fields are derived centrally from `set_criterion()` evaluations; generic `mcp_tool` endpoints retain their result-field contract.
7. Run `booley doctor`, then inspect the live catalog with `booley cheat --criteria`.
8. Exercise the endpoint diagnostically. To count in Goal Mode, its output must
   match a supported declared Goal; the project catalog cannot add a family.

---

## Chapter 4: Host-Provisioned EDA Policy Boundary

Host-provisioned EDA is not an MCP transport and not a Project extension point.
It is a trusted startup policy that makes approved installation files available
inside the Sandbox while leaving the ordinary Booley Flow and MCP
contracts unchanged.

### Authority and Issuance

The host authority stores three separate records:

- an **Installation Registration** identifies one supported tool release;
- an optional **License Profile** identifies one built-in, fixed licensing
  topology;
- an exact **Project Grant** authorizes one canonical Project root to use those
  opaque records.

Project configuration can request only the supported EDA kind and provisioning
source. The exact Project Grant is the sole selector for the opaque Installation
Registration and any License Profile. Project data cannot supply a host path,
Docker mount, image override, wrapper, environment variable, license
destination, or command.

Before Docker creates or resumes a runtime, Booley resolves the image to an
immutable identity and issues a stamped specification containing the exact
mount order, read-only flags, wrapper digest, Project identity, policy revision,
labels, and any licensing topology. Both VS Code Dev Containers and
`booley session` validate the same specification. Existing containers are
inspected rather than trusted by name; drift causes recreation or a fail-closed
error.

### MCP Contract

A host-provisioned tool is still invoked by its ordinary built-in Booley Flow.
The agent calls the same MCP schema, the Flow constructs the same
FuseSoC/Edalize build, the subprocess runs inside the Sandbox, and the
Flow produces the same `McpToolResult`, artifacts, and Criteria. Provisioning
changes only where approved executable files originate.

Custom MCP tools cannot request or synthesize host authority. A new commercial
EDA integration therefore requires a built-in installation policy, runtime
wrapper contract, Doctor probes, adversarial mount and lifecycle tests, and
full-Flow evidence before it can be added to
[SUPPORTED-EDA-TOOLS.md](../user/SUPPORTED-EDA-TOOLS.md).

### Licensing

When a built-in policy supports floating licensing, the runtime receives only
a fixed pointer to a session-owned relay. The Project and agent cannot choose
the upstream address or ports. The relay publishes no host port and owns the
only connection to its dedicated outbound network. Relay provisioning,
health, resume validation, revocation, reaping, and cleanup are part of the
runtime lifecycle rather than MCP tool behavior.

The current supported mounted-tool and experimental licensing status is
documented in [SUPPORTED-EDA-TOOLS.md](../user/SUPPORTED-EDA-TOOLS.md).

---

## Chapter 5: Validation and Diagnostics

MCP tool validation is split across the same boundaries as discovery. The in-container registry validates what it can expose; shared `mcp.endpoint_validation` checks custom-MCP-tool metadata and Criterion wiring, and Doctor composes those checks with initialized-Project diagnostics. Goal entry separately validates its worktree and concrete Goal arguments. None of these replaces an execution test of the real endpoint.

### Project Extension Checks

| # | Check | Severity |
|---|-------|----------|
| 1 | Python syntax errors in custom MCP tool files | Warning; AST discovery also cannot register the file |
| 2 | Missing literal `name` or `description` | Warning; AST discovery omits the class |
| 3 | No discoverable `McpTool` subclass in file | Warning; AST discovery omits the file |
| 4 | Custom MCP tool name collides with a discovered built-in MCP tool | Warning from validation |
| 5 | `satisfies` references undefined criterion | Warning |
| 6 | Project criteria redefines base criterion | **Hard fail** |
| 7 | Empty `satisfies` for an enabled MCP tool | Warning |

The Criterion collision in check 6 is a hard validation failure reported by Doctor. The other checks log diagnostics for the affected file. Registry discovery is separate, so a collision warning should not be treated as enforcement; fix it before running. Collision detection considers every installed built-in, independent of project `enabled` settings.

### Checking Project and Goal Entry Diagnostics

Run `booley doctor` for shared Project, custom endpoint, and Criteria validation. It reports aggregate health; per-file validation diagnostics use the shared validator's logging. Use `booley cheat --criteria` to inspect the live Criteria catalog. Goal entry refusals cover clean linked-worktree requirements, occupied records, Goal shapes, and bound inputs; they do not reproduce Ticket Preflight.

The Ticket Preflight wrapper is retained until Phase 9a removes it. `booley run` prints a Goal Mode pointer and exits 2 rather than starting intake or Preflight.

For built-in Booley Flows, use `booley doctor` to catch unavailable dependencies or incompatible project Targets, then invoke the Flow directly when diagnosing its arguments or EDA integration. The per-Flow evidence and artifact contracts are documented in [FLOW_IMPLEMENTATION.md](FLOW_IMPLEMENTATION.md).

### Extending It: Validate a Custom Flow

1. Run `booley doctor` and resolve every active custom-MCP-tool and Criterion finding.
2. Restart the Sandbox and confirm the MCP tool appears on the MCP surface.
3. Invoke it in Interactive Mode with a known passing case and a known failing case.
4. Use `booley flow <name> ...` for a Flow inside the Sandbox to isolate argument parsing and result interpretation from agent behavior; invoke a non-Flow endpoint through the Interactive Mode agent.
5. For a direct Flow, confirm exit 0, 1, and 2 mean met, unmet, and unable to run respectively; for a non-Flow endpoint, confirm the agent reports those verdict states clearly.
6. For Goal use, inspect `booley cheat --criteria`, enter with supported concrete Goals, and verify evidence publication and staleness with `goal_status`. A project-only Criterion is diagnostic unless it maps to a supported declared Goal; it cannot add a Goal family.

---

## Quick Reference

| I want to... | Do this |
|-------------|---------|
| Add an in-container endpoint | Write a `BooleyFlow`, `Specialist`, or direct `McpTool` subclass in `.booley_project/mcp_tools/`; discovery is automatic |
| Run a per-test build step before sim | `[flows.sim].pre_run_commands` ([CONFIG.md](../user/CONFIG.md#pre-sim-commands-flowssimpre_run_commands)) |
| Use host-provisioned Vivado | Follow [CONFIG.md](../user/CONFIG.md#commercial-eda-provisioning) for the Project request, [FLOW_IMPLEMENTATION.md](FLOW_IMPLEMENTATION.md#fpga) for the Flow contract, and [SUPPORTED-EDA-TOOLS.md](../user/SUPPORTED-EDA-TOOLS.md#vivado-host-provisioning-policy) for requirements |
| Add another host-provisioned EDA tool | Implement and validate a built-in policy; custom MCP tools cannot add host mounts or execution paths |
| Define when my Flow should run | Create a Criterion in `criteria.toml`, reference it in `satisfies` |
| Configure a built-in Booley Flow | Use the per-Flow reference in [CONFIG.md](../user/CONFIG.md#booleytoml) |
| Try a custom MCP tool | Restart the Sandbox, then ask the Interactive Mode agent to invoke it |
| Debug MCP tool discovery | `booley doctor` for aggregate checks; inspect shared endpoint-validation diagnostics |
| See base criteria for reference | Check `data/criteria.toml` in the Booley package |
| Wrap a legacy script as a Flow | Subclass `BooleyFlow`, call the script via `_build_command` |

## Built-in Flow calls

The built-in `sim`, `lint`, `synth`, and `fpga` endpoints expose the same
controls as their CLI ([FLOW_REFERENCE.md](../user/FLOW_REFERENCE.md)), with
these MCP-specific shapes.

**Target selection.** MCP keeps one comma-separated `target` string rather than
an array; caller order is preserved, as on the CLI.

**Verdict.** The call carries the Flow exit grade (`0`/`1`/`2`) in `EXIT_CODE:`
and in structured output. MCP `isError` is not the design verdict.

**Report.** The per-invocation report (fields in
[FLOW_REPORTS.md](FLOW_REPORTS.md#invocation-report)) is attached as
`structuredContent.reports[0]`, and `structuredContent.passed` repeats the
overall boolean verdict. If the report is too large for the MCP result,
`reports` is empty, `truncated` is `true`, and the result retains the Flow,
Target, exit code, and artifact pointers needed to open the durable report.

**Progress fallback.** For `progress.json` fallback evidence, `partial` is true
whenever the phase is not `complete` or pending Targets remain. Thus `aborted`
and `superseded` are terminal but partial, and neither appears as a running
checkpoint. After timeout or cancellation, the MCP supervisor attempts an
idempotent `aborted` repair only after it has reaped the child process; a
missing or unwritable checkpoint does not override the Job's exit or
cancellation result.

### Simulation input

The `sim` input takes test names as an array, not the former scalar shape:

```json
{"target":"sim_soc","test":["reset","interrupts"]}
```

MCP has no `tests_file` or `skip` property. It accepts exact test names directly
in `test`. Coverage selections use deterministic sorted test-name order for
Campaign identity and run numbering, regardless of array order. Plain
selections preserve array order (an omitted `test` follows the registry order);
HDL tests are scheduled in that order, while cocotb controls test-function
execution order within its filtered batch. `resume_from` names one manifest
and conflicts with `target`, `test`, explicit `mode`, `coverage`, `trace`, and `no_waivers`.

Structured campaign output reports `grade`, `complete`, aggregate
`observation_counts`, and a maximum-32 `observations` preview. Every preview
entry retains `test`, `execution`, `functional`, `assertions`,
`assertion_count`, nullable `cycle_count`, and bounded `detail`; `observation_total` and
`observations_truncated` disclose whether the preview is complete. The full
`report.json.cycle_counts` mapping (see
[FLOW_REPORTS.md](FLOW_REPORTS.md)) is dropped from inline output before the
64 KiB structured budget check; read the numbered report for all counts.
The independent observation axes mean:

- `execution`: whether the simulator completed, timed out, was guard-aborted,
  or failed before producing trustworthy test evidence;
- `functional`: the pass/fail/inconclusive test verdict;
- `assertions`: assertion evidence independently observed for that test.

Resolve the `manifest` artifact reference, then inspect its authenticated
terminal results for the complete durable record; the MCP preview is
intentionally not a replacement for those files.

### Simulation coverage input

The public `sim` schema exposes optional boolean `coverage` (default false).
CLI `--coverage` and permanent alias `--cov` map to the same request. Criteria
never activate it implicitly. All-Target Preflight precedes report allocation;
Icarus selection rejects atomically. Structured Target results preserve
simulation, collection, and evaluation independently and point to exact canonical
reports. The Coverage Analyst accepts the returned Campaign path in a separate
call. See [Flow contracts](FLOW_IMPLEMENTATION.md#coverage-campaign-orchestration)
for the ordered persistence and Criterion-evidence transaction.

## Reviewer evidence contract

Reviewer runs in both Interactive Mode and Goal Mode. Findings use the supplied
specification, steering, or concrete code behavior as their scope anchor; a
spec-review Goal names its specification file explicitly. Retained Ticket-bound
reviews still use their staged Ticket and accepted decisions until Phase 9a
removes that path. The shared Reviewer prompt owns
disposition and output instructions; category guides contain review checklists.

### Validation and dispositions

Reviewer validates the output schema and explicit source membership.
Specifications and Project policy inform the agent; phrase matching never
discards or rewrites valid dispositions. In-scope `current` findings can
make a `_clean` Criterion unmet. `advisory`, `deferred`, and `out_of_scope` findings
remain observations. A `_done` Criterion completes review regardless of
dispositions and preserves findings. In Goal Mode, a `done` Goal may Finish
with open findings exposed in the Review Package; those findings do not create
a human completion gate. A `clean` Goal requires current findings to be verified
fixed or explicitly waived with user-visible justification. The retained Ticket
review path still requires explicit approval of current findings before its
acceptance or completion, until Phase 9a removes it. Merging a Goal Branch is
ordinary Git outside Goal completion.

Filtered source proposals and malformed canonical, `ReportFindings` mirror,
and verification rows are separate non-gating audit evidence. Mixed valid and
invalid initial output keeps valid findings; all-invalid output or missing JSON
is a Specialist error. Invalid verification dispositions keep findings pending.
`FIXED` requires evidence and `WAIVED` requires justification.

### Publication and history

Review results have immutable JSON evidence under `reviewer-evidence/` in the
directory resolved by `booley.runtime.project_dir`, including Interactive Mode
and failures after input validation. Dry runs and input-validation failures do
not publish review evidence. Results include the path in `audit_evidence` and
`artifacts.reviewer_evidence`. Evidence is published atomically before a passing
Criterion is recorded.

Audit rows retain proposal ordinals, attempts, and phases within one contract.
A contract change archives the previous receipt and starts new audit history,
preserving open obligations and explicit disposition provenance. Historical
resolutions remain inspectable, but an active pending finding takes precedence
over any historical resolution with the same identity in normalized views.
Old automatic policy exclusions are historical only.

Live receipts predating the filtering revision require fresh discovery.
Historical accepted packages stay readable; previously discarded proposals
cannot be recovered. Audit evidence appears separately in review packages,
briefings, HTML explanations, and MCP results, without entering severity
counts, dispositions, acceptance predicates, or `review_report_required`.

## Report-driven Coverage Analyst

`coverage_analyst` accepts required `campaign` (one exact canonical `coverage.json`
path) and optional `instruction`. V3 or V4 input returns `booley.coverage-analysis/v2`
with the Campaign manifest and integrity-linked point-store digest as observed evidence.
The report carries immutable observed evidence, model-authored hypotheses and recommendations,
explicit limitations, source-access status, screened Waiver Candidates, and the exact
bounded evidence-retrieval scope.
The Analyst does not mark Goals met or mutate their evidence state. In Goal Mode,
the non-model wrapper records `ready_for_human_review` candidates in the bound
Goal Record's `waiver-candidates.json`, adds a
`waiver_candidate_record` detail (`recorded`, `filtered_by_rejection`,
`candidate_ids`, strict and provisional verdicts), and prints one summary line
`Waiver Candidates recorded N (filtered by rejection M); coverage strict X ·
provisional Y`. A recording failure reports `status: failed` without failing the
analysis. Outside a bound Goal Mode, ordinary Interactive analysis records no
candidates. The Ticket candidate-recording path is retained until Phase 9a
removes it; its store implementation is also reused by Goal Mode until relocation.
Invalid input or
malformed/model-incomplete output is an execution error; a valid advisory report
succeeds even when its Campaign records simulation failure or a coverage miss.

The wrapper checks canonical invocation/Target identity, the complete V3/V4
manifest/point-store relationship, and a matching completed Simulation projection
before model invocation. The deep module is
`analyze_coverage_campaign(campaign, sources, instruction)`; `CoverageAnalyzer`
constructor injection substitutes only the external text-model boundary.
`coverage_analyst(campaign: Path, instruction="")` is the default public composition.
Sources are an immutable complete fingerprint-verified snapshot, never file tools.
Native availability sidecars do not alter normalized measurement truth.

The model prompt contains only a compact Campaign reference. One isolated read-only
`coverage_evidence` tool exposes `overview`, filtered and cursor-paged `points`,
verified `source` excerpts, and cursor-paged `zero_point_sources` (sorted paths,
`limit` 1–100, optional `cursor`). The latter returns sources with no native
measurement records, independent of hits, point eligibility and waivers. It is
available in report-only mode, charges normal evidence byte budgets, and records
returned source counts with empty point IDs. Overview and its minimal fallback
retain discovery status, total source count, and a pagination hint. These findings
are advisory; they do not establish deadness or missing instantiation. Model-facing point records use deterministic short
`point_ref` values instead of the Campaign's long opaque IDs. A `point_ref` becomes
usable only after its exact point was successfully delivered within the response and
cumulative byte budgets; later `points` or `source` queries may pass delivered values
as `point_refs`. The host privately resolves them back to exact Campaign IDs before
publishing an advisory report. Point records include their complete eligible,
unscored, or waived disposition and Approved Waiver provenance. The host first
deep-validates the complete V3/V4 manifest/point-store pair, so paging and reference
resolution never weaken Campaign integrity. No other MCP tool is visible to this
model. The validated session retains references to immutable
Coverage Points rather than encoded population copies, uses an exact-ID index, derives
overview counts once, and keeps at most one filtered match set for consecutive pages.
Changing filters replaces that session-local cache; only returned page records are encoded.

Stored evaluation maps directly to closure recommendations:
`pass` → `coverage_ready`, `fail` → `coverage_not_ready`,
`blocked` → `coverage_evidence_blocked`, and
`not_requested` → `ungated_no_recommendation`.
Candidates identify exact points and remain `not_approved`: non-RTL/unscored,
unknown, duplicate, or invalid-reason candidates are `forbidden`; missing source
verification or evidence, or an `unreachable` point with observed hits, is
`investigate`; otherwise `ready_for_human_review` asks for a human accept or reject
through a coverage-waiver Goal Change Proposal. `unreachable` needs no model proof reference: approval writes
a `review` proof. The model never writes the record.

Capability-isolated Codex calls use a private exact-model catalog to remove model-provided
shell/patch/search tools and explicit startup settings to disable other tools,
apps, plugins and subagents. Claude uses an empty built-in tool list. Both receive
an empty temporary working directory, no project skills, and only the bound evidence
MCP server.
