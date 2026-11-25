# GUI in T3 Code

Bug-hunt Booley's GUI as a user sees it: T3 Code in a host browser, paired
through `booley session up`, on the picorv32 demo Project with a candidate
build. It covers first contact and pairing, the web Dashboard and its Run
form, Developer Agents in T3 Code threads, Sandbox stop and start, the
Measurement Report and the docs site. The main bug class is **the GUI
disagrees with the CLI**: every GUI claim gets checked against a CLI oracle,
or the missing oracle is logged as a gap. The `booley-qa-run` skill runs this
mission; to check one PR's GUI change, use `booley-gui-check` instead.

## Pins and prerequisites
- Booley: the candidate under test (set by the run skill; not pinned here),
  installed on the host and built into a local Sandbox Image (runbook step 2).
- Project: upstream picorv32 at the
  [picorv32 mission](../picorv32/MISSION.md)'s pin, with
  https://github.com/boldaxolotl/booley-prj-picorv32 `main` at run start as
  its Booley Directory (runbook step 3). Record the demo `main` SHA in
  `log.md`.
- Procedure: the shared [T3 Code GUI QA runbook](../../shared/t3/RUNBOOK.md).
  It owns setup, pairing, interaction rules, oracles, evidence naming and
  cleanup; this mission names the steps and adds what to hunt. Read it before
  area 1.
- Host: a T3 session with the `preview_*` tools on the host that runs the
  Sandbox, and the maintainer's approval to install the candidate as the
  canonical host install (runbook step 1).
- Agent: the demo's default Agent CLI (Codex) signed in on the host
  (`booley auth --status`), and Claude too for area 3's second thread. Area
  3's Zbb request runs about half an hour of agent time and area 4's reviewer
  a few minutes. Run at most one such request at a time.
- Budget: 4 h: 230 area minutes plus 10 contingency, not counting the
  candidate build (runbook step 2), which the run logs separately. Areas are
  in priority order. Areas 4 (its warnings item) and 6 need area 3's child or
  its measurement still running, and that work (about 30 min) usually ends
  inside area 3's hour: once it has run about 10 min and the Flow comparisons
  are done, do area 6 and area 4's warnings item, then finish area 3. Log the
  order.

## Mission-specific rules
- **Developer Agents run only in T3 Code threads** (runbook Hard rules). Apply
  the shared [Developer Agent child rules](../../shared/DEVELOPER-CHILD.md)
  for area 3's child.
- **Allowed external actions.** None beyond the run skill's: the candidate
  build, the Project clone and the Sandbox stay on the host, and the
  `qa/<short>` branch stays local. No issues, PR comments or evidence pushes.
- **Run skill adjustments.** Runbook step 2 replaces the run skill's
  host-install check (its step 2); record the candidate `<sha>` and the
  canonical revision it replaced in the `log.md` identity header.
- **Runbook hard rules apply throughout**: the pairing link is a secret, no
  clipboard, only your own tab, Developer Agents only in threads, and keep the
  maintainer's identity (Doctor's `sandbox.gitconfig-identity` line) out of
  evidence.
- **Every GUI claim gets an oracle.** Use the runbook step 8 table. Take the
  screenshot, run the oracle, look at the GUI again after one refresh (the
  Dashboard refreshes every 3 s), then compare. A surface with no oracle is
  checked against what you did, and the gap goes in `log.md` once.
- **Evidence.** One subdirectory per area under `evidence/`, named as runbook
  step 9 says (`<area>-<nn>-<what>.png` plus the matching `.txt` oracle
  output). Note the theme and viewport with each screenshot set. Look at every
  image before the run ends; delete any that shows a pairing link or token, an
  email address or anything outside your tab's page.
- **Not yet verified.** The runbook's "Not yet verified" list is part of area
  1: record each answer in `log.md`, and a wrong runbook statement is a
  finding against the runbook.

## Areas

### 1. setup — Candidate Sandbox and first contact (~35 min)
Intent: a new user gets from `booley session up` to the Project in a browser
without help.
Try:
- Runbook steps 2-4. Record in `log.md` the image build times, the time from
  `session up` to `Sandbox ready`, and from opening the link to the Project
  shown.
- Follow `docs/user/SANDBOX.md` as a new user would: do its `session up`,
  `session pair` and `session status` lines match what happened? Does
  `session status` report T3 Code and the relay?
- Pairing faults: open a used link in a new tab of your own, and a link older
  than 5 minutes; each gives a clear error, and a fresh `session pair` works.
  Close those extra tabs.
- Doctor as the user sees it: run `booley doctor 2>&1 | grep -v
  gitconfig-identity` in a thread's terminal drawer and compare it with the
  same command from the host through `booley session enter`.
