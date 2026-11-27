# T3 Code GUI QA runbook

This runbook takes a QA agent from a Booley commit `<sha>` to the picorv32
demo Project open in T3 Code, Booley's user interface, in a browser tab on
this host. It then checks the GUI against the CLI and cleans up. The
candidate build runs in the QA Host
([its runbook](../qa-host/RUNBOOK.md)), a Docker-in-Docker container with
its own Docker daemon and `HOME`, so the host's canonical Booley install and
its `booley-*` containers stay as they are. The `gui`
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
| `<run-dir>` | The run directory the mission or skill created, on the outer host |
| `qa$ ...` | A command run in the QA Host as user `qa`, as its runbook's table says; add `-w <dir>` for its working directory |
| `$QA_PROJECT` | `/home/qa/gui-project/picorv32` in the QA Host: one fixed path in the `qa-host-home` volume, so the Project keeps its relay port across runs |
| `<port>` | The T3 Code relay port that `booley session up` prints, inside the QA Host (3780-3784) |
| `<outer>` | `<port>` + 110: the outer host port your tab opens (3890-3894) |

Every `booley` command in this runbook runs in the QA Host as `qa$`, in
`$QA_PROJECT` unless it says otherwise. Only the `preview_*` tools, `docker`
commands that manage the QA Host, and `primary-ids.sh` run on the outer
host.

## Hard rules

- **Developer Agents run in T3 Code threads.** In a GUI run, start every
  agent from the T3 Code UI in your tab: the threads are part of the GUI
  under test. A bare `claude` or `codex` session bypasses that GUI, so it is
  never GUI evidence.
- **The pairing link is a secret.** It carries a one-time token. Write
  `booley session up` and `booley session pair` output only to a file created
  with `umask 077` under `/home/qa/scratch/` in the QA Host, copy it out only
  to a `umask 077` file under `<run-dir>/scratch/`, never print the link in a
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
- **One GUI run per host at a time.** There is one QA Host per host, with
  fixed ports. Two runs would share it, `$QA_PROJECT`, its relay port and the
  browser profile's session for `127.0.0.1`.
- **Leave the outer host's Booley alone.** Never install a candidate on the
  outer host or touch its `booley-*` resources; the QA Host runbook's rules
  apply throughout.
- **Keep the maintainer's identity out of evidence.** Before a screenshot or
  DOM read of Doctor output, filter its identity line
  (`booley doctor 2>&1 | grep -v gitconfig-identity`) or capture only the
  Summary.

## 1. Preconditions

- **Same host.** A T3 session with the `mcp__t3-code__preview_*` tools on the
  host whose Docker runs the QA Host. Its relay ports are published on outer
  `127.0.0.1` only, so a browser on another machine can't reach them.
- **QA Host.** The caller approved starting it (the QA Host runbook's Rules
  say who may). Outer ports 3890-3894 are free (`ss -ltn`) or held by a
  `qa-host` container that this run may reuse (step 2.3). A QA Host that
  can't be built or started, or ports held by anything else, block the run:
  `blocked: qa host`.
- **Agent sign-in.** For areas that run a Developer Agent, copy each Agent
  CLI's credential into the QA config home as QA Host step 1.5 says; then
  `qa$ booley auth --status` shows the Project's Agent CLIs signed in.
- **Disk.** Follow [`qa/DISK.md`](../../DISK.md). A new `qa-host-docker`
  volume needs about 20 GiB (QA Host Measurements); a reused one needs room
  for the rebuilt layers and the RISC-V Project image.
- **No leftovers.** In the QA Host, `$QA_PROJECT` is absent or empty, and no
  Sandbox of it runs (`qa$ booley session status` there reports it absent).
  Otherwise ask the maintainer before removing anything: it may be another
  run's live repro.

## 2. Candidate build

1. Save the outer daemon's IDs, as QA Host step 1.1 does, from the source
   checkout:
   `qa/shared/qa-host/primary-ids.sh > <run-dir>/evidence/qa-host/before.txt`.
   Record the outer host's `booley --version` in `log.md`.
2. Make a detached worktree of `<sha>` outside `.worktrees` and the run
   directory, and build a development wheel from it, as "Building a
   development wheel" in
   [CONTRIBUTING.md](../../../docs/internals/CONTRIBUTING.md) says.
