# Step 2 — Project config (.core, tests.toml, booley.toml)

> Part of the `booley-setup` skill. Run in order, or invoke this step alone
> with `booley-setup 2`. Most of this step runs inside the devcontainer. When
> the plan selects host-provisioned EDA, the small host-authority bootstrap
> below runs first, before that runtime is created.
> It **consumes the approved `SETUP-PLAN.md`** (Step 0): every decision-level
> choice — flow routing, target names, toplevels, TB flavor, sentinels, image,
> data-file strategy — is already made there. This step turns those decisions
> into config files; it does not re-open them. When reality contradicts the
> plan, follow the deviation rule in `SKILL.md`.

## The three artifacts

Booley's canonical Project config is split across **three** artifacts on two
sides of one line — **FuseSoC owns design-description (how to build); Booley
owns verification-intent (what to verify) and runtime policy:**

1. **`.core` FuseSoC Target(s)** — *design-description*: source files
   (`filesets`), top modules (`toplevel`), build-time parameters/defines
   (`vlogdefine`/`vlogparam`), the EDA tool (`flow_options.tool`), and the
   build targets themselves.
2. **`.booley_project/tests.toml`** — *verification-intent*: per-Target test
   lists and the run-time `select` plusarg template.
3. **`.booley_project/booley.toml`** (slimmed) — *execution policy + project
   meta*: each Booley Flow's `enabled` setting, the Session Runtime image,
   optional approved EDA provisioning, and `[project].name`.

For a fresh IP the job, end to end: list the RTL and TB files in `.core`
filesets and tag the testbench; author the Target set the plan settled — for an
SV testbench one per intent (sim/lint/ASIC synthesis), more only where a design genuinely
needs several (a distinct config or a distinct toplevel); for cocotb **one per
test module** (`cocotb_module` is per-Target); never one per submodule; make
each run's verdict legible (sentinels, or cocotb's `results.xml`); vendor
static data files and build compiled ones on demand; quarantine unwanted
vendored cores; then validate everything in-sandbox. Each item is detailed in
Phase 2 below.

## How this step runs

Three phases, and no approval gate — the plan approval in Step 0 was the gate:

1. **Gather config-level evidence** — inspect the repo for the details the
   plan deliberately left to execution (exact file lists, include dirs,
   parameter spellings).
2. **Draft** — author the three artifacts from the plan's decisions.
3. **Validate, write, report** — validate the drafts, write them, run
   `booley doctor`, and report with a decision log tying each value to a plan
   row or to evidence.

