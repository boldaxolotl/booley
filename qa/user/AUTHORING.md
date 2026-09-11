# Authoring QA scenarios

This guide owns the Scenario definition contract. Read
[Protocol](../agents/PROTOCOL.md) for execution,
[Qualification](QUALIFICATION.md) for scope and verdicts, and the
[run record format](../agents/FORMAT.md) for the files produced during a Scenario
Run. The [PicoRV32 production Scenario](../scenarios/picorv32/scenario.yaml) shows
the authoring contract in concrete use.

## Scenario definition contract

[scenario.schema.json](../scenario.schema.json) is the structural authority. Use
`format_version: 1`. Keep Scenario IDs and local Step and Check IDs stable; qualify
external Check references with the Scenario ID. A revision to the same Check retains
its ID, while a different Check receives a new ID. Never repurpose IDs. The repository
commit freezes the Scenario, protocol, and Capability Coverage definitions used by a
Scenario Run.

Each Scenario supplies inputs, shared budgets, named check sets, Configured Scenarios,
and ordered phases and Steps. Each phase records its `id`, `title`, and `minutes`; the
Scenario budget records its deadline, contingency, cleanup minutes, and cleanup start.
The cleanup reserve covers final records and mandatory quiescence; intentional review
retention does not extend the run. Phase minutes include cleanup, while contingency is
counted once in addition. A Step
records its ID, action, Checks, and any prerequisites, authority, timeout or retry
restrictions, recovery instructions, owned resources, and phase recovery point.
Shared behavior comes from the protocol; Scenario instructions may tighten it. Keep
lengthy prompts, Ticket payloads, and evaluator material in referenced assets.

Each Check is self-contained and records its ID, capability references, stimulus, expectation, public
contract source, evidence requirement, and capture point. Keep expectation authority
distinct from documentation used only for navigation. A Scenario Run records which
documentation it actually consulted.

[`coverage.yaml`](../coverage.yaml) owns the supported Capability inventory and its
public contract sources. Scenario files own Checks, named check sets, and Configured
Scenario assignments. Checks reference Capabilities directly; derive the reverse
index from those references. Do not repeat Check assignments in the inventory or use
generic dimension expansion. Do not author separate Coverage Obligations, Cells,
Allocations, or Verification Chains.

Inputs are named records with `id`, `kind`, `value`, `source`, and `verification`.
`git` and `sha256` inputs use full literal lowercase hashes. A `pre-run` input fixes an
identity before execution; it does not permit changing a pinned IP, workload, or
threshold.

Assets record a contained `path`, an explicit `audience`, and, for production assets,
a SHA-256 digest. `base` defaults to `scenario`; `base: shared` resolves within
`qa/shared/`. Paths and symlinks must remain inside the selected base. Every Step
lists the shared assets it uses. Templates list their allowed `substitutions`:
`run_root`, `artifact_root`, `ticket_id`, `commit_id`, `product_revision`, and
`provider`; any unlisted template variable fails validation. Substitutions cannot
change thresholds or disclose private assets.

A restoration Step's `recovery` record identifies its `baseline`, prior `detection`
Check IDs, and `instruction`. It requires the baseline and remains independent of the
detection's successful outcome: it cannot depend directly or transitively on that
outcome. Product cleanup Checks remain independently reachable. Finalization must
leave enough reserve to quiesce active resources and record retained review state.

Check sets are flat and disjoint. Each Check belongs to exactly one named set, and
each set is selected by at least one Configured Scenario. A Configured Scenario may
select several sets; the validator resolves them into one ordered Check list and
derives the prerequisite Checks and Steps. Perform that supporting work in the same
Scenario Run; unlisted setup and earlier-run artifacts cannot replace it. No scheduler
or automatic prerequisite expansion is required. Each Configured Scenario declares
whether it is required, binds `host_os`,
`cpu_architecture`, `native_host`, `agent_provider`, `interactive_mode_client`, and
`ticket_mode_backend`, and lists its pre-run requirements, check sets, and justified
exclusions. `run.json` records the actual execution values and observed identities.
The validator rejects missing or unused sets, duplicate Checks, unknown exclusions,
and unknown coverage, Scenario, or Configured Scenario fields.

The standalone validator checks fields, unique IDs, references, earlier
prerequisites, asset containment and hashes, Configured Scenario selections,
supporting work, budgets, recovery, and Capability Coverage without importing the
Booley product under test. It checks HTTPS authority syntax; Human Maintainer review
and build-matched execution establish the authority's meaning and currency.

## Authoring workflow

1. Identify the public behavior and its contract source. Assign a semantic,
   uppercase-kebab Capability ID that names the behavior rather than its category or
   sequence number. Consult the [capability inventory](../coverage.yaml), then extend
   the Scenario that covers it.
   Keep independently observable requirements in separate checks. A
   specification-backed check becomes mandatory once review introduces it; it need
   not pass a reference run first. Treat an expectation without an authoritative
   basis as a research question or observation until resolved. Ordinary maintainer
   review governs specification changes; no candidate/established assertion registry
   is needed.
