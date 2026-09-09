# Authoring QA scenarios

Read [Protocol](../agents/PROTOCOL.md) for execution, [Qualification](QUALIFICATION.md)
for scope and verdicts, and [Format](../agents/FORMAT.md) for fields. The
[worked example](../examples/README.md) shows how they fit together.

1. Identify the public behavior being checked and its contract source. Consult the
   capability inventory and extend the coherent journey that already exercises it.
   Preserve distinct checks for independently observable requirements.
2. Place stimulus, expectation, evidence, and capture point beside each check. Supply
   scenario-specific prerequisites, authority, timeout, recovery, and cleanup.
   Preserve the full fault/restoration sequence for seeded faults.
3. Assign capability references and explicit qualification profiles. UI/client claims
   need corresponding evidence. Record missing capabilities as gaps rather than
   treating a mapping or availability probe as product coverage.
4. Validate fields, IDs, references, prerequisite ordering, profile selections, and
   inventory coverage. Review oracle quality, permitted agent freedom, pre-run
   authority, feasible budgets, continuation, and cleanup.
5. Review and classify the change. Behavioral edits require affected-scenario reruns
   in required profiles; editorial edits do not. Keep actual tested revisions in
   qualification reports. Rehearse where feasible to discover underspecification;
   a product defect is a finding, not a reason to weaken a valid check.

A specification-backed check is mandatory from its reviewed introduction. It need
not first pass a reference run. An expectation without an authoritative basis is a
research question or observation until resolved; no candidate/established assertion
registry is needed. Ordinary maintainer review governs changes to the specification.

Link known defects to issues and retain their failures. Finding updates preserve
original evidence; a landed fix remains verification pending until the relevant
checks pass against it. Do not introduce a separate Known Condition expiry workflow.
Flaky failures require cause/classification and targeted validation justified in
review; there is no universal three-clean-runs-per-cell requirement.

Before publishing a scenario, verify that every retained required behavior has a concrete check,
profile assignment, and evidence contract. Keep every unresolved gap visible.

Run `python qa/validate.py` and `python -m pytest tests/qa/` after changing assets.
The former is a static check; the latter exercises rejection and fixture controls.
Use `--coverage-index <path>` to review the derived capability/check/profile mapping.
Review literal payloads, public expectation authority, all prerequisite work,
platform exclusions and the complete 480-minute allocation. Keep named check sets
explicit, and update asset hashes after reviewed content changes.
