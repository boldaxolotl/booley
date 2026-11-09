# Run Project Initialization anywhere

Status: accepted. Amends [ADR 0056](0056-separate-host-bootstrap-from-project-initialization.md).

## Context

ADR 0056 split Host Bootstrap from Project Initialization but left Project
Initialization holding host-only work: the Sandbox Image and Sandbox Issuance.
Project-file setup was spread over init-only steps, so `booley session refresh`
re-applied only the Dashboard task and a few excludes. Drift in Git hooks, Git
settings, guidance links and core projections survived a refresh, and nothing
could run the Project-file steps from inside a Sandbox
([#2751](https://github.com/boldaxolotl/booley/issues/2751)).

## Decision

- **Location-neutral.** Project Initialization is one step,
  `init_cmd.initialize_project`, over the Project Root and its Booley
  Directory. It uses no Docker, host credentials, host-wide lock, private store
  or Sandbox Issuance. Facts only the host knows, such as the Sandbox Git
  version behind `worktree.useRelativePaths`, and the Agent CLI choice are
  injected.
- **Host init is Project Initialization plus host-only steps.** Host
  `booley init` runs it, then Sandbox Issuance, image retirement, inventory and
  advisories; its output is unchanged. Its Sandbox Issuance step keeps owning
  the Dashboard task and Interactive excludes, with their preconditions and
  rollback, so host init skips the Project-file copy of that step.
- **Refresh never rewrites tracked content.** After the Sandbox replacement
  settles, `session refresh` runs Project Initialization in a mode that only
  inspects and reports every step that would rewrite an existing tracked file:
  line-ending normalization, attributes migration and tracked `.gitattributes`
  publication, the `.booley/.gitignore` append and the `[agent]` insertion into
  an existing `booley.toml`. Git settings, missing files, untracked
  projections and links are still applied; `core.autocrlf` and the local
  attributes file wait while CRLF checkouts exist, because they would only
  surface phantom diffs. A failed step fails the command but keeps the new
  Sandbox.
- **No run inside Booley-issued Sandboxes.** A run from inside one is refused;
  that is implemented in #2751.
- Doctor is unchanged.

## Considered options

- **Reorder host init** so the image step runs before Project Initialization.
  Rejected: it changes init's output order.
- **Run inside refresh's journaled finalization.** Rejected: a failure there
  leaves a committed-forward journal that reruns recovery on every host
  command.
