# Splitting the Booley Directory by Booley State kind

Booley does not reorganize the Booley Directory so that a path's folder
shows its kind of Booley State. Two shapes of this idea were rejected:

- Splitting the whole Booley Directory into a Common folder and a Checkout
  folder (#3474).
- Gathering only the non-versioned Checkout Booley State (build trees, Flow
  reports, locks, scratch) under one folder (#2854).

Paths stay where their writers put them. The classification table in
`src/booley/core/booley_dir_parts.py` gives each path its own row, and that
table, not folder location, is what code uses.

## Why this is out of scope

Code reads the classification table. The Snapshot copies only rows
classified Versioned. The Doctor common-state check and `booley worktree rm`
find Common paths through the table. Moving folders would mostly shorten
the table.

The cost is far larger:

- **Writers.** Many paths are built by joining a leaf onto the Booley
  Directory or onto `$BOOLEY_RUNTIME_DIR` by hand. That variable also holds
  Common state, so repointing it moves Common state too. Each leaf would
  need its own resolver.
- **Code that walks up from `.runtime`.** Some code treats the parent of
  `.runtime` as the Booley Directory.
- **Old Jobs.** Standalone Job records store the checkout's Booley
  Directory as `report_root`. Readers would need a fallback to old
  locations, or old Jobs lose their reports.
- **Report rescue.** `booley worktree rm` matches exact Flow report folders
  against table rows. Collapsing rows breaks that match.
- **User Projects.** The full split renames tracked files in every user
  Project, which conflicts with their open branches, amends ADR 0082, and
  needs a Feature Flag under ADR 0095.

The narrow move (#2854) was estimated at 450 to 800 changed lines for a
shorter table. The full split (#3474) runs to thousands of lines.

The one concrete gap the full split named, a file in a worktree's Booley
Directory that no row covers, is closed by a Doctor warning instead (#5625).

## When to reconsider

Reconsider if Booley starts depending on where state lives rather than on
the table. Examples: a Sandbox or Snapshot mechanism that can only include
or skip whole folders, or a tool outside Booley that must find Checkout
state without importing the classification table.

## Prior requests

- #2854: "Move non-versioned Checkout Booley State under one folder in the
  Booley Directory"
- #3474: "Split the Booley Directory into a Common folder and a Checkout
  folder"
