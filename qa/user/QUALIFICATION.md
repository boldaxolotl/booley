# Coverage and qualification

## Direct coverage mapping

Keep the supported Capability inventory and its public contract sources. Scenario
Checks reference Capabilities directly; generate the reverse index from those
references. Each Check owns its stimulus, expectation, and evidence once.

Independently observable requirements remain separate Checks, including meaningful
success, rejection, state transition, fault, restoration, persistence, and cleanup
claims. Baseline/fault/recovery ordering is expressed inside the Scenario. Separately
authored Coverage Obligations, Cells, Allocations, and Verification Chains are removed.

[`coverage.yaml`](../coverage.yaml) owns the Capability inventory and public sources.
Each production `scenario.yaml` owns its Checks, named check sets, and required or
optional Configured Scenarios. A Configured Scenario binds the host operating
system, CPU architecture, native-host scope, agent provider, Interactive Mode client,
Ticket Mode backend, pre-run requirements, check sets, and justified exclusions. The
inventory does not repeat Check assignments. Generate reverse indexes from these
references; no generic dimension-expansion language is required.

A Configured Scenario's selected Checks resolve to the prerequisite Checks and
Steps needed to produce their evidence. Perform that work in the same run; never borrow unlisted
setup or earlier-run artifacts to skip it. Validation requires every Scenario Check
to belong to exactly one named set and every set to be selected by at least one
Configured Scenario. No scheduler or automatic prerequisite expansion is required.

Inventory-to-Check completeness remains mandatory for the designed suite. A mapping
does not prove behavior: actual evidence is needed for Qualification. Report gaps by
name; do not silently drop a supported Capability to obtain a pass.

## Configured Scenarios

The production Scenarios preserve the reviewed qualification cadence:

- All three Scenarios run with Codex on Ubuntu 24.04 x86-64 and native Windows
  x86-64 with Docker Desktop/WSL2.
- PicoRV32 also runs with Claude on Ubuntu; its Windows/Claude GUI run remains
  optional until usage permits.
- Native CLI runs exercise core product behavior. GUI runs additionally exercise
  the supported VS Code Runtime Attachment and Waveform Viewer integration.
- Existing EDA tool, Runtime Image, Stealth Mode, and Linux provisioned-Vivado
  assignments remain unchanged. Windows has no provisioned-Vivado requirement;
  Linux unavailability does not remove it.

Classify each Interactive Mode Check by its evidence. Semantic MCP tool and Booley
Flow behavior may be exercised independently. Claims about the actual VS Code Runtime
Attachment and Waveform Viewer require attachment or visual evidence. A headless
child never earns VS Code credit. When no qualified driver or observer exists, retain
those Checks as unavailable and mark the affected Scenario Run incomplete.

Configured Scenarios are versioned scope decisions fixed before execution.
Pre-run observations determine availability, not scope. Required unavailable work
makes the Scenario Run Outcome incomplete. Only a reviewed Configured Scenario
may exclude genuinely inapplicable product and native-host combinations. Show
exclusions in the report with their rationale.

## Outcomes

Evaluate each Scenario Run against all of its selected Checks for the declared Booley
product revision and suite revision. Its scope cannot be narrowed after seeing Check Results.

1. Any trustworthy selected-Check failure or unresolved trustworthy Booley/docs
   defect within the Scenario Run's scope makes its Scenario Run Outcome `failed`.
   This includes new defects discovered outside a prewritten Check, known defects,
   and flaky failures.
2. Otherwise any missing, blocked, or unavailable selected Check, invalid evidence,
   or failed mandatory quiescence makes the outcome `incomplete`. Deliberately
   retained review state does not when it satisfies the protocol's retention predicate.
3. Otherwise the outcome is `passed`.

Keep failure precedence when a failed Scenario Run also has missing work or quiescence
problems. List every condition. Friction and impressions do not fail Qualification.
Findings outside the selected Scenario Run are visible without invalidating unrelated
claims. A Finding with unresolved scope or classification that could invalidate the
run leaves it incomplete until triaged; uncertainty must not manufacture pass.

A report lists each Scenario Run Outcome, execution status, and Findings.
Qualification passes only when a Scenario Run against every required Configured
Scenario has passed; optional Configured Scenarios are shown separately.

Example, with no claim that these runs have occurred:

```text
picorv32-ubuntu-codex-cli: passed
taxi-ubuntu-codex-cli: passed
uart-ubuntu-codex-cli: passed
picorv32-ubuntu-codex-vscode: incomplete — observer unavailable
picorv32-windows-claude-vscode: pending (optional)
Full qualification: incomplete
Quiescence: complete; review state retained
```

Name missing Checks and link evidence in a real report. A CLI Scenario Run Outcome
can be passed while a GUI Scenario Run Outcome is incomplete; an unqualified “suite
passed” is not valid.

## Revision and currency

Bind every Check Result to exact Scenario Run inputs. For a different Booley product revision,
whether published or unreleased, execute a Scenario Run against every required Configured
Scenario afresh; evidence does not carry forward automatically. A behavioral Scenario
revision reruns every affected required Configured Scenario. A shared behavioral protocol
change reruns every affected Configured Scenario. Editorial changes need no rerun; record
that classification. Reports identify the actual tested commits and any reviewed
editorial-only equivalence.

When only some Scenarios change, a Qualification report may use prior completed Scenario
Runs from unchanged Scenarios against the same Booley product revision. Review must
confirm that the Scenario inputs, selected Checks, run parameters, shared protocol,
referenced prompts/evaluator assets, and relevant environment identities are unchanged or
editorially equivalent. Record the source run and actual tested suite commit; otherwise
rerun it. This assembles a report from complete runs, not skipped Checks in a new run. A
different product revision always requires fresh runs. No per-cell
invalidation database is required.

The exact named Configured Scenarios live in the three production
`scenario.yaml` files. Each declares its required status, parameters, pre-run
requirements, check sets, and exclusions. The current implementation has no full
Qualification evidence.
