# Roadmap

Directions Booley hasn't taken yet, for contributors choosing work and for
users asking what is coming. Items are ordered by impact, highest first.
Commercial EDA backends are the best place for an outside contribution. What
ships today is in [FEATURES.md](../user/FEATURES.md) and
[SUPPORTED-EDA-TOOLS.md](../user/SUPPORTED-EDA-TOOLS.md); work already in
progress is tracked in GitHub issues, linked from each item. Each item opens
with a status:

- **Planned**: not started.
- **In progress**: issues are open and work has started.
- **Partial**: works in some supported configurations but not all.

## Verification Engineering

**Planned.** Booley should serve verification engineers as well as RTL design
engineers: build on a Project's existing SystemVerilog or cocotb testbench and
carry work from test execution to reproducible, machine-checkable evidence,
instead of treating the testbench only as support for an RTL change. Booley
already resolves Targets and runs Booley Flows in the Sandbox, so the same
machinery can run a full regression and report it the same way locally and in
CI, without a second set of project-specific scripts.

Today one `booley flow sim` run takes several Targets and multi-seed Test Runs
(`stress@x20`), and each Target's run is a resumable
[Simulation Record](../GLOSSARY.md) with bounded parallel Test Runs. Still
missing:

- **Regression management**: named regressions, scheduled (for example
  nightly) runs, and browsing and comparing results across runs.
  [Measurement Runs](#continuous-integration) cover the "does this commit
  pass" part.
- **Failure triage**: cluster related failures, tell design, testbench, and
  infrastructure problems apart, and rerun a representative failure with a
  waveform for focused diagnosis.

## Commercial EDA Tools

**Partial.** Host-provisioned Vivado 2025.2 on Linux x86-64 hosts is the one
supported commercial EDA tool. The FlexNet license relay is experimental: no
real paid-seat checkout, accounting, and return test was available. Planned:
Cadence, Synopsys, and Siemens tools such as Xcelium
([#1456](https://github.com/boldaxolotl/booley/issues/1456)), VCS, Questa,
Design Compiler, Genus, and HAL; newer Vivado releases
([#1065](https://github.com/boldaxolotl/booley/issues/1065)); seat caps for
licensed tools ([#3244](https://github.com/boldaxolotl/booley/issues/3244));
and cocotb on each new simulator.

Each new tool needs what Vivado has: a built-in installation contract,
read-only provisioning into the Sandbox, a license relay, Doctor checks,
security tests, and proof that the whole Flow works. A wrapper script alone is
not support.

The blocker is licenses, not design: the maintainer can't validate a Flow for
an EDA tool they can't run. [CONTRIBUTING.md](CONTRIBUTING.md#the-1-priority-port-commercial-eda-tools)
lists the tools worth porting per vendor, which of them Edalize already
drives, and what a port takes.

## VHDL and GHDL Support

**Planned.** Add VHDL as a first-class source language without a second
design-description path. FuseSoC and Edalize already carry VHDL source types,
ordered files, logical libraries, standards, and generics; Booley must keep
that metadata through Target resolution, Flow planning, fingerprints, Doctor,
source isolation, and evidence, instead of rebuilding Verilog-only inputs from
file suffixes.

The first slice is pure-VHDL simulation with a pinned
[GHDL](https://ghdl.github.io/ghdl/) in the Sandbox, using cocotb for per-test
verdicts and GHDL's own FST output for [Trace Artifacts](../GLOSSARY.md).
Acceptance fixtures define the supported VHDL-2008 subset, because GHDL calls
its VHDL-2008 support partial. Native VHDL testbenches need their own mapping
from assertions and test cases to Booley's per-test evidence.

Later slices add GHDL analysis as a VHDL lint, pass VHDL libraries and generics
through the Vivado `fpga` Flow, and try GHDL's Yosys frontend
(`ghdl-yosys-plugin`) ahead of the existing `synth` stages. Both GHDL and the
plugin call that integration experimental, so the synthesis slice must publish
a tested language subset. Coverage stays Verilog/SystemVerilog-only until a VHDL
coverage design exists.

Mixed VHDL/SystemVerilog simulation needs a mixed-language simulator, such as
Xcelium or VCS under the commercial requirements above, or a separately
qualified Vivado XSim; GHDL alone does not provide it.

## Coverage

**Partial.** Verilator line, branch, expression, toggle, and `cover property`
coverage ships with thresholds, waivers, and the Coverage Analyst (see
[COVERAGE.md](../user/COVERAGE.md) and the
[Simulation Coverage glossary](../../src/booley/flows/sim/GLOSSARY.md)).
Still missing:

- Thresholds for FSM and covergroup coverage. Booley records both but scores
  neither.
- Combined coverage across Targets, such as unit and top level
  ([#2748](https://github.com/boldaxolotl/booley/issues/2748)).
- Resuming an interrupted coverage run per Test Run instead of refusing it
  ([#2712](https://github.com/boldaxolotl/booley/issues/2712)).
- Waivers as plain per-file waiver files reviewed in the diff
  ([#3605](https://github.com/boldaxolotl/booley/issues/3605)).
- Coverage-driven stimulus: use coverage holes to guide new tests.

## Mutation Flow

**In progress.** The [Mutation Tester](../user/FEATURES.md#mutation-testing)
already generates Mutants deterministically, without an LLM. It still runs as
a Specialist with a `[models]` key it does not use;
[#2936](https://github.com/boldaxolotl/booley/issues/2936) makes it the
`booley flow mutation` Booley Flow that
[ADR 0088](../adr/0088-generate-mutants-deterministically-in-a-mutation-flow.md)
describes.

## Firmware Reviewer

**Planned.** The RTL and testbench Reviewers read firmware as context but
report no findings in it
([ADR 0091](../adr/0091-review-commits-outside-measurement.md)). A firmware
Reviewer would review firmware in its own right, for example driver code
against the register map and the spec, and add a `fw` role that a
[Declared Review](../GLOSSARY.md) can name.

## Continuous Integration

**In progress.** `booley measure`
([#3045](https://github.com/boldaxolotl/booley/issues/3045),
[ADR 0092](../adr/0092-measure-commits-against-target-metrics.md)) measures a
commit in a clean checkout against the [Target Metrics](../GLOSSARY.md) it
declares. A local result is advisory, because the Developer Agent can write to
the store from the Sandbox; a CI run, with no live working tree, is the
trusted one. Hosted CI gets its own design. The direction so far:

- Run `booley measure` on the pull request head as a required check, and
  measure `main` after each merge so the comparison run for the next pull
  request already exists.
- Reuse a run when a merge commit's tree equals a tree already measured.
- The Sandbox Image carries the EDA stack, so a hosted workflow needs only a
  few lines. A Stealth Mode Project keeps Booley out of its RTL repository, so
  its CI needs a home outside it.
- Optionally, after a failure, start an investigator agent with the saved
  evidence. It reports a probable cause, its confidence, and possible fixes. It
  never changes the verdict, edits code, or pushes.

## Enterprise Model Endpoints

**Partial.** A company running Booley on its own servers rarely sends model
traffic straight to OpenAI or Anthropic. Traffic goes through a gateway
(LiteLLM, Portkey, Kong, an in-house proxy) for billing, logging, and policy;
to a private cloud deployment (AWS Bedrock, Google Vertex AI, Azure OpenAI); or
to open-weight models served with vLLM. Booley has one such path today, the
[model gateway](GATEWAY.md): Codex only, one `https://` endpoint, a bearer
token. It was built for the Codespaces demo: Claude never uses it, and one
gateway's quirks (no priority tier, its own model catalog) are hardcoded.

The general version treats a model endpoint as a named definition owned by the
operator, separate from the Project:

- **Endpoint.** URL and protocol: Anthropic Messages, OpenAI Responses, OpenAI
  chat completions, Bedrock, Vertex AI, or Azure OpenAI. Gateway quirks become
  per-endpoint settings.
- **Credential.** A static token, a command that prints a short-lived token,
  custom headers, client certificates, or the cloud's own identity (AWS or GCP
  credentials). A definition names where a secret comes from (an environment
  variable or a command), never the secret itself.
- **Model names.** Gateways rename models, and Bedrock has its own model IDs.
  A per-endpoint alias map keeps `[models]` and the
  [model-to-CLI rule](../adr/0084-run-each-specialist-on-the-agent-cli-its-model-implies.md)
  working.
- **Binding.** A Project binds each Agent CLI it lists to an endpoint or to the
  vendor's own service. Each agent process gets only its own endpoint's
  secrets, the rule that already keeps the Codex gateway token away from
  Claude.

Endpoint definitions live in the host config next to the egress policy, not in
`booley.toml`, because the URL depends on the deployment and
[ADR 0080](../adr/0080-route-codex-specialists-through-an-env-selected-gateway.md)
already keeps secret locations out of committed files. Both Agent CLIs support
custom endpoints (Claude Code through `ANTHROPIC_BASE_URL`, `apiKeyHelper`, and
its Bedrock and Vertex AI modes; Codex through `model_providers`), so Booley
translates one definition into each CLI's configuration instead of proxying
model traffic itself. One module owns that translation, the hand-off into the
Sandbox, and the Doctor checks. Today's gateway becomes a built-in endpoint, so
existing setups keep working.

A separate slice covers the network: corporate networks often require an
outbound HTTPS proxy and a TLS-inspecting CA. Booley's egress proxy cannot
forward to an upstream proxy, and a Sandbox cannot be given an extra CA bundle,
which blocks Booley on such networks with or without a model gateway.

Open questions: whether a Project may define endpoints too; whether a model
alias or the endpoint's protocol picks the Agent CLI; and whether cloud
identities (Bedrock, Vertex AI) belong in the first slice.

## Full IP Design from Spec

**Planned.** Give Booley an IP-level specification, such as an SPI controller
or a complete AES core, and it breaks the work into dependency-ordered tasks,
runs each in a Developer Agent session, and measures each result, without
supervision.

## HDL Dependency Graph (intra-Target file pruning)

**Planned.** Hand a Booley Flow only the files its top module needs. A FuseSoC
[Target](../GLOSSARY.md) resolves to an ordered fileset that covers that
Target's top, but filesets are shared across Targets and `depends` pulls in
whole dependency cores, so a fileset is often much larger than what one top
instantiates. The extra files cost resolution time and surface syntax errors
from unrelated sources.

Booley will use slang as the front end and not write its own
Verilog/SystemVerilog parser; pyslang already ships in the Sandbox Image for
the Mutant generator. Booley passes slang the Target's ordered files, defines,
include directories, parameters, and top, takes `include` and `import` edges
from the sources and instantiation edges from the elaborated hierarchy, and
keeps only the transitive closure of the top. The result is cached until a
source or compilation input changes.

Narrow per-Flow Targets, which the FuseSoC model already expects, still help:
they shrink the work before slang runs. The graph covers the cases where
shared filesets and `depends` are still too coarse, without asking Project
authors to maintain more Targets.

## Additional Booley Flows

**Planned.** The built-in Flows cover simulation, lint, ASIC synthesis, and
FPGA implementation. `synth` is a fast area and timing estimate to guide RTL
work, not tape-out sign-off (see
[SUPPORTED-EDA-TOOLS.md](../user/SUPPORTED-EDA-TOOLS.md#built-in-flows)).
Production RTL work needs more:

- Logic equivalence checking (LEC)
- Formal verification
- Clock domain crossing (CDC) and reset domain crossing (RDC) checks
- Design for test (DFT) insertion
- Static timing analysis (STA)
- Power analysis and UPF
- FPGA implementation beyond AMD Vivado: an open-source Yosys and nextpnr
  backend ([#3603](https://github.com/boldaxolotl/booley/issues/3603)) and
  Intel tools
- `synth` on PDKs other than Nangate45
  ([#3599](https://github.com/boldaxolotl/booley/issues/3599),
  [#3600](https://github.com/boldaxolotl/booley/issues/3600))

## Best-of-N Sessions

**Planned.** Hardware results are measured, not judged: Fmax, area, Cycle
Count, coverage, and pass/fail come from fresh Booley Flow results, so
comparing several attempts at one task is cheap. One request starts N Developer
Agent sessions, each in its own linked worktree. Sessions may differ in model,
provider, effort, or a short strategy hint ("pipeline the multiplier",
"retime the adder tree").

Booley measures each candidate's final commit with `booley measure` and ranks
the candidates: first by enforced Target Metrics met, then by one metric the
user picks when starting (for example area or Fmax), then by diff size. The
user reviews the ranking and the winner and merges by hand; Booley never
merges a candidate.

Constraints: cost grows N-fold, so N is explicit and capped. Concurrent
sessions share `[jobs]` limits, commercial license seats, and model-provider
rate limits, so the scheduler must queue rather than fail. An optional early
stop cancels the remaining sessions once one candidate meets every enforced
metric. Losing candidates stay until the user removes them with `booley
worktree rm` or `booley worktree gc`.

## Notifications

**Planned.** Agent sessions and slow Booley Flows run for hours, and today the
only signal is the Dashboard's NEEDS YOU group, so the user has to keep
watching it. Booley should push a short notice to the desktop, a phone, or
email when a session waits for the user, stops, or crashes, or when a long
Flow Job finishes or fails.

The hard part is security. A notification service is a path out of the
Sandbox, and the Sandbox must not gain one, so push services such as ntfy.sh
stay off the egress allowlist and notices come from the host, never from the
agent:

- A host-side process watches Booley's own records (Job records, session
  state) and sends the notice. The agent has no tool that sends one.
- Message text comes from fixed templates filled with Booley-owned fields:
  event kind, status, durations, and a Project label the user chose.
  Agent-written text, file contents, and agent-chosen paths never appear.
- Notices are rate-limited and coalesced, so an agent cannot encode data in
  the timing or number of events it triggers.
- A content-free mode ("Booley needs you") suits Projects whose names are
  confidential.

## Native ADE Surface

**Planned.** Booley is an agentic development environment (ADE), but its
agent-facing UI is thin: Developer Agent sessions run in VS Code terminals,
and the Booley Dashboard, a terminal view, is the one native view of
sessions, Jobs, and Targets. The next step is UI for supervising agents
rather than editing code: a session panel, a Flow-run view, one-click
waveform open, and views for Measurement Reports and reviews.

The end state is Booley as a **standalone ADE**: an app built around running
and reviewing Developer Agent sessions, with the editor as one pane among
many. Whether it embeds VS Code, forks it, or uses a lighter editor is open;
the VS Code-based surface above is the path there.
