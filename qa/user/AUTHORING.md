# Authoring QA scenarios

Read [Protocol](../agents/PROTOCOL.md) for execution, [Qualification](QUALIFICATION.md)
for scope and verdicts, and [Format](../agents/FORMAT.md) for fields. The
[worked example](../examples/README.md) shows how they fit together.

1. Identify the public behavior and its contract source. Consult the
   [capability inventory](../coverage.yaml), then extend the journey that covers it.
   Keep independently observable requirements in separate checks. A
   specification-backed check becomes mandatory once review introduces it; it need
   not pass a reference run first. Treat an expectation without an authoritative
   basis as a research question or observation until resolved. Ordinary maintainer
   review governs specification changes; no candidate/established assertion registry
   is needed.
2. Record each check's stimulus, expectation, evidence, and capture point. At scenario
   level, supply prerequisites, authority, timeout, recovery, and cleanup. Preserve
   the full fault/restoration sequence for seeded faults. Link known defects to issues
   and retain their failures. Preserve original evidence in finding updates.
3. Reference capabilities and assign explicit qualification profiles. Runtime
   Attachment and Waveform Viewer claims require corresponding evidence. Record
   missing capabilities as gaps; a
   mapping or availability probe does not prove product coverage. Before publishing,
   confirm every retained required behavior has a check, profile assignment, and
   evidence contract. Keep unresolved gaps visible.
4. Review oracle quality, permitted sub-agent freedom, pre-run authority, feasible
   budgets, continuation, and cleanup. Review literal payloads, public expectation
   authority, prerequisite and supporting work, native-host exclusions, and the
   complete 480-minute allocation. Keep profile lists explicit, and review content changes
   before updating asset hashes.

   After changing assets, run:

   ```sh
   python qa/validate.py
   python -m pytest tests/qa/
   ```

   The validator checks fields, IDs, references, prerequisite ordering, profile
   selections, and inventory coverage. The tests exercise rejection and fixture
   controls. Use `--coverage-index <path>` to review the derived
   capability/check/profile mapping.

5. Classify the change. Behavioral edits require affected-scenario reruns
   in required profiles; editorial edits do not. Keep actual tested revisions in
   qualification reports. Rehearse where feasible to discover underspecification;
   a product defect is a finding, not a reason to weaken a valid check. Keep landed
   fixes verification pending until the relevant checks pass. Do not add a separate
   Known Condition expiry workflow. Classify flaky failures by cause and justify
   targeted validation in review; no universal three-clean-runs-per-cell rule applies.

## Worked example: verify rendered waveform state

The Taxi journey creates scoped waveform state and opens it in the Waveform Viewer
through a VS Code Runtime Attachment. The `viewer.visual-capture` check separately
proves that the Waveform Viewer shows the expected signals, markers, and cursor. Its
authoring follows the five steps above:

1. Start with capability [W-05](../coverage.yaml), whose public contract requires
   Waveform Control Protocol (WCP) readback and a rendered screenshot. Keep this check
   separate from `viewer.actual-client-open`: the Waveform Viewer can open without
   showing the correct state.
2. Add the check to the existing Viewer sequence in
   [the Taxi scenario](../scenarios/taxi/scenario.yaml). Record the stimulus, expected
   visible state, evidence, and capture point:

   ```yaml
   - id: viewer.visual-capture
     capabilities:
     - W-05
     stimulus: Capture Viewer only with pre-run qualified observer; verify visible scoped signals/markers/cursor.
     expected: Capture Viewer only with pre-run qualified observer; verify visible scoped signals/markers/cursor.
     authority_ref: https://github.com/boldaxolotl/booley/blob/b163fd1f45b76f3950005678e500e695232832fb/qa/handoff/taxi.md
     evidence: Timestamped screenshot with Waveform Viewer and run identity plus observer capability evidence; unavailable observer means incomplete GUI profile.
     capture: evidence/taxi-10g-mac-port-evolution.viewer.visual-capture
   ```

   The containing step requires the pinned inputs and clean baseline outputs. It uses
   the scenario's authority and budget; it needs no check-specific timeout or recovery.
3. Add the scenario-qualified check ID to the applicable named check set in
   [profiles.yaml](../profiles.yaml):

   ```yaml
   - id: taxi-gui
     scenario_id: taxi-10g-mac-port-evolution
     checks:
     - taxi-10g-mac-port-evolution.viewer.visual-capture
   ```

   Only GUI profiles select `taxi-gui`. Their pre-run probes
   require a VS Code Runtime Attachment, WCP access to the Waveform Viewer, and a
   qualified screenshot observer. Core profiles cannot earn credit for this visual
   claim.
4. Run the validation commands from step 4 and inspect `--coverage-index <path>`.
   Review the screenshot oracle, pre-run observer qualification, prerequisite order,
   profile selection, and budget. An unavailable observer leaves the GUI profile
   incomplete; it does not justify removing the check.
5. Classify the addition as behavioral because it adds a required observation and
   evidence contract. Rerun the whole Taxi scenario in both required GUI profiles and
   record the tested revisions. A rehearsal may find a product defect; retain the
   failure instead of weakening the check.
