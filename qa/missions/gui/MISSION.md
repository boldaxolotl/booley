# GUI on the Codespaces demo

Bug-hunt Booley's GUI surfaces in a browser Codespace on the published PicoRV32
demo: onboarding, the Dashboard, the terminal, the Measurement Report, Live
Preview and the docs site. The main
bug class is **the GUI disagrees with the CLI**: every GUI claim gets checked
against a CLI oracle, or the missing oracle is logged as a gap. The
`booley-qa-run` skill runs this mission; to check one PR's GUI change, use
`booley-gui-check` instead.

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
- Agent: the demo's own sign-in (START-HERE step 1); area 3's Zbb request uses
  about half an hour of that allowance and its reviewer run a few
  minutes. Run at most one such request at a time. Each claim uses one of the
  GitHub account's 3 active demo tokens, and deleting the Codespace loses it,
  so repeated runs can hit the limit. When `claim.sh` refuses, don't search for
  or copy a token: ask the maintainer to sign in with their own account in the
  Codespace (`codex login --device-auth`), then run
  `python -m booley.harness.incontainer_register` to switch the agent to that
  account, and confirm that `booley` starts and answers without
  `-c model_provider=openai`. Do the same after any re-sign-in. Record in
  `log.md` that the run did not use the demo token, and go on; it is not a
  finding.
