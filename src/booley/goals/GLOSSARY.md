# Goal Mode glossary

This is the vocabulary for the inside of one Goal Mode: how its Goals are
stored, changed, and bound to a worktree. **Goal Mode**, **Goal**,
**Goalset**, **Goal Branch**, **Goal Record**, **Goal Change Proposal**,
**Change Log**, **Protected Input**, **Review Package**, and **Session** are
defined in the [shared glossary](../../../docs/GLOSSARY.md); this glossary
uses them with that meaning.

## Language

**Goal State**:
Where one Goal Mode is in its lifecycle: `entering`, `active`, `finishing`,
`finished`, `abandoned`, or `failed`. Only `entering`, `active`, and
`finishing` occupy the worktree; `finished`, `abandoned`, and `failed` are
final, and a `failed` Goal Mode may be replaced by a new entry.
_Avoid_: status, board state, phase

**Worktree Identity**:
The worktree a Goal Record belongs to, as Git knows it: the repository plus
the checkout (`main` for the primary checkout, otherwise the linked
worktree's administrative name). Every path spelling of one worktree has the
same Worktree Identity. One worktree hosts at most one occupying Goal Mode.
_Avoid_: worktree path, session, owner

**Goal Origin**:
Where a Goal came from at entry: the name of a Goalset, or `ad-hoc` when the
human named it directly. A Goal merged from several requests keeps every
origin.
_Avoid_: source, template

**Goal Change**:
One approved Goal Change Proposal as the Change Log records it when it is
applied: it adds, relaxes, or retargets one Goal, or waives coverage points
(`add`, `relax`, `retarget`, `waiver`), together with the human's reason
and how the approval was obtained. Rejected proposals are not Goal Changes.
_Avoid_: amendment, criteria edit

**Interrupted Apply**:
A Goal Change whose intent is in the Change Log but whose application was
never confirmed, because the process stopped in between. The next call that
changes or finishes the Goal Mode completes it first.
_Avoid_: pending change, partial write

**Run Binding**:
What one Flow or Specialist run was admitted under: its active Goal Record
and that record's revision, the Goal Branch, the specification revision of
every Goal, and the Protected Inputs as the run started. The run's evidence
is checked against it again when it is published; it is never re-resolved.
_Avoid_: session binding, run context

**Discarded Evidence**:
A run's evidence that was not published because its Run Binding no longer
held when the run finished: the Goal Mode stopped being active, an affected
Goal changed, or a Protected Input differed at the start or the end of the
run. Nothing of it reaches the Goal state or the ledger.
_Avoid_: rejected run, failed evidence
