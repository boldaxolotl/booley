# GUI on the Codespaces demo

Bug-hunt Booley's GUI surfaces in a browser Codespace on the published PicoRV32
demo: onboarding, the Dashboard, the B-Wave Waveform Viewer (VaporView), the
terminal, the Review Package preview, Live Preview and the docs site. The main
bug class is **the GUI disagrees with the CLI**: every GUI claim gets checked
against a CLI oracle, or the missing oracle is logged as a gap.

## Pins and prerequisites
- Booley: the candidate under test (set by the run skill; not pinned here), run
  as a candidate Sandbox Image in the Codespace.
- Project: https://github.com/boldaxolotl/booley-prj-picorv32 `main` at run
  start, plus one commit that repins the image (runbook step 3). Record the
  demo `main` SHA in `log.md`.
- Procedure: the shared [Codespaces GUI QA runbook](../../shared/codespaces/RUNBOOK.md).
  It owns setup, reading the terminal,
  interaction rules, oracles, evidence naming and cleanup; this mission names
  the steps and adds what to hunt. Read it before area 1.
- Host: a T3 session with the `preview_*` tools, the `default` browser profile
  signed into GitHub as the maintainer, and `gh` with the `codespace` scope.
  Deleting a candidate image this run dispatched also needs `read:packages` and
  `delete:packages`.
- Agent: the demo's own sign-in (START-HERE step 1); the Zbb Goal uses about
  half an hour of that allowance. Run at most one Goal at a time.
- Budget: 4 h: 230 area minutes plus 10 contingency, one 4-core Codespace
  (about 4 h of the Free plan's ~30 h). Areas are in priority order. Area 5
  needs area 3's Goal still running: if area 4 would carry past the Goal's
  end, run area 5 first and say so in `log.md`.

## Mission-specific rules
- **Allowed external actions.** This mission needs some writes that the run
  skill otherwise forbids: the runbook's `qa/<short>` branch on the demo repo;
  creating, stopping and deleting the Codespace; and, when no candidate image
  exists, one `docker-publish.yml` dispatch and later the deletion of its
  image versions. Add each to `resources.md` before creating it. Nothing else
  leaves the host: no issues, PR comments or evidence pushes.
- **Run skill adjustments.** The candidate runs in the Codespace, not on the
  host: skip the run skill's host-install check (its step 2) and record the
  candidate `<sha>` and image tag in the `log.md` identity header instead.
  Its step-5 Goal Record copy can't be done here (the Codespace has no sshd):
  save `booley goal status --long` output instead, and if a Goal finding needs
  the full record, keep the Codespace stopped under the runbook's live-repro
  exception rather than deleting it.
- **Runbook hard rules apply throughout**: no clipboard, no typing into Monaco,
  only your own tab, no changes to the maintainer's VS Code settings, no reload
  unless the area is about reload, and keep the maintainer's identity
  (`booley doctor -v`, `/etc/gitconfig`) out of evidence.
- **Default settings first.** Areas 1 and 2 run with the Codespace exactly as
  a new user gets it: runbook step 6 (reading mode) waits until area 2's
  screenshots are taken. Log the switch.
- **Every GUI claim gets an oracle.** Use the runbook step 8 table. Take the
  screenshot, run the oracle, bring the GUI back to the front, wait one
  Dashboard refresh (2 s), then compare. A surface with no oracle is checked
  against what you did, and the gap goes in `log.md` once.
- **Evidence.** One subdirectory per area under `evidence/`, named as runbook
  step 9 says (`<area>-<nn>-<what>.png` plus the matching `.txt` oracle
  output). Note the theme and viewport with each screenshot set. Look at every
  image before the run ends; delete any that shows a token, an email address,
  a device code or anything outside the Codespace tab.
- **Candidate image.** Delete the run's candidate image versions (runbook
  step 10, "Candidate image versions") only if this run dispatched the build.
  A reused build, and any image a release tag points at, stays.

## Areas

### 1. setup — Candidate Codespace and first contact (~30 min)
Intent: a new user gets from "Open in Codespaces" to a working Booley without help.
Try:
- Runbook steps 1-5 with `<sha>` = the candidate. In the first command
  terminal, run `touch /tmp/qa-start` (area 8 uses it). Record in `log.md`: click on
  "Create codespace", state `Available`, trust prompt, Dashboard visible,
  `booley --version` correct, first Flow passing.
- Follow `START-HERE.md` as a new user would: does it open on its own in the
  Markdown preview, render cleanly, and lead through sign-in (steps 1-2)?
  Finish `booley-demo-claim` only if area 3 is in scope (it spends demo
  allowance); otherwise stop at its "Type yes" notice with Ctrl+C. Restricted
  Mode and the trust prompt: is it clear why, and what to click?
- Doctor as the user sees it: the Dashboard `Doctor:` line and the output in
  the terminal. Run `booley doctor` (never `-v`) and compare; maximize the
  panel to read all of it (runbook step 7, "Long output"), then restore it.
  A layout change is not a setting, so it doesn't break the default-settings
  rule.