3. Start the QA Host, or reuse it. Its image tag is the last commit that
   changed the image's inputs, read in the source checkout:
   `git log -1 --format=%h --abbrev=9 -- qa/shared/qa-host ':!*.md'`.
   - A `qa-host` container runs that image and no Sandbox runs in it
     (`docker exec qa-host docker ps` lists only `booley-proxy` and
     `booley-reaper`): reuse it. A Sandbox running there is another run's:
     `blocked: qa host`.
   - Any other `qa-host` container: `docker rm -f qa-host`. Its volumes keep
     the images, install and Projects.
   - No `qa-host:<tag>` image: build it, as QA Host step 1.2 does.
   - Unless you reuse one, start the container with QA Host step 1.2's
     `docker run` (with the `qa-host-docker` and `qa-host-home` volumes,
     existing or new). Done when `docker exec qa-host docker info` succeeds.
4. Install the wheel as QA Host step 1.3 says, then update Host Bootstrap:
   `qa$ booley bootstrap --update` on a reused `qa-host-home` volume, plain
   `qa$ booley bootstrap` on a new one. A new `qa-host-docker` volume builds
   the base Sandbox Image from scratch (about 20 minutes); a reused one
   rebuilds only the changed layers (about 1 minute). Note the time.

Worked when `qa$ booley --version` reads `booley <version> (<short>, <date>)`
and `qa$ booley bootstrap --check-only` reports Host Bootstrap current. A
build that fails or overruns the caller's budget is `blocked: image build`.

## 3. Project

Set the Project up in the QA Host as the demo README's Quick start says:
upstream picorv32 at the
[picorv32 mission](../../missions/picorv32/MISSION.md)'s pin, with the demo
cloned at `main` as its Stealth Booley Directory. Record the demo `main` SHA
in `log.md`.

```sh
qa$ git clone https://github.com/YosysHQ/picorv32 $QA_PROJECT
qa$ git -C $QA_PROJECT checkout --detach a473fc8fca393771d83b0ffcf0b14db3393339d8
qa$ git clone https://github.com/boldaxolotl/booley-prj-picorv32 $QA_PROJECT/.booley
```

Turn on the Run form's Feature Flag for this clone only: add
`enable = ["web-run-panel"]` under `[features]` in `.booley/booley.toml`, and
commit it on a local branch `qa/<short>` (never push it). User `qa` has no Git
identity, so give the commit one:
`qa$ git -C $QA_PROJECT/.booley -c user.name=qa -c user.email=qa@qa-host.invalid commit -qam 'QA: enable web-run-panel'`.
Then, in `$QA_PROJECT`:

```sh
qa$ booley init
qa$ umask 077; mkdir -p ~/scratch; booley session up > ~/scratch/session-up.txt 2>&1
qa$ grep -v 'pair a browser' ~/scratch/session-up.txt
```

`booley init` builds the Project's Sandbox Image (about 4 minutes); note its
time.

Worked when the filtered output shows `Sandbox ready: <name>` and
`T3 Code relay: 127.0.0.1:<port>` with `<port>` in 3780-3784, and the
unfiltered file holds one `pair a browser (link valid 5m): ` line. Never use
that first link: it went through a command's output on the way to the file.
Delete the file. A missing pair line comes with a
`warning: no T3 Code pairing link: ...` line on stderr while `session up`
still exits 0; that is a setup failure, so stop. A `<port>` outside
3780-3784 is not forwarded to the outer host: `blocked: qa host`.

## 4. Pair your tab

1. Make a fresh link in the QA Host with its port changed to `<outer>`,
   copy it out, and delete the inner file:

   ```sh
   qa$ umask 077; booley session pair | sed 's#//127\.0\.0\.1:<port>/#//127.0.0.1:<outer>/#' > ~/scratch/pair.txt
   (umask 077; docker exec qa-host cat /home/qa/scratch/pair.txt > <run-dir>/scratch/pair.txt)
   docker exec qa-host rm /home/qa/scratch/pair.txt
   ```

   The link is the text after `: ` on its one line. Read it with the Read
   tool, not a shell command, and check it holds `127.0.0.1:<outer>`.
2. Open it at once with `preview_open` (`reuseExistingTab: false`), then
   delete `<run-dir>/scratch/pair.txt`. Take one `preview_snapshot`
   (`save: true`, `includeImage: false`) to make the new tab draw (step 6,
   "Hidden tab"), then call `preview_resize` with `freeform` 1440×900 and
   confirm the size with `preview_status`.
