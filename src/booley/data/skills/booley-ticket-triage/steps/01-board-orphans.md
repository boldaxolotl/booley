# Step 1: Show Board & Handle Orphans

Display the board and detect orphans:

```bash
python -m booley.ticket_board board
python -m booley.ticket_board detect-orphans
```

If orphans are found, present each one (slug, stage, idle time) and ask: **force-fail** / **skip**.

- **Force-fail**: Run `python -m booley.ticket_board fail $SLUG --step <stage> --error "<msg>"` (both flags are required). Use the orphan's reported stage and an error such as `Force-failed orphaned running ticket for triage/inspection`. Print: `Force-failed -> will appear in blocked tickets below.`
- **Skip**: leave as-is

Notes:
- Orphans are rare in the developer path; they usually indicate an external kill, agent/API crash, or terminal-state bookkeeping bug.
