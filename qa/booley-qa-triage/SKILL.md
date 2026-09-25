---
name: booley-qa-triage
description: Triage finished Booley QA runs with the maintainer — deduplicate findings against each other and open issues, then file the approved ones.
---

# Triage Booley QA findings

Input: one or more QA run directories (each has `findings.md` and `log.md`).
The maintainer decides what gets filed; you prepare the decision.

1. **Read.** Read every run's `log.md` and `findings.md` in full. Note each
   run's identity header so findings from different builds stay distinguishable.
   Done when every finding has a reference such as `<run-id>#F-3`.
2. **Cluster.** Group findings that describe the same underlying problem, across
   runs as well as within one. Keep a causal chain as linked clusters (`caused-by`)
   rather than one merged cluster. Done when every finding sits in exactly one
   cluster.
3. **Check the tracker.** Search open and recently closed GitHub issues for each
   cluster (`gh issue list --search`). Mark clusters `known #<n>`, `fixed #<n>`
   (and whether the run's build predates the fix), or `new`.
4. **Present.** Show the maintainer one table: cluster, kind, severity, runs and
   finding refs, tracker status, and proposed action. For `qa-bug` clusters the
   choices are `fix mission` or `drop`; never propose an issue or comment. For
   every other kind the choices are `file`, `comment on #<n>`, or `drop`. Wait
   for the maintainer's decisions.
5. **Act on approved items only.**
   - `file` / `comment` (never for `qa-bug`): draft each issue or comment in a
     local file with a minimal repro, expected vs actual, build identity, and
     evidence excerpts. Follow the repository's issue rules in `AGENTS.md`
     (confidential-content scan before submitting) and the triage labels in
     `docs/internals/agents/triage-labels.md`.
   - `fix mission`: edit the mission under `qa/missions/`, usually adding the
     trap to its **Known traps** section.
6. **Summarize.** Report what was filed, commented, fixed, and dropped, with
   issue links.
