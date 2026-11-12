# Dashboard layout

This doc records the layout and row-lifetime rules of the
[Booley Dashboard](../GLOSSARY.md) (`booley dashboard`) that users only see as
a result, with the reason for each. Read it before changing
`src/booley/harness/dashboard/` or the Session Registry
(`src/booley/mcp/session_registry.py`). The Dashboard's design and the full
history of these rules are in
[ADR 0068](../adr/0068-booley-dashboard.md); launching Booley Flows from it is
[ADR 0086](../adr/0086-launch-flows-and-specialists-from-the-dashboard.md).

## Session rows

A row is one [Session](../GLOSSARY.md), and it lives as long as the Session
Registry holds it:

- A row appears at the session's first Booley tool call. `tools/list` and
  other requests never create or refresh one, because clients send them on
  connect: a tab that never used Booley would otherwise show up.
- When a thread or process session's client reports `SessionEnd` after its
  last Booley call, the hook leaves an end marker; the row is hidden at once
  and registry maintenance (once a minute) deletes it. A later tool call
  shows the session again. Hooks write markers under thread or process keys
  only, so a `worktree:` fallback row never matches one and stays until
  `[goals].quiet_after`.
- Doctor's `goals.quiet-session` check reads registry rows but not end
  markers. Once maintenance deletes an ended row, Doctor can warn within
  about a minute of `SessionEnd` instead of after `quiet_after`; the warning
  already says it is not evidence of abandonment.

## Sessions list

- Columns are Session, Mode, Branch, Work dir, Started, and Activity. When
  the list is too narrow, it drops Started, then Work dir, then Branch
  (`DROP_ORDER` in `sessions.py`). Activity is never dropped: it keeps at
  least 40 cells until those three are gone, then takes the cut, because it
  shows which session needs the user. Values that still do not fit end in
  `…`; Work dir keeps its tail, because paths differ at the end.
- "Too narrow" means the list's own width, not the terminal's. Beside the
  preview the list gets half the terminal, so it drops columns as a narrow
  terminal would.

## Preview pane

- At 120 columns or more (`WIDE_COLUMNS` in `app.py`), the Sessions overview
  splits into the list and a preview of the highlighted row. Below 120, or
  when the overview has no rows, the preview is hidden and the list uses the
  full width. The Jobs window uses the same threshold to put the Job detail
  beside the Jobs list.
- A session's preview shows its handle and mode, activity with duration,
  branch, work dir, start age, connection, last Booley call with its age, the
  shared-worktree warning when present, a one-line Goal summary in Goal Mode,
  and its unfinished Jobs. Finished Jobs and the full Goal list are left to
  session detail. Absent values show `—`.
- A Standalone Jobs row previews the same content as Job detail.
- The preview follows the cursor and refreshes with each snapshot without
  moving the cursor or the list scroll.
- `Enter` opens session detail (or Job detail for a Standalone Job). The
  preview and session detail build their shared facts from one
  `SessionOrientation`, so they cannot disagree.
