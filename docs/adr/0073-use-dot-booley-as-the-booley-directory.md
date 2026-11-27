---
status: accepted
---

# Use `.booley/` as the Booley Directory

The Booley Directory is `.booley/`; its selection variable is `BOOLEY_DIR` and
its Python interface is `booley.runtime.booley_dir`. Project Root retains its
name. Aligning the on-disk name with the shared vocabulary avoids confusing
Project Root with the directory holding Booley inputs and local state (#1361).

This is a hard cut: there is no alias or dual-read period, and the older
`.booley/project` and embedded Booley-source layouts are unsupported. Booley
0.4.x's `booley init` is the migrator from Booley 0.3.x's `.booley_project/`.
This release does not migrate. It refuses each 0.3.x leftover with one message
to upgrade through Booley 0.4.x first:

- `.booley_project/` at the Project Root, with or without `.booley/`;
- the journal of an interrupted 0.4.x migration;
- `BOOLEY_PROJECT_DIR` in the environment, even empty;
- a selection through `.booley_project`;
- a linked checkout's Git pointer into a missing `.booley_project` path.

`booley init` raises the refusal before it writes anything. Keeping the
migrator was rejected: its journal, Docker inventory, consumer leases and
layout lock cost every Project command and worktree change, for a step each
0.3.x Project takes once.

## In-Sandbox selection

- **Accepted `BOOLEY_DIR` spellings.** In-Sandbox Doctor accepts `BOOLEY_DIR`
  when it is exactly `/booley-project`, or when it is the `.booley` entry
  directly under the Project Root that Doctor inspects (for example
  `/workspaces/<project>/.booley` in a Codespace). Either spelling must be
  absolute and resolve to an existing directory; the `.booley` entry may be a
  symlink. Any other spelling, a `.booley` under another directory, or a
  dangling entry fails the Booley Directory identity check. Its remedy is in
  the Sandbox: set `BOOLEY_DIR` to `<Project Root>/.booley`, or open the
  Project through an issued Sandbox.
- **Paths decide, platform variables do not.** The rule ignores `CODESPACES`
  and other ambient platform variables. They are spoofable, and ADR 0077
  rejects inferring Sandbox behavior from them for the same reason.
- **Identity does not gate isolation.** The identity check is reported
  separately from the host-authority checks (Docker sockets, host EDA
  configuration, SSH credentials) and the mounted-Vivado checks, which run
  whatever the identity result.

This supersedes path spellings in ADR 0067 while preserving its Goal authority
and immutable-history decisions. Sandbox home `~/.booley/`, XDG configuration,
and caches keep their names.
