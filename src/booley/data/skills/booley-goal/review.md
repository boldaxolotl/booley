# Read a Review Package

Read this when Finish returns a Review Package or the human asks to review Goal
work. Use the returned package and file pointers; do not guess a latest record.

1. Read the Session Summary and diff against the Goal base. Explain what
   changed and why, then inspect the actual source changes.
2. Check each final Goal's evidence, Target identity, producer, and freshness.
   A `clean` review needs no open findings; a `done` review may leave advisory
   findings. List those findings and remaining uncertainties explicitly.
3. Read the Change Log. Show every approved add, relax, retarget, or waiver
   with the human's reason and approval source. Review coverage waiver evidence
   and justification; a candidate is not an approval.
4. Inspect Target changes, `.core` and `tests.toml` changes, and `.sdc`/`.xdc`
   constraint edits. Check that improved results come from the intended design
   rather than weakened tests or constraints.
5. Present the package paths and a concise account of the result. A finished
   Goal is evidence-backed completion; merging or publishing remains a separate
   human instruction. Open `done` findings need no second approval for Finish.

If findings need fixes while Goal Mode is active, work in the same Goal Branch,
rerun the affected evidence producers, commit, and Finish with a new operation
ID and Session Summary. For separate follow-up work after Finish, enter a new
Goal Mode from a clean linked worktree. Do not reset or rewrite terminal records.
