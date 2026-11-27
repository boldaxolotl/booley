# T3 Code GUI QA runbook

This runbook takes a QA agent from a Booley commit `<sha>` to the picorv32
demo Project open in T3 Code, Booley's user interface, in a browser tab on
this host. It then checks the GUI against the CLI and cleans up. The `gui`
mission ([MISSION.md](../../missions/gui/MISSION.md)) and the
`booley-gui-check` skill ([SKILL.md](../../booley-gui-check/SKILL.md)) follow
it and cite its step numbers; they say what to check, this file says how.

"The GUI" here is two web surfaces: the T3 Code web UI that the Sandbox
serves (Project, threads, worktrees) and Booley's web Dashboard (one page per
worktree with its Jobs and the Run form), which T3 Code shows in its preview
panel.

Steps 1-5 are setup: if one fails, stop and report, because nothing after it
is valid. Steps 6-9 are rules and references, and step 10 cleans up.

| Name | Value |
|---|---|
| `<sha>` | The Booley commit under test (full) |
| `<short>` | The first 9 characters of `<sha>`, as `booley --version` prints them |
| `<run-dir>` | The run directory the mission or skill created |
| `$QA_PROJECT` | `<artifact-root>/gui-project/picorv32`: one fixed path per host, so the Project keeps its relay port across runs |
| `<port>` | The T3 Code relay port that `booley session up` prints |

## Hard rules

- **Developer Agents run in T3 Code threads.** In a GUI run, start every
  agent from the T3 Code UI in your tab: the threads are part of the GUI
  under test. A bare `claude` or `codex` session bypasses that GUI, so it is
  never GUI evidence.
- **The pairing link is a secret.** It carries a one-time token. Write
  `booley session up` and `booley session pair` output only to a file created
  with `umask 077` under `<run-dir>/scratch/`, never print the link in a
  shell command's output, and keep it out of `log.md`, evidence, comments and
  screenshots. Use each link once, at once, then delete its file. The
  `preview_open` call that uses it lands in the thread transcript; the
  one-time use and the 5-minute expiry are what make that safe.
- **Never use the clipboard.** No clipboard read or write, no paste, no
  Ctrl+C/Ctrl+V and no "Copy" button: it is the maintainer's real system
  clipboard. Ctrl+C in a terminal with no selection, to interrupt a running
  process, is allowed.
- **Use only your own browser tab.** Tabs belong to the T3 thread you run in,
  and other agents may share it. Open yours with `preview_open` and
  `reuseExistingTab: false`, pass its `tabId` to every `preview_*` call, and
  close only that tab.
- **Leave the host's T3 Code alone.** Change settings only in the Sandbox's
  T3 Code in your tab; it belongs to this run. Never use the host tools that
  change your own T3 Code (`t3_theme_set`, `t3_environment_preferences_update`
  and the like).
- **One GUI run per host at a time.** Two runs would share `$QA_PROJECT`, its
  relay port and the browser profile's session for `127.0.0.1`.
- **Keep the maintainer's identity out of evidence.** Before a screenshot or
  DOM read of Doctor output, filter its identity line
  (`booley doctor 2>&1 | grep -v gitconfig-identity`) or capture only the
  Summary.

## 1. Preconditions

- **Same host.** A T3 session with the `mcp__t3-code__preview_*` tools on the
  host whose Docker runs the Sandbox. The relay listens on host `127.0.0.1`
  only, so a browser on another machine can't reach it.
- **Canonical host install.** Host commands such as `booley init` and
  `booley session up` refuse any Booley that is not the canonical host
  installation. Installing the candidate as the canonical one (step 2)
  rebuilds the shared `booley-sandbox` base image and replaces the shared
  `booley-proxy` and `booley-reaper` containers, so every other Booley Sandbox
  on the host is affected. Do step 2 only with the maintainer's approval and
  while no other Booley run uses the host. Without both, the run is blocked:
  `blocked: host install`.
- **Agent sign-in.** For areas that run a Developer Agent,
  `booley auth --status` shows the Project's Agent CLIs signed in.
- **Disk.** Follow [`qa/DISK.md`](../../DISK.md). The local Sandbox Image
  build needs room for a base image and the RISC-V Project image.
- **No leftovers.** `$QA_PROJECT` is absent or empty, and no Sandbox of it
  runs (`booley session status` there reports it absent). Otherwise ask the
  maintainer before removing anything: it may be another run's live repro.

## 2. Candidate build

Record the canonical host install's revision first
(`booley --version`) and the tool that installed it: cleanup restores it.

1. Make a detached worktree of `<sha>` outside `.worktrees` and the run
   directory, and build a development wheel from it, as "Building a
   development wheel" in
   [CONTRIBUTING.md](../../../docs/internals/CONTRIBUTING.md) says.
2. Install the wheel over the canonical host install, with the tool that
   installed it, and update Host Bootstrap:

   ```sh
   pipx install --force <wheel>   # or: uv tool install --force --reinstall <wheel>
   booley bootstrap --update
   ```

   Bootstrap builds the base Sandbox Image locally: about 20 minutes on the
   first run of this runbook, an hour or more with a changed base recipe.
   Note the time.

