# Public QA suite implementation handoff

Agreed design for
[Assemble the public Booley QA suite implementation handoff](https://github.com/boldaxolotl/booley/issues/376),
under [Map the public Booley QA scenario suite](https://github.com/boldaxolotl/booley/issues/246).
This is the design package for later implementation. No scenario, evaluator,
runner or GUI mechanism has been implemented or qualified by this handoff.

## Decisions incorporated

- Keep the three accepted IP journeys and exact pins, hardware semantics, Ticket
  contracts, faults, thresholds, source boundaries, evidence and cleanup. Apply
  the shared simplification in [Protocol](PROTOCOL.md), [Qualification](QUALIFICATION.md),
  [Format](FORMAT.md) and [Authoring](AUTHORING.md).
- Add the explicitly approved disposable submodule companion Project to Taxi;
  do not wrap or alter the authentic pinned Taxi checkout to claim coverage.
- Retain the eight-hour hard deadline. Revised allocations include every assigned
  probe and the companion; they are unmeasured caps, not proof that all work fits.
  PicoRV32's previous sum was 8h40, correcting the earlier handoff's 10h20
  arithmetic error; Taxi's was 8h30.
- Add physical synthesis to Taxi alongside its logical path, as requested after
  the audit showed logical synthesis supplies an ABC estimate rather than the
  accepted critical-path metric. Preserve the 0% thresholds on a directed
  physical comparison with a fixed, identical timing recipe.
- Preserve required GUI/client profiles and their same-run supporting work.
  Missing qualified infrastructure remains unavailable; core qualification can
  be reported independently and cannot claim VS Code or full-suite coverage.
- Preserve current public product expectations when an older inventory entry
  has been superseded, with an explicit migration explanation. Do not preserve
  an obsolete Scope rejection merely because an older inventory recorded it.

## Design package

| Document | Review responsibility |
| --- | --- |
| [Encoding and review contract](handoff/encoding.md) | Exact production file change set, one modest schema/validator interface, asset disclosure, results and validation commands. |
| [PicoRV32 checks](handoff/picorv32.md) | Published edit-free baseline, Wishbone fault/recovery, both dependency-linked Tickets, Zbb, Criteria, Target Plans and evidence. |
| [Taxi checks](handoff/taxi.md) | Authentic Setup/continuity, Cocotb/FST, observability/mutation, exact PFC fault and repair, logical and added physical synthesis. |
| [UART checks](handoff/uart.md) | Documentation-only greenfield path, independent evaluation, bounded repair, external-image lifecycle and cleanup. |
| [UART public oracles](handoff/uart-oracles.md) | Exact pinned register/field/case expansion, baud stimuli, documented boundaries and explicitly approved timing addendum. |
| [Inventory migration](handoff/inventory.md) | All 62 capability entries, independent supplemental outcomes, source revisions and supported EDA integrations. |
| [Profile selections](handoff/profiles.md) | Required/optional runs, exact named check lists, explicit platform exclusions, GUI prerequisites and complete Claude Ticket contracts. |
| [Submodule companion](handoff/submodules.md) | Deterministic separate Project, historical/recursive/paired reconstruction, selection, negatives, restoration and cleanup. |
| [Budgets and continuation](handoff/budgets.md) | Complete 480-minute allocations, reserve boundaries, finite retries and honest incomplete results. |
| [OpenTitan report draft](handoff/opentitan-doc-report.md) | Three requested pinned documentation inconsistencies and corrections, prepared but not submitted. |

The check catalogues are the migration detail. They keep independent outcomes
named even where they share a stimulus or artifact. Supplemental checks already
proven by an equivalent journey check resolve through an explicit mapping; they
are not a demand to repeat identical work. The central profile document owns
selections; selection columns in review catalogues are a migration view, not a
second production source of profile membership.

The exact older journey resolutions are retained as source annexes for payload
extraction. Current amendments take precedence over their superseded event replay,
general resume, promotion, status, profile or budget language. An old annex is not
an alternative protocol and must not reintroduce obsolete machinery during encoding.

## Final design resolution

The maintainer agreed the UART timing addendum and full PicoRV32 Claude Ticket
contracts. The same complete Tickets and Criteria run under Claude, with duplicate
host/stress probes left in their assigned Codex runs. The UART bounds are public
scenario requirements supplied to the Developer, not inferred OpenTitan timing facts.

Taxi's companion simulation must actually depend on both the top-level and nested
submodule RTL. It proves a fresh pass, dependency-specific simulation failure with
each required submodule absent (including warm-cache and cold-build checks), exact
restoration and a fresh pass. A root-only synthesis success or Git listing cannot
satisfy that requirement. All 26 companion checks remain within its 40-minute cap.

Enabled AXI Zbb coverage uses the explicit seventh ephemeral simulation Target.
Taxi physical synthesis uses the frozen five-clock SDC and identical baseline and
candidate methodology. The design frontier is settled; encoding, evaluator and
fixture construction, execution infrastructure and measured qualification remain
later work. Repository publication of this local handoff requires the separate
branch-push authority prescribed by the repository rules.

## Implementation and qualification acceptance checklist

- [ ] Encode production assets from the file manifest; extract complete prompts
  and Ticket payloads with only explicitly allowed run substitutions.
- [ ] Freeze IP/corpus identities, Target parameters and immutable thresholds.
  Record exact published Booley release/image identities per run.
- [ ] Preserve every independently observable inventory/journey requirement;
  verify each combined mapping's stimulus, outcome and evidence independently.
- [ ] Expand UART register/field/corner and seeded supplements to concrete case
  IDs, observation windows, finite timeouts and evidence paths.
- [ ] Implement and qualify independent evaluator controls, source isolation and
  complete same-seed rerun before claiming UART conformance.
- [x] Validate unique references, earlier-check prerequisites, source/asset paths,
  selected supporting work, fixed profile exclusions and 480-minute sums.
- [ ] Review expected negative pass, unexpected failure then recovery, missing
  result, unavailable GUI, flake, unresolved scoped Finding and cleanup failure.
- [ ] Require fresh artifact-backed success for every supported EDA integration,
  including both Taxi synthesis methodologies and Linux Vivado.
- [ ] Capture actual provider/backend/client identities. Headless MCP evidence
  never earns VS Code-client or rendered-waveform credit.
- [ ] Execute every required profile against exact release and suite identities.
  A valid mapping or parser result never substitutes for execution.
- [ ] Preserve failed attempts, missing work, deviations and cleanup evidence for
  direct Consolidate Findings ingestion; never send the run through Feedback.
- [x] Keep absent GUI/client infrastructure and measured runtime limitations
  visible. Do not declare full qualification while required work is unavailable.

Unchecked items still include implementation or execution work; see
[implementation status](IMPLEMENTATION.md) for completed portions and remaining gaps.
Checked validation and visibility items do not establish qualification. No branch push, pull request, scenario execution or external
OpenTitan report submission is implied by preparing this planning package.
