---
name: booley-qa-triage
description: Only when the user explicitly asks, triage finished Booley QA runs with the maintainer one finding at a time — verify each against current main, decide together, and file or fix as each decision lands.
---

# Triage Booley QA findings

Use this skill only when the user explicitly invokes it. This is behavioral
guidance for clients without enforceable invocation metadata. Resolve the real
path of this loaded `SKILL.md`; derive `qa/` and the source root from its parent
directories. Before acting, require that source to be a clean primary checkout
on `main`, with `.git` as a directory. Stop with restore/re-enable guidance if
validation fails. The installed `booley` revision does not matter here: triage
verifies every finding against `origin/main`, and each run's `log.md` records
the build it tested. Resolve repository inputs such as
`docs/internals/agents/triage-labels.md`, `qa/missions/`, and `qa/shared/` from
that derived root, never from the process working directory.

Input: one or more QA run directories (each has `findings.md` and `log.md`).
The maintainer decides; you verify each finding and prepare the decision.
Triage is a conversation, one **cluster summary** per turn.

1. **Read.** Read every run's `log.md` and `findings.md` in full. Note each
   run's identity header (build under test, mission revision) so findings from
   different builds stay distinguishable. Done when every finding has a reference
   such as `<run-id>#F-3`.
2. **Cluster.** Group findings that describe the same underlying problem, across
   runs as well as within one. Keep a causal chain as linked clusters (`caused-by`)
   rather than one merged cluster. Done when every finding sits in exactly one
   cluster.
3. **Check the tracker.** List open and recently closed issues once
   (`gh issue list --state all --limit 300`) and match every cluster against it.
   Mark clusters `known #<n>`, `fixed #<n>` (and whether the run's build predates
   the fix), or `new`.
4. **Open the decision log.** Create `<run-dir>/triage.md`: one line per cluster
   with its finding refs, the decision, and the issue number or mission change.
   It is the resume point after an interruption or handoff. Show the maintainer a
   short overview (counts by kind and severity, the cluster order) and start.
5. **Summarize one cluster, then wait.** Order: `qa-bug` clusters first, then product
   clusters by severity. Start every cluster summary, and every batch of
   low-severity clusters, with a progress line computed from `triage.md`:
   `Progress: <clusters decided>/<all clusters> clusters · <findings decided>/<all findings> findings · <findings left> findings left`.
   Each cluster summary states:
   - what happened, with the decisive command and output excerpt from the evidence;
   - the cause, **verified against current `origin/main`** (`git fetch`, then
     `git show`/`git grep` on `origin/main`) with `file:line`: the build under test
     may be stale, and a finding already fixed or documented on main says so;
   - why it is a product defect, doc defect, or `qa-bug`, or why it is weaker than
     reported. Downgrade, reclassify, or narrow a finding when the code or the design
     supports it, and say what the design intends;
   - **blast radius**: can the same defect live elsewhere in Booley, such as the
     same path handling in the other Flows, or the same pattern in sibling
     commands? Answer yes, no, or likely from the cause and a quick look at its
     callers and siblings. Stop there: the audit belongs to the agent that works
     the issue;
   - fix directions as lettered options with your recommendation.

   Wait for the maintainer's decision. Answer their questions with more code
   reading before re-proposing. Done when the maintainer has decided this cluster.
6. **Act on the decision immediately**, then log it in `triage.md` and summarize
   the next cluster.
   - `file` (a product or doc cluster, or a `qa-bug` the maintainer asks to file):
     draft two local files. The issue body holds the context: problem, repro,
     build identity, the verified cause with `file:line`, and the blast radius
     assessment. The Agent Brief holds the **approved fix direction only**, per
     `docs/internals/agents/agent-brief.md`. When the blast radius is yes or
     likely, the brief's `**Audit:**` field names the pattern and the suspected
     places, and an acceptance criterion requires fixing every place the audit
     finds. When the decision widens scope in other ways (for example "add a
     Doctor check"), write that into the brief too. Follow the issue rules in
     `AGENTS.md`: scan title, body, and brief with the confidential-content
     guard, then `gh issue create` with the category label (`bug`,
     `documentation`, or `enhancement`, matching the brief's Category) and
     `needs-triage`. When the fix is decided, post the brief with
     `gh issue comment`, then replace `needs-triage` with `ready-for-agent`, so
     the issue never looks ready without its brief. Open design keeps
     `needs-triage` and gets no brief. When the approved fix does not fit one
     PR, file a parent issue (context only, no brief, never
     `ready-for-agent`) and one sub-issue per piece, each with its own brief
     and `Blocked by` order, per the brief doc's "One PR per issue".
   - `comment on #<n>`: same drafting and scan, then `gh issue comment`.
   - `fix mission`: record the exact change in `triage.md`; mission edits are
     batched in step 7.
   - `drop`: record the reason.
   When the maintainer asks for progress, give findings done and left, and offer
   the remaining low-severity clusters in batches of four to six, one line each.
7. **Apply mission fixes** after the last decision, in one worktree and branch per
   `AGENTS.md`: every recorded `fix mission` change under `qa/missions/` and
   `qa/shared/`, with traps in the mission's **Known traps** section.
8. **Summarize.** Report what was filed, commented, fixed, and dropped, with issue
   links, and the path of `triage.md`.