- Time to first sim: run `booley flow sim --target sim_core` in a command
  terminal and note the time from the create click to its pass (2026-11-06:
  the sim took 20 s, about 5.5 min from the click).
Look for: onboarding steps that need knowledge the page doesn't give, a wrong
Dashboard terminal in front (#2700), a Doctor line that disagrees with the
CLI, slow or silent waits with no progress shown.
Evidence: `setup-*` screenshots of the creation page, trust prompt,
START-HERE, Dashboard and version line; the timing table in `log.md`.

### 2. dashboard-default — Dashboard with default settings (~15 min)
Intent: the Dashboard reads correctly for a user who changed nothing.
Try:
- Screenshots only: the terminal is a canvas, so read it from the image
  (blurry at 1280×800; zoom into the saved file when text is unclear). Take
  the Sessions window, the Jobs window (`→`), and Help (`↑` into the top bar,
  `→` until Help is highlighted, `Enter`; `↓` scrolls). Take a Job detail if
  any Job exists; otherwise screenshot the empty Jobs window under Running and
  All (#2298, see Known traps).
- Compare each with its oracle from a command terminal, also read from a
  screenshot.
- Then apply runbook step 6, which also pins the theme to `Dark 2026` for the
  main run. Record the switch in `log.md` with the time, the keys set, and
  before/after screenshots of the same Dashboard window.
Look for: truncated or overlapping text, columns that don't fit the default
panel height, glyphs that don't render, a window that disagrees with the CLI.
Evidence: `dashboard-default-*` screenshots; the switch entry in `log.md`.

### 3. dashboard-live — Dashboard under real work (~60 min)
Intent: with a Goal and Flows running, every Dashboard view matches the CLI.
Try:
- In one terminal start `booley` and send the demo README's Goal prompt
  ("Create a new zbb worktree and use /booley-goal with the zbb Goalset to
  implement the design described in .booley/specs/zbb-design.md"). Confirm the
  Goals when it asks, and note the worktree and session handle.
- While the Goal runs, start two overlapping Flows from a second command
  terminal: `booley flow sim --target sim_core` and
  `booley flow synth --target synth_core`.
- Compare against the oracles: the Sessions overview and preview pane (which
  sessions, branch, work dir, last call; `git worktree list`), session detail,
  Goal rows and Goal detail (`booley goal status --long` in
  `.booley/worktrees/zbb`), the Jobs window and Job detail (Job records and
  `ps`). Check that a worktree session (ADR 0082) shows its own worktree and
  that NEEDS YOU and `Sessions N◆` appear when the agent asks you to confirm.
- MCP from the in-Codespace client: in a second chat session, ask for the
  project status, the Targets, and one Specialist review (an RTL bug
  review of `picorv32.v`; `booley specialist reviewer --help`); the Dashboard shows the session and
  its Jobs, and the answers match `booley targets` and the report on disk.
- Background-task activity: while the agent waits on a long Flow, the session
  shows "waiting for tool call", not idle or stuck.
- Session end and pruning: end a short second chat session (`/exit`) and
  check how and when it leaves the overview, against the docs.
- Keyboard: every key the footer and Help list, including `↑` into the top
  bar, filters, `Enter`/`Escape` depth, and `q` then `booley dashboard`.
- Resize: drag the panel smaller and larger, and `preview_resize` to 1024×768;
  the Dashboard redraws without garbage.
- Reload recovery, last: Developer: Reload Window, then the documented way
  back to the Dashboard. #2857 (the Dashboard orphaned after reload) is
  known; record what you saw, not a new finding, unless it differs.
- Dashboard launches (ADR 0084): only if `docs/adr/0084-*` exists in the
  candidate. Launch one Flow from the Dashboard and compare it with the same
  CLI run. Otherwise log the item as skipped.
Look for: Jobs missing or duplicated, stale status after a run ends, wrong
session attribution between the Goal and the shell, counts that disagree with
`booley goal status`, keys that do nothing or do something the footer doesn't
say.
Evidence: `dashboard-live-*` screenshot plus oracle pairs for each view; one
pair per mismatch.

### 4. bwave-gui — Scoped waveform views (~35 min)
Intent: what VaporView shows is what `bwave gui` asked for and what it reported back.
Try:
- Register a fresh trace from area 1's or 3's `sim_core` run. Run scoped views
  in a command terminal: `-s` globs, `-g NAME=GLOB` groups (repeat a NAME),
  `%b`/`%h`/`%d` radix, `@red`/`@blue`/`@green` colors, `--time START:END`,
  `--cursor`, `--append`, `--no-clock`.
- Compare three things for each: the request, the command's readback report,
  and the screenshot. Check a few values in the view with `bwave value` /
  `bwave signal` at the marker times.
- Loud failure: a glob that matches nothing, a `--time` start past the trace
  end, a `--cursor` outside the range, a missing trace. Each must fail with a
  message; none may silently show the whole trace.
- Large trace: the largest trace the demo produces (`sim_dhry`'s
  `testbench.vcd`; B-Wave converts it to FST when it registers it). Note the open time and whether the UI stays responsive.
- Two traces in two VaporView tabs: a scoped view goes to the right one.
- Reopen after a reload (Developer: Reload Window), then a scoped view again.
- The bundled VaporView VSIX is the one installed: compare
  `ls /usr/local/libexec/booley/*.vsix` with `code --list-extensions --show-versions`.
Look for: readback that differs from the screen, radix or color ignored,
markers in the wrong place, a silent fallback, a view sent to the wrong tab.
Evidence: `bwave-gui-*` screenshots, each with the command and its report as
`.txt`.

### 5. stop-resume — Stop and resume the Codespace mid-Goal (~15 min)
Intent: Booley state survives a Codespace stop, and the GUI comes back to it.
Depends on: area 3's Goal still running (see the budget note).
Try:
- Note `booley goal status --long`, the Job records and the Dashboard. Stop
  the Codespace with `gh codespace stop --codespace <name>` and time it.
- Reopen it in your tab (`https://<name>.github.dev`), trust again if asked,
  and time the way back to the Dashboard.
- After resume: the Dashboard reattaches by itself; finished Jobs and their
  results are still listed; the Goal shows its true state. Resume the Goal the
  documented way (start `booley`, `/booley-goal start`) and check that it picks
  up where it stopped.
Look for: Jobs reported running forever, lost results, a Goal that can't
resume, a Dashboard that needs manual restarting.
Evidence: `stop-resume-*` before/after pairs with oracle output.

### 6. terminal-output — CLI rendering in the browser terminal (~15 min)
Intent: Booley's CLI output reads as well in the browser as in a desktop terminal.
Try:
- Glyphs and colors in `booley doctor`, `booley targets` and `booley help`;
  progress output of a running Flow; wrapping at a narrow panel.
- `file:line` paths in Flow and lint output: hover shows a link, and opening
  it lands on that line.
- Device-code sign-in: run `codex login --device-auth` and check that the
  URL is a link and the code is legible. Stop with Ctrl+C before signing in.
  Keep the code out of saved evidence. (`booley auth` has no device-code flow;
  for Claude it asks for a pasted token, which the no-clipboard rule rules out.)
Look for: mojibake, colors unreadable on the theme, links that open the wrong
file or line, progress that floods the scrollback.
Evidence: `terminal-output-*` screenshots.

### 7. goal-finish — Review Package in the editor (~15 min)
Intent: a user can read the finished Goal's Review Package inside the Codespace.
Depends on: area 3's Goal finishing; if it didn't, use the last state and say so.
Try:
- Open the Review Package path that Finish printed (also in
  `booley goal status`) in the Markdown preview. Check headings, tables,
  diffs, and that every relative link and image resolves.
- Compare its Goal list and verdicts with `booley goal status --long`.
Look for: broken links, raw Markdown where a table should be, a package that
disagrees with the CLI.
Evidence: `goal-finish-*` screenshots and the status output.

### 8. live-preview — HTML reports in Live Preview (~10 min)
Intent: HTML reports open through the bundled Live Preview extension.
Try: find the HTML reports this run produced
(`find /workspaces/picorv32 -name '*.html' -newer /tmp/qa-start`), including
any `explanation.html` from area 7, and open each with Live Preview. Check the
preview loads and its links work.
Look for: a blank preview (dead port tunnel), missing styles, broken links.
Evidence: `live-preview-*` screenshots.

### 9. matrix — Viewport and theme matrix (~15 min)
Intent: the main views still work at other sizes and themes.
Try: the main run is 1440×900, dark. Do a short pass over the Dashboard
Sessions window and one scoped VaporView view at 1024 wide, then `Light 2026`,
then `Dark High Contrast` and `Light High Contrast`. Change the theme only
through machine settings (the runbook step 6 one-liner with another
`workbench.colorTheme` value), never the theme picker.
Look for: status shown by color alone (it never may be), text that vanishes
on a theme, layout that breaks at 1024.
Evidence: `matrix-*` screenshots named by size and theme.

### 10. docs-site — Docs site and demo links (~10 min)
Intent: the public docs render and the demo's links lead somewhere real.
Try: open https://booley.dev/ in your tab. Check the pages the demo README
and START-HERE link to, their rendering, code blocks and images, and every
link from those two files.
Look for: dead links, a docs page that contradicts what the Codespace did.
Evidence: `docs-site-*` screenshots and the list of links checked.

### 11. cleanup — Runbook cleanup (~10 min)
Try: runbook step 10. Delete the candidate image versions only under the
rule above. Then release every `resources.md` row.
Look for: anything the runbook's checks still list, a Codespace that won't
delete.

## Known traps
- Shell `booley flow` runs write no Job records yet (#2298), so they don't
  appear in the Dashboard Jobs window. With a candidate that predates #2298,
  area 3's shell Flows are visible only as a busy Job slot; log it once.
- Area 3's Goal runs about half an hour. Start it as soon as area 3 begins,
  and do the keyboard and resize items while it works.
- The demo's own `zbb` Goalset is the one to use here, unlike the picorv32
  mission, which brings its own.
- A Codespace opens light or dark at random across runs, and a reload can flip
  it; runbook step 6 pins it, so areas 1-2 note the theme they got.
