# MCP Tools: Architecture, Execution, and Extension

This guide explains how Booley's MCP tools are discovered, called, and turned
into evidence, and how to add a [Custom Flow](../GLOSSARY.md#flows-eda-tools-and-mcp-tools),
Specialist, or other endpoint under `.booley/mcp_tools/`. It is for
contributors and agents who change the MCP framework or write a Project
extension. Read [GLOSSARY-MAP.md](../../GLOSSARY-MAP.md) and
[ARCHITECTURE.md](ARCHITECTURE.md) first; this guide assumes their vocabulary.
Neighboring docs own the rest:

| Document | Owns |
|---|---|
| [FLOW_REFERENCE.md](../user/FLOW_REFERENCE.md) and the per-Flow pages | Running built-in Booley Flows and reading their results |
| [FLOW_IMPLEMENTATION.md](FLOW_IMPLEMENTATION.md) | How the built-in `sim`, `lint`, `synth`, and `fpga` Flows build commands, run, and produce evidence |
| [FLOW_IMPLEMENTATION_SIM.md](FLOW_IMPLEMENTATION_SIM.md) | Simulation Flow internals: adapter seam, verdicts, Elaboration Check, coverage persistence and retention |
| [JOB_RECORDS.md](JOB_RECORDS.md) | How Jobs are recorded, owned, and found again |
| [FLOW_REPORTS.md](FLOW_REPORTS.md) | Report locations and JSON schemas of the built-in Flows |
| [SPECIALIST_IMPLEMENTATION.md](SPECIALIST_IMPLEMENTATION.md) | Evidence contracts of the built-in Reviewer and Coverage Analyst |
| [CONFIG.md](../user/CONFIG.md) | Every `booley.toml` key, default, and example |
| [SUPPORTED-EDA-TOOLS.md](../user/SUPPORTED-EDA-TOOLS.md) | Supported EDA tools, provisioning, and their requirements |

Check first whether you need an extension at all. If Booley already supports
the EDA tool, configure the built-in Flow. For a per-test build step, such as a
firmware compile, use
[Pre-Sim Commands](../user/FLOW-SIM.md#pre-sim-commands-flowssimpre_run_commands).
Write an extension for a Project-specific check that no built-in covers (DRC,
protocol compliance, custom lint rules) or for an LLM review with
Project-specific prompting.

## How an MCP call runs

The Developer Agent never starts an EDA program, Project script, or Specialist
itself. It calls an MCP tool inside the Sandbox, and that tool owns the request
schema, execution, result interpretation, and any Goal evidence update. Keeping
that ownership in one place is what lets Booley trust the evidence: the agent
chooses arguments, never verdicts.

Endpoints come in three families:

| Family | Built-in examples | Use it for |
|---|---|---|
| Booley Flow | `sim`, `lint`, `synth`, `fpga` | Deterministic subprocess work whose output becomes evidence |
| Specialist | `reviewer`, `coverage_analyst`, `mutation_tester` | A focused job given to a model with fresh context (`mutation_tester` uses the base without a model call) |
| Direct `McpTool` subclass | none | Any other orchestration |

Built-in and Project endpoints share the base classes, the registry, and the
MCP surface; only their source location differs.

### The common lifecycle

Every call follows the same lifecycle. How the resulting Job is recorded,
owned, and found again is in [JOB_RECORDS.md](JOB_RECORDS.md).


1. The registry discovers the implementation and exposes its arguments as an
   MCP schema.
2. The Developer Agent calls it by name; the server validates the arguments.
3. The shared coordinator in `booley.runtime.endpoint_execution` checks Target
   binding and admits the call as a [Job](../GLOSSARY.md#execution), holding
   its Job Class slot until the call completes.
4. The endpoint runs: a Flow runs its subprocess, a Specialist runs its model
   call, a direct endpoint runs its own code.
5. The endpoint turns the raw output into an outcome (`McpToolResult`).
6. The coordinator records acceptance before it persists any mutable state or
   report, then releases admission. In Goal Mode, evidence is appended under
   the call's [Run Binding](../../src/booley/goals/GLOSSARY.md) first; outside
   Goal Mode nothing persists as Goal evidence.

Recording acceptance before persistence means a crash can never leave a report
that claims evidence the Goal Record does not hold. If that final recording
fails, the call returns exit 2 with a structured `completion_error`, keeps the
result and Target facts it already has, skips the mutable persistence, and
makes one attempt to write a recovery report. A failure while updating a
Criterion during the run follows the ordinary error path instead; see
[Failures](FLOW_IMPLEMENTATION.md#failures).

The coordinator takes an already-prepared request, so it contains no CLI or MCP
request shape: the `booley flow` CLI and the MCP server reach the same code,
and the MCP side keeps only discovery, schema validation, and wire payloads.

The MCP server, every Flow, and every Specialist refuse to start outside the
Sandbox, so an extension and every subprocess it starts run there too. An
endpoint that needs extra software gets it from a
[custom Sandbox Image](../user/SANDBOX.md#custom-sandbox-image).

## Discovery and visibility

The registry reads endpoint metadata from Python source with the standard
`ast` module and never imports the file, so a broken or slow Project module
cannot crash or stall discovery. It scans:

- the built-in Flow packages `booley.flows.{sim,lint,synth,fpga}`;
- the built-in Specialists in `booley.specialists`;
- the Project's `mcp_tools/*.py` in the selected Booley Directory (by default
  `.booley/mcp_tools/`). Files whose name starts with `_` are skipped.

A file registers the first class whose direct base is named `BooleyFlow`,
`Specialist`, or `McpTool` and whose `name` and `description` are string
literals. A class that inherits through your own intermediate base is not
found. The protocol utilities (`booley_status`, `booley_poll`,
`booley_cancel`, `booley_targets`, `booley_report`, the Goal tools, `bwave`,
`booley_sleep`, and `coverage_evidence`) are defined in `booley.mcp.catalog`
rather than discovered.

Discovery is inclusive: every valid implementation is enabled unless
configuration turns it off.

| Endpoint kind | Turned off by |
|---|---|
| Flow (built-in or custom) | `[flows.<name>].enabled = false` |
| Specialist (built-in or custom) | `[specialists.<name>].enabled = false` |
| Direct `McpTool` endpoint | Renaming its file with a leading `_` |
| Protocol utility | Nothing; it has no Project switch |

[CONFIG.md](../user/CONFIG.md#flow-and-specialist-availability-enabled) owns
the exact keys. `booley.toml` tables `[tools]` and `[mcp_tools]` are rejected
with an error that says where each setting goes. Doctor and discovery also
reject a `[specialists.<name>]` table that names no discovered Specialist.

Endpoint names must be unique. Discovery refuses a catalog with two enabled
endpoints of the same name, so a custom endpoint that reuses a built-in name
blocks the tool list until it is renamed.

### Environment filters

Server environment variables decide which enabled endpoints a server lists.
They let Booley give each nested Specialist model only the tools its job
needs:

- A nested agent's server (`BOOLEY_NESTED_AGENT=1`) lists only the names in
  `BOOLEY_NESTED_MCP_TOOLS`. Otherwise `BOOLEY_MCP_TOOLS`, when set, limits
  the list; when unset, every enabled endpoint is listed.
- `booley_status` and `booley_report` appear on an interactive server
  (`BOOLEY_MCP_MODE=interactive`) or when `BOOLEY_MCP_TOOLS` names them.
- Interactive, non-nested servers list `goal_enter`, `goal_status`, `goal_propose_change`, and `goal_finish`; these Goal tools ignore `BOOLEY_MCP_TOOLS`, and other servers never list them.

Changing these variables needs a new server process.

### Live refresh

Each tool-list request rereads the selected Booley Directory's `booley.toml`.
Enabling or disabling an endpoint, or adding, removing, renaming, or editing a
custom endpoint, refreshes the running catalog, and the server sends
`notifications/tools/list_changed`; clients must listen for it or request a
fresh list. Booley never publishes a partial catalog: when a refresh fails,
for example on an invalid `booley.toml` or a duplicate name, the request fails
and the last complete catalog stays in place. A custom file that cannot be
parsed simply drops out of the list.

### `work_dir` routing

The Developer Agent passes the Goal worktree's absolute root as `work_dir` to
every tool that accepts it.

Built-in MCP worktree routing:

| Accepts `work_dir` | Does not accept `work_dir` |
| --- | --- |
| `sim`, `lint`, `synth`, `fpga` | `booley_status` |
| `reviewer`, `mutation_tester`, `coverage_analyst` | `booley_poll`, `booley_cancel` |
| `goal_enter`, `goal_status`, `goal_propose_change`, `goal_finish` | `booley_sleep`, `coverage_evidence` |
| `booley_targets`, `booley_report` | `bwave` |

`booley_sleep` appears only when diagnostic tools are enabled, and
`coverage_evidence` only in the Coverage Analyst's nested server.

While any Goal Record occupies the Project, or one is unreadable, Booley Flows,
Specialists, Project endpoints, Goal tools, and `booley_report` refuse a call
that names no checkout (no `work_dir` and no caller in a linked worktree),
even a diagnostic call meant for another checkout; `booley_targets` may omit
it. The refusal exists so a forgotten `work_dir` cannot silently run on the
main checkout while the agent edits a Goal worktree.

A call that omits `work_dir`, or passes an empty one, targets the checkout of
the calling agent's session process when that process's cwd is the root of a
linked worktree of the server's repository other than the server cwd. The
session process is the Claude Code peer, or the owner of a Codex session that
`booley chat --no-daemon` started; ordinary and hand-started Codex never route,
because a shared daemon's cwd belongs to no particular session. Any other
caller gets the server cwd, which in the Sandbox is the main checkout. The
checkout is filled in after schema validation and only for tools that accept
`work_dir`; an explicit `work_dir` always wins. A routed call counts as
explicit for Goal binding. A main-checkout caller is not routed and still gets
the Goal refusal.

Endpoints built on the shared parser get `work_dir` in their schema
automatically. A custom `mcp_schema()` hook that drops it makes the endpoint
unusable from a linked worktree: Booley refuses that routed call rather than
run it on the server cwd while the Dashboard shows the worktree.
Agent-facing guidance still tells the agent to pass `work_dir` explicitly.
The decision is in
[ADR 0068](../adr/0068-booley-dashboard.md#amendments-2026-11-08-the-callers-checkout-routes-calls-without-work_dir-2606).

## The Python contract

Import the bases from their defining modules; the package `__init__` files do
not re-export them:

| Class | Import from | Hooks you implement |
|---|---|---|
| `BooleyFlow` | `booley.flows.base` | `_add_args`, `_build_command`, `_interpret_result` (or `_run`) |
| `Specialist` | `booley.specialists.specialist` | `_add_agent_args`, `_build_prompt`, `_interpret_output` |
| `McpTool`, `McpToolResult` | `booley.mcp.base` | `_add_args`, `_run` |

The built-in Flows use a separate `BuiltinFlow` base with typed requests (see
[Code structure](FLOW_IMPLEMENTATION.md#code-structure)); `BooleyFlow`, `Specialist`, and
`McpTool` keep the argparse-based contract that Project extensions depend on.

Every implementation declares literal `name` and `description` class
attributes. Criterion-aware endpoints also declare `satisfies`, and endpoints
that edit sources declare `code_modifying = True`.

### Common arguments

Every endpoint inherits these arguments and adds only its own:

| Argument | Meaning |
|---|---|
| `-C`/`--project PATH` | Project Root, on the human CLI; MCP and the internal parser use `work_dir` |
| `--report-dir` | Where `report.json` lands; a Goal-bound run defaults to its Goal Record's reports |
| `--target` | The Target or Targets to operate on; repeat it or pass a comma-separated list. `self.args.target` always holds one comma-separated string |
| `--diagnostic` | Run without satisfying Goals |

The MCP schema is generated from the same argparse parser, so a new argument
reaches both the CLI and MCP without extra code. An endpoint may supply its
own schema with an `mcp_schema()` method.

Put `booley flow` options such as `-C` before the endpoint name
(`booley flow -C ../other drc_check`): once the CLI reaches an option it does
not know, it stops looking for its own. An option your endpoint defines under
the same spelling as a shared one keeps your meaning.

### `BooleyFlow`

`BooleyFlow` runs one subprocess per call: override `_add_args` to add
arguments, `_build_command` to return the argument list, and
`_interpret_result` to turn the `SubprocessResult` into an `McpToolResult` and
Criterion updates. The base handles the timeout, process-tree cleanup, and
output capture. A Flow that needs more than one subprocess overrides `_run()`
instead. `_load_flow_config(name, work_dir)` from `booley.flows.flow_config`
returns its `[flows.<name>]` table.

Booley points Python bytecode and pytest caches for Project endpoints and their
subprocesses at runtime storage, so a Custom Flow must not depend on cache
files in the source checkout.

### `Specialist`

The `Specialist` base runs the model call: override `_add_agent_args` to add
arguments, `_build_prompt` to build the prompt, and `_interpret_output` to turn
the model output into an `McpToolResult` and Criterion updates. Do not
override `_add_args`; the base uses it to register `--instruction`,
`--transcript-dir`, `--max-turns`, and `--timeout`, then calls
`_add_agent_args`.

| Class attribute | Meaning |
|---|---|
| `default_timeout`, `min_timeout` | Default and minimum model-call timeout, in seconds (1800 and 600) |
| `agent_capabilities` | Agent CLI capabilities the model may use, such as `["Read", "Grep", "Glob"]`; shapes Claude's agent loop, does not enforce file access |
| `workspace_access` | `"read_write"` (default) or `"read_only"`, which runs the model on a disposable snapshot |

The parsed timeout is `self.args.timeout_ms`, in milliseconds; for a
seconds-based `AgentCallParams`, use `self.timeout_seconds()`, which rounds up.
`--max-turns` and `--timeout` are human CLI controls and never appear in a
Specialist's MCP schema, even one from `mcp_schema()`.

A built-in Specialist's model comes from its `[models]` key. `[models]` accepts
only built-in Specialist names, so a Project Specialist always runs on the
built-in Specialist model of the Project's default Agent CLI.

### Direct `McpTool` subclasses

A direct subclass implements `_add_args` and `_run()` and returns an
`McpToolResult`. Prefer `BooleyFlow` for a deterministic subprocess: it gets
the Flow's admission, timeout, and freshness handling for free.

### `McpToolResult` and `set_criterion()`

```python
McpToolResult(
    exit_code: int = 0,             # 0 = met, 1 = ran and unmet, 2 = could not reach a verdict
    criterion_key: str = "",        # Criterion this result reports
    criterion_met: bool | None = False,
    detail: dict = {},              # structured evidence, written into report.json
    report_text: str = "",          # human-readable output, usually a log tail
)
```

Import `EXIT_ERROR` (2) and `EXIT_FAILURE` (1) from `booley.mcp.base` next to
it. Keep 1 and 2 apart: 1 says the design failed the check, 2 says the check
itself could not run, and only 1 may count against the design.

Call `self.set_criterion(key, met)` for each Criterion the run evaluates.
For a Flow or Specialist, Booley derives `criterion_key` and `criterion_met`
from those calls, so do not set them yourself: in a Goal-bound run, exactly one
mapped and evaluated Criterion gives its key and verdict; none, several, or
several selected Targets give an empty key and `None`. A call without Goal
binding, including an Interactive Mode MCP call, gets an empty key and `None`,
and its report omits both fields, so report readers must treat them as
optional. A direct `McpTool` endpoint reports the fields it sets, with
`criterion_met` defaulting to `False`.

The `Specialist` base fills the token and cost fields, and the base class adds
`lines_added`/`lines_removed` for code-modifying endpoints. Finish judges a
Goal only on persisted, current evidence, never on a result's headline.

### Common artifact contract

Reports with durable outputs carry an `artifacts` block: two entry-point files
plus the **directories** holding everything else.

```json
"artifacts": {
  "log":    ".booley/.runtime/edalize/synth/synth_soc/run.log",
  "report": ".booley/.runtime/flow-reports/synth_synth_soc.json",
  "dirs": {
    "build":  ".booley/.runtime/edalize/synth/synth_soc/synth",
    "timing": ".booley/.runtime/edalize/synth/synth_soc/synth/reports/timing"
  }
}
```

| Key | Meaning |
|---|---|
| `log` | This run's primary log, often outside the build directory |
| `report` | The durable structured report itself |
| `dirs` | `{role: path}` for artifact directories, such as `build`, `timing`, `impl`, `synth`, or `mutant_logs` |

The block appears at the top level of the durable report and inside the result
`detail`, which reaches the agent as MCP `structuredContent`. That copy
survives a tail-truncated log and the 64 KiB limit on structured output. The
contract names directory roles rather than files because EDA tools rename and
add output files far more often than the roles change; a reader lists the
directory when it needs everything. A multi-Target Flow nests one block per
Target (`artifacts[target]`). Two rules make every pointer trustworthy:

- **Never present when wrong.** Omit a key when its file or directory does not
  exist or cannot be shown to belong to this run. Each Flow's freshness rules
  are in [FLOW_IMPLEMENTATION.md](FLOW_IMPLEMENTATION.md) and
  [FLOW_IMPLEMENTATION_SIM.md](FLOW_IMPLEMENTATION_SIM.md).
- **Always relative to the work dir.** Absolute Sandbox paths do not carry
  over to the host. Paths are relative to the endpoint's work dir, even when
  Goal Mode writes reports into a Goal Record outside that worktree.

A `--baseline` run reports no `artifacts` block: it runs in a throwaway
worktree, so its relative paths would resolve to the current run's files.

For `synth` and `fpga`, `detail.implementation.results[target]` also carries
the grade, QoR metrics, baseline deltas, cache summary, and artifact pointers.
When the result is too large, Booley drops embedded recipe snapshots and
diagnostic excerpts first, then per-clock detail, then whole Targets in a fixed
order, and lists what it dropped; it never trades the QoR for links alone.

`mutation_tester` follows the same convention: each run writes one
self-contained campaign directory whose manifest cites the baseline log, one
log per mutant, the mutation specifications, results, and source variants. Its
`mutant_logs` role points at that run's copy, not the shared runtime
directory.

### `satisfies`, `satisfies_args`, and `per_target`

`satisfies` lists the Criteria an endpoint can evaluate.
`satisfies_args` maps a Criterion to the CLI arguments that evaluate it; Booley
shows them to the Developer Agent as hints and never runs them:

```python
class MultiCheck(Specialist):
    satisfies = ["check_a", "check_b"]
    satisfies_args = {"check_a": "--mode a", "check_b": "--mode b"}
```

Both must be literals. Discovery reads them without importing the file, so a
computed value such as `satisfies = BASE + ["extra"]` reads as empty, and
validation warns about it.

A Criterion with `per_target = true` expands to one key per Target, such as
`drc_clean_variant_a`. Set the expanded keys:

```python
targets = [item.strip() for item in self.args.target.split(",") if item.strip()]
for key in [f"drc_clean_{target}" for target in targets] or ["drc_clean"]:
    self.set_criterion(key, passed)
```

### `code_modifying`

After a successful run of an endpoint with `code_modifying = True`, Booley
classifies the git diff as RTL or testbench changes and resets every Criterion
whose `category` matches. Leaving the flag off on an endpoint that edits code
leaves those Criteria looking current. Goal status also compares source and
Target fingerprints on its own, so a Goal never stays met on stale evidence
because of a missing flag.

## Write a custom endpoint

Pick the base from the table above, implement only its hooks, and save the file
under `mcp_tools/` in the selected Booley Directory. The file is the
registration. Define every Criterion it names in `satisfies` (see
[Criteria](#criteria)); an undefined one draws a validation warning.

### Example: a `BooleyFlow`

This Flow runs a Project DRC script, applies its verdict to every selected
Target, and keeps a broken checker apart from a failing design:

```python
# .booley/mcp_tools/drc_check.py
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

### Example: a `Specialist`

```python
# .booley/mcp_tools/protocol_reviewer.py
from booley.specialists.specialist import Specialist
from booley.mcp.base import EXIT_ERROR, McpToolResult


class ProtocolReviewerSpecialist(Specialist):
    name = "protocol_reviewer"
    description = "LLM-powered protocol compliance review"
    code_modifying = False
    satisfies = ["protocol_compliant"]
    agent_capabilities = ["Read", "Grep", "Glob"]
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

### Read-only Specialists and source isolation

Set `workspace_access = "read_only"` to keep a Specialist from changing the
worktree. Booley then runs its model against a disposable snapshot of the
worktree's tracked and ordinary untracked files, uncommitted edits included, so
an interactive review sees the code on screen rather than only `HEAD`. The
model may write inside the snapshot; Booley discards it after the call and
maps snapshot paths in the result back to real worktree paths. Git-ignored
files and symlinks that point outside the worktree are left out.

The snapshot is the write boundary for both Agent CLIs. `agent_capabilities`
only narrows Claude's agent loop, and Claude treats that list as advisory when
it runs without permission prompts; Codex has no equivalent. So a custom
Specialist becomes read-only through `workspace_access`, not through a
capability list. Built-in Specialists may still deny Claude capabilities as an
extra layer: the Reviewer denies `Bash`, `BashOutput`, `KillShell`, `Write`,
`Edit`, `MultiEdit`, `NotebookEdit`, `Task`, `WebFetch`, `WebSearch`, and
`SlashCommand`.

[Specialist Source Isolation](../GLOSSARY.md#flows-eda-tools-and-mcp-tools)
uses the same snapshot. A Specialist whose `_workspace_isolation_category()`
returns a category works on a copy without the opposite category's sources;
shared paths stay visible and the real worktree keeps every file.
`workspace_access = "read_only"` alone hides nothing.

### Try it

Once the file exists, the running server picks it up at the next tool-list
request, without restarting the Sandbox. Then ask the Developer Agent to run it
as you would a built-in:

- *"Run `drc_check` on the `variant_a` Target with the signoff rule set."*
- *"Ask `protocol_reviewer` to check `rtl/axi_slave.sv`."*

The agent selects the arguments and invokes the MCP tool. In Goal Mode the same implementation runs under the Goal's Run Binding, and its evidence can count for a declared Goal when the evidence key and subject match. Outside Goal Mode the verdict comes back to the session and nothing persists as Goal evidence.

To take the agent out of the loop while you debug arguments or result
handling, run the endpoint from a Sandbox terminal
([entering the Sandbox](../user/SANDBOX.md#entering-the-sandbox-without-vs-code)):

```bash
booley flow drc_check --target variant_a --rule-set signoff
booley specialist protocol_reviewer --scope rtl/axi_slave.sv
```

A direct `McpTool` endpoint has no CLI command; exercise it through the agent.
CLI runs never record Goal evidence: they write reports to the checkout's
Booley Directory (`flow-reports/` for Flows, `mcp-tool-reports/` for
Specialists) unless you pass `--report-dir`, and return the usual exit codes.
Verify Goal evidence through a Goal-bound MCP call with `work_dir`.

### Where its output goes

An Interactive Mode MCP server writes its logs under
`.interactive_logs/<session-id>/` in the Main Booley Directory. A Goal-bound
run keeps logs under the Goal Record's `logs/` and reports under its
`.runtime/flow-reports/`. Use the paths the result returns rather than
guessing.

If a custom endpoint does not appear, check its syntax and literal metadata,
check that its `[flows.<name>]` or `[specialists.<name>]` table does not
disable it, run `booley doctor`, and request a fresh tool list.

## Criteria

A [Goal](../GLOSSARY.md#execution) is Goal Mode's mandatory success condition;
a [Criterion](../GLOSSARY.md#execution-and-evidence) is the internal
condition-plus-evidence form that Goal Mode translates its Goals into. Producers
call `set_criterion()`; Booley routes those updates to the declared Goals, and
Finish checks current evidence for every Goal (see
[GOAL-MODE.md](../user/GOAL-MODE.md#working-with-evidence)).

Goal entry accepts only the fixed families in `goals.model.GoalFamily`:
`lint`, `sim`, `elab`, `synth`, `fpga`, `cycle_count`, `coverage`, `mutation`,
and `review`. A Project Criterion does not add a family. A Custom Flow's
evidence counts for a Goal only when its evidence key and subject match a Goal
of one of those families; otherwise its Criteria are diagnostic. The fixed
list keeps every Goal judged by evidence whose meaning Booley defines.

### The Criterion catalog

| Location | Holds |
|---|---|
| `data/criteria.toml` in the Booley package | Base Criteria for the built-in endpoints |
| `criteria.toml` in the Booley Directory | Project Criteria for custom endpoints |

A Project file may not redefine a base Criterion; validation fails on a
collision. The catalog maps each Criterion to the one endpoint that claims it
in `satisfies`; two endpoints claiming the same Criterion raise
`CriterionEndpointCatalogError`, while identical repeated claims are accepted.

```toml
[drc_clean]
description     = "Project DRC rules pass"
workflow_region = "pre_sim"
per_target      = true
category        = "rtl"

[protocol_compliant]
description     = "RTL complies with the reviewed protocol"
workflow_region = "post_sim"
per_target      = false
category        = "none"
```

| Field | Values | Meaning |
|---|---|---|
| `description` | string | What the Criterion checks |
| `workflow_region` | `pre_sim`, `core_loop`, `post_sim` | When it applies: `pre_sim` (before simulation), `core_loop` (the edit-and-test loop), `post_sim` (after simulation); it only orders advice and never blocks a run |
| `per_target` | `true`, `false` | Whether it expands to one key per Target |
| `category` | `rtl`, `tb`, `none` | Which source changes reset it |

A `code_modifying` run that changes RTL resets every `rtl` Criterion, and one
that changes testbench files resets every `tb` Criterion. A `none` Criterion is
never reset automatically, so its endpoint must reset it after relevant changes
or it goes stale. Booley still reads the key `phase` in place of
`workflow_region`, with a warning; rename it.

To add a Criterion:

1. Pick a name that is not in the base catalog.
2. Add its four fields to the Project `criteria.toml`.
3. Add the base name to one custom endpoint's literal `satisfies` list, and
   `satisfies_args` hints if the arguments differ per Criterion.
4. For `per_target = true`, call `set_criterion()` with the expanded
   `<criterion>_<target>` keys.
5. Run `booley doctor`, then check the live catalog with `booley cheat --goals`.

## Validation and diagnostics

Shared validation in `booley.mcp.endpoint_validation` checks custom endpoint
metadata and Criterion wiring; `booley doctor` runs it alongside its Project
checks. Goal entry validates its worktree and Goal arguments separately. None
of these replaces running the endpoint on a real case.

| Check | Result |
|---|---|
| Python syntax error in a custom endpoint file | Warning; the file is skipped |
| No `BooleyFlow`, `Specialist`, or `McpTool` subclass with literal `name` and `description` | Warning; the file is skipped |
| Name matches a built-in endpoint | Warning; its remaining checks are skipped |
| `satisfies` names an undefined Criterion | Warning |
| Enabled endpoint with an empty `satisfies` | Warning |
| Project Criterion redefines a base Criterion | **Validation fails** |

Warnings go to the validator's log; Doctor shows the aggregate result.
Validation skips a disabled custom endpoint, but checks a name collision
against every built-in, enabled or not.

To validate a new endpoint:

1. Run `booley doctor` and fix every endpoint and Criterion finding.
2. Request a fresh tool list and confirm the endpoint is listed.
3. Run it through the Developer Agent on a known-good and a known-bad case.
4. Run a Flow with `booley flow` or a Specialist with `booley specialist` and
   confirm exit 0, 1, and 2 mean met, unmet, and could not reach a verdict.
5. For Goal use, enter Goal Mode with a supported Goal and confirm with
   `goal_status` that the evidence lands and goes stale when sources change.

## Host-provisioned EDA is not an extension point

A [Host-Provisioned Sandbox EDA Tool](../GLOSSARY.md#execution) runs through
its ordinary built-in Flow: the same MCP schema, the same FuseSoC/Edalize
build, the same subprocess inside the Sandbox, and the same result, artifacts,
and Criteria. Provisioning changes only where the installation files come
from.

The host owns that authority. An EDA Installation Registration, an optional
License Profile, and a Project Grant (see
[Configuration](../GLOSSARY.md#configuration)) are sealed into the Sandbox at
Sandbox Issuance, and license traffic goes only through a FlexNet License
Relay. Project configuration may only request host provisioning for a
supported EDA kind. Neither it nor a custom endpoint can name a host path,
mount, image, wrapper, environment variable, license destination, or command,
because both are files the Developer Agent can edit inside the Sandbox. A new
commercial EDA kind therefore needs a built-in policy with Doctor probes, mount
and lifecycle tests, and full-Flow evidence before it joins
[SUPPORTED-EDA-TOOLS.md](../user/SUPPORTED-EDA-TOOLS.md). The user-facing
setup is in [SANDBOX.md](../user/SANDBOX.md#commercial-eda-provisioning).

## Built-in Flow calls

The built-in `sim`, `lint`, `synth`, and `fpga` endpoints expose the same
controls as their CLI ([FLOW_REFERENCE.md](../user/FLOW_REFERENCE.md)), with
these MCP-specific shapes.

**Target selection.** MCP takes one comma-separated `target` string rather
than an array; caller order is kept, as on the CLI.

**Verdict.** The result carries the Flow's exit grade (`0`/`1`/`2`) in an
`EXIT_CODE:` line and in structured output. MCP `isError` is true for any
nonzero exit, so it cannot tell a design failure from a run that could not
reach a verdict; read the grade.

**Report.** The per-invocation report (fields in
[FLOW_REPORTS.md](FLOW_REPORTS.md#invocation-report)) is attached as
`structuredContent.reports[0]`, and `structuredContent.passed` repeats the
overall verdict. If the report is too large for the MCP result, `reports` is
empty, `truncated` is `true`, and the result keeps the Flow, Target, exit
code, and artifact pointers needed to open the durable report.

**Progress fallback.** For `progress.json` fallback evidence, `partial` is true
whenever the phase is not `complete` or Targets are still pending, so
`aborted` and `superseded` are terminal but partial, and neither appears as a
running checkpoint. When a Job ends without completing, is cancelled by the
user, or is found orphaned at server start, the supervisor marks an
unfinished checkpoint `aborted`, but only after it has reaped the child
process. The repair is best effort: a missing or unwritable checkpoint never
changes the Job's exit or cancellation result.

### Simulation input

The `sim` input takes Test Run selectors as a non-empty array of unique
strings:

```json
{"target":"sim_soc","test":["reset","interrupts"]}
```

Each entry is a Test name or a Test Run selector such as `reset+fast` or
`reset@7` (see [TESTS.md](../user/TESTS.md)). The CLI's `--tests-file` has no
MCP property; send the selectors directly. Coverage selections use sorted
test-name order for Campaign identity and run numbering, whatever the array
order. Plain selections keep array order, and an omitted `test` runs the
configured suite minus skipped Tests. HDL tests run in that order; cocotb
decides the function order within its filtered batch. `resume_from` names one
manifest and conflicts with `target`, `test`, `seed`, explicit `mode`,
`coverage`, `trace`, and `no_waivers`.

Structured campaign output reports `grade`, `complete`, aggregate
`observation_counts`, and a maximum-32 `observations` preview. Every preview
entry keeps nullable `test`, `execution`, nullable `failure_class` (`design` or
`infrastructure`), `functional`, `assertions`, `assertion_count`, nullable
`cycle_count`, bounded `detail`, and `log` and `trace` (the Test Run's
simulator log and waveform as POSIX paths relative to the directory of the
campaign's `artifacts.simulation` reference, `null` when the file does not
exist). `observation_total` and `observations_truncated` say whether the
preview is complete. The full `report.json.cycle_counts` mapping (see
[FLOW_REPORTS.md](FLOW_REPORTS.md)) is dropped from inline output before the
64 KiB structured-output check; read the numbered report for all counts.

`observation_counts` aggregates three independent axes:

- `execution`: whether the simulator completed, timed out, was guard-aborted,
  or failed before producing trustworthy test evidence;
- `functional`: the pass/fail/inconclusive test verdict;
- `assertions`: assertion evidence observed for that test.

`failure_class` is derived from those axes. It says whether a non-passing
outcome is blamed on the `infrastructure` or the `design`, and is `null` only
when the test passed: every other outcome that is not an infrastructure
failure, including an inconclusive one, is `design`.

The preview is deliberately not the full record. For that, resolve the
`manifest` artifact reference and read its authenticated terminal results.

### Simulation coverage input

`sim` takes an optional boolean `coverage` (default `false`); the CLI's
`--coverage` and `--cov` set the same field. No Criterion turns coverage on
by itself. Every selected Target passes coverage preflight before any report
is allocated, and one Target whose simulator cannot collect coverage (anything
but Verilator) rejects the whole request. Structured Target results keep
simulation, collection, and evaluation separate and point to the exact
canonical reports. The Coverage Analyst takes the returned Campaign path in a
separate call. The ordered persistence and Criterion-evidence steps are in
[Coverage Campaign orchestration](FLOW_IMPLEMENTATION_SIM.md#coverage-campaign-orchestration).
