# PicoRV32 published-demo continuity: migration and check catalogue

Scenario ID: `picorv32-published-demo-continuity`. This is an implementation design, not an executed qualification result.

Canonical journey: [Design the PicoRV32 published-demo continuity scenario — accepted resolution](https://github.com/boldaxolotl/booley/issues/374#issuecomment-5570739734). The [current issue amendment](https://github.com/boldaxolotl/booley/issues/374) supersedes historical shared mechanisms, but preserves the hardware workload, authority, prompts, Tickets, Criteria, faults, thresholds, evidence and cleanup.

## Reading this catalogue

Every check ID below is scenario-qualified. Each row states the action and observable expectation plus evidence to capture at that phase. The linked source section is the authority for its exact semantics. Rows deliberately separate distinct success, rejection, transition, persistence and cleanup claims. The exact accepted source contract is reproduced in the final annex so long prompts, field sets and parameter tables can be extracted without paraphrase. It is a historical source annex: its superseded bookkeeping is not the new execution contract.

The Selection column proposes a central `profiles.yaml` selection class; it is not check-owned profile membership. **Core** means semantic/product behavior; **Linux Vivado** means the existing Ubuntu/Codex provisioned-Vivado requirement; **GUI** requires actual supported-client or qualified visual evidence. Shared setup and producing phases must be selected in the same run for every selected downstream check. Core claims never imply actual VS Code client qualification.

Inherited from `qa/PROTOCOL.md`, `qa/FORMAT.md` and `qa/QUALIFICATION.md`: record compact immutable run inputs, append-only results/findings, explicit resource ownership and immutable evidence. Outcomes are pass/fail/blocked/unavailable; no `not runnable` pseudo-pass. Preserve original unexpected failures after recovery. Recovery points support the current live run only; coordinator interruption preserves a partial run, reconciles resources and starts a new run without reusing prior passes. All specification-backed checks are mandatory from reviewed introduction. No promotion, event replay, separate obligation/allocation entities or general resume system survives.

The parent handoff owns the approved eight-hour budget reallocation and central profile selections. Do not import the inconsistent historical allocation totals from the annex. All artifacts below include producing step, Target/EDA or agent identity as applicable, freshness evidence, immutable path and content identity; one artifact may support multiple independently evaluated rows. Commands/prompts are literal only where the source contract says so.

## Preparation, authority and Doctor

Authority: [accepted resolution, Frozen inputs and run declaration; ordered scenario](https://github.com/boldaxolotl/booley/issues/374#issuecomment-5570739734). Execute in the ordered journey below; subsequent phases depend on their required preceding state.

| Check ID | Selection | Stimulus and expected observation | Evidence / capture point |
|---|---|---|---|
| `picorv32-published-demo-continuity.inputs.project-pin` | Core | Prepare fresh `boldaxolotl/booley-prj-picorv32@b8fe2370cb9aa7d93617850169f42f07821865d6`; prove exact clean Project checkout. | Commit identity, clean status and initial tree hashes before initialization. |
| `picorv32-published-demo-continuity.inputs.upstream-pin` | Core | Prepare `YosysHQ/picorv32@a473fc8fca393771d83b0ffcf0b14db3393339d8`; prove exact clean upstream identity. | Commit identity and source hashes before preparation. |
| `picorv32-published-demo-continuity.inputs.release` | Core | Install exact published release; forbid floating tags, editable/local/development installs and source-checkout imports. | Release/package hash, release-doc identity, runtime import origin and selected image digest in run inputs. |
| `picorv32-published-demo-continuity.inputs.authority` | Core | Freeze provider/platform, eight-hour deadline, credentials mechanism without values, authorized operations and resource ownership before work. | Run declaration, capability probes and initial resource ledger. |
| `picorv32-published-demo-continuity.project.separate-repository` | Core | Create a separate clean `.booley_project` repository and initialize the prepared Project. | Repository topology, clean status, initialization output; frozen prepared recovery point. |
| `picorv32-published-demo-continuity.stealth.enabled` | Core | Enable Stealth and set `ignore_native_cores = true`. | Effective config and runtime configuration evidence. |
| `picorv32-published-demo-continuity.stealth.hidden-projection` | Core | Exercise hidden authored cores; only these may be projected. | Before/after projection inventory and resolved sources. |
| `picorv32-published-demo-continuity.stealth.native-no-copy` | Core | Inspect ignored native projections: no copied RTL may exist. | Projection paths and source/file inventory. |
| `picorv32-published-demo-continuity.stealth.native-no-symlink` | Core | Inspect ignored native projections: no symlinks may exist. | Filesystem link inventory independent of copied-file test. |
| `picorv32-published-demo-continuity.stealth.commit-sanitation` | Core | Exercise controlled banned-word commit sanitation. | Input commit text, hook invocation/result and resulting sanitized commit text; exact case to derive from public contract. |
| `picorv32-published-demo-continuity.stealth.attribution-removal` | Core | Exercise attribution-trailer removal. | Original and resulting commit messages with trailer comparison. |
| `picorv32-published-demo-continuity.doctor.first-product-exercise` | Core | Initialization/host preparation are setup; plain Doctor is the first Booley product exercise. | Ordered command record through first Doctor invocation. |
| `picorv32-published-demo-continuity.doctor.plain` | Core | Run plain Doctor; require warning-free result. | Doctor report and underlying diagnostics. |
| `picorv32-published-demo-continuity.doctor.deep` | Core | Run deep Doctor after plain Doctor; require warning-free result. | Deep report and all selected probes. |
| `picorv32-published-demo-continuity.doctor.plain-recheck` | Core | Run final plain Doctor; require warning-free result before baseline. | Recheck report; warning-free Doctor recovery point. |

## Clean published continuity

Authority: [accepted resolution, Prove clean published continuity](https://github.com/boldaxolotl/booley/issues/374#issuecomment-5570739734). Execute in the ordered journey below; subsequent phases depend on their required preceding state.

| Check ID | Selection | Stimulus and expected observation | Evidence / capture point |
|---|---|---|---|
| `picorv32-published-demo-continuity.baseline.source-unchanged` | Core | Build firmware and run continuity with both pinned source trees byte-identical; source changes begin only in later authorized disposable exercises. | Pre/post tree identities and diff around the complete baseline. |
| `picorv32-published-demo-continuity.baseline.firmware` | Core | Prepare demo firmware with recorded RISC-V compiler/image identity. | Firmware build command/log and image hashes; retain xPack GCC 15.2 calibration context. |
| `picorv32-published-demo-continuity.baseline.targets` | Core | Inventory the configured Targets before invoking them. | Identity-bound inventory and exact chosen Target selectors. |
| `picorv32-published-demo-continuity.baseline.icarus-main-core` | Core | Run the pinned unchanged main-core Icarus HDL simulation and require pass. | Normalized grade, actual simulator identity, logs and fresh simulation artifacts. |
| `picorv32-published-demo-continuity.baseline.icarus-axi` | Core | Run the pinned unchanged axi Icarus HDL simulation and require pass. | Normalized grade, actual simulator identity, logs and fresh simulation artifacts. |
| `picorv32-published-demo-continuity.baseline.icarus-wishbone` | Core | Run the pinned unchanged wishbone Icarus HDL simulation and require pass. | Normalized grade, actual simulator identity, logs and fresh simulation artifacts. |
| `picorv32-published-demo-continuity.baseline.icarus-dhrystone` | Core | Run the pinned unchanged dhrystone Icarus HDL simulation and require pass. | Normalized grade, actual simulator identity, logs and fresh simulation artifacts. |
| `picorv32-published-demo-continuity.baseline.verilator-lint` | Core | Run Verilator lint against the clean published demo; require its passing grade. | Target/EDA identity and lint report. |
| `picorv32-published-demo-continuity.baseline.physical-synthesis` | Core | Run physical sv2v/Yosys/OpenROAD synthesis; preserve physical implementation evidence. | Fresh synthesis/physical reports, metrics, artifacts and tool identities; edit-free continuity recovery point. |

## Linux provisioned Vivado administration

Authority: [accepted resolution, Exercise Linux Vivado administration](https://github.com/boldaxolotl/booley/issues/374#issuecomment-5570739734). Execute in the ordered journey below; subsequent phases depend on their required preceding state.

| Check ID | Selection | Stimulus and expected observation | Evidence / capture point |
|---|---|---|---|
| `picorv32-published-demo-continuity.vivado.version` | Linux Vivado | Use exact Vivado 2025.2 x86-64 on canonical Linux. | Version probes tied to registered installation. |
| `picorv32-published-demo-continuity.vivado.registration` | Linux Vivado | Create Installation Registration for borrowed installation. | Registration state and ledger ownership. |
| `picorv32-published-demo-continuity.vivado.project-grant` | Linux Vivado | Create Grant for the exact Project root. | Exact canonical root and resulting Grant. |
| `picorv32-published-demo-continuity.vivado.readonly-mount` | Linux Vivado | Expose borrowed installation as a read-only mount inside the Session Runtime. | Runtime mount metadata and read-only evidence. |
| `picorv32-published-demo-continuity.vivado.license-create` | Core | Create License Profile. | Created identity and nonsecret metadata. |
| `picorv32-published-demo-continuity.vivado.license-read` | Core | Read back License Profile state. | Observed identity/configuration. |
| `picorv32-published-demo-continuity.vivado.license-update` | Core | Update the run-owned License Profile. | Before/after configuration and command result. |
| `picorv32-published-demo-continuity.vivado.license-attach` | Linux Vivado | Attach the License Profile for runtime use. | Association and runtime connectivity evidence. |
| `picorv32-published-demo-continuity.vivado.relay-fault` | Linux Vivado | Inject controlled relay failure and observe expected failure. | Baseline connectivity, fault action and failure diagnostic before recovery. |
| `picorv32-published-demo-continuity.vivado.relay-recovery` | Linux Vivado | Restore relay and prove fresh successful operation. | Restoration action and post-restoration connectivity/artifact evidence. |
| `picorv32-published-demo-continuity.vivado.implementation` | Linux Vivado | Run implementation and require a fresh successful artifact. | Vivado grade and artifact identity/freshness. |
| `picorv32-published-demo-continuity.vivado.license-delete` | Core | Delete run-owned License Profile after dependents are released. | Absence proof and cleanup ledger entry. |

## Icarus B-Wave consumer matrix

Authority: [accepted resolution, Exercise B-Wave; Taxi ownership clarification](https://github.com/boldaxolotl/booley/issues/374#issuecomment-5570739734). Execute in the ordered journey below; subsequent phases depend on their required preceding state.

| Check ID | Selection | Stimulus and expected observation | Evidence / capture point |
|---|---|---|---|
| `picorv32-published-demo-continuity.bwave.semantic-wave` | Core | Invoke `wave` on PicoRV32 Icarus trace and verify signal/time semantics; CLI success alone is insufficient. | Exact query, trace identity, expected relationship and observed data. |
| `picorv32-published-demo-continuity.bwave.semantic-find` | Core | Invoke `find` on PicoRV32 Icarus trace and verify signal/time semantics; CLI success alone is insufficient. | Exact query, trace identity, expected relationship and observed data. |
| `picorv32-published-demo-continuity.bwave.semantic-sample` | Core | Invoke `sample` on PicoRV32 Icarus trace and verify signal/time semantics; CLI success alone is insufficient. | Exact query, trace identity, expected relationship and observed data. |
| `picorv32-published-demo-continuity.bwave.semantic-distance` | Core | Invoke `distance` on PicoRV32 Icarus trace and verify signal/time semantics; CLI success alone is insufficient. | Exact query, trace identity, expected relationship and observed data. |
| `picorv32-published-demo-continuity.bwave.semantic-value` | Core | Invoke `value` on PicoRV32 Icarus trace and verify signal/time semantics; CLI success alone is insufficient. | Exact query, trace identity, expected relationship and observed data. |
| `picorv32-published-demo-continuity.bwave.rejection-list` | Core | Exercise the agreed virtual-signal option matrix: `list` rejects the unsupported form with parser exit 2. Do not misread this as ordinary command rejection. | Exact invalid query, rejection diagnostic and exit status; literal `--virtual` conjunction and rejection forms are fixed in the amendment below. |
| `picorv32-published-demo-continuity.bwave.rejection-signal` | Core | Exercise the agreed virtual-signal option matrix: `signal` rejects the unsupported form with parser exit 2. Do not misread this as ordinary command rejection. | Exact invalid query, rejection diagnostic and exit status; literal `--virtual` conjunction and rejection forms are fixed in the amendment below. |
| `picorv32-published-demo-continuity.bwave.rejection-diff` | Core | Exercise the agreed virtual-signal option matrix: `diff` rejects the unsupported form with parser exit 2. Do not misread this as ordinary command rejection. | Exact invalid query, rejection diagnostic and exit status; literal `--virtual` conjunction and rejection forms are fixed in the amendment below. |
| `picorv32-published-demo-continuity.bwave.rejection-stats` | Core | Exercise the agreed virtual-signal option matrix: `stats` rejects the unsupported form with parser exit 2. Do not misread this as ordinary command rejection. | Exact invalid query, rejection diagnostic and exit status; literal `--virtual` conjunction and rejection forms are fixed in the amendment below. |
| `picorv32-published-demo-continuity.bwave.rejection-stuck` | Core | Exercise the agreed virtual-signal option matrix: `stuck` rejects the unsupported form with parser exit 2. Do not misread this as ordinary command rejection. | Exact invalid query, rejection diagnostic and exit status; literal `--virtual` conjunction and rejection forms are fixed in the amendment below. |
| `picorv32-published-demo-continuity.bwave.defect-preservation` | Core | Known defects in `sample`, `value`, `signal` or `diff` remain blocking Findings; do not relabel as expected behavior. | Original observation, public authority and scoped Finding linked to failing check. |

## Interactive fault, diagnosis, repair and restoration

Authority: [accepted resolution, Interactive Mode contract](https://github.com/boldaxolotl/booley/issues/374#issuecomment-5570739734). Execute in the ordered journey below; subsequent phases depend on their required preceding state.

| Check ID | Selection | Stimulus and expected observation | Evidence / capture point |
|---|---|---|---|
| `picorv32-published-demo-continuity.interactive.child-context` | Core | Launch one long-lived child inside the same Session Runtime with Project cwd, PTY, runtime identity and MCP access preserved; direct inherited child or conforming `booley session enter -- booley` is allowed. | Child/runtime/client/backend identities and launch context; no generic outer subagent substitution. |
| `picorv32-published-demo-continuity.interactive.readiness` | Core | Send the exact first prompt from source annex; child confirms runtime identity, cleanliness, Doctor and Targets without source edits. | Exact prompt/hash, durable child log and state evidence; pre-fault recovery point. |
| `picorv32-published-demo-continuity.interactive.wishbone-baseline` | Core | Child runs traced Wishbone simulation before injection and inspects artifacts with B-Wave. | Fresh passing trace, relevant queries/results and readiness report. |
| `picorv32-published-demo-continuity.interactive.inject` | Core | Operator changes only OR reduction of `mem_wstrb[3:0]` deriving `we` to AND inside `picorv32_wb` in `picorv32.v`; hide mutation/location from next prompt. | Exact patch, prior tree, operator authority, prompt and access separation. |
| `picorv32-published-demo-continuity.interactive.reproduce` | Core | Send exact second prompt to the same child; reproduce failing ordinary byte/halfword store behavior, preserving full-word behavior. | Failed run, early byte-store `ERROR` signature and fresh nonempty trace/VCD. |
| `picorv32-published-demo-continuity.interactive.trace-observability` | Core | Trace exposes `mem_wstrb`, `we`, `wbm_we_o`, `wbm_sel_o`, `wbm_stb_o`, `wbm_cyc_o`, `wbm_ack_i`, `mem_valid`, `mem_ready`, `ram_we`. | Signal inventory and identity-bound trace. |
| `picorv32-published-demo-continuity.interactive.bwave-diagnosis` | Core | Child diagnoses violated relationship using B-Wave, not hard-coded timestamps. | Queries, values and causal explanation grounded in relationships. |
| `picorv32-published-demo-continuity.interactive.repair` | Core | Child repairs root cause without weakening tests, removing stimulus, waivers or unrelated edits. | Patch and scope/test comparison. |
| `picorv32-published-demo-continuity.interactive.rerun-simulation` | Core | Child reruns relevant simulation after repair; require fresh passing result. | New simulation verdict and artifacts. |
| `picorv32-published-demo-continuity.interactive.rerun-lint` | Core | Child reruns relevant lint Target; require clean result. | Fresh lint result and Target identity. |
| `picorv32-published-demo-continuity.interactive.local-commit` | Core | Child commits repair locally; remote push remains blocked. | Commit identity, branch state and push-authority enforcement evidence. |
| `picorv32-published-demo-continuity.interactive.restore` | Core | Restore clean pre-exercise checkpoint and discard all Interactive changes. | Tree/branch comparison and removal proof; restored post-Interactive recovery point. |
| `picorv32-published-demo-continuity.interactive.supported-client` | GUI | When claiming actual supported VS Code integration, exercise this journey through that client and verify runtime attachment/context and MCP operations. | Actual client/backend/version, attachment evidence and durable client operation logs; headless child is insufficient. |

## Create both Tickets and preserve authority

Authority: [accepted resolution, Ticket Create invocations](https://github.com/boldaxolotl/booley/issues/374#issuecomment-5570739734). Execute in the ordered journey below; subsequent phases depend on their required preceding state.

| Check ID | Selection | Stimulus and expected observation | Evidence / capture point |
|---|---|---|---|
| `picorv32-published-demo-continuity.create.dhrystone-self-checking-cycle-contract.payload` | Core | Invoke Ticket Create skill for `dhrystone-self-checking-cycle-contract` with complete literal annex field set and `--agent --no-confirm`, using provider spelling. | Exact invocation, full prompt hash and payload; this is a skill invocation, not ordinary CLI. |
| `picorv32-published-demo-continuity.create.dhrystone-self-checking-cycle-contract.board` | Core | Creation returns board path and `queued` state for `dhrystone-self-checking-cycle-contract`. | Returned path, parsed ticket and board state. |
| `picorv32-published-demo-continuity.create.dhrystone-self-checking-cycle-contract.basis` | Core | Enqueue of `dhrystone-self-checking-cycle-contract` automatically publishes immutable Acceptance Basis; no manual seal or second confirmation. | Published Basis receipt/identity. |
| `picorv32-published-demo-continuity.create.rv32-zbb-pcpi.payload` | Core | Invoke Ticket Create skill for `rv32-zbb-pcpi` with complete literal annex field set and `--agent --no-confirm`, using provider spelling. | Exact invocation, full prompt hash and payload; this is a skill invocation, not ordinary CLI. |
| `picorv32-published-demo-continuity.create.rv32-zbb-pcpi.board` | Core | Creation returns board path and `waiting` state for `rv32-zbb-pcpi`. | Returned path, parsed ticket and board state. |
| `picorv32-published-demo-continuity.create.rv32-zbb-pcpi.basis` | Core | Enqueue of `rv32-zbb-pcpi` automatically publishes immutable Acceptance Basis; no manual seal or second confirmation. | Published Basis receipt/identity. |
| `picorv32-published-demo-continuity.create.barrier` | Core | Create both Tickets before running either; Ticket 2 declares Ticket 1 dependency. | Ordered creation/run records; two-Ticket creation recovery point. |
| `picorv32-published-demo-continuity.create.authoring-boundary` | Core | Ticket creation authors only approved Target definitions, owned `tests.toml` tables and empty `[new]` placeholders; Developer authors implementation. | Before/after source and configuration diff. |
| `picorv32-published-demo-continuity.retry.exact-allowance` | Core | At most one automatic retry with `max_attempts: 1`, only exact `API Error: Response stalled mid-stream`; crashes, design failures, timeouts, context or usage failures are not retried. | All attempts, exact error signature and retry decisions; retain original observations. |

## Dhrystone verification and Acceptance Basis dependency

Authority: [accepted resolution, Ticket 1; Acceptance Basis and dependency behavior](https://github.com/boldaxolotl/booley/issues/374#issuecomment-5570739734). Execute in the ordered journey below; subsequent phases depend on their required preceding state.

| Check ID | Selection | Stimulus and expected observation | Evidence / capture point |
|---|---|---|---|
| `picorv32-published-demo-continuity.ticket1.scope` | Core | Run `booley run --ticket dhrystone-self-checking-cycle-contract`; verification changes only `dhrystone/dhry_1.c` and `dhrystone/testbench.v`. | Exact call and complete diff. |
| `picorv32-published-demo-continuity.ticket1.fixed-iterations` | Core | Keep fixed 100-iteration demo. | Firmware code and execution evidence. |
| `picorv32-published-demo-continuity.ticket1.firmware-validation` | Core | Validate deterministic final Dhrystone results before success/cycle output; mismatch prints error and traps. | Implementation and execution log tied to expected values. |
| `picorv32-published-demo-continuity.ticket1.success-magic` | Core | Preserve `123456789` write to MMIO `0x20000000`; testbench passes only validated success. | Firmware/testbench evidence and passing run ordering. |
| `picorv32-published-demo-continuity.ticket1.cycle-record` | Core | Emit exactly `[SIM_CYCLES] dhry <User_Time>` after validation with deterministic timeout. | Output record ordering, count and timeout definition. |
| `picorv32-published-demo-continuity.ticket1.elaboration` | Core | Require `elab_pass: [sim_dhry_checked]`. | Normalized Elaboration Check and Target identity. |
| `picorv32-published-demo-continuity.ticket1.simulation` | Core | Require `dhrystone/testbench.v @ sim_dhry_checked @ dhry @ pass -> pass`. | Acceptance Basis/candidate evidence and test verdict. |
| `picorv32-published-demo-continuity.ticket1.cycle-cap` | Core | Require `cycle_count_max: 110000` inclusive for Target `sim_dhry_checked`, test `dhry`; no baseline count needed. Calibration is xPack GCC 15.2 `109734` cycles. | Parsed candidate cycle count, exact threshold and acceptance result. |
| `picorv32-published-demo-continuity.ticket1.tb-review` | Core | Require `review_tb_quality_done` bound to `sim_dhry_checked`. | Terminal review report and Target binding. |
| `picorv32-published-demo-continuity.ticket1.persistent-target` | Core | Target Plan uses persistent `sim_dhry_checked`; register `[sim_dhry_checked] dhry`; remains selectable after acceptance. | Created definitions/test table and post-acceptance fresh inventory. |
| `picorv32-published-demo-continuity.guard.negative-result` | Core | From disposable post-Ticket-1 recovery point corrupt one expected result; require deterministic failure. | Exact corruption, failing run and deterministic diagnostic. |
| `picorv32-published-demo-continuity.guard.no-success` | Core | Corrupted run emits no success magic. | Complete negative-run output/MMIO evidence. |
| `picorv32-published-demo-continuity.guard.no-cycles` | Core | Corrupted run emits no `[SIM_CYCLES]` record. | Complete negative-run output. |
| `picorv32-published-demo-continuity.guard.restore-rerun` | Core | Restore original expected result and rerun successfully. | Restored hashes, new passing result; Dhrystone guard recovery point. |
| `picorv32-published-demo-continuity.guard.discard` | Core | Discard guard disposable branch after evidence is retained. | Branch/worktree removal and resource reconciliation. |
| `picorv32-published-demo-continuity.refresh.old-basis` | Core | Retain waiting Ticket 2 original Basis before provider acceptance. | Original immutable Basis and waiting state. |
| `picorv32-published-demo-continuity.refresh.provider-surface` | Core | After Ticket 1 acceptance automatic pre-execution Basis Refresh includes accepted Dhrystone source and persistent Target. | Old/new Basis IDs and source/Target comparison. |
| `picorv32-published-demo-continuity.refresh.queue-transition` | Core | After refreshed provider verification atomically promote waiting to queue without user approval. | Board transition and published receipt ordering; Basis Refresh recovery point. |
| `picorv32-published-demo-continuity.refresh.authored-drift` | Core | Authored-input drift blocks for return-to-draft instead of changing authority. | Controlled drift and observed state/diagnostic; see gap list for fixture isolation. |
| `picorv32-published-demo-continuity.refresh.incompatible-provider` | Core | Incompatible provider surface blocks instead of silently refreshing authority. | Controlled incompatibility and block evidence; restore waiting state before real Ticket 2. |

## Zbb feature contract and Criteria

Authority: [accepted resolution, Ticket 2](https://github.com/boldaxolotl/booley/issues/374#issuecomment-5570739734). Execute in the ordered journey below; subsequent phases depend on their required preceding state.

| Check ID | Selection | Stimulus and expected observation | Evidence / capture point |
|---|---|---|---|
| `picorv32-published-demo-continuity.ticket2.scope` | Core | Run separate `booley run --ticket rv32-zbb-pcpi`; type feature, dependency Ticket 1; exact Scope `picorv32.v`, `testbench.v`, `testbench_wb.v`, `Makefile`, `tests/zbb.S [new]`. | Invocation, parsed Ticket and complete diff. |
| `picorv32-published-demo-continuity.ticket2.isa-authority` | Core | Use `/opt/riscv-docs/riscv-isa-manual.html` as technical authority. | Consulted document identity and ISA behavior evaluation. |
| `picorv32-published-demo-continuity.ticket2.pcpi-timing` | Core | Use internal registered PCPI implementation with fixed one-cycle response. | RTL and timing simulation evidence. |
| `picorv32-published-demo-continuity.ticket2.enable-default` | Core | Add `ENABLE_ZBB` default 0 across core, AXI and Wishbone wrappers. | Each wrapper declaration plus disabled/default execution evidence. |
| `picorv32-published-demo-continuity.ticket2.disabled-trap` | Core | Arm distinct MMIO marker immediately before first Zbb encoding; require ensuing illegal-instruction trap; unrelated trap cannot pass. | Instruction/marker/trap sequence and exact disabled test oracle. |
| `picorv32-published-demo-continuity.ticket2.zbb-andn` | Core | Implement and verify RV32 Zbb `ANDN` against pinned ISA authority. | Instruction stimulus, expected result and observed execution for enabled configuration. |
| `picorv32-published-demo-continuity.ticket2.zbb-orn` | Core | Implement and verify RV32 Zbb `ORN` against pinned ISA authority. | Instruction stimulus, expected result and observed execution for enabled configuration. |
| `picorv32-published-demo-continuity.ticket2.zbb-xnor` | Core | Implement and verify RV32 Zbb `XNOR` against pinned ISA authority. | Instruction stimulus, expected result and observed execution for enabled configuration. |
| `picorv32-published-demo-continuity.ticket2.zbb-clz` | Core | Implement and verify RV32 Zbb `CLZ` against pinned ISA authority. | Instruction stimulus, expected result and observed execution for enabled configuration. |
| `picorv32-published-demo-continuity.ticket2.zbb-ctz` | Core | Implement and verify RV32 Zbb `CTZ` against pinned ISA authority. | Instruction stimulus, expected result and observed execution for enabled configuration. |
| `picorv32-published-demo-continuity.ticket2.zbb-cpop` | Core | Implement and verify RV32 Zbb `CPOP` against pinned ISA authority. | Instruction stimulus, expected result and observed execution for enabled configuration. |
| `picorv32-published-demo-continuity.ticket2.zbb-min` | Core | Implement and verify RV32 Zbb `MIN` against pinned ISA authority. | Instruction stimulus, expected result and observed execution for enabled configuration. |
| `picorv32-published-demo-continuity.ticket2.zbb-minu` | Core | Implement and verify RV32 Zbb `MINU` against pinned ISA authority. | Instruction stimulus, expected result and observed execution for enabled configuration. |
| `picorv32-published-demo-continuity.ticket2.zbb-max` | Core | Implement and verify RV32 Zbb `MAX` against pinned ISA authority. | Instruction stimulus, expected result and observed execution for enabled configuration. |
| `picorv32-published-demo-continuity.ticket2.zbb-maxu` | Core | Implement and verify RV32 Zbb `MAXU` against pinned ISA authority. | Instruction stimulus, expected result and observed execution for enabled configuration. |
| `picorv32-published-demo-continuity.ticket2.zbb-sext-b` | Core | Implement and verify RV32 Zbb `SEXT.B` against pinned ISA authority. | Instruction stimulus, expected result and observed execution for enabled configuration. |
| `picorv32-published-demo-continuity.ticket2.zbb-sext-h` | Core | Implement and verify RV32 Zbb `SEXT.H` against pinned ISA authority. | Instruction stimulus, expected result and observed execution for enabled configuration. |
| `picorv32-published-demo-continuity.ticket2.zbb-zext-h` | Core | Implement and verify RV32 Zbb `ZEXT.H` against pinned ISA authority. | Instruction stimulus, expected result and observed execution for enabled configuration. |
| `picorv32-published-demo-continuity.ticket2.zbb-rol` | Core | Implement and verify RV32 Zbb `ROL` against pinned ISA authority. | Instruction stimulus, expected result and observed execution for enabled configuration. |
| `picorv32-published-demo-continuity.ticket2.zbb-ror` | Core | Implement and verify RV32 Zbb `ROR` against pinned ISA authority. | Instruction stimulus, expected result and observed execution for enabled configuration. |
| `picorv32-published-demo-continuity.ticket2.zbb-rori` | Core | Implement and verify RV32 Zbb `RORI` against pinned ISA authority. | Instruction stimulus, expected result and observed execution for enabled configuration. |
| `picorv32-published-demo-continuity.ticket2.zbb-orc-b` | Core | Implement and verify RV32 Zbb `ORC.B` against pinned ISA authority. | Instruction stimulus, expected result and observed execution for enabled configuration. |
| `picorv32-published-demo-continuity.ticket2.zbb-rev8` | Core | Implement and verify RV32 Zbb `REV8` against pinned ISA authority. | Instruction stimulus, expected result and observed execution for enabled configuration. |
| `picorv32-published-demo-continuity.ticket2.enabled-core` | Core | Execute enabled Zbb through core wrapper. | Test manifest and fresh wrapper-specific simulation evidence. |
| `picorv32-published-demo-continuity.ticket2.enabled-axi` | Core | Execute enabled Zbb through axi wrapper. | Test manifest and fresh wrapper-specific simulation evidence. |
| `picorv32-published-demo-continuity.ticket2.enabled-wishbone` | Core | Execute enabled Zbb through wishbone wrapper. | Test manifest and fresh wrapper-specific simulation evidence. |
| `picorv32-published-demo-continuity.ticket2.elab-sim_core_zbb` | Core | Require `elab_pass` for `sim_core_zbb`. | Target-bound normalized Elaboration Check. |
| `picorv32-published-demo-continuity.ticket2.sim-sim_core_zbb` | Core | Require `sim_pass` fail-to-pass for `sim_core_zbb`. | Baseline failure and candidate pass with test identity. |
| `picorv32-published-demo-continuity.ticket2.elab-sim_wb_zbb` | Core | Require `elab_pass` for `sim_wb_zbb`. | Target-bound normalized Elaboration Check. |
| `picorv32-published-demo-continuity.ticket2.sim-sim_wb_zbb` | Core | Require `sim_pass` fail-to-pass for `sim_wb_zbb`. | Baseline failure and candidate pass with test identity. |
| `picorv32-published-demo-continuity.ticket2.elab-sim_zbb_disabled` | Core | Require `elab_pass` for `sim_zbb_disabled`. | Target-bound normalized Elaboration Check. |
| `picorv32-published-demo-continuity.ticket2.sim-sim_zbb_disabled` | Core | Require `sim_pass` fail-to-pass for `sim_zbb_disabled`. | Baseline failure and candidate pass with test identity. |
| `picorv32-published-demo-continuity.ticket2.regression-main-core` | Core | Require existing default main-core simulation pass-to-pass. | Acceptance Basis and candidate simulation evidence. |
| `picorv32-published-demo-continuity.ticket2.regression-axi` | Core | Require existing default axi simulation pass-to-pass. | Acceptance Basis and candidate simulation evidence. |
| `picorv32-published-demo-continuity.ticket2.regression-wishbone` | Core | Require existing default wishbone simulation pass-to-pass. | Acceptance Basis and candidate simulation evidence. |
| `picorv32-published-demo-continuity.ticket2.provider-execution` | Core | Genuinely execute refreshed `sim_dhry_checked` pass-to-pass, not merely select its definition. | Actual Flow invocation and simulator artifact/test result. |
| `picorv32-published-demo-continuity.ticket2.standalone` | Core | Require `elaborate_standalone: true`. | Standalone elaboration evidence. |
| `picorv32-published-demo-continuity.ticket2.lint` | Core | Require clean `lint_core_zbb` and preserve default lint behavior. | Candidate/default Target lint results. |
| `picorv32-published-demo-continuity.ticket2.mutation` | Core | Mutate `picorv32.v` via `sim_core_zbb`; at least 14 detected out of 15, targeting Zbb decode/results/PCPI/enable gating. | Campaign manifest, each mutant/outcome, restoration and threshold evidence. |
| `picorv32-published-demo-continuity.ticket2.synthesis-cell` | Core | Directed pair baseline `synth_core`, candidate `synth_core_zbb`; cell increase at most 11%, measured with Zbb enabled. | Fresh paired metrics and exact inclusive comparison. |
| `picorv32-published-demo-continuity.ticket2.synthesis-timing` | Core | Same physical synthesis pair: critical-path increase at most 3%; no relaxed limit. | Paired critical-path metrics and comparison. |
| `picorv32-published-demo-continuity.ticket2.fpga` | Linux Vivado | Require `fpga_impl_ok` for `fpga_core_zbb`: successful completion and fresh artifact; no LUT threshold. | Actual implementation verdict and artifact identity. |
| `picorv32-published-demo-continuity.ticket2.review-bugs` | Core | Require terminal `review_rtl_bugs_done` (advisory completion, not clean disposition). | Focus-specific terminal report and criterion result. |
| `picorv32-published-demo-continuity.ticket2.review-protocol` | Core | Require terminal `review_rtl_protocol_done` (advisory completion, not clean disposition). | Focus-specific terminal report and criterion result. |
| `picorv32-published-demo-continuity.ticket2.review-spec` | Core | Require terminal `review_rtl_spec_done` (advisory completion, not clean disposition). | Focus-specific terminal report and criterion result. |
| `picorv32-published-demo-continuity.ticket2.review-code_style` | Core | Require terminal `review_rtl_code_style_done` (advisory completion, not clean disposition). | Focus-specific terminal report and criterion result. |
| `picorv32-published-demo-continuity.ticket2.review-optimization` | Core | Require terminal `review_rtl_optimization_done` (advisory completion, not clean disposition). | Focus-specific terminal report and criterion result. |
| `picorv32-published-demo-continuity.ticket2.review-security` | Core | Require terminal `review_rtl_security_done` (advisory completion, not clean disposition). | Focus-specific terminal report and criterion result. |
| `picorv32-published-demo-continuity.ticket2.tb-review` | Core | Require Target-bound `review_tb_quality_done`. | Terminal TB-quality report and binding. |
| `picorv32-published-demo-continuity.ticket2.bugs-clean` | Core | Also require corrective `review_rtl_bugs_clean`. | Clean disposition after any required corrective work. |
| `picorv32-published-demo-continuity.ticket2.ephemeral-retention` | Core | Six approved ephemeral Targets remain available during execution and acceptance: `sim_core_zbb`, `sim_wb_zbb`, `sim_zbb_disabled`, `lint_core_zbb`, `synth_core_zbb`, `fpga_core_zbb`. | Target Plan and inventory at execution/acceptance. |
| `picorv32-published-demo-continuity.ticket2.ephemeral-removal` | Core | After acceptance remove those definitions and unambiguously owned test tables automatically; no retired `on_success.remove_targets`. | Before/after definitions/test tables and retained unrelated content. |

## Lifecycle and final regression

Authority: [accepted resolution, Ordered scenario; Criteria; cleanup](https://github.com/boldaxolotl/booley/issues/374#issuecomment-5570739734). Execute in the ordered journey below; subsequent phases depend on their required preceding state.

| Check ID | Selection | Stimulus and expected observation | Evidence / capture point |
|---|---|---|---|
| `picorv32-published-demo-continuity.ticket1.done` | Core | Reach `destination: done`. | Board terminal state and acceptance evidence. |
| `picorv32-published-demo-continuity.ticket1.merge` | Core | Merge accepted local changes. | Accepted commit and destination branch identity. |
| `picorv32-published-demo-continuity.ticket1.workspace-cleanup` | Core | Clean Ticket worktree after acceptance. | Absence proof and resource entry. |
| `picorv32-published-demo-continuity.ticket1.triage-report` | Core | Generate triage report. | Fresh report artifact and ticket association. |
| `picorv32-published-demo-continuity.ticket2.done` | Core | Reach `destination: done`. | Board terminal state and acceptance evidence. |
| `picorv32-published-demo-continuity.ticket2.merge` | Core | Merge accepted local changes. | Accepted commit and destination branch identity. |
| `picorv32-published-demo-continuity.ticket2.workspace-cleanup` | Core | Clean Ticket worktree after acceptance. | Absence proof and resource entry. |
| `picorv32-published-demo-continuity.ticket2.triage-report` | Core | Generate triage report. | Fresh report artifact and ticket association. |
| `picorv32-published-demo-continuity.final.combined-regression` | Core | Execute full combined supported regression against merged result; retain all applicable simulation, lint and physical artifacts. | Fresh test manifest/results and merged identity; final regression recovery point. |
| `picorv32-published-demo-continuity.coverage.criterion-families` | Core | Across Tickets account for all 15 agreed Criterion families; Linux includes FPGA, Windows explicitly excludes provisioned FPGA. | Generated reverse coverage view referencing actual criterion/check results, never a substitute for those results. |

## Cleanup and durable evidence

Authority: [accepted resolution, Cleanup; evidence and checkpoints](https://github.com/boldaxolotl/booley/issues/374#issuecomment-5570739734). Execute in the ordered journey below; subsequent phases depend on their required preceding state.

| Check ID | Selection | Stimulus and expected observation | Evidence / capture point |
|---|---|---|---|
| `picorv32-published-demo-continuity.cleanup.branches` | Core | Reconcile and remove every run-owned branches in dependency order. | Owned identity, actual cleanup action and independent absence/state proof. |
| `picorv32-published-demo-continuity.cleanup.worktrees` | Core | Reconcile and remove every run-owned worktrees in dependency order. | Owned identity, actual cleanup action and independent absence/state proof. |
| `picorv32-published-demo-continuity.cleanup.targets` | Core | Reconcile and remove every run-owned Targets in dependency order. | Owned identity, actual cleanup action and independent absence/state proof. |
| `picorv32-published-demo-continuity.cleanup.projections` | Core | Reconcile and remove every run-owned projections in dependency order. | Owned identity, actual cleanup action and independent absence/state proof. |
| `picorv32-published-demo-continuity.cleanup.hooks` | Core | Reconcile and remove every run-owned hooks in dependency order. | Owned identity, actual cleanup action and independent absence/state proof. |
| `picorv32-published-demo-continuity.cleanup.runtimes` | Core | Reconcile and remove every run-owned runtimes in dependency order. | Owned identity, actual cleanup action and independent absence/state proof. |
| `picorv32-published-demo-continuity.cleanup.processes` | Core | Reconcile and remove every run-owned processes in dependency order. | Owned identity, actual cleanup action and independent absence/state proof. |
| `picorv32-published-demo-continuity.cleanup.project-grants` | Linux Vivado | Reconcile and remove every run-owned Project-Grants in dependency order. | Owned identity, actual cleanup action and independent absence/state proof. |
| `picorv32-published-demo-continuity.cleanup.license-profiles` | Core | Reconcile and remove every run-owned License-Profiles in dependency order. | Owned identity, actual cleanup action and independent absence/state proof. |
| `picorv32-published-demo-continuity.cleanup.relays` | Linux Vivado | Reconcile and remove every run-owned relays in dependency order. | Owned identity, actual cleanup action and independent absence/state proof. |
| `picorv32-published-demo-continuity.cleanup.installation-registrations` | Linux Vivado | Reconcile and remove every run-owned Installation-Registrations in dependency order. | Owned identity, actual cleanup action and independent absence/state proof. |
| `picorv32-published-demo-continuity.cleanup.mounts` | Core | Reconcile and remove every run-owned mounts in dependency order. | Owned identity, actual cleanup action and independent absence/state proof. |
| `picorv32-published-demo-continuity.cleanup.preserve-borrowed` | Core | Preserve borrowed Vivado installation, credentials, base images, caches and pre-existing host administration. | Before/after preserved resource identities; no secret values. |
| `picorv32-published-demo-continuity.cleanup.clean-pins` | Core | Require clean pinned repositories at final cleanup. | Tree/status against initial pins; accepted evolution commits retained as evidence before restoration. |
| `picorv32-published-demo-continuity.cleanup.remotes` | Core | Remotes remain unchanged and no branch is pushed. | Initial/final remote config/ref evidence and command authority log. |
| `picorv32-published-demo-continuity.evidence.finalize` | Core | Finalize record, Findings, recovery links and resource disposition with no secret values. | Compact run files and immutable external evidence; cleanup-complete recovery point. |

## Resolved profile and encoding requirements

- Preserve full Ubuntu/Windows Codex runs. The approved [Claude profile](profiles.md#approved-claude-compatibility-scope) uses both complete Ticket contracts and the Interactive exercise, excluding only duplicate supplemental host/stress probes. Optional Windows/Claude is fully specified but unexecuted. Windows excludes Linux provisioned Vivado; required Linux unavailability makes its profile incomplete.
- The virtual-signal option and deterministic conjunction oracle are concretized in the amendment below; ordinary B-Wave commands remain supported.
- The original missing explicit AXI Target is corrected below as seventh ephemeral `sim_axi_zbb`, with source-verified existing AXI top and explicit Criteria.
- The isolated authored-drift and missing-provider fixture cases below preserve the real successful two-Ticket sequence.
- Concrete Dhrystone one-bit expected-result corruption and literal sanitation input are specified below; their generated-code source span is captured when the accepted implementation exists.
- The [central GUI profile](profiles.md) supplies actual supported-client, WCP and qualified-observer prerequisites and evidence. The required mechanism remains unavailable; implementation/qualification of it is later work, and core cannot impersonate its claims.

## Approved eight-hour allocation

All work, including supplemental capability exercises, is inside these phase ceilings: preparation/Doctor 50m; clean baseline/Vivado 60m; Interactive 45m; Ticket creation 20m; Ticket1/negative/recovery 60m; Ticket2/final regression 210m; contingency 15m; cleanup 20m. Total 480m. Stop new work and begin cleanup at 7h40. Preserve existing per-command limits, additionally bounded by remaining phase/run time. Shorter ceilings do not reduce workload or authorize retry; feasibility remains unproven until execution.

## Concrete fixture and Target representation amendments

These are deterministic implementation-design choices for the accepted observations, not changes to live protected Project state. Every disposable clone, phase recovery point and trial artifact belongs to the current run and its existing phase ceiling. Use real published public paths; do not mock internals to claim product behavior.

### Enabled AXI Target

Add seventh ephemeral Target `sim_axi_zbb` to Ticket2 Target Plan. The [pinned `testbench.v`](https://github.com/YosysHQ/picorv32/blob/a473fc8fca393771d83b0ffcf0b14db3393339d8/testbench.v#L11) declares top `testbench`, instantiates `picorv32_wrapper`, and that wrapper instantiates `picorv32_axi` (line 163). Bind `sim_axi_zbb` to this existing AXI testbench top, approved Zbb firmware/test source, and `ENABLE_ZBB=1` propagated through the already-authorized `testbench.v` scope. Preserve the original existing-target AXI regression separately. Require `elab_pass` and explicit Zbb `fail -> pass` simulation for this Target. It follows the same ephemeral retention/removal rules as the original six Targets, so all seven remain until acceptance and are removed afterwards with their unambiguously owned test tables. This is a handoff representation correction for already-required enabled AXI behavior, exposed for final review.

| Check ID | Selection | Stimulus and expected observation | Evidence / capture point |
|---|---|---|---|
| `picorv32-published-demo-continuity.ticket2.elab-sim_axi_zbb` | Core | Elaborate the explicitly approved AXI Zbb Target using existing `testbench` top. | Target/source/parameter identity and normalized elaboration result. |
| `picorv32-published-demo-continuity.ticket2.sim-sim_axi_zbb` | Core | Run Zbb firmware through AXI Target and require Basis fail/candidate pass. | Test manifest including every agreed opcode, fresh Basis/candidate simulation and AXI wrapper identity. |

### Virtual-signal matrix

The public [Virtual signals reference](../../crates/bwave/docs/public/reference/virtual-signals.md) resolves the previously omitted option: `--virtual "name = expr"` is accepted on `wave`, `find`, `sample`, `distance`, `value`; rejected during parsing on `list`, `signal`, `diff`, `stats`, `stuck` (also `build`, outside this original five-command rejection allocation). Preserve the source matrix exactly.

On the required traced Wishbone baseline, bind `V` and `R` to the unique full hierarchical signal paths ending in `mem_valid` and `mem_ready` within the selected `picorv32_wb` instance. Zero or ambiguous matches block the fixture. Define `qa_hsk = *<V> & *<R>`, using those literal resolved paths. Compute the expected Boolean sequence independently from stored V/R rows at the same selected sampling instants. For `wave`/`value`, select `qa_hsk` and compare every returned row/value with the conjunction; for `find qa_hsk 1`, compare the complete matched sample set; for `sample qa_hsk 1 -s <V> -s <R>`, require both stored bits 1 at every expected match; for `distance qa_hsk 1`, compare consecutive matched sample-index differences. Freeze exact resolved command strings, clock/reset/sampling selection and oracle sequence in producing-step evidence. Each unsupported command uses its otherwise-valid canonical invocation against that same FST with the same `--virtual` argument appended; require exit 2 identifying the unrecognized option, not an unrelated missing positional argument. Known product defects remain failures.

### Dhrystone negative guard

After accepted Ticket1, make a disposable branch at the accepted source. Identify the first executed equality comparison in the newly added deterministic final-result guard, in lexical source order. Change only its expected integer operand from N to `N ^ 1` (one-bit corruption); record exact original/replacement span and value. The clean accepted run establishes equality before this mutation. Rebuild/rerun unchanged iteration count and testbench; require deterministic mismatch/trap, no success magic, no cycle record. Restore the exact original bytes, rebuild, require pass, then discard this branch. If the implementation has no such explicit guard operand, its verifier must expose the equivalent deterministic expected-result assertion as reviewable fixture input before mutation; do not guess or weaken the guard. This adaptation addresses generated implementation syntax, not a license to change expected behavior.

### Stealth sanitation and attribution

The [published Stealth contract](../../docs/user/CONFIG.md) lists `booley` among built-in banned words and requires in-place redaction while preserving rationale, with attribution trailers removed. On a run-owned disposable empty commit, with the accepted Stealth settings and no custom banned-word override, supply exactly:

```text
fix(qa): verify booley message handling

Keep this rationale intact.
Checked the booley configuration.

Co-Authored-By: QA Fixture <qa-fixture@example.invalid>
```

Require `booley` absent from final subject/body; retained `Keep this rationale intact.` and retained rewritten configuration sentence; no `Co-Authored-By` or attribution address. Preserve original and stored commit messages plus hook diagnostics. Do not require a particular substitute token unless the tested published release documents it. Restore/discard the disposable commit after evidence capture.

### Basis Refresh rejection fixtures

Copy the two-Ticket creation-barrier Project and its paired Project repository into two separately owned disposable Projects before the real Ticket1 run; preserve valid original Basis/receipts and independent resource ownership. Replay the real Ticket1 accepted commit/provider state into each clone using public Project/git operations. Neither clone may mutate the live journey or alter immutable Basis documents by hand.

For **authored drift**, change only consumer Ticket2's approved `synthesis_ok` `cell_count_increase_at_most` from `11%` to `12%` after the Basis was published. Invoke the normal pre-execution waiting-ticket path. Require it to reject drift for return-to-draft, preserve the old Basis and not start the Developer. The 12% value is deliberately invalid fixture input, never an accepted relaxed threshold. Capture exact diff and diagnostic, then delete the clone.

For **missing/incompatible provider**, keep the consumer Ticket payload untouched; after integrating the accepted provider state into the second clone, remove only its exported `sim_dhry_checked` Target definition from the run-owned Project configuration and commit that fixture change. Invoke normal pre-execution refresh. Require blocking because the pinned provider's runnable Target/control surface no longer matches; do not require one exact diagnostic branch (`missing`, surface mismatch or invalid provider Basis are all concrete manifestations). Preserve old/new lookup evidence and prove no waiting-to-queue execution transition occurred. Delete the clone. The genuine journey performs its successful automatic refresh against the unchanged accepted provider.

### Cross-platform License Profile CRUD

License Profile creation, readback, update and deletion are core host-administration checks on both supported platforms. They are independent of Windows' exclusion from provisioned Vivado execution. Exact Vivado registration/grant/read-only runtime mount, attachment and license-relay fault/recovery retain their accepted canonical Linux allocation. Central profiles must select the CRUD rows and their setup/cleanup on Windows without claiming Windows Vivado execution.

## Exact accepted journey source annex

Source snapshot: [Design the PicoRV32 published-demo continuity scenario](https://github.com/boldaxolotl/booley/issues/374#issuecomment-5570739734). The shared-contract amendment and the approved handoff decisions take precedence over historical bookkeeping and budgets in this annex. Original workload text is retained for exact prompt/Ticket extraction.

## Accepted design

Scenario ID: `picorv32-published-demo-continuity`.

This scenario is a published-demo continuity test and a realistic evolution test. Its first phase proves the pinned public demo without source edits. Later, explicitly disposable Interactive and Ticket branches may change RTL, firmware, and HDL testbenches as required by their exercises. Those changes are authorized by the scenario contract, recorded in the run ledger, and either accepted through Ticket Mode or discarded at the named recovery checkpoint. This is the precise interpretation of “the expected green path must not edit upstream RTL or testbenches”: the continuity baseline is edit-free; the subsequent evolution exercises are intentionally source-changing.

### Frozen inputs and run declaration

Every run freezes and records:

- Project: `boldaxolotl/booley-prj-picorv32@b8fe2370cb9aa7d93617850169f42f07821865d6`.
- Upstream IP: `YosysHQ/picorv32@a473fc8fca393771d83b0ffcf0b14db3393339d8`.
- The exact published Booley release, package hash, release-documentation identity, and container-image digest selected for that run. `latest`, floating tags, editable installs, local wheels, development versions, and imports from a Booley source checkout are forbidden.
- The provider/platform profile, Scenario Run ID, eight-hour absolute deadline, allowed retry signature, granted host resources, and cleanup ledger.

The run uses a fresh checkout and a separate clean `.booley_project` repository. It enables Stealth with `ignore_native_cores = true`. Only hidden authored cores may be projected; ignored native projections must produce neither copied RTL nor symlinks. Exercise the controlled banned-word commit sanitation path and attribution-trailer removal, and retain hook/projection cleanup evidence.

The absolute run deadline is eight hours. Budget: preparation plus Doctor 60 minutes; clean baseline plus Vivado 60 minutes; Interactive Mode 45 minutes; both Ticket Create calls 20 minutes; Ticket 1 plus negative/recovery proof 60 minutes; Ticket 2 plus final regression four hours; cleanup 20 minutes; contingency 15 minutes. At 7h40, stop new work and begin cleanup. Do not extend the deadline or silently reduce coverage.

### Provider and platform profiles

Each lane is fresh and uses stable semantic Ticket IDs with a profile-specific executable contract:

| Profile | Required coverage |
|---|---|
| Ubuntu 24.04 x86-64 + Codex | Canonical full run: both modes, both Tickets, all applicable Criteria, B-Wave, host administration, physical synthesis, and provisioned Vivado. |
| Native Windows x86-64 + Docker Desktop/WSL2 + Codex | Both modes, both Tickets, and common Criteria. Vivado and `fpga_impl_ok` are `not runnable`, receive no coverage credit, and are never counted as pass/fail. |
| Ubuntu 24.04 x86-64 + Claude | Both modes and both Ticket lifecycles with the reduced provider-compatibility obligation centered on artifact-backed Icarus evidence. |
| Windows + Claude | Declared but unexecuted until usage permits; do not infer coverage from another profile. |

Every executed profile must exercise both Interactive Mode and Ticket Mode. Unsupported or unavailable profile obligations are recorded as `not runnable`, never as passes.

### Ordered scenario

1. **Prepare and freeze.** Create the Run Declaration, install the exact published Booley release, check out the two pinned repositories, create run-owned configuration and repositories, provision the selected host resources, initialize Booley, and build the demo firmware. Initialization and host preparation are setup, not product exercises.
2. **Doctor first.** Make Doctor the first Booley product exercise: plain Doctor, deep Doctor, then a plain recheck. Require warning-free results and record all reports.
3. **Prove clean published continuity.** With both pinned source trees clean and unchanged, run firmware preparation and Target inventory, then Icarus simulation of main/core, AXI, Wishbone, and Dhrystone; Verilator lint; and physical sv2v/Yosys/OpenROAD synthesis. Preserve logs and artifacts.
4. **Exercise Linux Vivado administration.** On the canonical Linux profile only, use exact Vivado 2025.2 x86-64. Cover Installation Registration, version probes, exact project-root Grant creation, read-only mount, License Profile CRUD and attachment, a controlled relay failure and recovery, and a fresh implementation artifact. Never modify the borrowed installation. Windows records this obligation as `not runnable`.
5. **Exercise B-Wave.** Use the PicoRV32 Icarus trace for the semantic consumers `wave`, `find`, `sample`, `distance`, and `value`. Verify exit-2 parser rejection for `list`, `signal`, `diff`, `stats`, and `stuck`. Existing defects in `sample`, `value`, `signal`, or `diff` remain real blocking Findings; they must not be reclassified as expected behavior. PicoRV32 owns Icarus-trace B-Wave coverage; Taxi owns the separate Verilator-trace obligation.
6. **Run the Interactive Mode exercise** described below and restore its checkpoint.
7. **Create both Tickets before running either.** Invoke Ticket Create Agent Mode twice from the scenario, with complete structured input and `--no-confirm`. Capture each exact call, prompt hash, returned board path, board state, and published Acceptance Basis receipt. Ticket 1 must be queued; Ticket 2 must be waiting on Ticket 1.
8. **Run Ticket 1.** Execute `booley run --ticket dhrystone-self-checking-cycle-contract` as its own call. Require `destination: done`, merge, cleanup, and triage-report generation.
9. **Prove the Dhrystone guard.** From a disposable post-Ticket-1 checkpoint, corrupt one expected result. Require a deterministic failure, no success magic, and no `[SIM_CYCLES]` record. Restore, rerun successfully, and discard the disposable branch.
10. **Refresh Ticket 2.** After Ticket 1 is accepted, require Booley’s automatic pre-execution Basis Refresh of the untouched waiting Ticket. Record the old and new Acceptance Basis IDs and verify that the accepted Dhrystone source and persistent Target are present before the waiting-to-queue transition. Any authored-input drift blocks for return-to-draft.
11. **Run Ticket 2.** Execute `booley run --ticket rv32-zbb-pcpi` as a second, separate call. Require `destination: done`, merge, cleanup, and triage-report generation.
12. **Final regression.** Run the complete combined supported regression against the merged result and preserve artifacts.
13. **Cleanup.** Reconcile the cleanup ledger and prove that pinned repositories are clean and remotes unchanged.

### Interactive Mode contract

The QA Scenario Operator launches one long-lived Interactive Mode agent as its child inside the same Booley Session Runtime, with the project working directory, PTY, runtime identity, and MCP access preserved. A conforming launch is `booley session enter -- booley` when the environment requires it; a direct inherited child is also valid when those invariants are demonstrably preserved. This is not a generic outer orchestration sub-agent.

First prompt, before fault injection:

> Work interactively in the current pinned PicoRV32 project. Confirm the Booley session identity, repository cleanliness, Doctor state, and available Targets. Run the traced Wishbone simulation, inspect its artifacts with B-Wave, and report the readiness checkpoint without changing project sources.

The operator then injects exactly one defect in `picorv32.v`, inside `picorv32_wb`: replace the OR reduction that derives `we` from `mem_wstrb[3:0]` with an AND reduction. This preserves full-word writes while breaking byte and halfword stores. The mutation and its location are hidden from the agent prompt.

Second prompt to the same child:

> The previously passing Wishbone scenario now fails during ordinary store/load behavior. Reproduce the failure, collect and inspect a fresh trace, use B-Wave to identify the violated signal relationship, diagnose and repair the root cause, then rerun the relevant simulation and lint Target. Commit the repair locally. Do not weaken tests, remove stimulus, add waivers, or push any branch.

The trace must expose `mem_wstrb`, `we`, `wbm_we_o`, `wbm_sel_o`, `wbm_stb_o`, `wbm_cyc_o`, `wbm_ack_i`, `mem_valid`, `mem_ready`, and `ram_we`. Diagnosis is based on signal relationships, not hard-coded timestamps. A useful deterministic signature is the byte-store `ERROR` path; the clean pinned run is known to complete successfully while the seeded run fails early and still produces a nonempty trace/VCD. Record reproduction, trace, B-Wave queries, diagnosis, patch, clean reruns, and commit. Block remote push, then restore the clean pre-exercise checkpoint and discard all Interactive Mode changes.

### Ticket Create invocations

The scenario itself is the source of both Tickets. Do not ask Ticket Create to infer missing semantics. Pass the complete field sets below in one creation phase:

- Codex form: `$booley-ticket-create --agent --no-confirm <complete structured scenario payload>`
- Claude form: `/booley-ticket-create --agent --no-confirm <complete structured scenario payload>`

These are skill invocations, not ordinary CLI commands. Enqueue automatically publishes the immutable Acceptance Basis; there is no manual seal, Target Contract, `base_sha`, or second confirmation. Ticket creation may author only the approved Target definitions, owned `tests.toml` tables, and empty `[new]` placeholders. The Developer Agent authors the implementation.

Both Tickets use:

```yaml
on_success:
  destination: done
  merge: true
  cleanup: true
  triage_report: true
priority: medium
```

Only one automatic retry is permitted, with `max_attempts: 1`, and only when the exact recognized error is `API Error: Response stalled mid-stream`. Ordinary crashes, test failures, timeouts, context exhaustion, and usage-limit failures are not retried.

### Ticket 1 — `dhrystone-self-checking-cycle-contract`

Type: `verification`.

Scope:

```yaml
- dhrystone/dhry_1.c
- dhrystone/testbench.v
```

Required implementation:

- Keep the fixed 100-iteration demo.
- Validate the deterministic final Dhrystone result in firmware. A mismatch prints an error and traps before success or cycle reporting.
- Preserve success magic `123456789` to MMIO address `0x20000000`; the testbench recognizes it and only the validated success path may pass.
- Emit exactly `[SIM_CYCLES] dhry <User_Time>` after validation, with a deterministic timeout.
- The pinned calibration uses xPack GCC 15.2 and has `User_Time = 109734` cycles. Acceptance uses the absolute inclusive cap `110000`; it does not require a baseline cycle count.

Ticket creation authors this Target Plan entry and its owned test registration:

```yaml
target_plan:
  - target: sim_dhry_checked
    role: persistent
```

Register `[sim_dhry_checked] dhry`. Criteria are mandatory:

```yaml
elab_pass: [sim_dhry_checked]
sim_pass:
  - dhrystone/testbench.v @ sim_dhry_checked @ dhry @ pass -> pass
cycle_count:
  - target: sim_dhry_checked
    test: dhry
    cycle_count_max: 110000
review_tb_quality_done:
  target: sim_dhry_checked
```

The persistent Target remains selectable after acceptance and is the provider exported to Ticket 2.

### Ticket 2 — `rv32-zbb-pcpi`

Type: `feature`. Dependency: `dhrystone-self-checking-cycle-contract`. Technical authority: `/opt/riscv-docs/riscv-isa-manual.html`.

Scope:

```yaml
- picorv32.v
- testbench.v
- testbench_wb.v
- Makefile
- tests/zbb.S [new]
```

Required implementation:

- Implement all 18 agreed RV32 Zbb operations: `ANDN`, `ORN`, `XNOR`, `CLZ`, `CTZ`, `CPOP`, `MIN`, `MINU`, `MAX`, `MAXU`, `SEXT.B`, `SEXT.H`, `ZEXT.H`, `ROL`, `ROR`, `RORI`, `ORC.B`, and `REV8`.
- Add `ENABLE_ZBB`, defaulting to `0`, across core, AXI, and Wishbone wrappers.
- Use an internal registered PCPI implementation with a fixed one-cycle response.
- Test enabled execution through all three wrappers.
- In the disabled test, arm a distinct MMIO marker immediately before the first Zbb encoding and require the ensuing illegal-instruction trap. No unrelated trap may count as success.
- Preserve passing behavior on the existing default main/core, AXI, Wishbone, lint, and physical-synthesis Targets.
- Consume and genuinely execute the refreshed persistent `sim_dhry_checked` Target from Ticket 1.

Ticket creation authors six candidate Targets and their owned test tables, all ephemeral:

```yaml
target_plan:
  - {target: sim_core_zbb, role: ephemeral}
  - {target: sim_wb_zbb, role: ephemeral}
  - {target: sim_zbb_disabled, role: ephemeral}
  - {target: lint_core_zbb, role: ephemeral}
  - {target: synth_core_zbb, role: ephemeral}
  - {target: fpga_core_zbb, role: ephemeral}
```

They remain available through execution and acceptance, then their definitions and unambiguously owned test tables are removed automatically. Do not use the retired `on_success.remove_targets` mechanism.

Mandatory Criteria exercise all remaining catalog families:

- `elab_pass` for `sim_core_zbb`, `sim_wb_zbb`, and `sim_zbb_disabled`.
- `elaborate_standalone: true`.
- `lint_clean: [lint_core_zbb]`.
- `sim_pass` for existing main/core, AXI, and Wishbone regressions (`pass -> pass`); all three new Zbb simulation Targets (`fail -> pass`); and refreshed `sim_dhry_checked` (`pass -> pass`).
- `mutation_score` on `picorv32.v` through `sim_core_zbb`, with `min_detected: 14` and `total: 15`. Mutations should target the new Zbb decode, result generation, PCPI handshake, and enable gating.
- `synthesis_ok` using the directed pair `{baseline: synth_core, candidate: synth_core_zbb}`, with `cell_count_increase_at_most: 11%` and `critical_path_ps_increase_at_most: 3%`. These exact limits were measured with Zbb enabled and must not be relaxed.
- `fpga_impl_ok` for `fpga_core_zbb`, requiring successful completion and a fresh artifact but no LUT threshold. It is mandatory only where the exact Vivado profile is runnable.
- Advisory completion reviews `review_rtl_bugs_done`, `review_rtl_protocol_done`, `review_rtl_spec_done`, `review_rtl_code_style_done`, `review_rtl_optimization_done`, `review_rtl_security_done`, and a Target-bound `review_tb_quality_done`.
- In addition, corrective `review_rtl_bugs_clean` is mandatory, so at least one review family exercises the clean disposition.

Across the two Tickets, the suite must exercise all 15 agreed Criterion families: elaboration, standalone elaboration, lint, six RTL review focuses, TB-quality review, simulation, cycle count, mutation, synthesis, and FPGA implementation.

### Acceptance Basis and dependency behavior

Ticket 1’s `sim_dhry_checked` is persistent. Ticket 2 selects it in Criteria and declares Ticket 1 as a dependency, so Ticket 2 enters `waiting` with its original automatically published Acceptance Basis. After Ticket 1 is accepted, Booley performs the ADR-0060 pre-execution Basis Refresh, retains the old Basis as evidence, publishes the new Basis incorporating the accepted provider surface, and atomically promotes Ticket 2 to `queue`. There is no user approval during refresh. Drift or an incompatible provider surface blocks the run instead of silently changing authority.

### Evidence and checkpoints

Use one append-only JSONL Scenario Run Record plus immutable referenced artifacts. Each assertion record includes, as applicable: Run ID, profile, Step ID, assertion ID, timestamp, frozen repository/release/image identities, Session Runtime and child-agent identities, exact command or prompt hash, Ticket and Basis IDs, MCP/Flow invocation and verdict, board transition, artifact path and SHA, git state, retry decision, checkpoint/recovery link, applicability state, Finding link, and cleanup-ledger entry. Never store secret values; otherwise keep the internal QA evidence unredacted for direct Consolidate Findings ingestion.

Required checkpoints are: frozen prepared environment; warning-free Doctor; edit-free continuity baseline; pre-fault Interactive state; restored post-Interactive state; two-Ticket creation barrier; Ticket 1 accepted; Dhrystone negative proof recovered; Ticket 2 Basis Refresh; Ticket 2 accepted; final regression; and cleanup complete. Discovery runs record Findings and continue only from safe checkpoints; regression runs treat promoted assertions as mandatory. Operational completion and QA verdict remain separate.

### Cleanup

The run-owned-resource ledger must account for every branch, worktree, Target, projection, hook, runtime, process, Project Grant, License Profile, relay, and Installation Registration created by the scenario. Remove them in dependency order and record proof. Preserve the borrowed Vivado installation, credentials, base images, caches, and all pre-existing host administration. The final state requires clean pinned repositories, no run-owned processes or mounts, and unchanged remotes.

This resolution is the implementation-ready handoff for #374. Encoding or executing it remains outside this Wayfinder ticket.

