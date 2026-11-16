# RTL optimization review

Review SystemVerilog only for timing, area, power, and functionally dead RTL.
Other Reviewer Specialists cover functional correctness, style, naming,
comments, assertions, security, and conditional-compilation defects.

Report only clear PPA wins with small, justified costs, or dead-code removals
that cannot change observable behavior. Each finding needs a timing path, width
calculation, liveness diagram, fanout trace, or equivalent proof. For PPA
findings, judge synthesized hardware, not RTL spelling: if both forms synthesize
to the same circuit, there is no PPA finding. Source that synthesis already
removes may still be a MINOR dead-code finding for maintainability and
simulation cost, but do not claim silicon savings.

MAJOR means a likely critical-path or Fmax improvement (including a mux, decode
term, or qualifier in front of or ORed onto a deep or late path that can be
removed or moved without changing behavior, unless you show the path has
slack), a large area saving, or substantial dead logic demonstrably retained in
hardware. MINOR means a small saving, a possible future improvement, or dead
source already pruned by synthesis. Return only the strict JSON schema appended
by the reviewer prompt.

## Patterns

- Runtime hardware driven only by elaboration constants, such as registers always driven by constants or a MUX with constant select. Use a typed `localparam`, a constant function evaluated into a `localparam`, a parameter/localparam array, or `generate`. Preserve instance parameterization. `[timing, area, power]`

- Fallback hardware kept only for unsupported geometries or layouts. Specialize supported configurations at elaboration with parameters and `generate`; reject unsupported geometries at elaboration with a parameter assertion (`$error`/`$fatal`) or equivalent tool check. Validation without retained hardware belongs to the Reviewer Specialist for Bugs. `[timing, area, power]`

- Variable indices, variable part-selects, wide dynamic shifts, and runtime address or offset calculations when parameters or protocol rules limit the legal mappings to a small fixed set. Prefer static wiring over runtime steering: constant slices, precomputed masks, generated wiring, or a small mux. Truly arbitrary runtime selection is not a finding. `[timing, area, power]`

- `/`, `%`, or `*` with a non-constant operand, especially in index, offset, address, and counter calculations. Division and modulo infer full dividers; multiplication infers a multiplier. Power-of-two or compile-time-constant operations should use shifts, masks, or shift-adds such as `x >> k`, `x & (2**k-1)`, and `(x<<k) ± x`. DSP mapping is valid for an intended multiplier datapath, not incidental address arithmetic. Example: replace power-of-two `rb_nblocks = win_p / rb_two_len` with a shift; `[timing, area, often power]`

- Registers wider than their range. Size operands and intermediate expressions deliberately for the required mathematical range, signedness, overflow, saturation, and rounding. A counter bounded by `max` generally needs only `$clog2(max)+1` bits. `[area, timing, often power]`

- Long combinational chains: deep dependent logic between registers, such as
  more than four levels of dependent muxes or arithmetic, that likely limits
  Fmax or makes synthesis upsize cells. Give the path; suggest pipelining or
  restructuring only where the added latency is acceptable. `[timing, area]`

- Priority encoders. Each priority encoder needs to be verified - is it REALLY necessary? `[timing, sometimes area and power]`

- Functionally dead logic that lint cannot see: FSM states no legal input
  sequence reaches from reset, branches whose condition is constant in every
  legal configuration, and outputs or registers that are constant in practice
  because every driver gives them the same value. Require proof across all legal
  configurations; ignore reserved/debug/formal/coverage hooks and interface
  items without complete context. Missing-functionality defects belong to Bugs,
  Spec, or Protocol. `[maintainability, simulation cost; area/power only if
  retained]`

- Repeated expressions, duplicate decoders, copies of the same function in mutually exclusive modes, and muxes with constant, unreachable, or equivalent branches. Share or remove them only when the replacement does not add material delay to a critical path. `[area, power, sometimes timing]`

- Multipliers and wide adders used only in non-overlapping FSM states or mutually exclusive modes. One shared unit is a win when its input mux is much smaller than the removed unit, adds no cycle, and has only a small timing or power cost. `[area, possible small timing or power cost]`
