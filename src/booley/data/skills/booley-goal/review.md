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
   rather than weakened tests or constraints. For Goals that name Test Runs,
   read the frozen Test Runs and their outcomes, the list "Test Runs resolved
   after entry" (their defaults were taken from `tests.toml` during the work),
   and any "Simulation Campaigns not fully accounted for". Check that the
   testbench does not special-case a frozen seed.
5. Present the package paths and a concise account of the result. A finished
   Goal is evidence-backed completion; merging or publishing remains a separate
   human instruction. Open `done` findings need no second approval for Finish.

If findings need fixes while Goal Mode is active, work in the same Goal Branch,
rerun the affected evidence producers, commit, and Finish with a new operation
ID and Session Summary. For separate follow-up work after Finish, enter a new
Goal Mode from a clean linked worktree. Do not reset or rewrite terminal records.

For an active Review Goal, its contract declares exactly one `target` or `paths`
and, for spec review, `spec`. The Developer Agent discovers by category/focus
only. A `done` review needs no verification and never blocks later discovery.
For `clean`, after fixes, `reviewer(verify=true)` checks only the latest review's open
findings. Propose a waiver with `waive=[["<finding-id>", "<justification>"]]`;
acceptance belongs to the Reviewer. Verify accepts only waiver proposals as
review arguments, adds no findings and is refused with no open findings.

A passing verify meets `clean` at the verified source contents. Later changes
to those contents make it stale, and changed specification, TB policy or Target
file selection still need discovery after open findings are resolved. Verify
does not change `done` completion evidence. Goal Mode resolves each open
`clean` review before discovery for another focus, keeping its findings
reachable through verify. Interactive discovery replaces earlier findings.
Verify budgets remain two attempts between category resets and three cumulative
cycles; exhaustion preserves findings for a human Goal Change or abandonment
decision.
