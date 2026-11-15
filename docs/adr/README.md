# Architecture Decision Records

One file per design decision that is hard to reverse, surprising without
context, and the result of a real trade-off. Read an ADR to learn the decision
that holds today and why. `git log` keeps how it got there.

## Rules

- **State the current decision.** When a decision changes, rewrite the ADR in
  place. No "Amendments", "Implemented by", or "as built" sections.
- **Delete an ADR with its subject.** The pull request that removes a feature,
  or replaces a decision with a new ADR, deletes the old ADR and the links to
  it.
- **Keep it under about 4 KB.** Write the context, the decision, and the
  consequences or rejected alternatives that still constrain future work.
  Paths, constants, flags, schemas, and UI layout belong in the code.
- **Number a new ADR one above the highest existing file**, as
  `NNNN-slug.md`. Never reuse a number: code comments cite ADRs by number.

The `domain-modeling` skill covers when a decision deserves an ADR and how to
word one.