Look for: a link that pairs twice or not at all, a token left in the address
bar, "No project" after pairing, a `session status` that disagrees with what
runs, slow or silent waits with no progress shown.
Evidence: `setup-*` screenshots of the paired Project, each pairing error and
the Doctor output; the timing table in `log.md`.

### 2. dashboard-default — Dashboard with no work (~15 min)
Intent: the Dashboard reads correctly before anything has run.
Try:
- Runbook step 5's Dashboard for the main checkout (`/work`). Take the page at
  the default size: empty Jobs list, resource figures, any notice.
- Compare with the snapshot JSON and the Job records (runbook step 8).
- Open it for a worktree path that does not exist, and for a relative path;
  each gets a plain error and no Jobs.
Look for: truncated or overlapping text, an empty state that doesn't say what
to do, figures that disagree with the JSON, an error page with no message.
Evidence: `dashboard-default-*` screenshots with the JSON beside them.

### 3. dashboard-live — Dashboard under real work (~60 min)
Intent: with a child, a measurement and Flows running, every view matches the
CLI.
Try:
- From the host, write
  [`../picorv32/fixtures/metrics.toml`](../picorv32/fixtures/metrics.toml)
  into `.booley/metrics.toml` and commit it in the Booley Directory's
  repository.
- Make a `zbb` worktree and start a Codex thread on it (DEVELOPER-CHILD), then
  send "Implement the design described in .booley/specs/zbb-design.md. Then
  give me a Measured Commit." Approve a `metrics.toml` change when asked.
- While the child works, start two overlapping Flows from the host:
  `booley session enter -- booley flow sim --target sim_core` and
  `booley session enter -- booley flow synth --target synth_core`.
- Compare against the oracles: the `zbb` worktree's Dashboard shows the
  child's Jobs with the thread as owner and nothing from the main checkout;
  the main checkout's Dashboard shows the two Standalone Jobs owned by `you`;
  Job detail and the `measure` Job's report match the records and the stored
  run.
- MCP from a second thread, Claude, on the main checkout: ask for the project
  status, the Targets, and one Specialist review (an RTL bug review of
  `picorv32.v`); the answers match `booley targets` and the report on disk,
  and its Jobs show in the main checkout's Dashboard with that thread as
  owner.
- Rename the second thread in T3 Code; its Jobs' owner follows.
- Resize: `preview_resize` to 1024×768 and back; the Dashboard and the thread
  redraw cleanly.
Look for: Jobs missing, duplicated or in the wrong worktree's Dashboard,
stale status after a run ends, the wrong owner, a `measure` Job or report
that disagrees with the stored run.
Evidence: `dashboard-live-*` screenshot plus oracle pairs for each view; one
pair per mismatch.

### 4. run-wizard — Launch from the Dashboard's Run form (~35 min)
Intent: every launch from the Run form (ADR 0086) starts the same Standalone
Job as the shell command its review screen shows.
Try:
- Flow: open Run in the main checkout's Dashboard, pick a built-in Flow and a
  Target (`sim` on `sim_core`). Check the Target and Test Run choices against
  `booley targets`. On the review screen, compare the shown command with the
  form, the queue line with the Flow, and the timeout line with the Flow's
  settings. Submit, then run the shown command through
  `booley session enter` and compare the two Jobs (runbook step 8).
- Warnings: open Run in the `zbb` worktree's Dashboard while area 3's child
  works there and a Flow runs, with one uncommitted file in the worktree. The
  review screen warns about the thread, the running Job and the change, and
  still lets you submit. Back out from each stage; nothing starts.
- Reviewer Specialist: launch `reviewer` on `picorv32.v`. The review screen
  shows the model and Agent CLI, says it uses model tokens on that CLI's
  credential with its `auth` setting, and gives the per-call timeout. Check
  each line against `booley specialist reviewer --help` and the `[agent]`
  settings. Submit once; compare the finished report with the CLI's.
- Mutation Flow: launch `mutation` on `sim_core` with a small `--count` (5).
  The form offers only Targets the sim Flow can run. Compare the Job and its
  report with the same `booley flow mutation` command.
- `coverage_analyst`: with no Coverage Record the form says how to make one;
  check that text. Run `booley flow sim --coverage --target sim_core`, then
  launch `coverage_analyst` on the new record. If the demo's simulator can't
  record coverage, log the refusal and skip the launch.
- Copy command: don't press it (runbook Hard rules). Check that it sits beside
  Submit; log the copy itself as a standing gap.
Look for: a shown command that differs from what ran, a Job that starts with
different options or a different worktree, a missing or wrong warning, a model
or `auth` line that disagrees with the settings, a Submit that starts twice, a
back-out that leaves a Job behind.
Evidence: `run-wizard-*` review-screen screenshots, each with its Job record
and the CLI run's record.

