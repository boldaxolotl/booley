# MCP Tools: Architecture, Execution, and Extension

This guide defines Booley's MCP tool framework and explains how Custom Flows
and custom MCP tools extend it. It covers discovery, the shared Python contracts,
Criteria, in-container execution, and validation. Custom MCP tools work in both
Interactive Mode and Ticket Mode. Host EDA provisioning is deliberately not a
custom-MCP extension surface: it requires a built-in, evidence-backed policy.

## Document boundary

The documentation is split by responsibility, not by reader type:

| Document | Owns |
|---|---|
| **This document** | The MCP tool framework: discovery, lifecycle, base classes, `McpToolResult`, Criteria routing, and Custom Flows and MCP tools |
| [BOOLEY-FLOWS.md](BOOLEY-FLOWS.md) | The implementation and evidence contracts of the built-in deterministic `sim`, `elab`, `lint`, `synth`, and `fpga` Booley Flows |
| [CONFIG.md](CONFIG.md) | The project configuration surface: exact keys, defaults, examples, `.core` design description, and `tests.toml` |
| [SUPPORTED-EDA-TOOLS.md](SUPPORTED-EDA-TOOLS.md) | The source-of-truth matrix of supported EDA engines, provisioning, trace support, and installation requirements |

This document may show small configuration fragments when an extension contract
needs context, but it does not define the configuration schema or document the
built-in flows. Follow the links above for those references.

## Read this first

This is an implementation-level guide. It assumes the vocabulary and whole-system model from:

- **[CONTEXT.md](CONTEXT.md)** — the controlled vocabulary. This guide leans on *Booley Flow*, *Target*, *Criterion*, *Developer Agent*, *Specialist*, *Session Runtime*, *Ticket Mode*, and *Workflow Region* as already-defined terms.
- **[ARCHITECTURE.md](ARCHITECTURE.md)** — how the Developer Agent, Specialists, and the Booley Flow contract fit together at run time.
- **[CONFIG.md](CONFIG.md)** — the configuration reference for `booley.toml`, `.core` files, `tests.toml`, EDA provisioning, and Pre-Run Commands.
- **[BOOLEY-FLOWS.md](BOOLEY-FLOWS.md)** — how built-in deterministic Booley Flows turn FuseSoC Targets into commands and normalize EDA output into evidence.
- **[SUPPORTED-EDA-TOOLS.md](SUPPORTED-EDA-TOOLS.md)** — which EDA tools and provisioning sources are supported and what each requires.

This guide owns the common MCP tool lifecycle and extension contract.

## Overview

The Developer Agent does not invoke an EDA command, project script, or Specialist directly. It calls a discovered MCP tool inside the Session Runtime. That MCP tool owns the request schema, execution, result interpretation, and any Criterion updates.

Booley has three agent-facing implementation families:

| Family | Built-in examples | Responsibility |
|--------|-------------------|----------------|
| `BooleyFlow` | `sim`, `elab`, `lint`, `synth`, `fpga` | Run deterministic work and normalize its evidence; these are Booley Flows in Booley's controlled vocabulary |
| `Specialist` | `reviewer`, `mutation_tester` | Run a focused LLM agent with a purpose-built prompt and interpret its response |
| Direct `McpTool` subclass | `submit_run_report` | Implement orchestration that is neither a deterministic Flow nor a Specialist |

Built-in and custom MCP tools share the same base interfaces and MCP surface. Their source differs, but the calling model does not. Every agent-facing MCP tool and every subprocess it launches runs inside the Session Runtime. A supported host-provisioned EDA installation changes where immutable tool files originate, not where the command executes.

### The Common Lifecycle

Every agent-facing call follows the same shape:

1. The MCP registry discovers an implementation and exposes its declared arguments.
2. The agent calls it by its discovered name.
3. The MCP tool validates common and endpoint-specific arguments.
4. A Booley Flow runs deterministic work inside the Session Runtime, a Specialist runs its agent loop, or a direct `McpTool` subclass performs its own orchestration.
5. The implementation interprets raw output into a `McpToolResult`.
6. In Ticket Mode, it also records every Criterion verdict it evaluated; the Harness reads persistent Criterion state when deciding whether the ticket may advance.

Interactive Mode uses the same registry and implementations, but it has no Ticket state. The result is returned to the current session without persisting Criteria.

### How Host-Provisioned EDA Fits

Host provisioning is an administrative startup operation, not an MCP
execution route:

