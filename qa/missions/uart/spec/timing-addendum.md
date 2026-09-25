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

To complete an executable conformance contract, the following **approved public scenario addendum** supplies architecture-independent observation bounds under steady control. The maintainer approved these scenario requirements; they are not facts inferred from the corpus. Let B be the longest bit interval for the selected NCO: 64, 128 or 86 source clocks for exact-a, exact-b or fractional. Reset, NCO/control changes begin a new observation epoch; no case changes control mid-window except cases explicitly testing that transition.

1. **RX synchronization and visibility:** an externally driven RX transition reaches the sampling path within B/4 source clocks (round up where nonintegral). This is four nominal oversample periods, an external latency bound rather than a prescribed number of synchronizer stages. A modeled completed character/error becomes visible in FIFO/status/IRQ within B further clocks. False-start/noise tests retain the documented half-bit and 3-tap behavior, not a new filtering algorithm.
2. **VAL sampling:** normal mode, RX enabled, NF=0 and both loopbacks disabled. Across a complete pattern observation, allow one consistent synchronization delay d in [0, ceil(B/4)] source clocks and a consistent legal NCO phase. VAL contains the last 16 samples of that delayed RX, newest at bit zero, as of MMIO read acceptance. Do not independently refit delay for each sample/read. A constant RX pattern must settle to the corresponding all-zero/all-one VAL within 16 oversample ticks plus ceil(B/4) clocks, then be observable under the four-clock MMIO response bound. Testing NF's placement relative to VAL is avoided because the corpus does not specify it.
3. **TX liveness:** first START appears within 2B clocks after a byte is eligible (TX enabled, nonzero NCO, queued data, no reset/override/loopback). With another queued byte after a completed frame, the next START appears within 2B. Once START appears, the exact accumulator-derived bit cadence still applies; the liveness allowance cannot hide a wrong baud rate. The known-input TX serial checker follows observed START, so no reset-phase or internal pipeline equivalence is imposed.
4. **INTR_TEST:** write-one forces each selected level state/IRQ for at least one complete source-clock interval from request acceptance; an independent per-clock IRQ monitor observes this with that bit enabled. When the true level condition is false, the injected force ends within B clocks and live status is restored. If the true condition remains active, it stays asserted. Event bits remain latched until W1C. This avoids a new read-to-clear policy and does not require a pending bus response to complete before observing the forced IRQ. Byte masks and no-cross-bit effects still apply.

The finite limits are conservative test-contract choices relative to the selected serial rates, not product promises derived from an implementation. Publish them in the same addendum the Developer Agent receives. For observations not governed by these explicit bounds or documented timing requirements, reaching an operational timeout produces a blocked result rather than proof of an RTL timing defect.

For VAL, steady all-high/all-low input eventually yielding 0xffff/0x0000 is useful but cannot alone prove ordering. A known transition-rich input and consistent sample-phase/latency alignment is needed to check newest bit 0. A bounded alignment window requires the above public latency decision. Do not fit an arbitrary distinct delay per sample to force a match.

## Approved timeout rule — revision 2 (09 SEP 2026)

Restrict these timeout comparisons to NCO=0x4000, NF=0, normal RX, RX enabled,
TIMEOUT_CTRL.EN=1 and VAL=32, with a nonempty RX FIFO and no simultaneous reset,
control write, FIFO reset or competing timeout/depth event. B=64 source clocks.
A timeout event shall become visible on enabled IRQ bit 6 between 30B and 34B
source clocks, inclusive, after the event that restarts the timer. This ±2B
allowance is an approved scenario tolerance, chosen to admit the documented
1.5-bit phase example plus observation latitude. The corpus does not establish
it as a universal hardware fact.

Timer restart events are a successful nonempty RDATA read at MMIO acceptance,
a successfully received character changing FIFO depth, and a timeout event.
TIMEOUT_CTRL enable begins the first epoch at write acceptance. Disabling the
timer suppresses events. W1C acknowledges an event without restarting the timer.
A character discarded because the RX FIFO is full does not restart the timer.
Periodic events while depth stays nonzero obey the same 30B–34B interval.

## External observation and comparisons

Observe IRQ transitions every source clock, enabled throughout the experiment;
clear bit 6 before its next possible event. Record bus acceptance times and
bracket received-byte depth changes with consecutive FIFO_STATUS read acceptance
samples at most eight clocks apart. For a reset interval [L,U], the accepted
next-event window is [L+30B,U+34B]. An absent event past U+34B is a failure of this
new public bound, provided execution is otherwise healthy. Simulator/resource
failures remain blocked.

Run each trial from a fresh identical setup, first establishing an event E0.
Clear that event, then intervene sufficiently far from both window boundaries:
Issue RDATA near E0+8B (record actual acceptance); or finish a received character near E0+18B. The no-reset expected
window is [E0+30B,E0+34B]; a reset event at [L,U] creates the later window above.
Require these windows to be disjoint; otherwise block the comparison. Assert
FIFO contents/depth before and after so the intended event actually occurred.
The received/read cases start with two frozen payload bytes, so a single read
keeps the FIFO nonempty. The discarded case starts at full depth 64. Capture the
full payload in the materialized manifest. Inability to maintain the eight-clock
observation spacing is blocked evidence, not a new MMIO timing requirement.

To distinguish event reset from acknowledgement reset, repeat with W1C accepted
at E0+2B and E0+10B. The next event must stay in the E0-relative window in both
trials; do not use the W1C time as the reference. Record all initial failures,
paired observations and recovery results without replacing earlier evidence.

This revision was approved by the maintainer. It is a scenario requirement, not
a universal OpenTitan timing claim. The new public-input and evaluator digests
require a new frozen manifest; existing run manifests must not be replaced.
