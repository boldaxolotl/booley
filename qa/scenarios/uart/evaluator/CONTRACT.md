# UART public oracle derivation

This is the public-contract derivation used by the independent evaluator. It does not establish qualification. Only the seven permitted documents at `lowRISC/opentitan@615d3c74fadbbf674c8ca05a70f91094989849fb` were retrieved. No implementation, generated register source, tests, DIFs, HJSON or reference model was consulted. Concrete stimuli below are encoding choices within the agreed scope; scenario-defined observation bounds are explicitly separated from documented facts.

All IDs begin `opentitan-uart-clean-room-greenfield.`. The family prefixes below expand that prefix, using dots consistently. A register or field expansion produces independent check/result records, not a single aggregate pass. Source authority is the pinned public corpus plus the accepted scenario MMIO addendum. The public evaluator contract and IDs may live in the suite repository; actual evaluator implementation, materialized cases, seed-private inputs and complete logs remain operator-held and inaccessible from the Developer's Project/runtime.

## Exact register map and access expansion

Source: [pinned register definitions](https://github.com/lowRISC/opentitan/blob/615d3c74fadbbf674c8ca05a70f91094989849fb/hw/ip/uart/doc/registers.md). In each row, reset comparison uses **field-defined values**, not merely the generated top-level reset default. The field table explicitly marks some values `x`; do not convert those to zero. The accepted reset-FIFO behavior is tested independently as a behavioral operation.

| Register | Offset | Defined-bit mask | Reset comparison | Field/access expansion |
|---|---:|---:|---|---|
| INTR_STATE | 0x00 | 0x000001ff | 0x101 under mask 0x1ff | bits 0 tx_watermark, 1 rx_watermark, 8 tx_empty RO; bits 2 tx_done, 3 rx_overflow, 4 rx_frame_err, 5 rx_break_err, 6 rx_timeout, 7 rx_parity_err RW1C |
| INTR_ENABLE | 0x04 | 0x000001ff | 0 under 0x1ff | Same nine names/bit positions, all RW |
| INTR_TEST | 0x08 | 0x000001ff | Reads zero by addendum | Same nine names/bit positions, all WO; write-one forces corresponding INTR_STATE bit |
| ALERT_TEST | 0x0c | 0x00000001 | Reads zero | fatal_fault bit 0 WO in docs; accepted addendum replaces alert with acknowledged no-op |
| CTRL | 0x10 | 0xffff03f7 | 0 under 0xffff03f7 | TX[0], RX[1], NF[2], SLPBK[4], LLPBK[5], PARITY_EN[6], PARITY_ODD[7], RXBLVL[9:8], NCO[31:16], all RW |
| STATUS | 0x14 | 0x0000003f | 0x3c under **0x3c**, not 0x3f | TXFULL[0], RXFULL[1] have reset x; TXEMPTY[2], TXIDLE[3], RXIDLE[4], RXEMPTY[5] reset 1; all RO |
| RDATA | 0x18 | 0x000000ff | RDATA[7:0] reset x, not scored | RO data pop on accepted read; reserved bits zero |
| WDATA | 0x1c | 0x000000ff | Reads zero | WDATA[7:0] WO; enqueue only with low-byte strobe |
| FIFO_CTRL | 0x20 | 0x000000ff | RW fields reset zero; WO fields read zero | RXRST[0], TXRST[1] WO; RXILVL[4:2], TXILVL[7:5] RW |
| FIFO_STATUS | 0x24 | 0x00ff00ff | TXLVL and RXLVL reset x, not scored as numeric reset CSR | TXLVL[7:0], RXLVL[23:16] RO; behavior observes fill level |
| OVRD | 0x28 | 0x00000003 | 0 under 0x3 | TXEN[0], TXVAL[1] RW |
| VAL | 0x2c | 0x0000ffff | RX[15:0] reset x, not scored | RO newest 16 oversampled RX values; newest bit 0 |
| TIMEOUT_CTRL | 0x30 | 0x80ffffff | 0 under 0x80ffffff | VAL[23:0], EN[31] RW |

For each register `R` above, materialize `UART-REG.R.offset`, `UART-REG.R.reserved-read`, `UART-REG.R.reserved-write`, and `UART-REG.R.reset-defined` when it has defined scored reset fields. Reserved-mask expectation is `(~defined_mask) & 0xffffffff`; writes never create reserved-bit readback. No numeric reset assertion is generated for an all-x data register. Reads/writes retain request, response, before/after state and exact commit/evaluator/case identities.

For each RW field `R.F`, materialize `UART-REG.R.F.rw.v<VALUE>` for the concrete value set below. Write using a mask preserving unrelated legal fields, read back and prove no cross-field corruption. Reset/reestablish legal baseline between cases, because control writes can change UART behavior. Do not combine SLPBK and LLPBK or assign meanings to reserved control encodings.

| Field class | Concrete value set |
|---|---|
| Every single-bit RW field | 0, 1 |
| CTRL.RXBLVL | 0, 1, 2, 3 |
| CTRL.NCO | 0x0000, 0x0001, 0x2000, 0x3000, 0x4000, 0x5555, 0xaaaa, 0xffff for register access; serial timing tests use the three settings below |
| FIFO_CTRL.RXILVL | 0, 1, 2, 3, 4, 5, 6; encoding 7 excluded as reserved |
| FIFO_CTRL.TXILVL | 0, 1, 2, 3, 4; encodings 5–7 excluded as reserved |
| TIMEOUT_CTRL.VAL | 0, 1, 0x555555, 0xaaaaaa, 0xffffff for access; timing behavior uses positive value 32 |

For every RO field generate `UART-REG.R.F.ro-write` using all-one writes and retained behavioral state; for WO fields generate `UART-REG.R.F.wo-read`. For each INTR_STATE event bit generate `.w1c-zero`, `.w1c-one`, `.w1c-masked` and `.w1c-isolation` after provoking the event. For RO level bits, clear attempts must not suppress a persisting level condition. Each INTR_ENABLE and INTR_TEST bit has separate identity/enable/mask/inject checks. For byte-strobe checks enumerate all 16 masks `s0` through `s15`; effect is permitted only in enabled lanes. Read strobe independence uses that same set. Address rejection expands each offset plus 1/2/3, unmapped aligned 0x34, 0x100, 0xfffffffc and each valid offset OR 0x100/0x10000/0x80000000; these are concrete anti-truncation examples, not exhaustive address-space proof. All invalid operations expect error/zero/no effects by addendum.

## Exact FIFO, interrupt and break expansions

Source: [FIFO_CTRL thresholds](https://github.com/lowRISC/opentitan/blob/615d3c74fadbbf674c8ca05a70f91094989849fb/hw/ip/uart/doc/registers.md#fifo_ctrl), [interrupt behavior](https://github.com/lowRISC/opentitan/blob/615d3c74fadbbf674c8ca05a70f91094989849fb/hw/ip/uart/doc/theory_of_operation.md#interrupts).

| Direction | Encoding → level | Materialized boundary cases and oracle |
|---|---|---|
| TX | 0→1, 1→2, 2→4, 3→8, 4→16 | `UART-WATERMARK.tx.e<E>.n<N>` at N=level−1, level, level+1; asserted iff N<level. Disable TX while filling/reading capacity. |
| RX | 0→1, 1→2, 2→4, 3→8, 4→16, 5→32, 6→62 | `UART-WATERMARK.rx.e<E>.n<N>` at N=level−1, level, level+1; asserted iff N≥level. |

TX capacity cases are `.UART-FIFO.tx.n0/n1/n31/n32` (omit the initial dot when joining prefix); RX cases use n0/n1/n63/n64. Keep TX disabled to measure capacity, then enable and drain in order. RX overflow injects byte 65 after 64 known bytes, expects dropped new byte plus rx_overflow, then drains exactly the original sequence, clears and receives a good byte. The corpus explicitly specifies RX overflow/drop; it does **not** define a TX-overflow interrupt or an unambiguous write-to-full TX FIFO disposition. Do not invent one; FIFO overflow assertions must be RX-specific unless an addendum explicitly decides full-TX-write semantics.

All 256 data values produce `UART-TXRX.tx.b00`…`.bff` and corresponding `.rx.b00`…`.bff`, supplemented by back-to-back/full-duplex cases. TX serial bits are START=0, eight LSB-first data bits, optional parity then STOP=1; idle=1. Parity cases explicitly include 0x00 and 0x01 (the two data parity classes) under disabled/even/odd, and incorrect parity injections under both enabled modes. The Reception prose conditions FIFO insertion on a valid stop and correct optional parity; data/error oracles must follow that precise description unless a future addendum changes it.

For `UART-ERROR.break.e<E>.p<P>`, E=0/1/2/3 corresponds to 2/4/8/16 character-times; P=0/1 yields 10/11 bit-times per character. Hold RX continuously low below the threshold and beyond it, then return high for at least half a bit-time before a second break. Retain only one break event per continuous break even after W1C; frame errors may recur each character-time. Exact threshold comparison in theory is “more than”, so do not require the IRQ exactly on the equality clock edge. Timing margins and synchronizer uncertainty are discussed below. The theory mentions `STATUS.BREAK` and `INTR_STATE.BREAK`, but the register map exposes neither name: do not invent STATUS bits; use INTR_STATE.rx_break_err bit 5. Record this additional corpus inconsistency separately from the three requested report items.

Timeout uses VAL=32, FIFO nonempty. Exercise EN=0 and EN=1, a read changing depth, a received character changing depth, and a dropped character while full. The timer resets on FIFO-depth change or timeout event; a dropped character while full does not reset it. Clearing followed by good-character recovery remains mandatory. These are separate cases under `UART-ERROR.timeout.*`, not a single enabled/disabled pass.

## Baud model and selected numerical cases

The [baud documentation](https://github.com/lowRISC/opentitan/blob/615d3c74fadbbf674c8ca05a70f91094989849fb/hw/ip/uart/doc/theory_of_operation.md#setting-the-baud-rate) defines a 16-bit NCO and accumulator-overflow 16x tick. Therefore `baud = f_clk * NCO / 2^20`; oversample period is `65536/NCO` source clocks and bit period is `1048576/NCO`. No source fixes the accumulator's initial phase or an external-pin pipeline delay.

Choose f_clk=50 MHz (20 ns clock) and the following deterministic values. These are exact, reviewable **test choices**, not additional hardware requirements; all rates are below the README's stated 1 Mbps guarantee. The accepted requirement needs two exact-divider and one fractional setting; these satisfy it.

| Case | NCO | 16x tick interval in source clocks | Bit interval | Baud |
|---|---:|---|---|---:|
| UART-BAUD.exact-a | 0x4000 = 16384 | 4 | 64 clocks = 1280 ns | 781250 |
| UART-BAUD.exact-b | 0x2000 = 8192 | 8 | 128 clocks = 2560 ns | 390625 |
| UART-BAUD.fractional | 0x3000 = 12288 | 5 or 6 | 85 or 86 clocks; mean 256/3 | 585937.5 |

Model accumulator phase as an unknown legal integer A in [0,65535]. A tick count over n source clocks is `floor((A+n*NCO)/65536)−floor(A/65536)`. Use a **single consistent phase** over the complete observation, not a new phase per bit. With constant NCO and a fixed output latency, relative TX bit boundaries after an observed START must match the 16-tick cadence: at the exact settings each bit lasts exactly 64 or 128 source clocks. At the fractional setting the successive bit lengths are a phase rotation of 85,85,86 source clocks; every three complete bit periods total 256. Use 0x55/0xaa and adjacent complementary bytes so sufficient boundaries are externally observable, and retain the full waveform. The interval from request acceptance to first START is not fixed by this derivation.

For NCO=0, the model has zero new ticks; assert no TX progress during a declared finite observation interval with queued TX data. The finite test horizon is 4096 source clocks (32 bit-times of the slowest selected setting); this witnesses the requirement over that interval, not proof of forever. Record it as an evaluator stimulus/window choice. It does not authorize assuming a particular CTRL-write-to-pin latency.

For RX, the documented centering procedure checks low again after eight oversample ticks and then samples each data bit at 16-tick intervals; the STOP center is +144 ticks from centered START without parity and +160 with parity. Drive matching nominal rate with start phases at 0, 1/4, 1/2 and 3/4 of a 16x period relative to the source clock, keeping data transitions far from nominal bit centers. False starts shorter than half a bit-time must be ignored once detected as such. NF=1 uses the documented 3-tap repetition filter and ignores one source-clock noise. NF=0 removes that filter but does not remove the half-bit false-start rejection: do not expect every one-clock glitch to produce a character when NF is disabled.

## Defensible timing observations and unresolved bounds

**Already fixed and directly scoreable:** MMIO acceptance within four clocks, response within four clocks, exactly-once side effects at acceptance, stable stalled response, reset held at least four clocks then edge release (scenario addendum); steady-state bit cadence and 8/16 oversample spacing (corpus); break threshold character counts and half-bit rearm interval (corpus). The timeout discussion explicitly admits phase-dependent variation and gives an example reduced by 1.5 baud periods. It supplies no universal upper bound measured from the external RX pin or MMIO acceptance.

**Not established by these documents:** maximum TX launch delay after enqueue/enable, RX pin synchronizer depth and filter-to-VAL pipeline placement, maximum receive-to-FIFO/IRQ visibility latency, exact INTR_TEST level-bit force duration, reset or CTRL-write accumulator phase, and a universal timeout response bound. The RX half-bit centering fact is not a promise that external RX sampling occurs within a guessed two/three clocks. Treating any convenient finite wait as a product-failure deadline would add hidden semantics.

The **public scenario addendum** supplies architecture-independent observation bounds under steady control. These are scenario requirements, not facts inferred from the corpus. Let B be the longest bit interval for the selected NCO: 64, 128 or 86 source clocks for exact-a, exact-b or fractional. Reset, NCO/control changes begin a new observation epoch; no case changes control mid-window except cases explicitly testing that transition.

1. **RX synchronization and visibility:** an externally driven RX transition reaches the sampling path within B/4 source clocks (round up where nonintegral). This is four nominal oversample periods, an external latency bound rather than a prescribed number of synchronizer stages. A modeled completed character/error becomes visible in FIFO/status/IRQ within B further clocks. False-start/noise tests retain the documented half-bit and 3-tap behavior, not a new filtering algorithm.
2. **VAL sampling:** normal mode, RX enabled, NF=0 and both loopbacks disabled. Across a complete pattern observation, allow one consistent synchronization delay d in [0, ceil(B/4)] source clocks and a consistent legal NCO phase. VAL contains the last 16 samples of that delayed RX, newest at bit zero, as of MMIO read acceptance. Do not independently refit delay for each sample/read. A constant RX pattern must settle to the corresponding all-zero/all-one VAL within 16 oversample ticks plus ceil(B/4) clocks, then be observable under the four-clock MMIO response bound. Testing NF's placement relative to VAL is avoided because the corpus does not specify it.
3. **TX liveness:** first START appears within 2B clocks after a byte is eligible (TX enabled, nonzero NCO, queued data, no reset/override/loopback). With another queued byte after a completed frame, the next START appears within 2B. Once START appears, the exact accumulator-derived bit cadence still applies; the liveness allowance cannot hide a wrong baud rate. The known-input TX serial checker follows observed START, so no reset-phase or internal pipeline equivalence is imposed.
4. **INTR_TEST:** write-one forces each selected level state/IRQ for at least one complete source-clock interval from request acceptance; an independent per-clock IRQ monitor observes this with that bit enabled. When the true level condition is false, the injected force ends within B clocks and live status is restored. If the true condition remains active, it stays asserted. Event bits remain latched until W1C. This avoids a new read-to-clear policy and does not require a pending bus response to complete before observing the forced IRQ. Byte masks and no-cross-bit effects still apply.

The finite limits are conservative test-contract choices relative to the selected serial rates, not product promises derived from an implementation. Publish them in the same addendum the Developer receives. For observations not governed by these explicit bounds or documented timing requirements, reaching an operational timeout produces a blocked result rather than proof of an RTL timing defect.

For VAL, steady all-high/all-low input eventually yielding 0xffff/0x0000 is useful but cannot alone prove ordering. A known transition-rich input and consistent sample-phase/latency alignment is needed to check newest bit 0. A bounded alignment window requires the above public latency decision. Do not fit an arbitrary distinct delay per sample to force a match.

## Deterministic seed and case materialization

The coordinator supplies one 128-bit run seed as exactly 32 lowercase hexadecimal characters. Store it only in the operator-controlled run declaration and materialized manifest; the implementing Developer receives neither seed nor generated cases. The evaluator delegate receives the frozen materialized cases, not authority to regenerate them. A supplied seed with any other length/alphabet blocks preparation. This is an encoding choice for reproducibility, not a UART behavioral requirement.

Use counter-based SHA-256 without a library-specific pseudorandom generator. For family F, supplement index i and block index j, hash the following exact UTF-8/ASCII sequence, including its final LF:

```text
booley.qa.uart.seed.v1\n
<32-lowercase-hex-seed>\n
<F>\n
<i-as-8-lowercase-hex-digits>\n
<j-as-8-lowercase-hex-digits>\n
```

Here each displayed `\n` means one LF byte, not a literal backslash and n, and the display's line breaks do not add further bytes. F is the exact uppercase family name in the table below. i ranges 0–7 and j starts at 0. Concatenate raw 32-byte digest blocks in increasing j. Bytes are consumed in increasing offset; multi-byte values are unsigned **little-endian**. Let u0/u1/u2/u3 be the first four little-endian 32-bit words; bytes beginning at offset 16 supply payload bytes, continuing into subsequent blocks. For each supplement choose an existing legal deterministic template from that family's lexicographically sorted, fully expanded mandatory-case ID list at index `u0 mod list_length`. Keep the selected template's setup, checks, corners, oracle and recovery. Change only the allowed stimulus parameters in the following table. Where a parameter does not exist in that template, consume the bytes but leave the template unchanged; a duplicate stimulus is allowed and remains separately recorded.

| Family F | Supplemental stimulus choices; never replacements for mandatory cases |
|---|---|
| UART-TXRX | Replace ordinary payload bytes using the byte stream; choose inter-character idle gap `(u1 mod 3)` complete bit-times where the template permits a gap; keep mandatory back-to-back gap zero. |
| UART-BAUD | Select one of [0x4000,0x2000,0x3000] by `u1 mod 3` for ordinary nonzero-rate templates; keep NCO-zero template zero. Use transition-rich alternating payload starting with 0x55 or 0xaa selected by `u2 mod 2`. |
| UART-PARITY | Preserve parity mode and required data-parity class. Take the next payload byte and flip bit 0 iff needed to match the template's parity class. Wrong-parity templates invert the correctly computed parity bit. |
| UART-FIFO | Replace the initial ordered byte sequence using the byte stream. Preserve exact selected occupancy, controlled TX activity, overflow/drop operation and reset/drain sequence. |
| UART-WATERMARK | Replace queued byte values only; preserve selected legal encoding and exact below/at/above occupancy. |
| UART-IRQ | Replace payload values used to provoke the selected interrupt where they are immaterial to error class; preserve source bit, mask/test/W1C mode and level/event condition. |
| UART-ERROR | Replace valid-character recovery payloads using the stream; preserve the selected error type, break threshold/parity mode, timeout control/depth-change operations and deliberately invalid bit. |
| UART-FILTER | Select RX start phase from [0,1/4,1/2,3/4] of a nominal 16x period by `u1 mod 4`; preserve one-clock-noise width, false-start duration and filter mode. Use payload stream only for valid-character templates. |
| UART-LOOP | Replace ordinary loopback payload bytes, preserve exactly one enabled loopback and prescribed return to normal operation. |
| UART-OVERRIDE | Replace retained FIFO payload bytes, preserve low/high/release operation and expected preservation. |
| UART-HISTORY | Use the next 16 bytes' low bits as a known transition pattern, held for one 16x sample period per bit, then its complement. Keep normal/NF-off mode, consistent phase/latency and bit-zero-newest oracle. |
| UART-RESET | Replace pre-reset and recovery payload bytes, preserve the selected idle/active/occupied/pending-MMIO reset trigger and reset duration. |

Thus there are exactly **96 supplements**, eight for each of these 12 already required serial families. UART-BUS and UART-REG retain all deterministic expansions and receive no supplements. Supplement IDs are `opentitan-uart-clean-room-greenfield.<F>.seed.i00000000` through `.i00000007`. A supplement never satisfies or removes its original deterministic template's required result. Every seeded variation stays within an already accepted case class; arbitrary reserved encodings, simultaneous loopbacks or undocumented interactions are never synthesized.

Before evaluating any candidate, freeze a complete materialized manifest in ascending full check-ID order. Each entry contains full ID, selected template ID (for supplements), source requirement, concrete parameters/stimulus, reset/setup/recovery actions, exact circuit observations, operational cycle/wall limits and evidence paths. Include protocol/addendum version and digest, evaluator digest, seed and manifest SHA-256. The same bytes, digest and order apply to evaluation-0 and evaluation-1/2. A changed seed, generator, template, case selection or oracle starts another run; it never erases or replaces a failure in this run.

## Operational case limits and timeout observations

Use these exact **execution ceilings**, clipped to the remaining named phase budget and overall cleanup boundary. They are resource controls, not new RTL timing promises:

- Evaluator compile/elaboration and each operator fixture build: 300 seconds wall time per invocation; its phase deadline may end it sooner.
- Each materialized case, including its reset/setup/recovery: 30 seconds wall time and 1,048,576 source-clock cycles, whichever arrives first. A hung MMIO transaction can produce an earlier true failure under its accepted four-clock circuit contract; otherwise exhausting a ceiling is blocked evidence.
- NCO-zero no-progress observation: 4096 source clocks after its established setup, with its ordinary case ceilings still active.
- Timeout-CSR cases: VAL=32 and at most 4096 nominal bit-times of passive observation after each declared FIFO-depth/event operation, also subject to the case ceilings. This horizon is deliberately operational and is **not** a 4096-bit maximum hardware timeout promise.
- Initial controls plus full evaluation share the accepted 30-minute phase; each repair plus complete independent rerun shares that repair's 45-minute limit. No new case starts after the phase deadline or global 7h30 cleanup boundary. Finalize cases not completed as blocked and retain completed outcomes. Maxima do not imply feasibility; they cannot extend any accepted phase budget.

Timeout case IDs are `UART-ERROR.timeout.disabled`, `.enabled`, `.read-depth-reset`, `.receive-depth-reset`, `.event-reset`, `.full-drop-no-reset`, `.w1c` and `.good-recovery`, each with the scenario prefix. Disabled behavior is observed over the finite horizon with a nonempty FIFO; no timeout event is expected while EN=0. Enabled behavior records actual first/subsequent IRQ cycles and FIFO contents. For read/receive/event cases retain the depth-changing or timeout-event timestamp; for full-drop retain the unchanged FIFO depth and dropped byte timestamp. Record predicted reset/nonreset semantics from the documentation separately from observed times. Paired cases start from identical declared state, NCO and driving phase and differ only in the named operation, making timing shifts or lack of shifts inspectable without assuming internal counters.

A trustworthy observed contradiction of documented reset/nonreset, disabled-mode, W1C or FIFO semantics is a failure. Where external observations cannot distinguish a timer reset from permissible phase variation, the reset-semantic check remains blocked rather than being declared passed from a coincidental IRQ time. Missing an enabled IRQ before an operational horizon is blocked; no universal public external-pin/MMIO-to-timeout upper bound has been established. Preserve the complete raw timing trace and result for diagnosis. The approved general IRQ visibility bound applies **after an established modeled event**; it does not invent when the timeout counter's first event must occur.

## Fixed evidence layout

Within the operator-owned run artifact root, retain `evidence/uart/corpus-manifest.json`, `evidence/uart/interface-manifest.json`, and `evidence/uart/evaluator/manifest.json`. The latter holds evaluator identity, seed and full materialized cases. For each E in `evaluation-0`, `evaluation-1`, `evaluation-2` actually attempted, retain `evidence/uart/evaluator/<E>/candidate.json` with exact accepted commit plus build identity and `build.log`. For every full scenario-qualified case ID C, retain:

```text
evidence/uart/evaluator/<E>/cases/<C>/stimulus.json
evidence/uart/evaluator/<E>/cases/<C>/observations.json
evidence/uart/evaluator/<E>/cases/<C>/trace.vcd
evidence/uart/evaluator/<E>/cases/<C>/execution.log
```

`observations.json` records expected/observed values, source-clock times, raw evaluator classification and circuit/operational bound used; each case's append-only result references these files. The exact file format of implementation logs is not a new oracle decision. Keep full operator evidence outside Project/runtime; only up to five accepted bounded diagnostic excerpts go to each repair. Retain those exact excerpts separately in `evidence/uart/repair-1/diagnostics.json` and `repair-2/diagnostics.json` so the Developer's exposure is auditable. Never replace the operator case manifest with the bounded-feedback subset.

## Frozen corpus identities

The seven permitted documents are stored in [spec/corpus/](../spec/corpus/).
[corpus-manifest.json](../spec/corpus-manifest.json) records their pinned source
commit and exact SHA-256 values; use it as the checksum authority.
