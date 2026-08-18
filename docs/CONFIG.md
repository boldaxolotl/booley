# Configuration reference

Reference material for Booley's configuration files and advanced setups. This
is the "what each knob means" companion to [SETUP.md](SETUP.md), which is the
ordered list of setup **steps**. You do not fill these files by hand: the
`booley-setup` skill authors them interactively, and `booley doctor` validates
them. When a step needs the meaning of a field, it links here.

**Audience:** both humans and agents. A human can read this to understand the
config surface; an agent running the setup skill dereferences these anchors as it
fills each file.

**Read this first.** This is a reference, not a tutorial. It assumes you have
skimmed the [README](../README.md) overview and know Booley's controlled
vocabulary: terms used here without definition — **Target**, **Booley Flow**,
**EDA Provisioning**, **Session Runtime**, **Specialist**, **Developer Agent**, **VLNV** —
are all defined in the glossary, [CONTEXT.md](CONTEXT.md); keep it open. Every
project has exactly two mandatory pieces: `.booley_project/booley.toml` (first
section below) and at least one FuseSoC `.core` file describing your design. If
you have not described your design yet, start with the [Design description
(`.core`) section](#design-description-core-and-tests-teststoml) — every Booley Flow
builds from it — and come back to the per-Flow knobs after.

**Found a knob that lies, or one you wish existed?** Both are worth a minute:
tell the `/booley-feedback` skill in your agent chat. See
[Feedback](#feedback-feedback).

Contents:

- [booley.toml](#booleytoml): project identity, Flow selection, agent config
- [Design description (`.core`) and tests (`tests.toml`)](#design-description-core-and-tests-teststoml)
- [Cocotb Targets](#cocotb-targets-python-testbenches): Python testbenches, and
  baking their dependency stack into the image
- [Flat and vendored repos](#flat-and-vendored-repos)
- [Doctor waivers](#doctor-waivers-doctor-waiverstoml): reviewed exceptions to
  actionable setup warnings
- [Advanced setups](#advanced-setups): custom images and MCP tools, guides, hooks

## booley.toml

Project identity, Flow selection, and agent configuration live in
`.booley_project/booley.toml`. A per-knob reference follows, section by section.

`[project]` carries the project `name` and `preflight_checks` — see
[Per-Target environment](#per-target-environment-env) for the last one, which is
about ticket file-existence checks rather than identity. `[notifications]`
carries the ntfy.sh push topic and needs no explanation. Everything else is
detailed below, starting with the shared `enabled` flow setting.

### Booley Flow execution: `enabled`

Every Booley Flow builds and executes its command inside the Session Runtime.
`enabled = false` removes a Flow from agent and autonomous discovery.

The Target selects the concrete EDA tool. For an approved commercial tool, the
Project can request only a provisioning source; the host owns the installation,
mount, wrapper, environment, and any License Profile.

### Default Flow Target: `default_target`

Every enabled Target-aware Flow needs a default `.core` Target:

```toml
[flows.lint]
enabled = true
default_target = "lint_core"
```

`default_target` is used by `booley flow <name>` when that invocation does not
pass `--target`; Doctor uses the same default for its dry-run and deep smoke.
An explicit CLI or MCP `target` argument still selects that one invocation and
takes precedence. The former Flow key `target` is retired because it obscured
this fallback behavior; Doctor reports the exact rename when it encounters it.

### Commercial EDA provisioning

The initial policy supports Vivado 2025.2 on Linux x86-64 only. A Project can
request host provisioning as follows:

```toml
[eda.vivado]
provisioning = "host"
```

`provisioning` is `image` (the default) or `host`. For `host`, the exact
Project root must have a host-issued Grant selecting one Installation
Registration before a runtime can be created. That Grant is the sole source of
the installation name and host path; Project configuration cannot select
either. The host administrator manages registrations and grants using
`booley eda installation register` and `booley eda grant add`.

License Profiles are host-owned and never appear in Project configuration. A
licensed runtime receives only a fixed FlexNet relay pointer; it cannot choose
an upstream server or arbitrary license environment. Invalid, missing, drifted,
or revoked authority fails closed before Flow execution.

### Lint (`[flows.lint]`)

Beyond the shared `enabled` setting, lint takes an optional
`warnings_as_errors` (default `true`):

```toml
[flows.lint]
warnings_as_errors = false
```

Set `false` to keep warnings in the console/report but exit 0 on a
warnings-only run, so a CI gate only fails on hard errors.

### Simulation & pass/fail sentinels (`[flows.sim]`)

Booley decides sim pass/fail by scanning the testbench's stdout for a **sentinel
substring**, so a plain `$finish` (exit 0) is never mistaken for a pass. By
default it looks for `[SIM_RESULT] PASSED` / `[SIM_RESULT] FAILED`. You have two
options, and you never have to rewrite a testbench you don't own:

- Emit the built-in markers from your TB (`$display("[SIM_RESULT] PASSED")`);
  see `booley.data.refs/sim_result_sentinel.sv`.
- Keep your TB's existing wording and declare it in `booley.toml`:

  ```toml
  [flows.sim]
  pass_sentinels = ["ALL TESTS PASSED."]
  fail_sentinels = ["ERROR!", "TIMEOUT"]
  ```

  These are honored end-to-end in the built-in Icarus/Verilator flow (a fail
  sentinel wins ties). `booley doctor` greps your sim Target's TB for the
  default sentinel and warns at setup time if none is present. For a custom
  configured phrase, a missing literal is only a note because the TB may build
  it dynamically; the simulation smoke remains authoritative. Otherwise a
  passing run reads as INCONCLUSIVE only when you finally run it.

Cocotb Targets are the exception: they score from cocotb's `results.xml`, so
these knobs don't apply; see [Cocotb Targets](#cocotb-targets-python-testbenches).

### Frozen-clock watchdog (`[flows.sim].sim_time_grace_s`)

A cocotb generation whose compiled run loop doesn't match the simulator's
builds, loads and logs perfectly — and then no timed callback ever fires. The
symptom is nasty because everything *looks* alive: the heartbeat ticks, wall
clock advances, and **simulation** time sits at `0.00 ns` until the full
`timeout_ms` budget burns down. Booley watches the simulator's own clock in the
cocotb log and aborts early with the diagnosis instead:

```toml
[flows.sim]
sim_time_grace_s = 180   # default; 0 disables the watchdog
```

The watchdog only arms once the run has printed a cocotb log line (proof the
simulator started), and only fires while the highest simulation time seen is
*exactly* zero — a sim that advanced even one timestep is never touched again,
so a slow-but-live simulation can't be killed by it. Raise the value if your
testbench legitimately spends minutes in Python setup before the first clock
edge. The usual real fix is re-pinning the cocotb ↔ simulator pair; see
[Baking the cocotb stack into the image](#baking-the-cocotb-stack-into-the-image).

### Sim working directory (`[flows.sim].run_cwd`)

Some testbenches `$readmemh` vectors or firmware via paths relative to the
process working directory (`data/…`, `../fw/…`). Booley runs the sim from the
project root by default, so those reads miss. Point the run at the directory
those relative paths are authored against — usually the testbench dir:

```toml
[flows.sim]
run_cwd = "tests/work"   # relative to the repo root; unset = run from project root
```

Only the direct-binary (Verilator) run honors this as a literal cwd; the Icarus
`make run` target stays anchored to its build directory. Its resolved value is
exported to pre-run commands as `BOOLEY_RUN_CWD`.

**The directory must already exist.** Booley does not create it — the sim run
spawns with this as its cwd, and a missing one fails the spawn. If your run dir
is generated (a scratch dir a staging step fills), `mkdir -p` it from
[Pre-run commands](#pre-run-commands-flowssimpre_run_commands), which run
before the sim and are free to create it.

### Pre-run commands (`[flows.sim].pre_run_commands`)

Some tests need a non-RTL build step before the sim can run. The classic case
is a CPU core whose testbench loads a firmware image cross-compiled *per test
case*. Declare that step as **Pre-Run Commands**:

```toml
[flows.sim]
pre_run_commands = ["make -C tests build_case CASE=$BOOLEY_TEST_NAME"]
```

The lines run **inside the Session Runtime** immediately before each simulation run:
per test for an HDL-testbench Target, once before the batch for a Cocotb
Target.

Their working directory is the **repo root** — *not* `run_cwd`, and not the
build tree. Write paths relative to the repo root, or `cd "$BOOLEY_RUN_CWD"`
first. Nothing in the chain creates directories for you: neither `run_cwd` nor
any output dir your script writes into is auto-created, so a staging script
that targets a fresh directory must `mkdir -p` it itself.

Booley exports the run's identity and its authoritative directories, so
artifact staging never has to guess the sim's working directory:

| Variable | When set | Value |
|---|---|---|
| `BOOLEY_TARGET` | always | the Target being run |
| `BOOLEY_TEST_NAME` | single-test runs only | the selected test |
| `BOOLEY_TEST_NAMES` | always | the run's test list, space-joined |
| `BOOLEY_RUN_CWD` | always | the directory the Simulation Flow runs in ([`run_cwd`](#sim-working-directory-flowssimrun_cwd) when set; otherwise the Project root) |
| `BOOLEY_BUILD_ROOT` | after resolution | the resolved Edalize build tree |
| `BOOLEY_PROJECT_ROOT` / `BOOLEY_PROJECT_DIR` | always | same meaning as in the [post-setup hook](#post-setup-hook) |
| `BOOLEY_SIM_EDA_TOOL` | after Target resolution | concrete EDA tool driven by this Simulation Flow run |

`BOOLEY_PROJECT_DIR` deserves a note: **inside the Session Runtime it is
`/booley-project`**, not `/work/.booley_project`. Both paths reach the same
state directory — the project dir is bind-mounted at the short path as well —
but only the short one is exported, so a script that hardcodes
`/work/.booley_project` and a script that uses `$BOOLEY_PROJECT_DIR` are
writing to the same place under two different names. Prefer the variable.

Failure semantics: a nonzero exit records that test as a **failed** run with an
attributed tail (`pre-run commands failed (rc=N): …`) and the loop continues
with the next test, never a Flow crash. The commands share the per-test
timeout budget (`timeout_ms` / `--timeout`), `--dry-run` previews them in
their real position, and `booley doctor` validates the shape and notes when
they're configured.

Every firing is recorded in the run report — one line per invocation naming the
Target/test, the number of command lines, the exit status and the duration
(`pre_run_commands (2 line(s)) for div_test: rc=0 in 4.7s`) — so a hook doing
the wrong thing quietly is visible without breaking it on purpose.

Simulate-only by design: no ported project has ever needed a non-sim prebuild.
For a *once-per-worktree* setup step (not per-run), use the [post-setup
hook](#post-setup-hook) (defined under [Advanced setups](#advanced-setups))
instead.

### How Booley asks for a waveform (`[flows.sim].trace_args`)

By default `booley flow sim --trace` passes `+trace` and `+tracefile=<file>`, the pair
the shipped `booley_vcd_dump.sv` convention module consumes: an uninstantiated
module the `--trace` overlay roots to dump a VCD, with no testbench edits. Copy
the template from `booley.data.refs/booley_vcd_dump.sv` (the setup skills offer
it too) and list it in your testbench fileset.

A project that owns its C++ `main()` defines its own trace CLI instead (Ibex's
`VerilatorSimCtrl` enables capture only for `-t` / `--trace[=FILE]`). A mismatch
false-passes silently: the binary ignores the plusarg it doesn't know and emits
a header-only FST. Declare the contract:

```toml
[flows.sim]
trace_args = ["--trace={file}"]
```

`{file}` interpolates the trace destination; args without it are passed as-is
(`["-t"]`). This **replaces** the default pair, so include every argument the
binary needs. Booley also rejects a header-only waveform.

### Where the testbench drops its dump (`[flows.sim].trace_files`)

`trace_args` covers "how do I turn tracing on". The other half is "where does
the file land". Booley looks for the artifacts *it* asked for — `trace.fst` /
`trace.fifo` / `trace.vcd` in the build dir, plus the bwave cache. A testbench
that owns its C++ `main()` typically ignores all that and writes a hardcoded
name into whatever its working directory is, so a perfectly good waveform is
reported as `--trace requested but no waveform was produced`. Declare it:

```toml
[flows.sim]
trace_files = ["fpu.vcd"]        # globs allowed: ["dump_*.vcd"]
```

Each entry is resolved against `run_cwd`, then the trace/work dir, then the
build dir (absolute paths are used as given), and is consulted **only when
Booley's own artifacts are absent** — it is a fallback, not an override. A
matched `.vcd` goes through the normal VCD→bwave conversion, so the result is
queryable by `bwave` like any other trace.

### Per-run disk budget (`[flows.sim].max_rundir_bytes`)

A testbench with a default-on tracer, `$dumpfile` or `$fwrite` sink can fill the
disk (one runaway left 27 GB and killed an in-flight synthesis). A watchdog
polls the run directory and kills the run when it crosses the budget:

```toml
[flows.sim]
max_rundir_bytes = 5368709120   # 5 GiB, the default; 0 disables the guard
```

The budget is on **growth during the run**, measured from a baseline taken the
moment the simulator starts — not on the directory's total size. That matters
because `run_cwd` is routinely shared with the testbench's own inputs: charging
staged vectors or firmware images to the *output* budget makes the guard fire on
runs that wrote nothing at all. The kill message reports what it measured (the
baseline, the current size, the growth, the budget) and names the largest files
written during the run, then lists the plausible causes rather than asserting
one.

### FPGA implementation (`[flows.fpga]`)

FPGA implementation runs through Vivado inside the Session Runtime. The
Linux host-provisioned policy obtains a read-only registered Vivado 2025.2 release
through `[eda.vivado]`; it does not require Vivado on the runtime `PATH` from
the host.

`[flows.fpga]` contains execution policy and the default Target selection. Build
inputs belong to the selected `.core` Target: put `part` and `out_of_context`
under its `flow_options`, and XDC constraints in a `file_type: xdc` fileset.

```toml
[flows.fpga]
default_target = "fpga"
timeout_ms = 7200000
```

```yaml
# <design>.core
filesets:
  fpga_constraints:
    files:
      - constraints/top.xdc: {file_type: xdc}
      - constraints/clocks.xdc: {file_type: xdc}
targets:
  fpga:
    flow_options:
      tool: vivado
      part: xc7a100tcsg324-1
      out_of_context: true
    filesets: [rtl, fpga_constraints]
    toplevel: top
```

The `file_type: xdc` fileset is the sole constraints source and is mandatory.
There is no `[flows.fpga].xdc` key. Keeping constraints and the device part on
the Target prevents one global Flow section from applying the wrong design
intent to another Target.

Repeated runs use a Booley-owned content cache, not Make timestamps. The cache
fingerprint covers the resolved Target/EDAM, source/header/constraint bytes,
top, part, parameters/defines, flow options, and the supported Vivado plus
Edalize/FuseSoC identities. Reuse also re-hashes the routed report set and, for
a non-OOC Target, its bitstream; a hit is reported explicitly as
`cached: true`. A miss invokes Make with `-B`, so `Nothing to be done` cannot
turn old reports into either a false pass or a false failure.

### ASIC synthesis (`[flows.synth]`)

```toml
[flows.synth]
default_target = "synth_small,synth_full"
calibration_target = "synth_full" # reviewed heaviest target; Doctor --deep runs it
timeout_ms = 5400000       # per-config cap in ms (default 1800000 = 30 min)
expected_latches = 0       # intentional latches to allow
# fail_on_timing_violation = true    # negative slack becomes a design FAIL (exit 1)
```

Persistent build-recipe inputs live on the selected Target:

```yaml
# <design>.core
filesets:
  timing_constraints:
    files:
      - util/syn/sdc/block.sdc: {file_type: SDC}
targets:
  synth:
    flow_options:
      tool: yosys
      frontend: sv2v            # sv2v (default) | slang
      # slang_options: [--single-unit]
      ppa_profile: balanced     # compact | balanced | max_frequency
      flatten: true
      timing_engine: openroad   # openroad | opensta | none
      yosys:
        abc_recipe: balanced
        # generic_abc_before_mapping: false
        # abc_delay_ps: 3333
      openroad:
        utilization_pct: 50
        placement_density: 0.75
    filesets: [rtl, timing_constraints]
    toplevel: top
```

Timing intent belongs in the Target's SDC fileset. A Target with neither SDC
nor an explicit per-run clock is rejected rather than synthesized against a
silent default clock.

```tcl
# util/syn/sdc/block.sdc
create_clock -name clk_i -period 25.0 [get_ports clk_i]
set_input_delay  -clock clk_i 0.0 [all_inputs]
set_output_delay -clock clk_i 0.0 [all_outputs]
set_false_path -from [get_ports mode_i]
```

For normal use, choose only `ppa_profile` and `flatten`. Profiles are
EDA-tool-independent intent and can be translated by a future Genus backend as
well as the built-in Yosys/OpenROAD backend. The Target's `yosys` and
`openroad` subtables are expert overrides and deliberately remain
backend-specific.

| Profile | Yosys mapping | OpenROAD utilization / density |
| --- | --- | --- |
| `compact` | one default liberty-aware ABC pass | 40% / 0.65 |
| `balanced` | one balanced liberty-aware ABC pass | 50% / 0.75 |
| `max_frequency` | one fast liberty-aware ABC pass | 50% / 0.75 |

All profiles run `synth -noabc`, followed by `dfflibmap` and exactly one
liberty-aware ABC pass. `--ppa-profile` and `--flatten`/`--no-flatten` override
the Target defaults for one MCP tool call. Expert per-call flags override one
backend setting after the profile is selected. An explicit per-call profile
starts from that clean built-in profile (it does not inherit Target-level
`yosys` or `openroad` overrides); per-call expert flags can then refine it.

**Upgrade note:** `balanced` is the new default and intentionally replaces the
old implicit combination (generic ABC inside `synth`, default liberty ABC,
40% utilization). Targets that care about stable PPA must select a profile
explicitly. `compact` restores the old default liberty mapping and 40%/0.65
physical settings; add `generic_abc_before_mapping = true` only when reproducing
the old two-ABC-pass topology for a historical comparison.

Target `flow_options.slang_options` is passed to `read_slang` verbatim.
`--single-unit` is the
common one: slang compiles each file as its own compilation unit, so a repo whose
macros come from a defines header included once at the top of the filelist (a
convention sv2v and Verilator both honor) hits "unknown macro" errors without it.

**`fail_on_timing_violation`** defaults `false`: synthesis succeeded
structurally, and many projects synthesize against placeholder constraints, so a
violation prints `RESULT: WARN -- timing VIOLATED` and exits 0. Turn it on once
the SDC is real, or an rc-only consumer (ticket gate, CI step) reads a -2.6 ns
design as success.

The Target's `frontend` and `slang_options` reach beyond synthesis: `elab`
reads an ASIC Target through the same frontend, so its verdict matches the one
`synth` will reach — `sv2v` transpiles first and elaborates the
result, `slang` runs `read_slang` directly. That is what makes a SystemVerilog
ASIC Target elaboratable at all: the generic flow reads it with a plain
`read_verilog` that cannot parse a package `import`.

### Elaboration (`[flows.elab]`)

`elab` compiles and elaborates the design without running the testbench — a
fast build-only check that RTL/TB changes still compile. It runs inside the
Session Runtime.

```toml
[flows.elab]
# keep_build_dir = true          # keep the compiler build tree after a clean run
# standalone_frontend = "auto"   # auto | iverilog | verilator
```

**`keep_build_dir`** (default `false`). A verilated build tree runs ~130 MB per
Target, and elaboration is a compile-only check, so a Target that passes has its
tree removed. `run.log` is kept either way, and a FAILing Target keeps
everything for triage. Turn this on to get `make`'s incremental rebuild back
across repeated runs.

**`standalone_frontend`** picks which frontend proves the
`elaborate_standalone` criterion. `auto` (default) uses Verilator when
installed — the same frontend the Target's own elaborate drives, so the probe can
never reject SystemVerilog the design demonstrably compiles — and falls back to
`iverilog -g2012`. Pin `iverilog` or `verilator` to choose by hand.

A probe that cannot parse a construct the per-Target elaborate accepted is
reported as a frontend capability gap (exit 2, no verdict) rather than a design
FAIL — but **only when the probe frontend differs** from the one that elaborated
the Target. With the same EDA tool on both sides, a parse error means the standalone
build is genuinely missing a `+define+`/include the Target supplies, which is the
defect this criterion exists to catch. Modules that did grade still report their
verdict (a gap on one module never erases a real failure on another; those are
listed as "ungraded").

### Jobs & concurrency (`[jobs]`)

All Booley work executes inside the one per-folder Session Runtime, and the
number of running EDA tools must be limited or the container runs out of memory.
Every Booley Flow or Specialist run is a **Job** with a **Job Class** determined by where
it executes; each class has a cap, and work beyond the cap waits in a queue
(interactive work ahead of ticket work, FIFO within a class, running Jobs
never preempted) rather than being refused. The defaults:

```toml
[jobs]
max_heavy   = 1   # in-container EDA subprocesses (sim, synth, elab)
heavy_memory = "4g" # reserved memory per HEAVY job; calibrate with Doctor --deep
max_light   = 3   # Specialists (model-API-bound: reviewer, mutation_tester)
max_tickets = 2   # concurrent `booley run` Developer Agents
queue_max   = 8   # per-class queue depth; a full queue is the only BLOCKED response
```

When `[flows.synth].default_target` names more than one Target, setup must select the
reviewed heaviest one as `calibration_target`. `booley doctor --deep` performs a
real end-to-end synthesis of that Target, records the synthesis boundary's EDA
process-tree peak RSS, and applies 15% rounded-up headroom to the HEAVY
reservation. A later plain
Doctor run warns when `[sandbox].memory` cannot admit the calibrated peak plus
Developer-Agent memory and fixed session headroom. An OOM or timeout is an
incomplete calibration, never a successful PPA result.

The slot store is per-project. It does not arbitrate separate Projects' shared
host resources such as commercial-license seats.

### Auto-retry on transient crashes (`[developer.auto_retry]`)

When the Developer Agent dies to a server-side failure (today, an `API Error:
Response stalled mid-stream`), the ticket lands in `blocked/` with the
half-finished verdict (usually "exited with N unmet criteria"). No human can fix
a stream stall, so triaging it wastes a pass. Booley requeues the ticket itself:

```toml
[developer.auto_retry]
max_attempts = 1      # per ticket, over its whole lifetime; 0 disables
```

Only the known signature qualifies. Ordinary crashes, usage limits, context
exhaustion, and Developer budget expiry all fall through to triage unchanged;
retrying those just reproduces them.

### Sandbox (`[sandbox]`)

One container, one memory limit:

```toml
[sandbox]
# image = "my-project-booley-sandbox:latest"  # optional project image, see
#                                             # "Custom sandbox image" below
memory = "8g"   # single container memory limit, fed into the generated
                # devcontainer; unset means no explicit limit
# pip_requirements = ["sim-requirements.txt"]  # Python deps to bake, see below
```

`booley doctor` warns when the limit looks too small for the configured
`[jobs]` caps.

#### Commercial-license policy

Generic license environment forwarding is retired. A supported License Profile
is host-owned, separately granted to the exact Project root, and emits only the
fixed runtime pointer required by the built-in commercial policy. Do not put
license-server addresses, license-file paths, or environment forwarding in
`booley.toml`.

#### Host skills (`[sandbox].mount_host_skills`)

By default the sandbox carries only Booley's built-in `booley-*` skills (baked
into the image). Flip this on to also use your **own** global agent skills
inside the container:

```toml
[sandbox]
mount_host_skills = true   # default false
```

`booley init --seed` scans your host skill dirs — `~/.claude/skills` (Claude)
and `~/.agents/skills` (Codex) — resolves each entry to its **real** directory
(these dirs are usually full of symlinks, which would otherwise dangle inside
the container), and mounts each one **read-only**. Booley's own built-ins are
excluded (they are already in the image and always win a name clash), and each
skill is de-duplicated by name. The agent discovers them alongside the built-ins
on every container start. Nothing on the host is ever written. Rerun
`init --seed` after adding or removing host skills.

#### Python dependencies (`[sandbox].pip_requirements`)

The sandbox has no network while a Flow runs, so a project's Python deps must be
**baked into its image**, not installed on demand. `booley init` bakes exactly
the requirements files you list here, and **nothing is auto-discovered**, then
builds and automatically selects `<slug>-booley-sandbox`. Leave the key unset
(or empty) and the project runs the base image with no extra deps. Set
`[sandbox].image` only when selecting a genuinely custom image.

```toml
[sandbox]
pip_requirements = ["sim-requirements.txt", "python/tb/requirements.txt"]
```

The value is a **list of paths to requirements files** (a bare string is
ignored). Paths are relative to the repo root; any name and any depth works.
`init` warns on an entry it can't find and skips it — and if every entry is
missing, the project quietly falls back to the base image, so check the `init`
output rather than assuming the pins landed. Two content rules still apply to
the files it bakes:

- Non-bakeable lines are **skipped with a warning**: editable installs (`-e`),
  nested includes (`-r`/`-c`), and local/`file:` paths. The isolated build
  can't reach those paths, so install them at runtime instead.
- Pins on the packages the base image manages (`fusesoc`, `edalize`, and Booley
  itself) are **dropped with a warning**, not baked: a design repo pinning an
  older `fusesoc` would otherwise silently downgrade the driver out from under
  the flows.

**Everything else you pin wins.** That list of managed packages is short on
purpose: your project owns its verification stack. Pinning `cocotb==1.5.1`
replaces the base image's curated `cocotb==2.0.1`, and `init` says so:

```
pin 'cocotb==1.5.1' overrides the base image's cocotb==2.0.1 (project wins)
```

That line is a note, not a warning — nothing is blocked. But understand what
you took on: the base image validates *its* cocotb (imports, VPI library
present) at build time, and that check does not re-run on your layer. A project
pin is a commitment to test the resulting combination yourself, and the first
`booley flow sim` after the rebuild is that test.

**Where the file lives when upstream has no requirements.txt.** Plenty of
projects keep their test pins in `tox.ini`, `setup.py`, or a CI workflow, none
of which this knob reads. Transcribe them into a real file. Two homes work, and
the choice is about whether the pins should survive a fresh clone:

- **Tracked, in the repo** (e.g. `sim-requirements.txt` at the root, or next to
  the testbenches) — the file is a normal source file, reviewed and cloned like
  any other. Preferred when you can add a file to the repo.
- **Inside the project dir** (e.g. `.booley_project/sim-requirements.txt`) —
  legal, and the right call under [stealth mode](#stealth-mode-stealth) when you
  do not want a Booley-shaped file in the tracked tree. The catch is durability:
  `.booley_project/` is excluded from the host repo, so a fresh clone has no
  such file and `booley init` there produces a base-image project with none of
  your pins. See [what survives a fresh clone](#what-survives-a-fresh-clone).
  Do not name `.booley_project/docker/requirements.txt`: that path is the
  *generated* output of this knob.

Either way the file is an **image-build input**, consumed only by `booley init`
(Step 9b). It must exist on disk before the build, and changing it does nothing
until you re-run `booley init` and recreate the container.

This is also the supported way to pin a **cocotb 1.x** stack, or to add BFM
packages (`cocotbext-*`) the base image doesn't carry — with rules of its own:
see [Baking the cocotb stack into the
image](#baking-the-cocotb-stack-into-the-image).

### Stealth mode (`[stealth]`)

Keeps your AI-assisted workflow out of the git history (see
[FEATURES.md: Stealth Mode](FEATURES.md#stealth-mode)).

```toml
[stealth]
enabled = false              # setup default; set true to opt in
# ignore_native_cores = true # use only stealth-authored cores during Booley resolution
# banned_words = ["claude", "anthropic", "codex", "booley", ...]  # override
#                            # the built-in list; empty [] effectively disables
#                            # sanitization while keeping the hook installed
# enforce_convention = true  # enforce type(scope): summary subjects
#                            # (default: off — opt in)
# max_body_lines = 0         # cap the commit body (0 = subject line only);
#                            # unset = unlimited
# allowed_authors = ["*@example.com", "Jane Doe"]  # identity allowlist checked
#                            # at push time; unset or [] = unrestricted
```

Setup asks whether you want to enable stealth mode and keeps it off unless you
explicitly say yes. It persists that choice as `[stealth] enabled = false` so
commit messages stay verbatim. For compatibility with projects configured
before this setup policy, a missing `enabled` key still means **on**; write the
key rather than omitting it when you want stealth disabled.

Stealth also makes `.booley_project/` a self-contained home for authored
FuseSoC cores. Cores under `.booley_project/cores/` use repository-root-relative
fileset paths; `booley init` generates ignored `.booley-projected-*.core` copies
at the repository root so FuseSoC reads pristine RTL directly, with no source
symlinks or copied RTL. Booley refreshes the projections again before target
resolution. This filesystem behavior requires explicit `enabled = true`; the
legacy missing-key fallback applies only to history sanitation.

`ignore_native_cores` is an optional, default-false isolation switch for
repositories whose shipped `.core` files are invalid, obsolete, or deliberately
outside the Booley Target surface. It requires explicit `enabled = true`. When
set, Booley enumerates only `.booley_project/cores/` and gives FuseSoC a private
generated registry containing only those cores; repository-native `.core` files
are never parsed during Booley resolution. The private copies use absolute
source paths generated for the current workspace, so no source symlinks or RTL
copies are created. Run `booley init` or a Booley Flow rather than raw FuseSoC
when relying on this switch.

When enabled, a commit-msg hook sanitizes the built-in banned-word list out of
your commit messages. An already-installed hook no-ops at commit time when the
flag is off. `banned_words` replaces (not extends) the built-in list.

**Your message is redacted, not truncated.** Subject *and* body are kept, with
banned phrases substituted in place; the hook prints what it rewrote. The only
lines removed outright are **attribution trailers** (`Co-Authored-By:`, the
"Generated with …" footer), which carry no authorial content. Sanitization is a
scrub, not a word limit, so by default you write the long commit body and keep
the rationale.

#### Enforcing the subject convention (`enforce_convention`)

**Off by default — opt in.** Booley's own commits follow a
`type(scope): summary` subject (`feat`, `fix`, `refactor`, `test`, `review`,
`wip`, `docs`, `chore`), but that convention is *not* forced on your repo unless
you ask for it. A design repo carries human- and upstream-style commits on code
you don't own, and rejecting every one that doesn't match the format is noise,
not hygiene. Turn it on for a team that wants the convention across its own
history:

```toml
[stealth]
enforce_convention = true
```

With it on, a non-conforming subject is rejected (merge commits are exempt).
Independent of the toggle: the banned-word scrub and `max_body_lines` cap are
always in force when stealth is enabled. `BOOLEY_SKIP_COMMIT_VALIDATION=1` lands
one commit past the convention check and the body cap — sanitization still runs,
in every case.

#### Capping the body (`max_body_lines`)

Unset by default, meaning unlimited. Set it if you want terse history:
`max_body_lines = 0` allows the subject line only, `= 5` allows five lines of
prose. Blank lines and git's `#` comment lines don't count — only lines that
actually reach history, so the cap doesn't move when you re-space a paragraph.

An over-long message is **rejected, not trimmed**: the commit fails and tells
you the line count, so nothing you wrote is silently thrown away. Merge commits
are capped too (unlike the subject-format check, which exempts them) — a merge
body is exactly where long hand-written narratives collect.

#### Author allowlist (`allowed_authors`)

Enforced by the **pre-push** hook, not commit-msg — git hands the commit-msg
hook only the message file, so `git commit --author='someone <else@local>'`
cannot be caught there. Push time is the first point where both identities are
readable, and it also covers identities that arrive via rebase, cherry-pick, or
`--no-verify`.

Both the **author and the committer** of every outgoing commit must match at
least one entry. Each entry is an fnmatch glob, matched case-insensitively
against the bare email, the bare name, and the full `Name <email>` ident:

```toml
allowed_authors = [
    "*@example.com",              # a whole domain
    "jane.doe@personal.example",  # one exact address
    "Jane Doe",                   # a bare name, any address
]
```

Unset or `[]` disables the check. Remember that a push carries *every* outgoing
commit, so an allowlist has to cover your collaborators' historical identities
too, not just your own — otherwise pushing a branch that contains their work is
blocked. `BOOLEY_SKIP_PUSH_GUARD=1` skips the scan for one push.

#### What survives a fresh clone

Keeping Booley out of the git history has a price, and it is worth stating
plainly: **the setup is local**. `booley init` writes these entries into
`.git/info/exclude` (which is per-clone and itself never committed):

```
/.devcontainer
/.booley_project
/.claude
/AGENTS.md            # root symlink into .booley_project/AGENTS.md
/CLAUDE.md            # same file, second name
```

So a colleague who clones the repo gets your RTL, your `.core` files, your SDC
— and no Booley at all. Nothing breaks for them; that is the point. But it
means anything you park under `.booley_project/` is **your machine's state, not
the project's**, including:

- `booley.toml` and everything it configures;
- `AGENTS.md` / `CLAUDE.md` (the root files are symlinks — after a fresh clone
  they simply don't exist, and a stale one left behind by a copy is a dangling
  link);
- `hooks/post-setup.sh`, `mcp_tools/`, `tests.toml`;
- any requirements file you baked with
  [`pip_requirements`](#python-dependencies-sandboxpip_requirements).

Two consequences worth planning for:

**`git add -f` does not rescue a file under `.booley_project/`.** `booley init`
gives the project dir its **own** git repo when stealth is on, so the host repo
sees an embedded repository and refuses to track paths inside it. Commit that
inner repo (or back it up) if you want the setup to survive a machine, and use
`git add -f` only for files *outside* the project dir.

**A file that a tracked `.core` references must itself be tracked.** This is
most visible for a deliberately-broken lint fixture: it has to be a real file
in a real Target. `booley doctor` warns when it finds one on disk but not in git
("a fresh clone or CI checkout will lack them"), and it is right: put that file
in the **tracked tree**, not in `.booley_project/`. A `tb/fixtures/` or
`verif/lint_bad.sv` next to the sources reads as ordinary test material and
gives nothing away — which is what stealth mode is actually asking of you.

Simulation has one narrow exception because its fail-path fixture may replace
an already-staged firmware or vector rather than belong to a `.core`: mirror
the replacement beneath
`.booley_project/selftest/sim/bad-overlay/` (for example,
`bad-overlay/firmware/firmware.hex`). During `doctor --deep`, Booley overlays
that tree on an isolated simulation build variant of the default Target's
first runnable test. Ordinary simulation never applies or reuses it, and no
Doctor-only shell command belongs in `[flows.sim].pre_run_commands`.

Lint's corresponding convention is a `.core` Target named
`lint_selftest_bad`. Doctor uses `[flows.lint].default_target` as the good case
and that conventional Target as the bad case. There is no
`[flows.<flow>.selftest]` configuration table; legacy tables must be deleted.

### Feedback (`[feedback]`)

Booley keeps an append-only log of what went wrong, what was merely annoying, and
what you think of Booley — `.booley_project/findings.jsonl`. Setup fills it as
it runs; afterwards, tell the **`/booley-feedback`** skill from any normal
working session. The skill decides how to classify and record what you said:

| Feedback | For | What the skill needs |
| --- | --- | --- |
| Defect | something broke | a reproduction, what happened, and what you expected |
| Friction | nothing broke, but it was confusing | where it happened and what you expected instead |
| Impression | what you think of Booley — praise, gripes, wishes | one sentence |

**Impressions are the feedback nobody thinks to give, and they decide what gets
built.** Bug
reports say what is broken; they never say whether the thing is worth using.
"Best part is the waveform flow", "the setup grill is exhausting", "I want
per-Target coverage", "this replaced three days of manual work" — all of it is
wanted, all of it rides the same redaction and the same offer. The skill sorts
the sentiment without making you learn a reporting interface.

The skill keeps one persistent report from that log:

- **`.booley_project/SETUP-REPORT.md`** — yours. Local, unredacted, never
  published, written in every mode. On a project that never ran setup it is
  called `FEEDBACK-REPORT.md` instead.

The skill derives the maintainer-facing view from the same log: only entries
attributable to Booley, with enough evidence to act on, not already filed, and
with project identifiers redacted. It is not saved as a second report. Ask the
skill for a redacted export only when you explicitly want that view persisted as
`.booley_project/BOOLEY-FEEDBACK.md` for manual sharing.

`mode` decides whether Booley may *offer* to send that view, and where:

```toml
[feedback]
# mode = "ask"                  # default: offer once, after showing you the text
# mode = "email"                # same offer, by mail to the maintainer, not a public issue
# mode = "file-only"            # never submit; allow only an explicit redacted export
# mode = "off"                  # local report only, no offer
# redact_extra = ["codename"]   # extra terms to scrub from anything outgoing
# redact_identifiers = false    # keep module/Target names (default: replace them)
```

Setup's plan (Step 0, row 21) asks for this value up front, while you are already
thinking about disclosure rather than at the end of a long session. Unattended
runs resolve it to `file-only` — an agent may not accept the offer for you.

#### What the offer looks like (`ask` and `email`)

Once per setup run, at the very end, after the gate has already passed — or once
per report, when you asked the skill for one. You are shown the **exact text**
that would go out, what was substituted, and what redaction cannot catch. Three
answers are all fine: yes, "just give me the file", or no. A no is final for
that run; `mode = "off"` makes it permanent.

Nothing can be sent without the **confirmation token** printed with that preview.
The token is a digest of the report, so it only works for text that was actually
displayed, and re-rendering invalidates it — a guard against an agent approving
on your behalf. The skill passes the token only after you approve the exact text.

A filed issue is **public** and carries your GitHub account name. If that is the
sticking point, use `file-only`, ask the skill for the redacted Markdown file,
and post it yourself from whatever account you like. Tell the skill where you
posted it so those entries are marked filed and excluded from later batches; a
bug you file in July must not drag along March's setup findings.

`mode = "email"` swaps the destination for `boldaxolotl@proton.me`, Booley's
maintainer intake — nothing published, no GitHub account needed, same redaction,
preview and token. It is a pure hand-off: Booley builds a prefilled `mailto:`
link and stops. No SMTP, no password, no outbound connection; your client sends
it from your mailbox, so the last look is yours. Long reports get abbreviated in
the link because mail clients silently drop oversized ones; ask the skill for a
redacted export and attach it when prompted. Booley cannot see whether you ever
hit send, so tell the skill afterwards. Otherwise the batch is offered again
next time, which is the safe direction to be wrong in.

#### What redaction does and does not do

**Replaced:** absolute paths (repo root, `$HOME`, any `/home/<user>`), git remote
URLs and their `org/repo` slug, your `git config` name and email, and design
identifiers scraped from `booley.toml` and your `.core` files (project name,
Target names, VLNV segments, toplevels) — mapped to stable `<module-N>`
placeholders so the report still reads coherently. `redact_extra` adds your own
terms; a project that overrode `[stealth] banned_words` gets those scrubbed too.

**Kept, deliberately,** because a report without them is not actionable: EDA tool
names and versions, error text, tracebacks, and performance/area numbers. Two of
those carry real signal — which commercial EDA tools you license, and any
identifier sitting inside a quoted log line that was never in a `.core`. The
preview says so before you decide.

**It is a denylist, not a proof.** The honest promise is "best-effort scrubbing,
and here is the diff" — which is why you read the text before it goes anywhere.
For an open-source design whose module names are already public,
`redact_identifiers = false` produces a much more useful report.

That caveat bites hardest on **attachments**. A report can inline the tail of a
run log or a doctor transcript, which is usually what makes a bug diagnosable —
and a log line is arbitrary text no denylist can vet. Give the path to the skill;
attachments are redacted with the rest of the body and shown in full in the
preview. Read them there.

### Agent provider (`[agent]`)

Both modes run the Developer Agent and every nested Specialist on a single LLM
backend, Claude or Codex:

```toml
[agent]
provider = "claude"   # or "codex"
```

#### Pinning what bills (`[agent] auth`)

Pick which credential agents bill when several are present:

```toml
[agent]
provider = "claude"
auth = "subscription"   # "subscription" | "api_key" | "auto" (default)
```

- **`auto`** (default): the CLI's own precedence decides (an API key outbids a
  subscription); Booley reports the winner but changes nothing.
- **`subscription`**: bill the subscription. Booley scrubs the API-key env vars
  from the agent environment; a `booley auth` OAuth token still rides along.
- **`api_key`**: bill the API key.

Either non-`auto` value fails loud (`booley doctor`) when its credential is
absent, rather than silently falling back. See [USAGE.md: Auth &
billing](USAGE.md#auth--billing).

### Developer Agent policy (`[developer]`)

```toml
[developer]
human_in_the_loop = true   # default
run_report = true          # default

[developer.limits]
active_timeout_seconds = 1800   # 30 minutes (default)
wall_timeout_seconds = 43200    # 12 hours (default)
```

- **`human_in_the_loop`**: whether a human operator is available to unblock
  the agent. When `true` (default), the Developer Agent blocks on missing or
  ambiguous spec and spec reviewers enforce strict grounding. Set `false` for
  benchmarks and unattended bulk runs: the agent resolves spec-silent points
  itself and reviewers tolerate documented readings.
- **`run_report`**: whether every ticket run must end with a structured run
  report (`REPORT.md` written via `submit_run_report`). When `true` (default),
  the report is a hard exit condition — the run fails review until it is
  submitted, any unmet optional criteria require a justification in it, and
  any later code change stales it. Set `false` when nobody
  consumes the reports (benchmarks, bulk unattended runs): the exit condition
  becomes "all mandatory criteria met and every unmet optional criterion is
  justified." `submit_run_report` is skipped when every optional criterion is
  met, saving its per-run token cost; if an optional criterion remains unmet,
  the agent still submits a report containing the required justification.
- **`active_timeout_seconds`**: limits Developer Agent work that consumes its
  own session time: model turns, file inspection and edits, and shell commands.
  It pauses while the agent is synchronously waiting for a Booley MCP tool
  (including queue and Flow execution time) and during transient-provider retry
  backoff. Detached jobs do not pause it while the agent does other work.
- **`wall_timeout_seconds`**: hard elapsed-time ceiling for the Developer run.
  It never pauses, including during Booley tool waits. Both limits are fresh on
  a new run or human unblock and are shown live in the Ticket Mode status bar.
  Reaching either limit is a terminal local timeout, not a transient provider
  error, so backend retry does not multiply the configured budget.

### Model selection (`[models]`)

Every agent Booley runs picks a model through one of three **capability
tiers**: `heavy`, `standard`, `light`. The Developer Agent runs `heavy`,
specialists floor at `standard`, cheap internal steps run `light`. Each
provider ships defaults, so an untouched project needs no `[models]` at all.

Override a tier to move every agent on it at once:

```toml
[models]
heavy = "claude-fable-5"
standard = "claude-opus-4-8"
light = "claude-sonnet-5"
```

Tiers you leave out keep the active provider's default; overriding `heavy`
alone does not disturb the other two.

#### Pinning one agent (`[models.roles]`)

When a tier is the wrong grain (Developer Agent on your best model, but review
and mutate on something cheaper), pin a single agent by name:

```toml
[models.roles]
developer = "claude-fable-5"         # a literal model id
reviewer = "claude-opus-4-8"
mutation_tester = "light"            # …or a tier name
triage_report = "standard"           # precomputed rich HTML explanation
```

A tier name resolves through the `[models]` table (and tracks any override of
it); anything else is passed to the provider verbatim. Pinnable roles are the
harness steps `developer`, `recovery`, and `triage_report`, plus every specialist named as its
Specialist: `reviewer`, `mutation_tester`, `coverage_analyst`, `tb_coder`. An unknown
role FAILs `booley doctor`.

A pin overrides a specialist's minimum-tier floor and sets the **model only**;
reasoning effort still follows the tier the agent would otherwise use (a no-op
on Claude, whose SDK exposes no per-call effort knob).

## Design description (`.core`) and tests (`tests.toml`)

The design itself is described outside `booley.toml`: *how it builds* lives in
one or more FuseSoC CAPI2 `.core` files ([CAPI2 reference](https://fusesoc.readthedocs.io/en/stable/user/build_system/core_file.html)),
and *what to verify* in `.booley_project/tests.toml`.

> **Is FuseSoC mandatory?** Yes: every Booley Flow builds from a resolvable `.core`
> Target. A `.core` is usually a mechanical restatement of the build
> you already have (a filelist plus a toplevel); for the one thing it can't
> express, a per-test non-RTL build step like compiling a test's firmware,
> declare [Pre-run commands](#pre-run-commands-flowssimpre_run_commands)
> instead of abandoning the flow.

A `.core` describes *how the design builds*: source files (`filesets`), top
modules (`toplevel`), typed parameters/defines, and build **targets**:

```yaml
CAPI=2:
name: ::my_project:0
filesets:
  rtl:
    files:
      - rtl/pkg.sv: {file_type: systemVerilogSource}
      - rtl/dut.sv: {file_type: systemVerilogSource}
  tb:
    files:
      - tb/tb_dut.sv: {file_type: systemVerilogSource}
      - tb/booley_vcd_dump.sv: {file_type: systemVerilogSource}  # enables --trace
    tags: [tb]                        # required on the testbench fileset
  fw:                                 # non-RTL data the TB reads ($readmemh etc.)
    files:
      - fw/firmware.hex: {file_type: user, copyto: firmware.hex}
parameters:
  WIDTH: {datatype: int, paramtype: vlogparam, default: 16}
targets:
  sim:                                # target name == the --target value
    flow: sim
    flow_options: {tool: verilator}   # verilator | icarus
    filesets: [rtl, tb, fw]
    toplevel: tb_dut                  # a sim target's toplevel is its TB top
    parameters: [WIDTH]
  lint:
    flow: lint
    flow_options: {tool: verilator}
    filesets: [rtl]
    toplevel: dut
  synth:
    flow: generic
    flow_options: {tool: yosys, arch: xilinx}  # arch is edalize plumbing Booley
    filesets: [rtl]                            # ignores; the OpenROAD engine
    toplevel: dut                              # drives its own PDK/target
```

A few conventions worth calling out in that example:

- **`tb/booley_vcd_dump.sv`** is the trace-convention module described under
  [`trace_args`](#how-booley-asks-for-a-waveform-toolssimtrace_args).
  Without it, `booley flow sim --trace` has nothing to root and `booley doctor` warns at
  setup time.
- **A `file_type: user` file with `copyto:`** stages a non-RTL data file (a
  `$readmemh` image, a vectors file) into the build tree at the name the TB opens.
- **`flow_options.arch`** (and any other Edalize-only knob) is plumbing Booley
  passes through to the toolchain. The built-in synth path drives its own
  PDK/target via the OpenROAD engine and ignores `arch`.

### Target authoring

Name project-owned Targets `<axis>_<subject>` using the Booley Flow axis:

| Axis | Driven by | Example |
| --- | --- | --- |
| `sim_` | `sim`, `elab` | `sim_smoke` |
| `lint_` | `lint` | `lint_style` |
| `synth_` | `synth` | `synth_timing` |
| `fpga_` | `fpga` | `fpga_board` |

The axis is needed because CAPI2 has no synthesis flow: synth and FPGA Targets
can both resolve as `generic`. A bare axis such as `lint` is fine when there is
only one Target for it; `elab` reuses a sim Target. The old `asic_` prefix still
runs, but `booley doctor` recommends `synth_`.

Use `default:` only when another core depends on this one; Booley does not show
it as a selectable Target. Vendored upstream cores keep their original names.
Renaming a Target also requires updating its `tests.toml` section,
`[flows.*].default_target` pins, and ticket criteria. For Python testbenches, see
[Cocotb Targets](#cocotb-targets-python-testbenches).

### Tests (`tests.toml`)

`tests.toml` lists the tests to run per Target, plus an optional run-time
selector:

```toml
[test_lists]                          # optional shared, reusable lists
smoke = ["reset", "basic"]

[sim]                                 # keyed by .core target name
tests  = ["reset", "basic", "stress"]
select = "+test_id={index}"           # one plusarg; {index} (0-based) or {name}
skip   = ["stress"]                   # known-hangs to exclude from a full run

[sim_fast]
test_list = "smoke"                   # reference a shared list instead

[cpu_core]                            # a CPU core selecting firmware per test
tests  = ["hello.elf", "coremark.elf"]
select = "--meminit=ram,{name}"       # getopt argument, not a plusarg (SETUP-7)
```

`select` is **exactly one option token** (no embedded whitespace). Two forms:
a **plusarg** (`+…`), consumed by the testbench's `$value$plusargs` runtime; or
a **getopt argument** (`-…` / `--…`), forwarded verbatim to the sim binary's own
`main`: the form a CPU core needs when it selects boot software with e.g.
`--meminit=ram,<elf>`, which no plusarg can express. `{index}` (0-based) and
`{name}` are the only substitutions.

`skip` drops known-hanging / known-failing tests from a plain
`booley flow sim --target <target>`
run so each doesn't burn the full per-test wall-clock budget. Naming a skipped
test explicitly with `--test <name>` still runs it (an explicit override), and an
all-skip target ignores the list rather than passing with zero tests. For a
one-off exclusion without editing config, pass
`booley flow sim --target <target> --skip name1,name2`.

#### Per-Target environment (`env`)

Plenty of upstream testbenches are parameterized by an environment variable —
a cocotb module that branches on `os.getenv("FLAVOR")`, an SV testbench whose
C++ `main` reads a config var. Declare it per Target and Booley exports it into
the simulator process:

```toml
[sim_vanilla]
tests = ["run_test_001"]
env   = { FLAVOR = "vanilla" }        # NAME = "value", strings only

[sim_small]
tests = ["run_test_001"]
env   = { FLAVOR = "small", NOC_DEBUG = "1" }
```

Per Target because that is where the variance lives: the same testbench module
run under two RTL flavours is two Targets, each with its own value. The exports
happen in the shell that owns the build **and** the run, inside the Session
Runtime — no testbench edit needed. [Pre-run
commands](#pre-run-commands-flowssimpre_run_commands) see the same
variables (so a flavour-aware firmware build works), but they can't *provide*
them: their own exports die with their shell. The test filter selects tests
rather than configuring them. Values must be quoted
strings, and names must be
shell-exportable identifiers (`[A-Za-z_][A-Za-z0-9_]*`). `--dry-run` previews
the exact `export` lines.

Design fields never appear in `booley.toml`, and neither do the source-category
directory listings: RTL vs testbench source dirs are derived from the `.core`
filesets (the `tags:[tb]` partition marks the TB sources). What lives
in `booley.toml` is the per-Flow execution knobs (`enabled`) plus a few
unrelated flags — never the source-dir listing itself. One such flag is
`[sources.testbench].preflight_checks` (bool, default `true`): set it `false`
to skip the check that a ticket's testbench files already exist on disk, which
is what you want when the TB is authored during the run. (The broader
`[project].preflight_checks` toggles *all* such file-existence checks — scope
files and TB alike; the per-section flag narrows the relaxation to just the TB.)

## Cocotb Targets (Python testbenches)

A **Cocotb Target** is an ordinary sim Target whose testbench is a cocotb Python
module instead of HDL. It needs nothing beyond the built-in flow: declare `cocotb_module` in the
Target's flow options and Booley takes it from there.

```yaml
filesets:
  rtl:
    files:
      - rtl/counter.sv: {file_type: systemVerilogSource}
  tb:
    files:
      - tb/test_counter.py: {file_type: user, copyto: test_counter.py}
      # multi-file TBs: copyto preserves the package layout in the build root
      - tb/helpers/util.py: {file_type: user, copyto: helpers/util.py}
    tags: [tb]                       # required, same as an SV testbench fileset

targets:
  sim_cocotb:
    flow: sim
    flow_options:
      tool: icarus                   # icarus | verilator (sandbox-only)
      cocotb_module: test_counter    # THIS is what makes it a Cocotb Target
      iverilog_options: [-g2012]     # SystemVerilog sources need -g2012
      timescale: 1ns/1ps
    filesets: [rtl, tb]
    toplevel: counter                # what the Python TB attaches to
```

```toml
# .booley_project/tests.toml
[sim_cocotb]
tests = ["test_reset", "test_basic"]   # the @cocotb.test() FUNCTION names
# no `select` — see below
```

What differs from an SV Target:

- **`toplevel` is whatever the Python testbench attaches to.** For a simple
  design that is the **DUT itself**: cocotb's `dut` handle *is* the toplevel,
  and there is no HDL wrapper. But a design whose ports are **SystemVerilog
  interfaces** needs one: cocotb's BFMs bind to interface *instances*
  (`AxiStreamBus.from_entity(dut.s_axis_tx)`), and an instance has to be
  instantiated somewhere. Such projects ship a thin HDL wrapper that declares
  the interfaces and instantiates the DUT, and **the wrapper is the
  `toplevel`** (commonly named after the test module, e.g.
  `toplevel: test_taxi_eth_mac_10g` alongside
  `cocotb_module: test_taxi_eth_mac_10g`). Both shapes work as-is; point
  `toplevel` at the module the testbench actually drives.
- **`tests.toml` lists cocotb test-function names**, and must **not** declare a
  `select` template, since selection is an env-var filter Booley builds
  (`COCOTB_TEST_FILTER`, or `TESTCASE` on cocotb 1.x, auto-detected). A `select`
  on a Cocotb Target is a setup-time error. `skip` works unchanged.
- **No sentinels** ([as noted above](#simulation--passfail-sentinels-flowssim)):
  a missing or truncated `results.xml` is *inconclusive*, never a pass. RTL
  assertion output is still scanned.
- **One test module per Target.** A cocotb Target names exactly one
  `cocotb_module`, so a second test module is a second Target — this is a
  mechanical consequence of the `.core`, not a style preference, and it is the
  rule that wins over any "one Target per intent" advice you may read
  elsewhere. That advice applies to **classic HDL testbenches**, where one
  sentinel-scored TB toplevel usually covers a whole intent and splitting it
  buys nothing.
  A project with eight cocotb modules therefore has eight sim Targets. If it
  also has build variants (a `-D` set per hardware flavour), the Targets
  multiply: variants × modules. That is fine up to a point — the Targets are a
  handful of `.core` lines each and `booley targets` stays readable — but it is
  worth deciding **which** variants earn a full module sweep rather than
  generating the whole matrix. A common shape is: every module on the default
  variant, plus the one or two modules that actually exercise the varying
  behaviour on the other variants. Ticket criteria name Targets explicitly, so
  an unenumerated combination is simply one nobody gates on.
- **Image-provisioned only**, Icarus or Verilator. Commercial simulators are
  out of scope for Cocotb Targets.
- Tests generated by a **factory** (`TestFactory` / `generate_tests`) are fine:
  list the generated names in `tests.toml`; `booley doctor` recognizes the
  factory pattern and defers name-checking to `results.xml` at run time.

### Baking the cocotb stack into the image

The base sandbox pins **cocotb 2.x** with a curated BFM set (`cocotbext-axi`,
`cocotbext-uart`). A project that needs different pins, most often **cocotb
1.x**, which `TestFactory` and `cocotb.utils.get_time_from_sim_steps` still
require, bakes its own via
[`[sandbox].pip_requirements`](#python-dependencies-sandboxpip_requirements).

**Bake the project's full pinned test stack, not the subset the testbench looks
like it needs.** Module-scope imports like `cocotb_test.simulator` and `pytest`
(often only for an unused pytest entry point) run at import time, so dropping
them makes cocotb fail to import the testbench at all. Copy the project's own
pin set (`tox.ini` / `requirements.txt`) wholesale.

**Wholesale is still not always enough.** Old pin sets were written for old
Pythons, and the sandbox runs Python 3.13:

- **`distutils` is gone** (removed in Python 3.12). Packages of the cocotb-1.x
  era still do `from distutils.spawn import find_executable` — `cocotb-test`
  0.2.0 does. Nobody lists `setuptools` as a dependency, because virtualenvs
  used to ship it implicitly; you have to add it yourself. If a pinned stack
  dies on a missing `distutils`, put **`setuptools`** in the requirements file
  and rebuild.
- **Probe the import path the testbench actually uses.** `import cocotb_test`
  succeeds while `import cocotb_test.simulator` — the line the testbench runs —
  raises, because the submodule is where the dead import lives. Verify with the
  real thing:

  ```bash
  booley session enter -- python -c "import cocotb_test.simulator, cocotb; print(cocotb.__version__)"
  ```

**What a pin cannot fix: the simulator pairing.** cocotb's run loop is compiled
against the simulator, so an old cocotb with the image's current Verilator can
fail even after its Python dependencies import successfully. If simulation
stalls at time zero, follow the diagnosis and recovery in
[TROUBLESHOOTING.md](TROUBLESHOOTING.md#simulation-stalls-at-time-zero-without-results).

## Flat and vendored repos

- **Flat single-file repos** (one `picorv32.v` at the root) need no `rtl/`+`tb/`
  restructure. Classification comes from the project's mandatory `.core`
  ([as always](#per-target-environment-env)): a fileset listing the root-level
  file (e.g. `files: [picorv32.v]`) is classified by its **exact path** rather
  than a parent directory, so the single file is treated as RTL with no
  directory layout. TB files use the same fileset mechanism carrying
  `tags: [tb]`.
- **Vendored cores** you don't want Booley/FuseSoC to discover (a bundled SoC's
  example cores) are quarantined with a `FUSESOC_IGNORE` marker file in the
  directory. Booley's `.core` scanner skips any directory carrying one, exactly
  as FuseSoC's own scanner does.
- **Git submodules** work, but Ticket Mode does not clone them. See
  [Submodules](#submodules) below.
- **Multi-core repos** (a FuseSoC-native project shipping tens or hundreds of
  `.core` files: ibex has 208, with a `lint` target in 54 of them) work with
  **no `[fusesoc]` config at all**. Sharing a Target name across cores (many
  `lint`s, many `sim`s) is normal FuseSoC, not an error; Booley identifies a
  Target by its `(VLNV, name)` pair — VLNV being FuseSoC's
  Vendor:Library:Name:Version coordinate for a core — and only asks you to
  disambiguate when a bare name is genuinely contested.

### Submodules

Ticket Mode runs each ticket in its own git worktree, and a worktree cannot
initialize submodules the normal way — `git submodule update --init` there fails
with *"reference repository '.' as a linked checkout is not supported yet"*, and
even where it works it re-clones from the recorded URL, which the sandbox has no
network to reach.

So Booley doesn't clone. It **copies each submodule's working tree out of your
main repo** into the new worktree and rewrites the submodule's `.git` pointer to
an absolute path, then turns off submodule recursion in that worktree
(`submodule.recurse false`). Nothing goes over the network, and an SSH-only
submodule URL is irrelevant. Paths come from your `.gitmodules`, so the normal
case needs no configuration at all.

What this asks of you, once, on the host:

```bash
git submodule update --init --recursive
```

Every submodule must be **present and clean** in the main repo before a ticket
starts. Worktree setup hard-errors otherwise, naming the submodule and telling
you which of the two it is:

- `submodule <path> not found in main repo — run 'git submodule update --init' first`
- `submodule <path> is dirty in main repo — commit or stash changes first`

The "clean" rule is not fussiness: the copy is a snapshot, so uncommitted
submodule work would be silently duplicated into every ticket worktree with no
way back.

To copy only some of the submodules (a big docs or FPGA-board submodule nothing
builds against), list the ones you want:

```toml
[submodules]
paths = ["deps/bus_pkg", "deps/axi_verif"]
```

The list replaces `.gitmodules` discovery entirely — anything you leave out is
simply absent from ticket worktrees.

## Doctor waivers (`doctor-waivers.toml`)

`booley doctor` reserves `WARN` for an actionable failure mode. A clean setup
has no active warnings: fix the cause, or explicitly accept a deliberate
project constraint in `.booley_project/doctor-waivers.toml`. Waived findings
remain visible as `WAIVED` and are counted separately; the file does not hide
output or change failures.

Each warning prints a stable identity at the end of its line:

```text
WARN  trace unavailable for sim_fast [sim.trace-unavailable:sim_fast]
```

The part before the colon is `check`; the part after it is the optional
`subject`. Match that identity exactly:

```toml
version = 1

[[waiver]]
check = "sim.trace-unavailable"
subject = "sim_fast"
reason = "The upstream C++ harness has no trace switch; functional simulation is sufficient."
expires = 2026-11-01

[[waiver]]
check = "project.git-excludes-missing"
subject = ".booley_project"
reason = "This project deliberately tracks .booley_project in its parent repository."
permanent = true
```

The schema is intentionally strict:

- `version` must be the integer `1`.
- Every `[[waiver]]` needs a lowercase dot/dash-separated `check` and a
  non-empty `reason`.
- `subject`, when present, is an exact string match. Without it, the entry
  matches every subject for that check; use that broad scope only when one
  justification genuinely applies to all findings.
- Set exactly one of `expires` (a TOML date, active through that date) or
  `permanent = true`. Prefer expiration for temporary environment or upstream
  constraints.
- A duplicate `check`/`subject` pair, unknown key, invalid type, or malformed
  TOML makes Doctor fail rather than silently ignoring the intended policy.

Expired entries do not match; Doctor emits a note and reports the warning
again. `booley doctor --verbose` also notes active entries that matched no
current warning, which is the cue to delete stale waivers. Waivers apply only
to warnings—never failures or notes—and match structured IDs rather than
human-readable message text.

## Advanced setups

Every flow builds from a mandatory FuseSoC `.core` Target (see [Design
description](#design-description-core-and-tests-teststoml)). The two escape
hatches that remain, and what each is for:

- a **per-test non-RTL build step** (compiling the selected test's firmware,
  staging vectors) is [Pre-run
  commands](#pre-run-commands-flowssimpre_run_commands);
- a **new kind of analysis** with its own criteria is a [Custom
  MCP tool](MCP-TOOLS.md) — it adds an MCP tool alongside the built-ins, never a
  side door into `sim_pass_*`.

A simulator outside the built-in matrix is out of scope for Ticket Mode by
declared boundary; widening the matrix is the sanctioned extension axis
(per EDA tool: Edalize wiring → output parser → criteria-map row → Doctor probe).
The current matrix — sandbox image plus host EDA tools — is in
[SUPPORTED-EDA-TOOLS.md](SUPPORTED-EDA-TOOLS.md), and it grows over time.

**Language:** Booley drives SystemVerilog/Verilog only — VHDL is unsupported in
every Flow (see [SUPPORTED-EDA-TOOLS.md](SUPPORTED-EDA-TOOLS.md)).

### Custom sandbox image

To add project-specific EDA tools to every sandbox container, hand-author a
Dockerfile at `.booley_project/docker/Dockerfile` that extends `booley-sandbox`,
then run plain `booley init`:

```dockerfile
FROM booley-sandbox
RUN apt-get update && apt-get install -y --no-install-recommends my-tool
```

```console
$ booley init
[--] manual edits detected in docker/{Dockerfile} and image
     'myproj-booley-sandbox' is not built — using your files as the build
     input (leaving them untouched)
[OK] built myproj-booley-sandbox
```

That is one pass, not a recipe: init builds the image **from your file
verbatim** and re-seeds the devcontainer spec later in the same run. The image
name is derived automatically from the project slug whenever
`.booley_project/docker/Dockerfile` exists, so no matching `[sandbox].image`
entry is written or needed. Set `[sandbox].image` only to select a genuinely
custom name or a Booley flavor image.

What init decides, and how to steer it:

| Your `.booley_project/docker/` state | What `booley init` does |
|---|---|
| No Dockerfile, `[sandbox].pip_requirements` set | Generates `Dockerfile` + `requirements.txt` and builds the derived image |
| Hand-authored Dockerfile, image **not** built | Builds the derived image from your file untouched |
| Hand-authored Dockerfile, image already built | Leaves everything alone; rebuild with `docker build` yourself |
| `[sandbox].image` naming an image Booley doesn't recognise | Skips the step entirely — you manage that image |

"Hand-authored" means: the file does not carry init's generated header, *or* it
carries one but its contents no longer match the stamped self-hash (you edited
it), *or* it contains the line `# booley:keep`. Add `# booley:keep` to a file
that started life generated and that you now want to own — it is the explicit
"hands off" flag, and it works for `requirements.txt` too:

```dockerfile
# booley:keep
FROM booley-sandbox-riscv
RUN pip install --no-cache-dir -r /tmp/reqs.txt
```

Use the [post-setup hook](#post-setup-hook) (below) for per-worktree
preparation. Use a custom image for EDA tools that must exist in every container
before commands run.

**Changing an *already-built* image is a three-step operation, not a config
edit.** (Init's own build above already does step 1 for you.) The
devcontainer spec freezes the image name at seed time, and a running container
keeps the image it started on. So every image change (new `image`, or a rebuild
behind the same tag) needs:

1. `booley init --seed` on the host, which refreshes the devcontainer spec;
2. recreate the container with VS Code **Rebuild Container**, or
   `booley session down && booley session up`;
3. probe **inside the Session Runtime** (`booley session enter -- <new-eda-tool>
   --version`); a host `docker run` proves nothing about the container.

Skipping step 2 is the classic trap. `booley doctor` and `booley session up`
warn on image drift; treat those as "do the three steps".

#### RISC-V toolchain image (`booley-sandbox-riscv`)

RISC-V CPU-core projects (ibex, picorv32, biriscv, …) compile firmware/tests
with a RISC-V cross-compiler before simulating, so Booley ships a prebuilt
**`booley-sandbox-riscv`** image: the base sandbox plus:

- a **multilib RISC-V GNU toolchain** (xPack prebuilt, newlib) exposing both
  `riscv64-unknown-elf-` and `riscv32-unknown-elf-` prefixes and covering
  rv32i / rv32im / rv32imc (ilp32) plus the rv64 ABIs;
- **`srec_cat`** (srecord), which ibex's `.vmem` generation hard-depends on, and
  **`dtc`** (device-tree-compiler);
- **Spike** (`riscv-isa-sim`), the reference ISS for differential testing /
  co-simulation;
- the ratified **RISC-V International spec set** baked in for offline use at
  `$BOOLEY_RISCV_DOCS` (`/opt/riscv-docs`): the unprivileged + privileged ISA
  manual, the external debug spec, and the ELF psABI; **`pdftotext`** is
  included for shell/agent text extraction.

Point a project at it with the normal image selector:

```toml
[sandbox]
image = "booley-sandbox-riscv"
```

**Older projects need an explicit `zicsr`.** GCC 15 moved the CSR instructions
out of the base ISA into a `zicsr` extension, so a project defaulting to
`-march=rv32imc` (Ibex and most cores of that vintage) fails to assemble every
`csrr` / `csrw`. Override the ISA at the build invocation, not in the sources:

```bash
make -C sw/... ARCH=rv32imc_zicsr     # simple-system style firmware
make -C sw/... RV_ISA=rv32im_zicsr    # CoreMark's variable name
```

The right variable name is the project's own; grep its makefiles for `rv32i`.
Keep the override at the call site (the `post-setup` hook).

`booley init` owns this image like the base, building it if missing and
rebuilding it when the base moves (see the table above for what it does with a
name it doesn't recognise).

To build or refresh it by hand (this also rebuilds the base first):

```bash
./src/booley/data/docker/build-riscv.sh
```

Or pull the pre-built image from the GitHub Container Registry:

```bash
docker pull ghcr.io/boldaxolotl/booley-sandbox-riscv:latest
docker tag  ghcr.io/boldaxolotl/booley-sandbox-riscv:latest booley-sandbox-riscv
```

When the repo *also* has Python deps to bake (e.g. ibex), don't set `image`
directly. Instead hand-author `.booley_project/docker/Dockerfile` as
`FROM booley-sandbox-riscv` + your `pip install`, mark it `# booley:keep`, build
it, and point `[sandbox].image` at that image. This layers the deps on top of
the toolchain while keeping `booley init` from clobbering the file.

### Custom MCP tools

Project-specific MCP tools can be added via `.booley_project/mcp_tools/`, following the same base-class interface as built-in MCP tools. The Developer Agent discovers and invokes them just like built-in MCP tools. See [MCP-TOOLS.md](MCP-TOOLS.md) for the architecture and extension guide.

### Post-Setup Hook

Place a script at `.booley_project/hooks/post-setup.sh` (a `post-setup.py` or extensionless `post-setup` is also discovered) to run project-specific setup after worktree creation. The hook receives environment variables (`BOOLEY_WORKTREE`, `BOOLEY_PROJECT_DIR`, `BOOLEY_TICKET_SLUG`, `BOOLEY_TICKET_FILE`, `BOOLEY_SIM_FLOW_ENABLED`, `BOOLEY_IN_DOCKER`) for context.

Discovery order is `.sh`, `.py`, then extensionless; only the first existing
file runs. The hook runs once for each newly created ticket worktree, with that
worktree as its current directory, after project state has been copied and
before the Developer Agent starts. Shell and extensionless hooks run with the
platform Bash; Python hooks run with Booley's Python interpreter.

The hook has a 15-minute limit. A non-zero exit, timeout, or launch error blocks
the ticket setup and surfaces a bounded stderr diagnostic. Its stdout and
stderr are retained in debug logs. Write hooks to be idempotent: a recovered or
recreated worktree can run setup again. Generated tracked files are committed
on the ticket feature branch by the setup stage, so generate only deliberate,
reproducible project inputs; keep caches and bulky build outputs in ignored
runtime directories.

Stealth project state is intentionally outside the host repository's git
history. Back up or version `.booley_project/` separately, together with any
root `FUSESOC_IGNORE` quarantine marker. Booley propagates that marker into
ticket and baseline worktrees, but a fresh clone cannot reconstruct hidden
configuration that was never exported.
