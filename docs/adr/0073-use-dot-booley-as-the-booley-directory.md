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

## Amendments (2026-10-29)

Recorded while implementing
[#2287](https://github.com/boldaxolotl/booley/issues/2287).

- **Accepted in-Sandbox `BOOLEY_DIR` spellings.** In-Sandbox Doctor accepts
  `BOOLEY_DIR` when it is exactly `/booley-project`, or when it is the
  `.booley` entry directly under the Project Root that Doctor inspects
  (for example `/workspaces/<project>/.booley` in a Codespace). Either spelling
  must be absolute and resolve to an existing directory; the `.booley` entry
  may be a symlink. Any other spelling, a `.booley` under another directory, or
  a dangling entry fails the Booley Directory identity check. Its remedy is in
  the Sandbox: set `BOOLEY_DIR` to `<Project Root>/.booley`, or open the
  Project through an issued Sandbox.
- **Paths decide, platform variables do not.** The rule ignores `CODESPACES`
  and other ambient platform variables. They are spoofable, and ADR 0077
  rejects inferring Sandbox behavior from them for the same reason.
- **Identity does not gate isolation.** The identity check is reported
  separately from the host-authority checks (Docker sockets, host EDA
  configuration, SSH credentials) and the mounted-Vivado checks, which run
  whatever the identity result.

## Amendments (2026-10-29)

Recorded while implementing issue #2407. No release ever wrote Goal Records, so
Goal code applies no archival rules: it no longer rebases presentation paths,
accepts historical publication spellings, or replays a terminal operation
without its worktree. Migration still moves Goal Records byte for byte. The
paragraph above on archival support no longer describes Goal behavior.
