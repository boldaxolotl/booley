---
status: accepted
---

# Use `.booley/` as the Booley Directory

The Booley Directory is `.booley/`; its selection variable is `BOOLEY_DIR` and
its Python interface is `booley.runtime.booley_dir`. Project Root retains its
name. Aligning the on-disk name with the shared vocabulary avoids confusing
Project Root with the directory holding Booley inputs and local state (#1361).

This is a hard cut. Full `booley init` is the only migrator from
`.booley_project/`: it checks Goal occupancy before writes, journals directory
moves and Git pointer edits, preserves repository/admin identities, and repairs
both RTL worktrees and Paired Checkouts. Check mode and Doctor only observe.
All other consumers reject the retired directory and `BOOLEY_PROJECT_DIR`.
The older `.booley/project` and embedded Booley-source layouts are unsupported;
positive discovery, protection and hook-installation branches for them are removed.

A collision always refuses and reports both directories. There is no implicit
merge/adopt mode, alias or dual-read period. An interrupted migration resumes
only from its identity-bound journal; conflicting or substituted endpoints stop
recovery without deleting either tree. Migrate each clone before pulling a
tracked rename commit; already-pulled clones require explicit preservation and
manual reconciliation as documented in the changelog.

Init does not stage or commit tracked directory changes, switch branches or
rewrite history. Owners review and commit the rename in every retained checkout;
a detached checkout needs an explicit branch before such a commit. Existing
clean-tree rules continue to apply to Goal entry. Stealth exclusion remains a
separate policy: a non-Stealth Booley Directory and its Goal history remain
trackable.

Finished/abandoned records and sealed results retain their original bytes,
Worktree Identity and digests. Migration metadata rebases presentation/artifact
paths; confined historical publication spellings are validated only in the
migration-owned archival boundary. Terminal replay validates original sealed
facts without authorizing new publication or requiring a surviving worktree.
New finish attempts use the current format. This archival support does not
restore old-layout discovery or permit a nonterminal legacy operation.

Migration requires quiescent consumers and refuses live affected Sandboxes.
The host lifecycle lock and stable layout admission lock coordinate updated
Booley commands; direct Git/Docker operations and old binaries must be stopped
operationally. Cross-VM flock is not assumed. Native submodule metadata requires
non-destructive preparation rather than forced deinitialization by init.

This supersedes path spellings in ADR 0057 and ADR 0067 while preserving their
source-checkout separation, Goal authority, and immutable-history decisions.
Sandbox home `~/.booley/`, XDG configuration, and caches keep their names.
