# RTL Style Guide

Single source of truth for coding standards. Used by Reviewer Specialists and Developer Agents.

Project-specific overlays may be supplied by the caller or change-request context.

## 1. Comments

| Rule | Severity |
|------|----------|
| Comments must not contradict code — wrong comment is worse than none | MAJOR |
| Explain *why*, not *what* — no `// increment counter` above `cnt <= cnt + 1` | MINOR |
| No style-choice comments (e.g., "using wire instead of macro for safety") — only comment on functionality | MINOR |
| No stale references to removed signals, modules, or behaviors | MINOR |
| Complex logic, FSM transitions, non-obvious bit manipulation must have comments | MINOR |

## 2. Naming & Magic Numbers

| Rule | Severity |
|------|----------|
| No single-letter signals (except `i`, `j`, `k`), no generic names (`data`, `result`, `temp`) — use domain terms | MINOR |
| Consistent naming within a module — no mixing naming styles for the same kind of signal | MINOR |
| A parameter, signal, or port name must say what it holds or sizes — a width parameter names the quantity it sizes (`PKT_CNT_W` for a packet-counter width, not `TOTAL_W`; `BYTE_IDX_W`, not `IDX_W`), and a port names its content (`pkt_cnt_o`, not `total_o`). Flag any name a reader cannot map to its content without reading the logic | MINOR |
| Scope names to their block — a parent module must not reuse a submodule's block-local name unprefixed for its own constant or signal (a top-level `localparam WIDTH` mirroring submodule `fifo`'s `WIDTH` becomes `FIFO_WIDTH`). Bare `WIDTH`, `DEPTH`, `IDX_W` are fine only inside the block they describe | MINOR |
| No magic numbers — derive constants from existing parameters | MINOR |
| No redundant localparams that duplicate package constants | MINOR |
| No ascending bit ranges `[lo:hi]` in ports or signals — use descending `[N-1:0]`. Ascending ranges invert bit order relative to the descending convention used everywhere else, and VPI-based testbenches such as cocotb assume the left index is the MSB when converting an integer | CRITICAL |

## 3. Ifdef & Conditional Compilation

| Rule | Severity |
|------|----------|
| No near-identical `ifdef` paths differing only in names/widths/constants — consolidate via parameterization or runtime muxing | MINOR |

## 4. Arithmetic & Synthesis Cost

| Rule | Severity |
|------|----------|
| No `/`, `%`, or `*` with a non-constant operand unless a real divider/multiplier is intended — these infer dividers/multipliers on the critical path and cost area. When an operand is a power of two or compile-time constant, use a shift / mask / shift-add instead (`x >> k`, `x & (2**k-1)`, `(x<<k) ± x`). DSP-mapped multiply is only for deliberate multiplier datapaths (e.g. modular-mul), never index/offset/counter math | MAJOR |