1. A host administrator registers a supported installation and grants one
   exact Project root access.
2. The Project requests host provisioning without naming an installation.
3. Booley validates and stamps a runtime specification containing the fixed
   image, read-only mount, wrapper, labels, and optional licensing topology.
4. Docker creates or resumes the Session Runtime only if the issued contract
   and live container state still match.
5. The ordinary built-in Booley Flow launches the EDA subprocess inside the
   Session Runtime and interprets its evidence there.

Custom MCP code cannot add an arbitrary host path, command, environment
variable, license destination, or new commercial EDA policy. A new
host-provisioned EDA kind belongs in the built-in policy and support matrix
after equivalent security and full-Flow evidence.

```
┌──────────────────────────────────────────────────┐
│  Session Runtime (Docker)                        │
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

First check the [supported EDA tool matrix](SUPPORTED-EDA-TOOLS.md). If Booley already supports the workflow, configure the built-in Flow. Otherwise, use `BooleyFlow` for deterministic in-container subprocess logic, `Specialist` for LLM-powered work, or `McpTool` for other in-container orchestration. For a per-test build step, use [Pre-Run Commands](CONFIG.md#pre-run-commands-flowssimpre_run_commands). A missing commercial EDA policy cannot be replaced by a custom host wrapper.

---

## Chapter 1: Discovery, Visibility, and Configuration

MCP tools are discovered from either the installed Booley package or the project's `.booley_project/mcp_tools/` directory. Discovery is inclusive by default: every valid implementation is enabled unless its own configuration section says otherwise.

### Default Discovery and Explicit Opt-Out

```toml
[mcp_tools.reviewer]
enabled = false                 # remove one discovered Specialist MCP tool
```

- Built-in Flows are scanned from `booley.flows`; Specialists and other MCP tools are scanned from `booley.specialists`.
- Custom Flows and MCP tools are scanned from `.booley_project/mcp_tools/*.py`.
- `[flows.<name>].enabled = false` disables a Flow; `[mcp_tools.<name>].enabled = false` disables a Specialist or other non-Flow MCP tool.
- Visibility can still differ by runtime mode. Interactive Mode hides autonomous-only MCP tools such as `submit_run_report`; `tb_coder` is currently de-registered in all modes. Environment-level MCP filters also narrow nested or explicitly scoped servers, but they are not project registration.
- `booley flow` is the human diagnostic entry point for Booley Flows; the MCP tool diagnostic surface covers Specialists and non-Flow endpoints.

### Execution Boundary

Agent-facing MCP tools and the subprocesses they launch run inside the Session
Runtime. A custom endpoint that needs additional software adds it to a Project
image or uses a supported built-in EDA provisioning policy.

### Configuration Boundary

The framework reads Flow settings from `[flows.<name>]` and non-Flow endpoint
settings from `[mcp_tools.<name>]`. At this layer the shared effect is that
`enabled = false` removes the capability from normal discovery.

[CONFIG.md](CONFIG.md#booley-flow-execution-enabled) owns the exact TOML
schema, resolution order, defaults, and built-in per-Flow/endpoint settings. A
custom Flow can read its section with `_load_flow_config(name, work_dir)` from
`booley.flows.flow_config`. Discovery consumes `enabled` for direct endpoints
and Specialists; there is no generic `_load_tool_config()` API for additional
custom `[mcp_tools.<name>]` values, so an implementation that defines such values
must load and validate them explicitly.

### Summary: Discovery Rules

| MCP tool kind | Source | How enabled | Agent-visible? |
|-----------|--------|-------------|:---:|
| Built-in Flow | Installed `booley.flows` package | Enabled unless `[flows.<name>].enabled = false` | Yes, subject to mode-specific hiding |
| Built-in Specialist or endpoint | Installed `booley.specialists` package | Enabled unless `[mcp_tools.<name>].enabled = false` | Yes, subject to mode-specific hiding |
| Custom MCP tool | `.booley_project/mcp_tools/*.py` | Namespace depends on whether it is a Flow, Specialist, or direct endpoint | Yes, subject to mode-specific hiding |
Use unique MCP tool names. Preflight warns when a custom name collides with a discovered built-in MCP tool, but registry discovery is a separate pass, so the warning is not an enforcement boundary.

### Register a Custom Endpoint

Put one implementation with a unique name and literal `name` / `description`
metadata in `.booley_project/mcp_tools/<name>.py`; the file is the registration,
with no source allowlist. Chapter 2 covers implementation and exercise, and
Chapter 3 covers any project Criteria named by `satisfies`.

---

## Chapter 2: The Shared Python Contract

All MCP tool implementations inherit from `McpTool`. For agent-facing calls,
their Python orchestration runs inside the Session Runtime. The base classes
provide common argument parsing, report creation, Ticket-state integration,
change accounting, and exit-code handling. Built-in and custom implementations
use the same hooks.

Criterion-aware MCP endpoints can produce verdicts against *Criteria* (the named
pass/fail conditions a ticket gates on). This chapter explains how an
implementation declares and records those verdicts; Chapter 3 explains how the
Criteria themselves are defined and expanded.

### Base Classes

| Class | Contract | Built-in examples |
|-------|----------|-------------------|
| `BooleyFlow` | Run deterministic work and interpret its completed result | `sim`, `elab`, `lint`, `synth`, `fpga` |
| `Specialist` | Build a focused prompt, run an LLM agent loop, and interpret its output | `reviewer`, `mutation_tester` |
| `McpTool` | Implement orchestration directly when neither higher-level contract fits | `submit_run_report` |

Import `McpTool` / `McpToolResult` from `booley.mcp.base`, `Specialist`
from `booley.specialists.specialist`, and `BooleyFlow` from
`booley.flows.base`. The package `__init__` modules do not re-export them.

Every concrete implementation declares `name` and `description`. Criterion-aware implementations also declare `satisfies`; code-changing implementations declare `code_modifying = True` so successful edits invalidate stale evidence.

### Common CLI Arguments

Every MCP tool inherits a base argument set. A concrete implementation adds only its endpoint-specific flags:

| Arg | Meaning |
|-----|---------|
| `--work-dir` | Working directory (worktree root); defaults to the current directory |
| `--report-dir` | Where `report.json` lands; in ticket runs it defaults from the runtime env |
| `--target` | Which project Target to operate on (comma-separated). This is what `self.args.target` reads in the examples below |

The Target value is also what the `per_target` Criterion convention keys on.

### Execution Hooks

#### `BooleyFlow`

A command-backed `BooleyFlow` overrides `_add_args`, `_build_command`, and
`_interpret_result`. Complex built-ins and custom host wrappers may
override `_run()` instead. The minimal Custom Flow below uses the command-backed
contract without backend-specific machinery.

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

The shared `code_modifying` and `satisfies` attributes are explained below.

#### Direct `McpTool` Subclasses

A direct subclass implements `_run()` and returns a `McpToolResult`.
`submit_run_report` uses this shape because it writes Harness state rather than
wrapping a deterministic command or an LLM. A deterministic custom subprocess
normally remains a `BooleyFlow` so it inherits the common in-runtime lifecycle.

### McpToolResult

```python
McpToolResult(
    exit_code: int,           # 0 = met, 1 = unmet, 2 = unable to run
    criterion_key: str,       # which criterion was evaluated
    criterion_met: bool,      # did it pass?
    report_text: str,         # human-readable output (tail of log)
)
```

Criterion-aware results normally set these four fields. An unable-to-run result
may leave `criterion_key` empty and `criterion_met` at its default because no
Criterion verdict was reached. `McpToolResult` also carries optional fields,
most usefully `detail: dict` for structured evidence (written into
`report.json`). Token/cost fields are populated automatically by `Specialist`;
line-count fields (`lines_added`/`lines_removed`) are stamped by the base
`McpTool` for any code-modifying endpoint.

**`set_criterion()` vs. `McpToolResult`:** they are two different sinks.
`set_criterion(key, met)` writes the verdict into the persistent ticket state
(the file the harness gates completion on; direct standalone runs have no ticket state), while the
returned `McpToolResult` is the report contract: what lands in `report.json` and
what the Developer Agent reads back. Call `set_criterion` once per criterion
you evaluated (a multi-criterion MCP tool calls it several times); `criterion_key`
/ `criterion_met` on `McpToolResult` carry the headline verdict for the report.
Keep them consistent. The state file, not the report, is what completion is
judged on.

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
  [BOOLEY-FLOWS.md](BOOLEY-FLOWS.md).
- **Always work-dir-relative.** Absolute container paths are not portable
  artifact references. Paths are relative to the MCP tool's work dir,
  including when Ticket Mode places reports outside the
  ticket worktree.

A `--baseline` run is a deliberate exception: it executes in a throwaway
worktree, so paths relativized there could resolve to unrelated current-run
files from the real worktree. Baseline metrics therefore carry no `artifacts`
block.

Specialists that expose durable evidence follow the same convention. For
example, `mutation_tester` persists one log per mutant under the lock directory
and cites the collection through the `mutant_logs` role rather than pretending
that a shared `run.log` represents every mutant.

### `satisfies` and `satisfies_args`

Criterion-aware Flows, Specialists, and direct endpoints declare which Criteria
they can satisfy:

```python
class MyTool(BooleyFlow):
    satisfies = ["my_check"]        # list of criterion names
    satisfies_args = {}             # empty for simple Flows
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

`satisfies_args` are **prompt hints**: they tell the Developer Agent which CLI arguments to pass when invoking the MCP tool for a specific Criterion. They are not executed directly.

**Static-discovery limitation:** The MCP tool registry reads class metadata without importing the file, using Python's abstract syntax tree (AST). It can extract only literal values. A computed expression such as `satisfies = BASE + ["extra"]` therefore appears empty, and preflight warns about it.

### `per_target` Convention

When a Criterion has `per_target = true`, the framework expands it across the project Targets when the Criterion→MCP-tool map is built at run start (e.g., `drc_clean` × `[variant_a, variant_b, variant_c]` → `drc_clean_variant_a`, `drc_clean_variant_b`, `drc_clean_variant_c`). Custom Criteria expand across all Targets; built-in Flow-gated families (`sim_pass_*`, `lint_clean_*`, …) further filter by Target–Flow compatibility.

In your Flow code, use the convention:

```python
targets = [item.strip() for item in self.args.target.split(",") if item.strip()]
keys = [f"drc_clean_{target}" for target in targets] or ["drc_clean"]
for key in keys:
    self.set_criterion(key, passed)
```

### `code_modifying` Flag

When any endpoint declares `code_modifying = True`:
- After a successful (exit 0) run, a git diff triggers automatic criteria invalidation
- All criteria matching the modified category (RTL or TB) are reset
- **Getting it wrong** means stale Criteria: a false negative on `code_modifying` means the Developer Agent won't know to re-run checks after the endpoint changes code

### Runtime Boundary

For agent-facing MCP calls, the endpoint's Python orchestration runs inside the
Session Runtime, as does any subprocess it starts. Booley Flows enforce that
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
                criterion_key=keys[0],
                criterion_met=False,
                report_text=(evidence or "DRC command could not run")[-2000:],
            )

        passed = result.returncode == 0
        for key in keys:
            self.set_criterion(key, passed)
        return McpToolResult(
            exit_code=0 if passed else 1,
            criterion_key=keys[0],
            criterion_met=passed,
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
            criterion_key="protocol_compliant",
            criterion_met=passed,
            report_text=output[-3000:],
        )


if __name__ == "__main__":
    ProtocolReviewerSpecialist().cli()
```

#### Wire It In

Save each class under `.booley_project/mcp_tools/` and define every Criterion
named by `satisfies` as described in Chapter 3. An undefined Criterion draws a
preflight warning and can never be selected by a ticket.

#### Try It in Interactive Mode

Interactive Mode is the normal way to try a Custom Flow or Specialist. Once the MCP tool file exists, restart the Session Runtime (for example, stop and reopen the dev container) so its MCP server rebuilds the MCP tool registry. Then ask the Claude Code or Codex agent to use it, just as you would a built-in capability:

- *"Run `drc_check` on the `variant_a` Target with the signoff rule set."*
- *"Ask `protocol_reviewer` to check `rtl/axi_slave.sv`."*

The agent selects the arguments and invokes the custom MCP tool. Ticket Mode invokes the same implementation through the same registry; the difference is that its Developer Agent also supplies Ticket state and records Criterion results. Interactive Mode has no Ticket or persistent Criteria state, so the verdict is returned only to the current session.

#### Diagnose a Flow Through the Direct CLI

`booley flow` is the diagnostic entry point for deterministic Flows. From a
terminal already inside the Session Runtime:

```bash
booley flow drc_check --target variant_a --rule-set signoff
```

Invoke Specialists and other non-Flow MCP tools through the Interactive Mode
agent, which exercises their supported MCP interface.

You do not need `booley session enter` when VS Code or your terminal is already attached to the Session Runtime. That command exists for headless automation that needs to enter the runtime without an Interactive Mode client.

Direct Flow runs have no Ticket state: no Criterion is persisted, and
`report.json` is written only when `--report-dir` is supplied. Exit codes retain
their normal meaning: 0 = criterion met, 1 = ran and failed, 2 = unable to reach
a verdict.

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
    return ["Bash", "BashOutput", "KillShell", "Write", "Edit",
            "MultiEdit", "NotebookEdit", "Task", "WebFetch", "WebSearch",
            "SlashCommand"]
```

Category isolation is separate from write isolation. Some built-ins temporarily hide opposite-category sources through Booley's internal `workspace_isolation` helpers; `workspace_access = "read_only"` protects the real worktree from writes but does not hide files from the snapshot.

#### Find Its Logs

Interactive Mode logs land under `.booley_project/.interactive_logs/<session-id>/`; Ticket Mode logs land under `.booley_project/tickets/logs/<ticket-slug>/`. If a custom MCP tool does not appear in Interactive Mode, check its syntax and literal metadata, confirm the appropriate `[flows.<name>]` or `[mcp_tools.<name>]` section is not disabled, and restart the Session Runtime so MCP discovery runs again. In Ticket Mode, also check the Developer Agent output for preflight errors.

---

## Chapter 3: Criteria and MCP Tool Routing

Criteria are the success conditions of Ticket Mode: each Ticket declares mandatory and optional Criteria, and the Harness—not the agent—decides when they are met (see [USAGE.md](USAGE.md#acceptance-criteria)). An implementation's `satisfies` metadata builds the Criterion-to-MCP-tool map that tells the Developer Agent which capability can evaluate each condition.

Built-in families such as `sim_pass_*`, `lint_clean_*`, and `synthesis_ok_*` use exactly this mechanism. Project Criteria join the same catalog and routing map.

### Where Criteria Live

| Location | Purpose |
|----------|---------|
| Booley package `data/criteria.toml` | Base criteria (shipped with framework: sim, lint, etc.) |
| `.booley_project/criteria.toml` | Project-specific criteria you define |

Base criteria are read-only: look at them for format reference, but never redefine them in your project file (preflight hard-fails on collision).

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
| `workflow_region` | `pre_sim`, `core_loop`, `post_sim` | The Workflow Region the criterion belongs to; drives advisory ordering of Developer Agent activity (see *Workflow Region* in [CONTEXT.md](CONTEXT.md)) and never gates execution. Legacy key `phase` is still read |
| `per_target` | `true`/`false` | If true, expands to one criterion per target (e.g., `drc_clean_variant_a`, `drc_clean_variant_b`) |
| `category` | `rtl`, `tb`, `none` | Controls invalidation cascade |

**Invalidation cascade:** When a `code_modifying` endpoint runs and changes files, all Criteria whose `category` matches the type of files changed are marked unsatisfied and must be re-checked.

- `category = "rtl"`: reset when RTL files change
- `category = "tb"`: reset when TB files change
- `category = "none"`: never auto-invalidated (the endpoint must reset explicitly). **Warning:** `none` Criteria can go permanently stale if the implementation does not explicitly reset them after relevant code changes

### Rules

- Project criteria **cannot** override base criteria (hard error at preflight)
- An MCP tool with empty `satisfies` gets a warning (probably misconfigured)
- Multiple MCP tools can claim the same Criterion: the Criterion→MCP-tool map keeps one endpoint per Criterion (the last one discovered that claims it)
- A Flow's Criterion contract is independent of whether a supported EDA installation is image- or host-provisioned

### Extending It: Add a Project Criterion

1. Choose a unique Criterion name that does not collide with the base catalog.
2. Add its `description`, `workflow_region`, `per_target`, and `category` fields to `.booley_project/criteria.toml`.
3. Add the base name to one custom MCP tool's literal `satisfies` list.
4. If invocation arguments differ by Criterion, add literal `satisfies_args` prompt hints.
5. For `per_target = true`, set the expanded `<criterion>_<target>` key from the MCP tool result.
6. Keep the persistent `set_criterion()` verdict consistent with the headline `McpToolResult` verdict.
7. Run `booley doctor`, then inspect the live catalog with `booley cheat --criteria`.

---

## Chapter 4: Host-Provisioned EDA Policy Boundary

Host-provisioned EDA is not an MCP transport and not a Project extension point.
It is a trusted startup policy that makes approved installation files available
inside the Session Runtime while leaving the ordinary Booley Flow and MCP
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
FuseSoC/Edalize build, the subprocess runs inside the Session Runtime, and the
Flow produces the same `McpToolResult`, artifacts, and Criteria. Provisioning
changes only where approved executable files originate.

Custom MCP tools cannot request or synthesize host authority. A new commercial
EDA integration therefore requires a built-in installation policy, runtime
wrapper contract, Doctor probes, adversarial mount and lifecycle tests, and
full-Flow evidence before it can be added to
[SUPPORTED-EDA-TOOLS.md](SUPPORTED-EDA-TOOLS.md).

### Licensing

When a built-in policy supports floating licensing, the runtime receives only
a fixed pointer to a session-owned relay. The Project and agent cannot choose
the upstream address or ports. The relay publishes no host port and owns the
only connection to its dedicated outbound network. Relay provisioning,
health, resume validation, revocation, reaping, and cleanup are part of the
runtime lifecycle rather than MCP tool behavior.

The current supported mounted-tool and experimental licensing status is
documented in [SUPPORTED-EDA-TOOLS.md](SUPPORTED-EDA-TOOLS.md).

---

## Chapter 5: Validation and Diagnostics

MCP tool validation is split across the same boundaries as discovery. The in-container registry validates what it can expose, Ticket preflight checks custom-MCP-tool metadata and Criterion wiring, and Doctor checks the initialized project as a whole. None of these replaces an execution test of the real endpoint.

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

The Criterion collision in check 6 stops execution. The other checks log diagnostics for the affected file. Registry discovery is separate, so a collision warning should not be treated as enforcement; fix it before running. Collision detection considers every installed built-in, independent of project `enabled` settings.

### Checking Preflight Output

Preflight runs automatically at the start of every `booley run`; there is no standalone preflight command. `booley doctor` performs related aggregate checks for custom MCP tools and Criteria, but it does not reproduce every per-file preflight warning or print the Criterion-to-MCP-tool map. Use `booley cheat --criteria` to inspect the live Criteria catalog.

For built-in Booley Flows, use `booley doctor` to catch unavailable dependencies or incompatible project Targets, then invoke the Flow directly when diagnosing its arguments or EDA integration. The per-Flow evidence and artifact contracts are documented in [BOOLEY-FLOWS.md](BOOLEY-FLOWS.md).

### Extending It: Validate a Custom Flow

1. Run `booley doctor` and resolve every active custom-MCP-tool and Criterion finding.
2. Restart the Session Runtime and confirm the MCP tool appears on the MCP surface.
3. Invoke it in Interactive Mode with a known passing case and a known failing case.
4. Use `booley flow <name> ...` for a Flow inside the Session Runtime to isolate argument parsing and result interpretation from agent behavior; invoke a non-Flow endpoint through the Interactive Mode agent.
5. For a direct Flow, confirm exit 0, 1, and 2 mean met, unmet, and unable to run respectively; for a non-Flow endpoint, confirm the agent reports those verdict states clearly.
6. For Ticket use, inspect `booley cheat --criteria` and run a ticket that exercises persistent Criterion updates and invalidation.

---

## Quick Reference

| I want to... | Do this |
|-------------|---------|
| Add an in-container endpoint | Write a `BooleyFlow`, `Specialist`, or direct `McpTool` subclass in `.booley_project/mcp_tools/`; discovery is automatic |
| Run a per-test build step before sim | `[flows.sim].pre_run_commands` ([CONFIG.md](CONFIG.md#pre-run-commands-flowssimpre_run_commands)) |
| Use host-provisioned Vivado | Follow [CONFIG.md](CONFIG.md#commercial-eda-provisioning) for the Project request, [BOOLEY-FLOWS.md](BOOLEY-FLOWS.md#fpga) for the Flow contract, and [SUPPORTED-EDA-TOOLS.md](SUPPORTED-EDA-TOOLS.md#vivado-host-provisioning-policy) for requirements |
| Add another host-provisioned EDA tool | Implement and validate a built-in policy; custom MCP tools cannot add host mounts or execution paths |
| Define when my Flow should run | Create a Criterion in `criteria.toml`, reference it in `satisfies` |
| Configure a built-in Booley Flow | Use the per-Flow reference in [CONFIG.md](CONFIG.md#booleytoml) |
| Try a custom MCP tool | Restart the Session Runtime, then ask the Interactive Mode agent to invoke it |
| Debug MCP tool discovery | `booley doctor` for aggregate checks; inspect preflight logs for per-file warnings |
| See base criteria for reference | Check `data/criteria.toml` in the Booley package |
| Wrap a legacy script as a Flow | Subclass `BooleyFlow`, call the script via `_build_command` |
