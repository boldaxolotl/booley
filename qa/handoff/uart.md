# OpenTitan UART clean-room greenfield handoff

Planning artifact; no execution or coverage credit is claimed. Source: [Design the OpenTitan UART clean-room greenfield scenario](https://github.com/boldaxolotl/booley/issues/375#issuecomment-5581155601), amended by that issue's current body; portfolio source: [Choose the initial IP portfolio and allocate coverage](https://github.com/boldaxolotl/booley/issues/254#issuecomment-5553153331). Shared behavior is [Protocol](../PROTOCOL.md), [Format](../FORMAT.md) and [Qualification](../QUALIFICATION.md). The accepted detailed contract is retained verbatim below for exact payloads/assets; its old shared mechanisms are superseded by those shared files and the amendment.

## Profile selection and IDs

Every ID below is fully qualified by `opentitan-uart-clean-room-greenfield.`. Tables print the full IDs. All non-GUI checks are selected by required Ubuntu 24.04 x86-64/Codex and native Windows x86-64 Docker Desktop/WSL2/Codex **core** runs, with both Interactive and Ticket modes. Conditional repair checks apply only following actual independent mismatch; the absence of a trigger is recorded with evidence and never forces a repair. The external-image slice is required supporting work with its own capability probe; absence leaves its required profile incomplete. Claude remains portable specification, unrun and unqualified for this scenario; the portfolio's representative Claude run belongs to PicoRV32.

GUI/client profiles select the explicit GUI checks below plus their complete same-run prerequisites from the core tables. Actual VS Code integration cannot be inferred from semantic MCP work. Unavailable GUI driver/observer leaves GUI incomplete independently of core. This planning selection paragraph becomes explicit ID lists in profiles.yaml; production checks must not duplicate membership.

Within each table, the named source section is from the accepted linked UART resolution, supplemented by the frozen register/behavior corpus. Every row supplies stimulus and expectation/evidence; evidence is captured when the stimulus completes before state mutation. The shared recovery text immediately preceding each table applies to each listed check. Generated UART case families must expand to concrete register/field/value/boundary IDs while retaining every listed requirement. They are inventories for implementation, not executable evaluator code.

## Assets and timing constraints

Pin `lowRISC/opentitan@615d3c74fadbbf674c8ca05a70f91094989849fb` (`earlgrey_1.0.0`). The exact seven documentation assets, interface contract, original Interactive prompt, Feature payload, Criteria, public evaluator families and precedence are in the retained contract below. Proposed packaged asset paths: `assets/corpus/` plus SHA-256/provenance manifest, `assets/qa_uart.sv` interface-only stub, `assets/addendum.md`, `assets/interactive-prompt.md`, `assets/feature-ticket.md`, `assets/repair-ticket.md`, and operator-held evaluator/case-manifest assets outside Project/runtime. No evaluator implementation is supplied here.

The accepted schedule already totals **480 minutes**: preparation/bootstrap 45; Setup/Interactive 45; Feature 180; evaluator controls/initial evaluation 30; repairs 90 (two × 45, each including complete rerun); external image 15 including cleanup; final regression/archive 30; contingency 15; final cleanup 30. Cleanup begins at 450 minutes. No work is removed; feasibility is unproven until execution. Unused repair time can support declared work but never a third repair. Phase recovery names retain `prepared`, `scaffolded`, `setup-green`, `mmio-green`, `feature-accepted`, `evaluation-0/1/2`, `external-image-checked`, `final-evidence`, `cleanup-complete`; they do not implement general resume.

## Concrete check inventory

### Preparation and allowed inputs

Source: UART resolution, **Shared contract and frozen inputs; Documentation-only corpus**. Recovery: Stop dependent work on missing identity or source contamination; preserve evidence, restrict access again, and perform cleanup. Independent documentation checks may continue.

| Proposed check ID | Stimulus | Expected observation and retained evidence |
|---|---|---|
| `opentitan-uart-clean-room-greenfield.published-release` | Install/identify the exact published Booley package. | Package hash/version and matching public documentation identify the release; no development build, editable install, local wheel or Booley-source execution/import. |
| `opentitan-uart-clean-room-greenfield.run-inputs` | Freeze run manifest before product work. | Suite/protocol, provider/client/backend, platform, initial image, corpus/interface/evaluator digests, seed, owned repositories/branches, deadline and secret-free credential mechanism recorded. |
| `opentitan-uart-clean-room-greenfield.delegation` | Assign setup, development, evaluation, diagnostics and cleanup. | Recorded delegate identities perform actual work; coordinator sequences and integrates evidence. |
| `opentitan-uart-clean-room-greenfield.authority` | Declare edits, commits, Ticket creation and local acceptance before execution. | No live run approval, push, external report or permanent Booley repair; resource actions stay within declaration. |
| `opentitan-uart-clean-room-greenfield.empty-origin` | Create owned empty repository on disposable host/account. | Initial tree and host/Docker state prove empty origin and absent managed state for fresh-bootstrap claims; no deletion of others' resources. |
| `opentitan-uart-clean-room-greenfield.corpus-pin` | Package Earl Grey documentation from 615d3c74fadbbf674c8ca05a70f91094989849fb. | Provenance and SHA-256 for each of the seven exact allowed files; Apache-2.0 attribution retained. |
| `opentitan-uart-clean-room-greenfield.source-boundary` | Inspect material accessible to Developer. | Only frozen corpus, interface stub/addendum, published Booley docs/help/skills and ordinary Project; no HJSON/regtool/RTL/tests/UVM/DIF/model or recursive implementation/current-doc navigation. |
| `opentitan-uart-clean-room-greenfield.doc-precedence` | Resolve conflicting documentation during development. | Addendum precedes register definitions/precise behavior, then examples; 64-byte RX and 32-byte TX remain normative. |
| `opentitan-uart-clean-room-greenfield.doc-rx-depth` | Compare contradictory RX prose to normative contract. | Finding retains exact pinned source and 32-byte conflict; no inferred RTL defect. |
| `opentitan-uart-clean-room-greenfield.doc-tx-overflow` | Inspect undocumented TX-overflow interrupt example. | Documentation Finding retains example and inconsistency with public interrupt definitions. |
| `opentitan-uart-clean-room-greenfield.doc-watermark-shift` | Inspect RX watermark shift-by-three example. | Finding identifies incorrect shift and zero-valued threshold masking it. |
| `opentitan-uart-clean-room-greenfield.interface-only` | Supply qa_uart stub. | Stub contains only clock/reset, single-outstanding 32-bit MMIO, RX/TX and nine IRQ ports in INTR_STATE 0–8 order; no implementation/tests. |

### Bootstrap, Setup and Interactive baseline

Source: UART resolution, **Project, modes and Ticket payloads; Ordered steps and checkpoints**. Recovery: Preserve failed observations. Restore only owned state within this live run. Dependent phases require a newly demonstrated baseline; cleanup remains runnable.

| Proposed check ID | Stimulus | Expected observation and retained evidence |
|---|---|---|
| `opentitan-uart-clean-room-greenfield.bootstrap-pending` | Run Host Bootstrap check-only before reconciliation. | Pending state observed and retained. |
| `opentitan-uart-clean-room-greenfield.automatic-bootstrap` | Invoke booley init --scaffold without prior explicit bootstrap. | Automatic reconciliation succeeds; command/provider/options and resulting managed state recorded. |
| `opentitan-uart-clean-room-greenfield.bootstrap-clean` | Repeat bootstrap check-only after scaffold. | Clean state observed. |
| `opentitan-uart-clean-room-greenfield.init-idempotence` | Repeat init. | No unintended drift in source, config, generated state, images or packaged integrations. |
| `opentitan-uart-clean-room-greenfield.scaffold-assets` | Inspect generated core, counter, skills, standard image and Stealth state. | Selected provider, Verilator simulator/linter, SystemVerilog TB and ASIC support match advance choices; generated baseline retained. |
| `opentitan-uart-clean-room-greenfield.stealth-disabled` | Set [stealth] enabled = false and inspect Project. | Ordinary source/core locations and benign literal Project-identifying commit messages remain visible; no Stealth-on claim. |
| `opentitan-uart-clean-room-greenfield.setup-new` | Run packaged /booley-setup new inside Session Runtime. | Design-aware Setup completes from generated counter; allowed-source and skill invocation evidence retained. |
| `opentitan-uart-clean-room-greenfield.setup-doctor-plain` | Run plain Doctor on generated Project. | Baseline diagnostic output and identities retained; expected clean state. |
| `opentitan-uart-clean-room-greenfield.setup-doctor-deep` | Run deep Doctor on generated Project. | Deep diagnostic evidence and generated counter verification retained. |
| `opentitan-uart-clean-room-greenfield.setup-doctor-final` | Run final plain Doctor after deep baseline. | Clean final state and retained counter baseline. |
| `opentitan-uart-clean-room-greenfield.interactive-prompt` | Give Interactive delegate the exact baseline prompt below. | Prompt/hash, provider/backend/delegate identity and allowed inputs retained. |
| `opentitan-uart-clean-room-greenfield.interactive-edit` | Implement minimal CTRL reset/read/write and MMIO smoke. | Owned RTL/TB/config/guidance diff establishes sim_uart, lint_uart, synth_uart and does not implement subsequent Ticket's full journey prematurely. |
| `opentitan-uart-clean-room-greenfield.interactive-mcp` | Execute baseline through runtime MCP/Flow integration. | Durable calls correlate runtime/provider identity, Targets and artifacts; semantic claim only. |
| `opentitan-uart-clean-room-greenfield.interactive-sim` | Run sim_uart smoke covering reset/read/write/strobes/errors/backpressure. | Passing self-checking SystemVerilog simulation, actual normalized grade and fresh run artifacts. |
| `opentitan-uart-clean-room-greenfield.interactive-lint` | Run lint_uart with Verilator. | Clean lint and Target/Flow/tool identities with fresh log/report. |
| `opentitan-uart-clean-room-greenfield.interactive-synth` | Run synth_uart with logical Yosys. | Successful grade and fresh netlist/reports; no physical or PPA claim. |
| `opentitan-uart-clean-room-greenfield.interactive-doctor` | Run Doctor after MMIO baseline. | Clean baseline diagnostics. |
| `opentitan-uart-clean-room-greenfield.interactive-trace` | Generate a new MMIO smoke trace. | Fresh identity-bound Trace Artifact, provenance and source association. |
| `opentitan-uart-clean-room-greenfield.interactive-bwave` | Read baseline trace through B-Wave. | Artifact-backed signal/time readback agrees with smoke stimulus, independently of GUI claims. |
| `opentitan-uart-clean-room-greenfield.interactive-commit` | Commit clean MMIO baseline. | Exact commit, source/config diff, clean status and benign literal commit message retained at mmio-green. |

### Feature acceptance and conditional repairs

Source: UART resolution, **Project, modes and Ticket payloads; Independent evaluator and public cases**. Recovery: An unaccepted Feature blocks conformance and is never renamed a conformance repair. Continue external-image work after mmio-green if safe. A real mismatch permits only the bounded repairs below; do not weaken controls.

| Proposed check ID | Stimulus | Expected observation and retained evidence |
|---|---|---|
| `opentitan-uart-clean-room-greenfield.feature-create` | Invoke packaged Ticket Create Agent Mode --agent --no-confirm with exact Feature payload below. | Named Feature Implement the documented standalone UART; owned rtl/ and tb/ Scope; full specification, tests and Target controls frozen before enqueue. |
| `opentitan-uart-clean-room-greenfield.feature-run` | Run the named Feature with booley run. | Ticket Mode provider/backend identity, durable run logs and lifecycle transitions recorded. |
| `opentitan-uart-clean-room-greenfield.feature-acceptance-basis` | Inspect immutable Ticket inputs and Developer edits. | Scope, Criteria, Targets, Acceptance Basis and corpus/addendum review bindings preserved; protected acceptance controls not edited. |
| `opentitan-uart-clean-room-greenfield.feature-elaboration` | Execute Elaboration Check on sim_uart. | Successful compile/elaborate/link evidence; never substituted for complete Simulation. |
| `opentitan-uart-clean-room-greenfield.feature-simulation` | Execute complete registered Simulation on sim_uart. | Developer-authored self-checking TB and full passing results, grade and fresh artifacts. |
| `opentitan-uart-clean-room-greenfield.feature-lint` | Execute lint_uart. | Clean Verilator lint with bound identity and fresh report. |
| `opentitan-uart-clean-room-greenfield.feature-synthesis` | Execute synth_uart. | Successful logical Yosys synthesis with fresh netlist/reports; no relative PPA, mutation-score or hidden coverage gate. |
| `opentitan-uart-clean-room-greenfield.feature-review-rtl` | Run RTL-bugs review. | Clean bound review and retained report. |
| `opentitan-uart-clean-room-greenfield.feature-review-protocol` | Run protocol review. | Clean bound review and retained report. |
| `opentitan-uart-clean-room-greenfield.feature-review-spec` | Run specification review against immutable corpus/addendum. | Clean report tied to exact specification assets. |
| `opentitan-uart-clean-room-greenfield.feature-review-tb` | Run TB-quality review. | Clean report evaluates Developer-authored verification. |
| `opentitan-uart-clean-room-greenfield.feature-disposition` | Complete acceptance. | done disposition, local merge, Workspace cleanup, triage report, Board transitions and exact accepted commit; acceptance does not imply independent conformance. |
| `opentitan-uart-clean-room-greenfield.repair-trigger` | Observe actual independent mismatch on accepted commit. | Only that mismatch enables Repair standalone UART conformance — attempt 1/2; initially conforming candidate needs no forced repair. |
| `opentitan-uart-clean-room-greenfield.repair-create` | Create each needed Bug Fix Ticket separately. | Depends on preceding accepted implementation/repair, Scope owned RTL/TB, original public contract plus bounded feedback only. |
| `opentitan-uart-clean-room-greenfield.repair-diagnostics` | Select failing records deterministically by public case ID, preferring distinct families. | At most five records per repair, each only identity/stimulus/expected/observed/timing/relevant waveform excerpt; full logs remain operator-held. |
| `opentitan-uart-clean-room-greenfield.repair-regression` | Run repair with authored regression and complete original Criteria. | No weakened checks or edited acceptance controls; regression and all evidence plus accepted commit retained. |
| `opentitan-uart-clean-room-greenfield.repair-full-rerun` | Independently evaluate each accepted repair. | Entire original materialized case set and same seed, exact new commit, all outcomes retained; never only failing cases. |
| `opentitan-uart-clean-room-greenfield.repair-bounds` | Count all repair attempts and wall time. | At most two total, each at most 45 minutes including reevaluation; restart does not reset limit, no third repair from unused budget. |

### Evaluator independence and controls

Source: UART resolution, **Independent evaluator and public cases**. Recovery: Evaluator/fixture failure or contamination blocks conformance claims; never reveal hidden material. Remove each injected corruption and prove recovery before candidate evaluation. Preserve original failed results and classify design versus infrastructure separately.

| Proposed check ID | Stimulus | Expected observation and retained evidence |
|---|---|---|
| `opentitan-uart-clean-room-greenfield.evaluator-isolation` | Place evaluator outside Project and Session Runtime; inspect access boundaries. | Developer cannot access source/cases/prompts/logs/filesystem/network route; operator evidence proves separation. |
| `opentitan-uart-clean-room-greenfield.evaluator-identity` | Materialize deterministic corners plus seeded variations. | Evaluator digest, seed and full case manifest recorded; public identities/requirements remain public. |
| `opentitan-uart-clean-room-greenfield.evaluator-candidate` | Build accepted commit in disposable operator checkout. | Exact candidate RTL evaluated with independent drivers/monitors, never candidate TB/pass sentinel. |
| `opentitan-uart-clean-room-greenfield.control-positive-mmio` | Run known operator transport fixture without corruption. | MMIO oracle passes, expected/observed response evidence retained. |
| `opentitan-uart-clean-room-greenfield.control-negative-mmio` | Corrupt one defined MMIO response bit. | Corresponding oracle fails, deliberate negative-check pass records exact bit/stimulus and observed detection. |
| `opentitan-uart-clean-room-greenfield.control-recover-mmio` | Remove MMIO corruption and rerun fixture. | Oracle passes with restoration evidence. |
| `opentitan-uart-clean-room-greenfield.control-positive-serial` | Run known operator serial fixture without corruption. | Serial oracle passes with trace. |
| `opentitan-uart-clean-room-greenfield.control-negative-serial` | Corrupt one serial payload bit. | Corresponding oracle fails with exact bit and trace evidence. |
| `opentitan-uart-clean-room-greenfield.control-recover-serial` | Remove serial corruption and rerun fixture. | Oracle passes with restoration evidence; controls claim evaluator path, not UART RTL coverage. |
| `opentitan-uart-clean-room-greenfield.case-manifest` | Inspect every materialized subcase before evaluation. | Stable family-qualified ID, public source, deterministic input or seed-generation rule, exact observation window, timeout and evidence pointer; seeded choice never omits mandatory corners. |
| `opentitan-uart-clean-room-greenfield.evaluator-results` | Capture complete independent evaluation. | Every outcome, stimulus, expected/observed value and timing/trace retained; unspecified semantics never invented by hidden policy. |
| `opentitan-uart-clean-room-greenfield.evaluator-reuse` | Reuse a complete independent result only if identities match. | Candidate commit, evaluator and complete materialized case identity all identical; otherwise full reevaluation within budget. |

### Independent UART conformance cases

Source: UART resolution, **Interface and MMIO contract; Independent evaluator and public cases**. Recovery: On a trustworthy mismatch retain all evidence and apply only the accepted repair path. Each case resets/restores its own declared initial state; recovery cases prove good operation after the stimulus. Infrastructure failure blocks, not a design mismatch. Capture independent stimulus, expected/observed values and timings/traces at each observation.

| Proposed check ID | Stimulus | Expected observation and retained evidence |
|---|---|---|
| `opentitan-uart-clean-room-greenfield.UART-BUS.request-stable` | Stall request acceptance while valid remains asserted. | Request fields stable until rising-edge handshake; monitor protocol separately from DUT response. |
| `opentitan-uart-clean-room-greenfield.UART-BUS.response-stable` | Hold response ready low. | Response valid/data/error remain stable under backpressure. |
| `opentitan-uart-clean-room-greenfield.UART-BUS.single-outstanding` | Present request while previous response pending. | No second acceptance; at most one accepted request outstanding. |
| `opentitan-uart-clean-room-greenfield.UART-BUS.accept-latency` | Hold valid on idle released interface. | Acceptance within four clocks. |
| `opentitan-uart-clean-room-greenfield.UART-BUS.response-latency` | Accept request on rising edge. | Response presented within four clocks; no zero-cycle throughput demand. |
| `opentitan-uart-clean-room-greenfield.UART-BUS.read-snapshot` | Change relevant state after accepted read while stalling response. | Response reflects read snapshot at acceptance. |
| `opentitan-uart-clean-room-greenfield.UART-BUS.read-once` | Stall accepted RDATA read response. | Exactly one FIFO pop, at request acceptance. |
| `opentitan-uart-clean-room-greenfield.UART-BUS.write-once` | Stall accepted WDATA write response. | Exactly one enqueue, at request acceptance. |
| `opentitan-uart-clean-room-greenfield.UART-BUS.byte-masks` | Write each byte lane selectively. | Only strobed lanes change legal fields. |
| `opentitan-uart-clean-room-greenfield.UART-BUS.w1c-masks` | Write W1C fields with selected byte strobes. | Only written-one bits in enabled lanes clear. |
| `opentitan-uart-clean-room-greenfield.UART-BUS.wdata-strobe` | Write WDATA with/without low-byte strobe. | Enqueue only when low byte enabled. |
| `opentitan-uart-clean-room-greenfield.UART-BUS.zero-strobe` | Issue zero-strobe writes. | Success with no effects. |
| `opentitan-uart-clean-room-greenfield.UART-BUS.read-strobes` | Vary strobes on reads. | Reads ignore strobes. |
| `opentitan-uart-clean-room-greenfield.UART-BUS.ro-write` | Write each RO register. | Write ignored. |
| `opentitan-uart-clean-room-greenfield.UART-BUS.wo-read` | Read each WO register. | Zero returned. |
| `opentitan-uart-clean-room-greenfield.UART-BUS.misaligned` | Read/write misaligned addresses. | Error, zero data, no effects. |
| `opentitan-uart-clean-room-greenfield.UART-BUS.unmapped` | Read/write unmapped and high-bit-altered addresses. | Error, zero data, no effects; no address-truncation alias. |
| `opentitan-uart-clean-room-greenfield.UART-BUS.empty-read` | Read empty RX FIFO repeatedly. | No underflow; unspecified data values unscored. |
| `opentitan-uart-clean-room-greenfield.UART-REG.offsets` | Access every documented register offset. | Correct distinct mapping, no alias; manifest expands one concrete check per register/field requirement. |
| `opentitan-uart-clean-room-greenfield.UART-REG.reset-values` | Reset then read every defined register/field. | Exact pinned defined reset values; unspecified fields unscored. |
| `opentitan-uart-clean-room-greenfield.UART-REG.legal-rw` | Exercise every legal RW field. | Pinned access semantics and field values; reserved encodings not guessed. |
| `opentitan-uart-clean-room-greenfield.UART-REG.reserved` | Read/write reserved bits. | Read zero, ignore writes. |
| `opentitan-uart-clean-room-greenfield.UART-REG.alert-test` | Read/write 0x0c ALERT_TEST. | Read zero, acknowledge writes without effect. |
| `opentitan-uart-clean-room-greenfield.UART-TXRX.tx-bytes` | Transmit each value 0–255 using independent serial decode. | Correct eight data bits and one stop bit for every byte. |
| `opentitan-uart-clean-room-greenfield.UART-TXRX.rx-bytes` | Drive each value 0–255 from independent serial source. | Correct eight-bit/one-stop reception for every byte. |
| `opentitan-uart-clean-room-greenfield.UART-TXRX.back-to-back` | Send consecutive TX/RX characters. | Ordered correct reception/transmission without omitted back-to-back corner. |
| `opentitan-uart-clean-room-greenfield.UART-TXRX.full-duplex` | Transmit and receive simultaneously. | Both paths independently correct. |
| `opentitan-uart-clean-room-greenfield.UART-TXRX.enable-disable` | Exercise TX/RX enables and idle. | Pinned enabled/disabled/idle behavior. |
| `opentitan-uart-clean-room-greenfield.UART-BAUD.exact-a` | Run first exact-divider NCO setting. | Accumulator-based timing and 16x receive sampling within declared bounds. |
| `opentitan-uart-clean-room-greenfield.UART-BAUD.exact-b` | Run second exact-divider NCO setting. | Same independent timing oracle at distinct exact-divider rate. |
| `opentitan-uart-clean-room-greenfield.UART-BAUD.fractional` | Run fractional NCO setting across accumulation pattern. | Documented accumulator timing within declared phase/timing bounds. |
| `opentitan-uart-clean-room-greenfield.UART-BAUD.zero` | Set NCO to zero with pending transmit work. | No transmit progress in declared observation window. |
| `opentitan-uart-clean-room-greenfield.UART-PARITY.disabled` | TX/RX both data-parity classes with parity disabled. | Correct parity-disabled frame/data behavior. |
| `opentitan-uart-clean-room-greenfield.UART-PARITY.even` | TX/RX both data-parity classes with even parity. | Correct serial parity and accepted data. |
| `opentitan-uart-clean-room-greenfield.UART-PARITY.odd` | TX/RX both data-parity classes with odd parity. | Correct serial parity and accepted data. |
| `opentitan-uart-clean-room-greenfield.UART-PARITY.bad` | Inject wrong parity for each enabled parity mode. | Documented data/error outcomes, clearing and subsequent valid recovery. |
| `opentitan-uart-clean-room-greenfield.UART-FIFO.tx-occupancy` | Control TX activity and fill to 0/1/31/32. | Exact 32-byte capacity and each boundary status; manifest gives each occupancy separate case. |
| `opentitan-uart-clean-room-greenfield.UART-FIFO.rx-occupancy` | Fill RX to 0/1/63/64. | Exact 64-byte capacity and each boundary status; each occupancy materialized. |
| `opentitan-uart-clean-room-greenfield.UART-FIFO.overflow` | Attempt insertion into full FIFO. | Documented overflow/drop behavior, no invented TX-overflow interrupt. |
| `opentitan-uart-clean-room-greenfield.UART-FIFO.order` | Enqueue distinguishable data then drain. | FIFO ordering and complete drain correct. |
| `opentitan-uart-clean-room-greenfield.UART-FIFO.tx-reset` | Reset TX FIFO independently then send good data. | TX reset and recovery; other state preserved per contract. |
| `opentitan-uart-clean-room-greenfield.UART-FIFO.rx-reset` | Reset RX FIFO independently then receive good data. | RX reset and recovery; other state preserved per contract. |
| `opentitan-uart-clean-room-greenfield.UART-WATERMARK.tx` | For every legal TX threshold test immediately below/at/above. | TX condition strictly below threshold, each boundary concretely enumerated. |
| `opentitan-uart-clean-room-greenfield.UART-WATERMARK.rx` | For every legal RX threshold test immediately below/at/above. | RX condition at least threshold; explicitly include level 62. |
| `opentitan-uart-clean-room-greenfield.UART-IRQ.identities` | Drive each of nine public interrupt sources. | Correct INTR_STATE and output bit 0–8 identity, each separately materialized. |
| `opentitan-uart-clean-room-greenfield.UART-IRQ.enable-mask` | Enable/mask each source. | Correct per-bit mask behavior without cross-bit corruption. |
| `opentitan-uart-clean-room-greenfield.UART-IRQ.test` | Exercise every INTR_TEST bit. | Documented injection behavior. |
| `opentitan-uart-clean-room-greenfield.UART-IRQ.level` | Clear level condition while source persists then remove condition. | Documented reassertion and deassertion. |
| `opentitan-uart-clean-room-greenfield.UART-IRQ.event` | Generate/clear each event with W1C. | Correct sticky event clearing and no cross-bit corruption. |
| `opentitan-uart-clean-room-greenfield.UART-IRQ.tx-empty-done` | Observe empty FIFO while serial TX still active, then complete. | TX-empty distinguished from TX-done. |
| `opentitan-uart-clean-room-greenfield.UART-ERROR.stop` | Inject bad stop bit then clear and send good character. | Frame error, documented data behavior and recovery. |
| `opentitan-uart-clean-room-greenfield.UART-ERROR.break` | Exercise each of four break thresholds then recover. | Threshold-specific break detection and clearing; four concrete cases. |
| `opentitan-uart-clean-room-greenfield.UART-ERROR.timeout-enabled` | Enable timeout with nonempty RX FIFO. | Documented timeout detection/clearing and good-character recovery. |
| `opentitan-uart-clean-room-greenfield.UART-ERROR.timeout-disabled` | Disable timeout with nonempty RX FIFO. | No enabled-only timeout behavior. |
| `opentitan-uart-clean-room-greenfield.UART-FILTER.noise` | Exercise one-clock RX noise with filter on and off. | Documented filtering; rejected glitches create no character. |
| `opentitan-uart-clean-room-greenfield.UART-FILTER.false-start` | Drive false starts with filter on/off. | Rejection matches pinned behavior, no spurious character. |
| `opentitan-uart-clean-room-greenfield.UART-FILTER.phase-start` | Drive phase-shifted valid starts with filter on/off. | Valid reception within declared synchronization/timing bounds. |
| `opentitan-uart-clean-room-greenfield.UART-LOOP.system` | Enable system loopback alone then return to normal. | Internal receive works, external TX idle, subsequent normal communication good. |
| `opentitan-uart-clean-room-greenfield.UART-LOOP.line` | Enable line loopback alone then return to normal. | RX forwarded externally, internal RX idle, subsequent normal communication good. |
| `opentitan-uart-clean-room-greenfield.UART-OVERRIDE.low` | Assert TX override low. | External TX forced low, registers/FIFOs preserved. |
| `opentitan-uart-clean-room-greenfield.UART-OVERRIDE.high` | Assert TX override high. | External TX forced high, registers/FIFOs preserved. |
| `opentitan-uart-clean-room-greenfield.UART-OVERRIDE.release` | Release override then communicate. | Normal TX resumes with preserved state. |
| `opentitan-uart-clean-room-greenfield.UART-HISTORY.order` | Drive known RX patterns and observe VAL after synchronization window. | Correct history ordering, newest sample at bit zero. |
| `opentitan-uart-clean-room-greenfield.UART-RESET.idle` | Hold reset at least four clocks, RX high; release on clock boundary. | Defined UART/register/FIFO state and subsequent good communication. |
| `opentitan-uart-clean-room-greenfield.UART-RESET.active-tx` | Reset during active TX then communicate. | Defined reset behavior and good TX recovery. |
| `opentitan-uart-clean-room-greenfield.UART-RESET.active-rx` | Reset during active RX then communicate. | Defined reset behavior and good RX recovery. |
| `opentitan-uart-clean-room-greenfield.UART-RESET.occupied` | Reset with occupied FIFOs then refill/drain. | Defined empty/reset state and recovered operation. |
| `opentitan-uart-clean-room-greenfield.UART-RESET.pending-mmio` | Reset with accepted transaction/response pending. | Pending transaction cancelled, defined state, subsequent good MMIO/UART communication. |

### External image, final evidence and cleanup

Source: UART resolution, **Quick external-image check; Ordered steps and checkpoints; Profiles, budgets and allocation**. Recovery: External-image slice needs only mmio-green and continues independently of later development failure. Missing predeclared capability is unavailable; loss after availability is fail/blocked. Start owned-resource cleanup by 7h30 regardless of earlier outcome; preserve failed records.

| Proposed check ID | Stimulus | Expected observation and retained evidence |
|---|---|---|
| `opentitan-uart-clean-room-greenfield.external-prerequisite` | Probe pre-provisioned digest-pinned external image. | Same exact published release and required EDA present; no hidden download/build allocation, total slice including cleanup at most 15 minutes. |
| `opentitan-uart-clean-room-greenfield.external-project` | Copy MMIO-smoke sources to disposable Project with separate state; select external image through published config and initialize/seed spec. | Source/spec identities, distinct Project state and correct selected image recorded. |
| `opentitan-uart-clean-room-greenfield.external-release` | Start external runtime and inspect version/image. | Exact declared Booley version and immutable image digest. |
| `opentitan-uart-clean-room-greenfield.external-doctor` | Run plain Doctor in external runtime. | Expected clean diagnostic state. |
| `opentitan-uart-clean-room-greenfield.external-smoke-first` | Run only MMIO smoke. | Passing result and fresh evidence on declared source/image. |
| `opentitan-uart-clean-room-greenfield.external-recreate` | Stop/recreate through documented lifecycle. | New runtime, same external image digest, persistent Project sources, no image rebuild/modification. |
| `opentitan-uart-clean-room-greenfield.external-smoke-second` | Repeat identical MMIO smoke after recreation. | Passing result bound to unchanged source/image. |
| `opentitan-uart-clean-room-greenfield.external-cleanup` | Remove owned external-slice runtime/Project/state. | Owned resources released within 15 minutes, supplied image preserved. |
| `opentitan-uart-clean-room-greenfield.final-simulation` | Run complete Developer simulation on final accepted tree. | Full passing results and fresh evidence. |
| `opentitan-uart-clean-room-greenfield.final-lint` | Run final Verilator lint. | Clean grade/report on accepted tree. |
| `opentitan-uart-clean-room-greenfield.final-synthesis` | Run final logical Yosys synthesis. | Fresh successful netlist/reports bound to accepted tree. |
| `opentitan-uart-clean-room-greenfield.final-doctor` | Run final Doctor. | Clean diagnostic state. |
| `opentitan-uart-clean-room-greenfield.final-git-stealth` | Inspect accepted repositories and explicit Stealth-disabled state. | Clean accepted commit, local merge, ordinary source/core paths and literal benign identifying commit messages. |
| `opentitan-uart-clean-room-greenfield.archive` | Archive run records and evidence outside disposable state. | JSONL and derived human/Consolidate Findings views; corpus/interface/evaluator manifests; private cases/results; bounded diagnostics; logs/traces/netlist/reports; Ticket/basis/diff/commit evidence and Git bundle retained. |
| `opentitan-uart-clean-room-greenfield.cleanup-processes` | Stop/remove owned processes and runtimes. | Ledger and inspection prove release; pre-existing resources untouched. |
| `opentitan-uart-clean-room-greenfield.cleanup-git` | Remove owned Workspaces/worktrees/branches after archiving. | Durable bundle/evidence verified before deletion; no remaining owned scratch. |
| `opentitan-uart-clean-room-greenfield.cleanup-projects` | Remove owned Projects and inventory entries. | Ledger reconciled with remaining state, no unrelated deletion. |
| `opentitan-uart-clean-room-greenfield.cleanup-evaluator` | Remove owned evaluator scratch. | Operator private evidence archived and scratch removed. |
| `opentitan-uart-clean-room-greenfield.cleanup-preservation` | Compare borrowed/pre-existing state to declaration. | Credentials, caches, pre-existing state and supplied external image preserved. |
| `opentitan-uart-clean-room-greenfield.deadline` | Observe elapsed time and stop new work at 7h30. | Cleanup reserve honored, eight-hour completion recorded honestly; unfinished required checks blocked, never silently omitted. |
| `opentitan-uart-clean-room-greenfield.retry-policy` | Classify retry request before rerunning. | No automatic retry for design mismatch/evaluator mismatch/crash/usage exhaustion; only one exact configured known stream-stall retry within original deadline, original event retained. |
| `opentitan-uart-clean-room-greenfield.interruption` | Interrupt coordinator or encounter missing prerequisite. | Partial record retained, resources reconciled, new run starts without old check passes; live phase recovery only. |

## GUI/client checks

Source: UART resolution's Project/modes and profile sections, amended by current shared qualification rules. Recovery: preserve unavailable or failed evidence, continue independent core work; never substitute headless/CLI observations. Required same-run supporting phases are preparation, bootstrap/scaffold, Setup and complete Interactive baseline; archive/cleanup remains mandatory.

| Proposed check ID | Stimulus | Expected observation and evidence |
|---|---|---|
| `opentitan-uart-clean-room-greenfield.gui-runtime-client` | Attach selected supported VS Code client to Session Runtime and perform accepted baseline Interactive prompt. | Actual supported-client/provider/runtime identity, attachment/integration logs and timestamped qualified observation; not headless identity. |
| `opentitan-uart-clean-room-greenfield.gui-mcp-integration` | Invoke baseline MCP tools through that supported client. | Actual client integration shown by correlated client/tool logs and qualified observation. |
| `opentitan-uart-clean-room-greenfield.gui-waveform` | Open fresh MMIO trace in supported Waveform Viewer. | Qualified timestamped visual evidence of rendering and relevant signal/time readback linked to exact trace; semantic B-Wave result alone insufficient. |

## Resolved encoding contract and explicit exclusions

The [public oracle derivation](uart-oracles.md) supplies the pinned register/field
expansion and concrete baud stimuli. Its approved latency addendum is identified
separately from corpus facts and must be published in the Developer's allowed
contract before execution. Reserved encodings and undocumented behavior remain
unscored. The FIFO overflow check is specifically RX overflow/drop; the corpus
does not define full-TX-write disposition or a TX-overflow interrupt. The
nonexistent `STATUS.BREAK` name is recorded as a documentation inconsistency,
not an invented register field.

Repair checks encode the accepted **conditional policy**, not a demand to create
two Bugs when the first candidate conforms. Each repair-policy result records
the complete preceding evaluation identity and whether a real mismatch activated
that branch. With no mismatch, it proves no repair was created and no source was
changed under repair authority; it earns no claim of executed Bug Fix coverage.
With a mismatch, it requires the stated Ticket, bounded feedback, Criteria,
complete rerun and cap evidence. The selected check list does not change.

Retain every independent case outcome across candidate revisions. The shared
qualification failure rule still applies: an unexpected trustworthy failed
required check remains a failure even when a later candidate conforms. Report
final-candidate conformance separately from the run's preserved failures;
bounded repair is permission to continue, not permission to erase a failure.

The sole retry signature is `API Error: Response stalled mid-stream`, with at
most one automatic retry within the original phase/deadline. The
[OpenTitan documentation report draft](opentitan-doc-report.md) is now prepared;
submission remains unauthorized. UART does not absorb Taxi's submodule exercise.

## Retained accepted detailed contract

The following source preserves exact pins, assets, prompts, Ticket content, register/MMIO rules, public family requirements, budgets, evidence and cleanup. Its current-body amendment is included first; current shared protocol and profiles prevail over superseded bookkeeping in the original resolution.

## Shared-contract amendment — 08 SEP 2026

The accepted journey in the resolution below remains binding: preserve all pins,
hardware work, prompts/Tickets/Criteria, thresholds, faults, independent evaluation
where present, authority, provider/platform responsibilities, and cleanup.

Apply the current shared decisions in #249 (execution), #250 (maintenance), #251
(qualification), #253 (format), and #270 (authoring) instead of earlier shared
bookkeeping requirements in the resolution:

- Encode checks directly against capabilities and explicit profiles; retain every
  independently observable requirement. Remove separately authored obligation,
  cell, allocation, and verification-chain entities.
- Use the compact run manifest, result/finding JSONL, resource ledger, and evidence
  directory. Preserve all scenario-specific evidence; common run metadata need not
  repeat on every check. Typed workflow event replay is unnecessary.
- Named checkpoints become phase recovery points within a live run. Preserve fault
  restoration and accepted-commit evidence. After coordinator interruption, retain
  the partial run, reconcile resources, and start a new run without reusing old check
  passes. General resume is deferred.
- Specification-backed checks are mandatory from reviewed introduction; no assertion
  promotion registry remains. Use pass/fail/blocked/unavailable outcomes from #249.
- Keep both modes in the journey. Core reports only runnable semantic/product
  behavior. Actual VS Code integration and visual claims belong to GUI/client
  qualification and require corresponding evidence. Unavailable required GUI work
  makes that profile incomplete, while core may pass. Never claim complete client
  coverage from a headless child or an unqualified full-suite pass.

#376 must map every old requirement to a concrete preserved check and profile,
reconcile feasibility and coverage gaps, and apply this amendment during encoding.
Earlier resolution prose is superseded only for these shared mechanisms.

---

## Question

What exact ordered Scenario Steps, documentation corpus, interface stub, prompts, Tickets, Criteria, independent conformance cases, seeded faults, checkpoints, evidence, bounded diagnostics, recovery behavior, provider/platform profiles, and cleanup make the **OpenTitan UART clean-room greenfield** scenario implementation-ready?

Start with `booley init --scaffold` and `/booley-setup new` on the standard Booley Session Image. The implementing Developer Agent receives only the frozen Earl Grey 1.0.0 UART documentation and the scenario-owned 32-bit ready/valid MMIO interface contract; it receives no existing UART RTL or tests and must author both. Preserve the agreed standalone behavioral surface, 64-byte RX and 32-byte TX interpretation, operator-isolated seeded conformance evaluator, full-suite rerun after bounded failure feedback, supplementary externally managed image profile, explicit Stealth-disabled, Ubuntu, Windows, Codex, Interactive Mode, and Ticket Mode responsibilities. TL-UL, OpenTitan alert/integrity infrastructure, low-power integration, and SoC wiring remain outside this scenario.


## Resolution

Scenario ID: `opentitan-uart-clean-room-greenfield`.

Start from an empty disposable repository and the standard Booley Session Image. Exercise automatic Host Bootstrap and greenfield Setup, establish an MMIO smoke baseline in Interactive Mode, implement the UART and its verification in Ticket Mode, independently evaluate the exact candidate, and allow at most two conformance-driven repair Tickets. Complete evidence and cleanup within eight hours. External-image coverage is a **15-minute supplementary lifecycle check**, not another full scenario run.

This resolves scenario design. Encoding assets, implementing the evaluator, running scenarios, and repairing Booley remain outside this map.

### Shared contract and frozen inputs

Apply the [execution and evidence contract](https://github.com/boldaxolotl/booley/issues/249#issuecomment-5508926500), [coverage accounting decision](https://github.com/boldaxolotl/booley/issues/251), and [public format decision](https://github.com/boldaxolotl/booley/issues/253#issuecomment-5567625119). Shared rules belong in the Scenario Protocol; the choices below are scenario-specific.

The Run Declaration freezes the exact published Booley release/package hash and corresponding public docs, suite commit/digest, protocol, provider/client/backend, platform, image identity, documentation and interface digests, evaluator digest and seed, run-owned repositories/branches, deadline, credentials mechanism without secret values, and resource ownership. No development build, local wheel, editable installation, or import/execution from a Booley source checkout is allowed.

The coordinator delegates actual setup, development, evaluation, diagnostics and cleanup. All declared Project edits, local commits, Ticket creation and local acceptance/merge are authorized before execution. No live approval is requested during a run. No branch is pushed, report submitted externally, or permanent Booley repair performed.

### Documentation-only corpus

Pin OpenTitan `earlgrey_1.0.0` to [`615d3c74fadbbf674c8ca05a70f91094989849fb`](https://github.com/lowRISC/opentitan/tree/615d3c74fadbbf674c8ca05a70f91094989849fb). Package exactly:

- `hw/ip/uart/README.md`
- `hw/ip/uart/doc/theory_of_operation.md`
- `hw/ip/uart/doc/programmers_guide.md`
- `hw/ip/uart/doc/interfaces.md`
- `hw/ip/uart/doc/registers.md`
- `hw/ip/uart/doc/block_diagram.svg`
- `LICENSE`

Retain provenance, per-file SHA-256 hashes and Apache-2.0 attribution. Register Markdown already contains the generated documentation: do not include HJSON, regtool, RTL, tests, UVM, DIF, implementation or reference-model source. Do not recursively follow documentation links into current docs or implementation material. The Developer receives this corpus, the interface-only stub and scenario addendum, published Booley docs/help/packaged skills, and ordinary Project access.

Precedence is the explicit scenario addendum, then pinned register definitions and precise behavioral descriptions, then illustrative examples. Preserve **64-byte RX / 32-byte TX**. Record the contradictory 32-byte RX prose, undocumented TX-overflow interrupt example, and RX watermark shift-by-three example as documentation Findings. The displayed zero-valued threshold masks the shift error. These are not claims of RTL defects.

The maintainer requested an OpenTitan report draft as a planning-session follow-up. Submission is not authorized and is not part of a scenario run.

### Interface and MMIO contract

The interface-only `qa_uart` stub declares clock, active-low reset, request valid/ready, write flag, 32-bit byte address, 32-bit write data, four byte strobes, response valid/ready, 32-bit read data, error, serial RX/TX, and nine IRQ outputs ordered as INTR_STATE bits 0–8. It contains no UART implementation or tests.

The scenario-owned replacement bus follows these rules:

- Handshakes occur on rising edges. One accepted request may be outstanding. Request fields remain stable until acceptance; response data/error/valid remain stable under backpressure. No new request is accepted while a response is pending.
- An idle released interface accepts a continuously asserted request within four clocks and presents the response within four clocks of acceptance; no zero-cycle throughput requirement.
- Read snapshots and register side effects occur once at request acceptance, including RDATA pop and WDATA enqueue, never repeatedly during response stalls.
- Write strobes mask byte lanes, including W1C effects. WDATA enqueues only with its low byte enabled. Zero-strobe writes succeed without effects. Reads ignore strobes. RO writes are ignored, WO reads return zero, and reserved bits read zero/ignore writes.
- Misaligned or unmapped addresses return error and zero data without side effects; address truncation must not create aliases. Empty FIFO reads must not underflow; values explicitly unspecified by documentation are not scored.
- Reset cancels pending transactions and restores defined register/FIFO/UART state. Hold reset at least four clocks with RX high and release on a clock boundary. Check defined values, not fields documented as unspecified.
- Retain every register offset. **ALERT_TEST at 0x0c reads zero and acknowledges writes without effect**, an explicit exception for excluded alert infrastructure.

TL-UL, alert/integrity, low-power and SoC integration remain excluded. No OpenTitan implementation equivalence is claimed. Reserved control encodings or undocumented combinations are not assigned guessed RTL behavior.

### Project, modes and Ticket payloads

Use a SystemVerilog HDL testbench, Verilator Simulation and Lint, and logical Yosys synthesis on the standard image. Explicitly set `[stealth] enabled = false`. Preserve ordinary Project source/core locations and literal benign Project-identifying commit messages as evidence.

The Setup delegate runs `booley init --scaffold` with the selected provider, Verilator simulator/linter, SystemVerilog TB and ASIC support; then `/booley-setup new` inside the Session Runtime. These choices are supplied in advance. Prove the generated counter and plain/deep Doctor baseline before replacing it.

The Interactive delegate receives:

> Replace the scaffold counter with the interface contract and a minimal CTRL reset/read/write implementation. Author an MMIO smoke test covering reset, read/write, strobes, errors and response backpressure. Establish `sim_uart`, `lint_uart` and `synth_uart`; pass smoke simulation, lint, synthesis and Doctor. Retain a fresh trace and B-Wave readback, commit the clean baseline, and record the allowed-source and MCP evidence. Full UART behavior is subsequent Ticket work.

Interactive work may edit owned RTL, TB, configuration and guidance. Ticket Create subsequently freezes the full specification and test registrations/Target controls before enqueueing. Developer-time changes to protected acceptance controls remain forbidden.

Feature Ticket: **Implement the documented standalone UART**, type Feature, scoped to owned `rtl/` and `tb/` assets. Its payload is:

> Implement the complete standalone UART from the frozen documentation and MMIO addendum, authoring both RTL and self-checking SystemVerilog verification. Preserve the interface, full required behavior and Target contracts. Use only allowed documentation and ordinary Project inspection; retrieve no existing UART RTL or tests. Complete the bound Criteria and retain evidence. Report documentation conflicts rather than inventing replacement requirements.

Mandatory Criteria: Elaboration Check and complete Simulation on `sim_uart`; clean lint on `lint_uart`; successful logical synthesis on `synth_uart`; clean RTL-bugs, protocol, specification and TB-quality reviews. Bind the specification review to immutable corpus/addendum paths. Require fresh netlist/reports for synthesis. Do not invent a relative PPA improvement, hidden coverage Criterion or mutation-score gate.

Success disposition is `done`, with local merge, Workspace cleanup and triage report enabled. Record Scope, Criteria, Targets, Acceptance Basis, Board transitions, reports and accepted commit. **Independent conformance is a separate operator verdict; Ticket acceptance cannot alone make the scenario pass.**

An actual independent mismatch may create **Repair standalone UART conformance — attempt 1/2**, type Bug Fix, depending on the preceding accepted implementation/repair. Scope remains owned RTL/TB. Supply only bounded diagnostics plus the original public contract. Require an agent-authored regression and the same complete Criteria. Never weaken checks, alter acceptance controls, reveal evaluator material or rerun only selected operator cases. An unaccepted Feature Ticket is not relabelled as a conformance repair.

### Independent evaluator and public cases

The evaluator is held outside the Project and Session Runtime. The Developer cannot access its source, generated cases, prompts, logs, filesystem or network route. Public case identities and requirements remain public. Record evaluator digest, seed and full materialized-case manifest in operator evidence. Evaluate the exact accepted commit in a disposable checkout using independent drivers/monitors and candidate RTL, never trusting its testbench or pass sentinel.

First qualify the evaluator path using known operator-owned transport/serial fixtures: prove positive controls, corrupt one defined MMIO response bit and one serial payload bit separately, require the corresponding oracle failures, remove each corruption, and prove recovery. These controls do not claim UART RTL coverage. Failure blocks independent conformance claims. No forced product repair Ticket is needed for an initially conforming candidate.

All these public case families are mandatory. Deterministic corners run every time; seeded variation supplements them rather than randomly omitting requirements:

| Family | Required stimulus and oracle |
|---|---|
| UART-BUS | Handshake/stall stability, exactly-once side effects, byte masks/W1C, zero strobes, RO/WO, invalid/misaligned addresses, pending reset. |
| UART-REG | Every offset and defined reset value, legal RW fields, reserved bits, non-aliasing, ALERT_TEST exception. |
| UART-TXRX | Independent serial TX decode and RX stimulus; all 256 byte values, eight data bits/one stop bit, back-to-back and simultaneous traffic, enable/disable and idle. |
| UART-BAUD | NCO and 16x RX sampling; at least two exact-divider settings and one fractional setting; timing checked against documented accumulator behavior with declared phase/timing bounds; NCO zero produces no transmit progress. |
| UART-PARITY | Disabled/even/odd parity, both data-parity classes, deliberately wrong parity and documented data/error outcomes. |
| UART-FIFO | TX 0/1/31/32 and RX 0/1/63/64 occupancy; overflow/drop, ordering, drain, separate reset/recovery. Control TX activity while measuring TX capacity. |
| UART-WATERMARK | Every legal TX/RX threshold immediately below/at/above; TX strictly below, RX at least threshold, including RX level 62. |
| UART-IRQ | All nine identities, enable/mask, INTR_TEST, level reassertion versus event W1C, TX-empty versus TX-done, no cross-bit corruption. |
| UART-ERROR | Bad stop bit, parity error, all four break thresholds, enabled/disabled RX timeout with nonempty FIFO, clearing and good-character recovery. |
| UART-FILTER | Filter on/off, one-clock noise, false starts, phase-shifted valid starts; rejected glitches create no character. |
| UART-LOOP | Each loopback independently; system loopback receives internally with TX idle externally; line loopback forwards RX with internal RX idle; return to normal operation. |
| UART-OVERRIDE | Override low/high and release, preserving registers/FIFOs. |
| UART-HISTORY | Known RX patterns and VAL ordering, newest at bit zero, after the declared synchronization window. |
| UART-RESET | Idle/active TX/RX, occupied FIFO and pending-MMIO reset; defined state and subsequent good communication. |

When encoding, every concrete subcase has a stable family-qualified ID, source requirement, deterministic input or seed-generation rule, exact observation/timing window, timeout and evidence pointer. No hidden evaluator policy may decide unspecified semantics. Use identical full materialized cases and seed after repairs; changing seed creates another run, never replaces a failure.

Retain all operator case outcomes, stimuli, expected/observed values and timings/traces. Distinguish design mismatches from evaluator/infrastructure failure. Feedback is capped at **five failing-case records per repair**, selected deterministically by public case ID with distinct families preferred first. Each record contains only case identity, bounded reproducing stimulus, expected/observed result, timing and relevant waveform excerpt. Full logs remain operator-held. Two repairs is the total run limit, unaffected by restart.

### Ordered steps and checkpoints

1. **Prepare** — install/identify the exact published release, establish disposable host/account and Docker state, freeze declaration and resource ledger. A fresh-bootstrap claim requires proven absent managed state; never delete other users' resources to manufacture it. Checkpoint `prepared`.
2. **Bootstrap and scaffold** — observe bootstrap check-only pending, let `init --scaffold` reconcile automatically without prior explicit bootstrap, verify check-only clean, repeat init without drift, inspect images/skills/core/Stealth state. Checkpoint `scaffolded`.
3. **Greenfield Setup** — `/booley-setup new`, plain/deep/final-plain Doctor and retained generated counter baseline. Checkpoint `setup-green`.
4. **Interactive baseline** — selected VS Code client/MCP delegate implements the minimal MMIO contract; preserve prompt/hash, identity, allowed inputs, calls, source/config diff, smoke/trace/B-Wave, lint/synthesis and commit. Checkpoint `mmio-green`.
5. **Feature Ticket** — packaged Ticket Create Agent Mode (`--agent --no-confirm`) with the full payload, followed by a named `booley run`; record type, acceptance inputs and outcomes. Checkpoint `feature-accepted` only after successful acceptance.
6. **Independent evaluation** — positive/corrupt/recovered evaluator controls, then exact-commit full evaluation. Checkpoint `evaluation-0`.
7. **Conditional repairs** — at most two separately created/run Bug Fix Tickets and complete independent reruns. Checkpoints `evaluation-1/2` exist only when needed.
8. **Quick external image** — independent supplementary slice once `mmio-green` exists, including if later development fails. Checkpoint `external-image-checked`.
9. **Final evidence** — full developer simulation/lint/logical synthesis/Doctor, clean accepted repositories and Stealth evidence. Reuse the latest complete independent result only for identical candidate/evaluator/case identities; otherwise reevaluate within budget. Checkpoint `final-evidence`.
10. **Archive and cleanup** — preserve JSONL and derived human/Consolidate Findings views, corpus/interface/evaluator manifests, private case/results, permitted diagnostics, logs/traces/netlist/reports, Ticket/basis/diff/commit evidence and a Git bundle outside disposable state. Remove owned processes/runtimes/Workspaces/branches/Projects/inventory entries and evaluator scratch. Preserve pre-existing state, credentials, shared caches and the supplied external image. Checkpoint `cleanup-complete`.

Each checkpoint binds repository/image identities, evidence and resource state. Resume revalidates them. Failed prerequisites prune dependent work; trustworthy independent documentation, external-image and cleanup checks continue. Restoration never overwrites a failure or upgrades it to pass.

### Quick external-image check

Allow **15 minutes including its cleanup**. Use a pre-provisioned, digest-pinned external image containing the same exact published Booley release and required EDA programs; building/downloading a missing image is not hidden in this slice.

Use a disposable copy of the MMIO-smoke Project sources with separate Project state. Select the external image through the documented configuration, initialize/seed the specification, start the runtime, verify image identity and installed release, and run plain Doctor plus the single MMIO smoke test. Stop/recreate through the documented external-image lifecycle and repeat the same smoke. Prove image identity unchanged, Project source persistence, and no image rebuild/modification. Retain spec/version/image/both-smoke/cleanup evidence.

Do not repeat development, Tickets, reviews, synthesis qualification, full conformance or another Interactive session. This slice is not another complete provider/mode/scenario qualification. Pre-declared image unavailability yields not runnable and an unqualified coverage cell; loss/failure after declared availability is not retroactively a capability exclusion.

### Profiles, budgets and allocation

Routine mandatory main runs use Codex on Ubuntu 24.04 x86-64 and native Windows x86-64 with Docker Desktop/WSL2. Both Interactive and Ticket modes are required. Keep the specification provider-neutral; unrun Claude cells remain unqualified, with representative Claude initially assigned to PicoRV32. GUI-only observations remain capability-gated until a qualified observer exists; CLI evidence cannot impersonate a VS Code client assertion.

Eight-hour budget: preparation/bootstrap 45 minutes; Setup/Interactive baseline 45; Feature Ticket 180; evaluator controls/initial evaluation 30; two repairs including full reruns 90 total; external-image slice 15; final regression/archive 30; contingency 15; cleanup reserve 30. Each repair has at most 45 minutes including reevaluation. At 7h30 begin cleanup and start no new work. Unused repair budget may support declared work but never a third repair.

No automatic retry for design failure, evaluator mismatch, ordinary crash or usage exhaustion. A single known API stream-stall retry may follow the other scenarios' exact configured signature within the same step/deadline, retaining the original event. Contaminated evidence or evaluator failure blocks conformance claims, never authorizes access to hidden material.

Primary allocations are H-02 automatic Bootstrap/idempotence, H-04 scaffold, P-01 greenfield Setup, ST-01 explicit Stealth-disabled, standard image plus external-image lifecycle slice, T-02 Feature type, I-05/D-03 public navigation, Developer-authored verification and independent conformance. Doctor, Target identity, Flows, reviews, acceptance and cleanup carry explicitly scoped supporting assertions, not blanket coverage of whole inventory rows.

[Assemble the public Booley QA suite implementation handoff](https://github.com/boldaxolotl/booley/issues/376) must encode these contracts in the approved format, preserve the short external-image allocation, reconcile all atomic obligations across the portfolio and expose sufficiency gaps. Taxi's submodule gap is not silently assigned to this empty-origin Project.

### Human decisions

The maintainer agreed to the mode split, single-outstanding MMIO, two repair Tickets/five records each, eight-hour deadline/final 30-minute cleanup, documentation precedence/ALERT_TEST exception, toolchain/Criteria/separate conformance verdict, and deterministic-plus-seeded evaluator with negative controls. They rejected a full external-image rerun in favor of a quick check and requested preparation, not submission, of an OpenTitan documentation report.
