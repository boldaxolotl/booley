# Coverage and qualification

## Direct coverage mapping

Keep the supported capability inventory and its public contract sources. Scenario
checks reference capabilities directly; generate the reverse index from those
references. Each check owns its stimulus, expectation, and evidence once.

Independently observable requirements remain separate checks, including meaningful
success, rejection, state transition, fault, restoration, persistence, and cleanup
claims. Baseline/fault/recovery ordering is expressed inside the scenario. Separately
authored Coverage Obligations, Cells, Allocations, and Verification Chains are removed.

`coverage.yaml` owns the capability inventory and public sources. Scenario checks
alone own capability references. `profiles.yaml` alone owns required scenario runs,
platform/provider identities, selected scenario-qualified check IDs, and capability
prerequisites. Checks do not repeat profile membership, and the inventory does not
repeat check assignments. Generate reverse indexes from these references. Resolve
profiles before execution. No generic dimension-expansion language is required.

A profile's required run explicitly includes the prerequisite checks and supporting
steps needed to produce its evidence. Perform that work in the same run; never borrow
unlisted setup or earlier-run artifacts to skip it. Authors list those prerequisites
alongside the claims, and validation checks the list is complete. A run selecting
multiple profiles executes their combined ordered work once, retaining separate
profile verdicts. No scheduler or automatic prerequisite expansion is required.

Inventory-to-check completeness remains mandatory for the designed suite. A mapping
does not prove behavior: actual evidence is needed for qualification. Report gaps
by name; do not silently drop a supported capability to obtain a pass.

## Profiles

Separate core semantic/product qualification from GUI/client integration. Preserve
the existing qualification cadence and responsibilities:

- All three journeys with Codex on Ubuntu 24.04 x86-64 and native Windows x86-64
  with Docker Desktop/WSL2.
- Representative PicoRV32 Claude qualification on Ubuntu; Claude Windows when
  usage permits. The latter remains explicitly pending until run.
- Both Interactive Mode and Ticket Mode behavior from the accepted journeys.
- Existing EDA, image, Stealth, and Linux provisioned-Vivado assignments. Windows
  has no provisioned-Vivado requirement; Linux unavailability does not remove it.

Classify each Interactive Mode check by its evidence. Semantic MCP/Flow behavior
may be exercised independently. Claims about the actual supported VS Code client,
its attachment/integration, and GUI rendering remain in GUI/client integration and
require that client or visual evidence. A headless child never earns VS Code credit.
When no qualified driver/observer exists, retain these checks as unavailable in
that profile. The specification still includes the full journey; a core result
does not claim complete Interactive Mode client qualification.

Profiles are versioned scope decisions fixed before running. Pre-run capability
probes determine availability, not scope. Required unavailable work makes its
profile incomplete. Only the reviewed profile may exclude genuinely inapplicable
product/platform combinations. Show exclusions in the report with their rationale.

## Verdicts

Evaluate a profile against all its required runs and checks for the declared release
and suite revision. Existing profiles cannot be narrowed after seeing results.

1. Any trustworthy required-check failure or unresolved trustworthy Booley/docs
   defect within profile scope makes the profile `failed`. This includes new defects
   discovered outside a prewritten check, known defects, and flaky failures.
2. Otherwise any missing, blocked, or unavailable required check, missing required
   run, invalid evidence, or incomplete mandatory cleanup makes it `incomplete`.
3. Otherwise it is `passed`.

Keep failure precedence when a failed profile also has missing work or cleanup
problems. List every condition. Friction and impressions do not fail qualification.
Findings outside the selected profile are visible without invalidating unrelated
claims. A finding with unresolved scope/classification that could invalidate the
profile leaves it incomplete until triaged; uncertainty must not manufacture pass.

Do not define separate aggregate QA verdict and “green” mechanisms. A report has
profile verdicts plus operational completion and findings. Full qualification passes
only when every required profile passes; optional future runs are shown separately.

Example, with no claim that these runs have occurred:

```text
Core — Ubuntu/Codex: passed (all three required journeys)
Core — Windows/Codex: passed (all three required journeys)
Compatibility — Ubuntu/Claude: passed (PicoRV32)
GUI/client integration: incomplete — observer/driver unavailable
Full qualification: incomplete
Optional Windows/Claude: pending
Cleanup: complete
```

Name missing checks and link evidence in a real report. “Core passed” is valid even
while GUI is unavailable; an unqualified “suite passed” is not.

## Revision and currency

Bind every result to exact run inputs. For a new released Booley version, run the
required profiles afresh; evidence does not carry forward automatically. For a
behavioral scenario revision, rerun affected whole scenarios in their required
profiles. A shared behavioral protocol change reruns every affected scenario.
Editorial changes need no rerun; record that classification. Reports identify the
actual tested commits and any reviewed editorial-only equivalence.

When only some scenarios change, a qualification report may use prior completed
runs of unchanged whole scenarios from the same Booley release. Review must confirm
that their scenario inputs, selected checks/profile requirements, shared protocol,
referenced prompts/evaluator assets, and relevant environment identities are unchanged
or editorially equivalent. Record the source run and actual tested suite commit;
otherwise rerun the scenario. This assembles a report from complete runs, not skipped
checks in a new run. A new Booley release always requires fresh runs. No per-cell
invalidation database is required.

The exact named definitions now live in [profiles.yaml](profiles.yaml):
`core-ubuntu-codex`, `core-windows-codex`, `gui-ubuntu-codex`, `gui-windows-codex`,
`core-ubuntu-claude`, `gui-ubuntu-claude`, and optional `gui-windows-claude`.
Standalone GUI runs include all same-run core support. Both Claude runs retain
complete original PicoRV32 Ticket contracts; duplicate Codex stress probes are not
added. The current implementation has no full qualification results.