2. Make each Check self-contained: record its stimulus, expectation, evidence, and capture point. At Scenario
   level, supply prerequisites, authority, timeout, recovery, and cleanup. Preserve
   the full fault/restoration sequence for seeded faults. Link known defects to issues
   and retain their failures. Preserve original evidence in finding updates.
3. Reference Capabilities and assign each Check to explicit Configured Scenarios.
   Every supported Capability needs a Check; name every gap and never drop a
   Capability to obtain a pass. Runtime Attachment and Waveform Viewer claims require
   corresponding evidence. A mapping or availability assessment does not prove
   product coverage. Before publishing, confirm every retained required behavior has
   a Check, Configured Scenario assignment, and evidence contract. Keep unresolved
   gaps visible.
4. Review oracle quality, permitted sub-agent freedom, pre-run authority, feasible
   budgets, continuation, product cleanup, and final quiescence. Review literal payloads, public expectation
   authority, prerequisite and supporting work, native-host exclusions, and the
   complete 480-minute allocation. Keep Configured Scenario selections explicit, and review content changes
   before updating asset hashes.

   After changing assets, run:

   ```sh
   python qa/validate.py
   python -m pytest tests/qa/
   ```

   The validator checks fields, IDs, references, prerequisite ordering, Configured Scenario
   selections, and inventory coverage. The tests exercise rejection and fixture
   controls. Use `--coverage-index <path>` to review the derived
   capability/Check/Configured Scenario mapping.

5. Classify the change. Behavioral edits require affected-scenario reruns
   using required Configured Scenarios; editorial edits do not. Keep actual tested revisions in
   qualification reports. Rehearse where feasible to discover underspecification;
   a product defect is a finding, not a reason to weaken a valid check. Keep landed
   fixes verification pending until the relevant checks pass. Do not add a separate
   Known Condition expiry workflow. Classify flaky failures by cause and justify
   targeted validation in review; no universal three-clean-runs-per-cell rule applies.

## Worked example: verify rendered waveform state

The Taxi Scenario creates scoped waveform state and opens it in the Waveform Viewer
through a VS Code Runtime Attachment. The `viewer.visual-capture` check separately
proves that the Waveform Viewer shows the expected signals, markers, and cursor. Its
authoring follows the five steps above:

1. Start with capability [WAVEFORM-VIEWER](../coverage.yaml), whose public contract requires
   Waveform Control Protocol (WCP) readback and a rendered screenshot. Keep this check
   separate from `viewer.actual-client-open`: the Waveform Viewer can open without
   showing the correct state.
2. Add the check to the existing Viewer sequence in
   [the Taxi scenario](../scenarios/taxi/scenario.yaml). Record the stimulus, expected
   visible state, evidence, and capture point:

   ```yaml
   - id: viewer.visual-capture
     capabilities:
     - WAVEFORM-VIEWER
     stimulus: Capture Viewer only with pre-run qualified observer; verify visible scoped signals/markers/cursor.
     expected: Capture Viewer only with pre-run qualified observer; verify visible scoped signals/markers/cursor.
     authority_ref: https://github.com/boldaxolotl/booley/blob/b163fd1f45b76f3950005678e500e695232832fb/qa/handoff/taxi.md
     evidence: Timestamped screenshot with Waveform Viewer and run identity plus observer capability evidence; unavailable observer means the GUI Scenario Run is incomplete.
     capture: evidence/taxi-10g-mac-port-evolution.viewer.visual-capture
   ```

   The containing step requires the pinned inputs and clean baseline outputs. It uses
   the scenario's authority and budget; it needs no check-specific timeout or recovery.
3. Add the local check ID to the applicable named check set in
   [the Taxi Scenario](../scenarios/taxi/scenario.yaml), and select that set from
   each applicable Configured Scenario. Relevant excerpt (unchanged fields omitted):

   ```yaml
   check_sets:
   - id: taxi-gui
     checks:
     - viewer.visual-capture
   configured_scenarios:
   - id: taxi-ubuntu-codex-vscode
     required: true
     parameters:
       host_os: Ubuntu 24.04
       cpu_architecture: x86-64
       native_host: true
       agent_provider: Codex
       interactive_mode_client: VS Code Codex extension
       ticket_mode_backend: Codex
     pre_run_requirements:
     - Actual VS Code runtime attachment, MCP and WCP with qualified rendered screenshot observer
     check_sets:
     - taxi-core
     - taxi-gui
     exclusions: []
   ```

   Only GUI Configured Scenarios select `taxi-gui`. Their pre-run requirements
   require a VS Code Runtime Attachment, WCP access to the Waveform Viewer, and a
   qualified screenshot observer. CLI Scenario Runs cannot earn credit for this visual
   claim.
4. Run the validation commands from step 4 and inspect `--coverage-index <path>`.
   Review the screenshot oracle, pre-run observer qualification, prerequisite order,
   Configured Scenario selection, and budget. An unavailable observer leaves the GUI Scenario Run
   incomplete; it does not justify removing the check.
5. Classify the addition as behavioral because it adds a required observation and
   evidence contract. Rerun the whole Taxi Scenario using both required GUI Configured Scenarios and
   record the tested revisions. A rehearsal may find a product defect; retain the
   failure instead of weakening the check.
