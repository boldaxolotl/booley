# Goal Mode glossary

This is the vocabulary for the inside of one Goal Mode: how its Goals are
bound to a worktree and to the runs that produce their evidence. It is for contributors working on `booley.goals` and the evidence
path. [ADR 0092](../../../docs/adr/0092-measure-commits-against-target-metrics.md)
removes Goal Mode in 0.5.0, and this vocabulary retires with it.

**Goal Mode**, **Goal**, **Goalset**, **Goal Branch**, **Goal Record**,
**Goal Change**, **Goal Change Proposal**, **Change Log**, **Protected Input**,
**Review Package**, **Session**, **Test**, **Test Variant**, **Test Run**,
and **Default Seed** are defined in the
[shared glossary](../../../docs/GLOSSARY.md); this glossary uses them with
that meaning. **Waiver Candidate** and **Provisional Coverage
Verdict** belong to the [Simulation Coverage glossary](../flows/sim/GLOSSARY.md);
Goal Mode uses its proposal-and-approval workflow to promote a candidate.

## Language

**Worktree Identity**:
The worktree a Goal Record belongs to, as Git knows it: the repository plus
the checkout (`main` for the primary checkout, otherwise the linked
worktree's administrative name). Every path spelling of one worktree has the
same Worktree Identity. One worktree hosts at most one occupying Goal Mode.
_Avoid_: worktree path, session, owner

**Run Binding**:
What one Flow or Specialist run was admitted under: its active Goal Record
and that record's revision, the Goal Branch, the specification revision of
every Goal, and the Protected Inputs as the run started. The run's evidence
is checked against it again when it is published; it is never re-resolved.
_Avoid_: session binding, run context

**Frozen Test Run**:
A Test Run that a Goal requires, fixed in the Goal Record when the Goal is
resolved: at entry, or later when the Target, Test or Test Variant it names
first exists. It keeps its Test Variant, seed, and declared arguments and
environment whatever `tests.toml` says afterwards. A Goal comes to require
other Test Runs only through a Goal Change, or, when it covers every Test of
its Target, when a Test is added to that Target. Its outcome is `passed`,
`failed`, or `missing`. Any other Test Run is a **Diagnostic Test Run**: it is
reported, records no evidence, and its failure blocks no Goal.
_Avoid_: pinned test, locked seed, required test; extra run, unofficial run (for a Diagnostic Test Run)
