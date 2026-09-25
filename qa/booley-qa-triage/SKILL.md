---
name: booley-qa-triage
description: Triage finished Booley QA runs with the maintainer one finding at a time — verify each against current main, decide together, and file or fix as each decision lands.
---

# Triage Booley QA findings

Input: one or more QA run directories (each has `findings.md` and `log.md`).
The maintainer decides; you verify each finding and prepare the decision.
Triage is a conversation, one **brief** per turn.

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
5. **Brief one cluster, then wait.** Order: `qa-bug` clusters first, then product
   clusters by severity. Each brief states:
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
6. **Act on the decision immediately**, then log it in `triage.md` and brief the
   next cluster.
   - `file` (a product or doc cluster, or a `qa-bug` the maintainer asks to file):
     draft the issue in a local file with problem, repro, build identity, the verified
     cause, the **approved fix direction only**, and acceptance criteria. When the
     blast radius is yes or likely, the fix section starts with **Step 1: audit**,
     naming the suspected places and the pattern to search for; the fix then
     covers every affected place the audit finds. When the decision widens scope in
     other ways (for example "add a Doctor check"), write that into the issue too.
     Follow the issue rules in `AGENTS.md`: scan title and body with the
     confidential-content guard, then `gh issue create`. Label with `bug`,
     `documentation`, or `enhancement`, plus the triage role from
     `docs/internals/agents/triage-labels.md` (`ready-for-agent` when the fix is
     decided, `needs-triage` for open design).
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