### 5. bwave-gui — `bwave gui` without a viewer (~5 min)
Intent: `bwave gui` still checks its request, then says plainly that Booley
has no waveform viewer.
Try: in a thread's terminal drawer, with a trace registered from area 3's
`sim_core` run, run `bwave gui` with a valid `-s` glob, with no selection, and
with `-g NAME=GLOB`. Then run it with a missing trace, `-s` globs that all
match nothing, a `--time` start past the trace end and a `--cursor` past the
trace end.
Look for: a valid request that opens something or exits 0, a failing request
that reports the no-viewer message instead of its own error, a selector error
that is silent or unclear.
Evidence: `bwave-gui-*` `.txt` files with each command, its report and exit
code.

### 6. stop-resume — Stop and start the Sandbox mid-measurement (~20 min)
Intent: Booley state survives `booley session down` and `up`, and the GUI
comes back to it.
Depends on: area 3's child or measurement still running; run it inside area 3
(see the budget note).
Try:
- Note the Job records, both Dashboards and the `measure` Job. Run
  `booley session down` from the host and time it.
- `booley session up`, pair again (runbook steps 3-4), and time the way back
  to the Project. Restart the Dashboard (runbook step 5).
- After the restart: the relay keeps its port; threads, their worktrees and
  uncommitted edits are still there (`git worktree list`); finished Jobs and
  their results are still listed; an interrupted measurement is never shown
  Measured. Open the child's thread and ask it to continue; measuring again
  needs no `--again`.
Look for: lost threads or worktrees, Jobs reported running forever, lost
results, a cancelled run shown Measured, a relay that moved for no reason.
Evidence: `stop-resume-*` before/after pairs with oracle output.

### 7. terminal-output — CLI rendering in T3 Code's terminal drawer (~10 min)
Intent: Booley's CLI output reads as well in the browser as in a desktop
terminal.
Try:
- Glyphs and colors in `booley doctor` (its Summary only; runbook Hard
  rules), `booley targets` and `booley help`; progress output of a running
  Flow; wrapping in a narrow drawer.
- `file:line` paths in Flow and lint output: are they links, and where do
  they lead?
Look for: mojibake, colors unreadable on the theme, links that lead nowhere,
progress that floods the scrollback.
Evidence: `terminal-output-*` screenshots.

### 8. measure-report — Measurement Report from the Dashboard (~15 min)
Intent: a user can read a Measurement Report from the Dashboard.
Depends on: area 3's measurement; if none finished, ask the child's thread to
measure its commit.
Try:
- Open the report from the `measure` Job's report link. Check the verdict line,
  the header, failing rows first, the metric table, the Δ column when a base
  run exists, red and amber as words, and that every link resolves.
- Compare it with the stored run document (runbook step 8).
Look for: broken links, raw Markdown where a table should be, a report that
disagrees with the stored run.
Evidence: `measure-report-*` screenshots and the run document.

### 9. matrix — Viewport and appearance matrix (~15 min)
Intent: the main views still work at other sizes and color schemes.
Try: the main run is 1440×900 in the tab's default scheme. Do a short pass
over the paired Project, one thread and the main checkout's Dashboard at
1024×768, then with `preview_set_appearance` dark and light. Change the
scheme only that way, never in the host's T3 Code.
Look for: status shown by color alone (it never may be), text that vanishes in
a scheme, layout that breaks at 1024.
Evidence: `matrix-*` screenshots named by size and scheme.

### 10. docs-site — Docs site and demo links (~10 min)
Intent: the public docs render and the demo's links lead somewhere real.
Try: open https://booley.dev/ in your tab. Check the pages the demo README
links to, the Sandbox and T3 Code pages, their rendering, code blocks and
images, and every link from the demo README.
Look for: dead links, a docs page that contradicts what the run did.
Evidence: `docs-site-*` screenshots and the list of links checked.

### 11. cleanup — Runbook cleanup (~10 min)
Try: runbook step 10. Then release every `resources.md` row.
Look for: anything still running after `session down`, a canonical install
that doesn't come back.

## Known traps
- Area 3's request runs about half an hour. Start it as soon as area 3
  begins, and do the resize and MCP items while it works.
- Until #4749 the Dashboard needs a manual `booley dashboard --web`, and it
  stops with the Sandbox: restart it after area 6.
- Until #4566 lands, threads, pairings or worktrees lost across
  `session down` and `up` are known; record what you saw, not a new finding,
  unless it differs from that issue.
- The demo's README still starts agents with the retired chat command
  instead of T3 Code; that is a demo finding, logged once.
