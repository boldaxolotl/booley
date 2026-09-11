# Authoring QA scenarios

This guide owns the Scenario definition contract. Read [Protocol](PROTOCOL.md) for
execution, [Qualification](QUALIFICATION.md) for scope and verdicts, and the
[run record format](FORMAT.md) for Scenario Run files. The
[PicoRV32 production Scenario](../scenarios/picorv32/scenario.yaml) shows the
contract in concrete use.

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
counted once in addition. A Step records its ID, action, Checks, prerequisites,
authority, timeout or retry restrictions, recovery instructions, owned resources, and
phase recovery point.
Shared behavior comes from the protocol; Scenario instructions may tighten it. Keep
lengthy prompts, Ticket payloads, and evaluator material in referenced assets.

Each Check is self-contained and records its ID, Capability references, stimulus,
expectation, public contract source, evidence requirement, and capture point. Keep
expectation authority distinct from navigational documentation. A Scenario Run records
which documentation it consulted.

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

## Authoring workflow

1. Identify the public behavior and its authority. Assign a semantic uppercase-kebab
   Capability ID that names the behavior, not its category or sequence, in the
   [capability inventory](../coverage.yaml), then extend the covering Scenario. Keep
   independently observable requirements in separate Checks. A reviewed,
   specification-backed Check is mandatory without first passing a reference run;
   treat an unauthoritative expectation as research until resolved. Maintainer review
   governs specification changes; no candidate/established assertion registry is
   needed.
2. Apply the contract above to each Check and its containing Step. Preserve complete
   seeded-fault and restoration sequences. Link known defects to issues, retain their
   failures, and preserve original evidence in Finding updates.
3. Confirm every supported Capability has a Check and Configured Scenario assignment;
   name unresolved gaps instead of dropping Capabilities to obtain a pass. A mapping
   or availability assessment does not prove coverage. Apply the
   [client-evidence rules](QUALIFICATION.md#configured-scenarios) to Runtime Attachment
   and Waveform Viewer claims.
4. Review the oracle, sub-agent freedom, pre-run authority, literal payloads,
   expectation authority, prerequisites, supporting work, native-host exclusions,
   Configured Scenario selections, continuation, product cleanup, final quiescence,
   and the complete 480-minute allocation. Review content before updating asset
   hashes, then run:

   ```sh
   python qa/validate.py
   python -m pytest tests/qa/
   ```

   The standalone validator enforces the contract above without importing the product
   under test and validates HTTPS authority syntax; maintainer review and build-matched
   execution establish meaning and currency. The tests exercise rejection and fixture
   controls. Use `--coverage-index <path>` to inspect the derived
   Capability/Check/Configured Scenario mapping.
5. Classify the change under [Revision and currency](QUALIFICATION.md#revision-and-currency).
   Rehearse behavioral changes where feasible to expose underspecification; a product
   defect is a Finding, not a reason to weaken a valid Check. Keep landed-fix
   verification pending until the relevant Checks pass. Do not add a Known Condition
   expiry workflow. Classify flaky failures by cause and justify targeted validation
   in review; no universal
   three-clean-runs-per-cell rule applies.

## Worked example: verify rendered waveform state

The Taxi Scenario creates scoped waveform state and opens it through a VS Code Runtime
Attachment. Author its separate `viewer.visual-capture` Check as follows:

1. Reference [WAVEFORM-VIEWER](../coverage.yaml), whose contract requires Waveform
   Control Protocol (WCP) readback and a rendered screenshot. Keep the Check separate
   from `viewer.actual-client-open`: opening the viewer does not prove correct state.
2. In [the Taxi Scenario](../scenarios/taxi/scenario.yaml), require a pre-run qualified
   observer to capture the expected scoped signals, markers, and cursor. Use the frozen
   [Taxi authority](https://github.com/boldaxolotl/booley/blob/b163fd1f45b76f3950005678e500e695232832fb/qa/handoff/taxi.md),
   timestamped viewer/run/observer evidence, and capture path
   `evidence/taxi-10g-mac-port-evolution.viewer.visual-capture`. The containing Step
   requires pinned inputs and clean baseline outputs, inherits the Scenario's authority
   and budget, and needs no Check-specific timeout or recovery.
3. Put the Check in `taxi-gui`, selected only by applicable GUI Configured Scenarios
   such as `taxi-ubuntu-codex-vscode`. Their pre-run requirements include an actual VS
   Code Runtime Attachment, MCP and WCP access, and a qualified screenshot observer;
   CLI runs cannot earn this visual credit.
4. Run the validation commands above and inspect `--coverage-index <path>`. Review the
   screenshot oracle, observer qualification, prerequisites, selection, and budget.
   An unavailable observer makes the GUI run incomplete; it does not justify removing
   the Check.
5. Classify the addition as behavioral because it adds a required observation and
   evidence contract. Rerun the whole Taxi Scenario under both required GUI Configured
   Scenarios, record tested revisions, and preserve any rehearsal defect instead of
   weakening the Check.
