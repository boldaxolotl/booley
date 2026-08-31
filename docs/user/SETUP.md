# Setup

This guide connects an installed Booley CLI to a specific RTL project. It
assumes you have completed the [README installation](../../README.md#installation)
and that `booley --version` works.

The lifecycle has three parts: **Host Bootstrap** prepares reusable machine
resources, **Project Initialization** makes this codebase a Booley Project, and
the **`booley-setup` skill** performs design-aware Project Setup. Host Bootstrap
deploys that skill, and this guide hands off to it (see
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
  restatement of the filelists you already have). Six open-source IPs have
  gone through this path, from
  a single-file RV32 CPU to a 415 K-LOC out-of-order core with a vendor
  toolchain; see [Ports](#ports).

Both paths begin with a host-side bootstrap, then use their own skill mode. For
what each configuration field *means* (`booley.toml`, `.core`, `tests.toml`,
sentinels—the log-pattern strings that decide a simulation's pass/fail
verdict—and `enabled`/EDA provisioning), see [CONFIG.md](CONFIG.md).

> This guide is written for a person setting up a project. An agent can follow
> it too: point it at this file. After the bootstrap it hands off to the
> `booley-setup` skill. It assumes you've skimmed the
> [README](../../README.md); [CONTEXT.md](../CONTEXT.md) defines any Booley-specific
> term you hit here (Booley Flow, Specialist, Ticket/Interactive Mode).

## Ports

Booley has been driven end to end on [picorv32](https://github.com/YosysHQ/picorv32), [Ibex](https://github.com/lowRISC/ibex), [biRISC-V](https://github.com/ultraembedded/biriscv), [OpenC910](https://github.com/T-Head-Semi/openc910) (415 K LOC, out-of-order RV64GC), [verilog-pcie](https://github.com/alexforencich/verilog-pcie), and the [taxi](https://github.com/fpganinja/taxi) 10G Ethernet MAC: upstream RTL, upstream testbenches, no source restructuring, essentially zero testbench edits.

Their build systems have nothing in common (FuseSoC, bare Makefiles, a vendor test runner, cocotb under pytest), which is the useful part: the real requirement is just RTL that some EDA tool can already build. If your project clears that bar, the [`booley-setup`](#the-booley-setup-skill) skill can adapt it.

## Host Bootstrap · host

Run this once after installation and again after Booley upgrades:

```bash
booley bootstrap
```

It validates Git, Docker, and VS Code; deploys packaged skills; verifies the
shared Nangate45 cache; reconciles the base Session Image; and converges the
single global egress network, proxy, and reaper. It neither discovers a Project
nor selects an agent provider. `booley bootstrap --check-only` performs no
writes and returns 1 when work is pending; `--force` refreshes Booley-managed
host resources while preserving caches and user-owned files.

Skipping the explicit command is supported: ordinary `booley init` performs
the same reconciliation before it changes a Project.

## Initialize the Project · host

Run Project Initialization on the host before the skill takes over. The host versus
Session Runtime split is described in [ARCHITECTURE.md](../internals/ARCHITECTURE.md#overview).

> **`booley init` is the host command; the workflow CLI is container-only.**
> Bare `booley` (or `booley chat`), `booley run`, `booley board`, and `bwave`
> refuse to run on the host (Reopen in Container); `booley init` refuses inside
> the container, where there is no Docker. Either side fails fast with a message
> naming the fix.

> **For an existing project, start from a fresh clone when practical and run
> `booley init` before making local edits.** A clean tracked tree gives the
> initialization an unambiguous baseline for line-ending repair and other Git
> checks, minimizing avoidable setup conflicts. If you must use an existing
> checkout, commit or stash tracked changes first.

The command depends on your path:

```bash
cd your-rtl-project            # porting an existing project
booley init
```

On a terminal, init asks which provider (`claude` or `codex`) and auth policy
(`subscription`, `api-key`, or `auto`) this project uses. Press Enter to accept
the `claude` and `auto` defaults. CI and other unattended bootstraps apply the
same defaults; pass flags only when the project needs a different selection:

```bash
booley init --provider codex --auth subscription
```

The choice is recorded in `.booley_project/booley.toml` before credentials are
checked or the devcontainer is generated. Re-running init preserves an existing
`[agent]` selection; change that table directly when deliberately migrating a
project. `booley init --seed` follows the same contract, so an older project
without a provider records the default (or supplied flag) on its first reseed.

For CI, release validation, or another environment that must configure a
project without access to a user's secrets, pass `--skip-credentials`. Init
still records and validates the provider/auth selection, but does not inspect
the environment or warn that credentials are absent:

```bash
booley init --provider codex --auth subscription --skip-credentials
```

```bash
mkdir my_ip && cd my_ip && git init      # a new IP, from scratch
booley init --scaffold my_ip
```

`booley init` is idempotent (safe to re-run): it never overwrites a
`.booley_project/docker/{Dockerfile,requirements.txt}` you've hand-edited (it
detects the loss of its `# AUTO-GENERATED` header, or a `# booley:keep`
directive you add, and leaves your files and image untouched). Host Bootstrap
owns external dependency validation, system skill links, the Nangate45 cache,
the base Session Image, and global sidecars. Project Initialization walks through:

1. Creating `.booley_project/` with placeholder configs
2. Recording the selected agent provider and authentication policy
3. The tickets directory tree and selected-provider credential checks
4. Reconciling the Project-selected or Project-derived Session Image while
   verifying its immutable base ancestry
5. Installing Git hooks (repo-level and Project commit-msg)
6. Writing and issuing the Interactive Mode devcontainer specification
7. Post-setup advisories

The commit-msg hook installed in step 5 has one behavior worth knowing about
later; it doesn't affect the happy-path install, so it's spelled out under
[Notes](#notes) at the end.

### What `--scaffold` adds

On top of everything above, `--scaffold` writes a starter project. On a terminal
it asks four choices; every answer is also a flag, for non-interactive runs that
otherwise take the defaults:

| Choice | Options | Flag |
| --- | --- | --- |
| Simulator EDA tool | Verilator / Icarus | `--sim-eda-tool` |
| Testbench style | SystemVerilog / cocotb | `--tb-style` |
| Lint EDA tool | Verilator / Verible | `--lint-eda-tool` |
| Flows to enable | ASIC synthesis, FPGA (and the part) | `--asic` / `--no-asic`, `--fpga-part` |

Two combinations to know about: cocotb requires an image-provisioned simulator,
and the currently supported choices are Verilator and Icarus. Commercial
simulators are future work.

What lands, wired together and ready to run:

- `rtl/my_ip.sv`: a small parameterized counter standing in for your design
- `tb/tb_my_ip.sv` (or `tb/test_my_ip.py` for cocotb): a self-checking
  testbench with two tests, following the shipped TB style guides
- `my_ip.core`: one Target per enabled flow (`sim`, `lint`, and optionally
  `synth` + SDC constraints, `fpga` + XDC)
- `.booley_project/booley.toml` and `tests.toml`, populated rather than
  skeletons. The scaffolded config also turns
  [stealth mode](CONFIG.md#stealth-mode-stealth) off, since this repo exists to
  exercise Booley and commit messages should stay verbatim (set
  `[stealth] enabled = true` to opt in)

The scaffold refuses to run in a repo that already contains `.core` or RTL
files. That's a porting job, so use a plain `booley init` there. Once the
scaffold is written, the `booley-setup` skill takes it from here — see
[greenfield mode](#a-new-ip-greenfield-mode).

When the bootstrap finishes, the **`booley-setup`** skill is deployed into your
agent runtime (item 5 above). **Stay on the host**: invoke it in your agent chat
at the repo root.

---

## The `booley-setup` skill

Setup proper is driven by the skill. You invoke it **on the host** first; it
tells you when to **Reopen in Container**, and you re-invoke it there to finish.

> **Use the most capable model your agent runtime offers.** Setup is one of the
> heaviest reasoning tasks Booley asks of an agent — feasibility triage, a
> decision grill, and a config-authoring + doctor fix-loop that spans the
> host/container boundary. Run the skill on the smartest model your agent
> runtime offers (in Claude Code, select it with `/model` before invoking). A
> weaker model is more likely to skip a decision or mis-author config and cost
> you a round-trip.

```text
/booley-setup           # port: phase-detects, plans if no approved plan yet, else executes
/booley-setup new       # a new IP: greenfield mode
/booley-setup <N>       # runs just step N (0-5), to re-run or resume one
```

### Porting an existing project: plan, then execute

A single **0 → 4** sequence:

- **0 · Plan · host.** The feasibility triage (per-flow green/yellow/red across
  `sim`, `lint`, `synth`, `fpga`) plus a decision grill over
  everything the later steps need. Writes `.booley_project/SETUP-PLAN.md` and
  stops for your approval. **The only gate.**
- **1 · Environment · host.** Applies the plan's sandbox-image decision if it
  made one (`booley init` re-run), then hands you into **Reopen in Container**.
- **2 · Project config.** The `.core` Target(s), `tests.toml`, and `booley.toml`.
- **3 · AGENTS.md.** A concise Project-level guidance file for RTL agents.
- **4 · Doctor.** `booley doctor` is Booley's built-in health check: it audits
  your config, `.core` Targets, toolchain, and sandbox and prints a `PASS`/`FAIL`
  line per check, each `FAIL` carrying a `fix:` hint. `--deep` goes further and
  runs live sim/lint/synthesis smoke tests against the real EDA tools. Fix every
  `FAIL`, then run `--deep` and fix those too. The gate.

Steps 2-4 run in-container, gate-free, consuming the approved plan. A test
that needs a non-RTL build step (per-case firmware, vector staging) is
declared as `[flows.sim].pre_run_commands` during project config.
Each step is also a standalone how-to file in the skill's `steps/` directory if
you'd rather write it by hand.

Once `booley doctor` (and `--deep`) is green, you're set up. Head to
[USAGE.md](USAGE.md) to write and run your first ticket, or start an Interactive
Mode session.

### A new IP: greenfield mode

You already ran the scaffold on the host: `booley init --scaffold my_ip` (in the
bootstrap above) asked the four choices and wrote a working,
green-by-construction project. So there's no feasibility triage and no config to
author — the skill only has to finish the job.

**Reopen in Container**, then run `/booley-setup new` from the repo root. It
detects the scaffolded repo, reads the choices the scaffold recorded into the
config, and confirms them in one message rather than re-asking. Because the
scaffold already wrote the config (Step 2) and the project adopts the built-in
Booley Flows, it skips config authoring and runs only two:

- **The doctor gate (Step 4).** `booley doctor`, resolve every failure and
  warning, then `booley doctor --deep`, fix, and re-run until both exit 0 with
  zero active warnings; the skill does **not** declare the project ready before
  that. A deliberate project constraint can be recorded in a reviewed
  [`doctor-waivers.toml`](CONFIG.md#doctor-waivers-doctor-waiverstoml) entry;
  warnings are never merely ignored. For a scaffolded project the
  sim, lint, and synthesis smoke checks should pass before you've written a
  line. That's what proves the wiring. By hand it's the same two commands in a
  container terminal: read every `FAIL`, `WARN`, and `fix:` hint, and don't stop
  at a green plain `doctor`. The deep smokes are the point.
- **AGENTS.md (Step 3), optional.** The Project-level guidance file from the
  sequence above. A scaffolded project typically wants only this beyond the
  gate.

Then replace the counter in `rtl/my_ip.sv` with your design and grow the
testbench, keeping the generated wiring as the pattern. Re-run the doctor gate
whenever the shape of the project changes (new Target, new flow, new
constraints). From there, drive development the normal way: see
[USAGE.md](USAGE.md) to write and run your first ticket, or start an Interactive
Mode session.

## Notes

**Stealth mode and the commit-msg convention are opt-in.** Setup specifically
asks whether to enable stealth mode and writes `[stealth] enabled = false`
unless you say yes. A missing `enabled` key retains the older on-by-default
runtime fallback for compatibility with existing projects. When enabled,
stealth mode scrubs project-identifying details out of commit messages so
private IP names don't leak into git history. The project commit-msg hook
installed by `booley init` only *sanitizes* your messages that way; it does
**not** force a
`type(scope): summary` subject, so human and upstream-style commits on code you
don't own land as-is. A team that wants that convention across its own history
turns it on with `[stealth] enforce_convention = true` (see
[CONFIG.md](CONFIG.md#enforcing-the-subject-convention-enforce_convention)).
Once enabled, `BOOLEY_SKIP_COMMIT_VALIDATION=1` lands one non-conforming commit
anyway: it skips only the convention check; the IP-leak sanitization still runs
(unlike `git commit --no-verify`, which disables the hook entirely and lets
project details leak into history). Stealth mode itself is covered in
[CONFIG.md](CONFIG.md#stealth-mode-stealth).

## See also

- [README installation](../../README.md#installation): host prerequisites and CLI installation.
- [CONFIG.md](CONFIG.md): configuration field reference and advanced setups
  (custom image/EDA tools and post-setup hook).
- [SUPPORTED-EDA-TOOLS.md](SUPPORTED-EDA-TOOLS.md): which EDA tools Booley drives, at
  which boundary. The reference the skill's plan step checks against.
- [USAGE.md](USAGE.md): CLI reference and the ticket-driven workflow.
- [TROUBLESHOOTING.md](TROUBLESHOOTING.md): symptoms and their fixes when something misbehaves.
