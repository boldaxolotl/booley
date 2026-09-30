# Booley Flows and Specialists — Quick Reference

Booley ticket execution uses a **developer agent** that invokes Booley Flows and
Specialists to drive a ticket to completion. Tickets must have `criteria:`
frontmatter. After setup, the developer invokes Flows (`sim`, `lint`,
`synth`, `fpga`) and Specialists (`reviewer`, `mutation_tester`). Progress is tracked in
`logs/<slug>/.runtime/booley_state.json`.

For triage, detect the execution path from ticket frontmatter or by checking for
`logs/<slug>/.runtime/booley_state.json`.

## Reset

`reset` archives the current run artifacts and queues the Ticket for a clean
retry. For a current Ticket, it discards implementation state and restores all
participant worktrees at the same immutable Ticket baseline; it preserves that
baseline. A successful command prints the canonical slug, `queued` state, and
workspace disposition. Use `booley board reset $SLUG --reason "<correction reason>"`.

To change Target definitions or source baselines, return the Ticket to draft,
correct the authoring inputs, validate, and enqueue a new generation. For an
approved requirement relaxation within the supported operations, use amendment.
See `steps/02-blocked.md` for those recovery paths.

Old unsealed/legacy reset behavior discarded disposable worktrees and branches.
Current lifecycle commands reject unsupported legacy Tickets: recreate them
instead. `contract-seal`, `revise-contract`, and `reset-to` are retired commands.

## Available Booley Flows and Specialists

Run **`booley cheat --flows`** for the deterministic Flow roster. Use the MCP
catalog for Specialists and other MCP endpoints. Each catalog gives the name,
purpose, and criteria set, and includes project-specific extensions; a hand-copied table
here would drift. (`booley cheat --criteria` is the matching criterion catalog,
and `booley cheat --list` names the other sections.)

Those canonical names are exactly the names that appear in `booley_state.json`
and in the report directory layouts.

Note: there is no `investigate` or `implement` MCP tool — the developer edits code and
diagnoses failures directly, not via a registered Booley Flow, so no `investigate.json`
or `implement.json` report is ever written.

Flow reports are stored under `logs/<slug>/.runtime/flow-reports/`; Specialist
and other generic endpoint reports are stored under
`logs/<slug>/.runtime/mcp-tool-reports/`. Both use
`<name>/<N>/report.json` (highest `N` is latest) plus a flat `<name>.json`
compatibility copy.

For triage, read the criterion state first and open only the report that backs a
result requiring detail. Do not aggregate these reports into another saved
summary.

## Run report

The developer's last action is `submit_run_report`, which writes
`logs/<slug>/REPORT.md`: summary, one type-specific section (root cause /
design decisions / behavior preservation / coverage added), and the developer's
own uncertainties. It is the run's first-hand account — read it before the
mechanical evidence. Its absence means the developer never finished.