- Budget: 3 h 30 min: 200 area minutes plus 10 contingency, one 4-core
  Codespace (about 3.5 h of the Free plan's ~30 h). Areas are in priority
  order. Area 5 needs area 3's child or its measurement still running, and
  that work (about 30 min) usually ends inside area 3's hour: once it has run
  about 10 min and the Flow comparisons are done, do area 5, then finish
  area 3. Log the order.

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
  Its step-5 copy of the Main Booley Directory's `measurements/` can't be done
  here (the Codespace has no sshd): save the stored run documents and reports
  through the terminal instead, and if a measurement finding needs the whole
  store, keep the Codespace stopped under the runbook's live-repro
  exception rather than deleting it.
- **Runbook hard rules apply throughout**: no clipboard, no typing into Monaco,
  only your own tab, no changes to the maintainer's VS Code settings, no reload
  unless the area is about reload, and keep the maintainer's identity
  (`/etc/gitconfig`, which plain `booley doctor` also prints on its
  `sandbox.gitconfig-identity` line) out of evidence.
- **Default settings first.** Areas 1 and 2 run with the Codespace exactly as
  a new user gets it: runbook step 6 (reading mode) waits until area 2's
  screenshots are taken. Log the switch.
- **Every GUI claim gets an oracle.** Use the runbook step 8 table. Take the
  screenshot, run the oracle, bring the GUI back to the front, wait one
  Dashboard refresh (3 s), then compare. A surface with no oracle is checked
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
- Runbook steps 1-5 with `<sha>` = the candidate. Record in `log.md`: click on
  "Create codespace", state `Available`, trust prompt, Dashboard page open,
  `booley --version` correct, first Flow passing.
- Follow `START-HERE.md` as a new user would: does it open on its own, render
  cleanly in the Markdown preview, and lead through sign-in (steps 1-2)?
  Finish `booley-demo-claim` only if area 3 is in scope (it spends demo
  allowance); otherwise stop at its "Type yes" notice with Ctrl+C. Restricted
  Mode and the trust prompt: is it clear why, and what to click?
- Doctor as the user sees it: run
  `booley doctor 2>&1 | grep -v gitconfig-identity` (the filter keeps the
  maintainer's identity off the screen; runbook Hard rules); maximize the
  panel to read all of it (runbook step 7, "Long output"), then restore it.
  A layout change is not a setting, so it doesn't break the default-settings
  rule.
- Time to first sim: run `booley flow sim --target sim_core` in a command
  terminal and note the time from the create click to its pass (reference
  from the 2026-11-06 run: the sim took 20 s, about 5.5 min from the click).
Look for: onboarding steps that need knowledge the page doesn't give, a Doctor
finding the user can't act on, slow or silent waits with no progress shown.
Evidence: `setup-*` screenshots of the creation page, trust prompt,
START-HERE, Dashboard page and version line; the timing table in `log.md`.

### 2. dashboard-default — Dashboard with default settings (~15 min)
Intent: the Dashboard reads correctly for a user who changed nothing.
Try:
- Screenshot the page of `/workspaces/picorv32` that runbook step 5 opened: the
  header, Now, and Results, collapsed and then opened. Open a report link.
- Compare each Job with its oracles from a command terminal (runbook step 8):
  the Job records and `booley_status`.
- Open the page with no `?worktree=`, then with a path that is no worktree:
  each says what is wrong.
- Then apply runbook step 6, which also pins the theme to `Dark 2026` for the
  main run. Record the switch in `log.md` with the time, the keys set, and
  before/after screenshots of the same Dashboard page.
Look for: truncated or overlapping text, cards that don't fit the width, glyphs
that don't render, a card that disagrees with the CLI.
Evidence: `dashboard-default-*` screenshots; the switch entry in `log.md`.

### 3. dashboard-live — Dashboard under real work (~60 min)
Intent: with a child, a measurement and Flows running, each worktree's
Dashboard page matches the CLI.
Try:
- From a command terminal, write
  [`../picorv32/fixtures/metrics.toml`](../picorv32/fixtures/metrics.toml) into
  `.booley/metrics.toml` and commit it in the Booley Directory's repository.
- In a T3 Code thread start a Developer Agent (Claude or Codex) and send
  "Create a new zbb worktree and implement the design described in
  .booley/specs/zbb-design.md. Then give me a Measured Commit." Approve a
  `metrics.toml` change when asked, and note the worktree and session handle.
- While the child works, start two overlapping Flows from a second command
  terminal: `booley flow sim --target sim_core` and
  `booley flow synth --target synth_core`.
- Compare against the oracles (runbook step 8) on the main checkout's page and
  on the child worktree's page (`?worktree=` its path; `git worktree list`):
  each page lists only its worktree's Jobs, with each Job's handle, owner (the
  shell Flows are Standalone Jobs, owned by `you`), state, stage, queue
  position, result and report link, and the header's resources and Job
  admission.
- When the agent reports a Measured Commit, run `booley measure` in its
  worktree: it prints the stored run, which must match the agent's report.
- MCP from the in-Codespace client: in a second chat session, ask for the
  project status, the Targets, and one Specialist review (an RTL bug review of
  `picorv32.v`; `booley specialist reviewer --help`); its worktree's page shows
  the review Job with that thread as owner, and the answers match
  `booley targets` and the report on disk.
- Page: close and reopen Results, reload the page, and stop the server with
  Ctrl+C, then start `booley dashboard` again. The page says the server does
  not answer, then recovers.
- Resize: `preview_resize` to 1024×768; the page stays readable.
- Reload recovery, last: Developer: Reload Window, then check whether the page
  still answers and, if not, whether `booley dashboard` brings it back.
Look for: Jobs missing or duplicated, stale status after a run ends, a Job on
the wrong worktree's page, a wrong owner, a result that disagrees with its
report.
Evidence: `dashboard-live-*` screenshot plus oracle pairs for each page; one
pair per mismatch.

### 4. bwave-gui — `bwave gui` without a viewer (~5 min)
Intent: `bwave gui` still checks its request, then says plainly that Booley
has no waveform viewer.
Try: in a command terminal, with a trace registered from area 1's or 3's
`sim_core` run, run `bwave gui` with a valid `-s` glob, with no selection, and
with `-g NAME=GLOB`. Then run it with a missing trace, `-s` globs that all
match nothing, a `--time` start past the trace end and a `--cursor` past the
trace end.
Look for: a valid request that opens something or exits 0, a failing request
that reports the no-viewer message instead of its own error, a selector error
that is silent or unclear.
Evidence: `bwave-gui-*` `.txt` files with each command, its report and exit
code.

### 5. stop-resume — Stop and resume the Codespace mid-measurement (~15 min)
Intent: Booley state survives a Codespace stop, and the GUI comes back to it.
Depends on: area 3's child or measurement still running; run it inside area 3
(see the budget note).
Try:
- Note the Job records, the Dashboard pages and the stored measurement runs.
  Stop the Codespace with `gh codespace stop --codespace <name>` and time it.
- Reopen it in your tab (`https://<name>.github.dev`), trust again if asked,
  and time the way back to the Dashboard.
- After resume: `booley dashboard` serves the pages again; finished Jobs and
  their results are still listed; an interrupted measurement has no stored run.
  Resume the agent's T3 Code thread and ask it to continue; measuring again
  needs no `--again`.
Look for: Jobs reported running forever, lost results, a cancelled run stored
as Measured.
Evidence: `stop-resume-*` before/after pairs with oracle output.

### 6. terminal-output — CLI rendering in the browser terminal (~15 min)
Intent: Booley's CLI output reads as well in the browser as in a desktop terminal.
Try:
- Glyphs and colors in `booley doctor` (its Summary only; runbook Hard
  rules), `booley targets` and `booley help`;
  progress output of a running Flow; wrapping at a narrow panel.
- `file:line` paths in Flow and lint output: hover shows a link, and opening
  it lands on that line.
- Device-code sign-in: run
  `CODEX_HOME=$(mktemp -d) codex login --device-auth` and check that the URL
  is a link and the code is legible. Stop with Ctrl+C before signing in, then
  check that `codex login status` still reports the real sign-in: a
  device-code login in the real `CODEX_HOME` deletes it as it starts. Keep
  the code out of saved evidence. (`booley auth` is host-only, so it can't
  run in the Codespace.)
Look for: mojibake, colors unreadable on the theme, links that open the wrong
file or line, progress that floods the scrollback.
Evidence: `terminal-output-*` screenshots.

### 7. measure-report — Measurement Report in the editor (~15 min)
Intent: a user can read a Measurement Report inside the Codespace.
Depends on: area 3's measurement; if none finished, run `booley measure` in
that worktree.
Try:
- Run `booley measure` on the Measured Commit and open the Markdown report it
  names in the editor. In the Markdown preview check the verdict line, the
  header, failing rows first, the metric table, the Δ column when a base run
  exists, red and amber as words, and that every link resolves.
- Compare it with the stored run document (runbook step 8).
Look for: broken links, raw Markdown where a table should be, a report that
disagrees with the stored run.
Evidence: `measure-report-*` screenshots and the run document.

### 8. live-preview — HTML pages in Live Preview (~10 min)
Intent: an HTML page in the Codespace opens through the bundled Live Preview
extension.
Try:
- No Booley Flow writes HTML, so write a fixture from a command terminal: a
  small `index.html` under `/workspaces/picorv32/qa-a8/` with a stylesheet, an
  image, a relative link to a second page and an anchor link to a heading
  further down. Delete the directory when the area is done.
- Open `index.html`, then F1 "Live Preview: Show Preview (Internal Browser)".
  Check that the styles and the image load, the relative link opens the
  second page, and the anchor link scrolls.
- A blank preview: load the same page top-level ("Live Preview: Show Preview
  (External Browser)", or the forwarded-port URL in a tab of your own; runbook
  step 7, "Forwarded ports") and record whether it loads. Only a page that
  stays blank there, with Codespaces port auth ruled out, is a Booley finding.
Look for: a blank preview that stays blank top-level (dead port tunnel),
missing styles or image, broken links.
Evidence: `live-preview-*` screenshots.

### 9. matrix — Viewport and theme matrix (~15 min)
Intent: the main views still work at other sizes and themes.
Try: the main run is 1440×900, dark. Do a short pass over the Dashboard page at
1024 wide, then over the terminal and a Measurement Report in `Light 2026`,
then `Default High Contrast` and `Default High Contrast Light`. Change the
theme only through machine settings (the runbook step 6 one-liner with another
`workbench.colorTheme` ID), never the theme picker, and check that the
workbench className changed each time.
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
- A private forwarded port can stay blank in the preview tab; that is not a
  finding until the page also fails top-level (runbook step 7, "Forwarded
  ports").
- Area 3's request runs about half an hour. Start it as soon as area 3 begins,
  and do the keyboard and resize items while it works.
- The demo's README still describes a removed feature in its main event and
  ships its records, so Doctor warns `goals.leftover`; both are known, not
  findings. Use area 3's request instead.
- A Codespace opens light or dark at random across runs, and a reload can flip
  it; runbook step 6 pins it, so areas 1-2 note the theme they got.
