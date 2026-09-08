# Taxi 10G MAC port and evolution: migration and check catalogue

Scenario ID: `taxi-10g-mac-port-evolution`. This is an implementation design, not an executed qualification result.

Canonical journey: [Design the Taxi 10G MAC port-and-evolution scenario — accepted resolution](https://github.com/boldaxolotl/booley/issues/377#issuecomment-5572764904). The [current issue amendment](https://github.com/boldaxolotl/booley/issues/377) supersedes historical shared mechanisms, but preserves the hardware workload, authority, prompts, Tickets, Criteria, faults, thresholds, evidence and cleanup.

## Reading this catalogue

Every check ID below is scenario-qualified. Each row states the action and observable expectation plus evidence to capture at that phase. The linked source section is the authority for its exact semantics. Rows deliberately separate distinct success, rejection, transition, persistence and cleanup claims. The exact accepted source contract is reproduced in the final annex so long prompts, field sets and parameter tables can be extracted without paraphrase. It is a historical source annex: its superseded bookkeeping is not the new execution contract.

The Selection column proposes a central `profiles.yaml` selection class; it is not check-owned profile membership. **Core** means semantic/product behavior; **Linux Vivado** means the existing Ubuntu/Codex provisioned-Vivado requirement; **GUI** requires actual supported-client or qualified visual evidence. Shared setup and producing phases must be selected in the same run for every selected downstream check. Core claims never imply actual VS Code client qualification.

Inherited from `qa/PROTOCOL.md`, `qa/FORMAT.md` and `qa/QUALIFICATION.md`: record compact immutable run inputs, append-only results/findings, explicit resource ownership and immutable evidence. Outcomes are pass/fail/blocked/unavailable; no `not runnable` pseudo-pass. Preserve original unexpected failures after recovery. Recovery points support the current live run only; coordinator interruption preserves a partial run, reconciles resources and starts a new run without reusing prior passes. All specification-backed checks are mandatory from reviewed introduction. No promotion, event replay, separate obligation/allocation entities or general resume system survives.

The parent handoff owns the approved eight-hour budget reallocation and central profile selections. Do not import the inconsistent historical allocation totals from the annex. All artifacts below include producing step, Target/EDA or agent identity as applicable, freshness evidence, immutable path and content identity; one artifact may support multiple independently evaluated rows. Commands/prompts are literal only where the source contract says so.

## Preparation and complete Project Setup

Authority: [accepted resolution, Frozen inputs; representative configuration; Project Setup](https://github.com/boldaxolotl/booley/issues/377#issuecomment-5572764904). Execute in the ordered journey below; subsequent phases depend on their required preceding state.

| Check ID | Selection | Stimulus and expected observation | Evidence / capture point |
|---|---|---|---|
| `taxi-10g-mac-port-evolution.inputs.pin` | Core | Fresh direct clone `fpganinja/taxi@cc70b270b910d369ab1ad7b3855e76399fd461f1` on a run-owned local branch. | Exact commit, initial clean status and tree hashes. |
| `taxi-10g-mac-port-evolution.inputs.no-wrapper` | Core | Use direct Taxi clone, not consumer wrapper. Taxi has no submodules. | Repository topology and gitlink inventory. |
| `taxi-10g-mac-port-evolution.inputs.symlink` | Core | Preserve committed `src/eth/lib/taxi` symlink to `../../../`; resolve real sources without requiring a functional host symlink on Windows. | Git entry, host representation, canonical consumed source paths and before/after bytes. |
| `taxi-10g-mac-port-evolution.inputs.release` | Core | Install exact published release with package hash, release-doc identity, suite commit/digest, protocol revision and base Session Image digest; forbid local/editable/development/floating inputs. | Immutable run inputs and runtime import origin. |
| `taxi-10g-mac-port-evolution.inputs.authority` | Core | Freeze profile, eight-hour deadline, granted authority, auth mechanism without secret values, external artifact root, run-owned branch and cleanup ownership. | Run declaration, capability probes and ledger. |
| `taxi-10g-mac-port-evolution.setup.bootstrap-precondition` | Core | Host Bootstrap is already satisfied; Taxi does not claim automatic Host Bootstrap, owned by OpenTitan. | Host prerequisite probe results. |
| `taxi-10g-mac-port-evolution.setup.initialize` | Core | Delegate `booley init` and full Project Setup from uninitialized clone. | Initial uninitialized state, init invocation and resulting Project identity. |
| `taxi-10g-mac-port-evolution.setup.delegate-authority` | Core | Use one Setup agent with exact approved prompt from source annex; no second approval request. | Delegate identity, assignment, prompt/hash and complete durable log. |
| `taxi-10g-mac-port-evolution.setup.plan` | Core | Produce `SETUP-PLAN.md` as evidence of approved work, not an additional authorization gate. | Plan artifact and action chronology. |
| `taxi-10g-mac-port-evolution.setup.source-preservation` | Core | Do not modify existing RTL, testbench, manifest, symlink or repository metadata during clean setup. | Before/after complete existing-file comparison and repository-state evidence. |
| `taxi-10g-mac-port-evolution.setup.image-provenance` | Core | Build/select Project-derived image from complete pinned `tox.ini` dependency set. | Build recipe, requirements lock, build logs, parent/generated digest and provenance in step output. |
| `taxi-10g-mac-port-evolution.setup.no-runtime-install` | Core | Do not install dependencies over runtime network. | Image manifest, runtime dependency probe and network/install command record. |
| `taxi-10g-mac-port-evolution.setup.import-probe` | Core | Probe the real testbench import path inside Session Runtime. | Runtime identity, exact import command and result. |
| `taxi-10g-mac-port-evolution.setup.stealth-disabled` | Core | Set `[stealth] enabled = false` explicitly; omission is insufficient. | Authored and effective configuration. |
| `taxi-10g-mac-port-evolution.setup.public-navigation` | Core | Use only published setup docs, packaged skills, cheat sheets, CLI/MCP help and ordinary Project inspection until original observation is captured. | Consulted published document identities, command log, first observation before any source-verification activity. |
| `taxi-10g-mac-port-evolution.setup.image-clean-recovery` | Core | If Setup/image import cannot become clean within phase ceiling, preserve observation and bounded recovery; block continuity. | Original result, attempts and configuration trust decision; completed-Setup recovery point only after trustworthy success. |

## Complete pinned Taxi test dependencies

Authority: [accepted resolution, Project Setup contract](https://github.com/boldaxolotl/booley/issues/377#issuecomment-5572764904). Execute in the ordered journey below; subsequent phases depend on their required preceding state.

| Check ID | Selection | Stimulus and expected observation | Evidence / capture point |
|---|---|---|---|
| `taxi-10g-mac-port-evolution.image.package-pytest` | Core | Project-derived image contains `pytest==8.3.4`. | Inside-runtime installed-distribution probe tied to generated image digest. |
| `taxi-10g-mac-port-evolution.image.package-pytest-xdist` | Core | Project-derived image contains `pytest-xdist==3.6.1`. | Inside-runtime installed-distribution probe tied to generated image digest. |
| `taxi-10g-mac-port-evolution.image.package-pytest-split` | Core | Project-derived image contains `pytest-split==0.10.0`. | Inside-runtime installed-distribution probe tied to generated image digest. |
| `taxi-10g-mac-port-evolution.image.package-cocotb` | Core | Project-derived image contains `cocotb==2.0.1`. | Inside-runtime installed-distribution probe tied to generated image digest. |
| `taxi-10g-mac-port-evolution.image.package-cocotb-bus` | Core | Project-derived image contains `cocotb-bus==0.3.0`. | Inside-runtime installed-distribution probe tied to generated image digest. |
| `taxi-10g-mac-port-evolution.image.package-cocotb-test` | Core | Project-derived image contains `cocotb-test==0.2.6`. | Inside-runtime installed-distribution probe tied to generated image digest. |
| `taxi-10g-mac-port-evolution.image.package-cocotbext-axi` | Core | Project-derived image contains `cocotbext-axi==0.1.28`. | Inside-runtime installed-distribution probe tied to generated image digest. |
| `taxi-10g-mac-port-evolution.image.package-cocotbext-eth` | Core | Project-derived image contains `cocotbext-eth==0.1.28`. | Inside-runtime installed-distribution probe tied to generated image digest. |
| `taxi-10g-mac-port-evolution.image.package-cocotbext-i2c` | Core | Project-derived image contains `cocotbext-i2c==0.1.2`. | Inside-runtime installed-distribution probe tied to generated image digest. |
| `taxi-10g-mac-port-evolution.image.package-cocotbext-pcie` | Core | Project-derived image contains `cocotbext-pcie==0.2.16`. | Inside-runtime installed-distribution probe tied to generated image digest. |
| `taxi-10g-mac-port-evolution.image.package-cocotbext-uart` | Core | Project-derived image contains `cocotbext-uart==0.1.4`. | Inside-runtime installed-distribution probe tied to generated image digest. |
| `taxi-10g-mac-port-evolution.image.package-scapy` | Core | Project-derived image contains `scapy==2.6.1`. | Inside-runtime installed-distribution probe tied to generated image digest. |

## Target resolution and representative hardware contract

Authority: [accepted resolution, Representative hardware; Project Setup; ordered steps 4–5](https://github.com/boldaxolotl/booley/issues/377#issuecomment-5572764904). Execute in the ordered journey below; subsequent phases depend on their required preceding state.

| Check ID | Selection | Stimulus and expected observation | Evidence / capture point |
|---|---|---|---|
| `taxi-10g-mac-port-evolution.target.sim-driver` | Core | `sim_mac_10g` uses upstream `test_taxi_eth_mac_10g.sv` wrapper, `test_taxi_eth_mac_10g.py`, Verilator Cocotb and native `trace.fst` on traced runs. | Resolved Target inputs, backend/EDA identity and traced build configuration. |
| `taxi-10g-mac-port-evolution.target.source-closure` | Core | Resolve complete canonicalized source closure from `taxi_eth_mac_10g.f`. | Manifest expansion and actual consumed file list/hash comparison. |
| `taxi-10g-mac-port-evolution.target.lint-driver` | Core | `lint_mac_10g` drives Verible over relevant SystemVerilog sources. | Target identity, resolved inputs and Verible invocation. |
| `taxi-10g-mac-port-evolution.target.synth-driver` | Core | `synth_mac_10g` uses logical Yosys/slang, top `taxi_eth_mac_10g`; do not claim STA/tape-out significance. | Resolved Target and frontend/top/tool identities. |
| `taxi-10g-mac-port-evolution.target.unambiguous` | Core | All three Targets have unambiguous identities and appropriate deep-Doctor selection. | Target inventory and Doctor-selected configuration. |
| `taxi-10g-mac-port-evolution.target.cli-mcp` | Core | CLI and MCP discovery agree on identity, selectors, EDA programs, toplevels, parameters and source inputs. | Both structured inventories and field-level comparison. |
| `taxi-10g-mac-port-evolution.target.fully-qualified` | Core | Exercise fully qualified selector and resolve expected Target. | Selector input and resolved identity. |
| `taxi-10g-mac-port-evolution.target.discovery-refresh` | Core | Change run-owned Project configuration; discovery refreshes without touching Taxi sources. | Before/after config and inventories, underlying source byte comparison; restore approved configuration. |
| `taxi-10g-mac-port-evolution.target.parameter-data_w` | Core | Use `DATA_W: 64` in all applicable Targets; representative full MAC, gearbox disabled per pinned pytest driver. | Resolved parameter evidence from each applicable build, not only source configuration. |
| `taxi-10g-mac-port-evolution.target.parameter-tx_gbx_if_en` | Core | Use `TX_GBX_IF_EN: 0` in all applicable Targets; representative full MAC, gearbox disabled per pinned pytest driver. | Resolved parameter evidence from each applicable build, not only source configuration. |
| `taxi-10g-mac-port-evolution.target.parameter-rx_gbx_if_en` | Core | Use `RX_GBX_IF_EN: 0` in all applicable Targets; representative full MAC, gearbox disabled per pinned pytest driver. | Resolved parameter evidence from each applicable build, not only source configuration. |
| `taxi-10g-mac-port-evolution.target.parameter-gbx_cnt` | Core | Use `GBX_CNT: 1` in all applicable Targets; representative full MAC, gearbox disabled per pinned pytest driver. | Resolved parameter evidence from each applicable build, not only source configuration. |
| `taxi-10g-mac-port-evolution.target.parameter-dic_en` | Core | Use `DIC_EN: 1` in all applicable Targets; representative full MAC, gearbox disabled per pinned pytest driver. | Resolved parameter evidence from each applicable build, not only source configuration. |
| `taxi-10g-mac-port-evolution.target.parameter-ptp_ts_en` | Core | Use `PTP_TS_EN: 1` in all applicable Targets; representative full MAC, gearbox disabled per pinned pytest driver. | Resolved parameter evidence from each applicable build, not only source configuration. |
| `taxi-10g-mac-port-evolution.target.parameter-ptp_td_en` | Core | Use `PTP_TD_EN: 1` in all applicable Targets; representative full MAC, gearbox disabled per pinned pytest driver. | Resolved parameter evidence from each applicable build, not only source configuration. |
| `taxi-10g-mac-port-evolution.target.parameter-ptp_ts_fmt_tod` | Core | Use `PTP_TS_FMT_TOD: 1` in all applicable Targets; representative full MAC, gearbox disabled per pinned pytest driver. | Resolved parameter evidence from each applicable build, not only source configuration. |
| `taxi-10g-mac-port-evolution.target.parameter-ptp_ts_fns_w` | Core | Use `PTP_TS_FNS_W: 16` in all applicable Targets; representative full MAC, gearbox disabled per pinned pytest driver. | Resolved parameter evidence from each applicable build, not only source configuration. |
| `taxi-10g-mac-port-evolution.target.parameter-ptp_ts_w` | Core | Use `PTP_TS_W: 96` in all applicable Targets; representative full MAC, gearbox disabled per pinned pytest driver. | Resolved parameter evidence from each applicable build, not only source configuration. |
| `taxi-10g-mac-port-evolution.target.parameter-ptp_td_sdi_pipeline` | Core | Use `PTP_TD_SDI_PIPELINE: 2` in all applicable Targets; representative full MAC, gearbox disabled per pinned pytest driver. | Resolved parameter evidence from each applicable build, not only source configuration. |
| `taxi-10g-mac-port-evolution.target.parameter-tx_tag_w` | Core | Use `TX_TAG_W: 16` in all applicable Targets; representative full MAC, gearbox disabled per pinned pytest driver. | Resolved parameter evidence from each applicable build, not only source configuration. |
| `taxi-10g-mac-port-evolution.target.parameter-pfc_en` | Core | Use `PFC_EN: 1` in all applicable Targets; representative full MAC, gearbox disabled per pinned pytest driver. | Resolved parameter evidence from each applicable build, not only source configuration. |
| `taxi-10g-mac-port-evolution.target.parameter-pause_en` | Core | Use `PAUSE_EN: 1` in all applicable Targets; representative full MAC, gearbox disabled per pinned pytest driver. | Resolved parameter evidence from each applicable build, not only source configuration. |
| `taxi-10g-mac-port-evolution.target.parameter-stat_en` | Core | Use `STAT_EN: 1` in all applicable Targets; representative full MAC, gearbox disabled per pinned pytest driver. | Resolved parameter evidence from each applicable build, not only source configuration. |
| `taxi-10g-mac-port-evolution.target.parameter-stat_tx_level` | Core | Use `STAT_TX_LEVEL: 2` in all applicable Targets; representative full MAC, gearbox disabled per pinned pytest driver. | Resolved parameter evidence from each applicable build, not only source configuration. |
| `taxi-10g-mac-port-evolution.target.parameter-stat_rx_level` | Core | Use `STAT_RX_LEVEL: 2` in all applicable Targets; representative full MAC, gearbox disabled per pinned pytest driver. | Resolved parameter evidence from each applicable build, not only source configuration. |
| `taxi-10g-mac-port-evolution.target.parameter-stat_id_base` | Core | Use `STAT_ID_BASE: 0` in all applicable Targets; representative full MAC, gearbox disabled per pinned pytest driver. | Resolved parameter evidence from each applicable build, not only source configuration. |
| `taxi-10g-mac-port-evolution.target.parameter-stat_update_period` | Core | Use `STAT_UPDATE_PERIOD: 1024` in all applicable Targets; representative full MAC, gearbox disabled per pinned pytest driver. | Resolved parameter evidence from each applicable build, not only source configuration. |
| `taxi-10g-mac-port-evolution.target.parameter-stat_str_en` | Core | Use `STAT_STR_EN: 1` in all applicable Targets; representative full MAC, gearbox disabled per pinned pytest driver. | Resolved parameter evidence from each applicable build, not only source configuration. |
| `taxi-10g-mac-port-evolution.target.parameter-stat_prefix_str` | Core | Use `STAT_PREFIX_STR: MAC` in all applicable Targets; representative full MAC, gearbox disabled per pinned pytest driver. | Resolved parameter evidence from each applicable build, not only source configuration. |
| `taxi-10g-mac-port-evolution.target.register-rx` | Core | Register upstream rx Cocotb test function; no omission. | Owned test registration and discovered test manifest; preserve original function identity. |
| `taxi-10g-mac-port-evolution.target.register-tx` | Core | Register upstream tx Cocotb test function; no omission. | Owned test registration and discovered test manifest; preserve original function identity. |
| `taxi-10g-mac-port-evolution.target.register-tx-alignment` | Core | Register upstream tx-alignment Cocotb test function; no omission. | Owned test registration and discovered test manifest; preserve original function identity. |
| `taxi-10g-mac-port-evolution.target.register-tx-underrun` | Core | Register upstream tx-underrun Cocotb test function; no omission. | Owned test registration and discovered test manifest; preserve original function identity. |
| `taxi-10g-mac-port-evolution.target.register-tx-user-error` | Core | Register upstream tx-user-error Cocotb test function; no omission. | Owned test registration and discovered test manifest; preserve original function identity. |
| `taxi-10g-mac-port-evolution.target.register-lfc` | Core | Register upstream lfc Cocotb test function; no omission. | Owned test registration and discovered test manifest; preserve original function identity. |
| `taxi-10g-mac-port-evolution.target.register-pfc` | Core | Register upstream pfc Cocotb test function; no omission. | Owned test registration and discovered test manifest; preserve original function identity. |

## Doctor gate and unchanged full continuity

Authority: [accepted resolution, Ordered scenario steps 3–5](https://github.com/boldaxolotl/booley/issues/377#issuecomment-5572764904). Execute in the ordered journey below; subsequent phases depend on their required preceding state.

| Check ID | Selection | Stimulus and expected observation | Evidence / capture point |
|---|---|---|---|
| `taxi-10g-mac-port-evolution.doctor.plain` | Core | Run plain Doctor in order; require warning-free state. No waiver may manufacture clean result. | Doctor JSON/log and selected probes; last recheck establishes Doctor recovery point. |
| `taxi-10g-mac-port-evolution.doctor.deep` | Core | Run deep Doctor in order; require warning-free state. No waiver may manufacture clean result. | Doctor JSON/log and selected probes; last recheck establishes Doctor recovery point. |
| `taxi-10g-mac-port-evolution.doctor.plain-recheck` | Core | Run plain-recheck Doctor in order; require warning-free state. No waiver may manufacture clean result. | Doctor JSON/log and selected probes; last recheck establishes Doctor recovery point. |
| `taxi-10g-mac-port-evolution.doctor.failure-gate` | Core | If Doctor remains unclean at phase ceiling, preserve result and do not enter continuity with untrusted configuration. | Original diagnostics, bounded recovery attempts and blocked dependent checks. |
| `taxi-10g-mac-port-evolution.baseline.full-module` | Core | Run complete seven-function `sim_mac_10g` Cocotb module untraced against unchanged RTL/testbench. | Full results.xml, JSON, logs, resolved inputs, tool identity and source hashes. |
| `taxi-10g-mac-port-evolution.baseline.normal-frames` | Core | Complete upstream run exercises and passes normal-frames behavior in approved configuration. | Test/case manifest with expected/observed oracle, XML/JSON outcome and relevant simulation log. |
| `taxi-10g-mac-port-evolution.baseline.jumbo-frames` | Core | Complete upstream run exercises and passes jumbo-frames behavior in approved configuration. | Test/case manifest with expected/observed oracle, XML/JSON outcome and relevant simulation log. |
| `taxi-10g-mac-port-evolution.baseline.rx-ifg12` | Core | Complete upstream run exercises and passes rx-ifg12 behavior in approved configuration. | Test/case manifest with expected/observed oracle, XML/JSON outcome and relevant simulation log. |
| `taxi-10g-mac-port-evolution.baseline.rx-ifg0` | Core | Complete upstream run exercises and passes rx-ifg0 behavior in approved configuration. | Test/case manifest with expected/observed oracle, XML/JSON outcome and relevant simulation log. |
| `taxi-10g-mac-port-evolution.baseline.tx-ifg12` | Core | Complete upstream run exercises and passes tx-ifg12 behavior in approved configuration. | Test/case manifest with expected/observed oracle, XML/JSON outcome and relevant simulation log. |
| `taxi-10g-mac-port-evolution.baseline.tx-ifg0` | Core | Complete upstream run exercises and passes tx-ifg0 behavior in approved configuration. | Test/case manifest with expected/observed oracle, XML/JSON outcome and relevant simulation log. |
| `taxi-10g-mac-port-evolution.baseline.dic-alignment` | Core | Complete upstream run exercises and passes dic-alignment behavior in approved configuration. | Test/case manifest with expected/observed oracle, XML/JSON outcome and relevant simulation log. |
| `taxi-10g-mac-port-evolution.baseline.good-fcs` | Core | Complete upstream run exercises and passes good-fcs behavior in approved configuration. | Test/case manifest with expected/observed oracle, XML/JSON outcome and relevant simulation log. |
| `taxi-10g-mac-port-evolution.baseline.tx-underrun` | Core | Complete upstream run exercises and passes tx-underrun behavior in approved configuration. | Test/case manifest with expected/observed oracle, XML/JSON outcome and relevant simulation log. |
| `taxi-10g-mac-port-evolution.baseline.tx-user-error` | Core | Complete upstream run exercises and passes tx-user-error behavior in approved configuration. | Test/case manifest with expected/observed oracle, XML/JSON outcome and relevant simulation log. |
| `taxi-10g-mac-port-evolution.baseline.lfc-frame` | Core | Complete upstream run exercises and passes lfc-frame behavior in approved configuration. | Test/case manifest with expected/observed oracle, XML/JSON outcome and relevant simulation log. |
| `taxi-10g-mac-port-evolution.baseline.pfc-frame` | Core | Complete upstream run exercises and passes pfc-frame behavior in approved configuration. | Test/case manifest with expected/observed oracle, XML/JSON outcome and relevant simulation log. |
| `taxi-10g-mac-port-evolution.baseline.rx-timestamps` | Core | Complete upstream run exercises and passes rx-timestamps behavior in approved configuration. | Test/case manifest with expected/observed oracle, XML/JSON outcome and relevant simulation log. |
| `taxi-10g-mac-port-evolution.baseline.tx-timestamps` | Core | Complete upstream run exercises and passes tx-timestamps behavior in approved configuration. | Test/case manifest with expected/observed oracle, XML/JSON outcome and relevant simulation log. |
| `taxi-10g-mac-port-evolution.baseline.verible` | Core | Run clean continuity Verible lint and require clean grade. | Fresh Target-bound lint report. |
| `taxi-10g-mac-port-evolution.baseline.yosys` | Core | Run fresh slang/Yosys logical synthesis and require successful artifact-verified report. | Normalized result, actual tool/frontend identity and fresh artifacts; full clean continuity recovery point. |

## Interactive native FST, B-Wave and Viewer state

Authority: [accepted resolution, Interactive Mode contract](https://github.com/boldaxolotl/booley/issues/377#issuecomment-5572764904). Execute in the ordered journey below; subsequent phases depend on their required preceding state.

| Check ID | Selection | Stimulus and expected observation | Evidence / capture point |
|---|---|---|---|
| `taxi-10g-mac-port-evolution.interactive.child-context` | Core | Launch one long-lived read-only Interactive child in setup-complete Session Runtime with Project cwd and MCP preserved. | Child/runtime/client/backend identity and durable log. |
| `taxi-10g-mac-port-evolution.interactive.prompt` | Core | Send exact annex prompt; confirm runtime, cleanliness, Doctor and Targets; no file edits, commits, weaker checks or push. | Prompt/hash, agent log and before/after repository state. |
| `taxi-10g-mac-port-evolution.interactive.discovery` | Core | Child compares CLI/MCP Target discovery and resolved build inputs. | Both inventories and source/parameter comparison. |
| `taxi-10g-mac-port-evolution.interactive.focused-pfc` | Core | Run focused PFC test on `sim_mac_10g` with fresh native FST. | Exact test identity, fresh passing simulation and trace identity. |
| `taxi-10g-mac-port-evolution.fst.metadata` | Core | Prove nonempty identity-bound native FST with scopes, signal count, size and ticks. | Trace metadata and generating Verilator run identity. |
| `taxi-10g-mac-port-evolution.bwave.alias-register` | Core | Register named alias and `_last`. | Registration commands and observed references. |
| `taxi-10g-mac-port-evolution.bwave.alias-persist` | Core | Resolve named alias after a new command context. | Separate-context command and same trace identity. |
| `taxi-10g-mac-port-evolution.bwave.alias-stale` | Core | Reject stale registration. | Controlled stale reference, rejection output and state. |
| `taxi-10g-mac-port-evolution.bwave.alias-missing` | Core | Reject missing registration. | Missing reference and rejection result. |
| `taxi-10g-mac-port-evolution.bwave.marker-create` | Core | Create named markers and verify resulting state. | Exact commands, returned marker/time identities and subsequent readback/absence as applicable. |
| `taxi-10g-mac-port-evolution.bwave.marker-list` | Core | List named markers and verify resulting state. | Exact commands, returned marker/time identities and subsequent readback/absence as applicable. |
| `taxi-10g-mac-port-evolution.bwave.marker-resolve` | Core | Resolve named markers and verify resulting state. | Exact commands, returned marker/time identities and subsequent readback/absence as applicable. |
| `taxi-10g-mac-port-evolution.bwave.marker-delete` | Core | Delete named markers and verify resulting state. | Exact commands, returned marker/time identities and subsequent readback/absence as applicable. |
| `taxi-10g-mac-port-evolution.bwave.semantic-list` | Core | Exercise `list` against known PFC request, frame-start, XGMII data/control, timestamp, reset or statistics relationships. | Exact query, trace identity, independently expected value/relationship and observed result; source requires whole surface but fixture selects deterministic query cases. |
| `taxi-10g-mac-port-evolution.bwave.semantic-signal` | Core | Exercise `signal` against known PFC request, frame-start, XGMII data/control, timestamp, reset or statistics relationships. | Exact query, trace identity, independently expected value/relationship and observed result; source requires whole surface but fixture selects deterministic query cases. |
| `taxi-10g-mac-port-evolution.bwave.semantic-wave` | Core | Exercise `wave` against known PFC request, frame-start, XGMII data/control, timestamp, reset or statistics relationships. | Exact query, trace identity, independently expected value/relationship and observed result; source requires whole surface but fixture selects deterministic query cases. |
| `taxi-10g-mac-port-evolution.bwave.semantic-value` | Core | Exercise `value` against known PFC request, frame-start, XGMII data/control, timestamp, reset or statistics relationships. | Exact query, trace identity, independently expected value/relationship and observed result; source requires whole surface but fixture selects deterministic query cases. |
| `taxi-10g-mac-port-evolution.bwave.semantic-find` | Core | Exercise `find` against known PFC request, frame-start, XGMII data/control, timestamp, reset or statistics relationships. | Exact query, trace identity, independently expected value/relationship and observed result; source requires whole surface but fixture selects deterministic query cases. |
| `taxi-10g-mac-port-evolution.bwave.semantic-sample` | Core | Exercise `sample` against known PFC request, frame-start, XGMII data/control, timestamp, reset or statistics relationships. | Exact query, trace identity, independently expected value/relationship and observed result; source requires whole surface but fixture selects deterministic query cases. |
| `taxi-10g-mac-port-evolution.bwave.semantic-diff` | Core | Exercise `diff` against known PFC request, frame-start, XGMII data/control, timestamp, reset or statistics relationships. | Exact query, trace identity, independently expected value/relationship and observed result; source requires whole surface but fixture selects deterministic query cases. |
| `taxi-10g-mac-port-evolution.bwave.semantic-distance` | Core | Exercise `distance` against known PFC request, frame-start, XGMII data/control, timestamp, reset or statistics relationships. | Exact query, trace identity, independently expected value/relationship and observed result; source requires whole surface but fixture selects deterministic query cases. |
| `taxi-10g-mac-port-evolution.bwave.semantic-stats` | Core | Exercise `stats` against known PFC request, frame-start, XGMII data/control, timestamp, reset or statistics relationships. | Exact query, trace identity, independently expected value/relationship and observed result; source requires whole surface but fixture selects deterministic query cases. |
| `taxi-10g-mac-port-evolution.bwave.semantic-stuck` | Core | Exercise `stuck` against known PFC request, frame-start, XGMII data/control, timestamp, reset or statistics relationships. | Exact query, trace identity, independently expected value/relationship and observed result; source requires whole surface but fixture selects deterministic query cases. |
| `taxi-10g-mac-port-evolution.bwave.synchronous-view` | Core | Exercise synchronous-view using approved trace relationships. | Query and expected/observed time or sampling semantics. |
| `taxi-10g-mac-port-evolution.bwave.asynchronous-view` | Core | Exercise asynchronous-view using approved trace relationships. | Query and expected/observed time or sampling semantics. |
| `taxi-10g-mac-port-evolution.bwave.explicit-clock` | Core | Exercise explicit-clock using approved trace relationships. | Query and expected/observed time or sampling semantics. |
| `taxi-10g-mac-port-evolution.bwave.explicit-reset` | Core | Exercise explicit-reset using approved trace relationships. | Query and expected/observed time or sampling semantics. |
| `taxi-10g-mac-port-evolution.bwave.cycle-token` | Core | Exercise cycle-token using approved trace relationships. | Query and expected/observed time or sampling semantics. |
| `taxi-10g-mac-port-evolution.bwave.typed-physical-time` | Core | Exercise typed-physical-time using approved trace relationships. | Query and expected/observed time or sampling semantics. |
| `taxi-10g-mac-port-evolution.bwave.latency-crosscheck` | Core | Measure request-to-frame latency and cross-check against Cocotb observation, not CLI exit alone. | B-Wave timestamps/cycles, independent Cocotb timing observation and comparison. |
| `taxi-10g-mac-port-evolution.viewer.scoped-state` | GUI | Create scoped Waveform Viewer state containing clock, PFC request, packet-start, XGMII data/control and relevant statistics. | Live supported-client WCP state/readback with exact signal paths and client/runtime identity. Offline state alone does not satisfy the original live Viewer claim. |
| `taxi-10g-mac-port-evolution.viewer.markers-cursor` | GUI | Scoped Viewer state includes start/end markers and cursor. | Live supported-client WCP readback, expected times and client identity. |
| `taxi-10g-mac-port-evolution.viewer.actual-client-open` | GUI | Open that scoped state in actual supported Waveform Viewer/VS Code client. | Client version/attachment, open operation and actual state evidence; headless WCP cannot prove open/rendering. |
| `taxi-10g-mac-port-evolution.viewer.visual-capture` | GUI | Capture Viewer only with pre-run qualified observer; verify visible scoped signals/markers/cursor. | Timestamped screenshot with client/run identity and observer capability evidence; unavailable observer means incomplete GUI profile. |
| `taxi-10g-mac-port-evolution.interactive.tb-review` | Core | Invoke TB-quality Reviewer against `sim_mac_10g`. | Target-bound structured Specialist report and invocation. |
| `taxi-10g-mac-port-evolution.interactive.restore` | Core | Restore and verify clean post-Interactive state. | Source/config/tree comparison and alias/marker/viewer resource disposition; restored recovery point. |

## Ticket creation and retry boundaries

Authority: [accepted resolution, Ticket Create invocations; both Ticket contracts](https://github.com/boldaxolotl/booley/issues/377#issuecomment-5572764904). Execute in the ordered journey below; subsequent phases depend on their required preceding state.

| Check ID | Selection | Stimulus and expected observation | Evidence / capture point |
|---|---|---|---|
| `taxi-10g-mac-port-evolution.create.strengthen-10g-mac-observability.payload` | Core | Invoke Ticket Create Agent Mode for **Strengthen 10G MAC observability** with complete annex payload, `--agent --no-confirm` and provider spelling; no inferred semantics. | Exact invocation, full payload/hash, parsed Scope/Criteria/Target Plan and returned Ticket path. |
| `taxi-10g-mac-port-evolution.create.strengthen-10g-mac-observability.basis` | Core | Capture published Acceptance Basis for `strengthen-10g-mac-observability` and queued Board state. | Immutable receipt, ticket/board evidence and source revision. |
| `taxi-10g-mac-port-evolution.create.repair-pfc-priority-routing.payload` | Core | Invoke Ticket Create Agent Mode for **Repair PFC priority routing** with complete annex payload, `--agent --no-confirm` and provider spelling; no inferred semantics. | Exact invocation, full payload/hash, parsed Scope/Criteria/Target Plan and returned Ticket path. |
| `taxi-10g-mac-port-evolution.create.repair-pfc-priority-routing.basis` | Core | Capture published Acceptance Basis for `repair-pfc-priority-routing` and queued Board state. | Immutable receipt, ticket/board evidence and source revision. |
| `taxi-10g-mac-port-evolution.create.verification-scope` | Core | Verification Ticket only authors new `qa/taxi_eth_mac_10g/test_observability.py` and `qa/taxi_eth_mac_10g/test_observability.sv`; no pre-existing file edits or upstream test weakening. | Exact Scope and complete diff/test comparison. |
| `taxi-10g-mac-port-evolution.retry.exact-allowance` | Core | Only one automatic retry, `max_attempts: 1`, only exact `API Error: Response stalled mid-stream`; no automatic retry for failures, survivors, timeouts, crashes, context or usage exhaustion. | Every attempt/error and explicit retry decision. |

## Verification Ticket: independent observability assertions

Authority: [accepted resolution, Ticket 1 — Strengthen 10G MAC observability](https://github.com/boldaxolotl/booley/issues/377#issuecomment-5572764904). Execute in the ordered journey below; subsequent phases depend on their required preceding state.

| Check ID | Selection | Stimulus and expected observation | Evidence / capture point |
|---|---|---|---|
| `taxi-10g-mac-port-evolution.ticket1.target-definition` | Core | Create persistent `sim_mac_10g_observability`, new thin wrapper/toplevel and Cocotb module; reuse approved 64-bit gearbox-disabled DIC/PTP/PFC/statistics contract. | Target Plan, test registration, source closure and parameter comparison. |
| `taxi-10g-mac-port-evolution.ticket1.bad-fcs-stimulus` | Core | Corrupt received XGMII frame FCS without changing payload stimulus. | Exact frame payload/FCS generation and injected stimulus. |
| `taxi-10g-mac-port-evolution.ticket1.bad-fcs-tuser` | Core | Bad RX FCS raises RX error `tuser`. | Stimulus-correlated RX output assertion. |
| `taxi-10g-mac-port-evolution.ticket1.bad-fcs-indication` | Core | Bad RX FCS raises discrete bad-FCS indication. | Signal assertion/trace with frame association. |
| `taxi-10g-mac-port-evolution.ticket1.bad-fcs-counter` | Core | Bad RX FCS produces counter record ID 34. | Statistics records and frame-correlated assertion. |
| `taxi-10g-mac-port-evolution.ticket1.pfc-stimulus` | Core | Cover all eight classes with distinct quanta `[10,20,30,40,50,60,70,80]`. | Versioned test inputs and executed case manifest. |
| `taxi-10g-mac-port-evolution.ticket1.pfc-tx-bitmap` | Core | Require exact transmitted PFC class bitmap, not frame count alone. | Decoded transmitted frame and expected bitmap assertion. |
| `taxi-10g-mac-port-evolution.ticket1.pfc-rx-bitmap` | Core | Require exact received PFC class bitmap. | Decoded receive indication and expected bitmap assertion. |
| `taxi-10g-mac-port-evolution.ticket1.pfc-tx-quanta` | Core | Require each transmitted class quanta equals corresponding distinct input. | All eight per-class expected/observed quanta. |
| `taxi-10g-mac-port-evolution.ticket1.pfc-rx-quanta` | Core | Require each received class quanta equals corresponding distinct input. | All eight per-class expected/observed quanta. |
| `taxi-10g-mac-port-evolution.ticket1.pfc-counter25` | Core | Require relevant PFC counter ID 25. | Typed statistics record and stimulus association. |
| `taxi-10g-mac-port-evolution.ticket1.pfc-counter57` | Core | Require relevant PFC counter ID 57. | Typed statistics record and stimulus association. |
| `taxi-10g-mac-port-evolution.ticket1.underrun-stimulus` | Core | Reproduce deterministic four-cycle source pause. | Cycle-level source stimulus and timing proof. |
| `taxi-10g-mac-port-evolution.ticket1.underrun-termination` | Core | Require XGMII error termination following pause. | XGMII stream assertion/trace. |
| `taxi-10g-mac-port-evolution.ticket1.underrun-counter` | Core | Require underrun counter record ID 3. | Typed statistics records and assertion. |
| `taxi-10g-mac-port-evolution.ticket1.completion-tag` | Core | Uniquely tag frames; every completion returns correct 16-bit tag. | Input/completion bijection with expected/observed tags. |
| `taxi-10g-mac-port-evolution.ticket1.completion-time` | Core | Verify completion timestamp relationship to observed SFD. | Independently observed SFD time and completion timestamp comparison. |
| `taxi-10g-mac-port-evolution.ticket1.statistics-typing` | Core | Distinguish `tuser=0` counter records from `tuser=1` strings; never count strings as counters. | Executed mixed-stream cases and typed record assertions. |
| `taxi-10g-mac-port-evolution.ticket1.statistics-namespace` | Core | Verify expected statistics ID namespace. | Expected ID mapping and observed stream IDs. |
| `taxi-10g-mac-port-evolution.ticket1.elaboration` | Core | Require successful Elaboration Check for observability Target. | Target-bound normalized result. |
| `taxi-10g-mac-port-evolution.ticket1.simulation` | Core | Require complete observability Simulation success. | Full test manifest, XML/JSON and outcomes. |
| `taxi-10g-mac-port-evolution.ticket1.tb-review-clean` | Core | Require clean TB-quality review bound to observability Target. | Structured clean review and criterion outcome. |
| `taxi-10g-mac-port-evolution.ticket1.persistent-target` | Core | Accepted observability Target remains selectable after acceptance. | Post-acceptance fresh inventory and actual invocation. |

## Fixed 7-of-8 mutation campaign

Authority: [accepted resolution, Ticket 1 mutation contract](https://github.com/boldaxolotl/booley/issues/377#issuecomment-5572764904). Execute in the ordered journey below; subsequent phases depend on their required preceding state.

| Check ID | Selection | Stimulus and expected observation | Evidence / capture point |
|---|---|---|---|
| `taxi-10g-mac-port-evolution.mutation.contract` | Core | Run `mutation_score` Target `sim_mac_10g_observability`, scope `src/eth/rtl/taxi_eth_mac_10g.sv`, total 8, min_detected 7. | Exact campaign configuration and criterion result. |
| `taxi-10g-mac-port-evolution.mutation.source-isolation` | Core | Hide new Cocotb and wrapper source from Mutation Tester during proposal. | Specialist Source Isolation inputs and visible-source manifest. |
| `taxi-10g-mac-port-evolution.mutation.steering` | Core | Steer proposals to PFC request/ack routing, RX-error propagation, statistics enable/output and TX timestamp/tag wiring. | Steering prompt and proposal manifest. |
| `taxi-10g-mac-port-evolution.mutation.proposal-lock` | Core | Lock proposal before running mutants. | Immutable proposal lock and chronology. |
| `taxi-10g-mac-port-evolution.mutation.pristine-baseline` | Core | Retain passing pristine baseline. | Original source identity and fresh baseline results. |
| `taxi-10g-mac-port-evolution.mutation.isolated-results` | Core | Execute eight isolated source variants and retain each outcome/first-killing-test evidence. | Atomic campaign manifest with variant hashes and per-mutant test results. |
| `taxi-10g-mac-port-evolution.mutation.restoration` | Core | Restore pristine sources after mutant execution. | Source comparison and restoration proof. |
| `taxi-10g-mac-port-evolution.mutation.threshold` | Core | At least seven mutants detected; one survivor may be reported, fewer than seven fails. | Detected/survivor totals and exact threshold comparison. |
| `taxi-10g-mac-port-evolution.mutation.failure-gate` | Core | If Ticket 1 fails or mutation below seven, do not seed RTL; run trustworthy original baseline where possible and clean up. | Blocked seed/Ticket2 results, retained failed campaign and cleanup record. |

## Authorized PFC rotation, split oracle and repair

Authority: [accepted resolution, Seeded RTL fault; Ticket 2; failure and recovery](https://github.com/boldaxolotl/booley/issues/377#issuecomment-5572764904). Execute in the ordered journey below; subsequent phases depend on their required preceding state.

| Check ID | Selection | Stimulus and expected observation | Evidence / capture point |
|---|---|---|---|
| `taxi-10g-mac-port-evolution.seed.clean-baseline` | Core | After Ticket1 acceptance rerun complete upstream and observability simulations against pristine RTL. | Fresh passing results, accepted Target/commit identity; clean evolution recovery point. |
| `taxi-10g-mac-port-evolution.seed.authorized-diff` | Core | Only at `taxi_mac_pause_ctrl_tx` connection in `src/eth/rtl/taxi_eth_mac_10g.sv`, replace `.tx_pfc_req(tx_pfc_req)` with `.tx_pfc_req({tx_pfc_req[6:0], tx_pfc_req[7]})`; commit on run-owned branch. | Exact diff, seed commit and unchanged unrelated file hashes. |
| `taxi-10g-mac-port-evolution.seed.upstream-pass` | Core | Original count-only upstream PFC test still passes on seeded RTL. | Fresh original-test result and frame-count observation. |
| `taxi-10g-mac-port-evolution.seed.oracle-fail` | Core | New exact class/quanta assertion fails: class 0 observed as class 1. | Expected/observed class assertion and fresh failure log. |
| `taxi-10g-mac-port-evolution.seed.fst-relationship` | Core | Fresh FST proves corresponding priority rotation relationship. | Trace identity and B-Wave queries/values. |
| `taxi-10g-mac-port-evolution.seed.invalid-split-recovery` | Core | If exact original-pass/new-fail split is absent, restore clean evolution point and do not create Bug Fix Ticket. | Both original observations, restoration and blocked creation result. |
| `taxi-10g-mac-port-evolution.seed.hidden-location` | Core | Give repair Developer only assertion, expected/observed behavior, upstream-test contrast, Scope/evidence; hide seed location and repair. | Exact agent-visible prompt and evidence-access manifest. |
| `taxi-10g-mac-port-evolution.ticket2.basis-dependency` | Core | Bug Fix Ticket basis is seed commit; completed Verification Ticket is dependency. | Parsed Ticket, Basis source identity and dependency state. |
| `taxi-10g-mac-port-evolution.ticket2.scope` | Core | Run distinct `booley run` for repair; Scope only `src/eth/rtl/taxi_eth_mac_10g.sv`; do not alter tests, Criteria, config, unrelated RTL or accepted assets. | Invocation, full diff and authority comparison. |
| `taxi-10g-mac-port-evolution.ticket2.reproduce` | Core | Reproduce exact class-rotation failure on fresh simulation and trace. | New run/FST and assertion, separate from operator seed proof. |
| `taxi-10g-mac-port-evolution.ticket2.diagnose` | Core | Diagnose from evidence and ordinary Project inspection, not hidden seed text. | Agent log, B-Wave queries and relationship-based diagnosis. |
| `taxi-10g-mac-port-evolution.ticket2.repair-identity` | Core | Repair leaves affected RTL byte-identical to pinned upstream. | File hash comparison against pinned Git object. |
| `taxi-10g-mac-port-evolution.ticket2.elab-upstream` | Core | Require Elaboration Check for `sim_mac_10g`. | Normalized Target-bound result. |
| `taxi-10g-mac-port-evolution.ticket2.elab-observability` | Core | Require Elaboration Check for `sim_mac_10g_observability`. | Normalized Target-bound result. |
| `taxi-10g-mac-port-evolution.ticket2.sim-upstream` | Core | Complete upstream `sim_mac_10g` remains pass-to-pass. | Full Basis/candidate XML/JSON/test manifests. |
| `taxi-10g-mac-port-evolution.ticket2.sim-observability` | Core | Observability Target records seeded fail-to-pass. | Basis failure/candidate success with exact test identity. |
| `taxi-10g-mac-port-evolution.ticket2.lint` | Core | Require `lint_clean` for `lint_mac_10g`. | Fresh Verible result. |
| `taxi-10g-mac-port-evolution.ticket2.synthesis-cells` | Core | Directed Acceptance Basis/candidate `synth_mac_10g`: `cell_count_increase_at_most: 0%`. | Fresh logical-synthesis paired metrics and exact comparison. |
| `taxi-10g-mac-port-evolution.ticket2.review-bugs` | Core | Require clean RTL bugs review. | Structured clean report and criterion result. |
| `taxi-10g-mac-port-evolution.ticket2.review-protocol` | Core | Require terminal RTL protocol review. | Terminal focus-specific report. |
| `taxi-10g-mac-port-evolution.ticket2.review-spec` | Core | Require terminal RTL specification review. | Terminal focus-specific report. |
| `taxi-10g-mac-port-evolution.ticket2.failure-recovery` | Core | On repair failure preserve worktree/report/diff evidence before restoring clean post-Ticket1 point for cleanup. Restoration never changes failure into pass. | Archived worktree/git evidence, original failure and restored identity. |

## Ticket lifecycles and complete final regression

Authority: [accepted resolution, Ordered scenario; Final regression and cleanup](https://github.com/boldaxolotl/booley/issues/377#issuecomment-5572764904). Execute in the ordered journey below; subsequent phases depend on their required preceding state.

| Check ID | Selection | Stimulus and expected observation | Evidence / capture point |
|---|---|---|---|
| `taxi-10g-mac-port-evolution.ticket1.done` | Core | Reach direct acceptance and `destination: done`. | Acceptance result and Board state. |
| `taxi-10g-mac-port-evolution.ticket1.merge` | Core | Merge accepted changes locally. | Accepted commit and destination identity. |
| `taxi-10g-mac-port-evolution.ticket1.workspace-cleanup` | Core | Clean accepted Ticket Workspace. | Owned worktree absence and ledger. |
| `taxi-10g-mac-port-evolution.ticket1.triage-report` | Core | Produce triage report. | Fresh ticket-bound report artifact. |
| `taxi-10g-mac-port-evolution.ticket2.done` | Core | Reach direct acceptance and `destination: done`. | Acceptance result and Board state. |
| `taxi-10g-mac-port-evolution.ticket2.merge` | Core | Merge accepted changes locally. | Accepted commit and destination identity. |
| `taxi-10g-mac-port-evolution.ticket2.workspace-cleanup` | Core | Clean accepted Ticket Workspace. | Owned worktree absence and ledger. |
| `taxi-10g-mac-port-evolution.ticket2.triage-report` | Core | Produce triage report. | Fresh ticket-bound report artifact. |
| `taxi-10g-mac-port-evolution.final.doctor-plain` | Core | Rerun plain Doctor warning-free. | Fresh report. |
| `taxi-10g-mac-port-evolution.final.doctor-deep` | Core | Rerun deep Doctor warning-free. | Fresh report. |
| `taxi-10g-mac-port-evolution.final.upstream` | Core | Rerun complete upstream simulation. | Full new results.xml/JSON and test manifest. |
| `taxi-10g-mac-port-evolution.final.observability` | Core | Rerun complete observability simulation. | Full new results.xml/JSON and test manifest. |
| `taxi-10g-mac-port-evolution.final.lint` | Core | Rerun clean Verible lint. | Fresh lint result. |
| `taxi-10g-mac-port-evolution.final.synthesis` | Core | Rerun successful slang/Yosys logical synthesis. | Fresh result/artifacts. |
| `taxi-10g-mac-port-evolution.final.bwave` | Core | Rerun repaired PFC B-Wave relationships. | Fresh trace and relationship observations. |
| `taxi-10g-mac-port-evolution.final.source-identity` | Core | Every pre-existing Taxi RTL/testbench byte matches pinned revision. | Complete hash comparison. |
| `taxi-10g-mac-port-evolution.final.accepted-assets` | Core | Only accepted `qa/taxi_eth_mac_10g/` assets plus persistent Target remain as intentional content changes. | Full final diff/configuration inventory and clean repository state. |

## Evidence archive, cleanup and failure preservation

Authority: [accepted resolution, Failure and recovery; deadline; cleanup](https://github.com/boldaxolotl/booley/issues/377#issuecomment-5572764904). Execute in the ordered journey below; subsequent phases depend on their required preceding state.

| Check ID | Selection | Stimulus and expected observation | Evidence / capture point |
|---|---|---|---|
| `taxi-10g-mac-port-evolution.archive.before-delete` | Core | Archive outside disposable clone before deleting Project state. | Archive path and existence/identity; evidence-archive recovery point. |
| `taxi-10g-mac-port-evolution.archive.git-bundle` | Core | Preserve git bundle containing accepted/seed/repair history needed by evidence. | Bundle identity and reference verification. |
| `taxi-10g-mac-port-evolution.archive.evidence` | Core | Preserve compact run records, human summary/Consolidate Findings handoff, logs, XML/JSON, FST/B-Wave/WCP, mutation manifest/variants, Ticket/Basis, image provenance, hashes/diffs/commits. | Explicit archive manifest mapping each produced artifact; no secrets and no Booley Feedback invocation. |
| `taxi-10g-mac-port-evolution.cleanup.session-runtimes` | Core | Reconcile/remove every run-owned Session-Runtimes after evidence archive. | Owned identity, release action and absence/state proof. |
| `taxi-10g-mac-port-evolution.cleanup.processes` | Core | Reconcile/remove every run-owned processes after evidence archive. | Owned identity, release action and absence/state proof. |
| `taxi-10g-mac-port-evolution.cleanup.worktrees` | Core | Reconcile/remove every run-owned worktrees after evidence archive. | Owned identity, release action and absence/state proof. |
| `taxi-10g-mac-port-evolution.cleanup.local-branches` | Core | Reconcile/remove every run-owned local-branches after evidence archive. | Owned identity, release action and absence/state proof. |
| `taxi-10g-mac-port-evolution.cleanup.project-inventory-entries` | Core | Reconcile/remove every run-owned Project-Inventory-entries after evidence archive. | Owned identity, release action and absence/state proof. |
| `taxi-10g-mac-port-evolution.cleanup.project-derived-images` | Core | Reconcile/remove every run-owned Project-derived-images after evidence archive. | Owned identity, release action and absence/state proof. |
| `taxi-10g-mac-port-evolution.cleanup.volumes` | Core | Reconcile/remove every run-owned volumes after evidence archive. | Owned identity, release action and absence/state proof. |
| `taxi-10g-mac-port-evolution.cleanup.mounts` | Core | Reconcile/remove every run-owned mounts after evidence archive. | Owned identity, release action and absence/state proof. |
| `taxi-10g-mac-port-evolution.cleanup.inner-project-repository` | Core | Reconcile/remove every run-owned inner-Project-repository after evidence archive. | Owned identity, release action and absence/state proof. |
| `taxi-10g-mac-port-evolution.cleanup.disposable-clone` | Core | Reconcile/remove every run-owned disposable-clone after evidence archive. | Owned identity, release action and absence/state proof. |
| `taxi-10g-mac-port-evolution.cleanup.preserve-host` | Core | Preserve credentials, Booley-owned base images, caches and all pre-existing host state. | Before/after resource comparison excluding secret values. |
| `taxi-10g-mac-port-evolution.cleanup.remotes` | Core | Remotes unchanged; nothing pushed. | Initial/final remote identities and authority/operation log. |
| `taxi-10g-mac-port-evolution.cleanup.verdict` | Core | Incomplete mandatory cleanup prevents pass, separately visible from product failure. | Resource reconciliation, missing actions and profile/operational result. |
| `taxi-10g-mac-port-evolution.failure.independent-continuation` | Core | Unexpected failure prunes dependents only; independent documentation/platform/finalization/cleanup continue with trustworthy prerequisites. | Original result and explicit continuation/prerequisite decisions. |
| `taxi-10g-mac-port-evolution.deadline.finalize` | Core | At hard deadline finalize missing checks blocked, preserve partial record and attempt cleanup; do not silently drop assertions. | Timing record and full selected-check/result reconciliation. |

## Companion and central profile integration

The user approved a separately disposable submodule Project as a Taxi companion, preserving the authentic direct-clone hardware journey. The parent handoff supplies its concrete checks, central profile selections, same-run prerequisites and explicit phase allowance. No row above earns offline submodule credit from Taxi’s symlink or absence of gitlinks. Companion evidence must identify its own Project/resources; its source changes must never alter the pinned Taxi baseline.

Core Ubuntu 24.04 x86-64/Codex and native Windows x86-64 with Docker Desktop/WSL2/Codex retain the complete journey. Taxi has no initial Claude lane. Central profiles separate semantic FST/B-Wave evidence from live WCP/Viewer/client and visual assertions. Required unavailable GUI capability makes GUI qualification incomplete, while core may pass.

## Encoding clarifications that must remain visible

- The exact B-Wave queries, signal paths and expected values for each named consumer, view/token form and marker operation must be frozen as evaluator fixture material against the pinned trace. The accepted source specifies relationships and cross-checks, not literal query transcripts; implementation must not accept command success as the oracle.
- The live scoped WCP state, its markers/cursor and actual Viewer open belong to GUI/client qualification. A headless file does not satisfy these accepted live-client claims. Visual rendering additionally requires a qualified observer.
- The user resolved the logical critical-path metric conflict by adding physical synthesis while retaining logical synthesis. See the explicit amendment below; the original 0% limit now has a physical evidence source and is not silently converted to estimated Fmax.
- The exact configuration-refresh mutation is unspecified; choose and version a reversible Project-owned discovery-visible change while preserving approved hardware parameters for the substantive run.
- Source contract does not supply fixed numeric expected timestamp/ID-namespace oracles beyond declared relationships/IDs. Derive exact expected cases from pinned upstream protocol and testbench evidence, with independent observations where required; do not relax the relationships.

## Approved eight-hour allocation

All work, including supplemental capability exercises, is inside these phase ceilings: preparation/initialization/image/Setup 75m; Doctor/continuity 60m; Interactive 45m; disposable submodule companion 40m; Ticket1/mutation 100m; fault proof/Ticket2 100m; final regression 30m; contingency 15m; cleanup 15m. Total 480m. Start final regression by 7h15, or proceed to cleanup if regression cannot safely start; begin cleanup by 7h45. No development or companion work may consume the final-regression or 15m cleanup reserve. Preserve per-command limits, additionally bounded by remaining phase/run time. Shorter ceilings do not reduce workload or authorize retry; feasibility remains unproven until execution.

## Approved addition: physical synthesis for genuine timing evidence

The user chose **add physical synthesis**, retaining Taxi's existing slang/Yosys logical path. This amends the accepted source's attempt to apply `critical_path_ps_increase_at_most: 0%` to logical synthesis. The logical path remains required for Setup, continuity, Ticket2 and final regression; its original cell-count comparison remains 0%. Add a separate physical Target and apply both 0% cell and 0% critical-path comparisons there. Never reinterpret `estimated_fmax_mhz` as measured physical timing. The historical source annex remains unchanged.

### Added Target and constraints

Create Project-owned persistent Target `synth_mac_10g_physical` during Setup, with the same complete canonical source closure, top `taxi_eth_mac_10g`, and every parameter in the representative MAC table. Keep `frontend: slang`; set `tool: yosys`, `synth_mode: physical`, `ppa_profile: balanced`, `flatten: true` explicitly in Target `flow_options`. Use the published Booley Session Image's physical synthesis resources and record exact Yosys/OpenROAD, liberty/technology and recipe identities. No backend-specific override or alternative library is introduced by this design. The same frozen recipe, source closure, parameter set, library and SDC apply to both sides of every comparison.

`qa` here names the suite, not a hardcoded framework Project path. Author the SDC within the run's Project-owned configuration repository (the directory resolved by `booley.runtime.project_dir`), e.g. `constraints/taxi_mac_10g.sdc`, and reference it through the new Target's fileset with `file_type: SDC`. No existing Taxi file is changed. [The documented synthesis recipe](../../docs/user/CONFIG.md) requires explicit Target-owned SDC for physical synthesis and adds no generated clocks.

The [pinned MAC](https://github.com/fpganinja/taxi/blob/cc70b270b910d369ab1ad7b3855e76399fd461f1/src/eth/rtl/taxi_eth_mac_10g.sv) exposes `rx_clk`, `tx_clk`, `stat_clk`, `ptp_clk`, `ptp_sample_clk`. The [pinned testbench, lines 49–73](https://github.com/fpganinja/taxi/blob/cc70b270b910d369ab1ad7b3855e76399fd461f1/src/eth/tb/taxi_eth_mac_10g/test_taxi_eth_mac_10g.py#L49) supplies 6.4 ns for the first four under 64-bit operation (156.25 MHz), and 8 ns for `ptp_sample_clk` (125 MHz). Freeze these as the physical regression constraint fixture:

```tcl
create_clock -name rx_clk         -period 6.4 [get_ports rx_clk]
create_clock -name tx_clk         -period 6.4 [get_ports tx_clk]
create_clock -name stat_clk       -period 6.4 [get_ports stat_clk]
create_clock -name ptp_clk        -period 6.4 [get_ports ptp_clk]
create_clock -name ptp_sample_clk -period 8.0 [get_ports ptp_sample_clk]
# Fixture-only zero external delay, deliberately conservative across clocks.
# Exclude clock ports from external data-input delay application.
set qa_data_inputs [remove_from_collection [all_inputs] \
    [get_ports {rx_clk tx_clk stat_clk ptp_clk ptp_sample_clk}]]
foreach qa_clock_name {rx_clk tx_clk stat_clk ptp_clk ptp_sample_clk} {
    set_input_delay -clock [get_clocks $qa_clock_name] -add_delay 0.0 $qa_data_inputs
    set_output_delay -clock [get_clocks $qa_clock_name] -add_delay 0.0 [all_outputs]
}
```

This is a reproducible clocked-block regression fixture, not board integration/signoff. Do not change the explicit zero external-delay assumption, blank out clocks, tie enabled PTP/statistics away, or add broad false paths to obtain a pass. The approved fixture assumes zero external input/output delay against each named clock, using explicit `-add_delay` to retain all clock-relative constraints. This is a conservative relative-comparison fixture, not a board interface budget. No asynchronous-clock exemption is implied. Record timing-path coverage and any remaining unconstrained paths explicitly. Common phase for the four equal-period clocks follows the testbench's startup pattern as a regression modeling choice, not a claim that production clocks are phase-related. Any change to clock relations, CDC exceptions or I/O timing requires a reviewed constraint-fixture amendment. Retain the original unqualified 0% timing gate semantics; do not silently strengthen it to five per-clock percentage gates. Require valid per-clock timing coverage for every clock that survives the complete enabled-design synthesis, and prove why any absent clock was optimized out instead of silently accepting missing timing.

### Setup prompt addition

Append this exact authorized addition to the accepted Setup-agent prompt when creating the versioned fixture; preserve the original prompt's full workload:

> Also configure persistent `synth_mac_10g_physical` for the same complete `taxi_eth_mac_10g` source closure and every approved parameter, using slang/Yosys followed by OpenROAD physical synthesis, explicit balanced PPA profile and flattening. Author and select the provided five-clock SDC in Project-owned configuration without editing existing Taxi files. Keep `synth_mac_10g` logical. Include both synthesis Targets in CLI/MCP discovery and appropriate deep Doctor checks. Preserve physical tool/library/recipe identities, SDC hash and clock/path coverage. Do not replace missing physical timing evidence with the logical frequency estimate, alter clock periods, disable enabled hardware, add timing waivers or replace the provided zero external-delay fixture with invented board I/O constraints.

### Ordered work and Criteria amendment

1. Setup creates and validates the fourth Target and five-clock SDC. Discovery/refresh checks cover all four Targets; explicit source and parameter equivalence compares logical/physical Targets.
2. The unchanged continuity phase runs fresh logical **and physical** synthesis before Interactive Mode. Archive physical reports and artifacts alongside the existing baseline; they belong to the same live run.
3. Verification Ticket1's implementation Scope and Criteria remain unchanged. The new physical Target is already Project-owned, so it is available in the accepted state and the later seeded Basis. Preserve it through mutation restoration.
4. Bug Fix Ticket2 retains required logical `synth_mac_10g` with 0% cell increase. Move its impossible logical critical-path comparison to the new physical Target, and add a directed Acceptance Basis/candidate `synth_mac_10g_physical` `synthesis_ok` Criterion with **`cell_count_increase_at_most: 0%` and `critical_path_ps_increase_at_most: 0%`**. Both sides use the seeded Ticket Basis/candidate as usual; do not substitute the earlier clean continuity result for the declared Basis measurement. The repair Scope stays the one RTL file. No Target Plan change or new hardware edit is authorized by adding this already-existing Target to Criteria.
5. Final regression reruns both logical and fresh physical synthesis against repaired merged RTL. Archive result, normalized metrics, timing directory and source/constraint identities before cleanup; remove the additional run-owned Target/SDC with the Project.

The whole run remains eight hours. Physical work runs within the existing approved baseline, Ticket2 and final-regression phase ceilings (with the Setup addition inside its Setup ceiling); no image preparation, baseline or comparison is moved outside the clock, and no old artifact supplies a fresh required pass. Time exhaustion blocks missing physical work and makes qualification incomplete unless a trustworthy failure already makes it failed.

### Additional checks selected centrally for both Taxi core platforms

| Check ID | Selection | Stimulus and expected observation | Evidence / capture point |
|---|---|---|---|
| `taxi-10g-mac-port-evolution.physical.target` | Core | Resolve new physical Target; prove same full MAC sources, top and approved parameters as logical Target; explicit slang/physical/balanced/flatten configuration. | Both resolved recipes, source hashes and parameter comparison during Setup. |
| `taxi-10g-mac-port-evolution.physical.constraints` | Core | Load Project-owned SDC; create four 6.4ns clocks and one 8ns clock on exact ports. No silent timing exception or parameter change. | SDC bytes/hash, timing engine clock inventory and consumed files. |
| `taxi-10g-mac-port-evolution.physical.recipe-identity` | Core | Baseline/candidate/final use same selected library/technology and physical recipe. | Exact image/tool/library/recipe identities and pair comparison. |
| `taxi-10g-mac-port-evolution.physical.discovery-doctor` | Core | Include fourth Target in CLI/MCP identity checks and deep Doctor. | Discovery comparison and warning-free Doctor results. |
| `taxi-10g-mac-port-evolution.physical.clean-baseline` | Core | Fresh physical run on unchanged baseline completes with artifact-backed physical timing/structural/PPA evidence. | Normalized completion fields, placement/optimization/STA artifacts and source identity. |
| `taxi-10g-mac-port-evolution.physical.clock-coverage` | Core | Validate surviving clock timing coverage and expose unconstrained I/O/path limits; no fabricated missing-clock pass. | Per-clock report, optimized clock-net evidence where applicable, unconstrained-path report. |
| `taxi-10g-mac-port-evolution.physical.ticket2-cells` | Core | Directed seeded Acceptance Basis/candidate physical pair has cell-count increase at most 0%. | Fresh paired cells, Basis identity and exact Criterion comparison. |
| `taxi-10g-mac-port-evolution.ticket2.synthesis-path` | Core | Same physical pair has critical-path increase at most 0% using published unqualified timing metric semantics. | Fresh paired physical `per_clock` timing, selected representative metric and exact comparison. |
| `taxi-10g-mac-port-evolution.physical.final` | Core | Rerun successful fresh physical synthesis on final repaired merged state. | Physical result/artifacts, source/SDC/library identities and timing coverage. |
| `taxi-10g-mac-port-evolution.physical.archive-cleanup` | Core | Preserve physical reports/timing artifacts before deleting run-owned Target/SDC/Project. | Archive manifest and configuration/resource cleanup proof. |

## Exact accepted journey source annex

Source snapshot: [Design the Taxi 10G MAC port-and-evolution scenario](https://github.com/boldaxolotl/booley/issues/377#issuecomment-5572764904). The shared-contract amendment and the approved handoff decisions take precedence over historical bookkeeping and budgets in this annex. Original workload text is retained for exact prompt/Ticket extraction.

## Resolution

Scenario ID: `taxi-10g-mac-port-evolution`.

This scenario begins with a fresh clone of the pinned Taxi repository and exercises Booley Project Initialization and the complete plan-first Project Setup workflow before it performs any continuity or evolution work. It then proves one representative 10G MAC configuration against the unchanged upstream implementation, exercises Interactive Mode and native-Verilator-FST analysis, adds a persistent observability regression through a Verification Ticket, and uses that regression to diagnose and repair an authorized RTL fault through a Bug Fix Ticket.

The clean setup and continuity baseline never edit Taxi RTL, upstream testbenches, manifests, symlinks, or repository metadata. The later RTL edit is separately authorized by the versioned scenario. No branch is pushed.

### Frozen inputs and Run Declaration

Every run freezes and records:

- Upstream repository: [`fpganinja/taxi@cc70b270b910d369ab1ad7b3855e76399fd461f1`](https://github.com/fpganinja/taxi/tree/cc70b270b910d369ab1ad7b3855e76399fd461f1).
- The exact published Booley release, package hash, release-documentation identity, Scenario Protocol version, suite commit and digest, and selected base Session Image digest. A local wheel, editable install, development version, floating Booley version, or import from a Booley source checkout is forbidden.
- A unique Scenario Run ID, provider/platform profile, eight-hour absolute deadline, granted authority, selected credentials mechanism without secret values, run-owned branch, artifact root, and cleanup ledger.
- The clean pinned repository identity before Project Initialization, the generated Project-derived Session Image digest and provenance, and the final repository identities at every checkpoint.

The scenario uses a disposable direct clone of Taxi. It does **not** create a consumer wrapper repository. Taxi contains no git submodules; `src/eth/lib/taxi` is a committed repository-internal symlink to `../../../`. Project Setup must resolve the real source paths without modifying the symlink or relying on it being materialized as a functional host symlink on Windows.

Host Bootstrap is a precondition, not a Taxi exercise; the OpenTitan scenario owns automatic Host Bootstrap coverage. Taxi exercises `booley init` and Project Setup from the uninitialized clone.

### Representative hardware and test configuration

Use only the complete `taxi_eth_mac_10g`, with this fixed configuration:

```yaml
DATA_W: 64
TX_GBX_IF_EN: 0
RX_GBX_IF_EN: 0
GBX_CNT: 1
DIC_EN: 1
PTP_TS_EN: 1
PTP_TD_EN: 1
PTP_TS_FMT_TOD: 1
PTP_TS_FNS_W: 16
PTP_TS_W: 96
PTP_TD_SDI_PIPELINE: 2
TX_TAG_W: 16
PFC_EN: 1
PAUSE_EN: 1
STAT_EN: 1
STAT_TX_LEVEL: 2
STAT_RX_LEVEL: 2
STAT_ID_BASE: 0
STAT_UPDATE_PERIOD: 1024
STAT_STR_EN: 1
STAT_PREFIX_STR: MAC
```

Gearbox mode is explicitly disabled. This follows the pinned pytest driver rather than the Makefile default and is the representative standalone-MAC configuration. The full upstream module contains seven Cocotb functions: RX, TX, TX alignment, TX underrun, TX user error, LFC, and PFC. The RX and TX functions retain their IFG 12 and IFG 0 parameterizations.

### Project Setup contract

One delegated Setup agent performs Project Initialization and the complete Project Setup sequence. The versioned scenario supplies every decision and the Run Declaration grants the corresponding authority. The agent is not asked to seek or manufacture a second approval. `SETUP-PLAN.md` remains an evidence artifact describing what it did, not a new runtime authorization gate.

The Setup-agent intent is:

> Set up this fresh pinned Taxi checkout as a Booley Project. All design decisions in this prompt are approved; complete planning and execution without requesting further approval. Do not modify any existing Taxi RTL, testbench, manifest, symlink, or repository metadata. Create a Project-derived image from the complete pinned `tox.ini` dependency set. Configure `sim_mac_10g`, `lint_mac_10g`, and `synth_mac_10g` for the specified 64-bit PTP/PFC/statistics configuration, using Verilator Cocotb with native FST, Verible, and logical Yosys with slang respectively. Resolve source paths without depending on `src/eth/lib/taxi` being a functional host symlink. Register all seven upstream Cocotb tests, explicitly disable Stealth Mode, and finish only after plain and deep Doctor are warning-free. Retain the Setup Plan, configuration diff, image identity, Target inventory, Doctor reports, and every deviation.

The agent owns the exact `.core`, requirements-file, and configuration spelling. The following outcomes are mandatory:

- `sim_mac_10g` uses the upstream `test_taxi_eth_mac_10g.sv` wrapper and `test_taxi_eth_mac_10g.py` Cocotb module, the full canonicalized source closure from `taxi_eth_mac_10g.f`, Verilator, all seven test-function registrations, and native `trace.fst` generation for traced runs.
- `lint_mac_10g` drives Verible over the relevant SystemVerilog source set.
- `synth_mac_10g` drives logical Yosys with the slang frontend and `taxi_eth_mac_10g` as its top. It does not claim STA or tape-out significance.
- All three Targets use unambiguous identities, carry the agreed fixed parameter values where applicable, and are selected for the appropriate deep-Doctor checks.
- `[stealth] enabled = false` is explicit; omission is not equivalent.
- The Project-derived image contains the complete pinned Taxi test dependency set: pytest 8.3.4, pytest-xdist 3.6.1, pytest-split 0.10.0, cocotb 2.0.1, cocotb-bus 0.3.0, cocotb-test 0.2.6, cocotbext-axi 0.1.28, cocotbext-eth 0.1.28, cocotbext-i2c 0.1.2, cocotbext-pcie 0.2.16, cocotbext-uart 0.1.4, and scapy 2.6.1. Runtime network installation is forbidden. The real testbench import path is probed inside the Session Runtime.
- CLI and MCP Target discovery agree on identity, selectors, EDA programs, toplevels, parameters, and resolved source inputs.
- Project Setup uses only published setup documentation, packaged skills, cheat sheets, CLI/MCP help, and ordinary Project inspection until an observation requiring source verification has been captured.

### Provider and platform profiles

The regular initial qualification has two mandatory lanes:

| Profile | Required coverage |
|---|---|
| Ubuntu 24.04 x86-64 + Codex | Complete Project Initialization and Setup, both execution modes, both Tickets, every applicable Flow/Specialist/evidence assertion, and cleanup. |
| Native Windows x86-64 + Docker Desktop/WSL2 + Codex | The same complete run. The host CLI is native Windows and the Session Runtime is Linux. Setup must avoid dependence on Taxi's checked-out symlink representation. |

Taxi has no initial Claude lane; the PicoRV32 scenario remains the representative Claude compatibility run. The scenario definition remains provider-neutral and includes the Claude Ticket Create spelling for future qualification. No Windows/Linux performance-equivalence assertion is made.

Semantic Interactive Mode, FST, B-Wave, and Waveform Viewer state assertions are mandatory on both lanes. The visual screenshot assertion is capability-gated and currently expected `not runnable` when no qualified observer exists; it never converts the semantic assertions to pass.

### Ordered scenario

1. **Prepare and freeze.** Install and identify the exact published Booley release, create the Run Declaration and external artifact root, clone the pinned Taxi revision, prove the clone clean, and create the run-owned local branch. Record host prerequisites, provider/auth status without secret values, and cleanup ownership.
2. **Initialize and set up Taxi.** Run `booley init` and delegate the complete Setup-agent intent above. Build and select the Project-derived image, enter the Session Runtime, author the Project configuration and guidance, and retain the Setup Plan and all generated-state identities. Existing Taxi files remain byte-identical to the pinned tree.
3. **Pass the Doctor gate.** Run plain Doctor, deep Doctor, and a final plain recheck. Every active warning and failure must be resolved or recorded as a failure; this scenario does not use a waiver to manufacture a clean result.
4. **Prove Project and Target resolution.** Compare CLI and MCP inventory, verify the canonicalized source closure and representative parameters, exercise a fully qualified Target selector, and prove that changing Project configuration refreshes discovery without touching Taxi sources. Record the symlink representation on each host and the source paths actually consumed.
5. **Prove clean continuity.** Run the complete seven-function `sim_mac_10g` Cocotb module untraced, retaining full `results.xml`, JSON, logs, resolved files, and tool identities. It must cover normal and jumbo frames, IFG 12 and 0, DIC alignment, good FCS, TX underrun, TX user error, LFC/PFC frame behavior, and RX/TX timestamps. Run Verible lint and fresh slang/Yosys logical synthesis and retain artifact-verified reports.
6. **Exercise Interactive Mode.** Launch one long-lived Interactive Mode child in the same Session Runtime and give it the prompt below. Capture its identity, prompt hash, MCP invocations, Specialist report, and all artifacts. Restore and verify the clean post-Interactive checkpoint.
7. **Create the Verification Ticket.** Invoke Ticket Create Agent Mode with the complete `Strengthen 10G MAC observability` payload and no confirmation. Capture the invocation, prompt hash, Ticket path, Scope, Criteria, Target Plan, Acceptance Basis identity, and queued Board state.
8. **Run the Verification Ticket.** Run it as a distinct `booley run` call. Require direct acceptance, merge, Ticket Workspace cleanup, triage-report generation, the persistent observability Target, and the mandatory 7-of-8 mutation result.
9. **Record the clean evolution checkpoint.** Run the complete upstream and observability simulations against pristine Taxi RTL, record the accepted Target and commit identities, and freeze this checkpoint before fault injection.
10. **Inject and prove the RTL fault.** Make the one authorized PFC-routing change described below and commit it on the run-owned branch. Run the original PFC test and the observability test. Require the original count-only test to pass and the new exact class/quanta assertion to fail with the declared signature and a fresh FST. If that split does not occur, restore the clean evolution checkpoint and do not create the Bug Fix Ticket.
11. **Create the Bug Fix Ticket.** Invoke Ticket Create Agent Mode with the complete `Repair PFC priority routing` payload, the seeded commit as its basis, and the completed Verification Ticket as a dependency. Capture the same creation and Acceptance Basis evidence as above.
12. **Run the Bug Fix Ticket.** Run it as its own `booley run` call. Require exact reproduction, evidence-driven diagnosis, repair, Criteria completion, direct acceptance, merge, cleanup, and triage-report generation.
13. **Final regression.** Re-run plain and deep Doctor, the complete upstream and observability simulations, Verible lint, fresh slang/Yosys logical synthesis, and the repaired PFC B-Wave checks. Prove all pre-existing Taxi RTL and testbench bytes match the pinned revision and only the accepted scenario-owned verification assets remain as content changes.
14. **Archive and clean up.** Finalize the Scenario Run Record and derived Consolidate Findings handoff, archive the evidence described below outside the disposable clone, reconcile every resource, and remove all run-owned state.

### Interactive Mode contract

The Scenario Operator launches one long-lived Interactive Mode child inside the setup-complete Taxi Session Runtime with the Project working directory and MCP access preserved. It receives:

> Work read-only in the setup-complete pinned Taxi Project. Confirm the Session Runtime identity, repository cleanliness, Doctor state, and available Target identities. Compare CLI and MCP Target discovery and inspect the resolved build inputs. Run the focused PFC test on `sim_mac_10g` with a fresh native FST trace. Exercise the complete agreed B-Wave semantic surface, persistent aliases and markers, and scoped Waveform Viewer state using PFC request, packet-start, XGMII, timestamp, and statistics signals. Invoke a TB-quality Reviewer against `sim_mac_10g`. Preserve every structured report and artifact. Do not edit files, create commits, weaken checks, or push anything.

The FST exercise must:

- Prove a fresh, nonempty, identity-bound native FST with scope, signal-count, size, and tick metadata.
- Register a named alias and `_last`, prove the named alias after a new command context, create/list/resolve/delete named markers, and reject a stale or missing registration.
- Use `list`, `signal`, `wave`, `value`, `find`, `sample`, `diff`, `distance`, `stats`, and `stuck` semantically against known PFC request, frame-start, XGMII data/control, timestamp, reset, and statistics relationships.
- Cover synchronous and asynchronous views, explicit clock/reset selection, cycle and typed physical-time tokens, and one request-to-frame latency cross-checked against the Cocotb observation rather than trusted from CLI return code alone.
- Open a scoped Waveform Viewer state containing the clock, PFC request, packet-start, XGMII data/control, and relevant statistics signals, with start/end markers and cursor. Retain WCP readback. Attempt visual capture only when the pre-run capability probe proves a qualified observer.

PicoRV32 owns the virtual-signal option acceptance/rejection matrix; Taxi does not duplicate it.

### Ticket Create invocations

The scenario is the source of both complete Ticket contracts. Ticket Create must not infer omitted semantics:

- Codex form: `$booley-ticket-create --agent --no-confirm <complete structured scenario payload>`
- Claude-compatible form: `/booley-ticket-create --agent --no-confirm <complete structured scenario payload>`

Both Tickets use:

```yaml
on_success:
  destination: done
  merge: true
  cleanup: true
  triage_report: true
priority: medium
```

Only one automatic retry is permitted, with `max_attempts: 1`, and only for the exact recognized error `API Error: Response stalled mid-stream`. Design failures, mutation survivors, timeouts, crashes, context exhaustion, and usage-limit failures are not automatically retried.

### Ticket 1 — `strengthen-10g-mac-observability`

Title: **Strengthen 10G MAC observability**. Type: `verification`.

Scope is limited to new scenario-owned files:

```yaml
- qa/taxi_eth_mac_10g/test_observability.py [new]
- qa/taxi_eth_mac_10g/test_observability.sv [new]
```

Ticket creation authors this Target Plan entry and its owned test registrations:

```yaml
target_plan:
  - target: sim_mac_10g_observability
    role: persistent
```

The persistent Target reuses the Setup-approved 64-bit, gearbox-disabled, DIC/PTP/PFC/statistics source and parameter contract, uses the new thin wrapper as toplevel and new Cocotb module as its testbench, and remains selectable after acceptance.

The Ticket must implement these deterministic tests:

- **Bad RX FCS and statistics.** Corrupt a received XGMII frame's FCS without changing its payload stimulus; require RX error `tuser`, the discrete bad-FCS indication, and counter record ID 34.
- **Exact PFC class and quanta.** Use an enable vector covering all eight classes and distinct quanta `[10, 20, 30, 40, 50, 60, 70, 80]`; verify the exact transmitted and received class bitmap and each quanta value, not merely the number of MAC control frames. Require the relevant PFC counter records, including IDs 25 and 57.
- **Underrun statistics.** Reproduce the deterministic four-cycle source pause and require the XGMII error termination plus counter record ID 3.
- **TX completion identity.** Send uniquely tagged frames and verify every completion returns the correct 16-bit tag and the timestamp relationship to the observed SFD.
- **Statistics stream typing.** Correctly distinguish `tuser=0` counter records from `tuser=1` string records and verify the expected ID namespace; do not count strings as counters.

Mandatory Criteria are successful Elaboration Check and complete Simulation for `sim_mac_10g_observability`, a clean TB-quality review bound to that Target, and this fixed mutation campaign:

```yaml
mutation_score:
  - target: sim_mac_10g_observability
    scope: [src/eth/rtl/taxi_eth_mac_10g.sv]
    total: 8
    min_detected: 7
```

Mutation proposal steering targets PFC request/ack routing, RX-error propagation, statistics enablement/output, and TX completion timestamp/tag wiring. Specialist Source Isolation must hide the new Cocotb and wrapper sources from the Mutation Tester while it proposes mutations. Preserve the proposal lock, pristine baseline, every isolated mutant result, source variant, first-killing-test evidence, restoration proof, and atomic manifest. One survivor may be reported; fewer than seven detected mutants fails the mandatory Criterion.

The Ticket may not edit any pre-existing Taxi file or weaken an upstream test. Its accepted result is the new `qa/taxi_eth_mac_10g/` verification surface and persistent Target.

### Seeded RTL fault

After Ticket 1 is accepted and the clean evolution checkpoint is frozen, the Scenario Operator receives explicit authority to change only `src/eth/rtl/taxi_eth_mac_10g.sv`. At the `taxi_mac_pause_ctrl_tx` connection, replace:

```systemverilog
.tx_pfc_req(tx_pfc_req)
```

with:

```systemverilog
.tx_pfc_req({tx_pfc_req[6:0], tx_pfc_req[7]})
```

This rotates each requested priority by one while preserving the aggregate presence and count of PFC frames. The clean baseline and new oracle must pass before injection. After injection, the upstream count-only PFC test must still pass, while the new exact class/quanta test fails with class 0 observed as class 1 and a corresponding FST relationship. Capture the seed commit, exact diff, both results, trace, B-Wave queries, and repository cleanliness.

The fault location and repair are hidden from the Developer Agent. The agent receives the failing assertion, expected and observed class behavior, upstream-test contrast, Scope, and evidence pointers.

### Ticket 2 — `repair-pfc-priority-routing`

Title: **Repair PFC priority routing**. Type: `bugfix`. Dependency: **Strengthen 10G MAC observability**.

Scope:

```yaml
- src/eth/rtl/taxi_eth_mac_10g.sv
```

Required behavior:

- Reproduce the exact class-rotation failure on a fresh run and trace.
- Diagnose the routing defect using the provided evidence plus ordinary Project inspection; do not rely on the hidden seed description.
- Repair the RTL without changing tests, Criteria, Project configuration, unrelated RTL, or accepted verification assets.
- Preserve the complete upstream regression, exact PFC/quanta behavior, statistics behavior, timestamp/tag behavior, Verible cleanliness, and logical synthesis quality.
- Leave the repaired `taxi_eth_mac_10g.sv` byte-identical to the pinned upstream revision.

Mandatory Criteria:

- Elaboration Check for `sim_mac_10g` and `sim_mac_10g_observability`.
- Complete upstream `sim_mac_10g` remains pass-to-pass.
- `sim_mac_10g_observability` records the seeded fail-to-pass transition.
- `lint_clean` for `lint_mac_10g`.
- `synthesis_ok` for the directed Acceptance Basis/candidate pair of `synth_mac_10g`, with `cell_count_increase_at_most: 0%` and `critical_path_ps_increase_at_most: 0%`.
- Clean RTL bugs review, plus terminal RTL protocol and RTL specification reviews.

The Ticket must not weaken or skip tests, alter the Target contract, hide the failure, add waivers, or push a branch.

### Failure and recovery

The shared Scenario Protocol remains authoritative. Taxi adds these scenario-specific rules:

- If Project Setup, the Project-derived image import probe, or Doctor cannot become clean within the step budget, preserve the original observation and bounded recovery attempts; do not proceed into continuity work with untrusted configuration.
- If Ticket 1 fails or its mutation campaign detects fewer than seven of eight mutants, do not inject the RTL fault. Preserve its evidence, run the original baseline where still trustworthy, and clean up.
- If the injected fault does not produce the exact original-pass/new-oracle-fail split, restore the clean evolution checkpoint and do not create Ticket 2.
- If Ticket 2 fails, preserve its worktree, report, and diff evidence before restoring the clean post-Ticket-1 checkpoint for cleanup verification. That restored Project never converts the failed repair assertion into a pass.
- An unexpected failure prunes only dependent work. Independent documentation, platform, evidence-finalization, and cleanup assertions continue while their prerequisites remain trustworthy.
- One failed attempt, retry, workaround, or restored checkpoint never overwrites the original result. Source inspection beyond the clean-room public path is permitted only after the original observation is captured and solely to verify or classify it.

### Evidence and checkpoints

Use one append-only Scenario Run Record plus immutable referenced artifacts. Scenario-specific evidence includes the Setup Plan and prompt hash; Project Initialization and configuration diffs; pinned requirements and image provenance; Target inventories and resolved files; Doctor JSON/logs; Cocotb XML/JSON; Verible and Yosys reports and artifacts; native FST metadata; every B-Wave command, expected semantic observation, and output; WCP state; Reviewer and mutation manifests; Ticket, Acceptance Basis, Board-transition, branch, worktree, and commit identities; seed and repair diffs; exact upstream-file hashes; deviations; Findings; retry decisions; and cleanup entries. Secret values are never captured. Findings remain direct, unredacted internal QA records for Consolidate Findings; Booley Feedback is not invoked.

Required checkpoints are:

1. Frozen release, Run Declaration, pinned clean clone, and run-owned branch.
2. Project-derived image and completed Project Setup.
3. Warning-free plain/deep/final-plain Doctor gate.
4. Full clean continuity baseline.
5. Restored post-Interactive state.
6. Ticket 1 accepted with persistent Target and 7-of-8 mutation evidence.
7. Clean post-Ticket-1 evolution baseline.
8. Seeded RTL fault reproduced with the exact split and fresh FST.
9. Ticket 2 accepted with byte-identical restored RTL.
10. Complete final regression.
11. Evidence archive and cleanup complete.

Resume revalidates the Run Declaration, checkpoint digest, repository identities, persistent Target, evidence pointers, image, and owned-resource state. Drift is recorded and restored within the bounded recovery budget; it never silently changes the run identity.

### Deadline

The absolute run deadline is eight hours. Budget: preparation, Project Initialization, image build, and Project Setup 90 minutes; Doctor and clean continuity 75 minutes; Interactive Mode and FST/B-Wave work 60 minutes; Ticket 1 including mutation testing 105 minutes; fault proof and Ticket 2 105 minutes; final regression 30 minutes; cleanup 15 minutes; contingency 30 minutes. At 7h30, start final regression or cleanup and begin no new work. Deadline exhaustion produces a finalized partial record and cleanup attempt; it never silently drops assertions.

### Final regression and cleanup

Before cleanup, require warning-free plain and deep Doctor; passing complete upstream and observability simulations; clean Verible lint; fresh successful slang/Yosys logical synthesis; repaired B-Wave PFC relationships; clean repositories; byte identity between every pre-existing Taxi RTL/testbench file and the pinned revision; and only the accepted `qa/taxi_eth_mac_10g/` assets plus their persistent Target as intentional content changes.

Archive the append-only run record, derived human summary and Consolidate Findings handoff, logs, XML/JSON reports, FSTs, B-Wave/WCP evidence, mutation manifest and variants, Ticket and Acceptance Basis evidence, image provenance, file hashes, diffs, commit identities, and a git bundle outside the disposable clone. Then remove every run-owned Session Runtime, process, worktree, local branch, Project Inventory entry, Project-derived image, volume or mount, inner Project repository, and disposable clone. Preserve credentials, Booley-owned base images, caches, and all pre-existing host state. Remotes remain unchanged and nothing is pushed.

Incomplete mandatory cleanup prevents a pass and remains separately visible from product failures.

### Coverage-allocation consequence

This design intentionally uses a direct Taxi clone because that is the authentic existing-IP Project Setup journey. Taxi has no git submodules, so this scenario does **not** satisfy offline submodule-reconstruction coverage. [Assemble the public Booley QA suite implementation handoff](https://github.com/boldaxolotl/booley/issues/376) must reconcile that atomic obligation across the final portfolio or expose it as an unsatisfied sufficiency gap. The suite must not manufacture a consumer repository merely to claim the cell.

This resolution is the implementation-ready handoff for **Design the Taxi 10G MAC port-and-evolution scenario**. Encoding or executing it remains outside this Wayfinder ticket.

