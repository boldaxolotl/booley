# Contributing to Booley

This guide is for anyone changing Booley itself, human or agent: how to set up
a development environment, which contributions help most, and how a change
reaches `main`. Read the [README](../../README.md) first for what Booley is.
Neighboring docs own the rest: [ARCHITECTURE.md](ARCHITECTURE.md) for how the
pieces fit, [CODING_PRINCIPLES.md](CODING_PRINCIPLES.md) for Python code,
[DOCS_PRINCIPLES.md](DOCS_PRINCIPLES.md) for docs,
[`.github/ci/README.md`](../../.github/ci/README.md) for CI policy, and
[`AGENTS.md`](../../AGENTS.md) for the repository rules agents follow. The
[glossary map](../../GLOSSARY-MAP.md) points to the glossary for each part of
Booley; keep the relevant one open.

There are three ways to help:

- **Port a commercial EDA tool.** The highest-priority work. It needs access
  to a real licensed installation for validation.
- **Improve the framework.** Architecture review, custom MCP tools,
  Specialists, tests, and focused code fixes.
- **Improve the experience.** Bug reports, documentation corrections, setup
  feedback, and feature requests, none of which need a code change.

Any code change starts with [Getting set up](#getting-set-up): you can't
validate a change without a working environment and a green test suite.

## Getting set up

Booley needs Python 3.11 or later.

```bash
git clone https://github.com/boldaxolotl/Booley.git
cd Booley
python3 -m venv .venv
source .venv/bin/activate
pip install -e ".[dev]"     # editable install plus test and lint tools (pytest, Ruff, Pyright)
pytest                      # tests/ serially; pass a path or -k for a fast subset
```

Tests that need the native B-Wave binary skip
until you build it (see [Agent source-development checks](#agent-source-development-checks)).

CI installs with `-c .github/ci/constraints.txt`, a committed file that pins
every dependency version. The plain install above always works; to reproduce
CI's exact versions, add the flag:
`pip install -e ".[dev]" -c .github/ci/constraints.txt`. After changing
dependencies in `pyproject.toml`, regenerate the file:

```bash
uv pip compile pyproject.toml --all-extras --universal --python-version 3.11 -o .github/ci/constraints.txt
```

### Building a development wheel

Build a development wheel when you need an installed Booley made from your
checkout, for example to test Host Bootstrap or a Sandbox Image built from
your changes. A plain `python -m build` wheel lacks the embedded source
context that Booley needs to build a Sandbox Image, so Booley refuses it. From
the root of a complete checkout, with the editable environment active:

```bash
python3 -m pip install build
wheel=$(python3 -P .github/scripts/build_development_wheel.py)
```

The script stamps the build, removes the stamp files from `src/` even when the
build fails, and prints the path of the wheel it wrote to `dist/`. To check the
wheel without touching your editable install:

```bash
if [ -n "$wheel" ]; then
    wheel_env=$(mktemp -d)
    python3 -m venv "$wheel_env/venv"
    "$wheel_env/venv/bin/python" -m pip install "$wheel"
    (cd "$wheel_env" && "$wheel_env/venv/bin/booley" --version)
    rm -rf -- "$wheel_env"
fi
```

Host commands such as `booley bootstrap` refuse this throwaway environment. One
installed wheel per machine owns the host integrations (the skills under
`~/.agents` and `~/.claude`). Booley records it in
`~/.config/booley/host-installation.json` and refuses host commands from any
other installation, so a stray test install can't rewrite them. Only a wheel
installed in a persistent environment qualifies: editable installs and
packages under the system temporary directory, `.worktrees`, `.runtime`, or
`qa-runs` never do. To make your development wheel the host installation on
purpose, install it in a persistent environment and run
`booley bootstrap --update` from it.

### Agent source-development checks

Agents don't use the `.venv` workflow above. They run the read-only Agent
Readiness Check, `.github/scripts/agent_readiness.py`, at the points
[`AGENTS.md`](../../AGENTS.md) names; its output lists each command and when to
run it. It never installs Booley and runs every command from one shared
tools-only environment against the active worktree's source, so parallel
worktrees can't test each other's code. Rules the output doesn't spell out:

- The default `python` test profile leaves out every `native_bwave` test.
  `--test-profile full` adds them, and a `full` run must collect all of
  `tests/` with no marker, keyword, ignore, deselect, last-failed, or stepwise
  filter. A skipped native test counts as a failure.
- Native tests always use this checkout's debug build,
  `crates/bwave/target/debug/bwave`, never a release build or an installed
  binary. Readiness never builds it; when it is missing, it reports `blocked`
  and gives the build command. Rebuilding or deleting the binary during a run
  invalidates the run.
- Readiness output contains local paths; don't publish it.

## The #1 priority: port commercial EDA tools

Booley's biggest gap isn't code quality or features: it's **EDA tool
coverage**. The open-source stack (Verilator, Icarus, Verible, Yosys,
OpenROAD) runs inside the Sandbox Image today. The one supported commercial
EDA tool is **AMD Vivado 2025.2**, a
[Host-Provisioned Sandbox EDA Tool](../GLOSSARY.md#execution): the host
supplies its installation files read-only, and Vivado still runs inside the
Sandbox. Synopsys, Cadence, and Siemens tools are on the
[roadmap](ROADMAP.md#commercial-eda-tools).

The reason is blunt: **the maintainer has no license for these EDA tools and
cannot develop or validate an integration without one.** If you have access
to a licensed installation of any of them, porting it is the most valuable
thing you can contribute. The last column says whether Edalize 0.6.8, the
version Booley pins, already has a node for its flow API (see below):

| Vendor | EDA tool | Edalize |
|---|---|---|
| Synopsys | **VCS** (sim) | Flow-API node |
| Synopsys | **SpyGlass** (lint), **Design Compiler** (synth) | Legacy API only |
| Synopsys | **Verdi** (waveform/debug), **Fusion Compiler** | None |
| Cadence | **Xcelium** (sim) | Flow-API node |
| Cadence | **Genus** (synth) | Legacy API only |
| Cadence | **HAL** (lint) | None |
| Siemens | **Questa / ModelSim** (sim) | Legacy API only (`modelsim`) |
| Siemens | **Questa Lint / AutoCheck** (lint) | None |

**Lint is the cheapest high-value port.** Booley's `lint` Flow already turns
the findings of two EDA tools (Verilator, Verible) into one structured
verdict, so a third linter reuses the whole result path.

Formal is a separate item. **JasperGold**, Questa Formal, and VC Formal check
properties; they don't lint. They need a new Booley Flow with its own verdict
shape (the "Formal verification" line on the
[roadmap](ROADMAP.md#additional-booley-flows)), not a `lint` EDA tool. The
exception is **Jasper Superlint**, which wraps formal engines behind a
lint-style report and could fit under `lint`. The CDC/RDC apps (SpyGlass CDC,
Questa CDC) are related but also a separate roadmap line.

Booley never distributes proprietary code: an integration drives the EDA
tool's own CLI inside the Sandbox. Project configuration comes from the
repository being worked on, so Booley doesn't trust it with host access: it
cannot supply host paths, commands, mounts, or arbitrary environment values.
A commercial integration therefore needs a reviewed, built-in provisioning and
licensing policy like Vivado's: a host-owned
[EDA Installation Registration](../GLOSSARY.md#configuration) and
[Project Grant](../GLOSSARY.md#configuration), and one fixed read-only mount
point in the container (Vivado's is `/opt/booley-eda/vivado`). Only the host
source path the administrator registers may vary.

### How a port works: two moving parts

A port is almost always these two steps, in order.

**1. Invocation: does FuseSoC/Edalize already drive it?**

Booley resolves and builds designs through FuseSoC and Edalize; Edalize is the
layer that runs the EDA tool. Edalize has two interfaces. Booley uses the
newer **flow API**, where each EDA tool is a node in a named Edalize flow
such as `sim`, `lint`, or `generic`. A **legacy-API** backend (one class per EDA tool)
must be ported to a flow-API node before Booley can drive it. For a linter,
the Verible node Booley ships (`src/booley/data/edalize/verible.py`) is the
worked example; Edalize's legacy
[SpyGlass backend](https://github.com/olofk/edalize/blob/main/edalize/spyglass.py)
gives SpyGlass a head start.

- **Edalize has a flow-API node:** a Target's `flow_options.tool` selects the
  EDA tool that builds it ([TARGETS.md](../user/TARGETS.md)); wire the new
  value through the built-in Booley Flow. The `fpga` Flow is the exception: it
  always builds with Vivado.
- **Edalize has no node:** contribute one upstream to Edalize first.
  Booley has no Project-defined escape hatch for host commands.

Either way, a commercial EDA tool also needs the provisioning, licensing,
security, Doctor, and end-to-end validation policy described above; an
Edalize node is necessary but not enough. See
[SUPPORTED-EDA-TOOLS.md](../user/SUPPORTED-EDA-TOOLS.md) for how Vivado is set
up today; the built-in Flows live in `src/booley/flows/`.

**2. Interpretation: Booley needs the result parsing.**

Getting the EDA tool to run is half the port. Booley's value is turning raw
EDA-tool output into a structured verdict plus extracted metrics, so the
Developer Agent doesn't have to read raw logs. That parsing lives in Booley
and is written per EDA tool:

- **Simulators** parse raw output into the structured simulation result. See
  the adapters in [`src/booley/flows/sim/backends/`](../../src/booley/flows/sim/backends/)
  (`cocotb.py`, `icarus.py`, `verilator.py`).
  A new adapter can start as a sibling module, but users can't select it until
  the full runtime policy and real licensed end-to-end evidence land with it.
- **Synthesis, timing, and implementation** parse vendor report files into
  QoR (quality of results: area, timing, power) metrics. The `fpga` Flow's
  Vivado reports (`src/booley/flows/fpga/`) and the `synth` Flow's
  Yosys/OpenROAD timing (`src/booley/flows/synth/`) are the examples.
- **Waveform tools** (such as Verdi with FSDB) would feed B-Wave, Booley's
  waveform debug tool: the harder, more valuable end.

Build the parsers against **real logs from a real licensed run**, then commit
a sanitized fixture (see the IP rule below). A parser checked only against
made-up output is provisional at best.

### Before you start a port

- **Open an issue first** describing the EDA tool, your license and host
  situation, and whether Edalize already covers invocation. The maintainer
  can't reproduce your environment, so a port is a collaboration: the more of
  the real-tool validation you carry, the faster it lands.
- **IP rule: don't commit proprietary output.** Vendor logs, reports, and
  VCD/FSDB headers can leak design details and even EDA-tool internals.
  Commit a *synthetic* fixture in the EDA tool's output format and keep real
  dumps out of the repository entirely.

## Other welcome contributions

- **Architecture review.** Booley is "hardware engineer writing software", and
  the parts most likely to be wrong are the load-bearing ones: the Booley Flow
  boundary, the split between Sandbox and host authority, the Job model, and
  where state lives. If you've built and maintained software at this size,
  read [ARCHITECTURE.md](ARCHITECTURE.md) and tell the maintainer what's going
  to hurt in a year. An issue arguing that a decision is wrong, with the
  reasoning, is worth more than a PR that tidies the code implementing it.
- **Custom MCP tools and Custom Flows**: see [MCP-TOOLS.md](MCP-TOOLS.md).
- **Bug reports and reproductions**, especially from setting up new projects.
  You don't need this repository, a GitHub account, or a PR. Tell the
  `/booley-feedback` skill in your own project while the evidence is still on
  screen. It captures the failure, writes a local report, and shows you the
  exact redacted text before anything leaves your machine
  ([USAGE.md](../user/USAGE.md#when-booley-itself-misbehaves)).
- **Telling us what you think.** Tell the same `/booley-feedback` skill what
  you'd want built, what you liked, what you gave up on, or whether Booley
  earned its setup cost on a real project. Bug reports say what is broken,
  never whether the thing is worth using, and that second question is what
  the roadmap turns on. One sentence is a complete report.
- **Docs**: corrections, gaps, and clearer onboarding. A doc that lies is a
  bug. Give `/booley-feedback` the contradiction and the file; it needs no
  reproduction.

## Development basics

- **Run the test suite before opening a PR**; a change that breaks tests is
  yours to fix. Design and test rules are in
  [CODING_PRINCIPLES.md](CODING_PRINCIPLES.md).
- **One concern per PR.** An EDA-tool port, a refactor, and a doc fix are
  three PRs. Size caps are in
  [coding principle 18](CODING_PRINCIPLES.md#18-pull-requests-stay-small).
- **Record a user-visible change as a changelog fragment** in `changelog.d/`;
  never edit `CHANGELOG.md`. The rules are in
  [changelog.d/README.md](../../changelog.d/README.md).
- **Pyright exclusions and test timeouts** are documented where they are
  configured: `[tool.pyright] exclude` in `pyproject.toml`, and the timeout
  budget in `tests/timeout_headroom.py` and `tests/conftest.py`.
- **Register tests of Sandbox-only code** in `tests/sandbox_only.py`; see
  [Sandbox-only tests](../../.github/ci/README.md#sandbox-only-tests).
- **Keep assertions out of `contextlib.suppress`.** CI runs
  `.github/scripts/check_suppressed_assertions.py`; its docstring and messages
  explain each rule.

## Writing docs

Follow [DOCS_PRINCIPLES.md](DOCS_PRINCIPLES.md) when you write, edit, or
review a doc. A PR that changes docs gets the label `docs-premerge` or
`docs-postreview`; [doc-change-classes.md](agents/doc-change-classes.md) says
what each means.

Docs the Developer Agent reads in the Sandbox ship inside the package: edit
only the sources listed in `SHIPPED_DOCS` in `src/booley/runtime/packaged_docs.py`,
never a rendered copy under `src/booley/data/docs/`. Its module docstring
explains the render and the link rules.

## From branch to `main`

Branch, pull request, and merge-queue rules are in
[`AGENTS.md`](../../AGENTS.md) and [merge-queue.md](agents/merge-queue.md). A
pull request from a fork needs no setup; the `confidential-content` check
treats findings on commits you authored and on your PR text as advisory (see
[confidential-content.md](agents/confidential-content.md)). Merging into `main`
doesn't publish a release: a release is a versioned commit on `main` tagged
`v*`, and the tag triggers the publishing workflows.

Questions? Open an issue, especially if it starts with "I have a licensed copy
of…".
