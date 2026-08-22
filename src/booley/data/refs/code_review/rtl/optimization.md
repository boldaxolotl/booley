# RTL optimization review

Review SystemVerilog only for timing, area, and power. Other agents cover functional correctness, style, naming, comments, assertions, security, and conditional compilation.

Report only clear PPA wins with small, justified costs. Each finding needs a timing path, width calculation, liveness diagram, or equivalent proof. Judge synthesized hardware, not RTL spelling. If both forms synthesize to the same circuit, there is no finding.

MAJOR means a likely critical-path or Fmax improvement, or a large area saving. MINOR means a small saving or a possible future improvement. Return only the strict JSON schema appended by the reviewer prompt.

## Patterns

- Runtime hardware driven only by elaboration constants, such as registers always driven by constants or a MUX with constant select. Use a typed `localparam`, a constant function evaluated into a `localparam`, a parameter/localparam array, or `generate`. Preserve instance parameterization. `[timing, area, power]`

- Fallback hardware kept only for unsupported geometries or layouts. Specialize supported configurations at elaboration with parameters and `generate`; reject unsupported geometries at elaboration with a parameter assertion (`$error`/`$fatal`) or equivalent tool check. Validation without retained hardware belongs to the Bugs agent. `[timing, area, power]`

- Variable indices, variable part-selects, wide dynamic shifts, and runtime address or offset calculations when parameters or protocol rules limit the legal mappings to a small fixed set. Prefer static wiring over runtime steering: constant slices, precomputed masks, generated wiring, or a small mux. Truly arbitrary runtime selection is not a finding. `[timing, area, power]`

- `/`, `%`, or `*` with a non-constant operand, especially in index, offset, address, and counter calculations. Division and modulo infer full dividers; multiplication infers a multiplier. Power-of-two or compile-time-constant operations should use shifts, masks, or shift-adds such as `x >> k`, `x & (2**k-1)`, and `(x<<k) ± x`. DSP mapping is valid for an intended multiplier datapath, not incidental address arithmetic. Example: replace power-of-two `rb_nblocks = win_p / rb_two_len` with a shift; `[timing, area, often power]`

- Registers wider than their range. Size operands and intermediate expressions deliberately for the required mathematical range, signedness, overflow, saturation, and rounding. A counter bounded by `max` generally needs only `$clog2(max)+1` bits. `[area, timing, often power]`

- Priority encoders. Each priority encoder needs to be verified - is it REALLY necessary? `[timing, sometimes area and power]`

- Repeated expressions, duplicate decoders, copies of the same function in mutually exclusive modes, and muxes with constant, unreachable, or equivalent branches. Share or remove them only when the replacement does not add material delay to a critical path. `[area, power, sometimes timing]`

- Wide registers whose values are needed at different times may share the same storage. Check every pipeline stage and operating mode. Two values can share a register if one is no longer needed whenever the other is stored or used. Reading the old value and replacing it on the same clock edge is safe because downstream logic sees the value from before the edge. Moving a load by one cycle may also prevent the lifetimes from overlapping. Ignore small counters, flags, and other control registers. Show when each value is stored, used, and no longer needed; include the bits saved, schedule or throughput changes, and any cycle where both values would need to be written. `[area, power, possible timing or latency cost]`

- Multipliers and wide adders used only in non-overlapping FSM states or mutually exclusive modes. One shared unit is a win when its input mux is much smaller than the removed unit, adds no cycle, and has only a small timing or power cost. `[area, possible small timing or power cost]`
