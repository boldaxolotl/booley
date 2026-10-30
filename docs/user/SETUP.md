# Setup

This guide connects Booley to a specific RTL project. Before you start, finish
[Install](INSTALL.md), including Host Bootstrap, and sign in to your Claude Code
or Codex CLI on the host.

Setup has two parts:

1. **Project Initialization** (`booley init`) makes your codebase a Booley
   Project. It is a plain CLI command that you run in a host terminal; no agent
   is involved.
2. The **`booley-setup` skill** completes design-aware Project Setup. Your
   Claude Code or Codex agent runs it. Host Bootstrap already installed it,
   and this guide hands off to it (see
   [The `booley-setup` skill](#the-booley-setup-skill) below).

Two paths, sharing everything up to that handoff:

- **A new IP, from scratch.** The easy path. Every flow is green by
  construction with the built-in Booley Flows, so you skip the feasibility triage
  entirely: `booley init --scaffold` writes a working,
  wired-together starter project, and the skill drives it to a green
  `booley doctor` (Booley's built-in health check, explained at
  [Step 4](#porting-an-existing-project-plan-then-execute)).
- **An existing RTL project (a port).** The harder path: a per-flow feasibility
  triage and hand-authored config (a `.core` is usually a mechanical
  restatement of the filelists you already have). See [Ports](#ports) for
  open-source IPs that have gone through this path.

Both paths begin with host-side Project Initialization, then use their own skill mode. For
what each configuration field *means*, see [CONFIG.md](CONFIG.md) for
`booley.toml` and `enabled`, [TARGETS.md](TARGETS.md) for `.core` and
`tests.toml`, [FLOW-SIM.md](FLOW-SIM.md#simulation--passfail-sentinels-flowssim)
for sentinels (the log-pattern strings that decide a simulation's pass/fail
verdict), and [SANDBOX.md](SANDBOX.md#commercial-eda-provisioning) for EDA
provisioning.

> An agent session can follow this file too. The [context map](../../GLOSSARY-MAP.md)
> points to the definition of any Booley term you hit here.

## Ports

Booley has been set up end to end on these open-source designs, using their
upstream RTL and testbenches with no source restructuring and almost no
testbench edits:

| Design | What it is | Upstream build | Public Booley project |
|---|---|---|---|
| [PicoRV32](https://github.com/YosysHQ/picorv32) | Small RV32 CPU core | Makefile | [booley-prj-picorv32](https://github.com/boldaxolotl/booley-prj-picorv32) |
| [Caliptra](https://github.com/chipsalliance/caliptra-rtl) | Hardware root of trust: SoC with a RISC-V core and firmware | Verilator testbench and firmware scripts | [booley-prj-caliptra](https://github.com/boldaxolotl/booley-prj-caliptra) |
| [Ibex](https://github.com/lowRISC/ibex) | RV32 CPU core | FuseSoC | — |
| [biRISC-V](https://github.com/ultraembedded/biriscv) | Dual-issue RV32 CPU core | Makefiles | — |
| [OpenC910](https://github.com/T-Head-Semi/openc910) | Out-of-order RV64GC core, 415 K LOC | Vendor test runner | — |
| [verilog-pcie](https://github.com/alexforencich/verilog-pcie) | PCIe interface IP | cocotb under pytest | — |
| [taxi](https://github.com/fpganinja/taxi) | 10G Ethernet MAC | cocotb under pytest | — |

**Use the public projects as references.** Each one is a complete Booley
Directory (the `.booley/` that setup produces) published as its own
repository. When you set up your own design, compare your `booley.toml`,
`.core` files, and `tests.toml` with theirs. PicoRV32 is the small one; Caliptra shows a large SoC with firmware
tests. Both are also on the [Demos](https://booley.dev/demos/) page.

The upstream build systems have nothing in common, which is the useful part:
the real requirement is just RTL that some EDA tool can already build. If your
project clears that bar, the [`booley-setup`](#the-booley-setup-skill) skill can
adapt it.

## Initialize the Project · host

**For an existing project, commit or stash your changes first** (a fresh clone
is best). If the checkout has Windows (CRLF) line endings, which is common on
Windows, init converts them to LF, and it refuses to do that on a dirty tree.

The command depends on your path:

```bash
cd your-rtl-project            # porting an existing project
booley init
```

```bash
mkdir my_ip && cd my_ip && git init      # a new IP, from scratch
booley init --scaffold my_ip
```

Init asks which agent provider (`claude` or `codex`) and auth policy
(`subscription`, `api-key`, or `auto`) this project uses. Press Enter to accept
the `claude` and `auto` defaults, or pass them as flags:

```bash
booley init --provider codex --auth subscription
```

The choice is recorded in the `[agent]` table of `.booley/booley.toml`.
Re-running init keeps it; to change it later, edit that table (see
[CONFIG.md](CONFIG.md)).

`booley init` is safe to re-run. It never overwrites a hand-edited
`.booley/docker/{Dockerfile,requirements.txt}` (one missing its
`# AUTO-GENERATED` header or carrying `# booley:keep`). Project
Initialization walks through:

1. Creates `.booley/` with starter config files
2. Records your agent provider and auth policy
3. Checks credentials for that provider
4. Prepares the Project's Sandbox Image: the Docker image your agent and EDA
   tools run in, built on top of Booley's base image
5. Installs Booley's Git hooks
6. Writes the devcontainer config, so VS Code can open the Project in the
   Sandbox (Reopen in Container)
7. Prints any follow-up advice

### What `--scaffold` adds

> **New IP from scratch only.** Porting an existing project? Skip to
> [The `booley-setup` skill](#the-booley-setup-skill).

On top of everything above, `--scaffold` writes a starter project. On a terminal
it asks the choices below (synthesis mode only when ASIC is enabled); each
answer is also a flag, for non-interactive runs that otherwise take the defaults:

| Choice | Options | Flag |
| --- | --- | --- |
| Simulator EDA tool | Verilator / Icarus | `--sim-eda-tool` |
| Testbench style | SystemVerilog / cocotb | `--tb-style` |
| Lint EDA tool | Verilator / Verible | `--lint-eda-tool` |
| Flows to enable | ASIC synthesis, FPGA (and the part) | `--asic` / `--no-asic`, `--fpga-part` |
| ASIC synthesis mode | `physical` (default) / `logical` | `--synth-mode` |
| Keep Booley out of git history | off (default) / on | `--stealth` |

cocotb works with Verilator or Icarus.

Physical synthesis (Yosys + OpenROAD) gets a generated 10 ns SDC; logical
synthesis (Yosys area only) needs no SDC.

What lands, wired together and ready to run:

- `rtl/my_ip.sv`: a small parameterized counter standing in for your design
- `tb/tb_my_ip.sv` (or `tb/test_my_ip.py` for cocotb): a self-checking
  testbench with two tests, following the shipped TB style guides
- `my_ip.core`: one Target per enabled flow (`sim`, `lint`, and optionally
  `synth` (plus SDC in physical mode), `fpga` + XDC); with `--stealth` it
  lives at `.booley/cores/my_ip.core`
- `.booley/booley.toml` and `tests.toml`, populated rather than
  skeletons. The scaffolded config turns
  [stealth mode](STEALTH.md#stealth-mode) off, since this repo exists to
  exercise Booley and commit messages should stay verbatim. `--stealth`
  writes `[stealth] enabled = true` instead and puts the design and Doctor
  selftest cores under `.booley/cores/`

The scaffold refuses to run in a repo that already contains `.core` or RTL
files. That's a porting job, so use a plain `booley init` there. Once the
scaffold is written, the `booley-setup` skill takes it from here — see
[greenfield mode](#a-new-ip-greenfield-mode).

When initialization finishes, use the **`booley-setup`** skill.
[Host Bootstrap](INSTALL.md#host-bootstrap) deployed it into your Claude Code
or Codex installation. **Stay on the host**: invoke it in your Claude Code or
Codex chat at the Project Root.

---

## The `booley-setup` skill

Setup proper is driven by the skill. You invoke it **on the host** first; it
tells you when to **Reopen in Container**, and you re-invoke it in the Developer
Agent's chat to finish.

> **Use the most capable model you have** (`/model` in Claude Code). A weaker
> one tends to skip decisions and cost you round-trips.

```text
/booley-setup           # port: phase-detects, plans if no approved plan yet, else executes
/booley-setup new       # a new IP: greenfield mode
/booley-setup <N>       # runs just step N (0-7), to re-run or resume one
```

**Setup asks whether to keep Booley out of your git history.** Yes turns on
[Stealth Mode](STEALTH.md#stealth-mode), which keeps Booley's files out of
your repository and cleans AI attribution out of commit messages. The commit
hook doesn't force `type(scope):` subjects unless you set
[`enforce_convention`](STEALTH.md#enforcing-the-subject-convention-enforce_convention).

### Porting an existing project: plan, then execute

The skill runs eight steps. Steps 0–4 set the project up, and you're done when
Doctor passes in Step 4. Steps 5–7 follow: an optional cross-check, a report,
and cleanup. The skill stops for your approval only once, after Step 0.

- **0 · Plan · host.** The agent reads your repository and checks, for each
  Flow (simulation, lint, ASIC synthesis, FPGA), whether Booley can run it:
  green (works as is), yellow (possible, but something is missing, such as a
  timing constraints file), or red (not supported, such as a VHDL-only design).
  It then settles every choice the later steps need. It asks you only a few
  questions and proposes defaults for the rest, which you can accept in one
  go. It writes the result to `.booley/SETUP-PLAN.md` and stops for your
  approval.
- **1 · Environment · host.** Applies the plan's sandbox-image decision if it
  made one (`booley init` re-run), then hands you into **Reopen in Container**.
- **2 · Project config.** The `.core` Target(s), `tests.toml`, and `booley.toml`.
- **3 · AGENTS.md.** A concise Project-level guidance file for Developer Agents.
- **4 · Doctor.** `booley doctor` is Booley's built-in health check: it audits
  your config, `.core` Targets, toolchain, and sandbox and prints a `PASS`/`FAIL`
  line per check, each `FAIL` carrying a `fix:` hint. `--deep` goes further and
  runs live sim/lint/synthesis smoke tests against the real EDA tools. Fix every
  `FAIL`, then run `--deep` and fix those too. Setup is done when both pass.
- **5 · Parity · optional.** Cross-check Booley against the native build system
  when the approved plan selects one.
- **6 · Findings.** Report and triage findings from the setup run.
- **7 · Cleanup.** Remove the setup run's scratch root (`minimal`), or keep it (`diagnostic`).

Steps 2–4 run in the container and follow the approved plan without asking
again. A test
that needs a non-RTL build step (per-case firmware, vector staging) is
declared as `[flows.sim].pre_run_commands` during project config.
Each step is also a standalone how-to file in the skill's `steps/` directory if
you'd rather write it by hand.

Once `booley doctor` (and `--deep`) is green, you're set up. Head to
[GOAL-MODE.md](GOAL-MODE.md) to enter your first Goal Mode with `/booley-goal`, or start an
[Interactive Mode](USAGE.md#interactive-mode) session.

### A new IP: greenfield mode

You already ran the scaffold on the host: `booley init --scaffold my_ip` (in
Project Initialization above) asked the scaffold choices and wrote a working,
green-by-construction project. So there's no feasibility triage and no config to
author — the skill only has to finish the job.

**Reopen in Container**, then run `/booley-setup new` from the Project Root. It
detects the scaffolded repo, reads the choices the scaffold recorded into the
config, and confirms them in one message rather than re-asking. Because the
scaffold already wrote the config (Step 2) and the project adopts the built-in
Booley Flows, it skips config authoring and runs:

- **The Doctor check (Step 4).** Run `booley doctor`, then `booley doctor
  --deep`, and fix until both are clean with no warnings. Record a deliberate
  project constraint in [`doctor-waivers.toml`](DOCTOR.md#doctor-waivers-doctor-waiverstoml)
  instead of ignoring the warning. Don't stop at a green plain `doctor`: the
  deep smokes prove the wiring.
- **AGENTS.md (Step 3), optional.** The Project-level guidance file from the
  sequence above. A scaffolded project typically wants only this beyond the
  Doctor check.
- **Setup report (Step 6).** Summarizes what each Flow actually delivered
  versus what you asked for.

Then replace the counter in `rtl/my_ip.sv` with your design and grow the
testbench, keeping the generated wiring as the pattern. Refresh
`.booley/selftest/sim/bad-overlay/<top>.sv` under your selected Booley Directory
to match the current interface, keeping it buildable and behaviorally failing
the selected smoke Test. Re-run the Doctor check
whenever the shape of the project changes (new Target, new flow, new
constraints). From there, drive development the normal way: see
[GOAL-MODE.md](GOAL-MODE.md) to enter your first Goal Mode with `/booley-goal`, or start an
[Interactive Mode](USAGE.md#interactive-mode) session.

## See also

- [INSTALL.md](INSTALL.md): host prerequisites, CLI installation, and Host Bootstrap.
- [CONFIG.md](CONFIG.md): configuration field reference and advanced setups
  (custom MCP tools and worktree setup hooks).
- [SANDBOX.md](SANDBOX.md): custom Sandbox image and commercial EDA tools.
- [SUPPORTED-EDA-TOOLS.md](SUPPORTED-EDA-TOOLS.md): which EDA tools Booley drives, at
  which boundary. The reference the skill's plan step checks against.
- [USAGE.md](USAGE.md): day-to-day use and Interactive Mode.
- [CLI-REFERENCE.md](CLI-REFERENCE.md): CLI reference.
- [GOAL-MODE.md](GOAL-MODE.md): Goal Mode.
- [TROUBLESHOOTING.md](TROUBLESHOOTING.md): symptoms and their fixes when something misbehaves.
