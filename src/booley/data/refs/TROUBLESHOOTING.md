# Troubleshooting

Start with `booley doctor`; it catches setup breakage on its own, and `booley
doctor --deep` additionally runs real sim/lint/synthesis smoke checks. What
follows is the residue Booley cannot safely repair itself: upstream constraints,
host policy, project-specific intent, and third-party lifecycle behavior.
With an agent, invoke `/booley-heal` to drive that diagnosis-and-repair loop;
it uses this troubleshooting guide before improvising a fix and verifies both plain and deep
Doctor before calling the project healthy.
Run both `booley doctor` and `/booley-heal` after every Booley version update so
version-related drift is found, repaired, and fully verified.

For installation see the [README](https://github.com/boldaxolotl/Booley#installation), for project setup see
[SETUP.md](https://github.com/boldaxolotl/Booley/blob/main/docs/user/SETUP.md), for day-to-day driving see [USAGE.md](https://github.com/boldaxolotl/Booley/blob/main/docs/user/USAGE.md), and for
the config knobs named below see [CONFIG.md](https://github.com/boldaxolotl/Booley/blob/main/docs/user/CONFIG.md). For the Booley-specific
terms below (Session Runtime, Target, EDA Provisioning, Specialist, Booley Flow, Developer
Agent) see the glossary in [CONTEXT.md](https://github.com/boldaxolotl/Booley/blob/main/docs/CONTEXT.md).

## VS Code says “A mount config is invalid” while reopening the container

Booley validates every host bind in the current generated spec before Docker
creates the Session Runtime. The error names the missing or unavailable host
source and its container target; restore that source, or run `booley init
--seed` on the host when the source was intentionally removed.

A rebuild may otherwise select a stopped VS Code container whose old bind list
still mentions a deleted skill, credential file, tool installation, mask
directory, or editor-injected socket. During `booley session prepare`, Booley
now removes such a stopped container (without deleting named volumes) so Dev
Containers creates one from the current spec. It never removes a running
container, a headless `booley session` container, or a container belonging to a
different Project. If one is running with an older issuance, stop it and retry
the rebuild.

## `booley` is missing from `/mcp` in Claude Code or Codex

Run **Developer: Reload Window** so the agent session re-reads its MCP config.
If it still doesn't appear, check the server is actually serving, from a
terminal inside the Session Runtime:

```bash
curl -s -o /dev/null -w '%{http_code}' http://127.0.0.1:8814/mcp/
```

Note the **trailing slash**: the endpoint is a mount, so the bare `/mcp` answers
`307` (a redirect to `/mcp/`) and tells you nothing about the server behind it.
A healthy server answers the slashed URL with `406` — it wants an SSE `Accept`
header, which is exactly what a live MCP endpoint does; `200`/`405` are equally
fine. Connection refused means it's not up. Your *client* URL stays `/mcp` (the
redirect is transparent to it). The log is at `/tmp/booley_mcp_http.log`.
Reload the VS Code window first. If the service still does not return after the
Session Runtime restarts, run `booley init --seed` on the host and rebuild or
reopen the container. A server that remains absent after that supported
lifecycle is a Booley bug; report it with the Doctor output and MCP log.

## A Booley skill name is occupied or points to an older installation

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

## An MCP tool is missing from `/mcp`

Every valid built-in and custom MCP tool is discovered by default. The old
`[tools].builtin` and `[tools].custom` keys are migration errors and Doctor
rejects them. To remove a Booley Flow from agent and autonomous discovery, set
`[flows.<name>].enabled = false`; for a Specialist or other non-Flow endpoint,
set `[mcp_tools.<name>].enabled = false`.

Two intentional visibility cases remain. Interactive Mode hides
`submit_run_report` because it finalizes autonomous Ticket runs. `tb_coder` is
currently de-registered in every mode while the Developer Agent authors
testbench code directly. A direct `booley flow` diagnostic run deliberately
ignores both the project `enabled` discovery filter and Interactive MCP hiding,
so it can list an implementation that an agent cannot. An individual Booley Flow may
still report its configured flow as disabled when invoked directly.

For any other missing MCP tool:

1. Check that the applicable `[flows.<name>].enabled` or
   `[mcp_tools.<name>].enabled` is not `false`.
2. For a custom MCP tool, fix Python syntax and make `name` and `description` literal
   class attributes so AST discovery can read them.
3. Restart the Session Runtime after adding or renaming the file.
4. Check whether the MCP server was deliberately narrowed for a nested agent or
   through the explicit `BOOLEY_MCP_TOOLS` environment filter.

See [MCP-TOOLS.md](https://github.com/boldaxolotl/Booley/blob/main/docs/internals/MCP-TOOLS.md#default-discovery-and-explicit-opt-out) for the complete
discovery model.

## `bwave gui` fails on a scoped view

A scoped view (`--signals` / `--time` / `--cursor` / `--append`) drives a
*running* VaporView over its WCP (Waveform Control Protocol) control server —
the channel `bwave` uses to drive an already-open viewer — which the generated
devcontainer spec enables and pins to port 54322 (override: `BOOLEY_WCP_PORT`).
If nothing is listening there, the scoped view fails with a setup hint,
deliberately, rather than silently opening the whole trace and letting you read
the wrong picture. A bare `bwave gui` needs no WCP server: it falls back to
launching the editor CLI on the file.

Usually the fix is **"Developer: Reload Window"**, not a rebuild. VaporView only
wakes up for a waveform tab, so Booley patches its manifest to start on every
window instead — but that patch runs from `postAttachCommand`, which VS Code
runs *after* it has already started the extension host. On the first window of a
fresh container the patch therefore lands one beat too late and takes effect on
the next extension-host start. A rebuild puts you back in exactly that first
window; one reload does not. `booley doctor` probes the port and says so
("VaporView WCP server reachable ..."), so you find out before `bwave gui` does.

Do **not** use `WCP: Start Server` while Booley's auto-start setting is enabled.
VaporView 1.5.4 starts WCP during command-triggered activation but loses that
server reference, so the command tries the same port again and falsely reports
`EADDRINUSE` even though the first start succeeded. Booley disables that command
in auto-start mode after the manifest patch takes effect; Reload Window is the
reliable first-window recovery.

A view that opens but is missing signals is a different thing: signals the viewer
has no netlist entry for are dropped and named in a WARNING on stderr; the trace
itself is fine and still queryable.

## `pip install booley-rtl` fails with `externally-managed-environment`

Recent distributions ship Python as an *externally managed* environment
(PEP 668), where a plain `pip install` into the system interpreter refuses with
`externally-managed-environment`. Install with **`pipx`** instead: it puts the
CLI in its own virtualenv and links the `booley` executable into `~/.local/bin`
(already on `PATH` on most systems).

```bash
pipx install booley-rtl
```

## The wrong `booley` runs (stale install shadowing)

`booley init` and `booley session ...` are host commands that shell out to
Docker, so whichever environment you install into has to be the one your shell
resolves `booley` from. If `booley --version` and `pip show booley-rtl`
**disagree**, an older install is shadowing this one on `PATH`.

Run `command -v -a booley` (or `where.exe booley` on Windows) and remove or
upgrade older `pipx`, user, or system installations that appear before the one
you intend to use. Reinstalling with `pipx install --force booley-rtl` restores
the normal isolated CLI entry point.

## Windows first-run problems

The CLI runs **natively on Windows** (repo on `C:\...`, Windows Python), not
from inside WSL. Docker Desktop's WSL2 backend only hosts the containers. Four
first-run traps:

- **`python`/`py` open the Microsoft Store instead of running.** A fresh
  Windows has no real interpreter, only Store aliases. Install one first:
  `winget install Python.Python.3.13`.
- **`docker` isn't found even though Docker Desktop is installed.** Its CLI
  joins `PATH` only after the app has started: launch Docker Desktop, then open
  a **new** terminal before `booley init`. Init treats a missing CLI or stopped
  daemon as fatal and exits before creating or changing project files.
- **The container sees a fully modified tree / phantom diffs.** Git for
  Windows' `core.autocrlf=true` default checks files out with CRLF, which the
  Linux container reads as every file modified. `booley init` handles it: it
  inspects both the Project checkout and the resolved project-data directory
  when they are separate Git repositories. In each repository it sets
  `core.autocrlf=false` locally and adds `* text=auto eol=lf` as the first line
  of `.gitattributes`. `text=auto` preserves Git's binary-file detection, while
  the first-line position lets any more-specific rule below it still win.
  **Commit each `.gitattributes`**: the rule only reaches your team through git.

  Files already on disk with CRLF are a separate matter. From a clean tracked
  tree, init stages Git-filtered LF replacements, verifies that the affected
  files have not changed since inspection, then rewrites their content in place
  and reconciles Git's cached metadata for only those paths. This preserves
  filesystem metadata, leaves the index content unchanged, and leaves untracked
  and unaffected tracked files alone. Init refuses dirty trees, Git-protected
  affected paths, and hard-linked candidates; commit or stash changes and rerun:

  ```bash
  booley init
  ```

  `booley doctor` re-asks both repositories every run and identifies which one
  is unsafe, so a config reset, a fresh clone that drifts back to CRLF, or stale
  index metadata left by an earlier repair gets caught rather than surfacing as
  phantom diffs in the container. The old
  `--fix-line-endings` option remains accepted for CLI compatibility but is no
  longer required for a clean tree.

  (Doing this by hand is fiddlier than it looks: `git checkout -- .` on its own
  is **not** enough. With the clean filter in place the worktree files already
  match the index, so Git decides nothing needs rewriting and leaves the CRLF on
  disk. Init materializes filtered replacements separately and applies them only
  after all safety checks pass; it never deletes the originals.)

- **The first sandbox image build takes over an hour.** First builds compile
  EDA tools from source and can take well over an hour on a WSL2-backed Docker;
  the build timeout is overridable via `BOOLEY_IMAGE_BUILD_TIMEOUT` (seconds,
  default 7200).

## Lint or ASIC synth fails with an interface parameter mismatch

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

## A `.core` library `depend:` won't resolve

A `depend:` on a core from a FuseSoC
*library* (`vlog_tb_utils`, `wiredelay`, …) fails in the sandbox: it ships zero
FuseSoC libraries, and the egress proxy blocks fetching them, so
`fusesoc run --setup` fails with a clear conflict naming the missing core.
Either vendor the dependency into the repo as a real `.core`, or drop it; often
the testbench already guards its use behind an `ifdef`, or defines the module
locally, making the dependency vestigial. Check before you strip.

## RISC-V firmware won't assemble against the sandbox GCC

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
writing config:

```bash
booley shell -- riscv-none-elf-gcc -march=<theirs> ...
```

Better to plan for it during setup planning (Step 0 of the
[`booley-setup`](https://github.com/boldaxolotl/Booley/blob/main/docs/user/SETUP.md#the-booley-setup-skill) skill, whose setup plan
carries this probe on its execution-time checks list) than to learn it after
the config is written. For the image
contents and how to layer EDA tools on top, see
[CONFIG.md → RISC-V toolchain image](https://github.com/boldaxolotl/Booley/blob/main/docs/user/CONFIG.md#risc-v-toolchain-image-booley-sandbox-riscv).

## Agents turn into "Not logged in" partway through an unattended run

The credential both agent apps ship with by default **refreshes, and refreshing
rotates it**. The host and the container hold copies of the same refresh token,
so an unrelated agent session on the host can revoke a running container's copy
and turn every in-flight agent into "Not logged in" mid-run: the one failure
mode that bites long, unattended runs specifically.

The fix is a **rotation-free** credential, stored by `booley auth`:

| app | rotation-free credential | how |
|---|---|---|
| Claude | one-year OAuth token (never refreshes) | `booley auth`, which runs `claude setup-token` for you |
| Codex | API key (`OPENAI_API_KEY`) | `booley auth --app codex`, then paste the key |

`booley auth` writes the credential to `~/.config/booley/` (mode 0600,
deliberately outside every repo and bind mount so it cannot be committed) and
re-seeds the devcontainer spec; Booley then injects it on every container start.
**Rebuild an existing container once** so the read-only mount exists.
`booley auth --status` reports which credential each agent would use, and
`booley doctor` warns when a run is about to rely on a refreshing one. Full
billing and precedence detail is in [USAGE.md](https://github.com/boldaxolotl/Booley/blob/main/docs/user/USAGE.md#auth--billing).

## Two interactive agents keep clobbering each other's edits

**Tickets get their own git worktree automatically**, so ticket runs never step
on each other's tree. **Interactive tabs and terminals don't**: they all share
the repo you opened, so two interactive agents editing the same worktree trip
over each other's changes. When you want an interactive agent to work in
parallel with others, tell it up front to create a fresh worktree and work
there. (Background on the two modes: [USAGE.md](https://github.com/boldaxolotl/Booley/blob/main/docs/user/USAGE.md#interactive-mode).)

## RTL simulates cleanly but `synth` rejects it under `slang`

The `slang` frontend is stricter than Verilator, so RTL that simulates fine can
still fail at synthesis, and the error often names the wrong culprit. The known
case: `$size()` on an array of interface instances
(`my_axis_if #(...) stat_if[2]();` used as `.S_COUNT($size(stat_if))`) fails
with `error: 'stat_if' cannot be used in an expression`, naming the array rather
than the `$size` call. **Workaround:** hoist the count into a named constant
(`localparam STAT_CNT = 2;`) and use it at both the declaration and the
instantiation. This is not a Booley bug and not a dead end; the fix is usually a
two-line RTL change. The running list of `slang` limitations (and where to add a
new one) is in
[SUPPORTED-EDA-TOOLS.md](https://github.com/boldaxolotl/Booley/blob/main/docs/user/SUPPORTED-EDA-TOOLS.md#synth-rtl-frontend-sv2v-vs-slang).
If you don't need `slang`, stay on the default `sv2v` frontend.

## Simulation stalls at time zero without results

cocotb's VPI/VHPI run loop is compiled against the simulator, so pinning an old
cocotb release does not guarantee compatibility with the image's current
Verilator. The failure can be silent: cocotb 1.5.1 under Verilator 5.046 builds,
imports, and registers its VPI callbacks, but the timed callbacks never fire,
simulation time stays at `0.00 ns`, and the run consumes its timeout without
producing `results.xml`.

No Python-package pin fixes that simulator pairing. Usually the cheapest fix is
to modernize the testbench for cocotb 1.9+ or 2.x (`cocotb.fork` becomes
`cocotb.start_soon`, with a few import moves). The more expensive alternative is
a project image containing a mutually compatible old Verilator and Python
stack. The supported selection dialects and current image versions are in
[SUPPORTED-EDA-TOOLS.md](https://github.com/boldaxolotl/Booley/blob/main/docs/user/SUPPORTED-EDA-TOOLS.md#built-in-flows).

## Yosys rejects transpiled Verilog after `sv2v` succeeds

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
[SUPPORTED-EDA-TOOLS.md](https://github.com/boldaxolotl/Booley/blob/main/docs/user/SUPPORTED-EDA-TOOLS.md#synth-rtl-frontend-sv2v-vs-slang).

## Verilator 5 rejects Verilator-4-era RTL

The image ships Verilator 5. Designs and testbenches written for Verilator 4
usually build, but checks that used to be warnings can now stop the build:

- **`%Error-NEEDTIMINGOPT`** — the testbench uses delays or event controls
  (`#10` or `@(posedge clk)` in an event-driven testbench). Add `--timing` to
  the Target's `verilator_options`, or `--no-timing` for the old cycle-driven
  behavior.
- **`%Error-ENUMVALUE`** — a value outside an enum's declared members is assigned
  to or compared with an enum-typed variable, usually through an implicit
  conversion. Prefer an explicit RTL cast; if the use is intentional, waive it
  per Target with `-Wno-ENUMVALUE`.

Old waiver lists can also name codes that no longer cover the same findings.
Verilator 5 split the old `WIDTH` warning into `WIDTHTRUNC` and `WIDTHEXPAND`, so
an inherited `-Wno-WIDTH` may not silence the warning you expect. Read the code
from the failing `%Error-<CODE>` or `%Warning-<CODE>` line and either fix the RTL
or add the corresponding `-Wno-<CODE>` to that Target's `verilator_options`.

## A pre-run command dies with `Syntax error: Bad fd number`

Make defaults to `/bin/sh` (dash), where a recipe using `>&` redirection
(`make all >& build.log`) dies with `Syntax error: Bad fd number`. If the
project Makefile assumes bash, pass `SHELL=/bin/bash` on the make command
line (e.g. in a `[flows.sim].pre_run_commands` entry) or invoke via
`bash -c`.

## Yosys or Verilator fails with `Define not defined`

A header that defines project-wide macros (e.g. `` `PA_WIDTH ``) relies on being
compiled **first, in the same compilation unit**; `` `define `` does not persist
across separate `read_verilog` calls or separate Verilator units. Feeding files
one-per-`read_verilog` (Yosys) or as separate units breaks with `Define not
defined`. Compile the whole filelist (headers first) as **one** unit — in a
`.core`, list the `` `define `` headers first in the fileset.

## A pytest selftest under `.booley_project/` crashes at collection

A pytest-based selftest fixture under `.booley_project/` dies at collection when
the project's pytest config sets `--import-mode=importlib`:

```
TypeError: the 'package' argument is required to perform a relative import
for '.booley_project.selftest'
```

importlib mode derives a module name from the path, and the directory's leading
dot parses as a relative import. **Work around it** by passing
`--import-mode=prepend` on the pytest command line **for the selftest node
only**, leaving the project's configured mode untouched for its own test tree.
Any pytest fixture under a dotted directory hits this.

## Host-provisioned Vivado will not start

Run `booley doctor` on the host, then rerun it with `--deep` inside the Session
Runtime. Confirm that the Project has an exact Grant, the opaque Vivado
installation registration still points to a supported Linux x86-64 Vivado 2025.2
release, and the generated runtime specification has not drifted. Reseed the
runtime only after the host authority has been corrected.

Do not attempt to expose a host daemon, a Docker socket, a direct license-server
address, or a license environment variable to repair this. Those paths are not
part of the product and a failed registration/licensing check deliberately fails
closed. Use `booley eda installation list`, `booley eda license list`, and
`booley eda grant list` to inspect administrator-owned records.
