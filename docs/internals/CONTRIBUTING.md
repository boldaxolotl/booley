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

A bare `pytest` skips `tests/goals/` until Goal Mode is removed (#3276); run
those with `pytest tests/goals`. Tests that need the native B-Wave binary skip
until you build it (see [Agent source-development checks](#agent-source-development-checks)).

CI installs with `-c .github/ci/constraints.txt`, a committed file that pins
every dependency version. The plain install above always works; to reproduce
CI's exact versions, add the flag:
`pip install -e ".[dev]" -c .github/ci/constraints.txt`. After changing
dependencies in `pyproject.toml`, regenerate the file, or CI fails and prints
this command:

```bash
uv pip compile pyproject.toml --all-extras --universal --python-version 3.11 -o .github/ci/constraints.txt
```

### Development environment troubleshooting

Debian and Ubuntu ship `venv` as a separate package. If `python3 -m venv .venv`
fails with `ensurepip is not available`, install the package for your
interpreter's minor version, then recreate the environment:

```bash
python3 --version                       # e.g. Python 3.12.3
sudo apt install python3.12-venv        # match the version above
rm -r .venv                             # only the failed local environment
python3 -m venv .venv
```

The editable install puts `booley` in `.venv/bin`; activate the environment
before running contributor commands. If another `booley` runs instead, find it
with `type -a booley` and remove it. Don't link `.venv/bin/booley` into a
global bin directory instead: every project would then run whatever branch
this checkout has checked out.

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
`booley bootstrap --update` from it; see
[The wrong `booley` runs](../user/TROUBLESHOOTING.md#the-wrong-booley-runs).

### Agent source-development checks

Agents don't use the `.venv` workflow above. They run the read-only Agent
Readiness Check, `.github/scripts/agent_readiness.py`, at the points
[`AGENTS.md`](../../AGENTS.md) names. It needs only the standard library, so it
runs before anything is installed. It never installs Booley; it runs every
command from one shared tools-only environment against the active worktree's
source, so parallel worktrees share one set of tools and can't test each
other's code. Its output lists the verification commands, each tagged with the
point at which to run it:

| Command ID | Runs | When |
|---|---|---|
| `ruff.agent-gate` | `ruff check src/ tests/` | Before commit |
| `packaged-copies.check` | `sync_packaged_copies.py --check`: fails on stale packaged docs or `CHANGELOG.md` mirror | Before commit |
| `pytest.broad` | `pytest tests/` (which skips `tests/goals/`) with coverage, up to eight xdist workers | Optional broad verification |
| `ruff.ci-check`, `ruff.ci-format`, `pyright.ci-check` | CI's lint job: `ruff check .`, `ruff format --check .`, `pyright` | Handoff |
| `coverage.changed-lines` | CI's 90% changed-line coverage gate, against the newest merge base with `main` | Handoff |

`coverage.changed-lines` reads the coverage report that a passing
`pytest.broad` run wrote for the same tree, outside the checkout, and refuses
a missing or stale one. `pytest.broad` pins `--basetemp` to a per-checkout
directory on disk on POSIX, because `/tmp` is often a small in-memory file
system that parallel tests can fill. Set `PYTEST_XDIST_AUTO_NUM_WORKERS` to
use fewer workers on a smaller host, or drop the xdist options to run
serially.

`develop` uses the `python` test profile by default, which leaves out every
`native_bwave` test. `--test-profile full` adds them. Native tests always use
this checkout's debug build, `crates/bwave/target/debug/bwave` (`bwave.exe` on
Windows), never a release build or an installed binary, so a run tests exactly
the Rust code in the worktree. Readiness probes that binary with `--version`
but never builds it; when the binary is missing it reports `blocked`, leaves
out the test command, and gives the build command
(`cargo build --locked --manifest-path crates/bwave/Cargo.toml --target-dir crates/bwave/target`;
install the Rust toolchain first if Cargo is missing). Rebuilding or deleting
the binary during a test run invalidates the run. Neither profile covers Rust
tests or `crates/bwave/tests/`.

A `full` run must collect all of `tests/` with no marker, keyword, ignore,
deselect, last-failed, or stepwise filter, and a skipped native test counts as
a failure (except FIFO tests on platforms without POSIX FIFOs). Use plain
`pytest` without `--test-profile` for diagnosing a subset. Readiness output
contains local paths; don't publish it.

### Maintainer QA skills

The repository's QA skills under `qa/` drive agent bug hunts against a
candidate build. Maintainers install them with
`booley bootstrap --with-qa-skills` from a primary source checkout; see
[QA skills](../../qa/README.md#skills).

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
up today and [FLOW_IMPLEMENTATION.md](FLOW_IMPLEMENTATION.md) for how the
built-in Flows run.

**2. Interpretation: Booley needs the result parsing.**

Getting the EDA tool to run is half the port. Booley's value is turning raw
EDA-tool output into a structured verdict plus extracted metrics, so the
Developer Agent doesn't have to read raw logs. That parsing lives in Booley
and is written per EDA tool:

- **Simulators** parse raw output into the structured simulation result. See
  the adapters in [`src/booley/flows/sim/backends/`](../../src/booley/flows/sim/backends/)
  (`cocotb.py`, `icarus.py`, `verilator.py`) and
  [Adapter boundary](FLOW_IMPLEMENTATION_SIM.md#adapter-boundary).
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
  yours to fix.
- **Match the surrounding code**: comment density, naming, and idiom. Design
  and test rules are in [CODING_PRINCIPLES.md](CODING_PRINCIPLES.md).
- **One concern per PR.** An EDA-tool port, a refactor, and a doc fix are
  three PRs. Size caps are in
  [coding principle 18](CODING_PRINCIPLES.md#18-pull-requests-stay-small).
- **Record a user-visible change as a changelog fragment.** Add
  `changelog.d/<issue number>.md` (or `changelog.d/<lowercase-slug>.md` when
  there is no issue) holding one or more `- ` bullets, written as they should
  read under `## Unreleased`. Never edit `CHANGELOG.md` or its packaged mirror
  for an ordinary change; the release assembles the fragments. The format rules
  are in [changelog.d/README.md](../../changelog.d/README.md).
- **Register tests of Sandbox-only code.** Windows CI skips the test modules
  listed in `tests/sandbox_only.py`, because their code never runs on a
  Windows host. See [Sandbox-only tests](../../.github/ci/README.md#sandbox-only-tests)
  for when to add a module and the guard test that checks the list.
- **Keep slow tests well inside their time budget.** CI gives each test 60
  seconds unless a `@pytest.mark.timeout(N)` marker says otherwise. On Windows
  an expired budget kills the whole xdist worker with no traceback, so the
  Windows legs of the `Tests` workflow warn on a passing test that uses more
  than 50% of its budget and fail one above 80%. The Windows legs of the
  weekly full Python matrix fail above 50%, so creeping slowdowns get fixed in one batch instead of
  failing unrelated pull requests. Both messages name the marker value to use.
  To size a budget yourself, take three times the test's slowest Windows CI
  duration and round up to a multiple of 30 seconds. Durations are the `time`
  values in the `junit-windows-*` and `full-junit-windows-latest-*` artifacts;
  each test's `timeout_headroom_ratio` property records the share of its
  budget it used.

### Test assertion safety

An assertion inside `contextlib.suppress` can let a test pass without checking
anything: an earlier exception in the block skips the assertion, and
suppressing `AssertionError` or `Exception` swallows the failure itself. Keep
every contract assertion outside `contextlib.suppress`. CI's lint job enforces
this; run the same check before submitting test changes:

```sh
python3 .github/scripts/check_suppressed_assertions.py
```

It parses every `tests/**/*.py` file without importing it and prints one
`path:line:column: rule (scope)` line per violation. It also fails on a file
it can't read or parse (CI runs it on Python 3.13, so tests must parse there)
and on a bad exemption entry.

| Rule | Flags | Fix |
|---|---|---|
| `suppressed-assertion` | An `assert` inside the `with` block, including nested blocks and class bodies | Move it after the block |
| `suppressed-assertion-call` | A call inside the block named `fail`, `assert<Upper>…` (unittest), or starting with `assert_`, `_assert_`, `check_`, or `_check_`, mock assertions included; `subprocess.check_call`, `subprocess.check_output`, and `check_returncode` don't count | Move it after the block |
| `broad-suppression` | Suppressing `Exception`, `BaseException`, `AssertionError`, or a `failureException` such as `pytest.fail.Exception`, even with no visible assertion, because any helper called in the block may assert | Suppress only the exception you expect |
| `unsupported-suppress-use` | `contextlib.suppress(...)` built anywhere but directly in a `with` item, such as in `ExitStack.enter_context` or saved for later | Write `with contextlib.suppress(...)` |
| `unsupported-suppress-reference` | `suppress` passed through a container, factory, tuple unpacking, walrus, or attribute assignment | Call it directly or through a plain name alias |
| `unsupported-suppress-arguments` | A keyword argument, or an exception argument the checker can't trace to a name | Pass exception classes by name |
| `unsupported-contextlib-star-import` | `from contextlib import *` | Import names explicitly |

The checker follows `import contextlib`, renamed and `from` imports, simple
aliases, several context managers in one `with`, and async code. It tracks
every name a scope might bind, so reassigning an imported alias can produce a
finding even where the code is safe.

To test that code raises, require the exception with a specific match and
assert afterwards:

```python
with pytest.raises(RuntimeError, match="expected failure"):
    operation()
assert observed_result == expected_result
```

For best-effort cleanup, suppress only the cleanup errors you expect and put
required assertions after the cleanup block.

The check is lexical, not a proof. It doesn't see assertions inside helpers
called under a narrow suppression, dynamic imports, reflective lookups, or
`global`/`nonlocal` rebinding. Related hazards it doesn't check at all: a
broad `try`/`except` that swallows failures, and assertions placed after the
raising call inside `pytest.raises`. Review those by hand.

**Exemptions** live in `.github/scripts/suppressed_assertion_exemptions.json`,
which is empty. One is allowed only where the assertion is itself the test
input, in a test of how assertions are handled or skipped; a test of the
checker should feed it source strings instead. Each entry matches exactly one
current finding of rule `suppressed-assertion`, `suppressed-assertion-call`,
or `broad-suppression`:

```json
{
  "path": "tests/example.py",
  "line": 12,
  "column": 9,
  "scope": "TestHandler.test_assertion_input",
  "rule": "suppressed-assertion",
  "purpose": "intentional-assertion-input",
  "reason": "Describe why this assertion is deliberately test input.",
  "verification": "Identify the assertion outside suppression that checks the outcome."
}
```

A broad suppression around an intentional assertion needs two entries, one
per finding. Duplicate, stale, or malformed entries fail the check. The
checker validates only the match and the fields, so the reviewer must confirm
the reason and that the named outside assertion exists.
`tests/ci/test_suppressed_assertions.py` holds the checker's regression tests.

## Writing docs

Follow [DOCS_PRINCIPLES.md](DOCS_PRINCIPLES.md) when you write, edit, or
review a doc.

The Developer Agent in the Sandbox can't read this repository, so a fixed set
of docs ships inside the package and appears at `/booley-docs/` in the
Sandbox: `SHIPPED_DOCS` in `src/booley/runtime/packaged_docs.py`, all from
`docs/user/` plus `docs/internals/FLOW_REPORTS.md`. After editing one of them,
regenerate the packaged copies in `src/booley/data/docs/`:

```bash
python -m booley.dev_support.reference_docs            # add --check to verify only
python3 .github/scripts/sync_packaged_copies.py        # same, without installing Booley
```

Never edit a copy by hand. The sync rewrites links between shipped docs, turns
links to listed unshipped docs (`PLAIN_TEXT_TARGETS`) into plain text, and
fails on a link to any other doc; `booley.runtime.packaged_docs` has the rules.
The same command mirrors `CHANGELOG.md` to `src/booley/data/refs/`, which only
release preparation edits.

Run the sync before committing: the readiness packaged-copies check and
`tests/test_paths.py` fail on a stale copy. A stamped build
(`write_build_stamp` in `src/booley/runtime/build_stamp.py`) renders the
copies too and leaves a stale tracked copy rewritten in the working tree, but
a bare `python -m build` packs the copies as they are. The copies live under
`src/booley/`, so a docs edit that regenerates them also runs the Python CI
jobs.

## Branch and pull request workflow

Booley uses a mainline workflow with no long-lived integration branch. Start
each branch from the latest `main`, keep it to one concern, and open its pull
request into `main`. Once the required checks pass, a maintainer or
repository agent adds the PR to the Mergify merge queue, which tests and
merges queued PRs; you don't need write access. A PR labeled
`confidential-review` waits for a maintainer's review first. Delete your
branch only after Mergify reports the PR merged.

A pull request from a fork needs no setup, such as allowlisting your commit
identity. The `confidential-content` check, which scans for terms that must
not become public, treats findings on commits you authored and on your PR text
as advisory: they don't fail the check, and a maintainer reviews any flagged
match.

CI skips draft pull requests: `Tests`, the PicoRV32 demo contract, and the
site build start when the PR is opened ready or marked ready for review.
`confidential-content` runs on drafts too. Open a draft while the change is
still moving to save shared runner time. To test a draft anyway, run the
`Tests` workflow manually (`workflow_dispatch`) on its branch; a manual run
selects every job, image builds included.

Merging into `main` doesn't publish a release. A release is a versioned commit
on `main` tagged `v*`; the tag triggers the package and container publishing
workflows.

Questions? Open an issue, especially if it starts with "I have a licensed copy
of…".