Worked when `booley --version` reads `booley <version> (<short>, <date>)` and
`booley bootstrap --check-only` reports no pending change. A build that fails
or overruns the caller's budget is `blocked: image build`.

## 3. Project

Set the Project up as the demo README's Quick start says: upstream picorv32
at the [picorv32 mission](../../missions/picorv32/MISSION.md)'s pin, with the
demo cloned at `main` as its Stealth Booley Directory. Record the demo `main`
SHA in `log.md`.

```sh
git clone https://github.com/YosysHQ/picorv32 $QA_PROJECT
git -C $QA_PROJECT checkout --detach a473fc8fca393771d83b0ffcf0b14db3393339d8
git clone https://github.com/boldaxolotl/booley-prj-picorv32 $QA_PROJECT/.booley
```

Turn on the Run form's Feature Flag for this clone only: add
`enable = ["web-run-panel"]` under `[features]` in `.booley/booley.toml`, and
commit it with `git -C $QA_PROJECT/.booley` on a local branch `qa/<short>`
(never push it). Then:

```sh
cd $QA_PROJECT && booley init
(umask 077; booley session up > <run-dir>/scratch/session-up.txt 2>&1)
grep -v 'pair a browser' <run-dir>/scratch/session-up.txt
```

`booley init` builds the Project's Sandbox Image (about 7 minutes on the
first run); note its time.

Worked when the filtered output shows `Sandbox ready: <name>` and
`T3 Code relay: 127.0.0.1:<port>`, and the unfiltered file holds one
`pair a browser (link valid 5m): ` line. Never use that first link: it went
through a command's output on the way to the file. Delete the file. A missing
pair line comes with a `warning: no T3 Code pairing link: ...` line on stderr
while `session up` still exits 0; that is a setup failure, so stop.

## 4. Pair your tab

1. Make a fresh link:
   `(umask 077; booley session pair > <run-dir>/scratch/pair.txt)`. The link
   is the text after `: ` on its one line. Read it with the Read tool, not a
   shell command.
2. Open it at once with `preview_open` (`reuseExistingTab: false`), then
   delete `pair.txt`. Take one `preview_snapshot` (`save: true`,
   `includeImage: false`) to make the new tab draw (step 6, "Hidden tab"),
   then call `preview_resize` with `freeform` 1440×900 and confirm the size
   with `preview_status`.
3. Check that the URL `preview_status` reports no longer holds the token; a
   paired tab shows `/draft/<id>`. If it holds the token, log a finding, and
   keep the address bar out of every screenshot.

Worked when the page shows the T3 Code UI with the demo Project (`picorv32`)
in its project list, not "No project" and not a pairing prompt. Until #5686
is fixed, T3 Code lists the Project as `work`, the Sandbox's mount path,
because Booley registers it without a title: a check that expects `picorv32`
accepts `work` and notes #5686. An expired or used link shows a pairing
error: make a new one with step 4.1, never reuse one. Pair again the same way
after `booley session down` and `up`, or when the relay reports it moved
(`warning: 127.0.0.1:<old> is held by another process ...`).

## 5. Dashboard and threads

