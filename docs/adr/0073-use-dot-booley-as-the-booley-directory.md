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

Inside a Sandbox, Doctor accepts exactly two spellings of the Booley Directory:
`BOOLEY_DIR=/booley-project` (when that path is a directory) and
`BOOLEY_DIR=<Project Root>/.booley`. The second is the `.booley` entry directly
under the Project Root, as spelled: an absolute path without a `..` component,
whose parent is the Project Root, naming a directory or a link to one. Every
other value fails, including the link target spelled directly, a `.booley`
under another directory, a relative path, and a dangling or non-directory
entry. A Sandbox without the `/booley-project` alias (a Codespace has no
`/work`) therefore passes with the second spelling.

The rule reads only `BOOLEY_DIR`, the Project Root and the filesystem. It
ignores `CODESPACES` and every other platform variable: they are ambient and
spoofable, so Booley never infers trust from them (ADR 0077). Link targets are
pinned on the host only; inside a Sandbox a `.booley` link to any existing
directory is accepted. That adds no authority, because the Sandbox is the
isolation boundary and the rule only checks that Doctor and the MCP server read
the same directory.

A failed identity check no longer stops inspection: the Docker-socket,
EDA-credential, `~/.ssh` and Vivado checks still run and report.