The one stop is the deviation rule (`SKILL.md`): a finding that contradicts a
**decision-level** plan row (the routing is wrong, the planned toplevel
doesn't exist, the TB flavor was misjudged) halts for the user; a config-level
detail just resolves here, logged in the plan's §3 when it shifts a plan
detail.

## The Session Runtime, briefly

Every EDA tool executes inside the Session Runtime. The standard image is
`booley-sandbox`; it mounts the worktree at `/work`, and `fusesoc` is pinned in
the image. Its
authoritative build recipe is Booley's bundled `data/docker/Dockerfile` —
there is **no** `.booley/` directory in a target project; the file ships
inside the installed `booley` package. Find the data dir with:

```
python -c "import booley, pathlib; print(pathlib.Path(booley.__file__).parent / 'data')"
```

Two project-side extension points:

- **`post-setup` hook** — `.booley_project/hooks/post-setup.sh`, `….py`, or
  extensionless `post-setup` runs after each sandboxed worktree is created;
  it may install/build dependencies, copy files, create symlinks, or prepare
  generated inputs.
- **Project image** — to add open-source tools and project dependencies that
  must exist in every Session Runtime, build an image extending `booley-sandbox` from
  `.booley_project/docker/Dockerfile` and point `[sandbox].image` at it.
  Commercial EDA tools require a built-in Booley provisioning policy; a custom
  Project image does not make one publicly supported.

## Host-authority bootstrap

Run this subsection on the host only when approved plan row 14 selects a
host-provisioned EDA installation. The plan must already name the opaque
registration, canonical installation root, exact Project root, and whether an
approved License Profile is available.

First put the provisioning request in `.booley_project/booley.toml`; for the
current policy:

```toml
[eda.vivado]
provisioning = "host"
```

Then run the host-owned authority commands from the Project root. The Grant is
the single source of truth selecting the planned Installation Registration:

```console
booley eda installation register vivado_2025_2 \
  --kind vivado --source /path/to/Xilinx/2025.2
booley eda grant add /canonical/project/root \
  --kind vivado --installation vivado_2025_2
booley init --seed
booley doctor
```

If the administrator has approved a fixed floating-license topology, register
it before the Grant and add `--license-profile <name>` to `grant add`:

```console
booley eda license register <name> \
  --server-ipv4 <literal-ipv4> --server-hostid <server-hostid> \
  --lmgrd-port <fixed-port> --vendor-port <fixed-port>
```

Do not invent those values from Project files. When the approved paid-site
inputs are unavailable, omit the License Profile and record floating licensing
as an experimental, unverified limitation. After the authority and host Doctor
are clean, create the issued runtime with VS Code Rebuild/Reopen or
`booley session up --rebuild`, then continue the rest of this step inside it.

## Phase 1 — Gather config-level evidence

Step 0 already established the decision-level picture (flow routing, targets,
toplevels, TB flavor, sentinels, image, data files) — re-read `SETUP-PLAN.md`
before touching the repo, and start from its decision sheet rather than
re-deriving it. Prefer evidence from scripts, docs, manifests, CI, Makefiles,
filelists, and EDA wrappers over guesses:

- Inventory files with `rg --files`, excluding clearly transient or derived
  areas: `.git/`, local client settings, board logs/locks, build outputs,
  simulator/synthesis outputs, caches, dependency directories, and any
  `.core` found under a `build/`, `_build/`, or `.runtime/` tree (those are
  resolution artifacts, not sources).
- Read available docs and instructions: `README*`, `CONTRIBUTING*`,
  `CHANGELOG*`, `docs/`, `doc/`, `SPEC*`.
- Inspect CI, Makefiles, build scripts, `scripts/`, `sim/`, `tb/`, `dv/`,
  `test/`, filelists (`*.f`, `*.flist`), existing `*.core`, FuseSoC/Bender/
  IP-XACT manifests, and EDA scripts (`*.tcl`, `*.do`, `*.sh`, `*.py`).
  **Existing `.core`, FuseSoC/Bender/IP-XACT manifests, and `*.f` filelists
  are first-class evidence** — they map directly onto `.core` filesets,
  targets, and parameters.
- **Confirm the software-build dependency the plan flagged (row 8).** If a
  sim input is *compiled from repo source* (CPU-core firmware, a `$readmemh`
  ROM, generated RTL — tell-tales: `$readmemh`/`$readmemb` in the TB; a
  `firmware/`/`sw/`/`fw/`/`tests/` dir of `.c`/`.S`/`.asm`; a `Makefile`
  emitting `.hex`/`.mem`/`.bin`; a `riscv*-`/`arm-none-eabi-` prefix), it is
  a **required toolchain**, not a data file: record the missing compiler and
  remedy in the evidence grid (bake the toolchain into a project image +
  rebuild on demand — see "Data files & built artifacts" below). **Never**
  compile it once and vendor the prebuilt blob.
- Inspect `.booley_project/hooks/post-setup.*` if present — evidence of
  project-specific sandbox setup.
- Inspect the sandbox build recipe (the bundled `data/docker/Dockerfile`
  above) and `docs/CONFIG.md` when considering image provisioning or the
  Linux x86-64 host-provisioned Vivado policy. Treat floating FlexNet behavior
  as experimental until the required paid-site evidence exists.
- Read representative RTL/testbench headers only as needed to identify source
  dirs, include dirs, top modules, packages, and testbench tops.
- Read any existing `.core`, `.booley_project/tests.toml`, and
  `.booley_project/booley.toml`.

Keep the evidence as a Markdown table with columns `Topic`, `Evidence`,
`Chosen value`, `Source` (a `SETUP-PLAN.md` row, or this step's evidence);
carry the grid into the final report. Do not stop to have it approved — the
plan already was. Run any of the plan's execution-time checks that touch this
step's artifacts (target resolution, ingest smoke) as soon as the drafts
exist.

## Phase 2 — Draft config

If files exist, propose minimal diffs instead of wholesale rewrites.

### 2a. The `.core` (design-description)

Use the ownership decision recorded in plan rows 2 and 16:

| Git footprint | Native `.core` present? | Required action |
| --- | --- | --- |
| open | yes | Modify/modernize the appropriate native core; never create a parallel adapter core. |
| open | no | Author a tracked project core at the repo root or beside the RTL. |
| hidden | either | Leave every tracked native core unchanged and author a distinct-VLNV adapter under `.booley_project/cores/`. |

In a native core, touch only the Targets the plan selects; unrelated historical
or board Targets are not setup scope. If more than one native core is a credible
owner, stop and ask which should carry the new/modernized Target. Core placement
follows the git-footprint decision, not `[stealth]`, which controls only the
commit-message scrub.

**Template:** `../CORE_TEMPLATE.yaml` (annotated shape; copy it into a real
`*.core` file — the template ships as `.yaml` only so `.core` discovery does
not pick it up).

Authoring rules:

- **Tag the testbench.** Every TB fileset (or TB file) carries `tags: [tb]`.
  Source Isolation partitions RTL vs TB by this tag; an untagged TB is a
  setup-time error (`booley doctor`
  hard-fails a sim target whose TB is untagged).
- **Mark headers `is_include_file: true`** so synthesis feeds them as include
  *directories*, not as compiled sources.
- **Defines and parameters live here, not on the CLI.** A define is a
  `paramtype: vlogdefine` parameter; a `-G` parameter is
  `paramtype: vlogparam` with a **literal** `datatype` (no expr-params — see
  the security rules below).
- **Target names are project-unique**, follow `<axis>_<subject>` (`sim_` for
  `sim`/`elab`, `lint_`, `synth_`, `fpga_`, then a distinguishing subject,
  coarse to fine — `sim_soc`, `synth_matmul_b8`), and become the `<target>` in
  `sim_pass_<target>` / `lint_clean_<target>` / `synthesis_ok_<target>` and the value a
  Booley Flow passes as `--target`. The axis leads because nothing in CAPI2
  distinguishes an ASIC Target from an FPGA one — both resolve as `generic`.
  `booley doctor` notes names that don't. Do not author a `default` target
  unless another `.core` `depend:`s on this one — it is FuseSoC's
  dependency-build fallback, never a selectable config, and doctor flags a
  dead one (drop it from a depended-on core and that core silently
  contributes zero filesets).
- **Per-EDA-tool Target families:** `verilator` ⇒ may carry
  `sim_pass`/`lint_clean`; `verible` ⇒ `lint_clean` (lint only); `icarus` ⇒
  `sim_pass`; `yosys` ⇒ `synthesis_ok`; `vivado` ⇒ `fpga_impl_ok`. Split
  intents into separate targets when one design needs several.
- **Verilator 5 sim Targets need timing options.** Any event-driven Verilog
  TB (delays, `@(...)` waits — i.e. the normal self-checking TB this skill
  produces) fails Verilator 5 elaboration with `%Error-NEEDTIMINGOPT` unless
  the sim Target passes `--timing`. Give every verilator sim Target
  `flow_options: {tool: verilator, verilator_options: [--timing, --main, --exe]}`
  (`--main`/`--exe` generate and build the C++ main wrapper — there is no
  Booley-injected default). Icarus needs none of this.
- **Verilator 5 also promotes warning classes to hard errors** that the RTL's
  own era never tripped — `%Error-ENUMVALUE` (assigning a non-enum value to an
  enum) is the common one, and it hits **lint** Targets as well as sim. Nothing
  in the design is wrong; the class was a warning when the code was written.
  The failure tail names the class in its `%Error-<CLASS>` token: copy it into
  `--Wno-<CLASS>` in that Target's `verilator_options` (or waive it in a `.vlt`
  file in the fileset). Fix per Target, and note the waiver in AGENTS.md so a
  later reader knows it is an era gap, not a silenced real bug.
- **Optional style lint (offer, never impose).** Only if the
  plan's row 11 says yes: author a **second** lint Target (suggested name
  `lint_style`) with `flow: lint` + `flow_options: {tool: verible}` over the
  RTL fileset; it runs `verible-verilog-lint` in the sandbox and sets
  `lint_clean_lint_style`. Optional tuning is design-description in the
  `.core`: a single `file_type: veribleLintRules` rules-config file and/or
  `file_type: veribleLintWaiver` waiver files in the Target's fileset
  (analogous to Verilator's `.vlt`). Never change existing Target shapes.
- **`fpga` Targets:** `flow: generic`, `flow_options: {tool: vivado, …}`;
  `xdc` is a typed fileset, `top` is `toplevel`, and the board `part` plus
  `out_of_context` live in Target `flow_options`. **No `hooks:`** (decision 21,
  below).
- **Trace: nothing to wire.** `booley flow sim --trace` works without any fileset
  change — the trace overlay injects the `booley_vcd_dump` dump module from
  Booley's `refs/` at run time. Do **not** add it to the design's fileset: a
  tracked `booley_vcd_dump.sv` leaks Booley into the repo's git history
  (Stealth Mode).
- **Vendored/example cores** you do not want discovered get a
  `FUSESOC_IGNORE` marker file in their directory — Booley's `.core` scanner
  skips any directory carrying one (mirrors FuseSoC's own scanner).

#### Authored cores in a hidden project directory

When stealth mode keeps Booley state hidden, leave native tracked `.core` files
byte-for-byte unchanged and place the adapter `.core` under
`.booley_project/cores/`. Give it a distinct VLNV so it cannot collide with a
native core. Author its filesets relative to the **repository root**:
`rtl/top.sv` names upstream RTL directly, while project-owned constraints use
paths such as `.booley_project/cores/constraints/core.sdc`. Do not create
resolution symlinks or use escaping `../../` paths. With explicit
`[stealth] enabled = true`, `booley init` materializes ignored root-level core projections
and every Booley resolution refreshes them. Validate raw FuseSoC only after the
projection exists.

#### Editing a native `.core` in place (open footprint only)

Required instead of authoring a parallel one when an open-footprint repo already
ships the appropriate CAPI2 core. Modernize only selected Targets; leave
unrelated legacy Targets intact. This has its own traps, and most only explode under *real* fusesoc while
Booley's cheap `.core` reader stays green. Re-validate with
`booley doctor --deep` (or, by hand in the sandbox,
`fusesoc --cores-root <dir> run --setup --work-root "$(mktemp -d)" --target
<target> <vlnv>` — raw fusesoc takes `--cores-root` *before* `run` and rejects
Booley's `<vlnv>#<target>` spelling) after **every** `.core` edit, not just the
first:

- Legacy EDA-tool-API Targets (`default_tool:` + `tools:` blocks) should be
  converted to the flow API (`flow:` + `flow_options:`). Booley falls back to
  the declared EDA-tool family, but that cannot express whether a multi-purpose
  EDA tool such as Verilator means sim or lint. Doctor names the ambiguity and the
  rewrite shape is in `../CORE_TEMPLATE.yaml`.
- `depend:` keys must be YAML **arrays** — an empty scalar `depend:` parses
  fine and then kills target resolution with a truncated fusesoc error.
- **Delete the `provider:` block.** A published core often carries
  `provider: {name: github, user: …, repo: …}`, which tells fusesoc the sources
  live *remotely*: every local `fusesoc run` then tries to re-download the core
  instead of using the checkout you are standing in, and the sandbox egress
  proxy answers `403`. The error names neither the block nor the fix. The repo
  under setup **is** the source — drop the block. Doctor and ticket preflight
  reject a configured in-tree core with this offline-incompatible declaration.
- The yosys generic flow requires `arch:` (e.g. `arch: xilinx`) even when the
  value is meaningless for an ASIC run — dropping it fails only at resolution
  time.
- **Never invent CAPI2 keys.** Annotations go in `tags:`
  (`tags: [vendored]`) — an unknown key like `vendored: true` can invalidate
  the entire file under real fusesoc with all its targets.
- The YAML-anchor idiom (`<<: *default_target` + a `filesets:` override)
  **replaces** the fileset list, it does not merge — upstream targets built
  on it may be silently broken; list filesets explicitly in targets you
  author.

#### Security & confinement

The generated config must pass Booley's `.core` validator (`booley doctor`
runs it). "Validated" means **provenance + confinement**, not a content scan.
Author within these limits:

- **No `fpga` hooks.** A Target whose EDA tool is `vivado` (or any future
  FPGA implementation EDA tool) must not declare a `hooks:` block. Commercial
  provisioning is a fixed, reviewed Booley policy; Project-supplied imperative
  hooks are not part of that policy. Resolution-time `generators` are fine for
  `fpga` (they run inside the Session Runtime).
- **No expr-params.** Every `parameters:` entry must use a CAPI2 literal
  `datatype` (`bool`/`file`/`int`/`real`/`str`). An HDL expression cannot be
  a faithful `-G` vlogparam and Booley ships no evaluator, so
  resolve it to a literal, or express it as a `vlogdefine` whose value is the
  expression text.
- **Imperative scripts stay out of the agent's write Scope.** Any
  `generators`/`hooks` script files must live **outside** the writable
  category dirs (`rtl/`, `tb/`, …) — e.g. under `scripts/` — so the Scope
  pre-commit hook makes them agent-immutable. Scripts the agent could edit
  are rejected.

#### Data files & built artifacts

*Static* data files the TB reads (`$readmemh` images, vector files) are
fileset entries with `file_type: user` and `copyto: <name-the-TB-opens>`,
staged into the build tree at that name.

- **Toolchain-built inputs are NOT vendored data — build them, don't commit
  them.** If the TB input is the *output of a compiler/assembler* the project
  ships source for — CPU-core firmware built from C/asm, a ROM image emitted
  by a build step, generated RTL — do **not** commit the prebuilt binary
  (e.g. a `firmware.hex`): it freezes one build in, hides the real dependency
  (the cross-compiler), and dirties the repo (the footprint guardrail in
  `SKILL.md`). Instead make the artifact reproducible **inside the sandbox,
  on demand**:
  1. Give the sandbox the missing toolchain.
     - **RISC-V cores** (ibex, picorv32, biriscv, …): use the prebuilt
       **`booley-sandbox-riscv`** image — it bakes a multilib RISC-V GNU
       toolchain (`riscv32/64-unknown-elf-`), `srec_cat`, Spike, and the
       RISC-V spec set. Just set `[sandbox].image = "booley-sandbox-riscv"`:
       `booley init` recognises the name and builds/refreshes the image
       itself. If the repo also has Python deps to bake (ibex), extend it in
       `.booley_project/docker/Dockerfile` with `FROM booley-sandbox-riscv`
       (mark `# booley:keep`) and point `[sandbox].image` at that image.
     - **Other toolchains**: add them to a project image extending
       `booley-sandbox` at `.booley_project/docker/Dockerfile` and point
       `[sandbox].image` at it. Tag it with a name *other* than the
       auto-generated `<slug>-booley-sandbox` (which `booley init` regenerates
       and would overwrite).
  2. Regenerate the artifact per worktree from a
     `.booley_project/hooks/post-setup` hook (or a `.core` build step), e.g.
     `make TOOLCHAIN_PREFIX=riscv64-unknown-elf- firmware/firmware.hex`, so
     every run builds it fresh from source rather than reading a stale
     committed blob.

  Reach for vendoring only for genuinely **static** inputs the upstream repo
  already ships and has no build step for (fixed test vectors, reference
  dumps).
- **Warning — vendored data files may be gitignored.** A static data file you
  legitimately vendor into a ported repo (e.g. a `*.bin` memory image the
  upstream ships) often matches the **upstream** repo's `.gitignore`.
  `git add` then silently does nothing, the file is never tracked, and the
  resolved build has no input — the sim fails downstream with a confusing
  error. Force-add it — `git add -f <file>` — and confirm it landed with
  `git ls-files <file>` (empty output means still untracked).

#### Default-on trace/log sinks (SETUP-25)

Many testbenches enable a waveform/trace/log dump *by default* — a Verilator
`+*trace*`/`--trace` build, an unconditional `$dumpfile`/`$dumpvars`, a
per-cycle `$fwrite`/`$fdisplay` to a file, or a vendored tracer module (e.g.
Ibex's `ibex_tracer` wrote 272MB **per run**; a runaway once left 27GB and
filled the disk, killing an in-flight synth). Grep the TB for `$dumpfile`,
`$dumpvars`, `$fopen`/`$fwrite`, and `trace`, and make every such sink
**default-off, opt-in via a plusarg** (e.g. wrap it in
`if ($value$plusargs("trace=%d", en) && en) …`) so a plain pass/fail
`sim` writes nothing. Booley's `--trace` path drives its own waveform;
the TB should not dump on every run. (Booley also enforces a per-run disk
budget — `[flows.sim].max_rundir_bytes`, default 5 GiB — that kills a
runaway, but a default-off sink is the real fix.)

#### Cocotb Targets

The project's testbench flavor — `sv`, `cocotb`, or `mixed` with a default —
is a **project convention fixed at setup**, not a per-ticket choice
and was decided in the plan's row 4 (a mandatory grill
question in Step 0); read it from `SETUP-PLAN.md`, don't re-ask. Targets
authored here embody it, and during tickets the Developer follows the
existing Target's shape.

A **Cocotb Target** is an ordinary sim Target whose flow options declare the
Python test module. The authoring shape:

```yaml
filesets:
  rtl:
    files:
      - rtl/counter.sv
    file_type: systemVerilogSource
  tb:
    files:
      - tb/test_counter.py: { file_type: user, copyto: test_counter.py }
      # multi-file TBs: copyto preserves the package layout in the build root
      - tb/helpers/util.py: { file_type: user, copyto: helpers/util.py }
    tags: [tb]                     # Source Isolation partition, same as SV

targets:
  sim_cocotb:
    filesets: [rtl, tb]
    toplevel: counter              # what the Python TB attaches to (see below)
    flow: sim
    flow_options:
      tool: icarus                 # v1: icarus or verilator
      cocotb_module: test_counter  # marks this as a Cocotb Target
      iverilog_options: [-g2012]   # SystemVerilog sources need -g2012
      timescale: 1ns/1ps
```

Rules that differ from SV Targets:

- **`toplevel` is whatever the Python TB attaches to.** Usually the DUT
  itself: no HDL wrapper, cocotb's `dut` handle IS the toplevel (decision 3),
  and DUT Info degenerates accordingly. **But not for interface-based
  designs.** If the DUT carries SystemVerilog interfaces on its ports
  (`taxi_axis_if.snk s_axis_tx`), cocotb's BFMs bind to interface *instances*
  (`AxiStreamBus.from_entity(dut.s_axis_tx)`) — and an instance has to be
  instantiated somewhere, so such projects ship a thin HDL wrapper per
  testbench that declares the interfaces and instantiates the DUT. **That
  wrapper is the `toplevel`**, and it is often named after the test module
  (`toplevel: test_taxi_eth_mac_10g` + `cocotb_module:
  test_taxi_eth_mac_10g`). This works as-is — do not go hunting for a way to
  make the DUT the toplevel; point `toplevel` at the module the TB actually
  drives. (The same design will also need a *flat-port* wrapper for
  lint/synth — a different wrapper, with plain signal ports; see the
  non-scalar-toplevel-ports determinant in `0-plan.md`. Packed-struct ports are
  *not* this case and need no wrapper.)
- **`tests.toml` lists cocotb test-function names**, and must **not** declare
  a `select` plusarg template — selection is an env-var filter Booley builds;
  a `select` on a Cocotb Target is a setup-time error (see
  TESTS_TEMPLATE.toml's cocotb section). `skip` works unchanged.
- **No sentinels.** The verdict comes from cocotb's `results.xml`; do not add
  `pass_sentinels`/`fail_sentinels` for a Cocotb Target's sake.
- **Image-provisioned only in v1**: the image pins cocotb 2.x
  plus `numpy`, `cocotbext-axi`, `cocotbext-uart` (no network in Booley Flows —
  anything else is vendored in the project tree; note `cocotbext-spi` has no
  cocotb-2.x-compatible release yet). Commercial simulators are unsupported.
- **A project on different pins bakes its own image.** Upstream testbenches
  that use `TestFactory` or `cocotb.utils.get_time_from_sim_steps` are
  **cocotb 1.x** and will not run on the 2.x base image. Point
  `[sandbox].pip_requirements` at the project's own pin set (from its
  `tox.ini` / `requirements.txt`) and re-run `booley init` to build the
  project image. **`[sandbox].pip_requirements` is the only source of baked
  deps — nothing is auto-discovered**, so a repo-root `requirements.txt` is
  ignored until you list it here. Any path (any name, any depth, relative to
  the repo root) works; `init` warns on an entry it can't find. Booley speaks
  both dialects — it probes `cocotb-config --version` and emits `TESTCASE`
  instead of `COCOTB_TEST_FILTER` on 1.x, with no config from you.
  **Bake the FULL pinned stack, not the subset the TB seems to need.**
  Testbenches routinely `import cocotb_test.simulator` and `import pytest` at
  module scope for a pytest entry point Booley never calls (Booley drives the
  simulator itself). They look like trimmable test-*runner* deps, but they
  run at **import** time, so dropping them makes cocotb fail to import the
  testbench at all — and that costs an image rebuild plus a session recreate
  to undo.
- **One module per Target.** Multiple test modules require multiple Targets.
- Point new-TB authoring at `refs/cocotb_tb_style_guide.md` (the SV
  `tb_style_guide.md` does not apply to Python testbenches).

### 2b. `tests.toml` (verification-intent)

Per-Target test lists plus an optional run-time selector.

**Template:** `../TESTS_TEMPLATE.toml`.

- `select` must be **exactly one well-formed option token** with no whitespace:
  either a plusarg (leading `+`) or a getopt argument (leading `-` / `--`).
  Only `{index}` and `{name}` substitute. The default is `+test_id={index}`;
  CPU harnesses may instead use forms such as `--meminit=ram,{name}`.
- A section sets `tests` **or** `test_list`, never both.

### 2c. The slimmed `booley.toml` (execution policy)

**Template:** `../BOOLEY_TEMPLATE.toml`.

What goes here:

- **Stealth mode — always write what rows 16 and 20 settled.** Setup's default is the
  explicit `[stealth] enabled = false`; write `enabled = true` only when the
  user opted in or chose hidden authored cores. Do not omit the block: Booley's runtime fallback
  for a missing key is on, so omission would reverse setup's disabled default.
  If row 16 explicitly chose to exclude repository-native `.core` files, also
  write `ignore_native_cores = true`; otherwise omit it (default false). This
  switch is valid only with `enabled = true`.
- **Agent backend — write what row 19 settled.** If the plan chose `codex`,
  emit the `[agent]` block (`provider = "codex"`, plus `auth` — usually
  `subscription` to bill the `~/.codex/auth.json` login rather than an API
  key). If it chose `claude`, either write `[agent] provider = "claude"`
  explicitly or omit the block (Booley defaults to `claude`) — but never leave
  the choice unrecorded when the user asked for `codex`, or every agent run
  silently bills the wrong provider.
- **Feedback mode — write what row 21 settled**, whenever it is anything other
  than the `ask` default: `[feedback] mode = "email"`, `"file-only"`, or
  `"off"`. Writing it
  down is what makes the answer stick, so Step 6 (and every later re-run) honours
  it instead of asking again. If the user named terms they want scrubbed from any
  outgoing report, add them here too: `redact_extra = ["…"]`. See CONFIG.md →
  "Feedback".
- **MCP tool availability — exactly what the plan decided.** Every installed
  built-in and every valid MCP tool under `.booley_project/mcp_tools/` is discovered
  automatically. Write `[flows.<name>].enabled = false` for an explicit Flow
  opt-out; use `[mcp_tools.<name>].enabled = false` for a Specialist or other
  non-Flow MCP endpoint. There is no source allowlist.
- **First-run Flows start disabled** (`enabled = false`) for `sim`,
  `elab`, `lint`, and `synth` unless the user explicitly asks
  to wire a flow now. Every Flow command runs in the Session Runtime; the EDA
  tool (verilator/iverilog/yosys/vivado) lives in the `.core` Target's
  `flow_options.tool`. Host-provisioned Vivado is requested separately with
  `[eda.vivado]` and requires an exact Project Grant. Never write a `backend =`
  line — the knob is retired and config validation rejects every
  spelling with the exact replacement.
- **Every enabled Flow needs `[flows.<flow>].default_target`** — the name of the
  `.core` Target it drives (e.g. `default_target = "sim_core"` for `sim`). An
  enabled Flow that names no Target can never run, and `booley doctor` fails
  on it — including `elab`, which is enabled by default; point it at an elaboration-capable Target
  (the `lint` Target usually works) or set `enabled = false` to opt out.
  While a Flow is disabled (the first-run default), leave `default_target` unset; set
  it when the flow is wired up.
- **A synthesis matrix needs a heaviest calibration Target.** When
  `[flows.synth].default_target` contains several comma-separated Targets, write the
  row-13 decision as `[flows.synth].calibration_target = "<target>"`. It must
  be one member of the configured matrix. Do not infer "heaviest" from target
  order or name; carry the evidence from the approved plan. For a singleton,
  Doctor uses that Target implicitly.
- **Reserve memory for one HEAVY job.** Write `[jobs].heavy_memory` when row 13
  has an evidence-backed value. It is an admission/Doctor budget, not a hard
  per-process limiter; `[sandbox].memory` remains the one container cgroup
  limit. Step 4 calibrates both from the real heaviest-target synthesis.
- **Author fail-path self-tests now, before Step 4.** Every enabled verification
  Flow needs a conventional deliberately bad fixture that the Flow grades as a
  design failure; Doctor infers each known-good case from the Flow's configured
  default Target. For simulation, mirror replacement build-tree files beneath
  `.booley_project/selftest/sim/bad-overlay/`; Doctor applies the overlay only
  to its bad run, so do not leak this internal fixture through a
  `pre_run_commands` branch. Lint uses a Target named `lint_selftest_bad` with
  a tiny tracked source containing an undeclared RHS (an undeclared LHS is only
  an implicit-net warning). The bad lint fixture is genuine project
  verification input, so keep it in the tracked RTL/TB tree and reference it
  from the `.core`—never hide it under `.booley_project/`. Do not add a
  `[flows.<flow>.selftest]` table; that mapping is retired. Step 4 consumes
  these fixtures; it must not be the first step to discover they were omitted.
- **Sentinels** (SV Targets; the plan's row 5): make sure a run's verdict is
  legible — either the TB emits `[SIM_RESULT] PASSED`/`FAILED`, or set
  `[flows.sim].pass_sentinels`/`fail_sentinels` to the TB's own wording.
  Never require the user to rewrite a testbench they don't own. (Cocotb
  Targets are exempt — verdicts come from `results.xml`; sentinels are
  ignored.) **Write the set fail-dominant**: `fail_sentinels` must carry the
  TB's *input/setup-error* wording too (a missing vector or firmware file —
  `"… is not available!"`, `$fopen` failed — then `$finish`), not just its
  assertion failures. A pass sentinel that prints once per case has usually
  printed dozens of times before such a run dies, so without that entry the
  run scores **PASS**. A fail sentinel wins ties (CONFIG.md), which is exactly
  what makes the fail-dominant set safe — prove it with the deliberate
  fail-path run below (delete an input, expect FAIL).
