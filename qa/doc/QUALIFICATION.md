# Qualification

## Purpose and audience

This guide tells Human Maintainers and reviewers how the Public QA Suite qualifies one
Booley product revision from Scenario Run evidence. Scenario Operators follow the
[Scenario protocol](PROTOCOL.md).

Qualification aggregates required Scenario Run Outcomes under the
[outcome rules](#outcomes).

See the [suite README](../README.md#scenario-and-check-structure) for Scenario and
Check structure, and the [Public QA glossary](../CONTEXT.md) for canonical terms.

## Configured Scenarios

The [PicoRV32](../scenarios/picorv32/scenario.yaml),
[Taxi](../scenarios/taxi/scenario.yaml), and
[UART](../scenarios/uart/scenario.yaml) production files declare the exact Configured
Scenarios, required status, parameters, pre-run requirements, check sets, and
exclusions.

- All three Scenarios run under Codex on Ubuntu 24.04 x86-64 and native Windows
  x86-64 with Docker Desktop/WSL2.
- PicoRV32 also runs under Claude on Ubuntu; its Windows/Claude GUI run is optional
  until usage permits.
- Native CLI runs exercise core behavior. GUI runs also exercise the supported VS
  Code Runtime Attachment and Waveform Viewer integration.
- Keep each Scenario's EDA tool, Runtime Image, Stealth Mode, and Linux
  provisioned-Vivado assignments. Windows has no provisioned-Vivado requirement;
  Linux unavailability does not remove the Linux requirement.

Classify Interactive Mode Checks by evidence. Semantic MCP tool and Booley Flow
behavior may be exercised independently, but VS Code Runtime Attachment and Waveform
Viewer claims require attachment or visual evidence. A headless child earns no VS
Code credit. Without a qualified driver or observer, those Checks are unavailable and
the run is incomplete.

Fix each versioned Configured Scenario before execution. Pre-run observations affect
availability, not scope; unavailable required work makes the run incomplete. Only a
reviewed Configured Scenario may exclude an inapplicable product and native-host
combination. Report every exclusion and its rationale.

## Outcomes

Evaluate every selected Check against the declared Booley product and suite revisions.
Do not narrow run scope after seeing Check Results.

1. Any trustworthy selected-Check failure or unresolved trustworthy Booley or docs
   defect in scope makes the outcome `failed`, including known defects, flaky
   failures, and new defects outside a prewritten Check.
2. Otherwise, a missing, blocked, or unavailable Check, invalid evidence, or failed
   required resource cleanup makes it `incomplete`. Retained review state is allowed
   when it satisfies the protocol's retention predicate.
3. Otherwise, the outcome is `passed`.

Failure takes precedence over incomplete conditions; list them all. Friction and
impressions do not fail Qualification. Out-of-scope Findings stay visible without
invalidating unrelated claims. A potentially invalidating Finding leaves the run
incomplete until triage resolves its scope or classification. Uncertainty cannot
produce a pass.

Report each Scenario Run Outcome, execution status, and Findings. Show optional
Configured Scenarios separately.

Example, with no claim that these runs have occurred:

```text
picorv32-ubuntu-codex-cli: passed
taxi-ubuntu-codex-cli: passed
uart-ubuntu-codex-cli: passed
picorv32-ubuntu-codex-vscode: incomplete: observer unavailable
picorv32-windows-claude-vscode: pending (optional)
Full qualification: incomplete
Resource cleanup: complete; review state retained
```

In a real report, name missing Checks and link evidence. A CLI run may pass while a
GUI run is incomplete. Never report an unqualified "suite passed."

## Revision and currency

Bind each Check Result to exact Scenario Run inputs. Every product revision, published
or unreleased, needs fresh runs for all required Configured Scenarios; evidence cannot
cross revisions. Rerun affected required Configured Scenarios after a behavioral
Scenario change, and affected Configured Scenarios after a shared behavioral protocol
change. Editorial changes need no rerun, but record their classification. Reports
identify tested commits and reviewed editorial-only equivalence.

For the same product revision, a report may reuse complete runs from unchanged
Scenarios only after review confirms that Scenario inputs, selected Checks, run
parameters, shared protocol, referenced prompts and evaluator assets, and relevant
environment identities are unchanged or editorially equivalent. Record the source
run and tested suite commit; otherwise rerun. Reuse combines complete runs, not
skipped Checks in a new run. No per-cell invalidation database is required.

The current implementation has no full Qualification evidence.
