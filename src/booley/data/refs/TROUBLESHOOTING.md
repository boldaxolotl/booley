# Troubleshooting

For setup breakage, start with `booley doctor`. If you suspect agent-planted Git
configuration or hooks, read the [known Sandbox escape warning](https://booley.dev/docs/sandbox/#known-sandbox-escape-through-shared-git-metadata)
first: host-side Doctor can trigger that vulnerability.

`booley doctor --deep` additionally runs real sim/lint/synthesis smoke checks.
With a Developer Agent, invoke `/booley-heal` to drive that diagnosis-and-repair loop;
it uses this troubleshooting guide before improvising a fix and verifies both plain and deep
Doctor before calling the project healthy.
If Booley reports that its version changed, invoke `/booley-heal`.

For installation see [INSTALL.md](https://booley.dev/docs/install/), for project setup see
[SETUP.md](https://booley.dev/docs/setup/), for day-to-day driving see [USAGE.md](https://booley.dev/docs/usage/), and for
the config knobs named below see [CONFIG.md](https://booley.dev/docs/config/#where-each-setting-lives). For the Booley-specific
terms below (Sandbox, Target, EDA Provisioning, Specialist, Booley Flow, Developer
Agent) see the glossary in [GLOSSARY.md](https://github.com/boldaxolotl/Booley/blob/main/docs/GLOSSARY.md).

## Installation and editor integration

### Installation fails with `externally-managed-environment`

Recent distributions ship Python as an *externally managed* environment
(PEP 668), where system Python refuses user package installation. On Ubuntu or
Debian, install pipx from the distribution package; a pip-installed pipx fails
there because stock Python has no `ensurepip` until the `python3-venv` package,
which the `pipx` package depends on, is installed:

```bash
sudo apt install pipx
pipx ensurepath
```

Reopen your terminal, then install Booley and prepare the host:

```bash
pipx install booley-rtl
booley bootstrap
```

If you already have uv, use `uv tool install booley-rtl` instead.

### The wrong `booley` runs (stale install shadowing)

Host commands use the canonical installed Booley launcher. Compare
`booley --version` with `pipx list` or `uv tool list` for tool-managed installs;
use the intended interpreter's `python -m pip show booley-rtl` for pip installs.
An older launcher earlier on `PATH` can shadow the intended installation.

Run `type -a booley` (or `where.exe booley` on Windows) and remove or
upgrade older user or system installations that appear before the one you
intend to use. Restore the canonical installed Booley launcher, run
`pipx ensurepath` for pipx, and reopen the terminal. Then run
`booley bootstrap`, or `booley bootstrap --update` after an intentional upgrade.
Booley records the owning installation in `~/.config/booley/host-installation.json`
and refuses host commands from any other until `booley bootstrap --update`.

After a distro Python upgrade, repair pipx environments with
`pipx reinstall-all`. Once the intended launcher works, run
`booley bootstrap --update`: interpreter or distribution identity may have
changed even if the package version did not.

### VS Code says “A mount config is invalid” while reopening the container

Booley validates every host bind in the current generated spec before Docker
creates the Sandbox. The error names the missing or unavailable host
source and its container target; restore that source, or run `booley init
--seed` on the host when the source was intentionally removed.

If an existing container's bind list is stale, Booley recreates the container
from the current spec. Bind mounts and named volumes are kept; anything written
only inside the replaced container is lost. If Booley refuses because the
container is headless or ambiguous, stop and remove it yourself, then reopen.

### `booley` is missing from `/mcp` in Claude Code or Codex

Exit the agent CLI and start it again with `booley` so it re-reads its MCP config.
If it still doesn't appear, check the server is actually serving, from a
terminal inside the Sandbox:

```bash
curl -s -o /dev/null -w '%{http_code}' http://127.0.0.1:8814/mcp/
```

Note the **trailing slash**: the endpoint is a mount, so the bare `/mcp` answers
`307` (a redirect to `/mcp/`) and tells you nothing about the server behind it.
A healthy server answers the slashed URL with `406` — it wants an SSE `Accept`
header, which is exactly what a live MCP endpoint does; `200`/`405` are equally
fine. Connection refused means it's not up. Your *client* URL stays `/mcp` (the
redirect is transparent to it). The log is at `/tmp/booley_mcp_http.log`.
Restart the agent CLI first. If the service still does not return after the
Sandbox restarts, run `booley init --seed` on the host and rebuild or
reopen the container. A server that remains absent after that supported
lifecycle is a Booley bug; report it with the Doctor output and MCP log.

### A Booley skill name is occupied or points to an older installation

`booley bootstrap` preserves skill entries it cannot prove are safe to manage. A
live link whose complete skill tree matches the active package is accepted
without mutation. To move that equivalent link—or a link already recorded as
Booley-managed—to the active package, run `booley bootstrap --force`. The command
shows the current and requested targets before replacing the link. A link with
different content remains a conflict even under `--force`.

For other conflicts, inspect the named entry under `~/.agents/skills` or
`~/.claude/skills`. Keep it under another name when it is yours; remove it when
it is a dangling stale link, then rerun `booley bootstrap`.

Booley-managed links carry ownership metadata for later upgrades. A corrupt
`.booley-skill-links.json` fails closed instead of deleting links. Preserve the
file for diagnosis, repair or remove only the malformed metadata, and rerun
`booley bootstrap`.

### An MCP tool is missing from `/mcp`

Every valid built-in and custom MCP tool is discovered by default. To remove
a Booley Flow from Developer Agent discovery and autonomous execution, set
`[flows.<name>].enabled = false`; for a Specialist, set
`[specialists.<name>].enabled = false`.

For any other missing MCP tool:

1. Check that the applicable `[flows.<name>].enabled` or
   `[specialists.<name>].enabled` is not `false`.
2. For a custom MCP tool, fix Python syntax and make `name` and `description` literal
   class attributes so AST discovery can read them.
3. Request a fresh MCP tool list, or use the client's tool-list change
   notification channel. Installing, renaming, removing, or editing custom
   endpoint files and capability settings updates the running server without
   restarting the Sandbox.
4. Check whether the MCP server was deliberately narrowed for a Specialist or
   through the explicit `BOOLEY_MCP_TOOLS` environment filter.

### RTL opens as plain text, or Live Preview is missing

The Sandbox Image bundles the Verilog-HDL (highlighting) and Live Preview
extensions, and the generated devcontainer spec installs them from those files,
so they need no network. If one is missing in the attached window, install it
from the attached remote window's terminal:

```sh
code --install-extension /usr/local/libexec/booley/veriloghdl-1.29.0.vsix
code --install-extension /usr/local/libexec/booley/live-server-0.4.20.vsix
```

Then run **Developer: Reload Window**. If a file does not exist, refresh the
Sandbox Image. Live Preview needs VS Code 1.109 or newer (the minimum in
INSTALL.md); an older VS Code rejects that file. VS Code does not auto-update an extension installed
from a file. If Doctor reports devcontainer drift, refresh the Sandbox so its
spec installs the bundled files.

### `No space left on device` during an image build

The image recipe outgrew Booley's disk estimate. Free Docker space, for example
with `docker builder prune` (the next build may then rebuild the layers it
removed), and retry.

### Windows first-run problems

Windows-only first-run traps, from an outdated Git to Microsoft Store Python
aliases, are on the [Windows first-run problems](https://booley.dev/docs/windows/) page.

## Sandbox, Git, and worktrees

### Developer Agents and Specialists turn into "Not logged in" partway through an unattended run

The credential Claude Code and Codex ship with by default **refreshes, and refreshing
rotates it**. The host and the container hold copies of the same refresh token,
so an unrelated Claude Code or Codex session on the host can revoke a running container's copy
and turn every in-flight Developer Agent and Specialist call into "Not logged in" mid-run: the one failure
mode that bites long, unattended runs specifically.

The fix is a **rotation-free** credential: run `booley auth` for Claude or
`booley auth --app codex` for Codex, then **rebuild an existing container
once** so the credential mount exists. Billing options and credential choice
are in [USAGE.md](https://booley.dev/docs/usage/#auth--billing).

### Two Developer Agents keep clobbering each other's edits

Use one linked worktree per Developer Agent that edits code. Create it with
`booley worktree new <name>` in the Sandbox, then enter Goal Mode there with
`/booley-goal`. Tabs and terminals in the same worktree share files; the
Dashboard warns about shared-worktree activity but does not isolate it.

### Worktrees show as `prunable` on the host

Use Git inside the Sandbox to inspect its worktrees. A container path can be
unreachable from the host and appear `prunable` there. Do not prune an active
worktree based on that label. Inspect the Goal Record and preserve branch and
Project commits before removing a workspace. For Paired Checkout
removal, follow the absolute Git commands printed by `booley worktree new`.

### `git` cannot see files under `.booley/`

```
$ git checkout -- .booley/booley.toml
error: pathspec '.booley/booley.toml' did not match any file(s) known to git
```

`.booley/` is usually its own Git repository, and the outer repository
ignores it so your RTL history stays clean of Booley bookkeeping. Outer-repo
commands therefore cannot see anything inside it. Run them against the inner
repository instead:

```bash
git -C .booley checkout -- booley.toml     # restore Booley config
git -C .booley status                      # what changed in Booley's own repo
git -C .booley log --oneline -5
```

The same applies to `tests.toml`, `goalsets/*.md`, `criteria.toml`, and the `.core` files. If
`git -C .booley rev-parse --git-dir` errors, the directory is not a
repository on this machine and those files were never version-controlled: copy
one aside before you edit it.

### Doctor reports a Sandbox whose egress mode differs from the host

After you change
[`[sandbox] egress`](https://booley.dev/docs/sandbox/#host-configuration-configtoml),
host Doctor warns `host.sandbox-egress-mismatch` for every running Sandbox, in
any Project, still issued for the other mode. Recreate it from a fresh spec:

- Headless: run `booley session down --project <root>`, then
  `booley init --seed --project <root>`, then start the Sandbox again.
- VS Code: run `booley init --seed --project <root>`, then use Dev Containers:
  Rebuild Container in that Project's VS Code window.

To keep one running in the other mode deliberately, waive
`host.sandbox-egress-mismatch` with `subject` set to that Project's root, in the
`.booley/doctor-waivers.toml` of each Project where you run Doctor. Write a
Windows path as a single-quoted TOML string so its backslashes stay literal.
If Doctor instead fails with
`cannot check running Sandboxes against host egress mode`, fix what it names.

A stopped Sandbox that won't start after a switch to `"unproxied"` needs the
same recreation: run `booley session down`, then `booley session up`.

### Docker disk still used after removing a Project

Deleting a Project's directory leaves its Sandbox Images and agent state
volumes in Docker. `booley projects forget <root>` lists them with sizes but
removes nothing. `booley projects forget --purge <root>`, with the exact
absolute root, removes the unused ones, including the copy of the agent's login
and history in its state volumes. Both refuse while the root has a live Grant.

For images left by earlier forgets, run `booley projects prune-keepers`, then
`booley projects prune-keepers --confirm <digest>` with the digest it printed.
Purge keeps images marked `unrecorded`; remove one with `docker image rm <id>`
once no other Project needs it. See [`booley projects`](https://booley.dev/docs/cli-reference/#commands).

### Recover sources hidden by an interrupted older run

Doctor WARNs with `workspace.legacy-source-stash` when a stash record names this
worktree. It only reads the record. Recover manually inside the original
Sandbox, before rebuilding it or clearing its temp directory:

1. Back up the worktree, including untracked files.
2. Open the reported `booley_isolation_*.manifest.json` or
   `booley_hide_files_*.manifest.json`, including `.retired` records. Each
   `moved` pair lists the original path, then the stashed path under
   `stash_dir`.
3. If the stash directory has mode `000`, run `chmod u+rwx <stash-dir>`. Copy
   back only missing sources; never overwrite a present one.
4. Delete the stash and its record once the sources are confirmed.

## Goal Mode

### Goal evidence and state recovery

If `goal_status` says a Goal has no usable current evidence, rerun the producer
it names (ELAB: `sim mode=elab_only` on its Target; REVIEW: the Goal's review
category and focus). If saving Goal state keeps failing or the ledger reports
corruption, report it with the diagnostic and don't edit or delete state or
evidence files.

### Monitoring Goal work

Use `booley dashboard` inside the Sandbox for sessions, Goals, and Jobs.
Inspect records with `booley goal status`; ask the Developer Agent
for `goal_status(rules=true)` to recover evidence and rules after interruption.
Read the Review Package and Session Summary when Finish succeeds.

#### The Dashboard does not open on Sandbox attach

Use `booley dashboard` in the Sandbox to open it manually. Automatic opening uses
the **Booley Sandbox Integration** workspace extension bundled in the Sandbox Image, not
VS Code's global permission for automatic tasks. After upgrading Booley, run
`booley init` on the host and rebuild/reopen the Sandbox so its image contains
the extension. A running container does not gain files from a newer image.

Check **Output → Booley Dashboard** for an opt-out, task ownership conflict or
startup error. `[sandbox].dashboard = false`, an explicit user automatic-task
preference of off, and the folder opt-out in `.vscode/settings.json` suppress
opening. A deliberately removed or edited Dashboard task also stays an opt-out.
To restore a removed owned shortcut, set `dashboard = false`, run `booley init`
on the host, then set it to true and run init again.

Automatic opening works in Dev Containers and GitHub Codespaces windows. Local
windows and other remotes, such as SSH, WSL or tunnels, do not open it; the
Output channel names the skipped remote. Automatic opening requires a Git Project
Root. Untrusted windows wait for trust.
Headless sessions open no editor terminal. Closing the Dashboard does not restart
it in the same window session; reopen it manually or reload the window.

If no Output channel appears, check **Developer: Show Running Extensions** for
`booley.dashboard` running in the container. A disabled extension must be enabled
there. Doctor checks whether the bundled VSIX matches the installed Booley code;
missing/stale image artifacts require a Sandbox Image rebuild.

For a user-managed `[sandbox].image`, install the current Booley wheel and run
`RUN python3 -I -m booley.runtime.dashboard_extension` as root in the image recipe,
then restore its usual `USER`. Every Sandbox spec installs that VSIX, because the
same extension runs the Viewer Bridge behind `bwave gui` scoped views; an image
without it leaves both Dashboard auto-open and scoped views unavailable.
`booley dashboard` remains available manually.

#### A session shows `activity unknown`

The Dashboard shows the activity that Claude Code or Codex reports through
lifecycle hooks in the Sandbox Image. It never guesses, so a session is
listed under UNKNOWN, and its detail reads `Activity unknown`, when:

- the session has reported nothing since it first called Booley; the next
  prompt or tool call fills it in;
- the session has shown working or waiting for tool call for more than two
  hours without a new event, which usually means the client stopped mid-turn;
- the Sandbox Image has no activity hooks. `booley doctor` inside the Sandbox
  warns about this; rebuild the image, then rebuild and reopen the Sandbox;
- the agent client cannot be told apart from others in its worktree (the
  Dashboard row says the shared-worktree attribution is unavailable).

After you approve a permission request, the session keeps showing waiting for
user input until the tool finishes.

### A Goal that names Test Runs stays unmet

Simulation, Cycle Count and coverage Goals require exact Test Runs that Goal
Mode freezes at entry
([Goals that name Test Runs](https://booley.dev/docs/goal-mode/#goals-that-name-test-runs)).
`goal_status` shows each Goal's frozen Test Runs with their outcomes, and one
of these reasons when the Goal is unmet. A coverage Goal is decided by its
coverage floors alone, so it never shows `goal-test-run-incomplete`,
`goal-test-run-failed` or `goal-campaign-unaccounted`; it can be met while
`goal_status` lists a failed Test Run under it.

| Reason | Meaning | What to do |
|---|---|---|
| `goal-test-run-incomplete` | Some frozen Test Runs have not passed yet, or the evidence has no Test Run rows. | Run them. For a Goal that covers every Test of its Target, run the whole Target. For a Goal with a `test@xN` selector, run `--test` with that selector. For any other Goal, run `--test` with each frozen Test Run name that `goal_status` lists. |
| `goal-test-run-failed` | A frozen Test Run failed at the current file contents. A later pass at the same contents does not clear it, including when an interrupted run is recovered. | Fix the cause and run again. `goal_status` prints a rerun command for each failed Test Run. |
| `goal-test-run-pending` | The Goal names a Target, Test or Test Variant that does not exist yet. | Add it. Booley freezes the Test Run at the next Flow run or status read. |
| `goal-test-run-unresolvable` | The pending part cannot be frozen: `tests.toml` cannot be loaded or applied to the Target, the Goal names a seed and the Target is unseeded, or the Goal names a Test that `tests.toml` skips. | Fix `tests.toml` as the message says, or ask for a Goal change. |
| `goal-simulation-suite-empty` | The Goal covers every Test of its Target, and the Target runs none: every registered Test is skipped. | Register a Test, or remove it from `skip`. |
| `goal-test-run-missing` | A frozen Test Run's Target, Test or Test Variant is gone, its Test is skipped, or the Target cannot apply its seed. | Restore the declaration, or ask the Developer Agent to propose a `retarget`. When the message says the Test is skipped and the Goal names that Test, remove it from `skip` or ask for a Goal change that drops it; a retarget is refused. |
| `goal-test-run-changed` | The declared arguments or environment of a frozen Test Run changed in `tests.toml`. | Restore them, or ask the Developer Agent to propose a `retarget`. |
| `goal-test-run-metric-missing` | The latest run recorded Test Run outcomes but did not measure the Goal's Cycle Count or coverage. | Cycle Count: run the Goal's frozen Test Run. Coverage: run exactly the Goal's frozen Test Runs in one `--coverage` run. |
| `goal-campaign-unaccounted` | A Simulation Campaign that ran the Goal's Test Runs is still running, or its results cannot be read, so what it observed is not in the Goal's evidence yet. | Wait for the run to finish, or resume it, then read status again. A Simulation Campaign registration that cannot be read is moved aside only when you approve a Goal change. |

Don't edit or delete the Simulation Campaign records under the Goal Record's
`logs/acceptance/campaigns/`. An unreadable one shows
`goal-campaign-unaccounted`, and Booley moves it aside when you approve a Goal
change.

## Targets and simulation

### A `.core` library `depend:` won't resolve

A `depend:` on a core from a FuseSoC
*library* (`vlog_tb_utils`, `wiredelay`, …) fails in the sandbox: it ships zero
FuseSoC libraries, and the egress proxy blocks fetching them, so
`fusesoc run --setup` fails with a clear conflict naming the missing core.
Either vendor the dependency into the repo as a real `.core`, or drop it; often
the testbench already guards its use behind an `ifdef`, or defines the module
locally, making the dependency vestigial. Check before you strip.

### `flow sim` refuses a seed, a Test Variant or a Test Run selector

A Test Run is one Test Variant simulated with one seed, written
`test[+variant][@seed]`. `booley flow sim` checks every `--test` selector and
`--seed` before it builds anything. See
[Test Runs, Test Variants and seeds](https://booley.dev/docs/flow-sim/#test-runs-test-variants-and-seeds)
for the forms it accepts.

| Message | Cause | Fix |
|---|---|---|
| `--seed cannot be combined with a --test that names a seed` | The request names a seed twice: `--seed` and `test@seed` or `test@xN`. | Name the seed in one place. |
| `--seed applies to simulation, not to an elaboration mode` | `--seed` with `--mode elab-only` or `elab-only-standalone`. | Drop `--seed`. |
| `Target '<t>' is unseeded` | The Target cannot apply a seed: it is an Icarus Target with an HDL testbench and no `seed` template in `tests.toml`. | Add a `seed` template that the testbench reads, for example `seed = "+seed={seed}"`, or drop the seed. |
| `has no Test Variant` / `declares no Test Variants` | The selector names a Variant that `tests.toml` does not declare for that Test. | Use a declared Variant, or declare one under `[<target>.variants.<test>]`. |
| `selects Test Run '<name>' more than once` | Two selectors resolve to the same Test Run, such as `stress` and its default Variant `stress+short`. | Name each Test Run once. |
| `asks for more than 1000 Test Runs of Target` | One request may run at most 1000 Test Runs of one Target. | Split the request into several. |
| `seed count <N> exceeds the limit of 1000` | `test@xN` accepts N from 1 to 1000. | Use a smaller N. |
| `seed <n> is out of range` / `must not have leading zeros` | A seed is a decimal number from 1 to 2147483647 without a sign or leading zeros. | Write the seed in that form. |

`booley doctor` reports a `tests.toml` that cannot be loaded, or that a Target
cannot apply, as a `tests.toml` failure that names the Target and the key. The
rules are in [Tests (`tests.toml`)](https://booley.dev/docs/tests/).

### Simulation stalls at time zero without results

cocotb's VPI/VHPI run loop is compiled against the simulator, so pinning a prior
cocotb release does not guarantee compatibility with the image's current
Verilator. The failure can be silent: cocotb 1.5.1 under an older Verilator builds,
imports, and registers its VPI callbacks, but the timed callbacks never fire,
simulation time stays at `0.00 ns`, and the run consumes its timeout without
producing `results.xml`.

No Python-package pin fixes that simulator pairing. Usually the cheapest fix is
to modernize the testbench for cocotb 1.9+ or 2.x (`cocotb.fork` becomes
`cocotb.start_soon`, with a few import moves). The more expensive alternative is
a project image containing a mutually compatible prior Verilator and Python
stack. The supported selection dialects and current image versions are in
[SUPPORTED-EDA-TOOLS.md](https://booley.dev/docs/supported-eda-tools/#built-in-flows).

### Verilator reports timing or enum errors

The image ships Verilator 5. Check these diagnostics when a build fails:

- **`%Error-NEEDTIMINGOPT`** — the testbench uses delays or event controls
  (`#10` or `@(posedge clk)` in an event-driven testbench). Add `--timing` to
  the Target's `verilator_options`, or `--no-timing` for cycle-driven
  simulation.
- **`%Error-ENUMVALUE`** — a value outside an enum's declared members is assigned
  to or compared with an enum-typed variable, usually through an implicit
  conversion. Prefer an explicit RTL cast; if the use is intentional, waive it
  per Target with `-Wno-ENUMVALUE`.

Waiver lists must name the emitted warning code. Verilator 5 uses
`WIDTHTRUNC` and `WIDTHEXPAND` for width findings; a `-Wno-WIDTH` entry may
not silence the warning you expect. Read the code
from the failing `%Error-<CODE>` or `%Warning-<CODE>` line and either fix the RTL
or add the corresponding `-Wno-<CODE>` to that Target's `verilator_options`.

### Pre-Sim Commands fail with `Syntax error: Bad fd number`

Make defaults to `/bin/sh` (dash), where a recipe using `>&` redirection
(`make all >& build.log`) dies with `Syntax error: Bad fd number`. If the
project Makefile assumes bash, pass `SHELL=/bin/bash` on the make command
line (e.g. in a `[flows.sim].pre_run_commands` entry) or invoke via
`bash -c`.

### A pytest selftest under `.booley/` crashes at collection

A pytest-based selftest fixture under `.booley/` dies at collection when
the project's pytest config sets `--import-mode=importlib`:

```
TypeError: the 'package' argument is required to perform a relative import
for '.booley.selftest'
```

importlib mode derives a module name from the path, and the directory's leading
dot parses as a relative import. **Work around it** by passing
`--import-mode=prepend` on the pytest command line **for the selftest node
only**, leaving the project's configured mode untouched for its own test tree.
Any pytest fixture under a dotted directory hits this.

### RISC-V firmware won't assemble against the sandbox GCC

The RISC-V sandbox image ships a modern GCC (currently 15), and two `-march`
mismatches show up when building a core's boot software:

- **The base ISA lost its CSR instructions.** GCC 15 split the CSR
  instructions out of the base ISA, so an older project's default
  `-march=rv32imc` fails to assemble its own startup code. Add the explicit
  extension: `-march=rv32imc_zicsr` (e.g. `make ... ARCH=rv32imc_zicsr`).
- **A vendor ISA string is rejected outright.** A generic multilib GCC rejects
  vendor `-march` strings like `rv64imafdcxtheadc` (OpenC910); the only fix is
  baking the vendor's own toolchain into a project sandbox image.

Don't stop at "a RISC-V toolchain exists in the image". **Test the project's
exact compile flags** against the sandbox compiler on one real file before
writing config. Run the probe directly in the Sandbox shell:

```bash
riscv-none-elf-gcc -march=<theirs> ...
```

Better to plan for it during setup planning (Step 0 of the
[`booley-setup`](https://booley.dev/docs/setup/#the-booley-setup-skill) skill, whose setup plan
carries this probe on its execution-time checks list) than to learn it after
the config is written. For the image
contents and how to layer EDA tools on top, see
[SANDBOX.md → RISC-V toolchain image](https://booley.dev/docs/sandbox/#risc-v-toolchain-image-booley-sandbox-riscv).

### `bwave gui` fails on a scoped view

A scoped view (`--signals` / `--group` / `--time` / `--cursor` / `--append`)
needs a VS Code window attached to the Sandbox. A bare `bwave gui` doesn't.

When a window is attached, the usual fix is **"Developer: Reload Window"** in
it, not a rebuild: the bridge starts when the window loads. If Doctor also
reports devcontainer drift, refresh the Sandbox so its spec installs the
Booley Sandbox Integration extension. `booley doctor` asks every attached
window's Viewer Bridge whether it answers and has VaporView ready ("Viewer
Bridge answers in the attached VS Code window ..."), so you find out before
`bwave gui` does.

With several VS Code windows attached to one Sandbox, `bwave gui` uses the
window whose terminal you typed it in. Elsewhere it picks the window already
showing the trace, then the window you used most recently. When it cannot
tell, it says so: run it from a terminal of the window you want, or close the
windows you do not need.

Booley checks whether VaporView is installed in the attached remote window.
The Sandbox Image bundles the verified VaporView 1.6.0 build, and the generated
devcontainer spec installs it from that file, so it needs no network. If it is
missing, install it from the attached remote window's terminal with
`code --install-extension /usr/local/libexec/booley/vaporview-1.6.0.vsix`;
if that file does not exist, refresh the Sandbox Image.
After installation, the Sandbox's background watcher patches the extension
automatically; then use **Developer: Reload Window**. Verify with
`code --list-extensions --show-versions` in the remote terminal; it must list
`lramseyer.vaporview@1.6.0`. Be careful with a host terminal: a plain host
`code` command may install locally instead of into the attached container.

VS Code does not auto-update an extension installed from a file. A different
installed version, such as a newer Marketplace release installed by hand,
cannot drive scoped views. `bwave gui` and Doctor report it with both
versions. Run the install command above in the attached remote window, then
**Developer: Reload Window**. If Doctor also reports devcontainer drift,
refresh the Sandbox so its spec installs the bundled build.

On the first window of a fresh container, Booley's VaporView fixes (theme
lookup, `@red`/`@blue`/`@green` colors) take effect only after **Developer:
Reload Window**. If an "Error getting user theme" notification persists, see
`~/.booley/vaporview-install-watch.log`.

A view that opens but is missing signals is a different thing: signals the
viewer has no netlist entry for are left out and named in a WARNING on stderr;
the trace itself is fine and still queryable. A `--signals` glob that matches
nothing in the trace is named in a WARNING the same way; check the full path
with `bwave list`. If `bwave gui` reports that the
viewer "did not apply" the requested view, it has already restored the
previous view; retry once the window is idle (the viewer may still have been
loading the trace).

## Lint, synthesis, and FPGA

### Lint or ASIC synth fails with an interface parameter mismatch

If the module you point `lint` or `synth` at carries **SystemVerilog
interface ports** (`my_axis_if.snk s_axis`), especially if it reads their
parameters hierarchically (`localparam W = s_axis.DATA_W`), it **cannot be
elaborated standalone**. With no interface bound, its ports take the interface's
*default* parameters and the design's own elaboration checks fire
(`Error: Interface DATA_W parameter mismatch`). That reads like a bug in the IP,
but isn't: both flows elaborate the toplevel on its own, and upstream IP rarely
ships a standalone-elaboratable top because in-tree it's only ever instantiated
inside a board design or testbench that supplies the interfaces.

Fix: add a thin **flat-port wrapper**, a module that declares the interface
instances with real parameters, instantiates the DUT, and exposes plain signals
on its own ports, then point `lint` and `synth` at the wrapper. `sim` is
unaffected: a cocotb testbench brings its own wrapper, since its BFMs must bind
to interface *instances* anyway. `booley doctor` flags this at setup time. Cost:
one small file.

### RTL simulates cleanly but `synth` rejects it under `slang`

The `slang` frontend is stricter than Verilator, so RTL that simulates fine can
still fail at synthesis, and the error often names the wrong culprit. The known
case: `$size()` on an array of interface instances
(`my_axis_if #(...) stat_if[2]();` used as `.S_COUNT($size(stat_if))`) fails
with `error: 'stat_if' cannot be used in an expression`, naming the array rather
than the `$size` call. **Workaround:** hoist the count into a named constant
(`localparam STAT_CNT = 2;`) and use it at both the declaration and the
instantiation. This is not a Booley bug and not a dead end; the fix is usually a
two-line RTL change. The running list of `slang` limitations is in
[SUPPORTED-EDA-TOOLS.md](https://booley.dev/docs/supported-eda-tools/#synth-rtl-frontend-sv2v-vs-slang).
If you don't need `slang`, stay on the default `sv2v` frontend.

### Yosys rejects transpiled Verilog after `sv2v` succeeds

`sv2v` is a source-to-source transpiler, so some failures appear only when Yosys
parses the transpiled Verilog; the error can point at a line you never wrote. A
known case is an array of internal interface instances, such as
`router_if r_if[N]();` inside `gen_x[…].u_router`. It can produce port-width
expressions containing hierarchical function calls such as
`top.gen_noc_x_lines[0].u_router.my_pkg_MinBitWidth(2)`, which Yosys's Verilog
parser rejects even though `sv2v` exited successfully.

Switch the synthesis Target to `frontend = "slang"`, which reads the interfaces
natively. This is an upstream `sv2v` limitation rather than something the flow
can repair. Frontend selection and requirements are in
[SUPPORTED-EDA-TOOLS.md](https://booley.dev/docs/supported-eda-tools/#synth-rtl-frontend-sv2v-vs-slang).

### Yosys or Verilator fails with `Define not defined`

A header that defines project-wide macros (e.g. `` `PA_WIDTH ``) relies on being
compiled **first, in the same compilation unit**; `` `define `` does not persist
across separate `read_verilog` calls or separate Verilator units. Feeding files
one-per-`read_verilog` (Yosys) or as separate units breaks with `Define not
defined`. Compile the whole filelist (headers first) as **one** unit — in a
`.core`, list the `` `define `` headers first in the fileset.

### Host-provisioned Vivado will not start

Run `booley doctor` on the host, then rerun it with `--deep` inside the Sandbox. Confirm that the Project has an exact Grant, the registered Vivado
installation still points to a supported Linux x86-64 Vivado 2025.2
release, and the generated Sandbox specification has not drifted. Reseed the
Sandbox only after the host authority has been corrected.

Do not attempt to expose a host daemon, a Docker socket, a direct license-server
address, or a license environment variable to repair this. These are
unsupported. Use `booley eda installation list`, `booley eda license list`, and
`booley projects` to inspect administrator-owned records and see each Project's
Grants together.

If a granted Project Root was deleted, do not recreate it just to revoke
authority. Run `booley projects`, copy the missing root's exact absolute path,
then run:

```bash
booley eda grant revoke --kind vivado /exact/deleted/project
```

After every Grant for that root is gone, remove the obsolete inventory entry
with `booley projects forget /exact/deleted/project`.

## Coverage

### Coverage Campaign diagnostics and retention

To find a Campaign's files or prune them, see
[Where the evidence lives](https://booley.dev/docs/coverage/#where-the-evidence-lives)
and [Cleaning up completed Campaigns](https://booley.dev/docs/coverage/#cleaning-up-completed-campaigns).

| Message | What to do |
|---|---|
| `availability.json` status `pruning` | Cleanup was interrupted. Fix the filesystem error, then rerun the same cleanup command. |
| `invocation N is still being produced` | A run is still writing it. Wait, then retry. |
| `Simulation Campaign is being resumed` | A resume owns the Campaign. Wait for it to finish. |
| `collection has no recorded result` | Coverage collection never finished, so the Campaign can't resume. Start a new `booley flow sim --coverage` run; the message prints the command. |

Never delete a lock file to get past these. Keep empty `.pruned-N` directories:
they reserve used invocation numbers.

#### A Simulation Campaign will not resume

Always pass the exact printed `campaign/manifest.json` path. Do not pass a
Target name, `simulation.json`, `summary.json`, `coverage.json`, an invocation
directory, or a guessed `latest` path. Use a read-only preview first:

```bash
booley flow sim --resume-from /exact/path/to/campaign/manifest.json --dry-run
```

An integrity error is intentionally fail-closed. Truncated, resized, linked,
noncanonical, digest-mismatched, or identity-mismatched Campaign evidence must
be restored byte-for-byte from the same Campaign; do not hand-edit hashes or
copy evidence from another run. If source, `tests.toml`, Pre-Sim Commands,
`run_cwd`, runtime inputs, build-access mode, or the resolved Target revision
changed, start a new Campaign instead of weakening the mismatch check. An
unsupported manifest version must be rerun with the current Booley version;
Booley never rewrites an older immutable manifest in place.

If resume refuses to clean a templated `run_cwd` directory, inspect it and move
any foreign data out of the configured path, then retry.
Never delete Project job-slot or execution records to force progress:
their leases protect live EDA process trees.

Discard an abandoned Campaign with `--full` pruning (see
[Cleaning up completed Campaigns](https://booley.dev/docs/coverage/#cleaning-up-completed-campaigns));
add `--project-data <resolved-booley-directory>` only if it asks.

#### Coverage Analyst input and model availability

Call the `coverage_analyst` Specialist from your Developer Agent session with
`campaign="<reports>/sim/<number>/targets/<target>/coverage.json"`.
Target names, `latest`, the point-store path, waveforms, and flat
`coverage_report.json` are not Analyst inputs. A missing, malformed, or invalid
point store makes a Campaign unusable for analysis. Recollect unsupported
Campaigns with the current Booley version. A missing or incomplete
matching `simulation.json` means that Target is
not ready for analysis; another Target still running does not block a completed one.
Native-payload pruning preserves analysis. Full-invocation pruning removes the
Campaign, so select another retained invocation or collect new evidence.

The Analyst requires that canonical Target-level reference with matching completed
`simulation.json`; it does not accept the resolved nested manifest.

Missing, changed, unsafe, or mismatched Target sources produce report-only analysis.
Stealth-mode projects also use report-only analysis because resolving their sources
requires FuseSoC registry reconciliation, which would change project files.
This does not change the Campaign's measured verdict. The Analyst does not reuse
mutable `coverage_waivers.json` files or approve its candidates.

If the evidence budget or model context is exhausted, narrow the instruction or
use a larger-context model.

Codex analysis requires cached metadata for the exact configured model. If that
metadata is missing, start the configured Codex CLI to refresh model discovery,
then retry. Analysis fails closed instead of starting a Specialist with execution
capabilities.

#### Coverage collection and evaluation failures

- Preflight errors: select only Verilator Simulation Targets, use simulation
  mode, and place reset/hook controls in `flow_options.booley.coverage`.
- Hook errors: each process must prove the required exactly-once calls. With
  reset excluded, start after reset and before write. A missing, duplicate,
  failed, or out-of-order hook invalidates collection.
- Native errors: inspect per-run raw paths and query/merge evidence in the exact
  Target directory. Missing, stale, malformed, incompatible, or non-equivalent
  merged evidence is a command error. Re-run to create a new invocation.
- `blocked` evaluation: inspect structured findings for suite mismatch, zero
  eligible denominator, unavailable metrics, or invalid/stale/unmatched approvals
  for that Target. Approval matching is transactional per Target: one invalid
  point approval prevents all approvals for that Target from applying. Approvals
  naming a known Target outside the invocation are not checked against points by
  that run; unknown Target identities are rejected when the Approved Waiver Set
  is loaded. An infrastructure or persistence error can also block a requested
  evaluation without producing coverage findings. In that case, inspect the
  Target's `error` and `collection` status, correct the failure, and rerun.
- Coverage Goals use a Target-bound argument such as
  `{"family":"COVERAGE","target":"sim_core","tests":"all","metrics":{"toggle": 50}}`;
  replace `all` with an exact registered suite when needed. Choose only a
  supported native metric.
- Missing `sim_<target>.json`: follow the exact numbered report pointer.

#### Waiver Candidates at review

- Coverage remains unmet after Analyst screening: candidates are proposals,
  not approvals. Ask the Developer Agent to use `goal_propose_change(kind="waiver")` and
  inspect the exact candidate before deciding.
- `[coverage.waivers] is not configured`: select the
  [approval directory](https://booley.dev/docs/coverage/#approved-coverage-waivers) before approval.
  Configuration is protected during Goal Mode; arrange it before entry.
- A proposal remains pending: a cancelled or invalid form makes no decision.
  Resume its exact proposal ID; if chat fallback is needed, give an explicit
  approve/reject instruction and reason.
- A candidate is stale or invalid: rerun coverage and Analyst screening for
  the current source and Campaign. Do not copy candidate bytes into approval files.
- Approval succeeded but the Goal is unmet: rerun coverage for fresh strict
  evidence, then inspect `goal_status`. Approval alone cannot satisfy it.
