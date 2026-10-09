---
name: booley-add-to-qa
description: Only when the user explicitly asks, add a Booley behavior, fault idea, or known trap to the QA missions so future runs exercise it.
---

# Add to Booley QA

Use this skill only when the user explicitly invokes it. This is behavioral
guidance for clients without enforceable invocation metadata. Resolve the real
path of this loaded `SKILL.md`; derive `qa/` and the source checkout from its
parent directories. Before acting, require that source to be a clean primary
checkout on `main`, with `.git` as a directory and HEAD matching the canonical
host `booley --version` revision. Stop with restore/re-enable guidance if that
check fails. Resolve all repository inputs from this derived root, never from
the process working directory.

Input: prose describing a Booley behavior, a bug class, or a trap from a past
run.

1. **Place it.** Read `qa/AREAS.md` and the candidate missions under
   `qa/missions/`. Decide whether the behavior is already exercised, belongs in
   an existing area, needs a new area in one mission, or is a trap for
   **Known traps**. Prefer extending an existing area; a new mission is rare and
   needs a reason no existing IP can serve.
2. **Propose.** Show the maintainer the exact text to add: for an area, its
   intent, what to try (including one fault or edge case), what to look for,
   and its timebox; the `AREAS.md` line if a capability is new. Wait for
   approval.
3. **Apply.** Edit the mission and `AREAS.md`. Keep the mission's areas in
   priority order and its total timebox within 8 hours; if the addition pushes
   it over, say which area should shrink. Add any new fixture, prompt, or Goalset
   file next to the mission and reference it by relative path.
4. **Check.** Every path the edited mission references exists
   (`python -m pytest tests/qa/test_missions.py`).
