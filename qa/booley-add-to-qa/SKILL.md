---
name: booley-add-to-qa
description: Add a Booley behavior, fault idea, or known trap to the QA missions so future runs exercise it.
---

# Add to Booley QA

Input: prose describing a Booley behavior, a bug class, or a trap from a past
run.

1. **Place it.** Read [AREAS.md](../AREAS.md) and the candidate missions under
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
   it over, say which area should shrink. Add any new fixture, prompt, or Ticket
   file next to the mission and reference it by relative path.
4. **Check.** Every path the edited mission references exists
   (`python -m pytest tests/qa/test_missions.py`).
