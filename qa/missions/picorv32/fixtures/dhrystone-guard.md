### Dhrystone negative guard

After the continuity work is Measured and its worktree branches are integrated,
use a disposable checkout of the Project at that source. The operator commits
the negative seed; an Interactive child
collects evidence in a linked worktree.

Try:
- Identify the first executed equality comparison in the new deterministic
  final-result guard, in lexical source order. The clean accepted run must
  establish equality before mutation.
- Change only its expected integer operand from N to `N ^ 1`. Record the exact
  original/replacement span and value.
- Rebuild and rerun with unchanged iteration count and testbench. Require a
  deterministic mismatch/trap, no success magic and no cycle record.
- Restore the exact original bytes, rebuild and require pass. Remove
  the disposable worktree, then discard its owned branches.
- If no explicit guard operand exists, its verifier must expose the equivalent
  deterministic expected-result assertion as reviewable fixture input before
  mutation. Do not guess or weaken the guard. Adapt to generated syntax while
  preserving the expected behavior.
