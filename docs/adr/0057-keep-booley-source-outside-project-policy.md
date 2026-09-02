---
status: accepted
---

# Keep Booley Source Outside Project Policy

A Booley Source Checkout is never a Project: Project Initialization refuses it,
Project discovery fails closed there, and Project Stealth Mode and Git hooks do
not apply to its commits or tracked paths. Current source checkouts carry an
explicit tracked role marker, while the distribution name plus distinctive
source layout recognizes older branches, forks, and linked worktrees without
depending on a Git remote. Dogfood feedback remains available but stores its
local state under Git's shared metadata directory instead of creating
`.booley_project/`.

## Considered options

- Removing `booley` from the Stealth denylist was rejected because downstream
  Projects still need to prevent product and workflow identifiers from leaking.
- Classifying by Git remote was rejected because forks, offline clones, and
  linked worktrees are still Booley Source Checkouts.
- Letting source checkouts opt out through Project configuration was rejected
  because creating that configuration would itself violate the distinction.

## Consequences

Project commands reject stale source-local Project state and environment
overrides rather than adopting them. Existing `.booley_project/` directories
and foreign hooks in a source checkout require a separate, explicitly approved
operational migration; detection never deletes or rewrites them.
