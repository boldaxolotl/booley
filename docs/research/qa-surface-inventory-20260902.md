# Shipped Booley QA surface inventory — 2026-09-02

This note resolves
[`Inventory the shipped Booley QA surface`](https://github.com/boldaxolotl/booley/issues/247)
against [`main` at `ad2d4f52`](https://github.com/boldaxolotl/booley/tree/ad2d4f5287e360f5552dede715f8f9e796583195).
It inventories
user-observable contracts that a public, documentation-driven QA scenario suite
can exercise. It does not prescribe the scenarios or treat implementation
details as coverage merely because they exist in the tree.

The inventory is deliberately wider than the four built-in Booley Flows. A
real run crosses host installation and administration, Project Initialization
and Setup, a Session Runtime, an Interactive Mode client or Ticket Mode
Developer Agent, Targets and tests, Flows and Specialists, durable evidence,
review, waveform analysis, and feedback. A green EDA invocation alone does not
qualify that path.

## Classification rule

| Classification | Meaning for the initial suite |
| --- | --- |
| **Supported** | Publicly documented and shipped. It belongs in the coverage map, although pairwise rather than exhaustive combinations are sufficient. |
| **Supported, mode-scoped** | Shipped and supported, but intentionally available only in a named mode, host platform, or provisioned lane. A non-applicable run must report `not runnable`, not pass or fail. |
| **Experimental** | Shipped but explicitly lacking validation required for a support claim. Keep visible as a separately qualified risk; do not make an ordinary green lane depend on it. |
| **Hidden** | Code exists but is deliberately absent from normal discovery or exposed only behind a diagnostic switch. It is not mandatory initial coverage. |
| **Partial** | Shipped only in the explicitly named supported configurations. Cover the supported subset; do not generalize it to the missing configurations. |
| **Prototype** | Code exists but is not reliable enough for unattended use. It is not a current regression contract. |
| **Planned** | Publicly described future behavior that has not shipped. It is not a current regression contract. |
| **Unsupported** | Explicit boundary. A scenario should test clear rejection only when that failure path is valuable; it must not attempt to turn it into a supported route. |

The canonical user-facing sources are the [feature catalog](../user/FEATURES.md),
[Setup guide](../user/SETUP.md), [Usage guide](../user/USAGE.md),
[configuration reference](../user/CONFIG.md), [Flow reference](../user/FLOW_REFERENCE.md),
[supported EDA matrix](../user/SUPPORTED-EDA-TOOLS.md), and the generated
[cheat sheet](../../src/booley/data/cheatsheet.md). The
[roadmap](../internals/ROADMAP.md) is the authority for non-shipped statuses.

## Coverage contract

Every mapped item should name a stimulus and an external oracle. “The agent
mentioned it” and “the process returned zero” are not sufficient. The strongest
available evidence, in descending order, is:

1. a structured verdict or state transition with the requested identity;
2. a durable artifact whose contents and freshness can be checked;
3. a user-visible effect, such as the exact Waveform Viewer contents;
4. a precise failure class and recovery result;
5. console prose only when the contract is itself a diagnostic message.

For stateful behavior, the suite should prefer a success, a controlled failure,
and recovery or persistence across restart. For read-only discovery, a stable
machine-readable listing plus one rejected input is enough. The shared Flow
contract makes this concrete: exit `0` is a passing design verdict, exit `1` is
a reached-but-failed design verdict, and exit `2` is configuration,
infrastructure, or execution failure; each must remain distinguishable in MCP
and durable reports ([Flow result contract](../user/FLOW_REFERENCE.md#shared-result-contract)).

## 1. Host lifecycle and administration

All entries in this section are **Supported** unless qualified otherwise.

| ID | Capability and contract | Minimum useful scenario evidence |
| --- | --- | --- |
| H-01 | Install the Python 3.11+ host CLI with `pipx` (preferred) or `pip`; `booley --version` identifies the installed release. Git 2.37.2+, Docker, VS Code with Dev Containers, and enough Docker storage are prerequisites. Windows and Linux are supported; macOS is not ([installation](../../README.md#installation)). | Fresh install succeeds on a supported host, prerequisite failures name the missing or old component, executable discovery works in a new shell, and version output matches the package under test. Capture the documented `pip` externally-managed failure/recovery only on an applicable distro. Before the RISC-V lane, prove at least 21 GB of free Docker storage plus artifact headroom rather than confusing the roughly 6 GB compressed transfer with the roughly 20 GB extracted image. |
| H-02 | `booley bootstrap` reconciles reusable skills, PDK cache, base Session Image, network, proxy, and reaper. `--check-only` is read-only and returns 1 when work is pending; normal operation is idempotent; `--force` refreshes Booley-owned resources while preserving caches and user files ([Host Bootstrap](../user/SETUP.md#host-bootstrap--host)). | Run check-only before and after reconciliation, repeat normal bootstrap, verify image/PDK/skill-link identities, and inject one safe stale managed resource to prove repair without overwriting user-owned content. |
| H-03 | `booley init` is host-only and idempotently creates or reconciles Project state, provider/auth policy, Ticket Board, Session Image, Git hooks, and devcontainer issuance. It supports `--check-only`, `--force`, `--seed`, `--skip-credentials`, and explicit Claude/Codex auth selection ([Project Initialization](../user/SETUP.md#initialize-the-project--host)). | A port uses plain init, verifies the generated state and issued devcontainer, repeats init without drift, and proves host/container location guards. Across the suite, exercise both provider selections and credential policies. |
| H-04 | `booley init --scaffold` supports a greenfield IP with Verilator or Icarus, HDL or cocotb TB, Verilator or Verible lint, optional ASIC, and optional Vivado part ([scaffold contract](../user/SETUP.md#what---scaffold-adds)). | This is supported but does not naturally fit scenarios whose workload is porting existing IP. Record it as a known initial coverage gap unless a small separate scaffold scenario is added; do not claim it through a port. |
| H-05 | `booley auth` supports Claude and Codex status, storage, stdin input, and clear; provider/auth policy can pin subscription, API key, or automatic precedence ([Auth and billing](../user/USAGE.md#auth--billing)). | Use non-secret status fields and a disposable credential mechanism. Prove selection/override reporting and that secrets remain outside the Project and are not printed or committed. Do not record token values. |
| H-06 | `booley session up/status/enter/down/validate/refresh` owns headless Session Runtime lifecycle. `enter -- <command>` preserves exit/signal semantics and cleans descendants; refresh is transactional and refuses to replace a VS Code-owned runtime ([headless runtime](../user/USAGE.md#entering-the-session-runtime-without-vs-code)). The VS Code prepare lifecycle may identity-check, stop, validate, and remove exactly one authenticated legacy VS Code container; it refuses ambiguous, foreign, headless, or multiple matches and supplies an exact restart command if validation fails after the stop ([mount recovery](../user/TROUBLESHOOTING.md#vs-code-says-a-mount-config-is-invalid-while-reopening-the-container)). | Observe absent→running→stopped/absent, execute a success and nonzero command, interrupt one supervised process tree, validate the issued spec, and verify refresh either swaps to the expected immutable image or restores the old runtime. In a disposable VS Code fixture, prove both safe legacy replacement and post-stop validation recovery without touching a foreign/headless runtime or named volume. |
| H-07 | The host Project Inventory lists remembered roots and Grants, imports only explicitly bounded discovery roots without following directory symlinks, retains missing/uninitialized roots, and refuses `forget` while Grants remain ([CLI reference](../user/USAGE.md#cli-reference)). | Initialize/import a disposable Project, inspect human and JSON forms, make it missing, prove it remains visible, prove forget fails with a Grant, revoke, then forget exactly that root. |
| H-08 | Host-provisioned Vivado administration registers/lists/shows/removes one exact installation and adds/revokes an exact canonical-Project Grant. Projects request only `provisioning = "host"`; host policy owns the source mount and fixed container destination ([commercial provisioning](../user/CONFIG.md#commercial-eda-provisioning)). | On provisioned Linux x86-64, register a disposable name, prove stable JSON and human forms, grant the canonical Project, inspect through `booley projects`, validate the read-only `/opt/booley-eda/vivado` mount and executable version, revoke, and clean up. Never modify the registered installation. |
| H-09 | A License Profile can be registered and attached to a Grant, but the fixed FlexNet relay is **Experimental** because paid-seat checkout, accounting, concurrency, and return have not been validated ([Vivado policy](../user/SUPPORTED-EDA-TOOLS.md#vivado-host-provisioning-policy)). | Keep registration/validation visible in a separately qualified lane. A real licensed checkout cannot become mandatory until site approval and evidence exist. On Windows and unprovisioned hosts report `not runnable`. |
| H-10 | `booley doctor` is available on either side of the runtime boundary. Plain Doctor validates configuration and setup; `--deep` runs selected sim/lint/synth smoke checks while printing manual FPGA commands; `--skip-agent-checks` is a credential-free release-smoke option. Explicit, expiring `doctor-waivers.toml` entries waive warnings, never failures ([Doctor](../user/USAGE.md#first-verify-your-setup), [waivers](../user/CONFIG.md#doctor-waivers-doctor-waiverstoml)). | Prove clean plain/deep runs, structured `last.json` and `last.log`, a deliberate actionable warning, a valid waiver and its expiry/staleness behavior, one hard failure that cannot be waived, and automatic health freshness reporting after configuration change. |
| H-11 | `booley upgrade status/acknowledge` and `/booley-heal` form the release-change review and repair path ([Troubleshooting entry point](../user/TROUBLESHOOTING.md)). | Pin an old observed version in disposable Project state, verify pending status, run documented health recovery, and acknowledge only the exact verified target. This is a lifecycle check, not an excuse to mutate a live user Project. |
| H-12 | The user-owned host `config.toml` controls Docker-daemon-wide Interactive idle timeout, Session Runtime cap, and extra egress hostnames. Parsing is strict, entries are hostname-only, and invalid policy stops Host Bootstrap before mutation; Booley reads but never rewrites the file ([host configuration](../user/CONFIG.md#host-configuration-configtoml)). | With a disposable config root, prove absent-file defaults, one valid override for each field, cross-Project session-cap behavior, allowlisted and denied egress, rejection of schemes/paths/ports/IPs/wildcards and unknown keys, and byte-for-byte preservation on both success and validation failure. |

### Operating-system applicability

Scenario instructions should be OS-neutral and use Booley's documented commands,
not shell-specific substitutes. Qualification should include at least one full
Windows and one full Linux run without requiring the full scenario/OS cross
product. The standard image-provisioned toolchain is supported on both;
host-provisioned Vivado 2025.2 is supported only on Linux x86-64. Windows must
report that section `not runnable`, not silently skip or fail it
([Windows support](../user/FEATURES.md#windows-support),
[Vivado platform boundary](../user/SUPPORTED-EDA-TOOLS.md#vivado-host-provisioning-policy)).

## 2. Project Setup, configuration, Targets, and testbenches

| ID | Capability and contract | Minimum useful scenario evidence |
| --- | --- | --- |
| P-01 | `/booley-setup` is plan-first Project Setup: port feasibility and one approved `SETUP-PLAN.md`, then environment, `.core`/`tests.toml`/`booley.toml`, Project guidance, plain Doctor, and deep Doctor ([port sequence](../user/SETUP.md#porting-an-existing-project-plan-then-execute)). | The executor uses published Setup docs, skill instructions, `booley cheat`, and live help. Compare the approved plan to resulting files and record every deviation. No source-derived workaround may be consulted until an observation is captured. |
| P-02 | Every Booley Flow consumes an explicit named FuseSoC CAPI2 Target; there is no project-wide default. Qualified `vlnv#target` selectors resolve collisions. The Target owns its typed parameters, defines, top, tool, and values; there is no per-call parameter override. `booley targets`, filters, detail, JSON, and `booley_targets` expose the same surface ([Targets](../../src/booley/data/cheatsheet.md#targets), [Target authoring](../user/CONFIG.md#target-authoring)). | Use at least one multi-Target or colliding-name IP, compare CLI and MCP JSON identity, run a qualified selector, reject an ambiguous bare selector, and verify `.core` changes appear after refresh. Use two Targets for two parameter values and reject an invented per-call override. |
| P-03 | Project-owned Target axes are `sim_`, `lint_`, `synth_`, and `fpga_`; Doctor selection lives per Target. Native vendored names remain valid and Doctor self-test Targets stay hidden from ordinary target discovery ([Target authoring](../user/CONFIG.md#target-authoring)). | Exercise authored and vendored names, selected/non-selected Doctor Targets, and one hidden `lint_selftest_bad` used only by deep Doctor. |
| P-04 | HDL-testbench Targets use configurable pass/fail sentinels; fail wins over pass, and clean exit without a recognized verdict is inconclusive. Tests can be selected by one plusarg or getopt token, skipped by default yet explicitly overridden, and can emit named Cycle Counts ([sentinels](../user/CONFIG.md#simulation--passfail-sentinels-flowssim), [tests](../user/CONFIG.md#tests-teststoml)). | Demonstrate pass, fail, both markers, no marker, exact named selection, default skip plus explicit override, and valid/invalid Cycle Count evidence. Preserve logs and structured per-test verdicts. |
| P-05 | Cocotb Targets use one Python module per Target, registered test function names, `COCOTB_TEST_FILTER` for 2.x or `TESTCASE` for 1.x, `results.xml` rather than sentinels, and Verilator or Icarus only. Missing/truncated result XML is inconclusive ([Cocotb Targets](../user/CONFIG.md#cocotb-targets-python-testbenches)). | Run a full and focused suite, inspect compact and full result presentation plus retained XML/JSON, prove one assertion failure and one missing-result infrastructure case, and cover at least one multi-file TB package. |
| P-06 | Per-Target environment, pre-run commands, run working directory, frozen-simulation watchdog, trace arguments/files, and per-run disk-growth budget adapt upstream firmware/vector/testbench workflows without editing them ([simulation configuration](../user/CONFIG.md#simulation--passfail-sentinels-flowssim)). | A realistic firmware or vector build should prove environment propagation, per-test command substitution, staged input, timeout/frozen-clock classification, and disk-budget kill with largest-file diagnostics. Keep the zero-testbench-edit route; any exception requires per-run user approval. |
| P-07 | Project Python dependencies are baked by `[sandbox].pip_requirements`; a custom Session Image can extend a managed image; host skills are opt-in and read-only; a post-setup hook is bounded, idempotent, and runs once per new Ticket Workspace ([sandbox dependencies](../user/CONFIG.md#sandbox-sandbox), [advanced setup](../user/CONFIG.md#advanced-setups)). | Verify one derived image from pinned requirements, image freshness after a pin change, absence of runtime network install, read-only mounted host skill behavior if enabled, and a post-setup success plus controlled nonzero block. |
| P-08 | `booley-sandbox-riscv` is a supported Session Image variant with RISC-V GCC, `srec_cat`, `dtc`, Spike, and offline specifications; its documented Docker-storage requirement is part of the route, not incidental setup advice ([RISC-V image](../user/CONFIG.md#risc-v-toolchain-image-booley-sandbox-riscv)). | If a CPU IP is chosen, preflight the documented 21 GB free-storage requirement, build real firmware in pre-run commands, verify the exact toolchain and Spike identity inside the runtime, and retain the firmware/test artifact. Otherwise record this supported image lane as uncovered. |
| P-09 | Flat repos, vendored/FUSESOC_IGNORE trees, many cores, and initialized clean non-shallow submodules are supported. Ticket and baseline worktrees reconstruct pinned submodule objects without network access ([flat/vendored repos](../user/CONFIG.md#flat-and-vendored-repos), [submodules](../user/CONFIG.md#submodules)). | Prefer at least one real IP with vendored or submodule structure. Prove the selected historical gitlink, a clean materialization, and a precise rejection for dirty/shallow/missing source state without contacting the remote. |
| P-10 | Project Custom Flows, Specialists, or direct MCP endpoints may live under `.booley_project/mcp_tools/` with project Criteria, use normal discovery and report contracts, and can be explicitly disabled ([extension contract](../internals/MCP-TOOLS.md#chapter-1-discovery-visibility-and-configuration)). | A small deterministic Custom Flow is the cheapest complete check: discover it, dry-run/preflight it, satisfy its Criterion, disable it, and inject one syntax/schema failure that Doctor reports. It must not replace the built-in `sim` verdict. |

## 3. Interactive Mode and common MCP behavior

| ID | Capability and contract | Minimum useful scenario evidence |
| --- | --- | --- |
| I-01 | Interactive Mode uses the Project's Session Runtime and a Claude or Codex VS Code client, with Booley MCP registration, skills, full in-container permission mode, default-deny network, and no Ticket/Criteria state ([Interactive Mode](../user/USAGE.md#interactive-mode), [architecture](../internals/ARCHITECTURE.md#interactive-mode)). | Across the suite, start one Claude and one Codex VS Code session as distinct coverage dimensions, call `booley_status`, list Targets, run a real Flow and Specialist, inspect `.interactive_logs`, edit/commit in a disposable branch, and prove remote push is blocked. |
| I-02 | `booley_status` reports tab readiness; `booley_targets` lists accepted selectors; `booley_report` recovers the latest durable report after an inline client timeout; `booley_poll` completes detached long work; `booley_cancel` gives queued/running work a distinct cancelled outcome. Poll/targets/cancel are available in all modes, while status/report are primarily Interactive Mode ([MCP server contracts](../../src/booley/mcp/server.py)). | Verify tool discovery and schemas in both clients, start a job long enough to return a `run_id`, long-poll it, recover its report, cancel a second queued/running job, and confirm cancellation is neither design failure nor infrastructure error. |
| I-03 | Built-in Flows and active Specialists are discovered by default; `[flows.<name>].enabled = false` or `[mcp_tools.<name>].enabled = false` removes them. Interactive Mode intentionally hides `submit_run_report`; Ticket Mode retains it ([missing MCP tool guide](../user/TROUBLESHOOTING.md#an-mcp-tool-is-missing-from-mcp)). | Compare normal Interactive and Ticket MCP lists, disable and restore one endpoint, and prove the diagnostic error explains intentional hiding rather than claiming installation failure. |
| I-04 | Interactive sessions share the checked-out tree; multiple sessions may collide. Jobs share per-class limits with Interactive work ahead of Ticket work, FIFO within a class, no preemption, and bounded queues ([parallel instances](../user/FEATURES.md#parallel-instances)). | Run two non-editing Interactive calls plus concurrent Ticket work to observe safe admission and priority. Test shared-tree edit collision only in a disposable fixture; do not manufacture damage in a real port. |
| I-05 | `booley cheat`, command help, MCP descriptions, packaged skills, and user docs are public operational surfaces. The agent is expected to derive mechanics from them rather than maintainer knowledge ([Quick Reference](../../src/booley/data/cheatsheet.md)). | Every scenario step records which public source supplied the route. Missing, contradictory, or insufficient guidance is a docs Finding before source inspection. Help output and the rendered cheat catalog should agree on command/Flow/Specialist names. |

### Interactive client documentation conflict; coverage already decided

The public docs do not currently name one coherent client set. The Usage guide
documents bare `booley` launching either Claude Code or Codex CLI and recommends
the Codex CLI for concurrent Interactive sessions; it also documents the Claude
Code VS Code extension ([first session](../user/USAGE.md#open-your-first-agent-session)).
The canonical glossary instead says Interactive Mode uses the Claude Code or
Codex VS Code extension and that standalone apps are unsupported
([glossary](../CONTEXT.md#execution)). The generated cheat sheet again says
`booley` opens the configured CLI.

The parent map has already fixed the QA matrix: cover Claude and Codex as
Interactive Mode **VS Code clients**, and cover them separately as Ticket Mode
Developer Agent backends
([map standing constraints](https://github.com/boldaxolotl/booley/issues/246)).
Therefore this contradiction is a documentation Finding, not an unresolved
scenario-design choice and not a reason to add a standalone-client coverage
dimension. A run should still record the observed bare-`booley` behavior so the
public path can be repaired or reconciled without silently changing coverage.

## 4. Built-in Booley Flows and EDA tools

All four built-in Flows are **Supported**. Every Target-aware invocation must
name a Target. Each offers dry-run, normalized exit grades, durable invocation
reports, complete logs, and artifact pointers; multi-Target calls must continue
through all selected Targets and preserve the strongest failure grade
([Flow reference](../user/FLOW_REFERENCE.md)).

| ID | Flow contract | Minimum useful scenario evidence |
| --- | --- | --- |
| F-01 | `sim` supports full HDL/cocotb simulation, focused tests, skips, trace, result verbosity, multi-Target calls, Elaboration Check, and standalone module sweep. Successful elaboration is durable even when the later run fails ([Simulation Flow](../user/FLOW_REFERENCE.md#sim)). | Cover HDL and cocotb elsewhere in the matrix; prove pass/fail/inconclusive/infra grades, focus/skip, traced freshness, elab-only run-argument rejection, standalone pass plus real module failure, and `sim_<target>.json` identity/artifacts. |
| F-02 | `lint` drives Verilator structural/semantic lint or Verible style/naming lint, supports scope filtering, deduplicates findings across Targets, and records findings even when warnings are configured not to fail the CLI ([Lint Flow](../user/FLOW_REFERENCE.md#lint)). | Use one Target for each linter; prove clean, warning, hard/infrastructure failure, scoped filtering, cross-Target dedupe, and the distinction between CLI rc and report `passed`. |
| F-03 | `synth` drives Yosys logical or Yosys+OpenROAD physical PPA estimation, with sv2v or slang frontend, SDC/default clock, profiles/expert overrides, thresholds, optional baseline, and provenance. It is explicitly not tape-out/signoff ([Synthesis Flow](../user/FLOW_REFERENCE.md#synth)). | Cover logical and physical modes so both Yosys and OpenROAD execute, and cover both supported frontends on natural Targets. Prove metrics/provenance, latches/critical conditions, timing advisory versus configured failure, a baseline delta, and missing/incompatible frontend classification. |
| F-04 | `fpga` drives host-provisioned Vivado 2025.2, requires part/top/XDC, normalizes routed utilization/timing/critical conditions, has content-addressed caching, baseline comparison, and dry-run source inspection ([FPGA Flow](../user/FLOW_REFERENCE.md#fpga)). | In the Linux qualification lane, prove dry-run, fresh implementation, authenticated cache hit, `--no-cache`, a controlled design failure, an authority/infrastructure failure, metrics/artifacts, and baseline provenance. On other hosts report `not runnable`. |

### Mandatory EDA-tool coverage set

The [supported EDA matrix](../user/SUPPORTED-EDA-TOOLS.md#built-in-flows) defines
the concrete tools Booley drives today. “Every supported EDA tool” therefore
means at least one artifact-verified invocation whose observed tool version
matches the matrix (or is an explicitly qualified Project override) of:

| EDA tool or supported component | How to exercise it without a full cross-product |
| --- | --- |
| Verilator | One simulation Target and one lint Target are distinct public integrations; both should be covered because they use different Flow contracts. |
| Icarus Verilog | One simulation Target; it can carry either the HDL or cocotb TB axis. |
| Verible | One lint Target. |
| Yosys | One logical synth Target, plus the physical Target below. |
| OpenROAD | One physical synth Target; a logical-only run does not exercise it. |
| sv2v frontend | One synthesis Target using the default frontend. |
| slang frontend | One synthesis Target that naturally needs or supports slang; the two frontends are not interchangeable in all designs. |
| AMD Vivado 2025.2 | One provisioned Linux x86-64 FPGA Target; capability-gated elsewhere. |
| B-Wave | One real traced simulation and the query/GUI contract in the next section. |
| FuseSoC/Edalize resolution | Cross-cutting rather than a separate Flow: verify resolved Target identity and build inputs in every Flow family. |

The RISC-V GCC/Spike image is a supported Project toolchain lane, not a built-in
Flow EDA selector; it is covered separately as P-08. Cocotb is a supported
testbench framework, not an EDA tool. Full Cartesian coverage is unnecessary.

The following are **not supported EDA integrations**: VHDL/GHDL/NVC, UVM,
commercial cocotb simulators, Xcelium, VCS, Questa/ModelSim, Design/Fusion
Compiler, Genus, HAL, SpyGlass, Verdi, JasperGold, Quartus, tape-out synthesis,
signoff STA, CTS/routing, DRC/LVS, and multi-corner foundry analysis
([language and matrix boundary](../user/SUPPORTED-EDA-TOOLS.md#read-this-first),
[roadmap](../internals/ROADMAP.md#commercial-eda-tools)).

## 5. Specialists and Criteria

| ID | Capability and contract | Minimum useful scenario evidence |
| --- | --- | --- |
| S-01 | The active read-only `reviewer` has seven public focus contracts: RTL bugs, protocol, spec, code style, optimization, security, and TB quality. It reports CRITICAL/MAJOR/MINOR; `_done` is advisory terminal evidence, while `_clean` requires every finding fixed or explicitly waived and both stale after relevant edits. Shipped RTL/TB guidance is always present and optional Project guidance is appended with precedence ([reviewer catalog](../user/USAGE.md#reviewer), [RTL guides](../user/FEATURES.md#expert-written-rtl-guides)). | Distribute every focus across the initial scenarios. Seed at least one real finding and one clean focus, exercise `_done` and `_clean`, prove staleness after a source edit, and inspect report/transcript/waiver evidence. Prove one Project-guide override reaches the reviewer and wins a controlled conflict. Spec focus must use the Ticket/spec path, not reconstructed chat prose. |
| S-02 | `mutation_tester` creates exact proposal-locked replacements without seeing the TB, runs pristine baseline and isolated mutants, supports default 10/10, explicit N/K, and size-scaled 3–25 campaigns, reuses a valid lock, and emits one atomic manifest ([mutation catalog](../user/USAGE.md#mutation_tester)). | Run one small real campaign with a killed and surviving mutant, inspect baseline/per-mutant logs and first-killing-test evidence, use dry-run auto sizing, prove lock reuse, and regenerate only with `--regen-lock`. Avoid paying for every mode merely to repeat the same contract. |
| S-03 | Specialist Source Isolation hides the opposite side of the RTL/TB boundary when independence matters; Specialists run in fresh contexts and scoped workspaces ([glossary](../CONTEXT.md#flows-eda-tools-and-mcp-tools)). | Check the Specialist prompt/workspace manifest and transcript for the allowed source set, verify no forbidden source content appears, and prove source restoration/cleanup after completion or failure. |
| S-04 | Built-in Criteria are `elab_pass`, `elaborate_standalone`, per-Target `lint_clean`, all seven review families, per-Target `sim_pass`, per-test `cycle_count`, per-Target `mutation_score`, `synthesis_ok`, and `fpga_impl_ok` ([Criteria catalog](../user/USAGE.md#acceptance-criteria)). | Every family needs at least one bound Ticket across the suite. Do not infer coverage from a standalone Flow that did not bind the sealed Criterion. |
| S-05 | Mandatory Criteria block review; optional unmet Criteria require current justification. Relevant edits stale evidence. Calls outside a sealed Flow/Target contract are rejected unless explicitly diagnostic. Simulation `fail -> pass` requires recorded failure evidence ([acceptance semantics](../user/USAGE.md#acceptance-criteria)). | One Ticket should demonstrate optional justification, one stale-then-rerun path, one rejected out-of-contract call followed by `--diagnostic`, and one real fail→fix→pass sequence. |
| S-06 | Synthesis/FPGA/Cycle Count thresholds support absolute and baseline-relative forms, directed baseline/candidate Target pairs, per-clock timing scope, and fail closed on missing/mismatched evidence ([threshold parameters](../user/USAGE.md#threshold-parameters)). | Cover at least one absolute threshold, one relative threshold, one directed pair, one clock-scoped timing check, and one invalid/missing baseline rejection; artifacts must identify both refs and recipes. |

## 6. Ticket Mode and Ticket Board

| ID | Capability and contract | Minimum useful scenario evidence |
| --- | --- | --- |
| T-01 | `/booley-ticket-create` supports Lightweight and Detailed-plan creation, applies `ticket_creation.md`, shows one complete draft for approval, prepares/seals Target contracts, and enqueues only a validated Ticket ([creating Tickets](../user/USAGE.md#creating-tickets)). | Use both detail modes somewhere in the suite, prove Project guidance and explicit one-Ticket override precedence, reject one invalid Criterion/Scope/Target, and verify the approved draft equals the queued sealed contract. |
| T-02 | Four Ticket types are supported: Feature, Bug Fix, Refactor, and Verification. Type-specific report detail is enforced by `submit_run_report` ([Ticket workflow](../user/FEATURES.md#ticket-driven-workflow), [report endpoint](../../src/booley/mcp/submit_run_report.py)). | Distribute all four types across the suite. Each run should leave a correctly typed, committed REPORT or a deliberate justified no-report outcome under the configured policy. |
| T-03 | Normal lifecycle is draft→queued→running→review→done; dependency Tickets use waiting→queued; escalation uses running→blocked→queued and resumes the same workspace; explicit successful shortcut is running→done ([lifecycle](../user/USAGE.md#ticket-board-lifecycle)). | Observe every transition and transition log, include a dependency release and human-feedback unblock, and prove resume retains branch/workspace/evidence rather than starting a second Ticket. |
| T-04 | Review is a human decision: small in-place correction then done, full reset to a clean queue, or archive. Ordinary review→queue is invalid. Reset retires branch/worktree and archives prior-run artifacts ([review decisions](../user/USAGE.md#ticket-board-lifecycle)). | Use different Tickets for approval, reset, and archive; verify forbidden move rejection and prior-run preservation. Destructive reset/cleanup must target only disposable scenario Tickets. |
| T-05 | Scope is commit authorization, enforced by Ticket-worktree hooks; Harness bookkeeping and Target/control inputs are hard boundaries. `submit_run_report` rejects staged, modified, deleted, or untracked work. Scope deviations remain a backstop artifact ([Scope](../user/USAGE.md#scope)). | Attempt one safe out-of-Scope commit and one bookkeeping edit, verify hard rejection and clean recovery, then deliberately produce/read a deviation only through a documented legacy/bypass fixture if that can be done without weakening the real Project. |
| T-06 | Target Contracts seal all Criteria-bound Targets and repository refs before enqueue. Baseline-relative work uses immutable `base_sha`; creation-time `remove_targets` can omit temporary bound Targets from the accepted destination ([Target threshold contract](../user/USAGE.md#threshold-parameters), [on-success](../user/USAGE.md#where-the-work-lands-on_success)). | Verify seal identity, reject a Developer-time contract edit, revise/reseal through the creation/triage path, and inspect destination history after one temporary comparison Target is removed. |
| T-07 | `on_success` controls review/done destination, merge, cleanup, triage report, and target removal. Review-bound runs emit a stable `BOOLEY_RUN_RESULT` and a versioned deterministic JSON briefing; optional HTML generation may fail without failing accepted work ([on-success](../user/USAGE.md#where-the-work-lands-on_success)). | Cover review and done destinations, merge/cleanup, JSON record parsing, deterministic briefing, rich HTML success, and rich HTML regeneration/failure without altering acceptance. |
| T-08 | Ticket Mode checkpoints completed Flow/Specialist invocations, resumes after interruption, requeues subscription limits, and can auto-retry only a configured known transient stream-stall signature ([unattended execution](../user/USAGE.md#running-unattended), [auto-retry](../user/CONFIG.md#auto-retry-on-transient-crashes-developerauto_retry)). | Interrupt after at least one completed capability, restart, prove no duplicate accepted evidence, and distinguish human blocker, subscription wait/requeue, known auto-retry, ordinary crash, and Developer timeout. |
| T-09 | Multiple `booley run` instances claim separate Tickets/worktrees. Job Classes cap Developer, heavy EDA, and light Specialist work; excess waits, Interactive work has priority, and a full queue blocks admission ([concurrent Tickets](../user/USAGE.md#concurrent-tickets)). | Run two Tickets concurrently, observe atomic claim and isolated diffs/artifacts, force one queued Job and cancel it, and verify no cross-Ticket files or evidence. A tiny queue setting can test full-queue rejection without expensive parallel EDA. |
| T-10 | The default Console is a full-screen live view; `--no-console` is stable log mode. Board show/briefing, run dry-run/check-ready, named-ticket and idle-loop controls are public CLI behavior ([CLI reference](../user/USAGE.md#cli-reference)). | Exercise TUI and log modes, a dry-run/check-ready with no transition, a named run, drained timeout, board human output, and machine-readable review record. Screenshot only when a visual layout claim matters. |
| T-11 | `[developer]` selects human-availability semantics, structured run-report policy, and active versus wall timeout; `[models]` supplies provider-specific heavy/standard/light tiers and per-role model pins ([Developer Agent policy](../user/CONFIG.md#developer-agent-policy-developer), [model selection](../user/CONFIG.md#model-selection-models)). | Use the map's unattended setting without manufacturing live approval, cover both run-report policies and both timeout classes, verify a tier override and one role override in durable execution metadata, and reject an unknown role or invalid limit before agent work begins. Claude and Codex remain separate backend dimensions. |
| T-12 | Configured ntfy.sh notifications are a **Supported** user-facing contract for a Ticket completion/block and an automatic Doctor issue ([push notifications](../user/USAGE.md#push-notifications)). Notification delivery is advisory and must not change the underlying lifecycle outcome. | With a pre-authorized disposable topic and egress, observe one notification for each documented event, prove an absent topic is a no-op, and show an unavailable notifier cannot block the Ticket or Doctor transition. Do not reuse a personal topic, exercise undocumented event filtering as supported behavior, or infer delivery from a spawned `curl` process. |

### Push-notification documentation conflict

The user docs promise notifications when a Ticket “completes or blocks” and
when automatic Doctor finds an issue. Current source sends Ticket notifications
on `review` and `blocked`, not `done`, and also implements an undocumented
`notifications.events` filter plus a Claude rate-limit event
([Ticket operations](../../src/booley/ticket_board/operations.py),
[notification implementation](../../src/booley/ticket_board/notifications.py),
[automatic Doctor](../../src/booley/harness/auto_doctor.py)). The suite should
assert the documented completion/block/Doctor contract and report the mismatch;
it must not promote the undocumented events or selector to mandatory coverage.

## 7. B-Wave and the Waveform Viewer

B-Wave is a supported EDA tool with its own bundled public corpus. The
[introduction](../../crates/bwave/docs/public/intro.md) and
[subcommand overview](../../crates/bwave/docs/public/commands/overview.md) are
the canonical concepts; the Python wrapper adds persistent aliases, markers,
and GUI control.

| ID | Capability and contract | Minimum useful scenario evidence |
| --- | --- | --- |
| W-01 | A successful traced Simulation produces a fresh queryable FST Trace Artifact. Native FST is consumed directly; otherwise VCD can be converted, including FIFO streaming. Raw VCD and retired `.bwave` are not successful query stores ([trace contract](../user/FEATURES.md#waveform-based-debug), [build](../../crates/bwave/docs/public/commands/build.md)). | Produce one Verilator and one Icarus trace across the suite, verify freshness/size/scope/count metadata, query both, test direct native FST if the chosen IP has it, and reject raw VCD/legacy/empty/truncated stores with actionable guidance. |
| W-02 | The wrapper registers a file or sim directory under an alias and `_last`, can auto-build when requested, and persists named markers; queries resolve aliases to immutable paths ([wrapper implementation](../../src/booley/bwave/cli.py), [round-trip tests](../../tests/test_interactive_smoke.py)). | Register, restart the command/session context, query `@alias` and latest, set/list/delete markers, and prove stale or missing registered traces are reported rather than silently replaced. |
| W-03 | Public query/introspection commands are `list`, `signal`, `wave`, `value`, `find`, `sample`, `diff`, `distance`, `stats`, and `stuck`. They cover hierarchy, cycle/tick traces, snapshots, values/edges, triggered sampling, deltas, latency/period statistics, toggles/time-in-state, and stuck nets. Meta commands `schema`, `docs`, and `skill` expose the machine grammar and bundled operational corpus ([overview](../../crates/bwave/docs/public/commands/overview.md)). | Use one deterministic trace with known ground truth to invoke every query/introspection command at least once, checking actual numeric/value results rather than rc. Include pattern miss, ambiguous signal, output limit, and each documented JSON-supported/unsupported boundary. Verify `schema`, topic list/search/show, and skill output are installed and internally coherent. |
| W-04 | Sync mode samples post-edge cycles with clock/reset discovery; async mode reports raw transitions and requires typed time tokens. Queries support explicit clocks/reset, physical time/cycle/tick tokens, Verilog literals, bit slices, and virtual signals ([sync/async](../../crates/bwave/docs/public/reference/sync-vs-async.md), [time tokens](../../crates/bwave/docs/public/reference/time-tokens.md), [virtual signals](../../crates/bwave/docs/public/reference/virtual-signals.md)). | Check one known event in sync and async views, override an intentionally ambiguous clock, include/skip reset, use cross-unit time, value and edge queries, one composite virtual predicate, and errors for ambiguous bare async time, bad literal, unknown signal, and width mismatch. |
| W-05 | `bwave gui` is human-facing. Bare GUI can fall back to the editor CLI; scoped `--signals/--time/--cursor/--append` requires live VaporView WCP, automatically adds the clock on a new view, caps expansion, places two range markers, reads back displayed signals, and warns on dropped viewer signals ([GUI contract](../../crates/bwave/docs/public/commands/gui.md)). | On a VS Code-attached lane, first query the event, then open a scoped view. An unattended run must retain WCP readback plus a screenshot for later human review: trace identity, clock row 1, exact signal set, start/end markers, cursor, appended signal, and dropped-signal warning. Also prove scoped failure when WCP is unavailable and bare fallback. The separate GUI-observation ticket decides the reliable capture mechanism. |

This gives GUI automation a concrete route: protocol-level assertions through
WCP can establish exact viewer state, while a screenshot or narrow human check
establishes that VaporView actually rendered it. Full pixel automation is not
required to avoid treating “command exited zero” as GUI coverage. Existing WCP
failure and recovery behavior is documented in
[Troubleshooting](../user/TROUBLESHOOTING.md#bwave-gui-fails-on-a-scoped-view).

### B-Wave option-matrix documentation conflict

The bundled overview first says every query/introspection command accepts
`--virtual`, then its own consumer-options table narrows the set. The
virtual-signal reference excludes `list`, `stats`, `stuck`, `diff`, and
`build`, while the `diff` page advertises `--virtual` in its synopsis. The
introduction and per-command pages consistently limit implemented JSON output
to `list`, `value`, `find`, and `stats`; the remaining per-command synopses
accept `--format` but explicitly reject JSON output. Therefore JSON is a
documented supported/unsupported matrix, not a conflict. Live per-command
`--help` and behavior must resolve only the `--virtual` disagreement before a
scenario freezes that option matrix. The command families themselves remain
clearly supported.

## 8. Stealth Mode, security, and repository behavior

| ID | Capability and contract | Minimum useful scenario evidence |
| --- | --- | --- |
| ST-01 | Setup writes explicit `[stealth] enabled = false` unless the user opts in; omitted `enabled` retains a legacy enabled history-sanitization fallback. The suite needs at least one explicit true and one explicit false Project ([Stealth configuration](../user/CONFIG.md#stealth-mode-stealth)). | Non-stealth keeps verbatim commit prose and ordinary cores. Stealth proves sanitized history and hidden Project data. Treat the missing-key fallback as compatibility coverage, not the non-stealth lane. |
| ST-02 | Explicit stealth keeps authored cores in `.booley_project/cores`, refreshes ignored projected root cores, optionally ignores all native cores, and stores Project state in a separate local repo excluded from the outer history ([Stealth configuration](../user/CONFIG.md#stealth-mode-stealth)). | Resolve and run a projected core without copied/symlinked RTL, refresh after an authored-core edit, prove `ignore_native_cores` bypasses an intentionally invalid native core, and inspect outer/inner Git status and fresh-clone limitations. |
| ST-03 | Stealth commit hooks substitute banned words and remove attribution trailers; optional subject convention and body cap reject rather than truncate; one environment escape skips convention/body but not sanitization. Pre-push can enforce author/committer allowlists and reject banned paths/symlink targets ([history policy](../user/CONFIG.md#enforcing-the-subject-convention-enforce_convention)). | Use disposable commits/remotes to prove sanitization, trailer removal, convention/body rejection, sanitized escape, allowlisted and rejected identities, path/symlink rejection, and the documented push guard escape. Never place real private identifiers in a public scenario fixture. |
| ST-04 | The Session Runtime runs non-root with restricted mounts, dropped capabilities, no Docker socket/host home/SSH, and default-deny egress except provider and authorized license gateways. Host EDA is read-only; agents cannot push origin ([security model](../internals/ARCHITECTURE.md#security--trust-model)). | Use benign probes for UID/capabilities, absent sensitive mounts/socket, write rejection on PDK/Vivado mounts, blocked general network and git push, allowed provider path through a real agent call, and process cleanup after interruption. Do not probe unrelated host secrets or external services. |

## 9. Feedback, documentation, and public reporting

| ID | Capability and contract | Minimum useful scenario evidence |
| --- | --- | --- |
| D-01 | `/booley-feedback` and `booley feedback` record defects, friction, impressions, and wins in an append-only Findings Log; entries have project/Booley/docs/unknown buckets, can be re-triaged and marked filed, and render one local unredacted report ([feedback config](../user/CONFIG.md#feedback-feedback)). | Exercise this end-user capability in one assigned probe: all entry kinds, list, triage, filed exclusion, concurrent append safety, persistent report regeneration, and project-vs-Booley classification. It is not the scenario suite's own result channel; the map assigns that role to direct, unredacted QA records for Consolidate Findings. |
| D-02 | Outbound feedback is selected, redacted, previewed exactly, and protected by a content-bound confirmation token. Modes are public GitHub ask, private mail handoff, file-only, and off; unattended runs resolve to file-only. Nothing is sent merely because a report exists ([offer and consent](../user/CONFIG.md#what-the-offer-looks-like-ask-and-email)). | Public QA should default to file-only. Exercise redaction, preview, token invalidation after rerender, explicit export, and dry-run/blocked submit without sending. A real upstream issue or mail requires separate user approval after exact outgoing text is shown. |
| D-03 | Documentation itself is under test: README→Setup→Usage, Config/Flow/EDA references, Troubleshooting, cheat sheet, CLI/MCP help, and packaged skills form the supported user path. | Each step cites the source used and records contradictions, missing prerequisites, or private-workaround dependence as docs Findings. Source inspection is allowed only after the observation is captured, to verify/classify it. |
| D-04 | Public scenario definitions can be versioned in the repository without leaking tested downstream identifiers. They should pin upstream IP/commit/dependencies and keep findings/evidence separate from normative assertions. | The later design should give each capability a scenario/step/stimulus/oracle/evidence pointer and a status. Unexpected discovery failures remain failed assertions even when a documented fallback lets independent sections continue. |

## 10. Explicitly non-mandatory surface

### Experimental

- Fixed-destination FlexNet license relay and real paid-seat lifecycle evidence.
  Registration is shipped; checkout/accounting/concurrency/return is not yet a
  support claim.

### Hidden

- `coverage_analyst` and `tb_coder` exist in the tree but are deliberately
  removed from MCP discovery until they mature
  ([registry exclusion](../../src/booley/mcp/registry.py),
  [coverage roadmap](../internals/ROADMAP.md#coverage-measurement)).
- `booley_sleep` is a diagnostic endpoint exposed only through
  `BOOLEY_MCP_DEBUG_TOOLS`; it measures client timeout behavior and is not a
  product workflow ([MCP server](../../src/booley/mcp/server.py)).
- `submit_run_report` is not maturity-hidden: it is supported and intentionally
  mode-scoped to autonomous Ticket Mode.

### Partial, prototype, or planned

- Verification-engineering regression management and failure clustering:
  planned.
- `booley ci`: planned.
- GitHub Issues as a Ticket Mode source: planned. The current Ticket Board is
  local Markdown only.
- Deterministic coverage measurement: hidden/incomplete.
- Full IP design decomposition from specification: prototype.
- Commercial cocotb: partial; image-provisioned Icarus/Verilator cocotb is
  supported.
- HDL dependency graph, additional formal/LEC/CDC/RDC/DFT/STA/power Flows,
  Intel FPGA, native VS Code panels, and standalone IDE: planned.

These statuses come directly from the [roadmap](../internals/ROADMAP.md). A QA
scenario may observe a planned/hidden boundary, but must not count it as
supported feature coverage or use private imports to invoke it.

The public README adds a scale boundary: only IP-level designs have been
tested, not chip- or SoC-level integration ([limitations](../../README.md#limitations)).
Treat chip/SoC integration as unqualified and outside mandatory initial
coverage rather than inferring support from the SystemVerilog language path.

## 11. Consequences for scenario design

The inventory has enough independent axes that two scenarios may cover the
headline combinations but are unlikely to cover the supported contracts
honestly. The coverage design, not a preselected number, should determine
whether a third is necessary. In particular, the following anchors cannot be
collapsed merely by saying “a Flow ran”:

- Claude and Codex Interactive clients and Ticket backends;
- explicit Stealth true and false;
- HDL and cocotb testbenches;
- Verilator simulation and lint, Icarus simulation, Verible lint, logical
  Yosys, physical Yosys+OpenROAD, and provisioned Vivado;
- headless Session Runtime and VS Code/WCP-attached behavior;
- all four Ticket types and important lifecycle branches;
- every active Specialist focus and Criterion family;
- Windows and Linux qualification, with Vivado capability-gated to Linux.

The eventual feature map should have one row per ID above and at least these
columns:

```text
status | capability | scenario | step | stimulus | expected grade/state
| evidence path | failure injection | recovery | OS applicability
| discovery result | regression assertion
```

During early dogfood runs, unexpected behavior is a Finding and execution may
continue from a checkpoint when safe. Once an assertion is established, the
same scenario becomes regression-capable: the assertion is mandatory and a
new failure makes the run non-green. A fallback never converts a failed
assertion to pass, and scenario text changes only through normal repository
review.

## Resolution

The supported QA surface is the union of H-01–H-12, P-01–P-10, I-01–I-05,
F-01–F-04 and the mandatory EDA set, S-01–S-06, T-01–T-12, W-01–W-05,
ST-01–ST-04, and D-01–D-04, subject to the stated mode/platform applicability.
The initial suite may use pairwise allocation and record explicit gaps, but it
should not label experimental, hidden, planned, prototype, or unsupported
behavior as mandatory coverage. The map has already settled client coverage;
before finalizing detailed B-Wave virtual-signal assertions, reconcile that
remaining public option-matrix conflict. The push-notification completion event
is a concrete documentation/implementation Finding for the later scenario
design, not another coverage-policy decision.
