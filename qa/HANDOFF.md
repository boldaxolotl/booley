# Implementation handoff

[Issue #376](https://github.com/boldaxolotl/booley/issues/376) remains open until the
three accepted journeys can be encoded under this shared contract without lost
requirements or unresolved feasibility claims. The public suite is not executable yet.

## Deliverables

1. Specify the exact production file change set described by [Format](FORMAT.md),
   including the scenario schema and modest validation commands. Use the worked
   example to settle any remaining representation ambiguity.
2. Translate all three journey designs into concrete steps/checks and profile
   assignments. Preserve their workloads, pins, thresholds, prompts, Ticket payloads,
   faults, independent evaluator, authority, and cleanup. Adapt shared bookkeeping
   and recovery references to the new protocol.
3. Build a migration checklist from every old independently observable requirement
   to its new check. Mark retained, combined with named equivalent checks, or assigned
   to GUI/client integration. Combining representation must preserve each outcome.
   Account for all supported inventory entries and EDA integrations.
4. Enumerate required core and GUI/client profile runs, check selections, exclusions,
   and pre-run probes. Retain Ubuntu/Windows Codex and representative Ubuntu Claude
   responsibilities. Label optional Windows Claude separately. Confirm that every
   supported-client claim requires actual supported-client evidence.
5. Reconcile coverage gaps, deadlines, continuation, and cleanup. Expose unmet work
   instead of claiming the suite sufficient. Produce the implementation-ready review
   checklist and record outstanding execution infrastructure separately.

## Known unresolved questions

- Taxi contains no submodules. Its direct-clone journey cannot cover offline submodule
  reconstruction; assign that requirement honestly without changing the accepted
  journey implicitly or hiding it in greenfield work.
- PicoRV32's phase allocations total 10h20, Taxi's 8h30, against eight-hour limits.
  Reconcile the schedules with the preserved workloads. Do not declare feasibility
  solely from the stated overall limit or silently cut checks.
- GUI/client qualification lacks a qualified execution/observation mechanism on the
  reference hosts. Preserve unavailable checks; core qualification is independently
  reportable. CLI execution cannot fill the supported-client evidence gap.

## Review criteria

- An author finds a check's action, expectation, source, evidence, and recovery together.
- A normal pass, expected seeded failure, unexpected failure followed by recovery,
  missing result, unavailable GUI, flaky result, and incomplete cleanup each have
  one unambiguous scoped verdict with preserved evidence.
- Capability mappings are complete and generated in reverse from check references.
- Existing journey obligations are preserved; the simplified format does not
  introduce new hardware requirements.
- Records are readable without workflow event replay or a qualification database.
- Generic resume, assertion promotion, per-cell freshness, and Known Condition
  registries are not prerequisites for first execution.

Encoding production assets, implementing the UART evaluator or execution tooling,
running scenarios, and repairing Booley remain later implementation work. Completing
the design handoff must distinguish a fully specified unavailable profile from
executed qualification; it never grants coverage credit.