**Dashboard.** Until the Sandbox starts the Dashboard and T3 Code opens it
on its own (#4749), start it by hand. In the T3 Code UI, open the terminal
drawer of a thread on the main checkout and run `booley dashboard --web`
there; it serves on Sandbox `127.0.0.1:8090` until stopped. Open
`http://127.0.0.1:8090/?worktree=/work` (or another worktree's absolute path)
in T3 Code's preview panel. The relay forwards only T3 Code's own port, so
your tab can't load the Dashboard directly; it appears only inside the panel.

Worked when the preview panel shows the Dashboard page for that worktree and
it refreshes (every 3 s).

**Threads.** A Developer Agent is a new T3 Code thread on the Project: choose
the provider (Claude or Codex) and the checkout in the T3 Code UI, then send
the prompt. For a child, start the thread in a new worktree, as
[DEVELOPER-CHILD.md](../DEVELOPER-CHILD.md) says: T3 Code creates it under
`t3-worktrees/` in the Booley Directory, names its branch, and runs
`booley worktree prepare` before the first turn; if that fails, the thread
does not start. Approve or reject the agent's permission requests in the
thread.

## 6. Interaction rules

- **Clicking:** click by role or text locator or by an `aria-ref` from
  `preview_snapshot`, and check the result right after (the thread opened,
  the dialog closed): a missed click fails silently. Screenshots come back
  scaled (a 1440×900 viewport gives a 1280×800 image), so never click at
  coordinates read off a screenshot.
- **Reading:** T3 Code's UI is ordinary page text. Read it with
  `preview_evaluate` or `preview_snapshot`. The Dashboard inside the preview
  panel may not be readable from your tab's DOM; then read it from the
  screenshot and compare with its JSON (step 8).
- **Waiting:** wait for a known text with `preview_wait_for` (up to 60 s per
  call), never with a fixed sleep. An agent turn can take many minutes: wait
  in a loop and look at the thread between waits.
- **Hidden tab:** while your tab is not visible (`preview_status` reports
  `visible: false`) it renders no frames, and `preview_resize` fails with
  "desktop did not draw the page", as it does right after `preview_open`.
  Take a `preview_snapshot` (`save: true`, `includeImage: false`) first: it
  forces a repaint.
- **Snapshot timeouts:** `preview_snapshot` can fail or time out several
  times in a row while `preview_evaluate` still works. When it fails with
  "not producing screenshot frames", `preview_navigate` your tab to `/` and
  take the snapshot again; waiting does not help. Otherwise wait a minute and
  retry, for up to five minutes. If it still fails, see step 9.
- **Files:** write them from the host with
  `booley session enter -- sh -c '...'` (heredoc or Python), never by typing
  into an editor view.

## 7. Host commands

Run Sandbox commands from the host as `booley session enter -- <command>`;
the Project is at `/work` inside the Sandbox. A failing
`booley session enter` exit code of 126 or 127 means the command did not
start, not that a Flow failed.

## 8. Oracles

The CLI is the truth; the main bug class is "GUI disagrees with CLI". Run the
oracle right after the screenshot it checks, then look at the GUI again after
one refresh before calling a mismatch.

| GUI surface | Oracle |
|---|---|
| T3 Code project list, threads and their checkouts | `booley session enter -- git -C /work worktree list`, and what you started |
| Dashboard Jobs list and Job detail | `booley session enter -- curl -s 'http://127.0.0.1:8090/api/snapshot?worktree=<path>'` for what the page should render; the Job records for the data: `booley session enter -- sh -c 'for f in /work/.booley/{,logs/,logs/*/,job_logs/*/}.runtime/jobs/*.json; do jq -c "{run_id,endpoint,status,exit_code,owner,work_dir}" "$f"; done 2>/dev/null'`, plus `ps -eo pid,etime,args` for running ones |
| Job owner | The thread's T3 Code title, else the agent and a short thread id; `you` for a Standalone Job (CLI-REFERENCE, `booley dashboard --web`) |
| Dashboard resource figures | The snapshot JSON; roughly, `nproc`, `cat /sys/fs/cgroup/memory.current /sys/fs/cgroup/memory.max` and `df -h /work` |
| Run form choices and review screen | `booley targets`, `booley specialist <name> --help`, the `[agent]` settings; the shown command run in the Sandbox, whose Job record must match the form's |
| A `measure` Job and the report it links | The run's own document by run ID (the Job record carries `run_id`): `find /work/.booley/measurements -name '<run_id>.json'` read with `jq .`, and its commit against `git -C <worktree> rev-parse HEAD`. Never run `booley measure` as an oracle on a commit that is not Measured: it starts a measurement |
| Doctor | `booley doctor 2>&1 \| grep -v gitconfig-identity` |

A surface with no oracle is still checked against what you did. Record the
gap in `log.md` the first time you meet it.

## 9. Evidence

- Every claim gets a screenshot named `<area>-<nn>-<what>.png` (for example
  `dashboard-live-03-jobs-two-flows.png`). Take it with `preview_snapshot`
  (`save: true`, `includeImage: false`, your `tabId`) and copy the returned
  `screenshotPath` into the run's evidence directory under that name. Save
  the matching oracle output as `<area>-<nn>-<what>.txt` next to it.
- **When screenshots fail.** After the retries in step 6, the claim has no
  screenshot: record the tool's error. `booley-gui-check` then marks the step
  `blocked` (its step 4); the `gui` mission logs a finding and goes on.
- Recordings only to reproduce a finding.
- Screenshots come only from the public demo Project. Before posting any image
  anywhere public, look at it yourself: the confidential-content guard reads
  text, not images. Drop any image that shows a pairing link or token, an
  email address, a private repo, a host notification, or anything beyond
  your tab's page.

## 10. Cleanup

1. Stop `booley dashboard --web` (Ctrl+C in its terminal drawer) and close
   your tab (`t3_preview_close` with your `tabId`).
2. `cd $QA_PROJECT && booley session down`, which removes the Sandbox and
   stops the relay. The relay's port record stays on purpose: the next run at
   the same `$QA_PROJECT` reuses the port.
3. Remove the clone's contents and the `<run-dir>/scratch/` link files.
4. Restore the canonical host install the run started with (the revision you
   recorded in step 2), the same way step 2 installed the candidate, and run
   `booley bootstrap --update` from it. If its original wheel no longer
   exists, build a development wheel from that revision as step 2.1 does and
   install that.
5. Remove the candidate wheel and its detached worktree.

Check: `booley session status`, run in `$QA_PROJECT` before item 3, reports
the Sandbox absent, and `booley --version` prints the revision recorded in
step 2.

Exception: if a finding needs a live repro, leave the Sandbox up and the
candidate installed, skip items 2-5, and tell the maintainer the Project
path, the candidate revision, and the finding.

## Not yet verified

No run has confirmed these yet. The first run that reaches them records each
in its `log.md`; fix this file where it is wrong:

- T3 Code creates and prepares a thread's new worktree in the Sandbox.
- The preview panel loads the Dashboard at Sandbox `127.0.0.1:8090`, and
  whether its DOM is readable from your tab.
