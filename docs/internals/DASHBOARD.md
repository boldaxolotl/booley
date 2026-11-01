# Dashboard layout

Layout rules of the Booley Dashboard (`booley dashboard`) that users see only as a result. Code: `src/booley/harness/dashboard/`.

## Sessions list

- A narrow list drops columns in this order: Started, then Work dir, then Branch. Values that still do not fit are cut and end in `…`.
- "Narrow" means the list's own width, not the terminal's: beside the preview the list is half the terminal, so it drops columns like a narrow terminal does.
- A row appears at the session's first Booley tool call. `tools/list` and other requests never create or refresh one.
- A thread or process session is hidden at once, then removed by registry maintenance, when its client reports `SessionEnd` after its last Booley call. A `worktree:` fallback row is not affected: it waits out `quiet_after`.
- Quiet-session Doctor warnings do not read end markers, so one can follow an ended session within about a minute.

## Preview pane (Sessions window)

- At 120 columns or wider (`WIDE_COLUMNS` in `app.py`) the overview splits into the list and a preview of the highlighted row. Below 120 the preview is hidden and the list uses the full width. It is also hidden when the overview has no rows.
- A session's preview shows its handle and mode, activity with duration, branch, work dir, start age, connection, last Booley call with its age, the shared-worktree warning when present, a one-line Goal summary in Goal Mode, and its unfinished Jobs. Finished Jobs and the full Goal list are not shown. Absent values show `—`.
- A standalone Jobs row previews the same content as Job detail.
- The preview follows the cursor and refreshes with the snapshot without moving the cursor or the list scroll.
- `Enter` still opens the session detail (or the Job detail for a standalone Job). The preview and session detail share one orientation derivation (`SessionOrientation`), so their facts cannot disagree.