- **Per-test non-RTL build steps** (the plan's row 6): declare them as
  `[flows.sim].pre_run_commands` — shell lines run inside the Session Runtime
  before each sim run, under the `BOOLEY_*` env contract
  (`BOOLEY_TEST_NAME`, `BOOLEY_RUN_CWD`, …). Pair with `run_cwd` when the TB
  reads fixed-name inputs (e.g. `$readmemh("inst.pat")`) from a specific dir,
  and bake the toolchain the commands need into the project image.

### Presenting the draft

Per the **onboarding voice** (SKILL.md) — assume the reader is new to Booley.
Before showing the proposed config, give a concise plain-English setup summary.
Restate (don't re-derive) the three artifacts, the first-run Flow and Specialist opt-outs,
and why those capabilities start disabled, then cover the project-specific bits:

- **What the standard Session Runtime image likely provides:** common open-source RTL
  tooling — FuseSoC, Verilator, Icarus Verilog, Yosys/ABC, sv2v, bwave, make,
  GCC, Python, Node.js, Rust. Project extras (RISC-V GCC toolchains,
  `srec_cat`, firmware SDKs) still need evidence or user confirmation;
  `booley doctor` can verify the actual image. Licensed EDA support exists only
  through a built-in provisioning policy (currently Vivado 2025.2), not an
  environment module or arbitrary Project extension.
- Whether `.booley_project/hooks/post-setup.*` exists or should be proposed
  later to prepare project-specific sandbox dependencies.
- Whether `[sandbox].image` is needed because the project depends on EDA tools
  that must be installed before any sandbox command starts.
- **Which decisions came from evidence:** name the files or directories that
  justify project name, filesets, include dirs, top modules, targets,
  parameters, defines, and test lists.

Prefer beginner-readable language, for example:

> I have not written anything yet. This is the proposed first Booley config.
> The `.core` file describes how the design builds (files, tops, parameters);
> `tests.toml` lists the tests to run; `booley.toml` keeps each Flow and Specialist visible
> but not yet wired to a real EDA flow. The next setup skills will wire
> simulation, lint, and synthesis.

## Phase 3 — Validate drafts

Before writing:

- Parse the `.core` as YAML and `tests.toml`/`booley.toml` as TOML.
- Confirm referenced source/include files and category dirs exist.
- Confirm every sim target has a `tags: [tb]` fileset, target names are
  project-unique, parameters use literal datatypes, and no `fpga` hooks
  / in-Scope imperative scripts are present (the security rules in 2a).
- Confirm `tests.toml` `select` templates are single well-formed option tokens.
- Run `booley doctor` (its "FuseSoC .core checks" phase runs exactly these
  audits); when the sandbox is available, `booley doctor --deep` additionally
  resolves each Target through `fusesoc run --setup`.
- Where practical, run each Booley Flow in the sandbox against a resolved Target
  (`booley flow <name> …`) and prove the fail path with a deliberate
  mutation — a passing-only check is not evidence the Flow can detect a
  regression. Step 4 (Doctor) formalizes this as the convention-discovered
  per-Flow fail-path self-test.
  **These runs are minutes long and announce nothing when they end** — start
  them detached and poll them per SKILL.md → "Waiting on long runs". Never park
  "standing by" mid-run. This is also where the plan's smoke pin gets its real
  numbers: time every candidate Target, re-pin the smoke to the measured
  fastest, and log the re-pin in the plan's §3 (a minor deviation) — the
  host-side guess is routinely wrong, since test count does not predict runtime.
- Report validation failures as blockers or explicit low-confidence
  assumptions.

## Phase 4 — Write, validate, and report

Once the drafts validate, write the files — the plan approval in Step 0
covers the writes; there is no per-file gate. Re-read them from disk, run
validation again (`booley doctor`) when practical, then present the result as
a review artifact (not a question):

- Exact content for new files, or unified diffs for existing files, with a
  beginner-readable summary of what each file does.
- A decision log tying each non-obvious value (fileset membership, tags, EDA tool
  choice, toplevel, parameters, test lists) to its `SETUP-PLAN.md` row or to
  this step's evidence — anything tied to neither is a deviation and belongs
  in the plan's §3.
- A short note that leaving a Flow disabled (`enabled = false`) is expected at this stage and will be wired
  on by the per-Flow setup skills.

If the user wants contributor guidance next, use Step 3 (AGENTS.md) after the
approved config is on disk.
