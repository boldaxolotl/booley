# Contributing to Booley

## Overview

Thanks for wanting to help. Booley is early and the surface is wide, so almost
any contribution is useful, but some help is worth far more than others.

This guide assumes you've read the [README](../README.md). The domain terms it
leans on—Booley Flow, Target, Session Runtime, and the rest—are defined in
[CONTEXT.md](CONTEXT.md), Booley's controlled vocabulary; keep it open if a term
is unfamiliar. For how the pieces fit together, read [ARCHITECTURE.md](ARCHITECTURE.md).

There are three main contribution paths:

- **Port a commercial EDA tool.** This is the highest-priority work and requires access to a real licensed installation for validation.
- **Improve the framework.** Architecture review, custom MCP tools, Specialists, tests, and focused code fixes are all welcome.
- **Improve the experience.** Reproducible bug reports, documentation corrections, setup feedback, and feature requests are useful without a code change.

Code contributors should complete [Getting set up](#getting-set-up) before starting. The sections that follow explain the commercial-EDA-tool port first, then the other contribution paths and the development rules shared by all code changes.

## Getting set up

Do this once before attempting any contribution below, including the #1 port
path: you can't validate a change without a working dev env and a green suite.

```bash
git clone https://github.com/boldaxolotl/Booley.git
cd Booley
python3 -m venv .venv
source .venv/bin/activate
pip install -e ".[dev]"     # editable install + test deps (pytest, pytest-asyncio, …)
pytest                      # full suite; scope to a path/-k for a fast subset
```

Booley needs **Python 3.11+**. For venv, PATH, and Windows problems, see
[TROUBLESHOOTING.md](TROUBLESHOOTING.md).

## The #1 priority: port commercial EDA tools

Booley's biggest gap isn't code quality or features: it's **EDA tool coverage**.
The open-source stack (Verilator, Icarus, Yosys, sv2v) runs first-class today.
**AMD Vivado 2025.2** is the sole supported commercial policy: an administrator
registers and grants a host installation, which is mounted read-only while the
Vivado process still runs inside the Session Runtime. Everything else from the
"Big-3" is still on the [roadmap](ROADMAP.md#commercial-eda-tools): Synopsys,
Siemens (Mentor), and the rest of the Cadence line.

The reason is blunt: **the maintainer has no license for most of these EDA tools and
cannot develop or validate a backend without one.** If you have access to a
licensed installation of any of them, porting it is the single most valuable thing you can contribute:

| Vendor | EDA tools worth porting |
|---|---|
| Synopsys | **VCS** (an internal/incubating parser exists, but there is no public integration), **SpyGlass** (lint), **Verdi** (waveform/debug), **Design Compiler**, **Fusion Compiler** |
| Cadence | **Genus** (synth), **HAL** (lint), **Xcelium** (an internal/incubating parser exists, but there is no public integration) |
| Siemens (Mentor) | **Questa / ModelSim** (sim), **Questa Lint / AutoCheck** (lint) |

**Lint is the cheapest high-value port on that table.** Booley's `lint` Flow
already normalizes findings from two engines (Verilator, Verible) into one
structured verdict, so a third linter reuses the whole result surface.

Edalize has two backend interfaces. New work targets the newer **flow API**,
where each EDA tool is a *node* in a named *flow* (`lint`, `sim`, `synth`); the
older **legacy API** is the single-class backend style and has to be ported to
a flow node before Booley can drive it. Two linters have a head start: Edalize
ships a legacy-API
[SpyGlass backend](https://github.com/olofk/edalize/blob/main/edalize/spyglass.py)
(methodology + goals + rule parameters). Cadence **HAL** is commonly delivered
with `xrun`, but Booley has no supported Cadence installation or licensing
policy. Both still need a flow-API EDA-tool node for the Edalize `lint` flow;
the Verible port (`src/booley/data/edalize/verible.py`) is the worked example.

Formal is a different item. **JasperGold** (and Siemens' Questa Formal, Synopsys
VC Formal) is property checking, not lint. That's the "Formal verification"
line on the [roadmap](ROADMAP.md#additional-booley-flows), and it needs a
new Booley Flow with its own verdict shape, not a `lint` engine. The exception is
**Jasper Superlint**, which wraps formal engines behind a lint-style report and
could plausibly land under `lint`. Same for the CDC/RDC apps (SpyGlass CDC,
Questa CDC): related EDA tools, separate roadmap line.

Booley never distributes any proprietary code. An integration drives the EDA
tool's own CLI inside the Session Runtime. Commercial integrations additionally
need a reviewed, fixed provisioning and licensing policy; Project configuration
cannot supply host paths, commands, mounts, or arbitrary environment values.

### How a port works: two moving parts
Porting an EDA tool is **almost always these two steps**, in order:

**1. Invocation: does FuseSoC/Edalize already drive it?**

Booley resolves and invokes designs through the FuseSoC → Edalize stack.
Edalize is the layer that actually shells out to the EDA tool. So the **first
question for any EDA tool is: does Edalize already have a backend for it?**

- **Yes** (VCS, Genus, DC, Questa, … many are already in Edalize) → you get
  much of the invocation layer. Point the EDA-tool selector at it: a `.core` Target's
  `default_tool` field (or a per-flow `flow_options.tool`) chooses which EDA tool
  builds that Target. Then wire it through the built-in Booley Flow path. There
  is one builder (Booley's FuseSoC/Edalize flow) and no `backend` or execution-
  location knob: every Flow executes inside the Session Runtime. For commercial
  EDA, an Edalize backend is necessary but not sufficient—the contribution also
  needs a built-in installation, licensing, security, Doctor, and end-to-end
  validation policy comparable to Vivado's. See
  [SUPPORTED-EDA-TOOLS.md](SUPPORTED-EDA-TOOLS.md).
- **No** → contribute the backend upstream to Edalize, then integrate that
  flow node with the same Session Runtime and commercial-policy requirements.
  Booley has no Project-defined host-command escape hatch.

**2. Interpretation: Booley needs the result-parsing logic.**

Getting the EDA tool to *run* is only half the port. Booley's whole value is turning
raw EDA-tool output into a **normalized structured verdict** (`pass` / `fail` /
`inconclusive` / `timeout`) plus extracted metrics. The agent never reads raw
logs. That parsing lives in Booley and has to be written per EDA tool:

- **Simulators** parse raw output into the structured sim result (shared
  helpers in `sim_result.py`). See the existing parsers in
  [src/booley/sim/](../src/booley/sim/): `xcelium_run.py`, `vcs_run.py`,
  `verilator_run.py`, `iverilog_run.py`. Xcelium and VCS parsing code is
  internal incubation material only: those EDA tools are not selectable or
  supported Booley simulators. A new simulator parser can start as a sibling
  module, but it does not become public eligibility until the complete runtime
  policy and real licensed end-to-end evidence land with it.
- **Synthesis / timing / impl** parse vendor report files into QoR
  (quality-of-results: area, timing, power) metrics.
  See how `fpga` reads Vivado's routed reports and how Yosys+OpenROAD timing is
  interpreted in the built-in Flow implementations.
- **Waveform EDA tools** (e.g. Verdi/FSDB) feed the `bwave` debug surface, the
  harder, more valuable end of the spectrum.

Freeze the parsers against **real logs from a real licensed run** and commit a
sanitized fixture (see the "IP rule" below). A parser validated only against
synthetic output is provisional at best.

### Before you start a port

- **Open an issue first** describing the EDA tool, your license/host situation, and
  whether Edalize already covers invocation. The maintainer can't reproduce your
  environment, so a port is a collaboration: the more of the real-EDA-tool
  validation you can carry, the faster it lands.
- **Check the Edalize backend list** before assuming you have to write
  invocation from scratch.
- **IP rule: do not commit proprietary output.** Vendor logs, reports, and VCD/FSDB
  headers can leak design and even EDA-tool-internal details. Contribute a
  *synthetic* fixture in the EDA tool's dialect, and keep real dumps out of the repo
  entirely

## Other welcome contributions

Porting EDA tools is #1, but not the only way to help:

- **Architecture review.** Booley is "hardware engineer writing software", and
  the parts most likely to be wrong are the load-bearing ones: the Booley Flow
  boundary, the Session Runtime/host-authority split, the job model, where
  state lives. If you've
  built and maintained software at this size, read
  [ARCHITECTURE.md](ARCHITECTURE.md) and tell the
  maintainer what's going to hurt in a year. An issue arguing that a decision is
  wrong, with the reasoning, is worth more than a PR that tidies the code
  implementing it. Style and idiom nits are the cheap part; see
  [CODING_PRINCIPLES.md](CODING_PRINCIPLES.md) if you want them anyway.
- **MCP tool architecture and custom extensions** ([MCP-TOOLS.md](MCP-TOOLS.md)).
- **Bug reports and reproductions**, especially around setup on new projects.
  You do not need this repo, a GitHub account, or a PR to send one. Tell the
  `/booley-feedback` skill from your own project while the evidence is still on
  screen. It captures the failure, writes your local report, and shows the exact
  redacted text before anything goes anywhere
  ([USAGE.md](USAGE.md#when-booley-itself-misbehaves)).
- **Telling us what you think.** Tell the same `/booley-feedback` skill what
  you'd want built, what you liked, what you gave up on, or whether Booley
  earned its setup cost on a real project. Bug reports say what is broken and
  never whether the thing is worth using; that second question is the one the
  roadmap actually turns on. No reproduction is required; one sentence is a
  complete report.
- **Docs**: corrections, gaps, and clearer onboarding. A doc that lies is a
  first-class bug. Give `/booley-feedback` the contradiction and the file; it
  needs no reproduction.

## Development basics

- **Run the test suite before opening a PR** (setup is under
  [Getting set up](#getting-set-up) above); a change that breaks tests is yours
  to fix.
- **Match the surrounding code**: comment density, naming, and idiom.
- **One concern per PR.** An EDA-tool port, a refactor, and a doc fix are three PRs.

Questions? Open an issue. Especially if it starts with "I have a licensed
copy of…".
