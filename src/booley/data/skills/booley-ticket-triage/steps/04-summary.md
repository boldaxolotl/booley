# Step 4: Summary

Track actions taken during this triage session and present:

```
## Triage Complete
| Action | Count | Tickets |
|--------|-------|---------|
| Unblocked | <n> | ... |
| Retried | <n> | ... |
| Archived | <n> | ... |
| Approved | <n> | ... |
| Rejected | <n> | ... |
| Reset | <n> | ... |
| Skipped | <n> | ... |

Queued tickets ready: <count>
```

If any queued → suggest running ticket execution.

If any blocked/review tickets were skipped, note them as still requiring attention.

Then, across every ticket triaged this session, list what was accepted without
being met — the two things that pass silently and are never raised again once a
ticket is approved:

- **unmet optional criteria**, per ticket
- **scope deviations** you classified as Justified — files that shipped outside
  the ticket's declared scope

Both are legitimate outcomes, and neither is a reason to hold a ticket. Name
them anyway: this is the user's last chance to turn one into a follow-up ticket.
Say "none" when there were none.

Also list confirmed Booley-side bugs found during triage and whether each was
captured through `/booley-feedback`. A ticket-local incident without that
handoff is unfinished triage unless feedback mode is off or the feedback skill
explicitly withheld it.