3. Check that the URL `preview_status` reports no longer holds the token; a
   paired tab shows `/draft/<id>`. If it holds the token, log a finding, and
   keep the address bar out of every screenshot.

Worked when the page shows the T3 Code UI with the demo Project (`picorv32`)
in its project list, not "No project" and not a pairing prompt. An expired or
used link shows a pairing error: make a new one with step 4.1, never reuse one.
Pair again the same way after `booley session down` and `up`, or when the relay
reports it moved (`warning: 127.0.0.1:<old> is held by another process ...`).

## 5. Dashboard and threads

**Dashboard.** Until the Sandbox starts the Dashboard and T3 Code opens it
on its own (#4749), start it by hand. In the T3 Code UI, open the terminal
drawer of a thread on the main checkout and run `booley dashboard`
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
- **Files:** write them from the QA Host with
  `qa$ booley session enter -- sh -c '...'` (heredoc or Python), never by
  typing into an editor view.

## 7. Host commands

Run Sandbox commands as `qa$ booley session enter -- <command>` in
`$QA_PROJECT`; the Project is at `/work` inside the Sandbox. A failing
`booley session enter` exit code of 126 or 127 means the command did not
start, not that a Flow failed.

## 8. Oracles

The CLI is the truth; the main bug class is "GUI disagrees with CLI". Run the
oracle right after the screenshot it checks, then look at the GUI again after
one refresh before calling a mismatch.

| GUI surface | Oracle |
|---|---|
| T3 Code project list, threads and their checkouts | `booley session enter -- git -C /work worktree list`, and what you started |
| Dashboard Now and Results lists | `booley session enter -- curl -s 'http://127.0.0.1:8090/api/snapshot?worktree=<path>'` for what the page should render; the Job records for the data: `booley session enter -- sh -c 'for f in /work/.booley/{,logs/,logs/*/,job_logs/*/}.runtime/jobs/*.json; do jq -c "{run_id,endpoint,status,exit_code,owner,work_dir}" "$f"; done 2>/dev/null'`, plus `ps -eo pid,etime,args` for running ones |
| Job owner | The thread's T3 Code title, else the agent and a short thread id; `you` for a Standalone Job (CLI-REFERENCE, `booley dashboard`) |
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

1. Stop `booley dashboard` (Ctrl+C in its terminal drawer) and close
   your tab (`t3_preview_close` with your `tabId`).
2. `qa$ booley session down` in `$QA_PROJECT`, which removes the Sandbox and
   stops the relay. The relay's port record, in the QA config home, stays on
   purpose: the next run at the same `$QA_PROJECT` reuses the port.
3. `qa$ rm -rf $QA_PROJECT`, and remove any link files left in
   `/home/qa/scratch/` and `<run-dir>/scratch/`.
4. Remove the candidate wheel, inside the QA Host
   (`qa$ rm /home/qa/<wheel-file>`) and on the outer host, and its detached
   worktree. The candidate stays installed in the QA Host; the next run
   replaces it.
5. `docker rm -f qa-host`. Keep its volumes and image for the next run.
6. Save `qa/shared/qa-host/primary-ids.sh` output to
   `<run-dir>/evidence/qa-host/after.txt` and `diff` it with `before.txt`
   (QA Host step 3.2). Only `qa-host` rows may differ, such as a new
   `qa-host:<tag>` image. Any other difference is a finding: record each row
   in `log.md` with what made it, if known. A container row present in only
   one snapshot can be another process's short-lived container; still record
   it.

Check: `qa$ booley session status`, run in `$QA_PROJECT` before item 3,
reports the Sandbox absent, and the outer host's `booley --version` prints
the same revision as before the run.

Exception: if a finding needs a live repro, leave the QA Host and its
Sandbox up, skip items 2-5, and tell the maintainer the Project path in the
QA Host, the candidate revision, and the finding. Item 6 still runs.

## Not yet verified

No run has confirmed these yet. The first run that reaches them records each
in its `log.md`; fix this file where it is wrong:

- T3 Code creates and prepares a thread's new worktree in the Sandbox.
- The preview panel loads the Dashboard at Sandbox `127.0.0.1:8090`, and
  whether its DOM is readable from your tab.
