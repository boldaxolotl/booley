# Troubleshooting

Symptoms you may hit with Booley, what causes them, and how to fix them. Search
this page for the exact error text you see.

Start with `booley doctor`: it checks the host, the Project and the Sandbox and
prints a fix hint (`→`) under most problems; see
Doctor (host doc, not shipped). Easier still, type `/booley-heal` in
the Developer Agent chat. The agent runs Doctor, fixes what it can using this
page, checks the result with `booley doctor --deep`, and hands you the steps
only you can do on the host. Do the same when Booley tells you its version
changed.

If you suspect the agent planted Git configuration or hooks, read the
[known Sandbox escape warning](SANDBOX.md#known-sandbox-escape-through-shared-git-metadata)
before you run Doctor on the host: host-side Doctor can trigger it.

Windows-only first-run problems are on the Windows (host doc, not shipped)
page.

## Installation and editor integration

### Installation fails with `externally-managed-environment`

Your distribution's Python refuses `pip install` into the system interpreter
(PEP 668). Install Booley with pipx instead. On Ubuntu or Debian, take pipx from
the distribution: a pip-installed pipx fails there until `python3-venv` is
installed, and the `pipx` package pulls it in.

```bash
sudo apt install pipx
pipx ensurepath
# reopen the terminal
pipx install booley-rtl
booley bootstrap
```

With uv, use `uv tool install booley-rtl` instead.

### The wrong `booley` runs

An older `booley` earlier on `PATH` shadows the one you installed. Booley
records which installation owns the host and refuses commands from any other:

```text
this Booley process does not match the canonical host installation (...)
```

Run `type -a booley` (`where.exe booley` on Windows), then remove or upgrade
every install listed before the one you want. Compare `booley --version` with
`pipx list` or `uv tool list`. Then run `booley bootstrap --update` to record
the right installation; do the same after an intentional upgrade.

After a distribution Python upgrade, run `pipx reinstall-all` first, then
`booley bootstrap --update`.

### VS Code says "A mount config is invalid" while reopening the container

A folder the Sandbox mounts from the host is missing. Booley checks every mount
before Docker creates the container and names the missing one:

```text
refusing Sandbox preparation: generated bind source for <target> is missing: <source>
```

Restore that folder. If you removed it on purpose, run `booley init --seed` on
the host and reopen the container.

When a stopped container's mounts are out of date, Booley recreates the
container. Mounted folders and named volumes survive; anything written only
inside the container is lost. If Booley says a headless Sandbox is running, run
`booley session down` first. A running container whose Booley Directory mounts
are outdated, including any from before Booley 0.3.0, is refused, not migrated:
close VS Code, stop and remove that container, run `booley init --seed` on the
host, then reopen. If Booley calls the container ambiguous, inspect it and
remove it yourself if it is yours.

### `booley` is missing from `/mcp` in Claude Code or Codex

1. Exit the agent CLI and start it again, so it rereads its MCP configuration.
2. Inside the Sandbox, read `/tmp/booley_mcp_http.log`; the server listens on
   port 8814.
3. Still missing: run `booley init --seed` on the host, then rebuild or reopen
   the container.

If it is still missing after that, it is a Booley bug; report it (see
[Still stuck](#still-stuck)) with the Doctor output and that log.

### An MCP tool is missing from `/mcp`

- A Flow or Specialist with `enabled = false` under `[flows.<name>]` or
  `[specialists.<name>]` in `booley.toml` is hidden on purpose.
- A custom MCP tool is skipped when its file has a syntax error, or when `name`
  and `description` are not plain literal class attributes. Ask the agent to
  fix the file.
- Ask the agent to refresh its tool list. Adding or editing a custom tool, or
  changing `enabled`, takes effect without restarting the Sandbox.

### A Booley skill name is occupied

`booley bootstrap` links Booley's skills into `~/.agents/skills` and, if it
exists, `~/.claude/skills`. It never overwrites an entry it doesn't own, and
reports `desired skill name is occupied` instead. Look at the named entry:
rename it if it is your own skill, delete it if it is a dangling link, then
rerun `booley bootstrap`.

If `booley init` reports a link that needs `--force`, rerun it as
`booley init --force`. A corrupt `.booley-skill-links.json` in those folders
stops bootstrap without touching any link: fix or remove the broken entry, then
rerun.

### RTL opens as plain text, or Live Preview is missing

The Sandbox Image bundles the Verilog-HDL and Live Preview extensions. If one
is missing, install it from a terminal in the attached VS Code window, then run
**Developer: Reload Window**:

```sh
code --install-extension /usr/local/libexec/booley/veriloghdl-1.29.0.vsix
code --install-extension /usr/local/libexec/booley/live-server-0.4.20.vsix
```

Live Preview needs VS Code 1.109 or newer. If a file doesn't exist, or Doctor
reports devcontainer drift, rebuild the Sandbox Image and the container.

### The Dashboard does not open on Sandbox attach

Run `booley dashboard` in a Sandbox terminal to open it by hand. To find out
why it didn't open on its own, check **Output → Booley Dashboard** in VS Code.
It doesn't open when:

- `[sandbox].dashboard = false` is set in `booley.toml`;
- your user setting `task.allowAutomaticTasks` is `"off"`, or the Project's
  `.vscode/settings.json` sets it to `"off"`;
- you removed or edited the Dashboard task in `.vscode/tasks.json`, or Git
  tracks that file. To restore a removed task, set `[sandbox].dashboard = false`, run `booley init` (on the host, or in a hand-written Sandbox such as a Codespace; a Booley-issued Sandbox needs the host), set it
  back to `true` and run `booley init` again;
- the window isn't a Dev Containers or Codespaces window, the folder isn't a
  Git repository, or the window is not trusted yet;
- you closed it earlier in this window session; run **Booley: Open Dashboard**
  from the Command Palette to get it back;
- `booley dashboard` says it is already open: the Dashboard is shown in another
  window or terminal. One left behind by a window reload is replaced within
  about 15 seconds;
- the Booley Sandbox Integration extension is missing or out of date. Doctor
  warns about it. After upgrading Booley, run `booley init` on the host and
  rebuild the container: a running container never picks up a newer image.

With your own `[sandbox].image`, install the current Booley wheel and add
`RUN python3 -I -m booley.runtime.dashboard_extension` as root to the image
recipe. Without it, both Dashboard auto-open and `bwave gui` views are
unavailable.

### A session shows `activity unknown`

The Dashboard shows only what Claude Code or Codex reports; it never guesses.
A session shows `activity unknown` when:

- it hasn't reported anything yet; its next prompt or tool call fixes that;
- it has shown "working" or "waiting for tool call" for over two hours with
  no new event, which usually means the client stopped mid-turn;
- the Sandbox Image lacks Booley's activity hooks. Doctor in the Sandbox warns
  about it; rebuild the image and the container;
- several agent clients share one worktree and Booley can't tell them apart.

After you approve a permission request, a session keeps showing "waiting for
user input" until that tool finishes.

### `bwave gui` fails on a scoped view

A scoped view (`--signals`, `--group`, `--time`, `--cursor` or `--append`)
needs a VS Code window attached to the Sandbox; a bare `bwave gui` doesn't.
`booley doctor` in the Sandbox checks every attached window and tells you
before `bwave gui` does.

- **The window doesn't answer.** Run **Developer: Reload Window** in it. A
  rebuild is rarely needed.
- **Several windows are attached.** `bwave gui` uses the window whose terminal
  you typed it in. If it can't tell, it says so: run it from that window's
  terminal, or close the others.
- **VaporView is missing or the wrong version.** Booley works only with the
  bundled VaporView 1.6.0. In a terminal of the attached window (not a host
  terminal) run
  `code --install-extension /usr/local/libexec/booley/vaporview-1.6.0.vsix`,
  then **Developer: Reload Window**. Check with
  `code --list-extensions --show-versions`, which must list
  `lramseyer.vaporview@1.6.0`.
- **"Error getting user theme" or wrong signal colours.** The Sandbox Image
  ships VaporView already patched, so this means the container runs an image
  from Booley 0.4.0 or earlier, or VaporView was installed from somewhere other
  than the bundled VSIX. Update Booley and rebuild the container; until then,
  **Developer: Reload Window** clears it. If it persists after a reload, read
  `~/.booley/vaporview-install-watch.log`.
- **The view opens but signals are missing.** A WARNING names each signal that
  couldn't be shown or each `--signals` pattern that matched nothing; the trace
  itself is fine. Check the full name with `bwave list`.
- **The viewer "did not apply" the view.** Booley already restored the previous
  view; retry once the window has finished loading the trace.

### `No space left on device` during an image build

Docker ran out of disk mid-build, even though Booley checked free space first.
Free Docker space, for example with `docker builder prune`, and retry. Pruning
means the next build rebuilds the layers it removed.

## Sandbox, Git, and worktrees

### Developer Agents and Specialists turn into "Not logged in" partway through an unattended run

The default Claude Code and Codex sign-in rotates its token on every refresh.
The host and the container share that token, so a Claude Code or Codex session
on the host can invalidate the container's copy mid-run.

Use a sign-in that doesn't rotate: run `booley auth` for Claude, or
`booley auth --cli codex` for Codex, on the host. Rebuild an existing container
once so it gets the new credential. Billing choices are in
Setup (host doc, not shipped).

### Two Developer Agents keep clobbering each other's edits

They share one worktree. Sessions started with `booley chat` get their own
automatically: while the main checkout is in use, the next one starts in a
worktree `s<N>`. A plain `claude` tab or VS Code panel started before any
Booley tool call is not counted, so it may still share the main checkout. Start
such a session in a worktree from a container terminal: run
`booley chat --new-worktree`, or run `booley worktree new <name>`, `cd` to the
path it prints, and start `claude` there. Asking an agent that already runs in
the main checkout to create a worktree is not enough: its Booley tool calls
keep running on the main checkout. A VS Code panel cannot be started in a
worktree that way, so use a terminal with `booley chat` for a second editing session,
and start Codex with `booley chat` too. See
Several agents at once (host doc, not shipped).
The Dashboard warns about a shared worktree but doesn't separate the agents.

### `booley chat` starts in a worktree although the main checkout looks free

Read the reason at the end of the `Booley session S<N> works in worktree ...`
line:

- `the main checkout is in use by another booley chat`: another `booley chat`
  whose agent runs in the main checkout is still running, perhaps in another
  terminal or stopped with Ctrl-Z. That includes one started with
  `--new-worktree` or under `[chat] worktree = "always"` while the main checkout
  was free: it holds the main checkout until its session ends, although its
  agent works in a worktree. A `booley chat` that `auto` placed in a worktree
  holds nothing. A `booley chat` that
  crashed or was killed does not cause this, because the hold ends when its
  process exits, however it exits. If the launch also printed
  `NOTE: main-checkout lock unavailable: ...`, Booley could not open or lock
  `runtime/chat-main.lock` in the Main Booley Directory and counts the main
  checkout as in use; fix the error the note names.
- `Session S<N> is working in the main checkout`: that session made a Booley
  tool call from the main checkout and has not ended. A Codex started by hand, or a
  session Booley could not tell apart by process, that ended without notice counts as
  in use until it goes quiet (`[goals] quiet_after`, two hours by default).

Either way, `booley chat --here` starts in the main checkout now.

### `booley worktree rm` refuses a worktree that is locked by Git

```text
... is locked by Git (initializing)
```

`booley worktree gc` keeps such a worktree too. Someone ran
`git worktree lock`, or a worktree creation stopped abruptly while
`git worktree add` was checking out (killed with SIGKILL, or the Sandbox
stopped), which leaves the reason `initializing`. Git removes that lock itself
when the creation is merely interrupted.

Make sure no `booley chat` or `booley worktree new` is still creating the
worktree. Then, in the Sandbox, either run `git worktree unlock <path>` followed
by `booley worktree rm <name>`, or run `booley worktree rm --force <name>`;
`--force` also removes a Git-locked worktree, and a half-made checkout may
otherwise be refused again for uncommitted changes.

If `booley worktree rm` itself was interrupted, run it again with the same options:
it finishes the removal.

### Worktrees show as `prunable` on the host

A worktree created in the Sandbox can sit at a container path the host can't
reach, so host Git marks it `prunable`. Don't prune them from the host. Run Git worktree
commands inside the Sandbox, and remove a worktree with
`booley worktree rm <name>` (see the
CLI reference (host doc, not shipped)).

### A new worktree fails in the post-setup hook

```text
ERROR: post-setup hook failed (rc=3); log: .../.booley/worktrees/.logs/<name>.log
```

The same error appears from `booley worktree new` and from `booley chat` when it
creates the worktree. Under `booley chat`, the agent does not start and
`booley chat` exits 1.

Booley removed the worktree and its `booley/<name>` branch, so nothing is left
to clean up, unless the error also says `outer rollback failed` or the hook
committed on `booley/<name>`, which Booley keeps. Read the log the error names
for the hook's output. Fix `.booley/hooks/post-setup.*` and run the command
again. The same cleanup follows a failed submodule fill.

`booley chat` can also stop before setup starts:

```text
ERROR: cannot name a worktree for this session: ... Run booley chat --here to use the main checkout.
```

Booley could not reserve the session handle that names the worktree. Run
`booley chat --here`, or try again once other launches have finished.

If you press Ctrl-C during setup, `booley chat` prints
`Worktree setup interrupted; <name> was rolled back.` and nothing is left. If it
prints `Worktree setup interrupted; <name> may be left behind.` instead, run the
removal steps printed below it.

<details markdown>
<summary>In stealth mode</summary>

Booley also removed the `booley-worktree/<name>` branch. Commit the hook fix in
the Booley Directory's own repository before you retry: the worktree's Paired
Checkout starts from that repository's last commit. See
Stealth Mode (host doc, not shipped).

</details>

### A new worktree lacks my uncommitted `.booley` changes

This happens only in Stealth Mode (host doc, not shipped), where
the Booley Directory is its own Git repository.

`booley chat` or `booley worktree new` warned that files were left out:

```text
WARNING: the Paired Checkout was made from the last commit of <dir>; N uncommitted or untracked file(s) there were left out: ... Commit them in <dir> to include them in later worktrees.
```

A Paired Checkout starts from the last commit of your Booley Directory's
repository, so uncommitted and untracked files there are not copied. Commit the
files the warning names, then create the worktree again with a new name. Under
`booley chat`, commit them, end the session, discard its worktree, and start
`booley chat` again.

### `git` cannot see files under `.booley/`

```text
$ git checkout -- .booley/booley.toml
error: pathspec '.booley/booley.toml' did not match any file(s) known to git
```

This happens in Stealth Mode (host doc, not shipped), or in a
Project set up while Stealth was the default (Doctor then warns
`stealth.enabled-unset`): your RTL repository ignores `.booley/`, so its Git commands can't see `booley.toml`,
`tests.toml`, Goalsets or `.core` files there. If `.booley/` is its own Git
repository, run Git there:

```bash
git -C .booley status
git -C .booley checkout -- booley.toml
```

If `git -C .booley rev-parse --git-dir` fails, those files aren't under version
control at all: copy a file aside before you edit it.

### A commit is rejected for a banned term

```text
commit-msg: commit message rejected by Stealth Mode
  'booley' in the subject
  'booley' on line 3
Your message is kept unchanged in "/path/to/repo/.git/COMMIT_EDITMSG".
```

Stealth Mode (host doc, not shipped) rejects a commit message that
contains a banned word instead of rewriting it, and an attribution footer
(`Co-Authored-By:`, `Generated with <tool>`) is rejected first. Reword the
message, or remove the term from `[stealth] banned_words` / `banned_substrings`
in `.booley/booley.toml` if it is an ordinary name in your design. Your text
stays in the file the error names.

To retry, rerun the same `git commit` command with its options (`-a`,
`--amend`, paths), replacing `-m` or `-F` with `-v -e -F "<that path>"`. The
editor opens on the kept message. If a merge commit was rejected, finish it with
`git commit -m "<message>"`, or pass `-m` to `git merge`.

`BOOLEY_SKIP_COMMIT_VALIDATION=1` doesn't bypass this check. If the hook still
rewrites messages after an upgrade, run `booley init` to install the current
hook. `cursor` and `ticket` are no longer banned by default.

### Doctor reports a Sandbox whose egress mode differs from the host

You changed
[`[sandbox] egress`](SANDBOX.md#host-configuration-configtoml)
in the host `config.toml`, and a Sandbox still runs in the other mode. Doctor
warns with `host.sandbox-egress-mismatch`. Recreate that Sandbox:

- Headless: `booley session down --project <root>`, then
  `booley init --seed --project <root>`, then start it again.
- VS Code: `booley init --seed --project <root>`, then **Dev Containers:
  Rebuild Container** in that Project's window.

A stopped Sandbox refuses to start after the switch; run
`booley init --seed` and start it again. To keep one Sandbox in the other mode
on purpose, waive `host.sandbox-egress-mismatch` with `subject` set to its
Project root; see Doctor waivers (host doc, not shipped).

### Doctor reports a Session store file named for another Project Root

Doctor fails with `Session store check: ... identity mismatch: <file> is not
named for the Project Root it holds (<root>)`, and `booley session` commands
refuse with the same message. Booley keeps one private file per Project for
Sandbox issuance, pending editor starts, pending refreshes, and pending
invalidations, and it names each file after its Project Root. A file with the
wrong name is never trusted. Remove the file the message names, then reissue
the Sandbox spec on the host:

```bash
booley init --seed --project <root>
```

After upgrading on Windows, Booley names a Project Root without regard to case.
Files written by earlier releases are not migrated, so each Project needs this
once. Doctor names one file per store, so rerun it until it passes. Removing a
pending refresh file drops its rollback snapshot and its parked prior Sandbox;
removing a pending invalidation file skips its cleanup of the Project's FlexNet
relay containers and networks, which then need removing by hand in Docker.
`init --seed` regenerates the spec.

### Doctor reports that devcontainer.json differs

Doctor rebuilds the `.devcontainer/devcontainer.json` that `booley init` would
write now and diffs it against the file on disk. The warning
(`interactive.devcontainer-drift`) lists each changed key path (the first 20),
`-` for what is on disk and `+` for what init would write, so you see what a
reseed changes. Follow the remedy the warning prints; for a VS Code Sandbox
that is usually `booley init --seed --project <root>`, then **Dev Containers:
Rebuild Container** in that Project's window.

Inside the Sandbox, Doctor compares only what it can see (VS Code settings and
extensions, the attach hook, and the agent state volumes) and says host
`booley doctor` checks the rest. `interactive.devcontainer-unverified` means
the expected file could not be built (the warning names the error); fix that
error and rerun `booley doctor`.

### Docker disk still used after removing a Project

Deleting a Project's folder leaves its Sandbox Images and agent state volumes in
Docker. `booley projects forget <root>` drops the Project and its image tag,
and lists what remains with sizes. `booley projects forget --purge <root>`,
with the exact absolute root, also removes the unused images and state volumes,
including the agent's saved login and history. Both refuse while the Project
still has a Grant for a host EDA tool.

For images left over from earlier forgets, run `booley projects prune-keepers`,
then rerun it with the `--confirm <digest>` it prints. Images marked
`unrecorded` are kept; remove one with `docker image rm <id>` once no other
Project uses it. See the
CLI reference (host doc, not shipped).

### Recover sources hidden by an interrupted older run

Doctor warns `workspace.legacy-source-stash`: a Booley run from an earlier
version was interrupted while it had moved some source files aside. Doctor only
reports it. Recover the files inside the same Sandbox, before you rebuild it
or clear its temp directory:

1. Back up the worktree, including untracked files.
2. Open the reported `booley_isolation_*.manifest.json` or
   `booley_hide_files_*.manifest.json` record, including `.retired` records.
   Each `moved` pair lists the original path, then where it was stashed under
   `stash_dir`.
3. If the stash directory has mode `000`, run `chmod u+rwx <stash-dir>`. Copy
   back only missing files; never overwrite one that exists.
4. Once the sources are back, delete the stash and its record.

## Goal Mode

### Goal evidence and state recovery

If Goal status says a Goal has no current evidence, it names what produced it
(for example `rerun Reviewer`); ask the agent to rerun that. If saving Goal
state keeps failing or Booley reports a corrupt record, report it (see
[Still stuck](#still-stuck)). Don't edit or delete Goal state or evidence files.

### A Goal that names Test Runs stays unmet

Simulation, Cycle Count and coverage Goals are judged on the exact Test Runs
that Goal Mode fixed when it started; see
Simulation, Cycle Count, and coverage Goals (host doc, not shipped).
`booley goal status` lists each Goal's Test Runs with their outcomes and one of
these reasons. A coverage Goal is decided by its coverage floors alone, so it
can be met while one of its Test Runs failed.

| Reason | Meaning | Fix |
|---|---|---|
| `goal-test-run-incomplete` | Some Test Runs haven't passed yet. | Ask the agent to run them. |
| `goal-test-run-failed` | A Test Run failed on the current files; a later pass on the same files doesn't clear it. | Fix the cause, then rerun; status prints rerun commands. |
| `goal-test-run-pending` | The Goal names a Target, Test or Test Variant that doesn't exist yet. | Add it; Booley adds it to the Goal on the next run or status read. |
| `goal-test-run-unresolvable` | `tests.toml` can't be loaded, the Target can't take a seed, or the Test is skipped. | Fix `tests.toml` as the message says, or approve a Goal change. |
| `goal-simulation-suite-empty` | The Goal covers every Test of its Target, and every Test is skipped. | Register a Test, or remove one from `skip`. |
| `goal-test-run-missing` | A Test Run's Target, Test or Variant is gone or skipped. | Restore it, or approve a `retarget` the agent proposes. For a skipped Test, unskip it or approve dropping it. |
| `goal-test-run-changed` | A Test Run's arguments or environment changed in `tests.toml`. | Restore them, or approve a `retarget`. |
| `goal-test-run-metric-missing` | The last run didn't measure the Goal's Cycle Count or coverage. | Ask the agent to run exactly the Goal's Test Runs (with coverage, for a coverage Goal). |
| `goal-campaign-unaccounted` | A Simulation Campaign for the Goal is still running, holds an unadopted failure, or can't be read. | Wait for it or resume it, then read status again. |

Don't edit or delete the Simulation Campaign records under the Goal Record's
`logs/acceptance/campaigns/`. Booley moves an unreadable one aside when you
approve a Goal change.

## Simulation

### A `.core` library `depend:` won't resolve

The Sandbox has no FuseSoC libraries and its network proxy blocks fetching
them, so a `depend:` on a library core (`vlog_tb_utils`, `wiredelay`, ...)
fails, and Doctor reports the Target as failing to resolve. Copy the dependency
into your repository as a `.core`, or drop it. Often the testbench already
guards its use with an `ifdef` or defines the module itself; check before you
remove it.

### Every simulation test comes back INCONCLUSIVE

Every Test Run of an HDL testbench reads `INCONCLUSIVE`, `booley flow sim`
exits `1`, and each Test Run's log contains a line such as
`Verilator sim INCONCLUSIVE (rc=0, no sentinel)`.

Booley grades an HDL testbench by a pass or fail message it prints, a
[sentinel](FLOW-SIM.md#passfail-sentinels-and-cycle-counts).
A run is inconclusive when the simulator exits with code `0` but its output
holds no pass or fail sentinel and no error or assertion message. The
defaults are `[SIM_RESULT] PASSED` and `[SIM_RESULT] FAILED`;
`pass_sentinels` and `fail_sentinels` under `[flows.sim]` each replace their
default, and when a pass and a fail sentinel both appear the test fails. A
nonzero exit code without a sentinel is a failure, not inconclusive.

To check:

- Run `booley doctor`. It warns `sim.pass-sentinel-missing` when none of a
  sim Target's testbench files contains the pass sentinel literally. It
  skips Cocotb Targets, which take their verdict from cocotb, and with your
  own `pass_sentinels` it only adds a note, because the testbench may build
  the message at run time. If the warning says the testbench fileset is
  Python, the Target is missing `cocotb_module`; see
  [Cocotb Targets](TARGETS.md#cocotb-targets-python-testbenches).
- Open the Test Run's log (`…/evidence/*-run.log`, see
  [Reading results](FLOW-SIM.md#reading-results)) and
  search it for the sentinel. A testbench that ends on a bare `$finish`
  prints none.

To fix it, do one of these:

- Print the sentinel at the end of the test, for example
  `$display("[SIM_RESULT] PASSED");` or `$display("[SIM_RESULT] FAILED");`.
  Doctor's warning points to a fuller example.
- Declare the messages your testbench already prints as `pass_sentinels`
  and `fail_sentinels` under `[flows.sim]`.

If you ran with `--trace`, a test that would have passed is reported
inconclusive when the traced run produces no waveform Booley can read. This
is the exception to the missing-sentinel cause: the log still prints the pass
sentinel and a `PASSED` line, usually followed by an `ERROR:` line that says
why no waveform was kept. Fix the cause, often `trace_args` or `trace_files`
(see [Waveforms](FLOW-SIM.md#waveforms)), or rerun
without `--trace`. A failure or timeout keeps its verdict.

A Cocotb Target uses no sentinels: its Test Run is inconclusive when cocotb's
result file is missing or doesn't report that test as passed or failed, and
its log contains `cocotb sim INCONCLUSIVE (<reason>)`.

### Simulation stalls at time zero without results

A cocotb run builds, loads and logs, but simulation time stays at `0.00 ns`
and no `results.xml` appears. After `sim_time_grace_s` seconds at exactly
`0.00 ns` (180 by default), Booley stops the run with an `ERROR` verdict and
this message in the run log:

```text
ERROR: simulation killed: simulator time never advanced past 0.00 ns in 180s of wall clock.
```

followed by the likely cause. The watchdog starts counting at cocotb's first
log line, and a run whose clock moved past zero is never stopped by it. If
your testbench spends minutes in Python setup before the first clock edge,
raise `[flows.sim].sim_time_grace_s`; `0` turns the watchdog off, and the
run then waits for `timeout_ms`.

The usual cause is a cocotb release your project pins that doesn't work with
the image's Verilator 5, even though cocotb itself installs fine. No older
cocotb pin fixes it. Compare `cocotb-config --version` with the simulator's
version, then port the testbench to the image's cocotb 2.x (`cocotb.fork`
becomes `cocotb.start_soon`, plus a few import moves), or build a project
image with a matching Verilator. Image versions are in
[Supported EDA tools](SUPPORTED-EDA-TOOLS.md#versions-in-the-sandbox-image).

### A streamed `--trace` dump produces a trace incident

A `sim --trace` run converts a declared `.vcd` while the test runs, so the
testbench must not seek in, read back, or reopen that dump. If it does, the
trace incident names B-Wave's error. Write the dump once, front to back, or
dump FST instead.

### A run without `--trace` hangs after Booley was killed during a traced run

If Booley was killed hard during a `--trace` run, a leftover file at the
declared trace path can make the next run without `--trace` hang while it
writes there. The next `--trace` run removes the file, or delete it yourself.

### Verilator reports timing or enum errors

- **`%Error-NEEDTIMINGOPT`**: the testbench uses delays or event controls
  (`#10`, `@(posedge clk)`). Add `--timing` to the Target's
  `verilator_options`, or `--no-timing` for a cycle-driven testbench.
- **`%Error-ENUMVALUE`**: a value outside an enum's members is assigned to or
  compared with an enum variable. Add an explicit cast in the RTL; if it is
  intended, add `-Wno-ENUMVALUE` to that Target.

A waiver must name the code Verilator prints in `%Error-<CODE>` or
`%Warning-<CODE>`. Verilator 5 reports width problems as `WIDTHTRUNC` and
`WIDTHEXPAND`, so `-Wno-WIDTH` may not silence them.

### ccache settings are ignored or rebuilds miss the cache

Booley compiles Verilator builds through the Project's compiler cache (see
[Verilator builds](FLOW-SIM.md#verilator-builds)).
While the cache is in use, Booley sets `OBJCACHE`, `CCACHE_DIR`,
`CCACHE_MAXSIZE`, `CCACHE_COMPILERCHECK` and `CCACHE_BASEDIR` for each
build. These replace the same variables from your shell and from the
Target's [`env`](TESTS.md#per-target-environment-env);
for a Target `env` value that differs, Booley logs
`Target compiler-cache setting <NAME> yields to Booley's value`. When the
cache is disabled or its root is unset, Booley sets only `OBJCACHE`, to
empty. When ccache is missing or its storage can't be created, the other
values stay set but `OBJCACHE` is emptied, so ccache isn't used.

Only an assignment in the Target's `make_options` beats Booley, because
Make command-line variables override the environment. Two of them cost you:

- **`USER_CPPFLAGS=...`** replaces the debug-prefix map Booley appends to
  `USER_CPPFLAGS`, so a build with debug info (`-g`) misses the cache each
  time its build directory changes. Set extra flags in the Target `env`'s
  `USER_CPPFLAGS`, which Booley keeps, or pass them with Verilator's
  `-CFLAGS`.
- **Any `OBJCACHE` assignment**, including `OBJCACHE=` to skip ccache, also
  turns off [build reuse](FLOW-SIM.md#build-reuse), so
  every run rebuilds. To stop using the cache, set `enabled = false` under
  `[flows.sim.compiler_cache]` instead.

If the build warns `Verilator compiler cache unavailable: <reason>;
compiling uncached`, fix the named reason: ccache missing from the build
`PATH`, cache storage Booley can't create, or an unset cache root (run
`booley session refresh` on the host).

### Pre-Sim Commands fail with `Syntax error: Bad fd number`

Make runs recipes with `/bin/sh` (dash on Debian and Ubuntu), which doesn't
understand bash redirections such as `>& build.log`. Pass `SHELL=/bin/bash` on
the make command line in `[flows.sim].pre_run_commands`, or run it through
`bash -c`.

### RISC-V firmware won't assemble against the sandbox GCC

The RISC-V Sandbox Image ships GCC 15 (`riscv-none-elf-gcc`). Two `-march`
mismatches are common:

- **CSR instructions are missing.** GCC moved them out of the base ISA, so a
  project's `-march=rv32imc` fails on its startup code. Use
  `-march=rv32imc_zicsr` (for example `make ARCH=rv32imc_zicsr`).
- **A vendor ISA string is rejected**, such as `rv64imafdcxtheadc` (OpenC910).
  Only the vendor's own toolchain, built into a project image, accepts it.

Test the project's exact compile flags on one real file with
`riscv-none-elf-gcc -march=<theirs> ...` in the Sandbox before writing
configuration. The `booley-setup` skill plans that check during
setup (host doc, not shipped). Image contents:
[RISC-V toolchain image](SANDBOX.md#risc-v-toolchain-image-booley-sandbox-riscv).

## Lint, synthesis, and FPGA

### Lint or ASIC synth fails with an interface parameter mismatch

A top module with SystemVerilog interface ports (`my_axis_if.snk s_axis`)
can't be elaborated on its own: unbound interfaces take default parameters, and
the design's own checks fire, for example
`Error: Interface DATA_W parameter mismatch`. It isn't a bug in the IP; lint
and synthesis just elaborate the top alone. Doctor warns about such a top during
setup.

Add a thin flat-port wrapper: a module that creates the interfaces with real
parameters, instantiates the design, and exposes plain ports. Point the lint
and synthesis Targets at the wrapper. Simulation is unaffected.

### RTL simulates cleanly but `synth` rejects it under `slang`

The `slang` frontend is stricter than Verilator, and its error can name the
wrong thing. Known case: `$size()` on an array of interface instances
(`.S_COUNT($size(stat_if))`) fails with
`error: 'stat_if' cannot be used in an expression`. Put the count in a
`localparam` and use it in both the declaration and the instantiation. If you
don't need `slang`, stay on the default `sv2v` frontend; see
[the frontends](SUPPORTED-EDA-TOOLS.md#synth-rtl-frontend-sv2v-vs-slang).

### Yosys rejects transpiled Verilog after `sv2v` succeeds

`sv2v` can produce Verilog that Yosys can't parse, so the error points at a
line you never wrote. Known case: an array of interface instances inside a
generate block yields hierarchical function calls such as
`top.gen_x[0].u_router.my_pkg_MinBitWidth(2)` in port widths. Switch that
Target to the `slang` frontend, which reads interfaces natively: set
`frontend: slang` in the Target's `flow_options`, or ask the agent to run
`synth` with `--frontend slang` once.

### Yosys or Verilator fails with `Define not defined`

A header with project-wide `` `define``s must be compiled first, in the same
compilation unit as the files using them; macros don't carry over between
units. In the `.core`, list those headers first in the fileset.

### Host-provisioned Vivado will not start

Run `booley doctor` on the host, then `booley doctor --deep` in the Sandbox.
Check that the Project has a Grant, that the registered installation is still
a Linux x86-64 Vivado 2025.2, and that Doctor reports no devcontainer drift.
Fix the host side first, then apply it as [Applying Sandbox
changes](SANDBOX.md#applying-sandbox-changes) describes.
`booley eda installation list`, `booley eda license list` and `booley projects`
show the current records.

Don't expose a Docker socket, a host daemon, a license server address or a
license variable to the Sandbox to work around it; Booley doesn't support that.

If you deleted a Project that still has a Grant, don't recreate it. Copy its
exact path from `booley projects`, revoke the Grant, then forget the Project:

```bash
booley eda grant revoke --kind vivado /exact/deleted/project
booley projects forget /exact/deleted/project
```

## Coverage

### Coverage Campaign diagnostics

Where Campaign files live and how to delete finished runs:
[Coverage results](COVERAGE.md#reading-results)
and [Cleaning up reports](FLOW_REFERENCE.md#cleaning-up-reports).

| Message | Fix |
|---|---|
| `invocation N is still being produced` | A run is still writing it. Retry when it finishes. |
| `Simulation Campaign is being resumed` | A resume is running. Retry when it finishes. |
| `collection has no recorded result` | Coverage collection never finished, so the Campaign can't resume. Start the new run the message prints. |
| `Coverage evidence budget exhausted` | Narrow the instruction you gave the Coverage Analyst, or use a model with a larger context. |

Never delete a lock file to get past a message, and keep any empty
`.pruned-N` folders: they reserve used run numbers.

The Coverage Analyst gives report-only analysis, without Waiver Candidates,
when the Target's sources changed since the Campaign ran. The Campaign's
measured result stays the same.

<details markdown>
<summary>In stealth mode</summary>

The Coverage Analyst always gives report-only analysis, without Waiver
Candidates. See Stealth Mode (host doc, not shipped).

</details>

Coverage needs a Verilator simulation Target. Coverage controls such as reset
and hooks go in the Target's `flow_options.booley.coverage`. When evaluation is
`blocked`, read its findings; one invalid waiver for a Target stops all that
Target's waivers from applying.

### A Simulation Campaign will not resume

Pass the exact `campaign/manifest.json` path the run printed, not a Target name
or another file. Preview first; it changes nothing:

```bash
booley flow sim --resume-from /exact/path/to/campaign/manifest.json --dry-run
```

- **Integrity failure:** Campaign files were changed. Restore them exactly from
  that same Campaign, or start a new one; never edit hashes or copy files from
  another run.
- **Sources, `tests.toml`, Pre-Sim Commands or the Target changed:** start a new
  Campaign.
- **Unsupported manifest version:** start a new run with the current Booley.
- **`refusing to clean unowned run directory`:** move your own files out of the
  Target's run directory, then retry.

Never delete Booley's job or execution records to force progress; they protect
running EDA processes. To get rid of an abandoned Campaign, delete its run
folder once no run is active
([Cleaning up reports](FLOW_REFERENCE.md#cleaning-up-reports)).

### Waiver Candidates at review

How waivers are proposed and approved is in
[Coverage waivers at review](COVERAGE.md#coverage-waivers).

- **`[coverage.waivers] is not configured`:** set up the
  [approval directory](COVERAGE.md#waiver-directory-coveragewaivers)
  before you enter Goal Mode; `booley.toml` is protected during it.
- **A proposal stays pending:** cancelling or an invalid form decides nothing.
  Ask the agent to resume that proposal, or approve or reject it in chat with
  a reason.
- **A candidate is stale or invalid:** ask the agent to rerun coverage and the
  Coverage Analyst. Never copy a candidate into an approval file by hand.
- **Approved, but the Goal is still unmet:** approval alone doesn't meet a
  Goal. Ask the agent to rerun coverage, then check Goal status.

## Still stuck

Type `/booley-feedback` in the Developer Agent chat while the problem is still
on screen. It records how to reproduce it, checks Booley's code before blaming
it, and saves a report locally; see
When Booley itself misbehaves (host doc, not shipped).
Then [open an issue on GitHub](https://github.com/boldaxolotl/Booley/issues/new),
which is public, or email it privately to
[boldaxolotl@proton.me](mailto:boldaxolotl@proton.me).
